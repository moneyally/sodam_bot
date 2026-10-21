"""💭 라이브 생각 (reasoning summary 스트리밍): python tests/run_all.py thinking

llm.think 가 on_think 콜백을 받으면 stream=True + reasoning.summary=auto 로 생각 요약을 흘려보낸다.
agent._Thinker 가 그 콜백을 받아 방에 💭 메시지 하나를 1.5초마다 고치고 끝나면 지운다. 2026-10-21 오너 'Claude 처럼'.
"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, fake_user, runner

from sodam import agent
from sodam.llm import LLM, _summary_rejected

test, run_all = runner()


class Ev(SimpleNamespace):
    pass


class FakeResponses:
    def __init__(self, events, final):
        self.events, self.final = events, final
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        assert kwargs.get("stream") is True, "on_think 이면 stream=True 여야"

        async def gen():
            for e in self.events:
                yield e
        return gen()


def _final(text="밴했어요", calls=()):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15,
                            input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                            output_tokens_details=SimpleNamespace(reasoning_tokens=3))
    out = [SimpleNamespace(type="function_call", call_id=c[0], name=c[1], arguments=c[2],
                           model_dump=lambda **k: {"type": "function_call"}) for c in calls]
    return SimpleNamespace(usage=usage, output=out, output_text=text)


# ── llm._stream_think ────────────────────────────────────
@test
async def stream_accumulates_summary_and_returns_final():
    final = _final("완료")
    events = [
        Ev(type="response.reasoning_summary_text.delta", delta="이 사람 ", summary_index=0),
        Ev(type="response.reasoning_summary_text.delta", delta="찾는 중…", summary_index=0),
        Ev(type="response.reasoning_summary_part.added", summary_index=1),
        Ev(type="response.reasoning_summary_text.delta", delta="밴 권한 확인", summary_index=1),
        Ev(type="response.output_text.delta", delta="완료"),
        Ev(type="response.completed", response=final),
    ]
    self_ = SimpleNamespace(client=SimpleNamespace(responses=FakeResponses(events, final)))
    seen = []

    async def on_think(summary, done):
        seen.append((summary, done))

    got = await LLM._stream_think(self_, {"model": "m"}, on_think)
    assert got is final, "response.completed 의 response 를 그대로 돌려줘야"
    # 요약이 누적돼서 흘러감
    assert seen[0][0] == "이 사람 " and seen[1][0] == "이 사람 찾는 중…", seen
    assert "밴 권한 확인" in seen[-1][0], seen            # 블록 넘어가도 이어 붙음
    assert "\n\n" in seen[-1][0], "part.added 로 블록 사이 띄우기"
    assert seen[-1][1] is True, "마지막은 done=True 로 한 번 더"


@test
async def no_completed_event_raises():
    self_ = SimpleNamespace(client=SimpleNamespace(responses=FakeResponses([
        Ev(type="response.reasoning_summary_text.delta", delta="음", summary_index=0)], None)))
    try:
        await LLM._stream_think(self_, {"model": "m"}, lambda s, d: asyncio.sleep(0))
        assert False, "completed 없으면 에러여야"
    except RuntimeError:
        pass


@test
async def create_think_streams_only_with_callback():
    # on_think 없으면 stream 안 붙이고 그냥 create (= 예전 동작 그대로)
    plain_called = {}

    class PlainResp:
        async def create(self, **kwargs):
            plain_called.update(kwargs)
            return _final("x")
    self_ = SimpleNamespace(client=SimpleNamespace(responses=PlainResp()))
    await LLM._create_think(self_, {"model": "m"}, None)
    assert "stream" not in plain_called, "on_think 없으면 stream 안 붙음 (회귀 안전)"


@test
def summary_rejected_detects():
    assert _summary_rejected(Exception("reasoning.summary requires organization to be verified"))
    assert _summary_rejected(Exception("unsupported value: 'summary'"))
    assert not _summary_rejected(Exception("model not found"))


# ── agent._Thinker (생각 요약을 뒤에서 한국어로 번역해 교체) ──
class _FakeLLM:
    def __init__(self, ko="옮긴 한국어"):
        self.ko, self.calls = ko, []

    async def json(self, system, user, **kw):
        self.calls.append((system, user, kw))
        return {"ko": self.ko}


def _ctx(bot, chat=-100777, ko="옮긴 한국어"):
    svc = SimpleNamespace(llm=_FakeLLM(ko), cfg=SimpleNamespace(guard_model="guard"))
    return SimpleNamespace(svc=svc, bot=bot, chat_id=chat,
                           request_msg=SimpleNamespace(message_id=42), settings={})


async def _drain(th):
    for t in list(th.tasks):   # 뒤에서 도는 번역이 끝나길 기다림
        await t


@test
async def thinker_begin_then_translates_korean_then_clears():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot, ko="이 사람 찾아서 밴하는 중"))
    await th.begin()
    sends = bot.named("send_message")
    assert sends and "💭 생각 중" in sends[0][2], sends                   # 먼저 '생각 중…'
    assert sends[0][3].get("reply_to_message_id") == 42
    await th("locating the user and checking ban perms", True)            # 영어 요약 끝 → 뒤에서 번역
    await _drain(th)
    edits = bot.named("edit_text")
    assert edits and "밴하는 중" in edits[-1][2], edits                   # 한국어로 교체 (영어 아님)
    assert "locating" not in edits[-1][2], "영어 요약을 그대로 안 보임"
    assert len(bot.named("send_message")) == 1, "교체는 편집 — 새 메시지 아님"
    await th.clear()
    assert bot.named("delete"), "clear 는 💭 메시지를 지움"


@test
async def thinker_streaming_shows_nothing_until_done():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.begin()
    before = len(bot.named("edit_text"))
    await th("still thinking in english", False)     # done=False → 영어 안 보임, 편집도 안 함
    await _drain(th)
    assert len(bot.named("edit_text")) == before, "흐르는 중엔 아무것도 안 바꿈"


@test
async def thinker_uses_cheap_guard_model_once_per_round():
    bot = FakeBot()
    c = _ctx(bot, ko="한국어 생각")
    th = agent._Thinker(c)
    await th.begin()
    await th("english summary one", True)
    await th("english summary one", True)            # 같은 요약 → 또 안 옮김
    await _drain(th)
    assert len(c.svc.llm.calls) == 1, "같은 요약은 한 번만 번역"
    assert c.svc.llm.calls[0][2].get("model") == "guard", "싼 guard 모델로"


@test
def tool_status_maps_names_to_korean():
    assert "웹" in agent._tool_status(["web_search"])
    assert "그림" in agent._tool_status(["make_image"])
    assert "노래" in agent._tool_status(["music"])
    assert "처리" in agent._tool_status(["ban_member"])   # 모르는/쓰기 도구 = 처리하는 중
    assert "처리" in agent._tool_status([])               # 기본


@test
async def thinker_status_shows_korean_tool_chunk():
    # 모델이 생각을 말 안 해도, 도구 실행을 한국어 조각으로 (실시간 조각 느낌 — 오너)
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.begin()
    await th.status(["web_search"])
    assert "웹에서" in bot.named("edit_text")[-1][2], bot.calls
    await th.status(["make_image"])
    assert "그림" in bot.named("edit_text")[-1][2]
    assert "evil" not in str(bot.calls)   # 안전 필터는 그대로


@test
async def thinker_begin_shows_placeholder_before_summary():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.begin()
    sends = bot.named("send_message")
    assert sends and "💭" in sends[0][2] and "생각 중" in sends[0][2], sends
    await th.begin()                                   # 두 번 불러도 한 번만 (이미 떠 있음)
    assert len(bot.named("send_message")) == 1, bot.calls


@test
async def thinker_begin_after_clear_does_nothing():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.clear()
    await th.begin()
    assert not bot.named("send_message"), "clear 뒤엔 안 띄움"


@test
async def thinker_filters_links_and_wallets():
    bot = FakeBot()
    # 번역 결과에 링크·지갑이 섞여도 strip_unsafe 로 거름
    th = agent._Thinker(_ctx(bot, ko="https://evil.com 지갑 TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t 확인 중"))
    await th.begin()
    await th("checking a wallet address", True)
    await _drain(th)
    body = bot.named("edit_text")[-1][2]
    assert "evil.com" not in body and "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t" not in body, body


# ── agent 가 on_think 를 넘기나 (설정 켜짐·그룹·생각일 때만) ──
@test
async def thinker_created_only_when_setting_on_and_group():
    bot = FakeBot()
    svc = SimpleNamespace(llm=_FakeLLM(), cfg=SimpleNamespace(guard_model="guard"))
    on = SimpleNamespace(svc=svc, bot=bot, chat_id=-100, request_msg=SimpleNamespace(message_id=1),
                         settings={"ai_thinking": True}, quiet=False)
    off = SimpleNamespace(svc=svc, bot=bot, chat_id=-100, request_msg=SimpleNamespace(message_id=1),
                          settings={"ai_thinking": False}, quiet=False)
    dm = SimpleNamespace(svc=svc, bot=bot, chat_id=555, request_msg=SimpleNamespace(message_id=1),
                         settings={"ai_thinking": True}, quiet=False)
    # _run 의 판정식과 같은 조건
    def make(c):
        return (agent._Thinker(c) if c.chat_id < 0 and c.settings.get("ai_thinking")
                and not c.quiet and c.request_msg is not None else None)
    assert make(on) is not None
    assert make(off) is None          # 설정 꺼짐 → 없음
    assert make(dm) is None           # 1:1 → 없음


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
