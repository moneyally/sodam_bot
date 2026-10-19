"""💱 실시간 USDT↔원 시세 (rates.py): python tests/run_all.py rates

파싱(양방향·만/억·오탐)·포맷·sanity·캐시·폴백·rate_enabled 게이트·AI 도구. 외부 API 는 가짜(오프라인).
"""
import asyncio

from fakes import runner

import sodam.__main__  # noqa: F401  (전체 등록)
from sodam import rates
from sodam.panels import rates as rpanel

test, run_all = runner()


class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeClient:
    """url 로 거래소 구분. fail 에 든 거래소는 예외."""
    def __init__(self, upbit=None, bithumb=None, coingecko=None, fail=()):
        self.upbit, self.bithumb, self.coingecko = upbit, bithumb, coingecko
        self.fail = set(fail)
        self.calls = []

    async def get(self, url):
        self.calls.append(url)
        if "upbit" in url:
            if "upbit" in self.fail:
                raise RuntimeError("upbit down")
            return FakeResp([{"trade_price": self.upbit}])
        if "bithumb" in url:
            if "bithumb" in self.fail:
                raise RuntimeError("bithumb down")
            return FakeResp({"data": {"closing_price": self.bithumb}})
        if "coingecko" in self.fail:
            raise RuntimeError("cg down")
        return FakeResp({"tether": {"krw": self.coingecko}})


def _reset(client):
    rates._client = client
    rates._cache.update(rate=None, ts=0.0)
    rates._last.clear()


# ── 파싱 ──────────────────────────────────────────────────
@test
async def parse_both_directions_and_scale():
    assert rates.parse_query("50테더", strict=True) == (50.0, "u2k")
    assert rates.parse_query("249 usdt", strict=True) == (249.0, "u2k")
    assert rates.parse_query("150000원", strict=True) == (150000.0, "k2u")
    assert rates.parse_query("100만원", strict=True) == (1_000_000.0, "k2u")
    assert rates.parse_query("5만테더", strict=True) == (50_000.0, "u2k")
    assert rates.parse_query("1억원", strict=True) == (100_000_000.0, "k2u")
    assert rates.parse_query("0.4테더", strict=True) == (0.4, "u2k")


@test
async def parse_ignores_plain_chat():
    for t in ("안녕하세요", "50명", "밥 50000원 먹었어", "테더 언제 사", "ㅋㅋ 50"):
        assert rates.parse_query(t, strict=True) is None, t
    # 느슨(명령 인자)은 문장 속에서도 뽑음
    assert rates.parse_query("2만달러", strict=False) == (20_000.0, "u2k")


@test
async def format_matches_real_bot():
    # @ggigq_bot 실측값과 동일
    assert rates.answer(50, "u2k", 1346.0).startswith("50 USDT는 약 67,300원 입니다.")
    assert rates.answer(50_000, "u2k", 1353.0).startswith("50,000 USDT는 약 67,650,000원 입니다.")
    assert rates.answer(0.4, "u2k", 1347.5).startswith("0.4 USDT는 약 539원")
    assert rates.answer(150000, "k2u", 1346.0).startswith("150,000원은 약 111.4413 USDT 입니다.")
    assert "참고용" in rates.answer(1, "u2k", 1346.0)
    assert "직전 값" in rates.answer(1, "u2k", 1346.0, stale=True)


@test
async def sane_rejects_out_of_range():
    assert rates._sane(1346) == 1346.0
    assert rates._sane(50) is None          # 금액은 환율이 아님
    assert rates._sane(99999) is None       # 말도 안 되는 값
    assert rates._sane("x") is None


# ── 캐시·폴백·sanity ──────────────────────────────────────
@test
async def cache_avoids_second_external_call():
    c = FakeClient(upbit=1346)
    _reset(c)
    r1, s1 = await rates.get_rate()
    r2, s2 = await rates.get_rate()
    assert r1 == 1346.0 and r2 == 1346.0 and not s1 and not s2
    assert c.calls.count(rates.UPBIT) == 1, "60초 캐시 → 두 번째는 외부 호출 안 함"


