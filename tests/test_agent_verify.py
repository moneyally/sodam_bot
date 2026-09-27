"""보내기 전 검사: 도구 없이 '했어요' 라고 하면 한 번 다시 물음. python tests/run_all.py agent_verify"""
from fake_llm import Room, ScriptedLLM, fast_timers, restore_timers, tool_call
from fakes import fake_user, runner

from sodam.agent import _CLAIM, VERIFY_NOTE

test, run_all = runner()
BOSS, JUNHO = fake_user(1, "방장", "boss"), fake_user(20, "박준호", "junho")


async def room(script):
    r = Room()
    r.llm = ScriptedLLM(script)
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


def notes(llm):
    return [c for c in llm.of("chat") if any(m.get("content") == VERIFY_NOTE for m in c["messages"])]


@test
def claim_pattern():
    for t in ("박준호 10분 뮤트했어요", "예약 등록 완료!", "공지 올리고 알림 걸어뒀어요", "경고 처리했습니다"):
        assert _CLAIM.search(t), t
    for t in ("뮤트할까요?", "안녕하세요 대표님", "밴은 확인 버튼을 눌러야 돼요", "오늘 경고 받은 사람은 없어요"):
        assert not _CLAIM.search(t), t


@test
async def fake_done_gets_one_recheck_then_tool():
    old = fast_timers()
    try:
        r = await room(["박준호 10분 뮤트했어요", tool_call("mute", {"names": ["박준호"], "minutes": 10}), "확인 버튼 눌러주세요"])
        await r.say(BOSS, "소담아 박준호 10분 뮤트해줘")
        assert len(notes(r.llm)) == 2 and not r.llm.script              # 다시 물은 뒤 도구 호출까지
    finally:
        restore_timers(old)


@test
async def no_recheck_after_tool_or_for_plain_chat():
    old = fast_timers()
    try:
        r = await room([tool_call("room_rules"), "규칙 확인했어요, 예약 등록 완료된 건 없어요"])
        await r.say(BOSS, "소담아 방 규칙 알려줘")
        assert not notes(r.llm) and not r.llm.script                    # 도구를 썼으면 검사 안 함
        r = await room(["예약 등록 완료했어요", "기록엔 없어요", "안녕하세요"])
        await r.say(BOSS, "소담아 예약 좀 해줘")
        await r.say(BOSS, "소담아 안녕")
        assert len(notes(r.llm)) == 1 and not r.llm.script
        r = await room(["박준호 뮤트했어요", "박준호 뮤트했어요"])          # 또 우겨도 더는 안 물음 (호출 2번이 끝)
        await r.say(BOSS, "소담아 박준호 뮤트해줘")
        assert len(r.llm.of("chat")) == 2 and not r.llm.script              # 한 실행에 한 번만 
    finally:
        restore_timers(old)


if __name__ == "__main__":
    run_all()
