"""💰 보유금 목표 달성 알림 (화이트라벨/목사봇 — 특정인 하드코딩 0): 보유금≥목표면 그 방 운영자·오너 1:1 집계 알림.

python tests/run_all.py casino_goal
"""
from fakes import FakeQuery, fake_user, runner
from test_casino_cards import CHAT, setup

from sodam.casino import core, goal

test, run_all = runner()
MGR = 777        # 봇 넣은 운영자 (subscriptions.added_by) — 방마다 다름, 코드엔 안 박음
GOAL = 2_000_000


async def _goalroom(start=100_000):
    svc, bot, (u,) = await setup(start=start)
    await svc.db.set_setting(CHAT, "casino_goal", GOAL)
    await svc.db._write("INSERT INTO subscriptions(chat_id, added_by, updated_at) VALUES(?,?,0) "
                        "ON CONFLICT(chat_id) DO UPDATE SET added_by=excluded.added_by", (CHAT, MGR))
    await svc.db.upsert_user(fake_user(MGR, "운영자"))
    await svc.db.upsert_user(u)
    return svc, bot, u


def _sent_to(bot):
    return [c[1] for c in bot.named("send_message")]


# ── 보유금이 목표를 넘으면 1회 표시, 미달·끔이면 안 함 ──────────────
@test
async def credit_over_goal_claims_once():
    svc, bot, u = await _goalroom()
    await core.credit(svc.db, CHAT, u.id, 2_500_000, "win:baccarat")
    row = await svc.db._one("SELECT status, points, goal FROM casino_goal_hit WHERE chat_id=? AND user_id=?", (CHAT, u.id))
    assert row and row["status"] == "new" and row["points"] >= GOAL and row["goal"] == GOAL
    await core.credit(svc.db, CHAT, u.id, 500_000, "win:baccarat")    # 또 지급해도
    n = (await svc.db._one("SELECT COUNT(*) n FROM casino_goal_hit WHERE chat_id=?", (CHAT,)))["n"]
    assert n == 1, "한 사람 한 번만"


@test
async def under_goal_or_off_no_claim():
    svc, bot, u = await _goalroom()
    await core.credit(svc.db, CHAT, u.id, 100_000, "win:baccarat")    # 20만 < 목표 (뮤테이션: after<goal 지우면 생김)
    assert not await svc.db._one("SELECT 1 FROM casino_goal_hit WHERE chat_id=?", (CHAT,))
    await svc.db.set_setting(CHAT, "casino_goal", 0)                  # 끔 (뮤테이션: goal<=0 지우면 생김)
    await core.credit(svc.db, CHAT, u.id, 9_000_000, "win:baccarat")
    assert not await svc.db._one("SELECT 1 FROM casino_goal_hit WHERE chat_id=?", (CHAT,))


# ── 집계: 코드가 기록에서만 (총베팅·총획득·순익·판수) ──────────────
@test
async def tally_sums_wager_win_net_plays():
    svc, bot, u = await _goalroom()
    await core.debit(svc.db, CHAT, u.id, 10_000, "bet:baccarat")
    await core.debit(svc.db, CHAT, u.id, 10_000, "bet:baccarat")
    await core.credit(svc.db, CHAT, u.id, 30_000, "win:baccarat")
    t = await goal.tally(svc.db, CHAT, u.id)
    assert t["wagered"] == 20_000 and t["won"] == 30_000 and t["net"] == 10_000 and t["plays"] == 2, t


# ── 틱: 운영자·방에 알림, status new→notified, 다시는 안 보냄 ──────────
@test
async def tick_notifies_operator_and_room_once():
    svc, bot, u = await _goalroom()
    await core.credit(svc.db, CHAT, u.id, 2_500_000, "win:baccarat")
    await goal.tick(svc, bot)
    to = _sent_to(bot)
    assert MGR in to, "봇 넣은 운영자한테 집계 알림"
    assert CHAT in to, "방 축하 카드"
    dm = next(c[2] for c in bot.named("send_message") if c[1] == MGR)
    assert "집계" in dm and "총베팅" in dm and "순익" in dm
    assert (await svc.db._one("SELECT status FROM casino_goal_hit WHERE chat_id=?", (CHAT,)))["status"] == "notified"
    bot.calls.clear()
    await goal.tick(svc, bot)                                        # 뮤테이션: new→notified UPDATE 지우면 또 보냄
    assert not bot.named("send_message"), "이미 알린 건 다시 안 보냄"


# ── 운영자는 방마다 자동(added_by) — contact 설정이 있으면 그게 우선 ──────
@test
async def contact_setting_overrides_operator():
    svc, bot, u = await _goalroom()
    CONTACT = 888
    await svc.db.upsert_user(fake_user(CONTACT, "정산담당", "paydesk"))
    await svc.db.set_setting(CHAT, "casino_goal_contact", "@paydesk")
    await core.credit(svc.db, CHAT, u.id, 2_500_000, "win:baccarat")
    await goal.tick(svc, bot)
    to = _sent_to(bot)
    assert CONTACT in to and MGR not in to, "지정한 문의 대상한테만 (운영자 자동 대신)"
    room_msg = next(c for c in bot.named("send_message") if c[1] == CHAT)
    assert room_msg[2] and "달성" in room_msg[2]


# ── 확인 버튼 → 처리 완료(done) ──────────────────────────────
@test
async def confirm_marks_done():
    svc, bot, u = await _goalroom()
    await core.credit(svc.db, CHAT, u.id, 2_500_000, "win:baccarat")
    await goal.tick(svc, bot)
    q = FakeQuery(MGR, fake_user(MGR, "운영자"), f"cs:goal:ok:{CHAT}:{u.id}")
    await goal.cb(svc, bot, q, ["ok", str(CHAT), str(u.id)])
    assert len(q.answers) == 1
    assert (await svc.db._one("SELECT status FROM casino_goal_hit WHERE chat_id=?", (CHAT,)))["status"] == "done"


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
