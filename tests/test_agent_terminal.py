"""말싸움 길은 도구 설명 없이 (ask_senior 하나 — 무엇이든 부르면 큰 모델이 처음부터) · 결과를 방에 직접 올리는 도구만 쓴 라운드는
다음 AI 호출을 안 함 (버려질 답). python tests/run_all.py agent_terminal"""
import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import agent, route  # noqa: E402
from sodam.agent import VERIFY_NOTE  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(10, "방장", "boss"), fake_user(20, "박준호", "junho")
LIGHT = "gpt-5.4-mini"
MIRROR = {"ai_comeback": "mirror"}


async def room(script, settings=None, light=LIGHT):
    r = Room()
    r.llm = ScriptedLLM(script)
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, **(settings or {})})
    r.svc.cfg = dataclasses.replace(r.svc.cfg, light_model=light)
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


def names(call):
    return {t["function"]["name"] for t in call["tools"] or []}


async def last_run(r):
    return await r.db._one("SELECT purpose, events, steps FROM agent_runs ORDER BY id DESC LIMIT 1")


# ── 말싸움 길 ───────────────────────────────────────────────
@test
async def banter_sends_only_escape_tool():
    old = fast_timers()
    try:
        text = "소담아 병신아 뭐하냐"
        assert route.decide(route.Req(text, settings=MIRROR), light_model=LIGHT).lane == "banter"
        r = await room(["ㅋㅋ 니가 더 병신"], MIRROR)
        await r.say(JUNHO, text)
        calls = r.llm.of("chat")
        assert len(calls) == 1 and names(calls[0]) == {route.ESCALATE_TOOL}, names(calls[0])   # 도구 설명 2.7만 자 대신 1개
        assert (await last_run(r))["purpose"].endswith(":banter")
    finally:
        restore_timers(old)


@test
async def banter_with_real_request_escalates_to_full_tools():
    """'씨발 쟤 좀 어떻게 해봐' 처럼 실행 말이 애매해 말싸움으로 빠져도 — ask_senior 또는 아무 도구나 부르면 큰 모델이 도구 다 갖고 처음부터."""
    old = fast_timers()
    try:
        for first in (tool_call(route.ESCALATE_TOOL, {"reason": "제재 부탁"}), tool_call("mute_member", {"names": ["박준호"]})):
            r = await room([first, reply("어떻게 해 드릴까요?")], MIRROR)
            await r.say(BOSS, "소담아 씨발 쟤 좀 어떻게 해봐")
            calls = r.llm.of("chat")
            assert len(calls) == 2 and names(calls[0]) == {route.ESCALATE_TOOL}
            assert "mute_member" in names(calls[1]) or "find_tools" in names(calls[1]), names(calls[1])  # 큰 모델 = 전체 목록
            assert calls[1]["model"] != LIGHT and (await last_run(r))["purpose"].endswith(":escalated")
    finally:
        restore_timers(old)


@test
def insult_with_clear_command_is_work_lane():
    """욕 + 분명한 실행 말은 처음부터 일하는 길 (말싸움 판정은 맨 마지막)."""
    for text in ("씨발 박준호 뮤트해", "ㅅㅂ 채팅순위나 보여줘", "병신같은 놈 밴해"):
        lane = route.decide(route.Req(text, settings=MIRROR), light_model=LIGHT).lane
        assert lane != "banter", (text, lane)          # 도구 있는 길 (heavy, 보여줘 같은 조회는 light)
    for text in ("소담아 병신아 뭐하냐", "씨발 니가 해봐 ㅋㅋ", "꺼져 소담아"):          # 순수 욕은 그대로 말싸움
        assert route.decide(route.Req(text, settings=MIRROR), light_model=LIGHT).lane == "banter", text


@test
async def banter_fake_claim_still_checked():
    """도구 없는 말싸움에서도 '뮤트했어' 같은 거짓 완료는 보내기 전 검사가 잡음 (탈출 도구가 있어 allowed 가 비지 않음)."""
    old = fast_timers()
    try:
        r = await room(["ㅋㅋ 박준호 뮤트했어 꺼져", "ㅋㅋ 그 정도로는 안 꺼짐"], MIRROR)
        await r.say(JUNHO, "소담아 병신아 덤벼")
        assert any(any(m.get("content") == VERIFY_NOTE for m in c["messages"]) for c in r.llm.of("chat"))
    finally:
        restore_timers(old)


# ── 끝 도구: 결과를 방에 직접 올린 라운드 뒤엔 AI 를 또 부르지 않음 ─────────────────────
class Quiet:
    """agent.execute 대신: TERMINAL 도구면 ctx.quiet (실제 도구처럼)."""
    def __init__(self):
        self.ran: list[str] = []

    async def __call__(self, name, args, ctx):
        self.ran.append(name)
        if name in agent.TERMINAL:
            ctx.quiet = True
        return f"결과:{name}"


def multi(*names_):
    return SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id=f"c{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps({})))
        for i, n in enumerate(names_)])


@test
async def terminal_round_skips_next_call():
    old, real = fast_timers(), agent.execute
    agent.execute = Quiet()
    try:
        r = await room([tool_call("start_game", {"game": "끝말잇기"})], light="")
        await r.say(BOSS, "소담아 끝말잇기 하자")
        assert len(r.llm.of("chat")) == 1 and not r.llm.script                 # 대본이 남지 않음 = 두 번째 호출 없음
        row = await last_run(r)
        assert '"e": "terminal"' in row["events"]
    finally:
        agent.execute = real
        restore_timers(old)


@test
async def terminal_not_skipped_when_more_work():
    old, real = fast_timers(), agent.execute
    try:
        # ① 다른 도구가 섞인 라운드 ② 여러 단계 말('시작하고') → 다음 호출 그대로
        agent.execute = Quiet()
        r = await room([multi("start_game", "chat_stats"), reply("시작했고 통계는 이래요")], light="")
        await r.say(BOSS, "소담아 끝말잇기랑 오늘 통계")
        assert len(r.llm.of("chat")) == 2
        agent.execute = Quiet()
        r = await room([tool_call("start_game", {"game": "끝말잇기"}), tool_call("mention_members", {"names": ["박준호"]}),
                        reply("불렀어요")], light="")
        await r.say(BOSS, "소담아 끝말잇기 시작하고 박준호 불러줘")
        assert len(r.llm.of("chat")) == 3 and agent.execute.ran == ["start_game", "mention_members"]
    finally:
        agent.execute = real
        restore_timers(old)


@test
async def tool_that_failed_to_post_still_answers():
    """도구가 실패해서 방에 아무것도 안 올렸으면(quiet 아님) AI 가 이유를 말함."""
    old, real = fast_timers(), agent.execute

    async def fail(name, args, ctx):
        return "게임 메시지를 못 보냄: 권한 없음. 짧게 안내할 것."
    agent.execute = fail
    try:
        r = await room([tool_call("start_game", {"game": "끝말잇기"}), reply("지금은 게임을 못 열었어요")], light="")
        await r.say(BOSS, "소담아 끝말잇기 하자")
        assert len(r.llm.of("chat")) == 2
    finally:
        agent.execute = real
        restore_timers(old)


if __name__ == "__main__":
    run_all()
