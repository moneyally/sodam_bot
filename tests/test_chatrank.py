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
    # both = 이름 + @아이디 (2026-10-26 泰山 '아이디도 나오게')
    assert cr.show_name({"username": "senjo", "first_name": "센조"}, "both") == "센조 (@senjo)"
    assert cr.show_name({"username": None, "first_name": "센조"}, "both") == "센조"        # 아이디 없으면 이름만
    assert cr.show_name({"username": "onlyid", "first_name": ""}, "both") == "@onlyid"       # 이름 없으면 @아이디만
    assert "both" in cr.NAME_MODES
    assert cr.show_name({"username": None, "first_name": "가나다"}, "user") == "가나다"
    assert cr.show_name({"user_id": 8263231614, "username": None, "first_name": "@"}, "user") == "멤버1614", "'@' 뿐인 이름 (백악관 실제)"
    assert cr.show_name({"user_id": 8019304490, "username": None, "first_name": "\u2063"}, "name") == "멤버4490", "투명 글자뿐인 이름"


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


@test
async def member_reset_zeroes_only_that_person():
    e = await setup([(ADMIN, 9), (A, 5), (B_, 3), (C, 1)])
    c = cr.Cfg(top=4)
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert [r["user_id"] for r in rows] == [ADMIN.id, A.id, B_.id, C.id]
    # A(도배범)만 초기화 → A 는 순위에서 빠지고(0), 남들은 그대로
    await cr.member_reset(e.db, ROOM, A.id, ADMIN.id)
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    ids = [r["user_id"] for r in rows]
    assert A.id not in ids, "초기화한 사람은 순위에서 빠짐"
    assert ids == [ADMIN.id, B_.id, C.id], "남들은 그대로"
    assert await cr.my_place(e.svc, ROOM, A.id, c) == (0, 0), "본인은 0"
    assert await cr.my_place(e.svc, ROOM, ADMIN.id, c) == (1, 9), "남 등수·수 안 바뀜"
    assert await cr.my_place(e.svc, ROOM, B_.id, c) == (2, 3), "A 빠져서 B 가 2등으로"
    # 범위: .통계용 top_chatters(user_cuts 기본 꺼짐)는 A 를 그대로 셈
    plain = {r["user_id"]: r["n"] for r in await e.db.top_chatters(ROOM, 0, 10)}
    assert plain.get(A.id) == 5, ".통계/리포트는 영향 없음 (글 안 지움)"
    cut = {r["user_id"]: r["n"] for r in await e.db.top_chatters(ROOM, 0, 10, user_cuts=True)}
    assert A.id not in cut, "순위 쿼리만 A 제외"
    # 취소 → 복구
    assert await cr.member_unreset(e.db, ROOM, A.id) is True
    _, _, rows = await cr.standings(e.svc, ROOM, c, None, e.bot)
    assert [r["user_id"] for r in rows] == [ADMIN.id, A.id, B_.id, C.id], "취소하면 원래 순위"
    assert await cr.member_unreset(e.db, ROOM, A.id) is False, "두 번째 취소는 없음"


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


