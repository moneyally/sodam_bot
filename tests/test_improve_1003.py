"""2026-10-03 실측 개선 (why 로 찾은 실수들): python tests/run_all.py improve_1003

- 일루왕 '토이든님 태그해서 플 뱅 골라줘' → greet_members 로 인사가 나감 → mention_members
- 관리자 일 뒤 5분 안 잡담이 전부 큰 모델+추론 ($0.012/번) → 이어짐 신호 없으면 작은 모델, 잡담은 추론 없이
- 이옌 '관리자 추방해' 거절 이유가 안 보임 → 거절 결과에 '이유를 말할 것'
- 베베 음성 '권한 없음' 4번 → 할 일을 두 가지로 안내 · 'not in a call' = 오류 아님
- why '다시 요청' 신호에서 게임 한 판 더·그림 하나 더는 뺌
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402
from test_route import BOSS, JUNHO, LIGHT, _room  # noqa: E402

from sodam import route, tools, whyfail  # noqa: E402
from sodam.panels import voice as voice_panel  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.tools import ToolCtx  # noqa: E402

test, run_all = runner()


@test
async def tag_someone_uses_mention_not_greeting():
    old = fast_timers()
    try:
        llm = ScriptedLLM([tool_call("mention_members", {"names": ["박준호"]}), reply("플레이어 가시죠!")])
        r = await _room(llm, light="")
        m = await r.say(BOSS, "소담아 박준호님 태그해서 플 뱅 중 하나 골라줘")
        out = [x["content"] for x in llm.of("chat")[-1]["messages"] if x["role"] == "tool"][0]
        assert "태그 대상: 박준호" in out and "인사" in out and "환영" not in out.split("인사")[0], out
        assert any(f"id={JUNHO.id}" in t for t in m.replies), m.replies
    finally:
        restore_timers(old)
    greet = tools._BY_NAME["greet_members"].description
    assert "mention_members" in greet and "'인사'할 때만" in greet
    assert "mention_members" in route.LIGHT_WRITE


@test
async def admin_small_talk_after_heavy_goes_light_and_no_reasoning():
    assert route.decide(route.Req("오라버니야", Role.ADMIN, recent_heavy=True), light_model=LIGHT).lane == "light"
    assert route.decide(route.Req("하나 더 만들어줘", Role.ADMIN, recent_heavy=True), light_model=LIGHT).lane == "heavy"
    assert route.decide(route.Req("그거 말고 파란색", Role.ADMIN, recent_heavy=True), light_model=LIGHT).why == "continue"
    old = fast_timers()
    try:
        llm = ScriptedLLM([reply("오 멋진 사진이네요")])
        llm.think = None   # 추론을 부르면 터짐
        r = await _room(llm, think="auto")
        r.llm.script = [reply("오 멋진 사진이네요")]
        long_chat = "소담아 " + "오늘 날씨 진짜 좋다 " * 16           # long 신호 = 큰 모델, 하지만 잡담이라 추론 없이
        await r.say(BOSS, long_chat)
        run = await r.db._one("SELECT purpose FROM agent_runs ORDER BY id DESC LIMIT 1")
        assert run["purpose"] == "agent:admin", run["purpose"]
    finally:
        restore_timers(old)


@test
async def sanction_refusal_tells_the_reason():
    r = Room()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, JUNHO):
        await r.join(u)
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, JUNHO, Role.ADMIN, await r.db.get_settings(Room.CHAT))
    r.svc.perms.admins.add(JUNHO.id)
    out = await tools.execute("kick_member", '{"names": ["방장"], "reason": "x"}', ctx)
    assert "관리자" in out and tools.REFUSE_SAY in out, out
    desc = tools._BY_NAME["member_action"].description
    assert "만료된 링크" in desc and "unban" in desc


@test
async def voice_permission_message_says_what_to_do_and_not_in_call_is_closed():
    text = voice_panel._result_text("no_voice_right")
    assert "음성채팅 시작" in text and "관리자 추가" in text
    import asyncio
    from sodam.voice import bridge as br
    b = br.Bridge.__new__(br.Bridge)
    stopped = []
    b.stop = stopped.append
    b.out, b.stats, b._played, b.item = __import__("collections").deque(), __import__("collections").defaultdict(int), {}, None
    b.clock = lambda: 0.0

    async def boom(frame):
        raise RuntimeError("The userbot is not in a call")
    b._play = boom
    b._last_voice = 0
    await asyncio.wait_for(b._pacer(), 10)
    assert stopped == ["chat_closed"], stopped


@test
async def again_after_success_is_not_a_redo():
    base = {"id": 1, "chat_id": -1, "user_id": 5, "ts": 1000, "trigger": "끝말잇기", "status": "answered",
            "steps": '[{"tool": "start_game", "args": "", "result": "끝말잇기 시작했어요!", "gate": "ok", "w": 1}]',
            "events": "[]", "answer": "", "mode": "call", "purpose": "agent:member", "ms": 1, "usd_micro": 0}
    later = [{"id": 2, "ts": 1100, "trigger": "끝말잇기"}]
    assert not whyfail.analyze(base, later), "한 판 끝나고 또 = 실패 아님"
    busy = base | {"steps": '[{"tool": "make_video", "args": "", "result": "영상을 하나 만드는 중이라 지금은 못 만듦", "gate": "'
                   + whyfail.gate_of("영상을 하나 만드는 중이라 지금은 못 만듦") + '", "w": 1}]', "trigger": "영상 만들어줘"}
    found = {f.code for f in whyfail.analyze(busy, [{"id": 3, "ts": 1060, "trigger": "영상 만들어줘"}])}
    assert "redo" in found, found
    assert not whyfail.analyze(base | {"trigger": "(이름만 부름)", "steps": "[]"}, [{"id": 4, "ts": 1010, "trigger": "(이름만 부름)"}])


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
