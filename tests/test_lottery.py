"""📅 출석 → 🎟 복권 → 🎁 경품 (sodam/lottery.py · panels/lottery.py, 2026-10-11 FOX 고객 '.출석하면 복권 … 확률 1프로 … 일주일에 2명').
돈·포인트 걸기 없음, 경품은 방장이 직접: python tests/run_all.py lottery"""
import asyncio
import time
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import lottery, tools
from sodam.panels import lottery as P
from sodam.permissions import Role

test, run_all = runner()
ROOM = -100777
ADMIN, A, B_, NEW = fake_user(51, "방장"), fake_user(52, "가나"), fake_user(53, "다라"), fake_user(54, "신입")
DAY = 86400


async def setup(enabled=True, odds=100.0, week_max=2):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN.id})
    await db.ensure_chat(ROOM, "방")
    await db.set_setting(ROOM, "lotto_enabled", enabled)
    await db.set_setting(ROOM, "lotto_odds", odds)
    await db.set_setting(ROOM, "lotto_week_max", week_max)
    now = time.time()
    for u in (ADMIN, A, B_, NEW):
        await db.upsert_user(u)
    for u in (ADMIN, A, B_):     # 10일 전 들어와서 어제 말함
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                        (ROOM, u.id, int(now - 10 * DAY), int(now)))
        await db.log_message(ROOM, u.id, 1, "안녕", ts=int(now - DAY))
    await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                    (ROOM, NEW.id, int(now - 3600), int(now)))
    await db.log_message(ROOM, NEW.id, 2, "하이", ts=int(now - 60))
    return SimpleNamespace(svc=svc, db=db, bot=FakeBot(admins=[ADMIN]))


@test
async def checkin_once_a_day_and_streak_counts_consecutive_kst_days():
    e = await setup()
    tz = e.svc.cfg.tz
    t0 = time.time() - 5 * DAY
    assert (await lottery.checkin(e.db, tz, ROOM, A.id, now=t0))["streak"] == 1
    assert not (await lottery.checkin(e.db, tz, ROOM, A.id, now=t0 + 60))["ok"], "하루 한 번"
    assert (await lottery.checkin(e.db, tz, ROOM, A.id, now=t0 + DAY))["streak"] == 2
    assert (await lottery.checkin(e.db, tz, ROOM, A.id, now=t0 + 3 * DAY))["streak"] == 1, "하루 빠지면 다시 1"
    rows = await asyncio.gather(*(lottery.checkin(e.db, tz, ROOM, B_.id, now=t0, give_ticket=True) for _ in range(5)))
    assert sum(r["ok"] for r in rows) == 1 and await lottery.open_tickets(e.db, ROOM, B_.id, now=t0) == 1, "연타 = 1장"


@test
async def weekly_cap_holds_when_many_scratch_at_once():
    e = await setup(week_max=2)
    tz = e.svc.cfg.tz
    users = [fake_user(600 + i, f"u{i}") for i in range(6)]
    for u in users:
        await lottery.checkin(e.db, tz, ROOM, u.id, give_ticket=True)
    rows = await asyncio.gather(*(lottery.scratch(e.db, tz, ROOM, u.id, 100, 2, "커피", roll=0) for u in users))
    assert sorted(r["result"] for r in rows) == ["soldout"] * 4 + ["win"] * 2, rows
    assert await lottery.week_left(e.db, tz, ROOM, 2) == 0
    assert (await lottery.scratch(e.db, tz, ROOM, users[0].id, 100, 2, "커피"))["result"] == "none", "복권 없으면 none"


@test
async def odds_roll_and_same_person_wins_once_in_four_weeks():
    e = await setup(week_max=10)
    tz = e.svc.cfg.tz
    for _ in range(3):
        await e.db._write("INSERT INTO lotto_tickets(chat_id, user_id, created) VALUES(?,?,?)", (ROOM, A.id, int(time.time())))
    assert (await lottery.scratch(e.db, tz, ROOM, A.id, 1.0, 10, "커피", roll=100))["result"] == "lose", "1% = 100 미만만"
    assert (await lottery.scratch(e.db, tz, ROOM, A.id, 1.0, 10, "커피", roll=99))["result"] == "win"
    assert (await lottery.scratch(e.db, tz, ROOM, A.id, 1.0, 10, "커피", roll=0))["result"] == "recent", "4주 안 두 번 당첨 X"
    assert "다른 분 차례" in (await lottery.eligible(e.db, ROOM, A.id, 3) or "")


@test
async def eligibility_new_member_silent_member_and_admin_get_no_ticket():
    e = await setup()
    assert await lottery.eligible(e.db, ROOM, A.id, 3) is None
    assert "3일" in await lottery.eligible(e.db, ROOM, NEW.id, 3)
    assert await lottery.eligible(e.db, ROOM, NEW.id, 0) is None
    await e.db._write("DELETE FROM messages WHERE user_id=?", (B_.id,))
    assert "7일" in await lottery.eligible(e.db, ROOM, B_.id, 3), "말 안 하고 출석만 = 부계정 의심"
    text, kb = await P.do_checkin(e.svc, e.bot, ROOM, ADMIN, Role.ADMIN)
    assert "관리자는 복권에서 빠져요" in text and "lot:s:" not in str(kb.inline_keyboard)
    text, kb = await P.do_checkin(e.svc, e.bot, ROOM, A, Role.MEMBER)
    assert "복권 1장" in text and kb.inline_keyboard[0][0].callback_data == f"lot:s:{A.id}"
    off = await setup(enabled=False)
    text, kb = await P.do_checkin(off.svc, off.bot, ROOM, A, Role.MEMBER)
    assert "출석" in text and "복권" not in text and [b.callback_data for b in kb.inline_keyboard[0]] == ["lot:f"], "복권 꺼진 방 = 출석 + 운세"


