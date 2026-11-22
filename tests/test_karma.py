"""👍 멤버 평판 karma (sodam/karma.py · panels/karma.py). python tests/run_all.py karma

2026-11-22 경쟁조사 ⑦. +1 추천·자기금지·하루중복·봇제외·순위."""
from types import SimpleNamespace

from fakes import FakeBot, make_db, runner

from sodam import karma

test, run_all = runner()
CHAT = -1006666
A, B, C = 1, 2, 3


def svc_of(db):
    return SimpleNamespace(db=db)


def fmsg(uid, text, reply_uid=None, reply_is_bot=False):
    reply = None
    if reply_uid is not None:
        reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_uid, first_name=f"사람{reply_uid}", is_bot=reply_is_bot))
    return SimpleNamespace(chat_id=CHAT, from_user=SimpleNamespace(id=uid), message_id=9, text=text,
                           reply_to_message=reply)


@test
async def give_rules():
    db = await make_db()
    assert await karma.give(db, CHAT, A, A) is None          # 자기 자신 금지
    assert await karma.give(db, CHAT, A, B) == 1             # A→B +1
    assert await karma.points(db, CHAT, B) == 1
    assert await karma.give(db, CHAT, A, B) is None          # 같은 상대 하루 중복 금지
    assert await karma.give(db, CHAT, C, B) == 2             # 다른 사람(C)은 줄 수 있음
    assert await karma.points(db, CHAT, B) == 2


@test
async def rank_and_top():
    db = await make_db()
    await karma.give(db, CHAT, A, B)
    await karma.give(db, CHAT, C, B)   # B=2
    await karma.give(db, CHAT, B, A)   # A=1
    assert await karma.rank(db, CHAT, B) == (1, 2)
    assert await karma.rank(db, CHAT, A) == (2, 1)
    assert await karma.rank(db, CHAT, C) == (0, 0)           # 점수 없으면 순위 0
    rows = await karma.top(db, CHAT, 10)
    assert [r["user_id"] for r in rows] == [B, A]            # 점수순


@test
async def handle_plus_one_reply():
    db = await make_db()
    svc, bot = svc_of(db), FakeBot()
    # A 가 B 글에 '+1' 답장 → B 평판 +1, 처리됨
    assert await karma.handle(svc, bot, fmsg(A, "+1", reply_uid=B)) is True
    assert await karma.points(db, CHAT, B) == 1
    assert any("평판 +1" in str(c) for c in bot.calls)
    # 👍 도 됨
    assert await karma.handle(svc, bot, fmsg(C, "👍", reply_uid=B)) is True
    assert await karma.points(db, CHAT, B) == 2


@test
async def handle_ignores_non_triggers():
    db = await make_db()
    svc, bot = svc_of(db), FakeBot()
    assert await karma.handle(svc, bot, fmsg(A, "+1 하고 뭐 더", reply_uid=B)) is False  # 글 전체가 +1 아님
    assert await karma.handle(svc, bot, fmsg(A, "+1")) is False                          # 답장 아님
    assert await karma.handle(svc, bot, fmsg(A, "그냥 대화", reply_uid=B)) is False        # 트리거 아님
    assert await karma.handle(svc, bot, fmsg(A, "+1", reply_uid=9, reply_is_bot=True)) is False  # 봇에게는 X
    assert await karma.points(db, CHAT, B) == 0


@test
async def handle_self_plus_is_silently_dropped():
    db = await make_db()
    svc, bot = svc_of(db), FakeBot()
    assert await karma.handle(svc, bot, fmsg(A, "+1", reply_uid=A)) is True   # 자기 글에 +1 → 중단(조용히)
    assert await karma.points(db, CHAT, A) == 0                               # 점수 안 오름
    assert not any("평판 +1" in str(c) for c in bot.calls)
