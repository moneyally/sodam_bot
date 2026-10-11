"""🎁 방 전용 1:1 메뉴 (sodam/brand.py · brandcard.py · panels/brand.py, 2026-10-12 고객 'AI 픽스터처럼 · 다른 방과 안 겹치게 · 하드코딩 없이').
python tests/run_all.py brand"""
import json
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import brand, brandcard, hooks, lottery, menu, miniapp
from sodam.panels import brand as P

test, run_all = runner()
RA, RB = -100801, -100802
ADMIN, A, OUT = fake_user(61, "방장"), fake_user(62, "가나"), fake_user(63, "밖사람")
DAY = 86400


class Bot(FakeBot):
    async def set_chat_menu_button(self, chat_id=None, menu_button=None, **kw):
        self.calls.append(("menu_button", chat_id, menu_button))
        return True


async def setup(both=True):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN.id})
    now = time.time()
    for room, title in ((RA, "에이방"), (RB, "비방")):
        await db.ensure_chat(room, title)
        await db.set_setting(room, "lotto_enabled", True)
        await db.set_setting(room, "lotto_odds", 100.0)
        await db.set_setting(room, "lotto_min_days", 0)
        for u in (ADMIN, A):
            await db.upsert_user(u)
            await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                            (room, u.id, int(now - 10 * DAY), int(now) - (0 if room == RA else 100)))
            await db.log_message(room, u.id, 1, "안녕하세요", ts=int(now - 3600))
    await db.upsert_user(OUT)
    ra = await brand.ensure(db, RA, "에이방")
    await brand.update(db, RA, ADMIN.id, enabled=True)
    rb = await brand.ensure(db, RB, "비방")
    if both:
        await brand.update(db, RB, ADMIN.id, enabled=True, buttons=[{"k": "checkin", "t": "출석!"}, {"k": "lotto", "t": "비방 복권"}])
    return SimpleNamespace(svc=svc, db=db, bot=Bot(admins=[ADMIN]), ra=ra, rb=rb)


def dm(user, text):
    m = FakeMsg(user.id, user, text)
    return m


