"""📥 입장요청 쌓기·전원 승인·가라 거절 — python tests/run_all.py joinpending

텔레그램은 가입 신청 명단을 관리자 유저 계정에만 보여줘서(봇 API 엔 목록 기능 없음), 봇이 받는 신청을 joinpending 이 전부 쌓아둔다 →
그 명단으로 approveChatJoinRequest/declineChatJoinRequest 를 봇이 한 번에 돌린다. (2026-11-22 슈퍼배드 소통방)"""
from types import SimpleNamespace

from fakes import FakeBot, add_member, fake_user, make_db, make_svc, runner

import sodam.__main__  # noqa: F401  (패널·도구 등록)
from sodam import joinpending
from sodam.panels import joinpending as P
from sodam.permissions import Role
from sodam.tools import ToolCtx

joinpending.GAP = 0   # 테스트: 배치 사이 대기 없음

test, run_all = runner()

CHAT = -1001000000555
ADMIN, MEMBER = 801, 802
# 한글 이름 2명 + 비한글(가라) 3명
KO = [(9001, "철수"), (9002, "영희 김")]
GARA = [(9101, "Ivan"), (9102, "Чжан"), (9103, "x9f2k")]


def _req(uid, name, username=None):
    return SimpleNamespace(chat=SimpleNamespace(id=CHAT),
                           from_user=fake_user(uid, name, username=username))


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    bot = FakeBot()
    for uid, name in KO + GARA:
        await joinpending.record(svc, _req(uid, name))
    return db, svc, bot


def tctx(svc, bot, uid):
    return ToolCtx(svc=svc, bot=bot, chat_id=CHAT, caller=fake_user(uid, "문주"), role=Role.ADMIN, settings={})


@test
async def record_and_count_split_korean_foreign():
    db, svc, bot = await setup()
    total, ko, non = await joinpending.count(svc, CHAT)
    assert total == 5, total
    assert ko == 2, ko      # 철수·영희
    assert non == 3, non    # Ivan·Чжан·x9f2k


@test
async def record_ignores_bots():
    db, svc, bot = await setup()
    await joinpending.record(svc, SimpleNamespace(chat=SimpleNamespace(id=CHAT),
                                                  from_user=fake_user(9999, "botty", is_bot=True)))
    total, _, _ = await joinpending.count(svc, CHAT)
    assert total == 5, total   # 봇은 안 쌓임


@test
async def pending_scope_filters():
    db, svc, bot = await setup()
    assert len(await joinpending.pending(svc, CHAT, "all")) == 5
    assert set(await joinpending.pending(svc, CHAT, "korean")) == {9001, 9002}
    assert set(await joinpending.pending(svc, CHAT, "foreign")) == {9101, 9102, 9103}


@test
async def bulk_approve_all_calls_bot_and_clears():
    db, svc, bot = await setup()
    ok, fail = await joinpending.bulk(svc, bot, CHAT, approve=True, scope="all")
    assert (ok, fail) == (5, 0), (ok, fail)
    approved = {uid for (k, cid, uid) in bot.calls if k == "approve" and cid == CHAT}
    assert approved == {9001, 9002, 9101, 9102, 9103}, approved
    assert await joinpending.count(svc, CHAT) == (0, 0, 0)   # 명단 비워짐


@test
async def bulk_decline_foreign_keeps_korean():
    db, svc, bot = await setup()
    ok, fail = await joinpending.bulk(svc, bot, CHAT, approve=False, scope="foreign")
    assert ok == 3 and fail == 0, (ok, fail)
    declined = {uid for (k, cid, uid) in bot.calls if k == "decline"}
    assert declined == {9101, 9102, 9103}, declined
    assert not [c for c in bot.calls if c[0] == "approve"]          # 승인은 안 함
    total, ko, non = await joinpending.count(svc, CHAT)
    assert (total, ko, non) == (2, 2, 0), (total, ko, non)         # 한국인 2명 그대로 남음