@test
async def falls_back_to_bithumb_when_upbit_down():
    c = FakeClient(upbit=1346, bithumb=1345, fail=("upbit",))
    _reset(c)
    r, _ = await rates.get_rate()
    assert r == 1345.0, "업비트 실패 → 빗썸"
    assert any("upbit" in u for u in c.calls) and any("bithumb" in u for u in c.calls)


@test
async def all_sources_insane_or_down_returns_none():
    c = FakeClient(upbit=99999, bithumb=1345, fail=("bithumb", "coingecko"))  # 업비트 범위 밖 + 나머지 다운
    _reset(c)
    r, s = await rates.get_rate()
    assert r is None and not s, "범위 밖·전부 실패면 지어내지 않고 None"


@test
async def serves_stale_within_5min_on_failure():
    import time
    c = FakeClient(fail=("upbit", "bithumb", "coingecko"))
    _reset(c)
    rates._cache.update(rate=1300.0, ts=time.time() - 100)   # 100초 전 값 (TTL 지났지만 5분 안)
    r, s = await rates.get_rate()
    assert r == 1300.0 and s is True, "갱신 실패해도 5분 안 직전 값 (stale)"


# ── 그룹 훅 (rate_enabled 게이트) ─────────────────────────
class _User:
    id = 7
    is_bot = False
    first_name = "손님"


class _Msg:
    def __init__(self, text, chat_id=-100):
        self.text, self.chat_id = text, chat_id
        self.from_user = _User()
        self.replies = []

    async def reply_text(self, t, **kw):
        self.replies.append(t)


class _DB:
    def __init__(self, enabled):
        self._e = enabled

    async def get_settings(self, cid):
        return {"rate_enabled": self._e}


class _Svc:
    def __init__(self, enabled):
        self.db = _DB(enabled)


@test
async def group_hook_respects_rate_enabled():
    _reset(FakeClient(upbit=1346))
    off = _Msg("50테더")
    await rates.on_message(_Svc(False), None, off, 0)
    assert off.replies == [], "꺼진 방은 무응답"

    on = _Msg("50테더")
    await rates.on_message(_Svc(True), None, on, 0)
    assert on.replies and "67,300원" in on.replies[0], on.replies

    # 일반 대화는 켜져 있어도 무응답
    chat = _Msg("오늘 50명 왔어")
    await rates.on_message(_Svc(True), None, chat, 0)
    assert chat.replies == []


@test
async def group_hook_user_cooldown():
    _reset(FakeClient(upbit=1346))
    svc = _Svc(True)
    m1 = _Msg("50테더")
    await rates.on_message(svc, None, m1, 0)
    assert m1.replies, "첫 번째는 답"
    m2 = _Msg("100테더")
    await rates.on_message(svc, None, m2, 0)
    assert m2.replies == [], "같은 사람 2초 안엔 또 안 답 (도배 방지)"


# ── AI 도구 ───────────────────────────────────────────────
@test
async def ai_tool_rate_convert():
    _reset(FakeClient(upbit=1346))
    out = await rpanel.t_rate(None, {"amount": 500, "unit": "usdt"})
    assert out.startswith("500 USDT는 약 673,000원 입니다."), out
    out2 = await rpanel.t_rate(None, {"amount": 1_000_000, "unit": "krw"})
    assert "원은 약" in out2 and "USDT" in out2, out2
    bad = await rpanel.t_rate(None, {"amount": "abc"})
    assert "필요" in bad


@test
async def command_registered():
    from sodam import commands
    assert any(c.fn is rates.c_rate for c in commands.COMMANDS), ".시세 명령 등록됨"
    assert "시세" in commands._INDEX and "환율" in commands._INDEX


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