# ── 값 검사 (방장이 채우는 값) ────────────────────────────
@test
async def values_are_checked_before_saving():
    assert brand.clean_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    for bad in ("http://x.com", "javascript:alert(1)", "https://x", "https://a.com/\"><script>"):
        try:
            brand.clean_url(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert brand.clean_contact("@my_help") == "my_help" and brand.clean_contact("https://t.me/my_help") == "my_help"
    assert brand.clean_button({"k": "link", "t": "가기"}) is None, "값 없는 바로가기는 버림"
    assert brand.clean_button({"k": "nope"}) is None
    b = brand.clean_button({"k": "checkin", "t": " 출석 ", "s": "danger", "w": 2, "i": "5368324170671202286"})
    assert b == {"k": "checkin", "t": "출석", "s": "danger", "w": 2, "i": "5368324170671202286"}, b
    assert brand.clean_button({"k": "checkin", "s": "rainbow", "i": "abc"}) == {"k": "checkin", "t": "📅 출석체크"}
    many = brand.clean_buttons([{"k": "fortune", "t": f"운세{i}"} for i in range(20)])
    assert len(many) == brand.MAX_BUTTONS, "8개까지"
    dup = brand.clean_buttons([{"k": "fortune", "t": "운세"}, {"k": "checkin", "t": "운세"}, {"k": "lotto"}])
    assert [b["k"] for b in dup] == ["fortune", "lotto"], "같은 글자 버튼은 하나만 (누르면 어느 쪽인지 모름)"
    try:
        brand.clean_card_text("win_title", "잭팟 {prize}")
        raise AssertionError("당첨 제목엔 {prize} 칸 없음")
    except ValueError:
        pass
    assert brand.clean_card_text("win_sub", "{prize} 축하") == "{prize} 축하"


@test
async def texts_and_colors_are_room_choices_with_defaults():
    r = brand.Room(1, "x")
    assert r.text("checkin_title", streak=4) == "출석 4일째" and r.colors["accent"] == brand.THEMES["navy"]["accent"]
    r.texts, r.custom = {"checkin_title": "{streak}일 연속 🔥"}, {"accent": (1, 2, 3)}
    assert r.text("checkin_title", streak=4) == "4일 연속 🔥" and r.colors["accent"] == (1, 2, 3)
    db = await make_db()
    await brand.ensure(db, RA, "방")
    await brand.update(db, RA, 1, texts={"win_title": "잭팟", "zzz": "x"}, colors={"accent": "#ff0000", "bg1": "nope"})
    got = await brand.get(db, RA)
    assert got.texts == {"win_title": "잭팟"} and got.custom == {"accent": (255, 0, 0)}, (got.texts, got.custom)


# ── 다른 방과 안 섞이게 ───────────────────────────────────
@test
async def deep_link_opens_only_for_members_and_remembers_the_room():
    e = await setup()
    e.bot.member_status = {(RA, OUT.id): "left"}
    msg = dm(OUT, "")
    await P.on_deep(e.svc, e.bot, msg, e.ra.code)
    assert any("멤버만" in r for r in msg.replies) and await brand.selected(e.db, OUT.id) is None, "방 멤버가 아니면 안 열림"
    e.bot.member_status = {}
    await P.on_deep(e.svc, e.bot, dm(OUT, ""), e.ra.code)          # 말 안 한 새 멤버: DB 엔 없어도 텔레그램이 멤버라면 열림
    assert await brand.selected(e.db, OUT.id) == RA and await brand.is_member(e.db, RA, OUT.id)
    await P.on_deep(e.svc, e.bot, dm(A, ""), e.ra.code)
    assert await brand.selected(e.db, A.id) == RA
    photo = e.bot.named("send_photo")[-1]
    kb = photo[4]["reply_markup"].to_dict()
    assert kb["is_persistent"] and kb["keyboard"][0][0]["style"] == "danger", kb   # 기본 첫 버튼 = 빨강·넓게
    assert any(row[0]["text"] == P.SWITCH for row in kb["keyboard"]), "방이 둘이면 방 바꾸기"
    msg = dm(A, "")
    await P.on_deep(e.svc, e.bot, msg, "0000000000")
    assert any("닫혀" in r for r in msg.replies), "없는 코드"


@test
async def checkin_from_the_menu_counts_in_the_selected_room_only():
    e = await setup()
    await brand.select(e.db, A.id, RB)
    assert await P.on_text(e.svc, e.bot, dm(A, "출석!"))
    assert await e.db._one("SELECT 1 FROM attend WHERE chat_id=? AND user_id=?", (RB, A.id))
    assert not await e.db._one("SELECT 1 FROM attend WHERE chat_id=? AND user_id=?", (RA, A.id)), "다른 방엔 출석 안 됨"
    assert await lottery.open_tickets(e.db, RB, A.id) == 1 and await lottery.open_tickets(e.db, RA, A.id) == 0
    # A방 메뉴를 고른 뒤 A방 복권을 긁어도 B방 복권은 그대로
    await brand.select(e.db, A.id, RA)
    await lottery.checkin(e.db, e.svc.cfg.tz, RA, A.id, give_ticket=True)
    q = SimpleNamespace(from_user=A, message=SimpleNamespace(chat=SimpleNamespace(id=A.id), chat_id=A.id), answers=[], media=[])

    async def answer(text=None, show_alert=False):
        q.answers.append(text)

    async def edit_message_media(media, reply_markup=None):
        q.media.append(media)
    q.answer, q.edit_message_media = answer, edit_message_media
    await P.on_button(e.svc, e.bot, q, ["s", str(RA)])
    assert q.media and q.media[0].has_spoiler, "결과는 가림막 사진 (눌러야 보임)"
    assert await lottery.open_tickets(e.db, RA, A.id) == 0 and await lottery.open_tickets(e.db, RB, A.id) == 1
    eff = [c for c in e.bot.named("send_message") if c[1] == A.id and c[3].get("message_effect_id")]
    assert eff, "당첨 = 🎉 화면 효과"


@test
async def forged_callbacks_for_rooms_you_are_not_in_are_refused():
    e = await setup()
    await lottery.checkin(e.db, e.svc.cfg.tz, RA, A.id, give_ticket=True)
    q = SimpleNamespace(from_user=OUT, message=SimpleNamespace(chat=SimpleNamespace(id=OUT.id), chat_id=OUT.id), answers=[])

    async def answer(text=None, show_alert=False):
        q.answers.append(text)
    q.answer = answer
    await P.on_button(e.svc, e.bot, q, ["s", str(RA)])            # 방 멤버가 아닌 사람이 콜백을 지어서 보냄
    assert q.answers and "쓸 수 없어요" in q.answers[0], q.answers
    await P.on_button(e.svc, e.bot, q, ["p", str(RA)])
    assert await brand.selected(e.db, OUT.id) is None, "방 고르기도 안 됨"
    q.from_user = A
    q.message = SimpleNamespace(chat=SimpleNamespace(id=-5), chat_id=-5)  # 방(그룹)에서 누름
    await P.on_button(e.svc, e.bot, q, ["s", str(RA)])
    assert "1:1" in q.answers[-1] and await lottery.open_tickets(e.db, RA, A.id) == 1, "1:1 에서만"


@test
async def plain_chat_is_not_taken_as_a_button_and_old_keyboards_find_their_room():
    e = await setup()
    assert not await P.on_text(e.svc, e.bot, dm(A, "출석 언제 해?")), "평범한 말은 AI 로"
    assert not await P.on_text(e.svc, e.bot, dm(OUT, "출석!")), "방이 없는 사람"
    await brand.forget(e.db, A.id)
    assert await P.on_text(e.svc, e.bot, dm(A, "비방 복권")), "고른 방이 없어도 그 글자를 가진 방이 하나면"
    assert await brand.selected(e.db, A.id) == RB


@test
async def closed_or_expired_room_falls_back_and_never_mixes():
    e = await setup()
    await brand.select(e.db, A.id, RB)
    await brand.update(e.db, RB, ADMIN.id, enabled=False)
    room = await brand.current(e.svc, A.id)
    assert room and room.chat_id == RA, "고른 방이 꺼지면 남은 한 방으로"
    await brand.update(e.db, RB, ADMIN.id, enabled=True)
    await brand.forget(e.db, A.id)
    assert await brand.current(e.svc, A.id) is None, "방이 둘인데 안 골랐으면 되묻기"
    await e.db._write("INSERT INTO member_left(chat_id, user_id, ts) VALUES(?,?,?)", (RB, A.id, int(time.time())))
    room = await brand.current(e.svc, A.id)
    assert room and room.chat_id == RA, "나간 방은 빠짐"


@test
async def keyboard_uses_telegram_button_style_icon_and_web_app():
    e = await setup()
    room = await brand.get(e.db, RA)
    room.buttons = brand.clean_buttons([{"k": "link", "t": "사이트", "v": "https://example.com", "s": "success"},
                                        {"k": "link", "t": "채널", "v": "https://t.me/example"},
                                        {"k": "lotto", "t": "복권", "i": "5368324170671202286"}])
    kb = P.keyboard(e.svc, room, False).to_dict()["keyboard"]
    flat = [b for row in kb for b in row]
    assert flat[0]["web_app"]["url"] == "https://example.com" and flat[0]["style"] == "success", "↗ 사이트 = web_app"
    assert "web_app" not in flat[1], "t.me 는 텔레그램이 열어야 해서 글 버튼"
    assert flat[2]["icon_custom_emoji_id"] == "5368324170671202286"
    assert "web_app" not in flat[2], "미니앱 서버가 꺼져 있으면 복권은 채팅 버튼"
    no_icons = P.keyboard(e.svc, room, False, icons=False).to_dict()["keyboard"]
    assert all("icon_custom_emoji_id" not in b for row in no_icons for b in row)


@test
async def refused_button_emoji_is_dropped_and_message_still_goes():
    from telegram.error import BadRequest
    e = await setup(both=False)
    await brand.update(e.db, RA, ADMIN.id, buttons=[{"k": "fortune", "t": "운세", "i": "5368324170671202286"}])
    sent = []

    async def send_photo(chat_id, photo, caption=None, **kw):
        if "icon_custom_emoji_id" in json.dumps(kw["reply_markup"].to_dict()):
            raise BadRequest("Custom emoji can't be used")
        sent.append(kw)
    e.bot.send_photo = send_photo
    P._icons_off[0] = False
    await P.open_room(e.svc, e.bot, A.id, await brand.get(e.db, RA))
    assert sent and P._icons_off[0], "이모지 빼고 다시 보냄"
    P._icons_off[0] = False


@test
async def admin_screens_add_edit_and_reorder_buttons():
    e = await setup(both=False)
    c = menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, [])
    sc = await P.s_bd(c)
    assert "입구 링크" in sc.text and brand.link("sodambot", e.ra) in sc.text
    await P.r_add(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["fortune"]))
    room = await brand.get(e.db, RA)
    assert room.buttons[-1]["k"] == "fortune" and room.buttons[-1]["t"] == "🔮 오늘의 운세 2", "같은 이름이면 번호"
    sc = await P.r_add(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["link"]))
    p = e.svc.inputs[ADMIN.id]
    assert p.kind == "bdval" and "https" in sc.text, "값이 필요한 버튼은 값부터"
    ok, _ = await P.in_newvalue(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, p.args), dm(ADMIN, "ftp://x"))
    assert not ok
    ok, _ = await P.in_newvalue(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, p.args), dm(ADMIN, "https://example.com"))
    room = await brand.get(e.db, RA)
    assert ok and room.buttons[-1] == {"k": "link", "t": "🔗 바로가기", "v": "https://example.com"}
    n = len(room.buttons)
    await P.r_style(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, [str(n - 1), "primary"]))
    await P.r_up(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, [str(n - 1)]))
    room = await brand.get(e.db, RA)
    assert room.buttons[n - 2]["k"] == "link" and room.buttons[n - 2]["s"] == "primary"
    await P.r_del(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, [str(n - 2)]))
    assert all(b["k"] != "link" for b in (await brand.get(e.db, RA)).buttons)
    ok, msg = await P.in_label(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["0"]), dm(ADMIN, P.SWITCH))
    assert not ok, "방 바꾸기 글자와 같으면 안 됨"


