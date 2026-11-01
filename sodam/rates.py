"""💱 실시간 USDT ↔ 원 시세 (업자방용).

- 방에서 `50테더`·`249 usdt`·`150000원`·`100만원`·`5만테더` 처럼 치면 소담이 바로 변환해서 답한다
  (방 설정 rate_enabled 켜진 방만, 그룹 메시지 훅). 오탐 방지: 메시지 전체가 "숫자+단위"일 때만.
- `.시세`(별칭 환율·rate): 인자 있으면 변환, 없으면 현재가 "1 USDT = N원". 설정과 무관하게 명령은 항상 됨 (1:1 도).
- AI 도구 rate_convert (panels/rates.py 에서 등록) — "소담아 500테더 얼마야".
- 시세 = 업비트 KRW-USDT(주) → 빗썸(폴백) → 코인게코. 전역 60초 캐시(외부 호출 분당 1회 = 레이트리밋 0.2%).
  업비트 약관상 '복제·배포'는 회색지대라 **참고용**으로만 표시, 지어내지 않음(sanity 800~2500원 밖·전부 실패면 "확인 불가").
  업비트는 Origin 헤더 붙이면 느려지므로 안 붙인다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from . import hooks
from .permissions import Role
from .settings import register_setting

log = logging.getLogger(__name__)

UPBIT = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
BITHUMB = "https://api.bithumb.com/public/ticker/USDT_KRW"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=krw"

MIN_KRW, MAX_KRW = 800.0, 2500.0     # 말도 안 되는 값 버리기 (오픈소스 공통 sanity)
CACHE_TTL = 60                        # 전역 캐시(초) — 외부 호출을 분당 1회로
STALE_OK = 300                        # 갱신 실패해도 이 안이면 직전 값 (초)
TIMEOUT = 5.0
TAIL = " (업비트 기준·참고용)"

register_setting("rate_enabled", False, "💱 시세 조회")   # 토글 (bool) — UI 는 panels/rates.py register_toggle

_cache: dict = {"rate": None, "ts": 0.0}
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None
_last: dict[tuple[int, int], float] = {}   # (방,사람) → 마지막 답 시각 (도배 방지)
USER_GAP = 2.0


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)   # Origin 헤더 없음 (업비트 느려짐 방지)
    return _client


def _sane(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if MIN_KRW <= v <= MAX_KRW else None


async def _fetch_upbit(c: httpx.AsyncClient) -> float | None:
    r = await c.get(UPBIT)
    r.raise_for_status()
    return _sane(r.json()[0]["trade_price"])


async def _fetch_bithumb(c: httpx.AsyncClient) -> float | None:
    r = await c.get(BITHUMB)
    r.raise_for_status()
    return _sane(r.json()["data"]["closing_price"])


async def _fetch_coingecko(c: httpx.AsyncClient) -> float | None:
    r = await c.get(COINGECKO)
    r.raise_for_status()
    return _sane(r.json()["tether"]["krw"])


SOURCES = (("업비트", _fetch_upbit), ("빗썸", _fetch_bithumb), ("코인게코", _fetch_coingecko))


async def fetch_rate() -> float | None:
    """외부 거래소에서 한 번 가져오기 (업비트→빗썸→코인게코 폴백). 캐시 안 씀."""
    c = await _get_client()
    for name, fn in SOURCES:
        try:
            v = await fn(c)
        except Exception as e:   # noqa: BLE001 — 어떤 오류든 다음 소스로
            log.warning("시세 소스 실패 %s: %s", name, e)
            continue
        if v is not None:
            return v
    return None


async def get_rate() -> tuple[float | None, bool]:
    """(1 USDT 당 원, stale). 60초 캐시 → 만료 시 새로. 실패해도 5분 안 캐시는 stale=True 로 씀. 둘 다 없으면 (None, False)."""
    now = time.time()
    async with _lock:
        if _cache["rate"] is not None and now - _cache["ts"] < CACHE_TTL:
            return _cache["rate"], False
        v = await fetch_rate()
        if v is not None:
            _cache["rate"], _cache["ts"] = v, now
            return v, False
        if _cache["rate"] is not None and now - _cache["ts"] < STALE_OK:
            return _cache["rate"], True
        return None, False


# ── 입력 파싱 ─────────────────────────────────────────────
_USDT_UNITS = ("usdt", "테더", "달러", "불", "tether")
_KRW_UNITS = ("원", "won", "krw", "₩")
_UNIT_RE = "|".join(re.escape(u) for u in (*_USDT_UNITS, *_KRW_UNITS))
# 숫자 + (만|억) + 단위. strict=True 면 메시지 전체가 이것뿐일 때만 (오탐 방지).
_BODY = rf"([\d,]+(?:\.\d+)?)\s*(만|억)?\s*({_UNIT_RE})"
_RE_STRICT = re.compile(rf"^\s*{_BODY}\s*[?!.]*$", re.I)
_RE_LOOSE = re.compile(_BODY, re.I)


def parse_query(text: str, *, strict: bool) -> tuple[float, str] | None:
    """-> (금액, 방향). 방향: 'u2k'=USDT→원, 'k2u'=원→USDT. 못 읽으면 None."""
    if not text:
        return None
    m = (_RE_STRICT.match if strict else _RE_LOOSE.search)(text.strip())
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    scale = m.group(2)
    if scale == "만":
        amount *= 10_000
    elif scale == "억":
        amount *= 100_000_000
    unit = m.group(3).lower()
    direction = "k2u" if unit in _KRW_UNITS else "u2k"
    return amount, direction


def _fmt_usdt(v: float) -> str:
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def answer(amount: float, direction: str, rate: float, stale: bool = False) -> str:
    tail = TAIL + (" · 갱신 실패로 직전 값" if stale else "")
    if direction == "u2k":
        return f"{_fmt_usdt(amount)} USDT는 약 {amount * rate:,.0f}원 입니다.{tail}"
    return f"{amount:,.0f}원은 약 {_fmt_usdt(amount / rate)} USDT 입니다.{tail}"


FAIL = "지금 시세를 못 가져왔어요 (거래소 응답 없음). 잠시 뒤 다시 해 줘."


async def reply_for(text: str, *, strict: bool) -> str | None:
    """입력 → 답 문자열 (매칭 안 되면 None). 명령·훅·AI 공통."""
    q = parse_query(text, strict=strict)
    if q is None:
        return None
    rate, stale = await get_rate()
    return FAIL if rate is None else answer(*q, rate, stale)


async def current_line() -> str:
    rate, stale = await get_rate()
    if rate is None:
        return FAIL
    return f"💱 지금 <b>1 USDT = {rate:,.0f}원</b>{TAIL}" + (" · 갱신 실패로 직전 값" if stale else "")


# ── 즉답 (호칭·'얼마'가 붙어도 AI 안 거치고 코드로) ────────
# "소담아 500테더 얼마야" 처럼 소담을 불러도 순수 시세 질문이면 AI 왕복 없이 바로 답한다.
# 호칭·물음말을 떼고 남은 게 '금액+단위'뿐일 때만 (다른 낱말이 섞이면 None → 일반 대화로 AI 로 넘김 = 오탐 0).
_CALL_RE = re.compile(r"소담아|소담이|소담", re.I)
_ASK_RE = re.compile(
    r"얼마에요|얼마예요|얼마냐|얼만가요?|얼마야|얼마는|얼만|얼마|시세가|시세는|시세|환율이|환율은|환율|"
    r"몇\s*원|지금|요즘|현재가?|알려줘|알려|변환해?\s*줘?|변환|바꿔\s*줘?|바꿔|좀|이야|인가요?|해\s*줘|줘", re.I)
_TRAIL_RE = re.compile(r"[\s?!~.]+$")


def fast_body(text: str) -> str | None:
    """호칭·물음말을 뗀 뒤 남은 게 순수 '금액+단위'면 그 조각, 아니면 None."""
    t = (text or "").strip()
    if not t:
        return None
    t = _CALL_RE.sub(" ", t)
    t = _ASK_RE.sub(" ", t)
    t = _TRAIL_RE.sub("", " ".join(t.split())).strip()
    return t if t and _RE_STRICT.match(t) else None


async def fast_reply(svc, bot, msg, text: str) -> bool:
    """그룹에서 호칭+시세질문이면 AI 전에 코드로 즉답. True 면 handlers 가 AI 를 안 부른다."""
    if msg.chat_id > 0 or not msg.from_user or msg.from_user.is_bot:
        return False
    s = await svc.db.get_settings(msg.chat_id)
    if not s.get("rate_enabled"):
        return False
    body = fast_body(text)
    if body is None:
        return False
    # 호칭·물음 없는 '맨 숫자'(50테더)는 그룹훅 on_message 가 전담한다. handlers 가 훅을 asyncio.create_task 로
    # 메인 핸들러와 동시에 돌려서, 여기서도 답하면 _last 쿨다운을 둘 다 통과해 두 번 떴다 (2026-11-01 테더정산방).
    if not (_CALL_RE.search(text) or _ASK_RE.search(text)):
        return False
    now = time.time()
    key = (msg.chat_id, msg.from_user.id)
    if now - _last.get(key, 0.0) < USER_GAP:
        return True   # 쿨다운: 조용히 삼킴 (AI 로 안 넘김)
    out = await reply_for(body, strict=True)
    if out is None:
        return False
    _last[key] = now
    try:
        await msg.reply_text(out, parse_mode="HTML")
    except Exception as e:   # noqa: BLE001
        log.warning("시세 즉답 못 보냄 %s: %s", msg.chat_id, e)
    return True


# ── 그룹 훅 (맨 숫자) ─────────────────────────────────────
async def on_message(svc, bot, msg, role) -> None:
    if msg.chat_id > 0 or not msg.from_user or msg.from_user.is_bot or not (msg.text or "").strip():
        return
    s = await svc.db.get_settings(msg.chat_id)
    if not s.get("rate_enabled"):
        return
    now = time.time()
    key = (msg.chat_id, msg.from_user.id)
    if now - _last.get(key, 0.0) < USER_GAP:
        return
    out = await reply_for(_TRAIL_RE.sub("", (msg.text or "").strip()), strict=True)   # 끝 ?!~. 떼고 (맨 숫자+물음표도)
    if out is None:
        return
    _last[key] = now
    try:
        await msg.reply_text(out, parse_mode="HTML")
    except Exception as e:   # noqa: BLE001
        log.warning("시세 답 못 보냄 %s: %s", msg.chat_id, e)


hooks.add_group_message_hook(on_message)


# ── .시세 명령 (설정 무관·1:1 도) ─────────────────────────
async def c_rate(ctx) -> None:
    argstr = (ctx.argstr or "").strip()
    if not argstr:
        await ctx.reply(await current_line())
        return
    out = await reply_for(argstr, strict=False)
    if out is None:
        await ctx.reply("예: <code>.시세 50테더</code> · <code>.시세 100만원</code> · <code>.시세</code>(현재가)")
        return
    await ctx.reply(out)


def register_commands() -> None:
    """handlers 가 import 뒤 부름 (commands→menu→panels 순환 피함)."""
    from . import commands
    if any(c.fn is c_rate for c in commands.COMMANDS):
        return
    cmd = commands.Cmd(("시세", "환율", "rate"), c_rate, Role.MEMBER,
                       usage="50테더 | 100만원 | (없으면 현재가)", help="USDT↔원 실시간 시세", group="일반", dm_ok=True)
    commands.COMMANDS.append(cmd)
    for name in cmd.names:
        commands._INDEX.setdefault(name.lower(), cmd)
