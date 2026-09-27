"""감사 2차 회귀 테스트 (스팸 방패·운영 인박스·확인 카드·hot path): python tests/run_all.py fix_audit2

각 테스트는 고친 줄을 되돌리면 FAIL 한다 (뮤테이션 검증).
"""
import asyncio
from types import SimpleNamespace

from fake_llm import Room, reply, tool_call
from fakes import FakeQuery, runner
from test_sanction_multi import A, BOSS, ask, room

from sodam import handlers, menu

test, run_all = runner()


async def _press(r, user, data):
    q = FakeQuery(Room.CHAT, user, data)   # 방에 뜬 확인 카드는 방에서 누름
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


@test
async def room_card_double_tap_after_restart_saves_once():
    """재시작 뒤(메모리 토큰 없음) 확인 카드 [✅]를 두 번 빨리 누르면(텔레그램이 콜백 2개를 동시에 줌) 두 번 저장됐음.
    DB 토큰은 SELECT 로만 확인해서 둘 다 통과 → 이제 DELETE 한 문장이 차지."""
    r = await room()
    await ask(r, BOSS, [tool_call("schedule_task", {"when": "매일 23:00", "action": "remind", "text": "마감"})])
    [card] = [c for c in r.bot.named("send_message") if c[1] == Room.CHAT and "예약할까요" in c[2]]
    ok = card[3]["reply_markup"].inline_keyboard[0][0].callback_data
    r.svc.menu_tokens.clear()                                    # 재시작 = 메모리 토큰 사라짐
    await asyncio.gather(_press(r, BOSS, ok), _press(r, BOSS, ok))
    assert len(await r.db.schedules(Room.CHAT)) == 1, [dict(x) for x in await r.db.schedules(Room.CHAT)]


@test
async def burst_answers_latest_message_even_if_processed_first():
    """연달아 보낸 두 말: 동시 처리라 늦게 보낸 말(ID 큼)이 먼저 처리될 수 있음 → 예전엔 먼저 보낸 말에 답장하고
    요청 순서도 뒤집혔음. 이제 메시지 ID 순."""
    r = await room()
    r.llm.script = [reply("둘 다 봤어요")]
    await r.say(A, "안녕하세요")                                  # 두 메시지가 같은 경로(기록 생략)로 처리되게
    m1, m2 = r.msg(A, "소담아 오늘 뭐 먹지"), r.msg(A, "소담아 매운 걸로")
    handlers.BURST_SECONDS = 0.05
    try:
        await asyncio.gather(*(handlers.on_group_message(SimpleNamespace(message=m), r.ctx) for m in (m2, m1)))
        await r.settle()
    finally:
        handlers.BURST_SECONDS = 0
    [call] = r.llm.of("chat")
    request = call["messages"][-1]["content"].split("<request", 1)[1]
    assert request.index("뭐 먹지") < request.index("매운 걸로"), request[:300]
    assert not m1.replies and m2.replies == ["둘 다 봤어요"], (m1.replies, m2.replies)



if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(run_all()))