def press(user, data, chat=ROOM):
    box = []

    async def answer(text=None, show_alert=False):
        box.append(text)

    async def edit_message_text(text, **kw):
        box.append(("edited", text))
    q = SimpleNamespace(from_user=user, data=data, answer=answer, edit_message_text=edit_message_text,
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=5, text_html="🎁 당첨"))
    return q, box


@test
async def scratch_button_owner_only_win_notifies_admins_paid_once():
    e = await setup()
    await P.do_checkin(e.svc, e.bot, ROOM, A, Role.MEMBER)
    q, box = press(B_, f"lot:s:{A.id}")
    await P.on_button(e.svc, e.bot, q, ["s", str(A.id)])
    assert "본인" in box[0] and await lottery.open_tickets(e.db, ROOM, A.id) == 1, "남이 못 긁음"
    q, box = press(A, f"lot:s:{A.id}")
    await P.on_button(e.svc, e.bot, q, ["s", str(A.id)])
    room = [c for c in e.bot.named("send_message") if c[1] == ROOM]
    assert "당첨" in room[-1][2]
    [dm] = [c for c in e.bot.named("send_message") if c[1] == ADMIN.id]
    data = dm[3]["reply_markup"].inline_keyboard[0][0].callback_data
    assert data.startswith(f"lotp:{ROOM}:")
    q, box = press(A, data, chat=A.id)
    await P.on_paid(e.svc, e.bot, q, data.split(":")[1:])
    assert "관리자만" in box[0]
    q, box = press(ADMIN, data, chat=ADMIN.id)
    await P.on_paid(e.svc, e.bot, q, data.split(":")[1:])
    await P.on_paid(e.svc, e.bot, q, data.split(":")[1:])
    assert "지급 완료로" in box[0] and "이미" in box[-1]
    assert "✅ 지급" in await P.wins_text(e.svc, ROOM)


@test
async def ai_tool_checkin_and_scratch_post_to_room_and_stay_quiet():
    e = await setup()
    ctx = tools.ToolCtx(e.svc, e.bot, ROOM, A, Role.MEMBER, {})
    out = await P.t_attendance(ctx, {"action": "checkin"})
    assert ctx.quiet and "올렸음" in out
    ctx2 = tools.ToolCtx(e.svc, e.bot, ROOM, A, Role.MEMBER, {})
    await P.t_attendance(ctx2, {"action": "scratch"})
    assert ctx2.quiet and any("당첨" in c[2] for c in e.bot.named("send_message") if c[1] == ROOM)
    dm = tools.ToolCtx(e.svc, e.bot, A.id, A, Role.MEMBER, {})
    assert "그룹방" in await P.t_attendance(dm, {"action": "checkin"})
    assert "가나" in await P.rank_text(e.svc, ROOM)


@test
async def fortune_is_fixed_per_person_and_day_and_differs_between_people():
    from datetime import timedelta, timezone
    from sodam import fortune
    tz = timezone(timedelta(hours=9))
    t = 1_780_000_000
    assert fortune.today(tz, 5, t) == fortune.today(tz, 5, t + 3600), "같은 날 다시 봐도 같음"
    days = {fortune.today(tz, 5, t + i * 86400)["overall"] for i in range(20)}
    people = {fortune.today(tz, u, t)["overall"] for u in range(40)}
    assert len(days) > 3 and len(people) > 3
    grades = [fortune.today(tz, u, t)["grade"] for u in range(2000)]
    assert 100 < grades.count("🌟 대길") < 300 and grades.count("🌧 조심") < 200, "비율 대로 (대길 10%)"
    for u in range(300):
        f = fortune.today(tz, u, t)
        assert len(fortune.short(f)) <= 200 and 1 <= f["number"] <= 45
    e = await setup()
    q, box = press(B_, "lot:f")
    await P.on_button(e.svc, e.bot, q, ["f"])
    assert box[0] == fortune.short(fortune.today(e.svc.cfg.tz, B_.id)) and not e.bot.named("send_message"), "팝업만, 방에 안 올림"
    dm = tools.ToolCtx(e.svc, e.bot, A.id, A, Role.MEMBER, {})
    await P.t_attendance(dm, {"action": "fortune"})
    assert dm.quiet and "운세" in e.bot.named("send_message")[-1][2], "운세는 1:1 에서도"


@test
async def prize_input_refuses_links():
    try:
        lottery._prize("t.me/+abc 경품")
        raise AssertionError("링크 막아야 함")
    except ValueError:
        pass
    assert lottery._odds("1프로") == 1.0
    try:
        lottery._odds("90")
        raise AssertionError("50% 넘으면 거절")
    except ValueError:
        pass


if __name__ == "__main__":
    run_all()