@test
async def card_text_and_icon_inputs():
    e = await setup(both=False)
    c = menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["win_title"])
    ok, _ = await P.in_text(c, dm(ADMIN, "잭팟 터짐"))
    assert ok and (await brand.get(e.db, RA)).text("win_title") == "잭팟 터짐"
    ok, _ = await P.in_text(c, dm(ADMIN, "기본"))
    assert (await brand.get(e.db, RA)).text("win_title") == "당첨!"
    m = dm(ADMIN, "x")
    m.entities = (SimpleNamespace(type="custom_emoji", custom_emoji_id="5368324170671202286"),)
    m.sticker = None
    ok, _ = await P.in_icon(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["0"]), m)
    assert ok and (await brand.get(e.db, RA)).buttons[0]["i"] == "5368324170671202286"
    m.entities = ()
    ok, _ = await P.in_icon(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["0"]), m)
    assert not ok, "프리미엄 이모지가 없으면 다시"


@test
async def cards_render_with_room_text_and_uploaded_background():
    import io
    from PIL import Image
    e = await setup(both=False)
    room = await brand.get(e.db, RA)
    room.texts = {"win_title": "잭팟", "lose_title": "꽝"}
    for kind in ("lotto", "win", "lose", "soldout", "checkin", "fortune", "welcome"):
        jpg = brandcard.render(kind, room, None, left=2, prize="예시 경품", streak=12, month=20, ticket=True, grade="🌟 대길",
                               line="예시 한 줄", number=7, color="금색")
        assert Image.open(io.BytesIO(jpg)).size == (1280, 720), kind
    src = io.BytesIO()
    Image.new("RGB", (900, 900), (200, 10, 10)).save(src, "PNG")
    await P._store_image(e.svc, RA, "bg", src.getvalue(), "upload")
    bg = await P.bg_bytes(e.svc, RA)
    assert Image.open(io.BytesIO(bg)).size == (1280, 720), "16:9 로 잘라 보관"
    assert await P.bg_bytes(e.svc, RA, "win") == bg, "당첨 그림이 없으면 배경"
    try:
        brandcard.normalize(b"not an image")
        raise AssertionError("사진 아님")
    except ValueError:
        pass
    assert brandcard.DISPLAY_FOR("갃") == brandcard.BODY, "Black Han Sans 에 없는 글자 → 나눔고딕"


