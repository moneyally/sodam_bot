"""연달아 보낸 말은 한 번에 답 · '천천히' 안내는 1분에 1번: python tests/run_all.py burst

실제 사례: '끝말잇기 하자' → '과일' → '?' → '소담아' 를 1분 안에 보내자 4번째에 '⏳ 조금만 천천히 불러주세요!'.
"""
import asyncio

from fake_llm import Room, reply
from fakes import runner
from test_sanction_multi import A, room

from sodam import handlers

test, run_all = runner()


@test
async def quick_double_send_gets_one_answer_with_both():
    r = await room()
    r.llm.script = [reply("둘 다 봤어요")]
    handlers.BURST_SECONDS = 0.05
    try:
        m1, m2 = await asyncio.gather(r.say(A, "소담아 오늘 뭐 먹지", settle=False), r.say(A, "소담아 매운 걸로", settle=False))
        await r.settle()
    finally:
        handlers.BURST_SECONDS = 0
    [call] = r.llm.of("chat")
    request = call["messages"][-1]["content"].split("<request", 1)[1]      # 대화 기록 말고 요청 칸에 둘 다
    assert "뭐 먹지" in request and "매운 걸로" in request, request[:300]
    assert not m1.replies and m2.replies == ["둘 다 봤어요"], (m1.replies, m2.replies)


@test
async def slow_down_notice_once_per_minute():
    r = await room(user_rate_per_min=1)
    r.llm.script = [reply("네")]
    for t in ("소담아 하나", "소담아 둘", "소담아 셋"):
        await r.say(A, t)
    notices = [c for c in r.bot.named("send_message") if "천천히" in c[2]]
    assert len(r.llm.of("chat")) == 1 and len(notices) == 1, notices
