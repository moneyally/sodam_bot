"""🏆 채팅 순위 틀 (sodam/chatrank.py · panels/chatrank.py, 2026-10-12 고객 'FOX 이벤트 봇처럼 · 하드코딩 없이 방장이 꾸미게').
python tests/run_all.py chatrank"""
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import brand, chatrank as cr, menu
from sodam.menu import PanelCtx
from sodam.panels import brand as BP
from sodam.panels import chatrank as P

test, run_all = runner()
ROOM = -100931
ADMIN, A, B_, C = fake_user(81, "방장", "boss"), fake_user(82, "가나", "sin1n1"), fake_user(83, "다라"), fake_user(84, "마바", "k2")


async def setup(counts=None, yday=None):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN.id})
    await db.ensure_chat(ROOM, "순위방")
    now = int(time.time())
    from sodam.util import day_start
    today0, y0 = day_start(svc.cfg.tz, 0), day_start(svc.cfg.tz, 1)
    mid = 1
    for u in (ADMIN, A, B_, C):
        await db.upsert_user(u)
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)", (ROOM, u.id, now - 9e5, now))
    for u, n in (counts or []):
        for i in range(n):
            mid += 1
            await db.log_message(ROOM, u.id, mid, f"오늘 글 {i}번째", ts=max(today0, now - 600) + i % 60)
    for u, n in (yday or []):
        for i in range(n):
            mid += 1
            await db.log_message(ROOM, u.id, mid, f"어제 글 {i}번째", ts=y0 + 60 + i)
    return SimpleNamespace(svc=svc, db=db, bot=FakeBot(admins=[ADMIN]))


def ctx(e, uid=ADMIN.id, args=()):
    return PanelCtx(e.svc, e.bot, uid, ROOM, list(args))


def html_msg(text, html=None):
    m = FakeMsg(ADMIN.id, ADMIN, text)
    if html:                                   # 움직이는 이모지가 든 메시지 (util.rich_html 이 쓰는 칸)
        m.entities = (SimpleNamespace(type="custom_emoji"),)
        m.text_html = html
    return m