@test
async def ticket_life_day_expires_at_midnight():
    db = await make_db()
    tz = (await make_svc(db)).cfg.tz
    from datetime import datetime, timedelta
    noon = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    yday = (noon - timedelta(days=1)).timestamp()
    await db._write("INSERT INTO lotto_tickets(chat_id, user_id, created) VALUES(?,?,?)", (RA, 5, int(yday)))
    assert await lottery.open_tickets(db, RA, 5, now=noon.timestamp(), life="7d", tz=tz) == 1
    assert await lottery.open_tickets(db, RA, 5, now=noon.timestamp(), life="day", tz=tz) == 0, "어제 받은 건 자정에 끝"
    r = await lottery.scratch(db, tz, RA, 5, 100, 5, "x", now=noon.timestamp(), life="day")
    assert r["result"] == "none"


@test
async def review_fixes_keyboard_web_app_limits_and_closed_rooms():
    e = await setup()
    room = await brand.get(e.db, RA)
    room.buttons = brand.clean_buttons([{"k": "lotto", "t": "복권"}, {"k": "app", "t": "혜택"}])
    miniapp._server = object()                                    # 미니앱이 켜져 있어도
    import os
    os.environ["MINIAPP_URL"] = "https://x.example/app"
    try:
        flat = [b for row in P.keyboard(e.svc, room, False).to_dict()["keyboard"] for b in row]
        assert all("web_app" not in b for b in flat), "메뉴판 web_app 은 initData 가 비어서 미니앱이 안 됨 → 글 버튼"
        kb = P._scratch_kb(e.svc, room, RA, 1).to_dict()
        assert kb["inline_keyboard"][0][1]["web_app"]["url"].startswith("https://x.example/app/?r="), "글 아래 버튼으로 엶"
    finally:
        miniapp._server = None
        os.environ.pop("MINIAPP_URL", None)
    try:
        brand.clean_label(".출석")
        raise AssertionError("점으로 시작하면 명령어로 읽혀 안 눌림")
    except ValueError:
        pass
    # AI 배경: 방 그림 한도(image_daily) 를 같이 씀 — 0 이면 못 그림
    await e.db.set_setting(RA, "image_daily", 0)
    called = []

    async def image(*a, **k):
        called.append(1)
        return b"x"
    e.svc.llm = SimpleNamespace(image=image)
    ok, msg = await P.in_ai(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, ["bg"]), dm(ADMIN, "금화 카지노"))
    assert not called and "한도" in msg, msg
    assert await e.db.counter(P._kst_day(e.svc), RA, "image") == 0, "거절이면 한도 되돌림"
    # 큰 그림 거절
    import io
    from PIL import Image
    big = io.BytesIO()
    Image.new("1", (8000, 6000)).save(big, "PNG")
    try:
        brandcard.normalize(big.getvalue())
        raise AssertionError("4천만 픽셀 넘음")
    except ValueError:
        pass
    # 고른 방이 닫히면 옛 메뉴판을 누를 때 메뉴판을 치움
    await brand.select(e.db, A.id, RB)
    await brand.update(e.db, RB, ADMIN.id, enabled=False)
    await brand.update(e.db, RA, ADMIN.id, enabled=False)
    m = dm(A, "출석!")
    assert await P.on_text(e.svc, e.bot, m)
    assert "닫혀" in m.replies[-1] and type(m.reply_kws[-1]["reply_markup"]).__name__ == "ReplyKeyboardRemove"
    # 소담이 못 본 사이 밴된 사람: 1:1 출석 안 됨
    await brand.update(e.db, RA, ADMIN.id, enabled=True)
    await brand.select(e.db, A.id, RA)
    e.bot.member_status = {(RA, A.id): "kicked"}
    assert await P.on_text(e.svc, e.bot, dm(A, "📅 출석체크"))
    assert not await e.db._one("SELECT 1 FROM attend WHERE chat_id=? AND user_id=?", (RA, A.id)), "밴된 사람 출석 X"


