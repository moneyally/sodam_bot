"""🎉 포인트 이벤트 (2026-10-17 블랙컴퍼니 '토끼가 퀴즈 내고 정답자에게 리필 · 12~3시 제일 많이 딴 사람'): python tests/run_all.py casino_event

진행자 지정(관리자) → 진행자 !지급 은 자기 잔액에서만 · 멤버는 못 씀 · 이벤트 시간 안 게임 순수익만 순위 · '1억'·'3천만' 금액.
"""
import asyncio
import time
from datetime import datetime

from fakes import fake_user, runner
from test_casino_cards import CHAT, Msg, cmd, setup

import sodam.__main__  # noqa: F401
from sodam import casino
from sodam.casino import core, event

test, run_all = runner()


async def world():
    svc, bot, (host, guest, admin) = await setup(n_users=3, start=10_000)
    svc.perms.admins = {admin.id}
    for u in (host, guest, admin):
        await svc.db.touch_member(CHAT, u.id)
    return svc, bot, host, guest, admin


async def reply_cmd(svc, bot, user, text, to):
    msg = Msg(CHAT, user, text)
    msg.reply_to_message = Msg(CHAT, to, "정답!")
    core._last_bet.clear()
    assert await casino.dispatch(svc, bot, msg, CHAT, user, None, text)
    return msg


@test
async def amounts_read_eok_and_cheonman():
    assert core.parse_amount("1억", 0) == 100_000_000 and core.parse_amount("3천만", 0) == 30_000_000
    assert core.parse_amount("5만", 0) == 50_000 and core.parse_amount("2천", 0) == 2_000
    assert event.big(1_025_000_000) == "10억 2,500만P" and event.big(0) == "0P"


@test
async def host_pays_from_own_balance_only():
    svc, bot, host, guest, admin = await world()
    db = svc.db
    m = await reply_cmd(svc, bot, guest, "!지급 5천", host)
    assert "진행자·관리자만" in m.replies[-1] and await core.balance(db, CHAT, host.id) == 10_000, "멤버는 못 씀"
    m = await reply_cmd(svc, bot, guest, "!진행자", host)
    assert "관리자만" in m.replies[-1] and not event._hosts(await db.get_settings(CHAT))
    await reply_cmd(svc, bot, admin, "!진행자", host)
    assert event._hosts(await db.get_settings(CHAT)) == {host.id}
    await core.credit(db, CHAT, host.id, 500_000_000, "owner_grant")
    m = await reply_cmd(svc, bot, host, "!지급 1억", guest)
    assert "1억P" in m.replies[-1], m.replies
    assert await core.balance(db, CHAT, guest.id) == 100_010_000 and await core.balance(db, CHAT, host.id) == 400_010_000
    m = await reply_cmd(svc, bot, host, "!지급 10억", guest)
    assert "모자라요" in m.replies[-1] and await core.balance(db, CHAT, host.id) == 400_010_000, "잔액보다 많이는 X"
    m = await reply_cmd(svc, bot, host, "!지급 1억", host)
    assert "자기 자신" in m.replies[-1]
    rows = await db._all("SELECT user_id, delta, reason FROM casino_ledger WHERE reason LIKE 'pay_%'")
    assert sorted((r["delta"], r["reason"].split(":")[0]) for r in rows) == [(-100_000_000, "pay_out"), (100_000_000, "pay_in")]
    await reply_cmd(svc, bot, admin, "!진행자", host)            # 다시 = 해제
    m = await reply_cmd(svc, bot, host, "!지급 1만", guest)
    assert "진행자·관리자만" in m.replies[-1]


@test
async def event_rank_counts_only_game_profit_inside_window():
    svc, bot, host, guest, admin = await world()
    db = svc.db
    now = int(time.time())
    await db.set_setting(CHAT, "casino_event", f"{now - 3600},{now + 3600},{1_000_000}")
    rows = [(guest.id, -500_000, "bet:baccarat", now - 100), (guest.id, 2_000_000, "win:baccarat", now - 90),
            (host.id, -100_000, "bet:baccarat", now - 80), (host.id, 300_000, "win:baccarat", now - 70),
            (host.id, 50_000_000, "pay_in:1", now - 60), (host.id, 9_000_000, "bailout", now - 50),
            (admin.id, 9_000_000, "win:baccarat", now - 7200)]           # 시작 전 당첨 = 안 셈
    for uid, d, r, ts in rows:
        await db._write("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)", (CHAT, uid, d, r, ts))
    st = await event.standings(db, CHAT, now - 3600, now + 1)
    assert [(r["user_id"], r["net"]) for r in st] == [(guest.id, 1_500_000), (host.id, 200_000)], st
    m = await cmd(svc, bot, guest, "!이벤트순위")
    text = m.replies[-1]
    assert "🥇" in text and "150만P" in text and "목표 미달" in text and "진행 중" in text, text
    m = await cmd(svc, bot, guest, "!이벤트 00:00 03:00 10억")
    assert "관리자만" in m.replies[-1]
    st = await event.standings(db, CHAT, now - 3600, now + 1, games="바카라")
    assert [r["user_id"] for r in st] == [guest.id, host.id]
    await db._write("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                    (CHAT, admin.id, 5_000_000, "win:hilo", now - 10))
    assert admin.id not in [r["user_id"] for r in await event.standings(db, CHAT, now - 3600, now + 1, games="바카라")], "바카라만"
    assert admin.id in [r["user_id"] for r in await event.standings(db, CHAT, now - 3600, now + 1)]
    m = await cmd(svc, bot, admin, "!이벤트 00:00 03:00 10억 바카라")
    s, e, goal, games = event._event(await db.get_settings(CHAT))
    assert goal == 1_000_000_000 and e - s == 3 * 3600 and e > time.time() and games == "바카라", (s, e)
    assert datetime.fromtimestamp(s, svc.cfg.tz).hour == 0


@test
async def join_gift_follows_room_setting():
    from types import SimpleNamespace
    from test_casino_core import A, CHAT as C2, FakeMsg, setup as core_setup
    db, svc, bot, ctx = await core_setup()
    m = FakeMsg(C2, A, "!가입")
    m.chat, m.sender_chat = SimpleNamespace(id=C2, title="S", type="supergroup"), None
    await db.set_setting(C2, "casino_join_points", 5_000_000)
    await casino.dispatch(svc, bot, m, C2, A, 0, "!가입")
    assert await core.balance(db, C2, A.id) == 5_000_000, "방 설정 가입 선물"
    assert await core.join_points(db, C2 + 1) == core.START_POINTS, "다른 방은 기본"


@test
async def plan_rolls_to_next_day_and_handles_midnight():
    tz = datetime.now().astimezone().tzinfo
    now = datetime(2026, 10, 17, 20, 50, tzinfo=tz)
    s, e = event.plan(now, "00:00", "03:00")
    assert datetime.fromtimestamp(s, tz) == datetime(2026, 10, 18, 0, 0, tzinfo=tz) and e - s == 3 * 3600
    s, e = event.plan(datetime(2026, 10, 18, 1, 0, tzinfo=tz), "00:00", "03:00")   # 진행 중 → 오늘 그대로
    assert datetime.fromtimestamp(s, tz) == datetime(2026, 10, 18, 0, 0, tzinfo=tz)
    s, e = event.plan(now, "22:00", "01:00")
    assert e - s == 3 * 3600


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