@test
async def weekly_mode_counts_from_monday_and_awards_last_week_on_monday_midnight():
    """고객 2026-10-12 (백악관): '추첨은 일주일에 한 번 월요일 자정 00시' + 매주 0부터 다시."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Seoul")
    wed = datetime(2026, 10, 14, 15, 0, tzinfo=tz).timestamp()          # 수요일
    since, until, date = cr.span("thisweek", tz, wed)
    assert datetime.fromtimestamp(since, tz).strftime("%a %H") == "Mon 00" and until is None and date == "10/12~10/18"
    since, until, date = cr.span("lastweek", tz, wed)
    assert (datetime.fromtimestamp(since, tz).day, datetime.fromtimestamp(until, tz).day, date) == (5, 12, "10/5~10/11")
    c = cr.Cfg(period="thisweek", award=cr.WEEKLY)
    assert cr.award_key(tz, c, wed) == ("w2026-10-05", "lastweek") and cr.winners_label(c) == "지난주 당첨자"
    assert P.due(c, datetime(2026, 10, 19, 0, 5, tzinfo=tz)) and not P.due(c, datetime(2026, 10, 20, 0, 5, tzinfo=tz)), "월요일 0시만"
    assert not P.due(c, datetime(2026, 10, 19, 9, 0, tzinfo=tz))
    assert cr.award_key(tz, cr.Cfg(award=0), wed)[1] == "yesterday" and P.due(cr.Cfg(award=0), datetime(2026, 10, 20, 0, 1, tzinfo=tz))
    assert cr.load('{"period": "lastweek"}').period == "today", "방장이 고르는 기간은 PERIOD_CHOICES 만"
    # 지난 주에 쓴 글은 이번 주 순위에 안 들어감 (= 매주 0부터)
    e = await setup([(A, 3)])
    last = int(cr.span("lastweek", e.svc.cfg.tz)[0]) + 3600
    for i in range(9):
        await e.db.log_message(ROOM, B_.id, 700 + i, f"지난주 글 {i}번째", ts=last + i)
    wk = cr.Cfg(period="thisweek", award=cr.WEEKLY, prizes=["5만원"])
    label, _, rows = await cr.standings(e.svc, ROOM, wk, None, e.bot)
    assert label == "이번 주" and [r["user_id"] for r in rows] == [A.id]
    key, period = cr.award_key(e.svc.cfg.tz, wk)
    assert await P.award(e.svc, e.bot, ROOM, wk, key, period)
    post = [c for c in e.bot.named("send_message") if c[1] == ROOM][-1][2]
    assert "지난 주" in post and "다라 — 9회 5만원" in post, post
    [dm] = [c for c in e.bot.named("send_message") if c[1] == ADMIN.id]
    data = dm[3]["reply_markup"].inline_keyboard[0][0].callback_data
    assert data == f"rkp:{ROOM}:{key}:1" and data.count(":") == 3, "날짜 열쇠에 ':' 없음 (버튼 쪼개기)"
    html = await P.board(e.svc, e.bot, ROOM, wk)
    assert "지난주 당첨자" in html and "다라 — 9회 5만원" in html


@test
async def reset_counts_only_from_now_and_can_be_undone():
    """고객 2026-10-12 백악관 '채팅 수만 초기화돼요?' — 글은 안 지우고 그 뒤 글만 셈."""
    e = await setup([(A, 4), (B_, 2)])
    n_msgs = (await e.db._one("SELECT COUNT(*) n FROM messages WHERE chat_id=?", (ROOM,)))["n"]
    sc = await P.r_zero_ask(ctx(e))
    assert "0부터 다시" in sc.text and (await cr.get(e.db, ROOM)).reset_at == 0, "묻기만 (바로 안 바뀜)"
    await P.r_zero(ctx(e))
    cfg = await cr.get(e.db, ROOM)
    assert cfg.reset_at > 0
    _, _, rows = await cr.standings(e.svc, ROOM, cfg, None, e.bot)
    assert rows == [] and await cr.my_place(e.svc, ROOM, A.id, cfg) == (0, 0), "초기화 뒤엔 0"
    assert (await e.db._one("SELECT COUNT(*) n FROM messages WHERE chat_id=?", (ROOM,)))["n"] == n_msgs, "글은 그대로"
    await e.db.log_message(ROOM, C.id, 950, "초기화 뒤 첫 글이에요", ts=int(time.time()) + 2)
    cr._CACHE.clear()
    _, _, rows = await cr.standings(e.svc, ROOM, cfg, None, e.bot)
    assert [r["user_id"] for r in rows] == [C.id]
    assert "초기화 취소" in str((await P.s_crk(ctx(e))).kb.to_dict())
    await P.r_reset(ctx(e))
    assert (await cr.get(e.db, ROOM)).reset_at == cfg.reset_at, "모양 처음으로 해도 초기화는 그대로"
    await P.r_zero_undo(ctx(e))
    cfg = await cr.get(e.db, ROOM)
    _, _, rows = await cr.standings(e.svc, ROOM, cfg, None, e.bot)
    assert cfg.reset_at == 0 and [r["user_id"] for r in rows][:2] == [A.id, B_.id], "취소하면 원래 숫자"
    from sodam import tools
    from sodam.permissions import Role
    dm = tools.ToolCtx(e.svc, e.bot, ADMIN.id, ADMIN, Role.MEMBER, {})
    out = await P.t_chat_rank(dm, {"action": "set", "reset": "now"})
    assert "0부터 다시" in out and (await cr.get(e.db, ROOM)).reset_at > 0, "말로도 ('채팅 수 초기화해줘')"


@test
async def admin_sets_ranking_by_talking_in_dm_including_premium_emoji():
    """고객 2026-10-12: 1:1 에 '1등 5만원… 5글자 이상… 🥇🥈(움직이는 이모지)' 라고 말하면 그대로."""
    from sodam import tools
    from sodam.permissions import Role
    e = await setup([(A, 4), (B_, 2)])
    req = FakeMsg(ADMIN.id, ADMIN, "1등 5만원 2등 4만원, 5글자 이상만, 매주 월요일 추첨 🔥 👑")
    req.entities = (SimpleNamespace(type="custom_emoji"),)
    req.text_html = '1등 5만원 2등 4만원, 5글자 이상만 <tg-emoji emoji-id="11">🔥</tg-emoji> <tg-emoji emoji-id="22">👑</tg-emoji>'
    dm = tools.ToolCtx(e.svc, e.bot, ADMIN.id, ADMIN, Role.MEMBER, {}, request_msg=req)
    out = await P.t_chat_rank(dm, {"action": "set", "prizes": ["5만원", "4만원"], "min_chars": 5, "award": "weekly",
                                   "period": "thisweek", "icons_from_message": True, "head": "🏆 {기간} 채팅왕", "no_admin": True})
    cfg = await cr.get(e.db, ROOM)
    assert cfg.prizes == ["5만원", "4만원"] and cfg.award == cr.WEEKLY and cfg.period == "thisweek" and cfg.no_admin, out
    assert cfg.icons == ['<tg-emoji emoji-id="11">🔥</tg-emoji>', '<tg-emoji emoji-id="22">👑</tg-emoji>'], "숨은 번호는 메시지에서"
    assert cfg.head == "🏆 {기간} 채팅왕" and (await e.db.get_settings(ROOM))["chat_min_chars"] == 5
    prev = [c for c in e.bot.named("send_message") if c[1] == ADMIN.id][-1][2]
    assert prev.startswith("👀 <b>미리보기</b>") and "이번 주 채팅왕" in prev, "1:1 에 실제 모양"
    out = await P.t_chat_rank(dm, {"action": "set", "head": "{닉네임} 순위", "prizes": ["t.me/+abc"]})
    assert "못 넣은 것" in out and (await cr.get(e.db, ROOM)).head == "🏆 {기간} 채팅왕", "틀린 값은 안 들어감"
    member = tools.ToolCtx(e.svc, e.bot, A.id, A, Role.MEMBER, {})
    assert "관리자만" in await P.t_chat_rank(member, {"action": "set", "top": 3}) and (await cr.get(e.db, ROOM)).top == 5
    assert "[지금 설정]" in await P.t_chat_rank(dm, {"action": "show"})
    assert "chat_rank_setup" in tools._BY_NAME


@test
async def top_can_go_up_to_20_rows():
    """채팅 랭킹 20위까지 (New world 멤버 '20위까지 보고싶은데 10위만'). TOP_MAX=20."""
    assert cr.TOP_MAX == 20 and 20 in P.TOPS and 15 in P.TOPS, "20등 프리셋"
    # 저장·불러오기: 20 허용, 20 초과는 거절(기본 5), prizes/icons 도 20까지
    assert cr.load('{"top":20}').top == 20 and cr.load('{"top":25}').top == 5, "20 OK · 초과 거절"
    import json
    assert len(cr.load(json.dumps({"prizes": [f"{i}등" for i in range(25)]})).prizes) == 20, "상품도 20까지"
    # 21명이 서로 다르게 떠들면 standings 가 top(20)만큼 내보냄
    e = await setup()
    now = int(time.time()); mid = 1000
    from sodam.util import day_start
    today0 = day_start(e.svc.cfg.tz, 0)
    talkers = [fake_user(300 + i, f"유저{i:02d}") for i in range(21)]
    for i, u in enumerate(talkers):
        await e.db.upsert_user(u)
        await e.db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)", (ROOM, u.id, now - 9e5, now))
        for j in range(21 - i):                       # 유저00=21번 … 유저20=1번 (서로 다른 수)
            mid += 1
            await e.db.log_message(ROOM, u.id, mid, f"오늘 글 {j}번째", ts=max(today0, now - 600) + j % 50)
    cfg = await cr.get(e.db, ROOM)
    cfg.top = 20
    await cr.save(e.db, ROOM, cfg, ADMIN.id)
    assert (await cr.get(e.db, ROOM)).top == 20, "방장이 20등까지 저장"
    _, _, rows = await cr.standings(e.svc, ROOM, await cr.get(e.db, ROOM))
    assert len(rows) == 20, f"20줄까지 (TOP_MAX 10 이면 저장이 5로 떨어져 5줄): {len(rows)}"


@test
async def period_arg_picks_period_for_one_look_without_changing_room_default():
    """`.순위 저번주`·`.나 이번주` 처럼 끝에 기간을 붙이면 그 기간만 보여주고 방 기본은 안 바뀜 (New world 손님 '이번주/저번주 구분')."""
    e = await setup([(A, 4), (B_, 2)])
    from sodam.commands import CmdCtx
    from sodam.permissions import Role
    assert P._PERIOD_WORDS["저번주"] == "lastweek" and P._PERIOD_WORDS["이번주"] == "thisweek" and P._PERIOD_WORDS["오늘"] == "today"
    # .순위 저번주 → 지난주 기간, 방 기본(today)은 그대로
    c = CmdCtx(e.svc, e.bot, FakeMsg(ROOM, A, ".순위 저번주", message_id=11), ROOM, A, Role.MEMBER, ["저번주"], "저번주")
    await P.c_rank(c)
    [(_, _, text, _)] = e.bot.named("send_message")
    assert "지난 주" in text, text                  # lastweek 기간으로 (머리말 {기간})
    assert (await cr.get(e.db, ROOM)).period == "today", "1회성 조회라 방 기본 기간은 안 바뀜"
    # 모르는/없는 낱말이면 방 기본(오늘) — 저번주로 오해 X (period None → 방 기본 기간 사용)
    e.bot.calls.clear()
    await P.c_rank(CmdCtx(e.svc, e.bot, FakeMsg(ROOM, A, ".순위 ㅋㅋ", message_id=13), ROOM, A, Role.MEMBER, ["ㅋㅋ"], "ㅋㅋ"))
    assert "오늘" in e.bot.named("send_message")[0][2], "모르는 기간 낱말·인자 없음은 방 기본"
    # .나 이번주
    e.bot.calls.clear()
    await P.c_me(CmdCtx(e.svc, e.bot, FakeMsg(ROOM, B_, ".나 이번주", message_id=14), ROOM, B_, Role.MEMBER, ["이번주"], "이번주"))
    assert "이번 주" in e.bot.named("send_message")[0][2], e.bot.named("send_message")[0][2]


@test
async def top_over_ten_renders_without_icon_indexerror():
    # 라이브 버그(2026-10-20): TOP_MAX 20 인데 ICONS 10개 → top>10·아이콘 빈 방에서 IndexError 로 .순위 깨짐
    c = cr.Cfg(top=20)   # icons=[] 기본
    assert c.icon(9) in cr.ICONS and c.icon(10) == "11." and c.icon(19) == "20.", "11~20위는 번호로"
    rows = [(f"user{i}", 200 - i) for i in range(20)]
    out = cr.render(c, "오늘", "10/20", rows)   # IndexError 안 나야
    assert "11." in out and "20." in out and "user19" in out, out


if __name__ == "__main__":
    run_all()