# ── 틀 검사 ───────────────────────────────────────────────
@test
async def templates_only_take_known_fields_and_no_links():
    assert cr.clean_tpl("{등수} {이름} — {횟수}회 {상품}", cr.LINE_FIELDS, 120, ("이름",))
    for bad, why in (("{등수} {닉네임}", "모르는 칸"), ("{등수} {횟수}", "이름 칸 필요"), ("{이름} https://x.com", "링크"),
                     ("{이름} t.me/abc", "링크"), ("<b>{이름}", "깨진 서식"), ("가" * 130 + "{이름}", "너무 긺")):
        try:
            cr.clean_tpl(bad, cr.LINE_FIELDS, 120, ("이름",))
            raise AssertionError(why)
        except ValueError:
            pass
    assert cr.clean_prizes("1등 50,000원\n2등: 40,000원\n\n3. 30,000원") == ["50,000원", "40,000원", "30,000원"]
    for bad in ("1등 t.me/+abc", "1등 @scam_admin", "가" * 40):
        try:
            cr.clean_prizes(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    icons = cr.clean_icons('🥇 2️⃣ <tg-emoji emoji-id="5368">🔥</tg-emoji>')
    assert icons == ["🥇", "2️⃣", '<tg-emoji emoji-id="5368">🔥</tg-emoji>'], icons
    assert cr.strip_custom('<tg-emoji emoji-id="1">🔥</tg-emoji> 1등') == "🔥 1등"


@test
async def broken_saved_values_fall_back_to_defaults():
    c = cr.load('{"period":"forever","top":99,"line":"<b>{이름}","names":"x","award":7,"prizes":["a"]}')
    assert (c.period, c.top, c.line, c.names, c.award, c.prizes) == ("today", 5, cr.DEFAULT["line"], "user", -1, ["a"])
    assert cr.load("not json").line == cr.DEFAULT["line"]


# ── 글 만들기 ─────────────────────────────────────────────
@test
async def render_fills_room_template_with_counts_prizes_and_my_place():
    c = cr.Cfg(prizes=["50,000원", "40,000원"], icons=['<tg-emoji emoji-id="9">1️⃣</tg-emoji>'],
               head="🦊 {기간}의 채팅 순위\n📅 {날짜}", foot="매일 자정 시상식")
    out = cr.render(c, "오늘", "10/12", [("sin1n1", 1819), ("<b>x</b>", 1292), ("c", 930)], [(0, "어제왕", 5, "50,000원")], "10/11", (7, 34))
    assert out.startswith("🦊 오늘의 채팅 순위\n📅 10/12"), out
    assert '<tg-emoji emoji-id="9">1️⃣</tg-emoji> sin1n1 — 1,819회 50,000원' in out, "방장 그림·상품·천 단위"
    assert "🥈 &lt;b&gt;x&lt;/b&gt; — 1,292회 40,000원" in out, "이름은 이스케이프 (이름으로 서식 못 넣음)"
    assert "🥉 c — 930회\n" in out, "상품 없는 등수는 꼬리 없이"
    assert "👤 나 — 7위 · 34회" in out and "어제 당첨자" in out and "1️⃣</tg-emoji> 어제왕 — 5회 50,000원" in out
    assert out.endswith("매일 자정 시상식")
    assert cr.show_name({"username": "sin1n1", "first_name": "가나"}, "mask") == "si***"
    assert cr.show_name({"username": None, "first_name": "가나다"}, "user") == "가나다"


@test
async def standings_count_by_room_rules_and_can_skip_admins():
    e = await setup([(ADMIN, 9), (A, 5), (B_, 3), (C, 1)])
    c = cr.Cfg(top=3)
    label, date, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert label == "오늘" and [r["user_id"] for r in rows] == [ADMIN.id, A.id, B_.id]
    c.no_admin = True
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert [r["user_id"] for r in rows] == [A.id, B_.id, C.id], "관리자 빼면 다음 사람이 올라옴"
    await e.db.set_setting(ROOM, "chat_min_chars", 10)        # 📊 세는 기준 그대로 따름 ('오늘 글 N번째' = 7~8글자)
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert rows == [], "글자 수 기준 안 맞으면 안 셈"
    await e.db.set_setting(ROOM, "chat_min_chars", 0)
    for i in range(6):                                          # 30초 안엔 다시 안 셈 (미니앱 열 때마다 하루치 GROUP BY X)
        await e.db.log_message(ROOM, C.id, 900 + i, "새로 쓴 긴 글이에요", ts=int(time.time()))
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert [r["user_id"] for r in rows] == [A.id, B_.id, C.id]
    cr._CACHE.clear()
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert [r["user_id"] for r in rows][0] == C.id
    assert await cr.my_place(e.svc, ROOM, B_.id, c) == (4, 3) and await cr.my_place(e.svc, ROOM, 999, c) == (0, 0)


# ── 방 명령 · 1:1 · 미니앱 ────────────────────────────────
@test
async def room_command_posts_template_with_menu_buttons_and_is_rate_limited():
    e = await setup([(A, 4), (B_, 2)], yday=[(B_, 6)])
    cfg = await cr.get(e.db, ROOM)
    cfg.prizes, cfg.head = ["치킨", "커피"], "🏆 <b>{기간} 채팅왕</b> {날짜}"
    await cr.save(e.db, ROOM, cfg, ADMIN.id)
    await brand.ensure(e.db, ROOM, "순위방")
    await brand.update(e.db, ROOM, ADMIN.id, buttons=[{"k": "link", "t": "가입하기", "v": "https://example.com/j"},
                                                      {"k": "contact", "t": "문의하기", "v": "help_me"}, {"k": "fortune"}])
    from sodam.commands import CmdCtx
    from sodam.permissions import Role
    msg = FakeMsg(ROOM, A, ".순위", message_id=77)
    c = CmdCtx(e.svc, e.bot, msg, ROOM, A, Role.MEMBER, [], "")
    await P.c_rank(c)
    [(_, chat, text, kw)] = e.bot.named("send_message")
    assert chat == ROOM and text.startswith("🏆 <b>오늘 채팅왕</b>") and "sin1n1 — 4회 치킨" in text, text
    assert "어제 당첨자" in text and "다라 — 6회 치킨" in text, "어제 1등 = 상품 1등 글"
    urls = [b.url for row in kw["reply_markup"].inline_keyboard for b in row]
    assert urls == ["https://example.com/j", "https://t.me/help_me"], "🎁 메뉴의 바로가기·문의 버튼이 같이 (운세 같은 기능 버튼은 X)"
    assert kw["reply_parameters"].message_id == 77
    await P.c_rank(c)
    assert len(e.bot.named("send_message")) == 1, "연달아 치면 조용히"


@test
async def me_command_shows_my_count_and_rank_in_room_template():
    e = await setup([(A, 4), (B_, 2)])
    from sodam.commands import CmdCtx, _INDEX
    from sodam.permissions import Role
    assert _INDEX["나"].fn is P.c_me and _INDEX["순위"].fn is P.c_rank, "고객이 쓰는 이름 그대로"
    c = CmdCtx(e.svc, e.bot, FakeMsg(ROOM, B_, ".나", message_id=5), ROOM, B_, Role.MEMBER, [], "")
    await P.c_me(c)
    [(_, chat, text, kw)] = e.bot.named("send_message")
    assert chat == ROOM and text == "📊 다라 님 — 오늘 채팅 2회 · 2위" and kw["reply_parameters"].message_id == 5, text
    await P.c_me(c)
    assert len(e.bot.named("send_message")) == 1, "한 사람 10초에 한 번"
    ok, _ = await P.in_me(ctx(e), html_msg("{이름}님 {기간} {횟수}개 {등수}위 🔥"))
    assert ok
    c2 = CmdCtx(e.svc, e.bot, FakeMsg(ROOM, A, ".나 어제"), ROOM, A, Role.MEMBER, ["어제"], "어제")
    await P.c_me(c2)
    assert e.bot.named("send_message")[-1][2] == "📊 sin1n1 님 — 어제 채팅 기록이 아직 없어요."
    c3 = CmdCtx(e.svc, e.bot, FakeMsg(ROOM, C, ".나"), ROOM, C, Role.MEMBER, [], "")
    cr._CACHE.clear()
    await P.c_me(c3)
    assert "기록이 아직 없어요" in e.bot.named("send_message")[-1][2]
    await e.db.log_message(ROOM, C.id, 999, "처음 쓴 글이에요", ts=int(time.time()))
    c4 = CmdCtx(e.svc, e.bot, FakeMsg(ROOM, A, ".나"), ROOM, A, Role.MEMBER, [], "")
    e.svc.db._write  # noqa
    await e.db._write("DELETE FROM claims WHERE k LIKE 'crkme:%'")
    await P.c_me(c4)
    assert e.bot.named("send_message")[-1][2] == "sin1n1님 오늘 4개 1위 🔥", "방장이 바꾼 .나 모양"
    ok, note = await P.in_me(ctx(e), html_msg("{이름} {등수}위"))
    assert not ok and "횟수" in note


@test
async def custom_emoji_refused_is_resent_as_plain_emoji():
    class Picky(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if "<tg-emoji" in text:
                from telegram.error import BadRequest
                raise BadRequest("Can't parse entities: custom emoji not allowed")
            return await super().send_message(chat_id, text, **kw)
    bot = Picky()
    await P.send_html(bot, 5, '<tg-emoji emoji-id="1">🔥</tg-emoji> <b>순위</b>')
    assert bot.named("send_message")[-1][2] == "🔥 <b>순위</b>"


@test
async def dm_button_and_mini_app_show_my_place_in_that_room_only():
    e = await setup([(A, 4), (B_, 2), (C, 1)])
    room = await brand.ensure(e.db, ROOM, "순위방")
    await brand.update(e.db, ROOM, ADMIN.id, enabled=True, buttons=[{"k": "rank"}, {"k": "checkin"}])
    room = await brand.get(e.db, ROOM)
    assert room.buttons[0]["t"] == "🏆 채팅 순위", "버튼 부품 'rank'"
    await BP.press(e.svc, e.bot, C, room, room.buttons[0])
    text = e.bot.named("send_message")[-1][2]
    assert e.bot.named("send_message")[-1][1] == C.id and "👤 나 — 3위 · 1회" in text, text
    st = await BP._state(e.svc, e.bot, room, {"id": B_.id, "first_name": "다라"})
    assert "rank" in st["tabs"] and st["rank"]["me"] == {"rank": 2, "n": 2}
    assert [r["name"] for r in st["rank"]["rows"]] == ["sin1n1", "다라", "k2"] and st["rank"]["rows"][1]["me"]
    real = P.app_data

    async def broken(*a, **k):
        raise RuntimeError("순위 계산 오류")
    P.app_data = broken
    try:
        st = await BP._state(e.svc, e.bot, room, {"id": B_.id})
    finally:
        P.app_data = real
    assert "rank" not in st["tabs"], "순위 계산이 실패하면 빈 칸을 안 보여 줌"
    await brand.update(e.db, ROOM, ADMIN.id, buttons=[{"k": "checkin"}])
    st = await BP._state(e.svc, e.bot, await brand.get(e.db, ROOM), {"id": B_.id})
    assert "rank" not in st["tabs"] and st["rank"] is None, "메뉴판에 없으면 미니앱 칸도 없음"


# ── 방장 화면 ─────────────────────────────────────────────
@test
async def admin_screen_inputs_change_only_this_room():
    e = await setup([(A, 2)])
    ok, note = await P.in_prizes(ctx(e, args=[]), html_msg("1등 5만원\n2등 3만원"))
    assert ok and (await cr.get(e.db, ROOM)).prizes == ["5만원", "3만원"], note
    ok, note = await P.in_line(ctx(e), html_msg("{등수} {닉} {횟수}"))
    assert not ok and "모르는 칸" in note and (await cr.get(e.db, ROOM)).line == cr.DEFAULT["line"]
    ok, _ = await P.in_line(ctx(e), html_msg("x", '<tg-emoji emoji-id="7">👑</tg-emoji> {등수}위 {이름} ({횟수}) {상품}'))
    assert ok and (await cr.get(e.db, ROOM)).line.startswith("<tg-emoji"), "붙여 넣은 움직이는 이모지 그대로 보관"
    ok, _ = await P.in_icons(ctx(e), html_msg("🦊 🐺 🐻"))
    assert ok and (await cr.get(e.db, ROOM)).icons == ["🦊", "🐺", "🐻"]
    ok, _ = await P.in_foot(ctx(e), html_msg("매일 자정 시상식"))
    ok2, _ = await P.in_foot(ctx(e), html_msg("없음"))
    assert ok and ok2 and (await cr.get(e.db, ROOM)).foot == ""
    sc = await P.r_period(ctx(e))
    assert (await cr.get(e.db, ROOM)).period == "yesterday" and "어제" in sc.toast
    sc = await P.r_top(ctx(e))
    assert (await cr.get(e.db, ROOM)).top == 7
    await P.r_award(ctx(e))
    assert (await cr.get(e.db, ROOM)).award == 0
    await P.r_reset(ctx(e))
    got = await cr.get(e.db, ROOM)
    assert got.prizes == [] and got.icons == [] and got.award == 0, "처음으로 = 모양만, 시상식 시각은 그대로"
    assert await cr.get(e.db, -1) == cr.Cfg(), "다른 방은 그대로 기본"
    assert "crk" in [i.code for i in menu.HUB_ITEMS]


# ── 시상식 ────────────────────────────────────────────────
@test
async def award_posts_yesterday_once_and_admin_marks_paid_once():
    e = await setup([(A, 1)], yday=[(B_, 5), (C, 3), (A, 1)])
    cfg = cr.Cfg(top=3, prizes=["치킨", "커피"], award=0)
    await cr.save(e.db, ROOM, cfg, ADMIN.id)
    day = cr.day_of(e.svc.cfg.tz, 1)
    assert await P.award(e.svc, e.bot, ROOM, cfg, day)
    room = [c for c in e.bot.named("send_message") if c[1] == ROOM]
    assert "시상식" in room[0][2] and "다라 — 5회 치킨" in room[0][2] and "k2 — 3회 커피" in room[0][2]
    rows = await e.db._all("SELECT rank, user_id, prize FROM rank_awards WHERE chat_id=? AND day=? ORDER BY rank", (ROOM, day))
    assert [(r["rank"], r["user_id"], r["prize"]) for r in rows] == [(1, B_.id, "치킨"), (2, C.id, "커피"), (3, A.id, "")]
    again = [{"user_id": C.id, "n": 99, "username": "k2", "first_name": "마바"}]
    assert await cr.record_awards(e.db, ROOM, day, cfg, again) == 0, "같은 날 두 번 기록 X"
    assert (await e.db._one("SELECT user_id FROM rank_awards WHERE chat_id=? AND day=? AND rank=1", (ROOM, day)))["user_id"] == B_.id
    [dm] = [c for c in e.bot.named("send_message") if c[1] == ADMIN.id]
    datas = [b.callback_data for row in dm[3]["reply_markup"].inline_keyboard for b in row]
    assert datas == [f"rkp:{ROOM}:{day}:1", f"rkp:{ROOM}:{day}:2"], "상품 있는 등수만 버튼"
    box = []

    async def answer(text=None, show_alert=False):
        box.append(text)

    async def edit(text, **kw):
        box.append(("edit", text))
    q = SimpleNamespace(from_user=A, answer=answer, edit_message_text=edit, message=SimpleNamespace(text_html="🏆", reply_markup=None))
    await P.on_paid(e.svc, e.bot, q, datas[0].split(":")[1:])
    assert "관리자만" in box[-1]
    q.from_user = ADMIN
    await P.on_paid(e.svc, e.bot, q, datas[0].split(":")[1:])
    await P.on_paid(e.svc, e.bot, q, datas[0].split(":")[1:])
    assert any(isinstance(x, tuple) and "1등 지급 완료" in x[1] for x in box) and box[-1].startswith("이미")
    _, yd = await cr.yday_winners(e.svc, ROOM, cfg)
    assert yd[0][1:] == ("다라", 5, "치킨"), "어제 당첨자 칸 = 시상식 기록"
    cfg.names = "mask"
    _, yd = await cr.yday_winners(e.svc, ROOM, cfg)
    assert [x[1] for x in yd] == ["다***", "k***", "si***"], "가리기면 기록된 당첨자도 가림"


@test
async def award_tick_runs_once_per_room_per_day_at_the_chosen_hour():
    e = await setup(yday=[(A, 2)])
    from datetime import datetime
    hour = datetime.now(e.svc.cfg.tz).hour
    real = cr.AWARD_HOURS
    cr.AWARD_HOURS = tuple(sorted({*real, hour, (hour + 1) % 24}))          # 지금 시각을 시상식 시각으로 (시험 시각과 무관하게)
    try:
        await cr.save(e.db, ROOM, cr.Cfg(award=(hour + 1) % 24), ADMIN.id)
        await P._award_tick(e.svc, e.bot)
        assert not [c for c in e.bot.named("send_message") if c[1] == ROOM], "다른 시각이면 안 올림"
        await cr.save(e.db, ROOM, cr.Cfg(award=hour), ADMIN.id)
        await P._award_tick(e.svc, e.bot)
        await P._award_tick(e.svc, e.bot)
    finally:
        cr.AWARD_HOURS = real
    posts = [c for c in e.bot.named("send_message") if c[1] == ROOM]
    assert len(posts) == 1, "그 시각에 하루 한 번만"


if __name__ == "__main__":
    run_all()
