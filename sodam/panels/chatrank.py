"""🏆 채팅 순위 — 방장 꾸미기 화면(허브 m:crk) · 방 명령 .순위 · 1:1 메뉴판 부품 'rank' · 미니앱 칸 · 시상식(그 시각에 어제 순위 + 지급 완료 버튼).
데이터·틀은 sodam/chatrank.py. 버튼 rkp:<방>:<날짜>:<등수> = 그 방 텔레그램 관리자 '지급 완료'. 테스트 tests/test_chatrank.py.
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import BadRequest, Forbidden, TelegramError

from .. import brand, chatrank as cr, hooks, menu, persist
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..util import esc, html_plain, rich_html

log = logging.getLogger(__name__)

POST_GAP = 20            # 방에서 .순위 간격 (초)
NOTIFY_ADMINS = 5
TOPS = (3, 5, 7, 10)


# ── 보내기 ────────────────────────────────────────────────
async def send_html(bot, chat_id: int, html: str, kb=None, reply_to: int | None = None) -> Message | None:
    """움직이는 이모지가 거절되면(봇 주인이 프리미엄 아님) 보통 이모지로, 그래도 안 되면 글자만."""
    kw = {"parse_mode": "HTML", "reply_markup": kb, "disable_web_page_preview": True}
    if reply_to:
        kw["reply_parameters"] = ReplyParameters(reply_to, allow_sending_without_reply=True)
    for body in dict.fromkeys((html, cr.strip_custom(html))):
        try:
            return await bot.send_message(chat_id, body[:4000], **kw)
        except BadRequest as e:
            log.warning("chat rank send refused %s: %s", chat_id, e)
    kw["parse_mode"] = None
    return await bot.send_message(chat_id, html_plain(html)[:4000], **kw)


async def room_buttons(svc, bot, chat_id: int) -> InlineKeyboardMarkup | None:
    """🎁 1:1 메뉴에 방장이 넣은 바로가기·문의 버튼을 순위 글 아래에도 (같은 값 한 번만 넣으면 됨)."""
    room = await brand.get(svc.db, chat_id)
    if room is None:
        return None
    rows, half = [], []
    for b in room.buttons:
        if b["k"] == "link" and b.get("v"):
            btn = InlineKeyboardButton(b["t"][:30], url=b["v"])
        elif b["k"] == "contact" and b.get("v"):
            btn = InlineKeyboardButton(b["t"][:30], url=f"https://t.me/{b['v']}")
        else:
            continue
        half.append(btn)
        if len(half) == 2:
            rows.append(half)
            half = []
    if half:
        rows.append(half)
    if room.enabled and getattr(bot, "username", None) and await svc.paid_features(chat_id):
        rows.append([InlineKeyboardButton(f"🎁 {room.name[:20]} 혜택 메뉴", url=brand.link(bot.username, room))])
    return InlineKeyboardMarkup(rows[:4]) if rows else None


async def board(svc, bot, chat_id: int, cfg: cr.Cfg | None = None, period: str | None = None,
                me: int | None = None) -> str:
    cfg = cfg or await cr.get(svc.db, chat_id)
    label, date, rows = await cr.standings(svc, chat_id, cfg, period, bot)
    yday, ydate = None, ""
    if cfg.yday and (period or cfg.period) != "yesterday":
        ydate, yday = await cr.yday_winners(svc, chat_id, cfg, bot)
    mine = await cr.my_place(svc, chat_id, me, cfg, period) if me else None
    return cr.render(cfg, label, date, [(cr.show_name(r, cfg.names), r["n"]) for r in rows], yday or None, ydate, mine)


# ── 방 명령 ───────────────────────────────────────────────
_PERIOD_WORDS = {"오늘": "today", "어제": "yesterday", "주간": "week", "이번주": "week", "월간": "month", "이번달": "month"}


async def c_rank(ctx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("방에서 <code>.순위</code> 를 쳐 주세요. 1:1 에선 🎁 혜택 메뉴의 🏆 버튼으로 봐요.")
        return
    period = _PERIOD_WORDS.get((ctx.args[0] if ctx.args else "").strip())
    if not await persist.claim(ctx.svc.db, f"crk:{ctx.chat_id}:{period or ''}", POST_GAP):
        return                                   # 연달아 치면 조용히 (도배 방지)
    html = await board(ctx.svc, ctx.bot, ctx.chat_id, period=period)
    await send_html(ctx.bot, ctx.chat_id, html, await room_buttons(ctx.svc, ctx.bot, ctx.chat_id), ctx.msg.message_id)


def register_commands() -> None:
    from .. import commands
    if any(c.fn is c_rank for c in commands.COMMANDS):
        return
    cmd = commands.Cmd(("순위", "채팅순위", "채팅왕"), c_rank, usage="[오늘|어제|주간|월간]",
                       help="방장이 꾸민 채팅 순위 (상품·어제 당첨자)", group="집계")
    commands.COMMANDS.append(cmd)
    for name in cmd.names:
        commands._INDEX.setdefault(name.lower(), cmd)


# ── 1:1 메뉴판 · 미니앱 ───────────────────────────────────
async def dm_board(svc, bot, uid: int, room) -> None:
    await send_html(bot, uid, await board(svc, bot, room.chat_id, me=uid))


async def app_data(svc, bot, chat_id: int, uid: int) -> dict:
    """미니앱 🏆 칸 (글자는 화면이 textContent 로 — 움직이는 이모지는 보통 이모지로 보임)."""
    cfg = await cr.get(svc.db, chat_id)
    label, date, rows = await cr.standings(svc, chat_id, cfg, None, bot)
    rank, n = await cr.my_place(svc, chat_id, uid, cfg)
    out = {"label": label, "date": date, "title": html_plain(cr.fill(cfg.head, {"기간": label, "날짜": date})).strip().split("\n")[0][:60],
           "rows": [{"rank": i + 1, "icon": html_plain(cfg.icon(i)), "name": cr.show_name(r, cfg.names), "n": r["n"],
                     "prize": cfg.prize(i), "me": r["user_id"] == uid} for i, r in enumerate(rows)],
           "me": {"rank": rank, "n": n}, "foot": html_plain(cfg.foot).strip()[:600], "yday": [], "ydate": ""}
    if cfg.yday and cfg.period != "yesterday":
        ydate, yday = await cr.yday_winners(svc, chat_id, cfg, bot)
        out["ydate"] = ydate
        out["yday"] = [{"rank": i + 1, "icon": html_plain(cfg.icon(i)), "name": name, "n": cnt, "prize": prize}
                       for i, name, cnt, prize in yday]
    return out


# ── 시상식 ────────────────────────────────────────────────
async def award(svc, bot, chat_id: int, cfg: cr.Cfg, day: str) -> bool:
    """어제(day) 하루 순위를 방에 올리고 기록 + 상품 있는 등수는 관리자 1:1 지급 완료 버튼."""
    _, _, rows = await cr.standings(svc, chat_id, cfg, "yesterday", bot)
    if not rows:
        return False
    await cr.record_awards(svc.db, chat_id, day, cfg, rows)
    html = "🎉 <b>시상식</b>\n" + cr.render(cfg, "어제", cr.short_date(day), [(cr.show_name(r, cfg.names), r["n"]) for r in rows])
    try:
        await send_html(bot, chat_id, html, await room_buttons(svc, bot, chat_id))
    except TelegramError as e:
        log.warning("chat rank award post failed %s: %s", chat_id, e)
    prized = [(i, r) for i, r in enumerate(rows) if cfg.prize(i)]
    if prized:
        await _notify_admins(svc, bot, chat_id, day, cfg, prized)
    return True


async def _notify_admins(svc, bot, chat_id: int, day: str, cfg: cr.Cfg, prized: list) -> int:
    try:
        admins = [u.id for u in await svc.perms.admin_users(bot, chat_id) if not getattr(u, "is_bot", False)][:NOTIFY_ADMINS]
    except TelegramError:
        admins = []
    from ..subscription import chat_title
    title = await chat_title(svc, chat_id)
    lines = [f"🏆 <b>채팅 순위 시상</b> — {esc(title)} ({cr.short_date(day)})"]
    lines += [f"{i + 1}등 {esc(cr.show_name(r, 'user'))} (ID {r['user_id']}) · {r['n']:,}회 — {esc(cfg.prize(i))}" for i, r in prized]
    lines.append("상품을 보낸 뒤 그 등수 버튼을 눌러 주세요.")
    btns = [B(f"✅ {i + 1}등 지급", f"rkp:{chat_id}:{day}:{i + 1}") for i, _ in prized]
    kb = menu._kb([btns[k:k + 2] for k in range(0, len(btns), 2)])
    sent = 0
    for aid in admins:
        try:
            await bot.send_message(aid, "\n".join(lines), parse_mode="HTML", reply_markup=kb)
            sent += 1
        except Forbidden:
            continue
        except TelegramError as e:
            log.warning("chat rank admin notify failed %s: %s", aid, e)
    return sent


async def _award_tick(svc, bot) -> None:
    now = datetime.now(svc.cfg.tz)
    if now.hour not in cr.AWARD_HOURS:
        return
    for chat_id, cfg in await cr.award_rooms(svc.db):
        if cfg.award != now.hour:
            continue
        day = cr.day_of(svc.cfg.tz, 1)
        if not await svc.paid_features(chat_id):
            continue
        if not await persist.claim(svc.db, f"rkaward:{chat_id}:{day}", 2 * 86400):
            continue
        try:
            await award(svc, bot, chat_id, cfg, day)
        except Exception:
            log.exception("chat rank award failed %s", chat_id)


async def on_paid(svc, bot, q, parts) -> None:
    """rkp:<방>:<날짜>:<등수> — 그 방 텔레그램 관리자만, 한 번만."""
    try:
        chat_id, day, rank = int(parts[0]), parts[1], int(parts[2])
    except (IndexError, ValueError):
        await q.answer()
        return
    try:
        admin = await svc.perms.is_tg_admin(bot, chat_id, q.from_user.id)
    except TelegramError:
        admin = False
    if not admin:
        await q.answer("그 방 관리자만 누를 수 있어요.", show_alert=True)
        return
    ok = await cr.mark_paid(svc.db, chat_id, day, rank, q.from_user.id)
    await q.answer(f"✅ {rank}등 지급 완료로 표시했어요" if ok else "이미 지급 완료예요")
    if ok and q.message is not None:
        try:
            await q.edit_message_text((getattr(q.message, "text_html", None) or "🏆 채팅 순위 시상") + f"\n✅ {rank}등 지급 완료",
                                      parse_mode="HTML", reply_markup=q.message.reply_markup)
        except (TelegramError, AttributeError):
            pass


# ── 방장 화면 ─────────────────────────────────────────────
def _next(seq, cur):
    seq = list(seq)
    return seq[(seq.index(cur) + 1) % len(seq)] if cur in seq else seq[0]


def _hour(h: int) -> str:
    return "끔" if h < 0 else f"매일 {h}시"


async def s_crk(c: PanelCtx) -> Screen:
    cfg = await cr.get(c.svc.db, c.cid)
    s = await c.svc.db.get_settings(c.cid)
    rule = (f"{int(s.get('chat_min_chars') or 0)}글자 이상만" if s.get("chat_min_chars") else "모든 글") + \
           (" · 도배 빼고" if s.get("chat_no_repeat") else "")
    prizes = " / ".join(f"{i + 1}등 {esc(p)}" for i, p in enumerate(cfg.prizes)) or "(없음)"
    lines = ["🏆 <b>채팅 순위 꾸미기</b>",
             "숫자는 소담이 세고, 모양은 전부 여기서 정해요. 방에서 <code>.순위</code> · 1:1 메뉴판 🏆 버튼 · 미니앱에 같은 모양으로 나와요.",
             "", f"기간: <b>{cr.PERIODS[cfg.period][1]}</b> · {cfg.top}등까지 · 이름: {cr.NAME_MODES[cfg.names]}",
             f"상품: {prizes}",
             f"줄 모양: <code>{esc(html_plain(cfg.line))}</code>",
             f"등수 그림: {esc(' '.join(html_plain(cfg.icon(i)) for i in range(cfg.top)))}",
             f"관리자 빼기: {'✅' if cfg.no_admin else '❌'} · 어제 당첨자 칸: {'✅' if cfg.yday else '❌'}",
             f"🕛 시상식: <b>{_hour(cfg.award)}</b> (그 시각에 어제 하루 순위를 방에 올리고, 상품 있으면 관리자 1:1 에 [✅ 지급] 버튼)",
             f"📏 세는 기준: {rule} (📊 리포트 화면에서 바꿔요)"]
    cid = c.cid
    rows = [[B(f"📅 기간: {cr.PERIODS[cfg.period][1]} ▸", f"m:crkp:{cid}"), B(f"🔢 {cfg.top}등까지 ▸", f"m:crkn:{cid}")],
            [B("💰 상품", f"m:in:{cid}:crkpz"), B("✏️ 줄 모양", f"m:in:{cid}:crkln")],
            [B("🏅 등수 그림", f"m:in:{cid}:crkic"), B(f"👤 이름: {cr.NAME_MODES[cfg.names].split(' ')[0]} ▸", f"m:crkm:{cid}")],
            [B("📝 머리말", f"m:in:{cid}:crkhd"), B("📝 꼬리말", f"m:in:{cid}:crkft")],
            [B(("✅" if cfg.no_admin else "❌") + " 관리자 빼기", f"m:crka:{cid}"),
             B(("✅" if cfg.yday else "❌") + " 어제 당첨자", f"m:crky:{cid}")],
            [B(f"🕛 시상식: {_hour(cfg.award)} ▸", f"m:crkw:{cid}"), B("📏 세는 기준", f"m:rp:{cid}")],
            [B("👀 미리보기 (내 1:1)", f"m:crkv:{cid}"), B("📣 방에 올리기", f"m:crkpost:{cid}")],
            [B("↩️ 처음 모양으로", f"m:crkr:{cid}")], menu._back(cid)]
    return Screen("\n".join(lines)[:3900], menu._kb(rows))


async def _change(c: PanelCtx, toast: str, **kw) -> Screen:
    cfg = await cr.get(c.svc.db, c.cid)
    for k, v in kw.items():
        setattr(cfg, k, v)
    await cr.save(c.svc.db, c.cid, cfg, c.uid)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "chat_rank " + ",".join(f"{k}={v}" for k, v in kw.items())[:200])
    sc = await s_crk(c)
    sc.toast = toast
    return sc


async def r_period(c: PanelCtx) -> Screen:
    cur = (await cr.get(c.svc.db, c.cid)).period
    nxt = _next(cr.PERIODS, cur)
    return await _change(c, f"기간: {cr.PERIODS[nxt][1]}", period=nxt)


async def r_top(c: PanelCtx) -> Screen:
    nxt = _next(TOPS, (await cr.get(c.svc.db, c.cid)).top)
    return await _change(c, f"{nxt}등까지", top=nxt)


async def r_names(c: PanelCtx) -> Screen:
    nxt = _next(cr.NAME_MODES, (await cr.get(c.svc.db, c.cid)).names)
    return await _change(c, f"이름: {cr.NAME_MODES[nxt]}", names=nxt)


async def r_admin(c: PanelCtx) -> Screen:
    v = not (await cr.get(c.svc.db, c.cid)).no_admin
    return await _change(c, "관리자를 순위에서 빼요" if v else "관리자도 순위에 넣어요", no_admin=v)


async def r_yday(c: PanelCtx) -> Screen:
    v = not (await cr.get(c.svc.db, c.cid)).yday
    return await _change(c, "어제 당첨자 칸 켬" if v else "어제 당첨자 칸 끔", yday=v)


async def r_award(c: PanelCtx) -> Screen:
    nxt = _next(cr.AWARD_HOURS, (await cr.get(c.svc.db, c.cid)).award)
    if nxt >= 0 and not await c.svc.paid_features(c.cid):
        return Screen(None, toast="시상식은 이용 중인 방에서만 돼요.", alert=True)
    return await _change(c, f"시상식: {_hour(nxt)}", award=nxt)


async def r_reset(c: PanelCtx) -> Screen:
    old = await cr.get(c.svc.db, c.cid)
    fresh = cr.Cfg(award=old.award)        # 시상식 시각은 그대로 (모양만 처음으로)
    await cr.save(c.svc.db, c.cid, fresh, c.uid)
    sc = await s_crk(c)
    sc.toast = "↩️ 처음 모양으로 돌렸어요 (상품·그림·머리말·꼬리말 지움)"
    sc.alert = True
    return sc


async def r_preview(c: PanelCtx) -> Screen:
    try:
        await send_html(c.bot, c.uid, await board(c.svc, c.bot, c.cid, me=c.uid),
                        await room_buttons(c.svc, c.bot, c.cid))
    except TelegramError:
        return Screen(None, toast="1:1 로 못 보냈어요. 소담 1:1 을 먼저 열어 주세요.", alert=True)
    return Screen(None, toast="👀 1:1 로 보냈어요 (아래 '👤 나' 줄은 1:1 에서만 보여요)")


async def r_post(c: PanelCtx) -> Screen:
    if not await persist.claim(c.svc.db, f"crk:{c.cid}:", POST_GAP):
        return Screen(None, toast=f"방금 올렸어요. {POST_GAP}초 뒤에 다시.", alert=True)
    try:
        await send_html(c.bot, c.cid, await board(c.svc, c.bot, c.cid), await room_buttons(c.svc, c.bot, c.cid))
    except TelegramError as e:
        return Screen(None, toast=f"방에 못 올렸어요 ({str(e)[:60]})", alert=True)
    return Screen(None, toast="📣 방에 올렸어요")


def _html_of(msg: Message) -> str:
    return rich_html(msg) or esc((msg.text or msg.caption or "").strip())


async def _set_tpl(c: PanelCtx, msg: Message, key: str, fields, limit: int, need=(), can_clear: bool = False) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if raw in ("기본", "처음"):
        return await _store(c, key, cr.DEFAULT[key], "↩️ 처음 모양으로 돌렸어요")
    if can_clear and raw in ("없음", "지우기"):
        return await _store(c, key, "", "🗑 지웠어요")
    try:
        html = cr.clean_tpl(_html_of(msg), fields, limit, need)
    except ValueError as e:
        return False, f"❌ {e}"
    return await _store(c, key, html, "✅ 바꿨어요. 👀 미리보기로 확인해 보세요.")


async def _store(c: PanelCtx, key: str, val, ok: str) -> tuple[bool, str]:
    cfg = await cr.get(c.svc.db, c.cid)
    setattr(cfg, key, val)
    await cr.save(c.svc.db, c.cid, cfg, c.uid)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"chat_rank {key}")
    return True, ok


async def in_line(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    return await _set_tpl(c, msg, "line", cr.LINE_FIELDS, cr.LINE_MAX, need=("이름",))


async def in_head(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    return await _set_tpl(c, msg, "head", cr.HEAD_FIELDS, cr.HEAD_MAX, can_clear=True)


async def in_foot(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    return await _set_tpl(c, msg, "foot", (), cr.FOOT_MAX, can_clear=True)


async def in_prizes(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if raw in ("없음", "지우기"):
        return await _store(c, "prizes", [], "🗑 상품을 지웠어요")
    try:
        prizes = cr.clean_prizes(raw)
    except ValueError as e:
        return False, f"❌ {e}"
    if not prizes:
        return False, "❌ 한 줄에 하나씩 보내 주세요"
    return await _store(c, "prizes", prizes, f"✅ 상품 {len(prizes)}개 — " + " / ".join(f"{i + 1}등 {p}" for i, p in enumerate(prizes)))


async def in_icons(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    if raw in ("기본", "처음", "없음"):
        return await _store(c, "icons", [], "↩️ 기본 그림(🥇🥈🥉 4️⃣…)으로")
    try:
        icons = cr.clean_icons(_html_of(msg))
    except ValueError as e:
        return False, f"❌ {e}"
    return await _store(c, "icons", icons, f"✅ 등수 그림 {len(icons)}개 (모자란 등수는 기본 그림)")


# 등록
menu.register_hub(HubItem(54, "crk", "🏆 채팅 순위"))
menu.register_screen("crk", s_crk)
for _code in ("crkpz", "crkln", "crkic", "crkhd", "crkft"):
    menu.register_screen(_code, s_crk)
for _code, _fn in (("crkp", r_period), ("crkn", r_top), ("crkm", r_names), ("crka", r_admin), ("crky", r_yday),
                   ("crkw", r_award), ("crkr", r_reset), ("crkv", r_preview), ("crkpost", r_post)):
    menu.register_route(_code, Route(_fn, ADMIN))
menu.register_input("crkpz", "💰 등수별 상품을 <b>한 줄에 하나씩</b> 1등부터 보내 주세요.\n예:\n<code>50,000원\n40,000원\n30,000원</code>\n"
                    f"(하나에 {cr.PRIZE_MAX}자 · 링크 X · 지우려면 <code>없음</code>)", "crk", in_prizes, s_crk)
menu.register_input("crkln", "✏️ 한 줄 모양을 보내 주세요. 칸: <code>{등수}</code> <code>{이름}</code> <code>{횟수}</code> <code>{상품}</code>\n"
                    "예: <code>{등수} {이름} — {횟수}회 {상품}</code>\n움직이는 이모지·굵게도 그대로 써요. 처음으로 = <code>기본</code>",
                    "crk", in_line, s_crk)
menu.register_input("crkic", "🏅 1등부터 등수 그림을 <b>띄어서</b> 보내 주세요 (움직이는 이모지 OK).\n예: <code>🥇 🥈 🥉 4️⃣ 5️⃣</code> · 처음으로 = <code>기본</code>",
                    "crk", in_icons, s_crk)
menu.register_input("crkhd", "📝 머리말을 보내 주세요. 칸: <code>{기간}</code> <code>{날짜}</code>\n예: <code>🦊 {기간}의 채팅 순위\n📅 {날짜}</code>\n"
                    "지우려면 <code>없음</code> · 처음으로 = <code>기본</code>", "crk", in_head, s_crk)
menu.register_input("crkft", f"📝 꼬리말을 보내 주세요 ({cr.FOOT_MAX}자 안). 예: <code>매일 자정 시상식 · 5글자 이상만 집계</code>\n"
                    "움직이는 이모지·굵게 OK, 링크는 X (🎁 1:1 메뉴의 바로가기 버튼이 아래에 붙어요). 지우려면 <code>없음</code>",
                    "crk", in_foot, s_crk)
hooks.add_callback_handler("rkp", on_paid)
hooks.add_tick_hook(_award_tick)
