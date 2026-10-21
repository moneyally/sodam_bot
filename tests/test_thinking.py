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


# ── agent._Thinker (위=한국어 상태 문구 + 아래=영어 추론 원문 실시간) ──
class _FakeLLM:
    def __init__(self, ko="옮긴 한국어"):
        self.ko, self.calls = ko, []

    async def json(self, system, user, **kw):
        self.calls.append((system, user, kw))
        return {"ko": self.ko, "s": self.ko}   # ko=요약 번역, s=작업 문구 (둘 다 가짜로 같은 값)


def _call(name, args="{}"):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))


def _ctx(bot, chat=-100777, ko="옮긴 한국어"):
    svc = SimpleNamespace(llm=_FakeLLM(ko), cfg=SimpleNamespace(guard_model="guard"))
    return SimpleNamespace(svc=svc, bot=bot, chat_id=chat,
                           request_msg=SimpleNamespace(message_id=42), settings={})


async def _drain(th):
    for t in list(th.tasks):   # 뒤에서 도는 번역이 끝나길 기다림
        await t


@test
async def thinker_head_korean_tail_english_then_clears():
    # 위=한국어 상태, 아래=영어 추론 원문 (오너 '교체 문구 밑에 영어 델타도')
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.begin()
    sends = bot.named("send_message")
    assert sends and "💭 생각 중" in sends[0][2], sends                   # 먼저 '생각 중…'
    assert sends[0][3].get("reply_to_message_id") == 42
    await th("Calculating recurrence step by step", True)                 # 영어 추론 → 밑에 그대로 흐름
    body = bot.named("edit_text")[-1][2]
    assert "💭 생각 중" in body and "Calculating recurrence" in body, body   # 위 한국어 + 아래 영어 둘 다
    assert len(bot.named("send_message")) == 1, "한 메시지 안에서 교체 — 새 메시지 아님"
    await th.clear()
    assert bot.named("delete"), "clear 는 💭 메시지를 지움"


@test
async def thinker_streaming_throttled_but_done_forces():
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.begin()
    th.last = 0.0                                     # 간격 지난 척 → 첫 조각은 바로
    await th("first chunk", False)
    assert any("first chunk" in e[2] for e in bot.named("edit_text")), bot.calls
    n = len(bot.named("edit_text"))
    await th("first chunk more", False)               # THINK_GAP 안 → 묶어서 (안 고침)
    assert len(bot.named("edit_text")) == n, "흐르는 중엔 묶어서 고침 (플러드 방지)"
    await th("first chunk more and done", True)       # done=True → 바로
    assert "done" in bot.named("edit_text")[-1][2], bot.calls


@test
async def thinker_no_translation_call_on_reasoning():
    # 영어 원문을 그대로 흘리니 번역 호출(비용) 없음 — on_think 은 LLM 안 부름
    bot = FakeBot()
    c = _ctx(bot)
    th = agent._Thinker(c)
    await th.begin()
    await th("english reasoning text", True)
    await _drain(th)
    assert c.svc.llm.calls == [], "추론 흘리기는 LLM 호출 없음 (번역 안 함)"


@test
async def thinker_status_instant_fallback_then_gpt_phrase():
    # 즉시 기본 조각(하드코딩) → 뒤에서 GPT 가 지은 문구로 교체 (틀 — 오너)
    bot = FakeBot()
    c = _ctx(bot, ko="채팅 기록 뒤져서 세는 중")
    th = agent._Thinker(c)
    await th.begin()
    await th.status([_call("chat_stats", '{"over":100}')], "100개 넘게 친 사람 정리해줘")
    # 즉시 기본 조각이 먼저 떠 있어야 (GPT 문구 오기 전)
    assert any("찾아보는 중" in e[2] for e in bot.named("edit_text")), bot.calls
    await _drain(th)                                  # GPT 문구 생성 끝
    assert "채팅 기록 뒤져서 세는 중" in bot.named("edit_text")[-1][2], "GPT 가 지은 문구로 교체"
    # 싼 guard 모델로 작업 문구 생성 (thinkstatus)
    assert any(k[2].get("purpose") == "thinkstatus" and k[2].get("model") == "guard" for k in c.svc.llm.calls)


@test
async def thinker_status_instant_fallback_then_gpt_phrase():
    # 즉시 기본 조각(하드코딩) → 뒤에서 GPT 가 지은 문구로 교체 (틀 — 오너)
    bot = FakeBot()
    c = _ctx(bot, ko="채팅 기록 뒤져서 세는 중")
    th = agent._Thinker(c)
    await th.begin()
    await th.status([_call("chat_stats", '{"over":100}')], "100개 넘게 친 사람 정리해줘")
    # 즉시 기본 조각이 먼저 떠 있어야 (GPT 문구 오기 전)
    assert any("찾아보는 중" in e[2] for e in bot.named("edit_text")), bot.calls
    await _drain(th)                                  # GPT 문구 생성 끝
    assert "채팅 기록 뒤져서 세는 중" in bot.named("edit_text")[-1][2], "GPT 가 지은 문구로 교체"
    # 싼 guard 모델로 작업 문구 생성 (thinkstatus)
    assert any(k[2].get("purpose") == "thinkstatus" and k[2].get("model") == "guard" for k in c.svc.llm.calls)


@test
def tool_status_maps_names_to_korean():
    assert "웹" in agent._tool_status(["web_search"])
    assert "그림" in agent._tool_status(["make_image"])
    assert "노래" in agent._tool_status(["music"])
    assert "처리" in agent._tool_status(["ban_member"])   # 모르는/쓰기 도구 = 처리하는 중
    assert "처리" in agent._tool_status([])               # 기본


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


@test
async def md_to_html_makes_telegram_bold():
    # 텔레그램 봇이니 마크다운을 떼지 말고 서식으로 (오너 '텔레그램 굵게로 처리하면 되잖아')
    assert agent._md_to_html("**Calculating** the `recurrence`") == "<b>Calculating</b> the <code>recurrence</code>"
    assert agent._md_to_html("### Step 1\ndo it") == "Step 1\ndo it", "제목 # 는 뗌(텔레그램 제목 서식 없음)"
    assert agent._md_to_html("~~wrong~~") == "<s>wrong</s>"
    assert agent._md_to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d", "< > & 는 먼저 esc (태그 주입 방지)"
    assert agent._md_to_html("half **open") == "half open", "스트리밍 중 반쪽 마커는 제거 — 태그 안 깨지게"


@test
async def thinker_tail_markdown_rendered_html():
    # 흐르는 영어 추론의 **굵게** 를 텔레그램 <b> 로, parse_mode HTML 로 전송
    bot = FakeBot()
    th = agent._Thinker(_ctx(bot))
    await th.begin()
    assert bot.named("send_message")[0][3].get("parse_mode") == "HTML", "💭 는 HTML 로 보냄"
    await th("**Comparing** the two teams", True)
    body = bot.named("edit_text")[-1][2]
    assert "<b>Comparing</b>" in body and "**" not in body, body
    assert bot.named("edit_text")[-1][3].get("parse_mode") == "HTML", "편집도 HTML"


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