@test
async def forget_removes_one():
    db, svc, bot = await setup()
    await joinpending.forget(svc, CHAT, 9001)
    assert set(await joinpending.pending(svc, CHAT, "all")) == {9002, 9101, 9102, 9103}


@test
async def bulk_already_handled_error_still_clears_row():
    db, svc, bot = await setup()

    async def boom(chat_id, user_id):   # 텔레그램이 '이미 처리됨' 류 오류
        from telegram.error import BadRequest
        bot.calls.append(("approve", chat_id, user_id))
        raise BadRequest("User_already_participant")
    bot.approve_chat_join_request = boom
    ok, fail = await joinpending.bulk(svc, bot, CHAT, approve=True, scope="all")
    assert fail == 0 and ok == 5, (ok, fail)                       # 오류도 '더 할 게 없음' = 성공 처리
    assert await joinpending.count(svc, CHAT) == (0, 0, 0)      # 줄은 비워짐


@test
async def tool_count_returns_numbers():
    db, svc, bot = await setup()
    out = await P.t_join_requests(tctx(svc, bot, ADMIN), {"action": "count"})
    assert "5" in out and "2" in out and "3" in out, out


@test
async def tool_approve_posts_confirm_card():
    db, svc, bot = await setup()
    out = await P.t_join_requests(tctx(svc, bot, ADMIN), {"action": "approve", "scope": "all"})
    assert "확인 버튼" in out, out
    sent = [c for c in bot.calls if c[0] == "send_message" and c[1] == CHAT]
    assert sent and sent[-1][-1].get("reply_markup") is not None   # 카드 버튼 달림
    # 아직 승인 안 함 (카드만)
    assert not [c for c in bot.calls if c[0] == "approve"]
    assert (await joinpending.count(svc, CHAT))[0] == 5


@test
async def tool_empty_room_no_card():
    db, svc, bot = await setup()
    for uid in (9001, 9002, 9101, 9102, 9103):
        await joinpending.forget(svc, CHAT, uid)
    out = await P.t_join_requests(tctx(svc, bot, ADMIN), {"action": "approve", "scope": "all"})
    assert "없" in out, out
    assert not [c for c in bot.calls if c[0] == "send_message"]


@test
async def card_press_runs_bulk_approve():
    db, svc, bot = await setup()
    await P.t_join_requests(tctx(svc, bot, ADMIN), {"action": "approve", "scope": "all"})
    row = await svc.db._one("SELECT card FROM ai_cards WHERE tool='join_requests' ORDER BY created DESC LIMIT 1")
    spec = {"action": "approve", "scope": "all", "card": row["card"]}
    captured = []
    orig = P.persist.spawn
    P.persist.spawn = lambda coro: captured.append(coro)
    try:
        c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN)
        scr = await P.jreq_ok(c, spec)
        assert "중이에요" in (scr.text or ""), scr.text
        assert captured, "bulk 이 spawn 안 됨"
        await captured[0]
    finally:
        P.persist.spawn = orig
    approved = {c[2] for c in bot.calls if c[0] == "approve"}
    assert approved == {9001, 9002, 9101, 9102, 9103}, approved
    assert await joinpending.count(svc, CHAT) == (0, 0, 0)


@test
async def card_press_blocks_non_admin():
    db, svc, bot = await setup()
    await P.t_join_requests(tctx(svc, bot, ADMIN), {"action": "approve", "scope": "all"})
    row = await svc.db._one("SELECT card FROM ai_cards WHERE tool='join_requests' ORDER BY created DESC LIMIT 1")
    spec = {"action": "approve", "scope": "all", "card": row["card"]}
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=MEMBER)   # 관리자 아님
    scr = await P.jreq_ok(c, spec)
    assert "관리자" in (scr.text or ""), scr.text
    assert not [cc for cc in bot.calls if cc[0] == "approve"]     # 실행 안 됨


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
