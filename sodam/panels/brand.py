"""🎁 방 전용 1:1 메뉴 — 손님 화면(아래쪽 메뉴판·그림 카드·미니앱 API)과 방장 꾸미기 화면. 데이터·검사는 sodam/brand.py.

텔레그램 공식 기능 (core.telegram.org/bots/api · /bots/webapps, 2026-10-12 확인):
- 아래쪽 메뉴판 = ReplyKeyboardMarkup(is_persistent) · 버튼 색 = KeyboardButton.style (danger 빨강·success 초록·primary 파랑, Bot API 9.4)
- 버튼 앞 프리미엄 이모지 = icon_custom_emoji_id — 봇 주인이 텔레그램 프리미엄(또는 Fragment 아이디)일 때만 받아 줌 → 거절되면 이모지 빼고 다시 보냄
- ↗ 바로가기 = KeyboardButton.web_app (1:1 만, https) · ≡ 메뉴 버튼 = setChatMenuButton(사람마다) → 미니앱
- 긁은 결과 = has_spoiler 사진(반짝이 가림막, 눌러야 보임) · 당첨 = message_effect_id 🎉 (1:1 만)
흐름: 방 공지의 [🎁 혜택 메뉴] → t.me/<봇>?start=b_<코드> → 그 방 메뉴판 · 미니앱 주소 /app/?r=<코드>
방장: 그룹 허브 🎁(m:bd) — 켜기·이름·색·버튼(부품+값)·카드 글자·그림(올리기·AI)·당첨 스티커·미리보기·방에 버튼 올리기.
테스트 tests/test_brand.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto, KeyboardButton, Message,
                      MenuButtonDefault, MenuButtonWebApp, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)
from telegram.error import BadRequest, Forbidden, TelegramError

from .. import brand, brandcard, fortune, hooks, lottery, menu, miniapp, persist, tools
from ..menu import ADMIN, PUBLIC, B, HubItem, PanelCtx, Route, Screen
from ..permissions import Role
from ..util import esc, mention
from . import lottery as lot

log = logging.getLogger(__name__)

SWITCH = "🏠 방 바꾸기"
EFFECT_PARTY = "5046509860389126442"     # 🎉 메시지 효과 (공식 앱의 기본 효과 id, 1:1 만)
STICKER_DAILY = 5                        # 방마다 하루 당첨 스티커 만들기 (스티커 엔진은 전체에서 한 번에 하나 — 멤버 스티커가 밀리지 않게)
PRESS_PER_MIN = 12                       # 한 사람 아래쪽 메뉴 누르기 분당
POST_GAP = 60                            # 방에 [🎁 혜택 메뉴] 글 올리기 간격
_icons_off = [False]                     # 이 봇이 버튼 이모지를 못 쓰면(프리미엄 아님) 프로세스 동안 빼고 보냄


# ── 공통 ──────────────────────────────────────────────────
def _kst_day(svc) -> str:
    from datetime import datetime
    return datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")


def _user_ns(u: dict):
    return SimpleNamespace(id=u["id"], first_name=u.get("first_name") or "멤버", last_name=u.get("last_name"),
                           username=u.get("username"), is_bot=False)


def app_url(svc, room: brand.Room) -> str | None:
    base = miniapp.public_base(svc)
    return f"{base}?r={room.code}" if base and miniapp.running() else None


def _kbtn(svc, room: brand.Room, b: dict, icons: bool) -> KeyboardButton:
    """버튼 색(style)·이모지(icon_custom_emoji_id)는 PTB 21 이 아직 모르는 칸 → api_kwargs 로 그대로 실림 (시험으로 확인)."""
    extra = {}
    if b.get("s"):
        extra["style"] = b["s"]
    if icons and b.get("i"):
        extra["icon_custom_emoji_id"] = b["i"]
    web = None
    # 복권·혜택 화면은 아래쪽 메뉴판 web_app 으로 열면 안 됨: 텔레그램이 initData(누가 열었는지)를 비워서 넘김
    # (공식 문서 WebAppInitData: "empty if the Mini App was launched from a keyboard button") → 글 버튼으로 두고
    # 누르면 글 아래 버튼(InlineKeyboardButton.web_app)·≡ 메뉴 버튼으로 엶 (이 둘은 initData 가 옴). 리뷰 2026-10-12
    if b["k"] == "link" and brand.opens_inside(b.get("v", "")):
        web = WebAppInfo(b["v"])
    return KeyboardButton(b["t"], web_app=web, api_kwargs=extra or None)


def keyboard(svc, room: brand.Room, switch: bool, icons: bool = True) -> ReplyKeyboardMarkup:
    rows, half = [], []
    for b in room.buttons:
        btn = _kbtn(svc, room, b, icons)
        if b.get("w") == 2:
            if half:
                rows.append(half)
                half = []
            rows.append([btn])
        else:
            half.append(btn)
            if len(half) == 2:
                rows.append(half)
                half = []
    if half:
        rows.append(half)
    if switch:
        rows.append([KeyboardButton(SWITCH)])
    return ReplyKeyboardMarkup(rows or [[KeyboardButton(SWITCH)]], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder=(room.name or "메뉴")[:64])


async def bg_bytes(svc, chat_id: int, slot: str = "bg") -> bytes | None:
    r = await svc.db._one("SELECT file FROM brand_images WHERE chat_id=? AND slot=?", (chat_id, slot))
    data = brandcard.load_file(svc.db, r["file"]) if r else None
    if data is None and slot == "win":
        return await bg_bytes(svc, chat_id, "bg")
    return data


async def card(svc, room: brand.Room, kind: str, **kw) -> bytes:
    bg = await bg_bytes(svc, room.chat_id, "win" if kind == "win" else "bg")
    return await asyncio.to_thread(brandcard.render, kind, room, bg, **kw)


async def _rooms_ok(svc, user_id: int) -> list[tuple[int, str]]:
    return [(c, n) for c, n in await brand.rooms_for(svc.db, user_id) if await svc.paid_features(c)]


async def _member_live(svc, bot, chat_id: int, user_id: int) -> bool:
    """DB 에 멤버 기록이 없을 때 텔레그램에 한 번 더 (말 안 한 새 멤버). 맞으면 기록해 둠."""
    if await brand.is_member(svc.db, chat_id, user_id):
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    ok = m.status in ("member", "administrator", "creator") or (m.status == "restricted" and getattr(m, "is_member", False))
    if ok:
        await svc.db.touch_member(chat_id, user_id)
        try:
            await svc.db._write("DELETE FROM member_left WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        except Exception:
            pass
    return ok


async def _still_in(svc, bot, chat_id: int, user_id: int) -> bool:
    """출석·긁기 직전 텔레그램에 한 번 더 (소담이 못 본 사이 밴·나감). 물어보기 실패면 DB 판단 그대로."""
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return True
    if m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True)):
        try:
            await svc.db._write("INSERT OR REPLACE INTO member_left(chat_id, user_id, ts) VALUES(?,?,?)",
                                (chat_id, user_id, int(time.time())))
        except Exception:
            pass
        return False
    return True


async def _set_menu_button(svc, bot, user_id: int, room: brand.Room | None) -> None:
    try:
        url = app_url(svc, room) if room else None
        mb = MenuButtonWebApp(f"🎁 {room.name}"[:20] if room and room.name else "🎁 혜택", WebAppInfo(url)) if url \
            else MenuButtonDefault()
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=mb)
    except Exception as e:   # 메뉴 버튼은 덤 — 실패해도 메뉴판은 그대로
        log.info("menu button set failed %s: %s", user_id, e)


# ── 손님: 열기 ─────────────────────────────────────────────
async def open_room(svc, bot, user_id: int, room: brand.Room, intro: bool = True) -> None:
    """방을 고르고 메뉴판 + 첫 카드. 버튼 이모지를 텔레그램이 거절하면 빼고 다시."""
    await brand.select(svc.db, user_id, room.chat_id)
    switch = len(await _rooms_ok(svc, user_id)) > 1
    caption = f"🎁 <b>{esc(room.name)}</b>\n{esc(room.text('welcome_sub'))}"
    photo = await card(svc, room, "welcome") if intro else None
    for icons in ((False,) if _icons_off[0] else (True, False)):
        kb = keyboard(svc, room, switch, icons)
        try:
            if photo:
                await bot.send_photo(user_id, photo, caption=caption, parse_mode="HTML", reply_markup=kb)
            else:
                await bot.send_message(user_id, caption, parse_mode="HTML", reply_markup=kb)
            break
        except BadRequest as e:
            if icons and any(b.get("i") for b in room.buttons):
                log.info("button emoji refused (%s) — sending without", e)
                _icons_off[0] = True
                continue
            raise
    await _set_menu_button(svc, bot, user_id, room)


async def picker(svc, bot, user_id: int) -> None:
    rooms = await _rooms_ok(svc, user_id)
    if not rooms:
        await bot.send_message(user_id, "🎁 혜택 메뉴를 켠 방에 들어가 있지 않아요. 방 공지의 [🎁 혜택 메뉴] 버튼으로 들어와 주세요.")
        return
    rows = [[B(f"🏠 {n[:30]}", f"bd:p:{c}")] for c, n in rooms]
    await bot.send_message(user_id, "어느 방 메뉴를 열까요?", reply_markup=InlineKeyboardMarkup(rows))


async def on_deep(svc, bot, msg: Message, code: str) -> None:
    """/start b_<코드> — 방 전용 링크."""
    uid = msg.from_user.id
    room = await brand.by_code(svc.db, code)
    if room is None or not room.enabled or not await svc.paid_features(room.chat_id):
        await msg.reply_text("이 혜택 메뉴는 지금 닫혀 있어요. 방 관리자에게 물어봐 주세요.")
        return
    if not await _member_live(svc, bot, room.chat_id, uid):
        await msg.reply_text("이 메뉴는 그 방 멤버만 열 수 있어요. 방에 들어간 뒤 다시 눌러 주세요.")
        return
    await open_room(svc, bot, uid, room)


# ── 손님: 아래쪽 버튼 ─────────────────────────────────────
async def on_text(svc, bot, msg: Message) -> bool:
    text = " ".join((msg.text or "").split())
    if not text or text.startswith(("/", ".")):
        return False
    uid = msg.from_user.id
    if text == SWITCH:
        await picker(svc, bot, uid)
        return True
    prev = await brand.selected(svc.db, uid)
    room = await brand.current(svc, uid)
    b = brand.button_for(room, text) if room else None
    if b is None:   # 고른 방이 없거나 바뀌었는데 옛 메뉴판을 누른 경우 → 그 글자를 가진 방이 하나면 그 방으로
        hits = []
        for cid, _ in await _rooms_ok(svc, uid):
            r = await brand.get(svc.db, cid)
            if r and (bb := brand.button_for(r, text)):
                hits.append((r, bb))
        if len(hits) != 1:
            if prev is not None and (room is None or room.chat_id != prev):   # 고른 방이 닫혔는데 그 방 메뉴판을 누름
                old = await brand.get(svc.db, prev)
                if old and brand.button_for(old, text):
                    await msg.reply_text("이 방 혜택 메뉴는 지금 닫혀 있어요.", reply_markup=ReplyKeyboardRemove())
                    await _set_menu_button(svc, bot, uid, None)
                    return True
            return False
        room, b = hits[0]
        await brand.select(svc.db, uid, room.chat_id)
    if b is None:
        return False
    if not svc.menu_limiter.allow(("bdpress", uid), PRESS_PER_MIN):   # 연타 = 카드 그리기·보내기 반복 (CPU·텔레그램 한도)
        await msg.reply_text("천천히 눌러 주세요 🙏")
        return True
    await press(svc, bot, msg.from_user, room, b)
    return True


def _scratch_kb(svc, room: brand.Room, chat_id: int, left: int) -> InlineKeyboardMarkup | None:
    if left <= 0:
        return None
    row = [B("🪙 긁기", f"bd:s:{chat_id}")]
    if url := app_url(svc, room):
        row.append(InlineKeyboardButton("✋ 손가락으로 긁기", web_app=WebAppInfo(url + "&t=lotto")))
    return InlineKeyboardMarkup([row])


async def press(svc, bot, user, room: brand.Room, b: dict) -> None:
    kind, cid, uid = b["k"], room.chat_id, user.id
    if kind in ("checkin", "lotto") and not await _still_in(svc, bot, cid, uid):
        await bot.send_message(uid, "이 방 멤버만 쓸 수 있어요.", reply_markup=ReplyKeyboardRemove())
        return
    if kind == "checkin":
        role = await svc.perms.role(bot, cid, uid)
        text, r, s, left = await lot.checkin_info(svc, bot, cid, user, role)
        photo = await card(svc, room, "checkin", streak=r["streak"], month=r["month"], ticket=bool(r["ticket"]))
        await bot.send_photo(uid, photo, caption=text[:1000], parse_mode="HTML", reply_markup=_scratch_kb(svc, room, cid, left))
    elif kind == "lotto":
        s = await svc.db.get_settings(cid)
        if not s.get("lotto_enabled"):
            await bot.send_message(uid, "이 방은 지금 복권이 꺼져 있어요.")
            return
        left = await lottery.open_tickets(svc.db, cid, uid, life=s.get("lotto_ticket_life", "7d"), tz=svc.cfg.tz)
        if left <= 0:
            await bot.send_message(uid, "🎟 지금 긁을 복권이 없어요. 📅 출석하면 하루 1장!")
            return
        cap = f"🎟 <b>행운의 복권</b>\n남은 복권 {left}장" + (f"\n\n{esc(room.notice)}" if room.notice else "")
        await bot.send_photo(uid, await card(svc, room, "lotto", left=left), caption=cap[:1000], parse_mode="HTML",
                             reply_markup=_scratch_kb(svc, room, cid, left))
    elif kind == "fortune":
        f = fortune.today(svc.cfg.tz, uid)
        photo = await card(svc, room, "fortune", grade=f["grade"], line=f["overall"], number=f["number"], color=f["color"])
        await bot.send_photo(uid, photo, caption=fortune.text(f)[:1000], parse_mode="HTML")
    elif kind in ("picks", "alerts"):
        route = menu.ROUTES.get("sxt" if kind == "picks" else "sx")
        if route is None:
            await bot.send_message(uid, "지금은 스포츠 기능을 쓸 수 없어요.")
            return
        sc = await route.handler(PanelCtx(svc, bot, uid, None, []))
        await menu.send_panel(svc, bot, uid, lambda: bot.send_message(uid, sc.text, parse_mode="HTML", reply_markup=sc.kb))
    elif kind == "app":
        url = app_url(svc, room)
        if url:
            await bot.send_message(uid, "✨ 아래 버튼으로 열어 주세요.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✨ 혜택 화면 열기", web_app=WebAppInfo(url))]]))
        else:
            await bot.send_message(uid, "✨ 혜택 화면은 잠시 준비 중이에요. 아래 메뉴 버튼을 써 주세요.")
    elif kind == "link":
        await bot.send_message(uid, f"🔗 {esc(b['t'])}", parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("열기 ↗", url=b["v"])]]))
    elif kind == "contact":
        await bot.send_message(uid, f"💬 {esc(b['t'])} — @{esc(b['v'])}", parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("1:1 열기 ↗", url=f"https://t.me/{b['v']}")]]))
    elif kind == "text":
        await bot.send_message(uid, esc(b["v"]), parse_mode="HTML", disable_web_page_preview=True)


# ── 손님: 긁기 (채팅) ─────────────────────────────────────
async def scratch_for(svc, bot, room: brand.Room, user) -> dict:
    """긁기 한 번 → {'kind', 'text', 'win', 'left'} (채팅·미니앱 공통). 당첨이면 관리자 알림까지."""
    text, kind, win = await lot.do_scratch(svc, bot, room.chat_id, user)
    s = await svc.db.get_settings(room.chat_id)
    left = await lottery.open_tickets(svc.db, room.chat_id, user.id, life=s.get("lotto_ticket_life", "7d"), tz=svc.cfg.tz)
    if win:
        persist.spawn(lot.announce_win(svc, bot, room.chat_id, user, win))
    return {"kind": kind, "text": text, "win": win, "left": left, "prize": str(s.get("lotto_prize", ""))}


async def celebrate(svc, bot, uid: int, room: brand.Room, prize: str) -> None:
    """당첨: 🎉 화면 효과 글 + (만들어 둔) 당첨 스티커."""
    msg = f"🎉 <b>{esc(room.text('win_title'))}</b> {esc(room.text('win_sub', prize=prize))}"
    if room.contact:
        msg += f"\n💬 @{esc(room.contact)} 로 연락 주세요"
    else:
        msg += "\n방 관리자가 1:1 로 연락드려요"
    try:
        await bot.send_message(uid, msg, parse_mode="HTML", message_effect_id=EFFECT_PARTY)
    except BadRequest:
        await bot.send_message(uid, msg, parse_mode="HTML")
    except TelegramError:
        return
    r = await svc.db._one("SELECT sticker FROM brand_images WHERE chat_id=? AND slot='stk'", (room.chat_id,))
    if r and r["sticker"]:
        try:
            await bot.send_sticker(uid, r["sticker"])
        except TelegramError:
            pass


async def on_button(svc, bot, q, parts) -> None:
    """bd:s:<방> 긁기 · bd:p:<방> 방 고르기 (1:1 만)."""
    uid = q.from_user.id
    if not q.message or q.message.chat.id != uid:
        await q.answer("1:1 에서만 돼요.", show_alert=True)
        return
    try:
        cid = int(parts[1])
    except (IndexError, ValueError):
        await q.answer()
        return
    room = await brand.usable(svc, cid, uid)
    if room is None:
        await q.answer("이 방 메뉴는 지금 쓸 수 없어요.", show_alert=True)
        return
    if parts[0] == "p":
        await q.answer()
        await open_room(svc, bot, uid, room)
        return
    if parts[0] != "s":
        await q.answer()
        return
    if not svc.menu_limiter.allow(("bd", uid), 20):
        await q.answer("너무 빨라요. 잠시 후 다시.")
        return
    if not await _still_in(svc, bot, cid, uid):
        await q.answer("이 방 멤버만 긁을 수 있어요.", show_alert=True)
        return
    user = SimpleNamespace(id=uid, first_name=q.from_user.first_name, last_name=q.from_user.last_name, username=q.from_user.username)
    got = await scratch_for(svc, bot, room, user)
    kind = got["kind"]
    if kind in ("none", "off"):
        await q.answer("긁을 복권이 없어요. 📅 출석하면 1장!" if kind == "none" else "복권이 꺼져 있어요.", show_alert=True)
        return
    await q.answer("🪙 사사삭…")
    photo = await card(svc, room, "win" if kind == "win" else ("soldout" if kind == "soldout" else "lose"),
                       prize=got["prize"])
    cap = "✨ 그림을 눌러 결과를 확인하세요!" + (f"\n남은 복권 {got['left']}장" if got["left"] else "")
    try:
        await q.edit_message_media(InputMediaPhoto(photo, caption=cap, has_spoiler=True),
                                   reply_markup=_scratch_kb(svc, room, cid, got["left"]))
    except TelegramError:
        await bot.send_photo(uid, photo, caption=cap, has_spoiler=True, reply_markup=_scratch_kb(svc, room, cid, got["left"]))
    if kind == "win":
        await celebrate(svc, bot, uid, room, got["prize"])


# ── 미니앱 API ────────────────────────────────────────────
async def _app_room(svc, bot, user: dict, req: dict) -> tuple[brand.Room | None, str]:
    room = await brand.by_code(svc.db, str(req.get("room") or ""))
    if room is None:
        cid = await brand.selected(svc.db, user["id"])
        room = await brand.get(svc.db, cid) if cid else None
    if room is None or not room.enabled or not await svc.paid_features(room.chat_id):
        return None, "이 혜택 메뉴는 지금 닫혀 있어요"
    if not await _member_live(svc, bot, room.chat_id, user["id"]):
        return None, "그 방 멤버만 열 수 있어요"
    return room, ""


async def _state(svc, bot, room: brand.Room, user: dict) -> dict:
    uid, cid = user["id"], room.chat_id
    s = await svc.db.get_settings(cid)
    tz = svc.cfg.tz
    now = time.time()
    today = lottery.kst_day(tz, now)
    month = today[:7]
    days = [r["day"] for r in await svc.db._all("SELECT day FROM attend WHERE chat_id=? AND user_id=? AND day LIKE ?",
                                                (cid, uid, month + "%"))]
    st = await svc.db._one("SELECT streak FROM attend WHERE chat_id=? AND user_id=? ORDER BY day DESC LIMIT 1", (cid, uid))
    f = fortune.today(tz, uid)
    kinds = {b["k"] for b in room.buttons}
    picks_out = []
    if "picks" in kinds or "app" in kinds:
        try:
            from .sportsdm import today_picks
            from ..sports import fmt
            from ..sports.leagues import ko_name
            for g, sp_ in await today_picks(svc, 8):
                n = sum(sp_.values())
                picks_out.append({"tag": fmt.tag(g).strip("[]"), "time": fmt.hhmm(g.start), "home": ko_name(g.home, g.league),
                                  "away": ko_name(g.away, g.league), "n": n,
                                  "home_pct": sp_.get("home", 0) * 100 // n if n else 0,
                                  "away_pct": sp_.get("away", 0) * 100 // n if n else 0})
        except Exception:
            log.exception("miniapp picks failed")
    c = room.colors
    bg = await svc.db._one("SELECT file FROM brand_images WHERE chat_id=? AND slot='bg'", (cid,))
    return {
        "name": room.name, "notice": room.notice, "contact": room.contact,
        "colors": {k: brand.rgb_hex(c[k]) for k in ("bg1", "bg2", "accent", "fg")},
        "texts": {k: room.text(k) for k in brand.TEXTS if not brand.TEXTS[k][2]},
        "tpl": {k: room.texts.get(k) or brand.TEXTS[k][1] for k in brand.TEXTS if brand.TEXTS[k][2]},
        "bg": f"f/bg/{room.code}.jpg?v={bg['file'][-14:-4]}" if bg else None,
        "tabs": [k for k in ("lotto", "checkin", "fortune", "picks") if k in kinds or "app" in kinds],
        "lotto": {"on": bool(s.get("lotto_enabled")), "prize": str(s.get("lotto_prize", "")),
                  "left": await lottery.open_tickets(svc.db, cid, uid, life=s.get("lotto_ticket_life", "7d"), tz=tz)
                  if s.get("lotto_enabled") else 0,
                  "life": s.get("lotto_ticket_life", "7d")},
        "checkin": {"today": today in days, "todayn": int(today[8:]), "days": sorted(int(d[8:]) for d in days), "month": month,
                    "streak": st["streak"] if st else 0},
        "fortune": {"grade": f["grade"], "overall": f["overall"], "money": f["money"], "love": f["love"], "health": f["health"],
                    "number": f["number"], "color": f["color"], "tip": f["tip"]},
        "picks": picks_out,
    }


async def api_state(svc, bot, user: dict, req: dict):
    room, why = await _app_room(svc, bot, user, req)
    if room is None:
        return 403, {"error": why}
    await brand.select(svc.db, user["id"], room.chat_id)
    return 200, await _state(svc, bot, room, user)


async def api_checkin(svc, bot, user: dict, req: dict):
    room, why = await _app_room(svc, bot, user, req)
    if room is None:
        return 403, {"error": why}
    role = await svc.perms.role(bot, room.chat_id, user["id"])
    text, r, s, left = await lot.checkin_info(svc, bot, room.chat_id, _user_ns(user), role)
    from ..util import html_plain
    return 200, {"ok": r["ok"], "ticket": bool(r["ticket"]), "message": html_plain(text)[:300],
                 "state": await _state(svc, bot, room, user)}


async def api_scratch(svc, bot, user: dict, req: dict):
    room, why = await _app_room(svc, bot, user, req)
    if room is None:
        return 403, {"error": why}
    got = await scratch_for(svc, bot, room, _user_ns(user))
    kind = got["kind"]
    if kind in ("none", "off"):
        return 200, {"result": kind, "left": got["left"]}
    if kind == "win":
        persist.spawn(celebrate(svc, bot, user["id"], room, got["prize"]))
    title = {"win": room.text("win_title"), "soldout": room.text("soldout_title")}.get(kind, room.text("lose_title"))
    sub = room.text("win_sub", prize=got["prize"]) if kind == "win" else (
        "" if kind == "soldout" else room.text("lose_sub"))
    return 200, {"result": kind, "title": title, "sub": sub, "left": got["left"],
                 "contact": room.contact if kind == "win" else ""}


async def file_bg(svc, bot, name: str):
    code = name[:-4] if name.endswith(".jpg") else ""
    room = await brand.by_code(svc.db, code)
    if room is None or not room.enabled:
        return None
    data = await bg_bytes(svc, room.chat_id, "bg")
    return (data, "image/jpeg") if data else None


miniapp.register_api("state", api_state)
miniapp.register_api("checkin", api_checkin)
miniapp.register_api("scratch", api_scratch)
miniapp.register_files("bg", file_bg)


# ── 방장 화면 ─────────────────────────────────────────────
async def s_bd(c: PanelCtx) -> Screen:
    from ..subscription import chat_title
    room = await brand.ensure(c.svc.db, c.cid, await chat_title(c.svc, c.cid))
    url = app_url(c.svc, room)
    link = brand.link(c.bot.username, room)
    btns = "\n".join(f"  {i}. {esc(b['t'])}" + (f" ({brand.STYLES[b['s']]})" if b.get("s") else "") for i, b in enumerate(room.buttons, 1))
    lines = ["🎁 <b>방 전용 1:1 메뉴</b>",
             "멤버가 소담 1:1 에서 이 방만의 메뉴판(출석·복권·운세·추천픽·바로가기·문의)과 그림 카드를 봐요. "
             "다른 방과 기록이 안 섞여요. 버튼·글자·색·그림은 전부 여기서 정해요.",
             "", f"지금: <b>{'✅ 켜짐' if room.enabled else '❌ 꺼짐'}</b> · 이름 <b>{esc(room.name)}</b> · 색 {brand.THEMES[room.theme]['label']}",
             f"버튼 {len(room.buttons)}개:\n{btns or '  (없음)'}",
             f"복권 안내: {esc(room.notice) or '(없음)'} · 당첨 연락처: {('@' + esc(room.contact)) if room.contact else '(없음 — 관리자 1:1 알림만)'}",
             f"미니앱(손가락 긁기): {'✅ 준비됨' if url else '⏳ 서버 주소 준비 전 — 채팅 버튼으로 대신해요'}",
             f"멤버 입구 링크: <code>{esc(link)}</code>"]
    rows = [[B("❌ 끄기" if room.enabled else "✅ 켜기", f"m:bdt:{c.cid}:{0 if room.enabled else 1}")],
            [B("🔘 버튼", f"m:bdb:{c.cid}"), B("📝 카드 글자", f"m:bdx:{c.cid}")],
            [B("🎨 색", f"m:bdc:{c.cid}"), B("🖼 그림·스티커", f"m:bdi:{c.cid}")],
            [B("✏️ 이름", f"m:in:{c.cid}:bdname"), B("📢 복권 안내", f"m:in:{c.cid}:bdnote")],
            [B("💬 당첨 연락처", f"m:in:{c.cid}:bdcont"), B("🎟 복권 설정", f"m:lot:{c.cid}")],
            [B("👀 미리보기 (내 1:1)", f"m:bdv:{c.cid}"), B("📣 방에 버튼 올리기", f"m:bdpost:{c.cid}")],
            [B("🔄 입구 링크 바꾸기", f"m:bdrl:{c.cid}")], menu._back(c.cid)]
    return Screen("\n".join(lines)[:3900], menu._kb(rows))


async def r_toggle(c: PanelCtx) -> Screen:
    on = c.arg(0) == "1"
    room = await brand.ensure(c.svc.db, c.cid)
    if on and not room.buttons:
        await brand.update(c.svc.db, c.cid, c.uid, buttons=brand.DEFAULT_BUTTONS)
    await brand.update(c.svc.db, c.cid, c.uid, enabled=on)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"brand_enabled={int(on)}")
    sc = await s_bd(c)
    sc.toast = "✅ 켰어요 — 📣 방에 버튼을 올려 멤버에게 알려 주세요" if on else "껐어요"
    return sc


async def r_relink(c: PanelCtx) -> Screen:
    await brand.ensure(c.svc.db, c.cid)
    await brand.new_code(c.svc.db, c.cid, c.uid)
    sc = await s_bd(c)
    sc.toast = "🔄 새 링크로 바꿨어요. 옛 링크·옛 방 버튼은 이제 안 열려요."
    sc.alert = True
    return sc


async def r_preview(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    try:
        await open_room(c.svc, c.bot, c.uid, room)
    except TelegramError as e:
        return Screen(None, toast=f"보내기 실패: {str(e)[:120]}", alert=True)
    return Screen(None, toast="👀 아래에 멤버가 보는 화면을 띄웠어요 (꺼져 있어도 미리보기는 돼요)")


async def r_post(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    if not room.enabled:
        return Screen(None, toast="먼저 ✅ 켜기 를 눌러 주세요.", alert=True)
    if not await persist.claim(c.svc.db, f"brandpost:{c.cid}", POST_GAP):
        return Screen(None, toast="방금 올렸어요. 1분 뒤에 다시.", alert=True)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎁 {room.name[:24]} 혜택 메뉴", url=brand.link(c.bot.username, room))]])
    try:
        await c.bot.send_message(c.cid, f"🎁 <b>{esc(room.name)}</b> 혜택 메뉴\n출석·복권·운세·추천픽을 소담 1:1 에서 받아 보세요 👇",
                                 parse_mode="HTML", reply_markup=kb)
    except TelegramError as e:
        return Screen(None, toast=f"방에 못 올렸어요: {str(e)[:100]}", alert=True)
    return Screen(None, toast="📣 방에 올렸어요. 고정해 두면 새 멤버도 찾기 쉬워요.", alert=True)


# 버튼
async def s_buttons(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    lines = ["🔘 <b>아래쪽 메뉴 버튼</b> (최대 8개, 위에서부터 2개씩 한 줄 · '넓게'는 한 줄 혼자)",
             "버튼마다 이름·색(빨강·초록·파랑)·넓이·앞에 붙는 프리미엄 이모지를 정할 수 있어요.", ""]
    rows = []
    for i, b in enumerate(room.buttons):
        part = brand.PARTS[b["k"]]
        extra = " · ".join(x for x in (brand.STYLES.get(b.get("s", ""), "") if b.get("s") else "",
                                       "넓게" if b.get("w") == 2 else "", "이모지" if b.get("i") else "") if x)
        lines.append(f"{i + 1}. {esc(b['t'])} — {esc(part.desc)}" + (f" [{extra}]" if extra else ""))
        rows.append([B(f"✏️ {i + 1}", f"m:bde:{c.cid}:{i}"), B("⬆️", f"m:bdu:{c.cid}:{i}"), B("🗑", f"m:bdd:{c.cid}:{i}")])
    if len(room.buttons) < brand.MAX_BUTTONS:
        rows.append([B("➕ 버튼 더하기", f"m:bda:{c.cid}")])
    rows.append([B("⬅️ 메뉴로", f"m:bd:{c.cid}")])
    return Screen("\n".join(lines)[:3900], menu._kb(rows))


async def s_add(c: PanelCtx) -> Screen:
    lines = ["➕ <b>어떤 버튼을 더할까요?</b>", ""] + [f"• {esc(p.label)} — {esc(p.desc)}" for p in brand.PARTS.values()]
    rows = menu._chunks([B(p.label, f"m:bdap:{c.cid}:{p.kind}") for p in brand.PARTS.values()], 2)
    return Screen("\n".join(lines), menu._kb(rows + [[B("⬅️ 버튼", f"m:bdb:{c.cid}")]]))


async def r_add(c: PanelCtx) -> Screen:
    kind = c.arg(0)
    if kind not in brand.PARTS:
        return Screen(None)
    room = await brand.ensure(c.svc.db, c.cid)
    if len(room.buttons) >= brand.MAX_BUTTONS:
        return Screen(None, toast=f"버튼은 {brand.MAX_BUTTONS}개까지예요.", alert=True)
    part = brand.PARTS[kind]
    label, n = part.label, 2
    while any(b["t"] == label for b in room.buttons):
        label, n = f"{part.label} {n}", n + 1
    if part.value:   # 값이 있어야 하는 버튼은 값부터 받음 (값 없이 저장하면 clean 이 버림)
        return await _ask_value(c, kind, label)
    await brand.update(c.svc.db, c.cid, c.uid, buttons=room.buttons + [{"k": kind, "t": label}])
    sc = await s_buttons(c)
    sc.toast = f"➕ {label} 더했어요"
    return sc


async def _ask_value(c: PanelCtx, kind: str, label: str) -> Screen:
    prompt = {"url": "🔗 열 주소를 보내 주세요 (https:// 로 시작). 예: <code>https://example.com</code>",
              "contact": "💬 문의 받을 @아이디를 보내 주세요. 예: <code>@my_help</code>",
              "text": f"📢 버튼을 누르면 보여 줄 글을 보내 주세요 ({brand.TEXT_MAX}자 안)."}[brand.PARTS[kind].value]
    from ..services import PendingInput
    c.svc.inputs[c.uid] = PendingInput("bdval", c.cid, args=[str(-1), kind, label])
    if hasattr(c.svc.inputs, "save"):   # 재시작에도 남게 (sodam/persist.py InputStore)
        c.svc.inputs.save(c.uid)
    return Screen(prompt + "\n\n5분 안에 보내주세요. 그만두려면 <code>취소</code>", menu._kb([[B("❌ 취소", f"m:bdb:{c.cid}")]]))


async def s_edit(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    i = _idx(c, room)
    if i is None:
        return await s_buttons(c)
    b = room.buttons[i]
    part = brand.PARTS[b["k"]]
    lines = [f"✏️ <b>{i + 1}번 버튼</b> — {esc(b['t'])}", esc(part.desc)]
    if b.get("v"):
        lines.append(f"값: <code>{esc(b['v'][:200])}</code>")
    lines.append(f"이모지: {'있음' if b.get('i') else '없음'} — 프리미엄 이모지를 보내면 버튼 글자 앞에 붙어요 "
                 "(봇 주인이 텔레그램 프리미엄일 때만 보여요)")
    st = [B(("● " if b.get("s", "") == k else "") + v, f"m:bdst:{c.cid}:{i}:{k or 'x'}") for k, v in brand.STYLES.items()]
    rows = [st, [B("✏️ 이름", f"m:in:{c.cid}:bdlabel:{i}"), B("↔️ 넓게" if b.get("w") != 2 else "↔️ 반 칸", f"m:bdw:{c.cid}:{i}")],
            [B("😀 이모지", f"m:in:{c.cid}:bdicon:{i}")] + ([B("🚫 이모지 빼기", f"m:bdni:{c.cid}:{i}")] if b.get("i") else [])]
    if part.value:
        rows.append([B("✏️ 값 바꾸기", f"m:in:{c.cid}:bdvalue:{i}")])
    rows.append([B("⬅️ 버튼", f"m:bdb:{c.cid}")])
    return Screen("\n".join(lines), menu._kb(rows))


def _idx(c: PanelCtx, room: brand.Room, pos: int = 0) -> int | None:
    from ..util import to_int
    i = to_int(c.arg(pos))
    return i if i is not None and 0 <= i < len(room.buttons) else None


async def _change(c: PanelCtx, fn, toast: str) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    i = _idx(c, room)
    if i is None:
        return await s_buttons(c)
    buttons = [dict(b) for b in room.buttons]
    fn(buttons, i)
    await brand.update(c.svc.db, c.cid, c.uid, buttons=buttons)
    sc = await (s_buttons(c) if c.args and c.args[-1] == "list" else s_edit(c))
    sc.toast = toast
    return sc


async def r_style(c: PanelCtx) -> Screen:
    st = "" if c.arg(1) == "x" else c.arg(1)
    if st not in brand.STYLES:
        return Screen(None)

    def f(bs, i):
        bs[i].pop("s", None)
        if st:
            bs[i]["s"] = st
    return await _change(c, f, f"색: {brand.STYLES[st]}")


async def r_wide(c: PanelCtx) -> Screen:
    def f(bs, i):
        if bs[i].get("w") == 2:
            bs[i].pop("w")
        else:
            bs[i]["w"] = 2
    return await _change(c, f, "넓이를 바꿨어요")


async def r_noicon(c: PanelCtx) -> Screen:
    return await _change(c, lambda bs, i: bs[i].pop("i", None), "이모지를 뺐어요")


async def r_up(c: PanelCtx) -> Screen:
    c.args = c.args + ["list"]

    def f(bs, i):
        if i > 0:
            bs[i - 1], bs[i] = bs[i], bs[i - 1]
    return await _change(c, f, "⬆️ 올렸어요")


async def r_del(c: PanelCtx) -> Screen:
    c.args = c.args + ["list"]
    return await _change(c, lambda bs, i: bs.pop(i), "🗑 지웠어요")


# 글자·색
async def s_texts(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    lines = ["📝 <b>카드·화면 글자</b> — 전부 바꿀 수 있어요. {칸} 은 그때 값으로 채워져요.", ""]
    rows = []
    for k, (label, default, holes) in brand.TEXTS.items():
        cur = room.texts.get(k)
        lines.append(f"• {label}: <b>{esc(cur or default)}</b>" + ("" if cur else " <i>(기본)</i>")
                     + (f"  칸: {' '.join('{' + h + '}' for h in holes)}" if holes else ""))
        rows.append(B(f"✏️ {label}", f"m:in:{c.cid}:bdtext:{k}"))
    rows = menu._chunks(rows, 2) + [[B("↩️ 전부 기본으로", f"m:bdxr:{c.cid}")], [B("⬅️ 메뉴로", f"m:bd:{c.cid}")]]
    return Screen("\n".join(lines)[:3900], menu._kb(rows))


async def r_texts_reset(c: PanelCtx) -> Screen:
    await brand.update(c.svc.db, c.cid, c.uid, texts={})
    sc = await s_texts(c)
    sc.toast = "기본 글자로 돌렸어요"
    return sc


async def s_colors(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    col = room.colors
    lines = ["🎨 <b>색</b> — 묶음을 고르고, 색 하나하나를 #RRGGBB 로 바꿀 수도 있어요 (카드·미니앱 같이 바뀜).", "",
             f"지금: {brand.THEMES[room.theme]['label']}" + (" + 직접 고른 색" if room.custom else "")]
    lines += [f"• {v}: <code>{brand.rgb_hex(col[k])}</code>" for k, v in brand.COLOR_KEYS.items()]
    rows = menu._chunks([B(("● " if room.theme == k else "") + v["label"], f"m:bdth:{c.cid}:{k}") for k, v in brand.THEMES.items()], 2)
    rows += menu._chunks([B(f"✏️ {v}", f"m:in:{c.cid}:bdcolor:{k}") for k, v in brand.COLOR_KEYS.items()], 3)
    if room.custom:
        rows.append([B("↩️ 직접 고른 색 지우기", f"m:bdcr:{c.cid}")])
    rows.append([B("⬅️ 메뉴로", f"m:bd:{c.cid}")])
    return Screen("\n".join(lines), menu._kb(rows))


async def r_theme(c: PanelCtx) -> Screen:
    if c.arg(0) not in brand.THEMES:
        return Screen(None)
    await brand.update(c.svc.db, c.cid, c.uid, theme=c.arg(0))
    return await s_colors(c)


async def r_colors_reset(c: PanelCtx) -> Screen:
    await brand.update(c.svc.db, c.cid, c.uid, colors={})
    return await s_colors(c)


# 그림·스티커
async def s_images(c: PanelCtx) -> Screen:
    rows_db = {r["slot"]: r for r in await c.svc.db._all("SELECT slot, src, sticker FROM brand_images WHERE chat_id=?", (c.cid,))}
    stk = rows_db.pop("stk", None)
    day = _kst_day(c.svc)
    used = await c.svc.db.counter(day, c.cid, "image")
    limit = int((await c.svc.db.get_settings(c.cid)).get("image_daily", 5))
    lines = ["🖼 <b>그림</b> — 카드 배경·당첨 그림을 사진으로 올리거나 AI 로 만들어요. 글자는 소담이 위에 얹어요 "
             "(AI 에겐 글자를 안 쓰게 함 — 한글이 깨짐).", ""]
    for slot, label in brand.SLOTS.items():
        r = rows_db.get(slot)
        lines.append(f"• {label}: " + ({"upload": "올린 사진", "ai": "AI 그림"}.get(r["src"], "있음") if r else
                                       ("없음 (카드 배경 사용)" if slot == "win" else "없음 (색 그라데이션)")))
    lines.append(f"• 당첨 스티커: {'있음 🎉 (다시 만들면 바뀜)' if stk and stk['sticker'] else '없음'}")
    lines.append(f"\nAI 그림 오늘 {used}/{limit}장 (방 그림 한도와 같이 셈)")
    rows = []
    for slot, label in brand.SLOTS.items():
        rows.append([B(f"📤 {label} 올리기", f"m:in:{c.cid}:bdimg:{slot}"), B("🤖 AI", f"m:in:{c.cid}:bdai:{slot}")]
                    + ([B("🗑", f"m:bdix:{c.cid}:{slot}")] if slot in rows_db else []))
    rows += menu._chunks([B(f"🎉 스티커: {v[0]}", f"m:bdstk:{c.cid}:{k}") for k, v in STICKER_LOOKS.items()], 2)
    rows.append([B("🃏 카드 견본 받기 (내 1:1)", f"m:bdsamp:{c.cid}")])
    rows.append([B("⬅️ 메뉴로", f"m:bd:{c.cid}")])
    return Screen("\n".join(lines), menu._kb(rows))


async def _store_image(svc, chat_id: int, slot: str, raw: bytes, src: str) -> None:
    jpeg = await asyncio.to_thread(brandcard.normalize, raw)
    old = await svc.db._one("SELECT file FROM brand_images WHERE chat_id=? AND slot=?", (chat_id, slot))
    name = await asyncio.to_thread(brandcard.save_file, svc.db, chat_id, slot, jpeg)
    await svc.db._write("INSERT INTO brand_images(chat_id, slot, file, src, updated) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(chat_id, slot) DO UPDATE SET file=excluded.file, src=excluded.src, "
                        "updated=excluded.updated", (chat_id, slot, name, src, int(time.time())))
    if old and old["file"] != name:
        brandcard.remove_file(svc.db, old["file"])


async def r_image_del(c: PanelCtx) -> Screen:
    slot = c.arg(0)
    if slot not in brand.SLOTS:
        return await s_images(c)
    old = await c.svc.db._one("SELECT file FROM brand_images WHERE chat_id=? AND slot=?", (c.cid, slot))
    if old:
        await c.svc.db._write("DELETE FROM brand_images WHERE chat_id=? AND slot=?", (c.cid, slot))
        brandcard.remove_file(c.svc.db, old["file"])
    return await s_images(c)


async def r_samples(c: PanelCtx) -> Screen:
    room = await brand.ensure(c.svc.db, c.cid)
    s = await c.svc.db.get_settings(c.cid)
    try:
        for kind, kw in (("lotto", {"left": 1}), ("win", {"prize": s.get("lotto_prize", "")}), ("lose", {}),
                         ("checkin", {"streak": 3, "month": 5, "ticket": True})):
            await c.bot.send_photo(c.uid, await card(c.svc, room, kind, **kw), caption=f"🃏 견본: {kind}")
    except TelegramError as e:
        return Screen(None, toast=f"보내기 실패: {str(e)[:100]}", alert=True)
    return Screen(None, toast="🃏 견본 카드를 보냈어요")


_stk_running: set = set()
# 당첨 스티커 연출 = 스티커 공방의 검증된 조합(recipes) 중 고름 — 글자는 방장의 '당첨 제목'
STICKER_LOOKS = {"money": ("💸 돈비", "돈비_대박"), "pop": ("✨ 반짝 등장", "짠_등장"), "stamp": ("💥 쾅 도장", "쾅_도장"),
                 "heart": ("💖 하트", "사랑_하트펀치")}


async def r_sticker(c: PanelCtx) -> Screen:
    look = STICKER_LOOKS.get(c.arg(0)) or STICKER_LOOKS["money"]
    if c.cid in _stk_running:
        return Screen(None, toast="만드는 중이에요. 잠시만요.", alert=True)
    if not await c.svc.paid_features(c.cid):
        return Screen(None, toast="이용 기간 중인 방만 만들 수 있어요.", alert=True)
    day = _kst_day(c.svc)
    if await c.svc.db.bump(day, c.cid, "brand_stk") > STICKER_DAILY:
        await c.svc.db.bump(day, c.cid, "brand_stk", -1)
        return Screen(None, toast=f"당첨 스티커는 방마다 하루 {STICKER_DAILY}번까지예요.", alert=True)
    _stk_running.add(c.cid)        # 누르는 쪽에서 바로 (빨리 두 번 눌러도 하나만)
    room = await brand.ensure(c.svc.db, c.cid)
    persist.spawn(_make_sticker(c.svc, c.bot, c.uid, room, look[1]))
    return Screen(None, toast="🎉 당첨 스티커 만드는 중… 1분쯤 걸려요. 다 되면 1:1 로 보내 드려요.", alert=True)


async def _make_sticker(svc, bot, uid: int, room: brand.Room, recipe: str = "돈비_대박") -> None:
    from .. import stickerforge as SF
    _stk_running.add(room.chat_id)
    try:
        src = await asyncio.to_thread(brandcard.backdrop, room, await bg_bytes(svc, room.chat_id, "win"))
        spec, err = SF.sanitize({"recipe": recipe, "mode": "photo", "seed": int(time.time()) % 1000,
                                 "caption": {"text": brandcard.plain(room.text("win_title"))[:8] or "당첨!", "size_max": 120}})
        if spec is None:
            raise ValueError(err)
        res = await SF.forge(src, spec)
        if not res.ok or not res.webm:
            spec2, _ = SF.sanitize({"recipe": recipe, "mode": "photo", "caption": {"text": "당첨!"}})
            res = await SF.forge(src, spec2) if spec2 else res
        if not res.ok or not res.webm:
            raise ValueError(res.error or res.summary())
        sent = await bot.send_sticker(uid, InputFile(res.webm, filename="win.webm"))
        fid = sent.sticker.file_id if sent and sent.sticker else None
        if fid:   # 스티커는 따로 한 줄 (slot 'stk', 파일 없음) — 그림을 바꿔도 지워지지 않고, 다시 만들면 덮어씀
            await svc.db._write("INSERT OR REPLACE INTO brand_images(chat_id, slot, file, src, sticker, updated) VALUES(?,?,?,?,?,?)",
                                (room.chat_id, "stk", "", "sticker", fid, int(time.time())))
        await bot.send_message(uid, "🎉 당첨 스티커를 만들었어요. 이제 당첨된 멤버에게 1:1 로 같이 보내요.")
    except Exception as e:
        log.warning("brand win sticker failed: %s", e)
        try:
            await bot.send_message(uid, "😢 당첨 스티커를 못 만들었어요. 그림을 바꿔서 다시 해 보세요.")
        except TelegramError:
            pass
    finally:
        _stk_running.discard(room.chat_id)


# ── 글자 입력 ─────────────────────────────────────────────
async def _room_and_buttons(c: PanelCtx) -> tuple[brand.Room, list]:
    room = await brand.ensure(c.svc.db, c.cid)
    return room, [dict(b) for b in room.buttons]


async def in_name(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    try:
        v = brand.clean_name(msg.text or "")
    except ValueError as e:
        return False, f"❌ {e}"
    await brand.update(c.svc.db, c.cid, c.uid, name=v)
    return True, f"✅ 이름을 <b>{esc(v)}</b>(으)로 했어요."


async def in_notice(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if raw in ("없음", "지우기"):
        await brand.update(c.svc.db, c.cid, c.uid, notice="")
        return True, "✅ 복권 안내를 지웠어요."
    try:
        v = brand.clean_text(raw, brand.NOTICE_MAX)
    except ValueError as e:
        return False, f"❌ {e}"
    await brand.update(c.svc.db, c.cid, c.uid, notice=v)
    return True, "✅ 복권 안내를 바꿨어요."


async def in_contact(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if raw in ("없음", "지우기"):
        await brand.update(c.svc.db, c.cid, c.uid, contact="")
        return True, "✅ 당첨 연락처를 지웠어요 (관리자 1:1 알림만)."
    try:
        v = brand.clean_contact(raw)
    except ValueError as e:
        return False, f"❌ {e}"
    await brand.update(c.svc.db, c.cid, c.uid, contact=v)
    return True, f"✅ 당첨되면 @{esc(v)} 로 연락하라고 보여 줘요."


async def in_label(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    room, bs = await _room_and_buttons(c)
    i = _idx(c, room)
    if i is None:
        return False, "❌ 버튼이 바뀌었어요. 다시 골라 주세요."
    try:
        v = brand.clean_label(msg.text or "")
    except ValueError as e:
        return False, f"❌ {e}"
    if any(b["t"] == v for j, b in enumerate(bs) if j != i) or v == SWITCH:
        return False, "❌ 같은 이름 버튼이 이미 있어요."
    bs[i]["t"] = v
    await brand.update(c.svc.db, c.cid, c.uid, buttons=bs)
    return True, f"✅ 버튼 이름을 <b>{esc(v)}</b>(으)로."


async def in_value(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    room, bs = await _room_and_buttons(c)
    i = _idx(c, room)
    if i is None:
        return False, "❌ 버튼이 바뀌었어요. 다시 골라 주세요."
    kind = brand.PARTS[bs[i]["k"]].value
    try:
        v = {"url": brand.clean_url, "contact": brand.clean_contact, "text": brand.clean_text}[kind](msg.text or "")
    except ValueError as e:
        return False, f"❌ {e}"
    bs[i]["v"] = v
    await brand.update(c.svc.db, c.cid, c.uid, buttons=bs)
    return True, "✅ 값을 바꿨어요."


async def in_newvalue(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    """새 버튼 (값이 필요한 부품) — args = [-1, 부품, 이름]."""
    kind, label = c.arg(1), c.arg(2)
    if kind not in brand.PARTS:
        return False, "❌ 다시 골라 주세요."
    room, bs = await _room_and_buttons(c)
    if len(bs) >= brand.MAX_BUTTONS:
        return True, f"버튼은 {brand.MAX_BUTTONS}개까지예요."
    b = {"k": kind, "t": label, "v": (msg.text or "").strip()}
    if brand.clean_button(b) is None:
        try:
            {"url": brand.clean_url, "contact": brand.clean_contact, "text": brand.clean_text}[brand.PARTS[kind].value](b["v"])
        except ValueError as e:
            return False, f"❌ {e}"
        return False, "❌ 형식이 안 맞아요."
    await brand.update(c.svc.db, c.cid, c.uid, buttons=bs + [b])
    return True, f"✅ {esc(label)} 버튼을 더했어요. 이름·색은 ✏️ 에서 바꿔요."


async def in_icon(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    room, bs = await _room_and_buttons(c)
    i = _idx(c, room)
    if i is None:
        return False, "❌ 버튼이 바뀌었어요."
    ids = [e.custom_emoji_id for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if not ids and msg.sticker and getattr(msg.sticker, "custom_emoji_id", None):
        ids = [msg.sticker.custom_emoji_id]
    if not ids:
        return False, "❌ 프리미엄(움직이는) 이모지를 하나 보내 주세요. 보통 이모지는 버튼 글자에 그냥 쓰면 돼요."
    bs[i]["i"] = ids[0]
    await brand.update(c.svc.db, c.cid, c.uid, buttons=bs)
    return True, "✅ 버튼 앞에 이모지를 붙였어요. (봇 주인이 프리미엄이 아니면 텔레그램이 빼고 보여 줘요)"


async def in_text(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    key = c.arg(0)
    if key not in brand.TEXTS:
        return False, "❌ 다시 골라 주세요."
    room = await brand.ensure(c.svc.db, c.cid)
    raw = (msg.text or "").strip()
    texts = dict(room.texts)
    if raw in ("기본", "지우기"):
        texts.pop(key, None)
    else:
        try:
            texts[key] = brand.clean_card_text(key, raw)
        except ValueError as e:
            return False, f"❌ {e}"
    await brand.update(c.svc.db, c.cid, c.uid, texts=texts)
    return True, "✅ 글자를 바꿨어요. 🃏 카드 견본으로 확인해 보세요."


async def in_color(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    key = c.arg(0)
    if key not in brand.COLOR_KEYS:
        return False, "❌ 다시 골라 주세요."
    rgb = brand.hex_rgb(msg.text or "")
    if not rgb:
        return False, "❌ #RRGGBB 로 보내 주세요. 예: <code>#f2c14e</code> (금색)"
    room = await brand.ensure(c.svc.db, c.cid)
    custom = {k: brand.rgb_hex(v) for k, v in room.custom.items()}
    custom[key] = brand.rgb_hex(rgb)
    await brand.update(c.svc.db, c.cid, c.uid, colors=custom)
    return True, f"✅ {brand.COLOR_KEYS[key]}을 {brand.rgb_hex(rgb)} 로."


async def in_image(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    slot = c.arg(0)
    if slot not in brand.SLOTS:
        return False, "❌ 다시 골라 주세요."
    fid = None
    if msg.photo:
        fid = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        fid = msg.document.file_id
    if not fid:
        return False, "❌ 사진을 보내 주세요 (가로로 긴 사진이 좋아요 — 가운데를 16:9 로 잘라 써요)."
    try:
        f = await c.bot.get_file(fid)
        raw = bytes(await f.download_as_bytearray())
        await _store_image(c.svc, c.cid, slot, raw, "upload")
    except ValueError as e:
        return False, f"❌ {e}"
    except TelegramError as e:
        return False, f"❌ 사진을 못 받았어요 ({str(e)[:80]})"
    return True, f"✅ {brand.SLOTS[slot]}을 바꿨어요. 🃏 카드 견본으로 확인해 보세요."


AI_PROMPT = ("Wide 16:9 promotional banner background for a Telegram community event card. Theme: {want}. "
             "Rich, glossy, high quality, centered empty space in the middle for text. "
             "Absolutely no text, no letters, no numbers, no logos, no watermarks, no real people or celebrities.")


async def in_ai(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    slot = c.arg(0)
    want = " ".join((msg.text or "").split())[:300]
    if slot not in brand.SLOTS or not want:
        return False, "❌ 어떤 느낌인지 글로 보내 주세요. 예: <code>금화가 쏟아지는 카지노 밤, 남색과 금색</code>"
    from ..mediapolicy import hard_line
    if why := hard_line(want, False):
        return True, f"🙅 {esc(why[:200])}"
    if not await c.svc.paid_features(c.cid):
        return True, "AI 그림은 이용 기간 중인 방만 돼요. 사진 올리기는 돼요."
    owner = c.uid in await c.svc.perms.owners()
    day = _kst_day(c.svc)
    limit = int((await c.svc.db.get_settings(c.cid)).get("image_daily", 5))
    if not owner:   # 방 그림 한도(image_daily, 오너만 올림)와 같은 카운터 — 먼저 자리를 잡고(+1) 넘으면 되돌림 (동시에 눌러도 안 넘게)
        if await c.svc.db.bump(day, c.cid, "image") > limit:
            await c.svc.db.bump(day, c.cid, "image", -1)
            return True, f"오늘 이 방 그림 한도({limit}장)를 다 썼어요. 내일 다시 하거나 사진을 올려 주세요."
    await c.bot.send_message(c.uid, "🤖 그리는 중… (30초~1분)")
    try:
        data = await c.svc.llm.image(AI_PROMPT.format(want=want), None, c.cid)
        await _store_image(c.svc, c.cid, slot, data, "ai")
        if owner:
            await c.svc.db.bump(day, c.cid, "image")
    except Exception as e:
        if not owner:
            await c.svc.db.bump(day, c.cid, "image", -1)   # 못 그렸으면 한도 돌려줌
        if isinstance(e, ValueError):
            return True, f"❌ {e}"
        log.warning("brand ai image failed: %s", e)
        return True, "😢 그림을 못 만들었어요 (AI 한도·안전 정책·서버 문제). 표현을 바꿔 다시 해 보세요."
    room = await brand.ensure(c.svc.db, c.cid)
    try:
        await c.bot.send_photo(c.uid, await card(c.svc, room, "win" if slot == "win" else "lotto", left=1,
                                                 prize="예시 경품"), caption="🤖 이렇게 나와요")
    except TelegramError:
        pass
    return True, f"✅ {brand.SLOTS[slot]}을 AI 그림으로 바꿨어요."


# ── 방 명령 ───────────────────────────────────────────────
async def c_menu(ctx) -> None:
    """.혜택 — 방: 입구 버튼 글 / 1:1: 메뉴 열기."""
    if ctx.chat_id > 0:
        room = await brand.current(ctx.svc, ctx.user.id)
        if room:
            await open_room(ctx.svc, ctx.bot, ctx.user.id, room)
        else:
            await picker(ctx.svc, ctx.bot, ctx.user.id)
        return
    room = await brand.get(ctx.svc.db, ctx.chat_id)
    if not room or not room.enabled or not await ctx.svc.paid_features(ctx.chat_id):
        await ctx.reply("이 방은 혜택 메뉴가 꺼져 있어요. (관리자: 1:1 메뉴 → 🎁 1:1 메뉴)")
        return
    if not await persist.claim(ctx.svc.db, f"brandpost:{ctx.chat_id}", POST_GAP):
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎁 {room.name[:24]} 혜택 메뉴", url=brand.link(ctx.bot.username, room))]])
    await ctx.reply(f"🎁 <b>{esc(room.name)}</b> — 출석·복권·운세·추천픽은 소담 1:1 에서 👇", reply_markup=kb)


def register_commands() -> None:
    from .. import commands
    cmd = commands.Cmd(("혜택", "혜택메뉴", "메뉴판"), c_menu, help="방 전용 혜택 메뉴 (1:1 에서 열림)", group="출석·복권", dm_ok=True)
    if any(c.fn is c_menu for c in commands.COMMANDS):
        return
    commands.COMMANDS.append(cmd)
    for name in cmd.names:
        commands._INDEX.setdefault(name.lower(), cmd)


# ── 1:1 메인 메뉴 ─────────────────────────────────────────
async def _has_rooms(svc, bot, uid: int) -> bool:
    return bool(await _rooms_ok(svc, uid))


async def r_main(c: PanelCtx) -> Screen:
    room = await brand.current(c.svc, c.uid)
    if room:
        await open_room(c.svc, c.bot, c.uid, room)
        return Screen(None, toast=f"🎁 {room.name}")
    await picker(c.svc, c.bot, c.uid)
    return Screen(None)


async def _prune_tick(svc, bot) -> None:
    now = time.time()
    if now - _pruned[0] >= 6 * 3600:
        _pruned[0] = now
        await brand.prune(svc.db, now)


_pruned = [0.0]

# 등록
menu.register_hub(HubItem(52, "bd", "🎁 1:1 메뉴"))
menu.register_screen("bd", s_bd)
for code, fn in (("bdb", s_buttons), ("bda", s_add), ("bde", s_edit), ("bdx", s_texts), ("bdc", s_colors), ("bdi", s_images),
                 ("bdname", s_bd), ("bdnote", s_bd), ("bdcont", s_bd),           # 아래는 글자 입력의 '⬅️ 메뉴로' 가 돌아갈 화면
                 ("bdlabel", s_buttons), ("bdvalue", s_buttons), ("bdval", s_buttons), ("bdicon", s_buttons),
                 ("bdtext", s_texts), ("bdcolor", s_colors), ("bdimg", s_images), ("bdai", s_images)):
    menu.register_screen(code, fn)
for code, fn in (("bdt", r_toggle), ("bdrl", r_relink), ("bdv", r_preview), ("bdpost", r_post), ("bdap", r_add),
                 ("bdst", r_style), ("bdw", r_wide), ("bdni", r_noicon), ("bdu", r_up), ("bdd", r_del), ("bdxr", r_texts_reset),
                 ("bdth", r_theme), ("bdcr", r_colors_reset), ("bdix", r_image_del), ("bdsamp", r_samples), ("bdstk", r_sticker)):
    menu.register_route(code, Route(fn, ADMIN))
menu.register_input("bdname", f"✏️ 메뉴 이름을 보내 주세요 ({brand.NAME_MAX}자 안). 예: <code>럭키라운지</code>", "bd", in_name, s_bd)
menu.register_input("bdnote", f"📢 복권 카드 아래 안내를 보내 주세요 ({brand.NOTICE_MAX}자 안). 지우려면 <code>없음</code>",
                    "bd", in_notice, s_bd)
menu.register_input("bdcont", "💬 당첨되면 연락할 @아이디를 보내 주세요. 지우려면 <code>없음</code>", "bd", in_contact, s_bd)
menu.register_input("bdlabel", f"✏️ 버튼 이름을 보내 주세요 ({brand.LABEL_MAX}자 안, 이모지 OK)", "bdb", in_label, s_edit)
menu.register_input("bdvalue", "✏️ 새 값을 보내 주세요 (바로가기 = https 주소 · 문의 = @아이디 · 안내 = 글)", "bdb", in_value, s_edit)
menu.register_input("bdval", "값을 보내 주세요", "bdb", in_newvalue, s_buttons)
menu.register_input("bdicon", "😀 버튼 앞에 붙일 <b>프리미엄 이모지</b>를 하나 보내 주세요.", "bdb", in_icon, s_edit)
menu.register_input("bdtext", "📝 새 글자를 보내 주세요 (40자 안). 기본으로 돌리려면 <code>기본</code>", "bdx", in_text, s_texts)
menu.register_input("bdcolor", "🎨 색을 #RRGGBB 로 보내 주세요. 예: <code>#f2c14e</code>", "bdc", in_color, s_colors)
menu.register_input("bdimg", "📤 사진을 보내 주세요 (가로로 긴 사진 · 가운데를 16:9 로 잘라 써요).", "bdi", in_image, s_images,
                    media=True)
menu.register_input("bdai", "🤖 어떤 그림을 원하는지 글로 보내 주세요. 예: <code>금화가 쏟아지는 카지노 밤, 남색과 금색</code>\n"
                    "(글자는 소담이 따로 얹어요 — 그림엔 글자 없이)", "bdi", in_ai, s_images)
menu.register_route("bdm", Route(r_main, PUBLIC, scoped=False))
menu.register_main(24, "bdm", "🎁 혜택 메뉴", _has_rooms)
hooks.add_deep_link("b", on_deep)
hooks.add_dm_text_hook(on_text)
hooks.add_callback_handler("bd", on_button)
hooks.add_tick_hook(_prune_tick)


async def t_benefit(ctx: tools.ToolCtx, a: dict) -> str:
    """'혜택 메뉴 줘' — 방: 입구 버튼 올림 · 1:1: 메뉴 열기."""
    if ctx.chat_id > 0:
        room = await brand.current(ctx.svc, ctx.caller.id)
        if room:
            await open_room(ctx.svc, ctx.bot, ctx.caller.id, room)
            ctx.quiet = True
            return "혜택 메뉴판을 띄웠음."
        if await _rooms_ok(ctx.svc, ctx.caller.id):
            await picker(ctx.svc, ctx.bot, ctx.caller.id)
            ctx.quiet = True
            return "방 고르기 버튼을 보냈음."
        return "혜택 메뉴를 켠 방에 들어가 있지 않음. 방 공지의 [🎁 혜택 메뉴] 버튼으로 들어오라고 안내할 것."
    room = await brand.get(ctx.svc.db, ctx.chat_id)
    if not room or not room.enabled or not await ctx.svc.paid_features(ctx.chat_id):
        return "이 방은 혜택 메뉴가 꺼져 있음. 관리자가 1:1 메뉴 → 🎁 1:1 메뉴에서 켤 수 있다고 안내할 것."
    if not await persist.claim(ctx.svc.db, f"brandpost:{ctx.chat_id}", POST_GAP):
        return "방금 혜택 메뉴 버튼을 올렸음 (1분에 한 번). 위의 버튼을 누르라고 짧게 안내할 것."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎁 {room.name[:24]} 혜택 메뉴", url=brand.link(ctx.bot.username, room))]])
    try:
        await ctx.bot.send_message(ctx.chat_id, f"🎁 <b>{esc(room.name)}</b> — 출석·복권·운세·추천픽은 소담 1:1 에서 👇",
                                   parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        return "입구 버튼을 못 올렸음. 링크: " + brand.link(ctx.bot.username, room)
    ctx.quiet = True
    return "방에 혜택 메뉴 입구 버튼을 올렸음."


tools.register_tool(tools.Tool(
    "benefit_menu", "방 전용 혜택 메뉴(1:1 메뉴판: 출석·복권·운세·추천픽·바로가기·문의): '혜택 메뉴', '메뉴판 줘', '혜택 어디서 봐'. "
                    "방에서 = 입구 버튼 올림, 1:1 = 메뉴판 띄움. 꾸미기는 관리자가 1:1 메뉴 🎁 에서.",
    {}, [], t_benefit), read_only=False)
