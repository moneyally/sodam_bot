"""AI 요금 계산 (달러) · 하루 달러 예산 · 방 하루 한도. 요금표는 OpenAI 공개 가격 (2026-09 확인, 1M 토큰당): 바뀌면 PRICES 만 고친다.

counters(chat_id=0) 의 모델별 키 m:<모델>:in/cached/out 으로 정확히 계산 (llm._record, 2026-09-27 부터 기록).
그 전 날짜(모델 구분 없음)는 합계 tokens/prompt_tokens/cached_tokens 로 '전부 기본 모델' ~ '전부 mini' 범위만.
달러 사용량(USD·ROOM_USD)은 2026-09-28 부터 기록.
"""
from __future__ import annotations

import math

from .settings import register_setting

# 모델: (입력, 캐시 입력, 출력) $/1M 토큰
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),   # 뉴스 요약 선택(.env NEWS_MODEL) — OpenAI 모델 문서 2026-09-29 확인
    # 이미지 모델 (OpenAI 모델 문서 2026-09-27 확인, 두 모델 같은 값): 글 입력 $5 · 캐시 $1.25, 사진 입력 $8 · 캐시 $2,
    # 이미지 출력 $30 (글 출력은 없음). 이미지 요금은 장당이 아니라 토큰. llm.image → _record 는 usage.input_tokens(글+사진 합계)·
    # 캐시·출력(total-input) 만 넘겨서 글/사진 입력을 못 나눈다 → 입력은 더 비싼 '사진 입력' 값으로 (보수적, 글 프롬프트
    # 수백 토큰 × $3/1M 차이라 과다 청구는 1장에 $0.001 안쪽). 장당 토큰 수는 모델이 정함 (예: 출력 1,056토큰이면 ≈ $0.032).
    "gpt-image-2.5-flare": (8.00, 2.00, 30.00),
    "gpt-image-2.5-sunburst": (8.00, 2.00, 30.00),
    "text-embedding-3-small": (0.02, 0.02, 0.0),   # 의미 검색 색인 (sodam/semsearch.py) — 메시지 2천 개 ≈ $0.001
}
IMAGE_PREFIX = "gpt-image"
WEB_SEARCH_PER_CALL = 0.01       # 웹 검색 도구 1번 ($10 / 1천 번)
# 영상 모델: 초당 $ (sodam/video.py). Veo https://ai.google.dev/gemini-api/docs/pricing (720p, 소리 포함, 만들어진 것만 청구) ·
# xAI https://docs.x.ai/developers/models/grok-imagine-video(-1.5) — 2026-09-30 확인. 모르는 영상 모델 = 이 표의 가장 비싼 값.
VIDEO_PER_SEC: dict[str, float] = {
    "veo-3.1-lite-generate-preview": 0.05,
    "veo-3.1-fast-generate-preview": 0.10,
    "veo-3.1-generate-preview": 0.40,
    "grok-imagine-video-1.5-lite": 0.02,   # 2026-10-03 xAI 요금표 (글·사진 → 영상만, 영상 입력 X)
    "grok-imagine-video": 0.05,
    "grok-imagine-video-1.5": 0.08,
}


def video_usd_micro(model: str, seconds: int) -> int:
    """영상 한 개 요금 (정수 마이크로달러) = 초당 요금 × 초."""
    per = VIDEO_PER_SEC.get(model, max(VIDEO_PER_SEC.values()))
    return math.ceil(round(per * max(0, seconds) * MICRO, 6))