@test
async def admin_preview_works_on_a_disabled_room_for_an_hour():
    e = await setup(both=False)
    await brand.update(e.db, RA, ADMIN.id, enabled=False)
    await e.db._write("DELETE FROM members WHERE chat_id=? AND user_id=?", (RA, ADMIN.id))   # 관리자가 멤버 기록에 없어도
    sc = await P.r_preview(menu.PanelCtx(e.svc, e.bot, ADMIN.id, RA, []))
    assert "미리보기" in sc.toast or "띄웠" in sc.toast
    n = len(e.bot.named("send_photo"))
    assert await P.on_text(e.svc, e.bot, dm(ADMIN, "🔮 오늘의 운세"))
    assert len(e.bot.named("send_photo")) == n + 1, "꺼진 방도 미리보기 중엔 운세 카드가 감"
    await e.db._write("UPDATE brand_session SET ts=ts-? WHERE user_id=?", (brand.PREVIEW_SEC + 5, ADMIN.id))
    assert not await P.on_text(e.svc, e.bot, dm(A, "🔮 오늘의 운세")), "멤버는 꺼진 방 메뉴 못 씀"
    assert await brand.usable(e.svc, RA, ADMIN.id) is None, "1시간 지나면 미리보기 끝"
    assert menu.ROUTES["bdv"].need == menu.ADMIN


@test
async def a_failing_button_never_falls_through_to_ai():
    e = await setup(both=False)
    await brand.select(e.db, A.id, RA)

    async def boom(*a, **k):
        raise RuntimeError("사진 서버 오류")
    e.bot.send_photo = boom
    m = dm(A, "🔮 오늘의 운세")
    assert await P.on_text(e.svc, e.bot, m), "버튼으로 맞았으면 오류가 나도 True (AI 로 안 감)"
    assert "다시" in m.replies[-1]


@test
async def hooks_wired():
    assert P.on_text in hooks.DM_TEXT_HOOKS and "b" in hooks.DEEP_LINKS and "bd" in hooks.CALLBACK_HANDLERS
    assert miniapp.start in hooks.STARTUP_HOOKS and {"state", "checkin", "scratch"} <= set(miniapp.API)
    assert "bd" in menu.ROUTES and "bdm" in menu.ROUTES


if __name__ == "__main__":
    run_all()
