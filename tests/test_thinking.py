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


# ── agent._Thinker ───────────────────────────────────────
def _ctx(bot, chat=-100777):
    return SimpleNamespace(bot=bot, chat_id=chat,
                           request_msg=SimpleNamespace(message_id=42), settings={})


@test
async def thinker_sends_then_edits_then_clears():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th(" 이 사람 찾는 중 ", True)               # done=True → 바로
    sends = bot.named("send_message")
    assert sends and "💭" in sends[0][2] and "찾는 중" in sends[0][2], sends
    assert sends[0][3].get("reply_to_message_id") == 42         # 요청에 답장
    await th("이 사람 찾는 중 — 밴한다", True)          # 두 번째 = 편집
    assert bot.named("edit_text"), bot.calls
    await th.clear()
    assert bot.named("delete"), "clear 는 💭 메시지를 지움"


@test
async def thinker_filters_links_and_wallets():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th("https://evil.com 지갑 TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t 확인 중", True)
    body = bot.named("send_message")[0][2]
    assert "evil.com" not in body and "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t" not in body, body   # strip_unsafe 통과


@test
async def thinker_throttles_within_gap():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    import time as _t
    th.last = _t.monotonic()                      # 방금 고친 척
    await th("새 생각", False)                     # done=False + 간격 안 → 아무것도 안 함
    assert not bot.calls, "1.5초 안엔 편집 안 함"
    await th("새 생각", True)                      # done=True → 무조건 보냄
    assert bot.named("send_message"), bot.calls


# ── agent 가 on_think 를 넘기나 (설정 켜짐·그룹·생각일 때만) ──
@test
async def thinker_created_only_when_setting_on_and_group():
    bot = FakeBot()
    on = SimpleNamespace(bot=bot, chat_id=-100, request_msg=SimpleNamespace(message_id=1),
                         settings={"ai_thinking": True}, quiet=False)
    off = SimpleNamespace(bot=bot, chat_id=-100, request_msg=SimpleNamespace(message_id=1),
                          settings={"ai_thinking": False}, quiet=False)
    dm = SimpleNamespace(bot=bot, chat_id=555, request_msg=SimpleNamespace(message_id=1),
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