def price_of(model: str) -> tuple[float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    base = max((m for m in PRICES if model.startswith(m + "-20")), key=len, default=None)   # 날짜 붙은 스냅샷 이름
    return PRICES.get(base) if base else None


def token_cost(model: str, inp: int, cached: int, out: int) -> float | None:
    p = price_of(model)
    if p is None:
        return None
    return ((inp - cached) * p[0] + cached * p[1] + out * p[2]) / 1_000_000


def by_model(counters: dict[str, int]) -> dict[str, dict]:
    """counters(key→n, chat_id=0 하루치) → 모델별 {in, cached, out, calls, usd(None=요금 모름)}."""
    out: dict[str, dict] = {}
    for k, n in counters.items():
        if k.startswith("m:") and k.count(":") >= 2:
            model, field = k[2:].rsplit(":", 1)
            out.setdefault(model, {"in": 0, "cached": 0, "out": 0, "calls": 0})[field] = n
    for m, d in out.items():
        d["usd"] = token_cost(m, d["in"], d["cached"], d["out"])
    return out


def total_range(prompt: int, cached: int, total: int, main: str, mini: str) -> tuple[float, float]:
    """모델 구분이 없는 합계로 (전부 mini, 전부 기본 모델) 요금 범위."""
    out = max(0, total - prompt)
    lo = token_cost(mini, prompt, cached, out) or 0.0
    hi = token_cost(main, prompt, cached, out) or 0.0
    return lo, hi


# ── 하루 예산(달러) · 방 한도 ─────────────────────────────
# 캐시로 읽은 입력은 10% 값인데 토큰 수로 세면 전액처럼 잡혀서(2M 토큰 = 실제 $1~3) 예산은 달러로 센다.
# counters 에 정수 마이크로달러(1 = $0.000001): 전체는 chat_id=0 의 USD, 방(1:1 포함)마다 ROOM_USD.
MICRO = 1_000_000
USD = "usd_micro"
ROOM_USD = "room_usd_micro"
DEFAULT_USD_BUDGET = 8.0          # .env DAILY_USD_BUDGET (0 = 달러 예산 끔)
PLAN_KEY = "ai_usd_plan"          # chat_state: 오너가 정한 방 하루 요금제(센트). 방 설정이 아니라서 방 관리자는 못 바꿈
PLAN_CENTS = (50, 150, 300, 500, 1000)
DEFAULT_PLAN_CENTS = 1000         # 요금제를 안 정한 방·1:1 = 하루 $10 (사용자 결정 2026-09-27: 막지 말고 넉넉히)
PCT_KEY = "ai_room_budget_pct"    # 방 관리자 설정: 요금제의 몇 %까지 쓸지 (기본=상한 100 → 줄이기만 가능)

register_setting(PCT_KEY, 100, "방 하루 AI 사용 한도(%)", range_=(10, 100))


def usd_micro(model: str, inp: int, cached: int, out: int, fallback: str = "") -> int:
    """정수 마이크로달러(올림). 요금표에 없는 모델은 기본 모델 요금, 그것도 없으면 가장 비싼 요금 (보수적으로).
    이미지 모델은 PRICES 에 토큰 요금으로 있음. 요금표에 없는 gpt-image-* 는 아는 이미지 요금 중 가장 비싼 값
    (.env 로 다른 이미지 모델을 쓰면 PRICES 에 추가할 것)."""
    usd = token_cost(model, inp, cached, out)
    if usd is None and model.startswith(IMAGE_PREFIX):   # 요금표에 없는 새 이미지 모델 → 아는 이미지 요금 중 가장 비싼 값
        usd = max(token_cost(m, inp, cached, out) or 0.0 for m in PRICES if m.startswith(IMAGE_PREFIX))
    if usd is None and fallback:
        usd = token_cost(fallback, inp, cached, out)
    if usd is None:
        # 대화 모델 중 가장 비싼 값 (이미지 출력 $30 은 대화 모델에 쓰면 2배 과다라 뺌)
        usd = max(token_cost(m, inp, cached, out) or 0.0 for m in PRICES if not m.startswith(IMAGE_PREFIX))
    return math.ceil(round(usd * MICRO, 6)) if usd > 0 else 0   # round: 부동소수 오차로 1 더 올림 방지


def fmt_usd(micro: int, digits: int = 2) -> str:
    return f"${micro / MICRO:,.{digits}f}"


def plan_label(cents: int) -> str:
    return f"${cents / 100:,.2f}"


async def room_plan_cents(db, chat_id: int) -> int:
    raw = await db.get_state(chat_id, PLAN_KEY)
    return raw if isinstance(raw, int) and raw in PLAN_CENTS else DEFAULT_PLAN_CENTS


async def room_cap_micro(db, chat_id: int, settings: dict | None = None) -> int:
    """방 하루 달러 한도 = 오너 요금제 × 방 관리자 % (저장값이 범위를 벗어나도 10~100% 로 자름)."""
    s = settings if settings is not None else await db.get_settings(chat_id)
    pct = min(max(int(s.get(PCT_KEY) or 100), 10), 100)
    return await room_plan_cents(db, chat_id) * (MICRO // 100) * pct // 100


async def usage(db, day: str, top: int = 8) -> dict:
    """그날 달러 사용량: 전체 + 방별 상위 [(chat_id, 마이크로달러, 한도)] (오너 화면·보고용)."""
    rows = await db._all("SELECT chat_id, n FROM counters WHERE day=? AND key=? ORDER BY n DESC LIMIT ?",
                         (day, ROOM_USD, top))
    return {"usd": await db.counter(day, 0, USD), "tokens": await db.counter(day, 0, "tokens"),
            "rooms": [(r["chat_id"], r["n"], await room_cap_micro(db, r["chat_id"])) for r in rows]}
