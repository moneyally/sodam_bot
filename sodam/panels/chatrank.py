"""🏆 채팅 순위 — 방장 꾸미기 화면(허브 m:crk) · 방 명령 .순위 · .나(내 채팅 수·등수) · 1:1 메뉴판 부품 'rank' · 미니앱 칸 · 시상식(그 시각에 어제 순위 + 지급 완료 버튼).
데이터·틀은 sodam/chatrank.py. 버튼 rkp:<방>:<날짜>:<등수> = 그 방 텔레그램 관리자 '지급 완료'. 테스트 tests/test_chatrank.py.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import BadRequest, Forbidden, TelegramError

from .. import brand, chatrank as cr, hooks, menu, persist, tools
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..permissions import Role
from ..util import esc, html_plain, rich_html

log = logging.getLogger(__name__)

POST_GAP = 20            # 방에서 .순위 간격 (초)
ME_GAP = 10              # .나 한 사람 간격 (초)
NOTIFY_ADMINS = 5
TOPS = (3, 5, 7, 10, 15, 20)


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
    main = [(cr.show_name(r, cfg.names), r["n"]) for r in rows]
    yday, ydate = None, ""
    if cfg.yday and (period or cfg.period) not in ("yesterday", "lastweek"):
        ydate, yday = await cr.yday_winners(svc, chat_id, cfg, bot)
    mine = await cr.my_place(svc, chat_id, me, cfg, period) if me else None
    if cfg.extra and not period:        # 한 카드에 본 기간 + 추가 기간들 (.순위 로 기간 지정하면 그 기간만)
        secs = [cr.section(cfg, label, date, main)]
        for p in cfg.extra:
            l2, d2, r2 = await cr.standings(svc, chat_id, cfg, p, bot)
            secs.append(cr.section(cfg, l2, d2, [(cr.show_name(r, cfg.names), r["n"]) for r in r2]))
        return cr.render_sections(cfg, secs, yday or None, ydate, mine, cr.winners_label(cfg))
    return cr.render(cfg, label, date, main, yday or None, ydate, mine, cr.winners_label(cfg))


# ── 방 명령 ───────────────────────────────────────────────
_PERIOD_WORDS = {"오늘": "today", "어제": "yesterday", "이번주": "thisweek", "금주": "thisweek", "지난주": "lastweek", "저번주": "lastweek",
                 "주간": "week", "7일": "week", "월간": "month", "이번달": "month"}


async def c_rank(ctx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("방에서 <code>.순위</code> 를 쳐 주세요. 1:1 에선 🎁 혜택 메뉴의 🏆 버튼으로 봐요.")
        return
    period = _PERIOD_WORDS.get((ctx.args[0] if ctx.args else "").strip())
    if not await persist.claim(ctx.svc.db, f"crk:{ctx.chat_id}:{period or ''}", POST_GAP):
        return                                   # 연달아 치면 조용히 (도배 방지)
    html = await board(ctx.svc, ctx.bot, ctx.chat_id, period=period)
    await send_html(ctx.bot, ctx.chat_id, html, await room_buttons(ctx.svc, ctx.bot, ctx.chat_id), ctx.msg.message_id)


async def c_me(ctx) -> None:
    """.나 — 내 채팅 수·등수 (순위와 같은 기간·세는 기준). 한 사람 10초에 한 번."""
    if ctx.chat_id > 0:
        await ctx.reply("방에서 <code>.나</code> 를 쳐 주세요.")
        return
    if not await persist.claim(ctx.svc.db, f"crkme:{ctx.chat_id}:{ctx.user.id}", ME_GAP):
        return
    period = _PERIOD_WORDS.get((ctx.args[0] if ctx.args else "").strip())
    cfg = await cr.get(ctx.svc.db, ctx.chat_id)
    rank, n = await cr.my_place(ctx.svc, ctx.chat_id, ctx.user.id, cfg, period)
    row = {"username": getattr(ctx.user, "username", None), "first_name": ctx.user.first_name}
    html = cr.me_html(cfg, cr.show_name(row, "name" if cfg.names == "mask" else cfg.names), cr.PERIODS[period or cfg.period][1], rank, n)
    await send_html(ctx.bot, ctx.chat_id, html, reply_to=ctx.msg.message_id)


async def can_reset_room(svc, bot, chat_id: int, user_id: int) -> bool:
    """방 전체 채팅 수 초기화 권한 — 소담 운영자(owner) 또는 그 방 소유자(creator)만.
    일반 관리자·봇관리자는 제외 (관리자 남용 방지 — 오너 요청 2026-11-02)."""
    if user_id in await svc.perms.owners():
        return True
    return await svc.perms.is_creator(bot, chat_id, user_id)


async def c_rank_reset_all(ctx) -> None:
    """방 전체 채팅 수를 0부터 다시 (타수 초기화) — 방 소유자·소담 운영자만, [취소] 로 되돌림.
    글은 안 지움: 대화 기록·AI 기억·검색·.통계·.랭킹 은 그대로, .순위·.나·시상식·미니앱만 바뀜."""
    if ctx.chat_id > 0:
        await ctx.reply("방에서 쳐 주세요 (그 방 소유자 또는 소담 운영자만).")
        return
    if not await can_reset_room(ctx.svc, ctx.bot, ctx.chat_id, ctx.user.id):
        await ctx.reply("⛔ 방 전체 채팅 수 초기화는 <b>방 소유자(방 만든 사람)</b> 또는 소담 운영자만 할 수 있어요 "
                        "(관리자 남용 방지).\n한 사람만 초기화하려면 관리자가 <code>.채팅리셋</code> (답장/@아이디).")
        return
    undo = bool(ctx.args) and ctx.args[0].strip() in ("취소", "해제", "undo", "되돌리기", "원래대로")
    cfg = await cr.get(ctx.svc.db, ctx.chat_id)
    if undo:
        cfg.reset_at = 0
        await cr.save(ctx.svc.db, ctx.chat_id, cfg, ctx.user.id)
        cr._CACHE.clear()
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, None, "rank_reset_all_undo", "전체 채팅수 초기화 취소")
        await ctx.reply("↩️ 방 전체 채팅 수 초기화를 취소했어요 (원래 숫자로).")
        return
    cfg.reset_at = int(datetime.now(ctx.svc.cfg.tz).timestamp())
    await cr.save(ctx.svc.db, ctx.chat_id, cfg, ctx.user.id)
    cr._CACHE.clear()
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, None, "rank_reset_all", "전체 채팅수 초기화")
    await ctx.reply("🔄 지금부터 <b>방 전체 채팅 수</b>를 0부터 다시 세요.\n"
                    "<code>.순위</code>·<code>.나</code>·시상식·미니앱만 바뀌고 "
                    "<b>대화 기록·AI 기억·.랭킹·.통계는 그대로</b>예요.\n"
                    "되돌리기: <code>.전체순위초기화 취소</code>")


async def c_chatreset(ctx) -> None:
    """특정 멤버만 채팅 수 0부터 다시 (도배범 순위 초기화). 글·기록은 그대로, .순위·.나·미니앱만."""
    from .. import commands
    t = await commands._target(ctx, sanction=False)
    if not t:
        return
    uid, name, _ = t
    await cr.member_reset(ctx.svc.db, ctx.chat_id, uid, ctx.user.id)
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, uid, "rank_reset", "채팅수 초기화")
    await ctx.reply(f"🔄 <b>{esc(name)}</b> 님 채팅 수를 지금부터 0으로 다시 세요.\n"
                    "<code>.순위</code>·<code>.나</code>·미니앱만 바뀌고 대화 기록·AI 기억은 그대로예요. "
                    "되돌리기: 그 사람에게 <code>.채팅리셋취소</code> (답장/@아이디).")


async def c_chatreset_cancel(ctx) -> None:
    """특정 멤버 채팅 수 초기화 취소 (원래 수로)."""
    from .. import commands
    t = await commands._target(ctx, sanction=False)
    if not t:
        return
    uid, name, _ = t
    if await cr.member_unreset(ctx.svc.db, ctx.chat_id, uid):
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, uid, "rank_unreset", "채팅수 초기화 취소")
        await ctx.reply(f"↩️ <b>{esc(name)}</b> 님 채팅 수 초기화를 취소했어요 (원래 수로 복구).")
    else:
        await ctx.reply(f"<b>{esc(name)}</b> 님은 채팅 수 초기화된 적이 없어요.")


def register_commands() -> None:
    from .. import commands
    if any(c.fn is c_rank for c in commands.COMMANDS):
        return
    for cmd in (commands.Cmd(("순위", "채팅순위", "채팅왕"), c_rank, usage="[오늘|어제|이번주|지난주|주간|월간]",
                             help="방장이 꾸민 채팅 순위 (상품·어제 당첨자)", group="집계"),
                commands.Cmd(("나", "내순위", "내채팅"), c_me, usage="[오늘|어제|주간|월간]",
                             help="내 채팅 수·등수", group="집계"),
                commands.Cmd(("채팅리셋", "순위리셋", "채팅초기화"), c_chatreset, Role.ADMIN, usage="@user|답장|ID",
                             help="그 사람만 채팅 수 0부터 다시 (도배범 순위 초기화)", group="집계"),
                commands.Cmd(("채팅리셋취소", "순위리셋취소", "채팅초기화취소"), c_chatreset_cancel, Role.ADMIN,
                             usage="@user|답장|ID", help="그 사람 채팅 수 초기화 취소", group="집계"),
                commands.Cmd(("전체순위초기화", "전체채팅초기화", "타수초기화", "방순위초기화"), c_rank_reset_all, Role.ADMIN,
                             usage="[취소]", help="방 전체 채팅 수 0부터 다시 (방 소유자·운영자만, 기억은 그대로)", group="집계")):
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)


async def t_chat_reset_member(ctx: tools.ToolCtx, a: dict) -> str:
    """AI 도구: 특정 멤버만 채팅 수 초기화 / 취소 (관리자·그 방)."""
    row, err = await tools._resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    uid, name = row["user_id"], tools._row_name(row)
    on = bool(a.get("on", True))
    if on:
        await cr.member_reset(ctx.svc.db, ctx.chat_id, uid, ctx.caller.id)
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, uid, "rank_reset", "채팅수 초기화")
        return (f"{name}님 채팅 수를 지금부터 0으로 다시 세게 함 (.순위·.나·미니앱만, 글·기록은 그대로). "
                "짧게 '초기화했어요' 정도로.")
    if not await cr.member_unreset(ctx.svc.db, ctx.chat_id, uid):
        return f"{name}님은 채팅 수 초기화된 적이 없음. 그렇게 안내."
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, uid, "rank_unreset", "채팅수 초기화 취소")
    return f"{name}님 채팅 수 초기화를 취소함 (원래 수로). 짧게 안내."


tools.register_tool(tools.Tool(
    "chat_reset_member",
    "특정 멤버 한 명의 채팅 순위 수(.순위·.나·미니앱)를 지금부터 0으로 다시 세게 한다 (on=true) / 취소한다 (on=false). "
    "'쟤 채팅 수 초기화해줘', '도배하는 ○○ 순위 0으로', '○○ 채팅 리셋' → on=true. 대화 기록·AI 기억·.통계는 안 건드림. "
    "방 전체 리셋이 아니라 그 사람만. name 에 이름·@아이디·숫자 ID.",
    {"name": {"type": "string"}, "on": {"type": "boolean"}},
    ["name"], t_chat_reset_member, Role.ADMIN, where="room"), read_only=False)


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
        out["ydate"], out["ylabel"] = ydate, cr.winners_label(cfg)
        out["yday"] = [{"rank": i + 1, "icon": html_plain(cfg.icon(i)), "name": name, "n": cnt, "prize": prize}
                       for i, name, cnt, prize in yday]
    return out


# ── 시상식 ────────────────────────────────────────────────
async def award(svc, bot, chat_id: int, cfg: cr.Cfg, day: str, period: str = "yesterday") -> bool:
    """어제(매주면 지난 주 월~일) 순위를 방에 올리고 기록(day = 기록 열쇠) + 상품 있는 등수는 관리자 1:1 지급 완료 버튼."""
    label, date, rows = await cr.standings(svc, chat_id, cfg, period, bot)
    if not rows:
        return False
    await cr.record_awards(svc.db, chat_id, day, cfg, rows)
    html = "🎉 <b>시상식</b>\n" + cr.render(cfg, label, date, [(cr.show_name(r, cfg.names), r["n"]) for r in rows])
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
    lines = [f"🏆 <b>채팅 순위 시상</b> — {esc(title)} ({cr.short_date(day.lstrip('w'))}{'~ 한 주' if day.startswith('w') else ''})"]
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


def due(cfg: cr.Cfg, now: datetime) -> bool:
    if cfg.award == cr.WEEKLY:
        return now.weekday() == 0 and now.hour == 0
    return cfg.award == now.hour


async def _award_tick(svc, bot) -> None:
    now = datetime.now(svc.cfg.tz)
    if now.hour not in cr.AWARD_HOURS:
        return
    for chat_id, cfg in await cr.award_rooms(svc.db):
        if not due(cfg, now):
            continue
        day, period = cr.award_key(svc.cfg.tz, cfg)
        if not await svc.paid_features(chat_id):
            continue
        if not await persist.claim(svc.db, f"rkaward:{chat_id}:{day}", 8 * 86400):
            continue
        try:
            await award(svc, bot, chat_id, cfg, day, period)
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
    return cr.AWARD_LABEL.get(h, f"매일 {h}시")


async def s_crk(c: PanelCtx) -> Screen:
    cfg = await cr.get(c.svc.db, c.cid)
    s = await c.svc.db.get_settings(c.cid)
    rule = (f"{int(s.get('chat_min_chars') or 0)}글자 이상만" if s.get("chat_min_chars") else "모든 글") + \
           (" · 도배 빼고" if s.get("chat_no_repeat") else "")
    prizes = " / ".join(f"{i + 1}등 {esc(p)}" for i, p in enumerate(cfg.prizes)) or "(없음)"
    lines = ["🏆 <b>채팅 순위 꾸미기</b>",
             "숫자는 소담이 세고, 모양은 전부 여기서 정해요. 방에서 <code>.순위</code> · 1:1 메뉴판 🏆 버튼 · 미니앱에 같은 모양으로 나와요.",
             "", f"기간: <b>{cr.PERIODS[cfg.period][1]}</b> · {cfg.top}등까지 · 이름: {cr.NAME_MODES[cfg.names]}",
             f"➕ 추가 기간(한 카드에 같이): {' + '.join(cr.PERIODS[p][0] for p in cfg.extra) if cfg.extra else '없음'}",
             f"상품: {prizes}",
             f"줄 모양: <code>{esc(html_plain(cfg.line))}</code>",
             f"<code>.나</code> 모양: <code>{esc(html_plain(cfg.me))}</code>",
             f"등수 그림: {esc(' '.join(html_plain(cfg.icon(i)) for i in range(cfg.top)))}",
             f"관리자 빼기: {'✅' if cfg.no_admin else '❌'} · 어제 당첨자 칸: {'✅' if cfg.yday else '❌'}",
             f"🕛 시상식: <b>{_hour(cfg.award)}</b> (매일 = 어제 하루 순위, 매주 월요일 0시 = 지난 주 월~일 순위를 방에 올리고, 상품 있으면 관리자 1:1 에 [✅ 지급] 버튼)",
             f"📏 세는 기준: {rule} (📊 리포트 화면에서 바꿔요)"]
    if cfg.reset_at:
        lines.append(f"🔄 채팅 수 초기화: {datetime.fromtimestamp(cfg.reset_at, c.svc.cfg.tz):%m/%d %H:%M} 뒤 글만 셈 (대화 기록은 그대로)")
    cid = c.cid
    rows = [[B(f"📅 기간: {cr.PERIODS[cfg.period][1]} ▸", f"m:crkp:{cid}"), B(f"🔢 {cfg.top}등까지 ▸", f"m:crkn:{cid}")],
            [B(f"➕ 추가 기간 ({len(cfg.extra)}개) ▸", f"m:crkx:{cid}")],
            [B("💰 상품", f"m:in:{cid}:crkpz"), B("✏️ 줄 모양", f"m:in:{cid}:crkln")],
            [B("🏅 등수 그림", f"m:in:{cid}:crkic"), B(f"👤 이름: {cr.NAME_MODES[cfg.names].split(' ')[0]} ▸", f"m:crkm:{cid}")],
            [B("📝 머리말", f"m:in:{cid}:crkhd"), B("📝 꼬리말", f"m:in:{cid}:crkft")],
            [B("🙋 .나 모양", f"m:in:{cid}:crkme")],
            [B(("✅" if cfg.no_admin else "❌") + " 관리자 빼기", f"m:crka:{cid}"),
             B(("✅" if cfg.yday else "❌") + " 어제 당첨자", f"m:crky:{cid}")],
            [B(f"🕛 시상식: {_hour(cfg.award)} ▸", f"m:crkw:{cid}"), B("📏 세는 기준", f"m:rp:{cid}")],
            [B("👀 미리보기 (내 1:1)", f"m:crkv:{cid}"), B("📣 방에 올리기", f"m:crkpost:{cid}")],
            [B("🔄 채팅 수 0부터 다시", f"m:crk0:{cid}")] + ([B("↩️ 초기화 취소", f"m:crk0x:{cid}")] if cfg.reset_at else []),
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
    nxt = _next(cr.PERIOD_CHOICES, cur)
    return await _change(c, f"기간: {cr.PERIODS[nxt][1]}", period=nxt)


async def r_top(c: PanelCtx) -> Screen:
    nxt = _next(TOPS, (await cr.get(c.svc.db, c.cid)).top)
    return await _change(c, f"{nxt}등까지", top=nxt)


async def r_extra(c: PanelCtx) -> Screen:
    """➕ 추가 기간: 본 기간 말고 한 카드에 같이 쌓을 기간을 체크 (泰山 봇처럼 일간+주간)."""
    cfg = await cr.get(c.svc.db, c.cid)
    cid = c.cid
    rows = []
    for p in cr.PERIOD_CHOICES:
        if p == cfg.period:
            continue
        on = p in cfg.extra
        rows.append([B(("✅ " if on else "⬜ ") + cr.PERIODS[p][1], f"m:crkxt:{cid}:{p}")])
    now = " + ".join(cr.PERIODS[p][0] for p in cfg.extra) if cfg.extra else "없음"
    return Screen(f"➕ <b>추가 기간</b> (한 카드에 같이 보여요)\n\n본 기간: <b>{cr.PERIODS[cfg.period][1]}</b>\n"
                  f"추가: <b>{now}</b> (최대 {cr.EXTRA_MAX}개)\n\n체크한 기간이 본 기간 아래에 섹션으로 쌓여요.",
                  menu._kb(rows + [[B("⬅️ 뒤로", f"m:crk:{cid}")]]))


async def r_extra_toggle(c: PanelCtx) -> Screen:
    p = c.arg(1)
    cfg = await cr.get(c.svc.db, c.cid)
    if p not in cr.PERIOD_CHOICES or p == cfg.period:
        return await r_extra(c)
    extra = list(cfg.extra)
    if p in extra:
        extra.remove(p)
        toast = f"{cr.PERIODS[p][0]} 뺌"
    elif len(extra) >= cr.EXTRA_MAX:
        sc = await r_extra(c)
        sc.toast = f"추가 기간은 최대 {cr.EXTRA_MAX}개예요."
        sc.alert = True
        return sc
    else:
        extra.append(p)
        toast = f"{cr.PERIODS[p][0]} 추가"
    cfg.extra = extra
    await cr.save(c.svc.db, c.cid, cfg, c.uid)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"chat_rank extra={extra}")
    sc = await r_extra(c)
    sc.toast = toast
    return sc


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
    fresh = cr.Cfg(award=old.award, reset_at=old.reset_at)   # 시상식 시각·채팅 수 초기화는 그대로 (모양만 처음으로)
    await cr.save(c.svc.db, c.cid, fresh, c.uid)
    sc = await s_crk(c)
    sc.toast = "↩️ 처음 모양으로 돌렸어요 (상품·그림·머리말·꼬리말 지움)"
    sc.alert = True
    return sc


async def _reset_guard(c: PanelCtx) -> Screen | None:
    """방 전체 초기화는 방 소유자·소담 운영자만 (관리자 남용 방지)."""
    if not await can_reset_room(c.svc, c.bot, c.cid, c.uid):
        return Screen(None, toast="방 전체 채팅 수 초기화는 방 소유자(방 만든 사람) 또는 소담 운영자만 돼요.", alert=True)
    return None


async def r_zero_ask(c: PanelCtx) -> Screen:
    """초기화는 되돌릴 수 있지만 순위가 바로 바뀌니 한 번 더 묻기."""
    if g := await _reset_guard(c):
        return g
    text = ("🔄 <b>채팅 수를 0부터 다시 셀까요?</b>\n"
            "지금부터 쓴 글만 <code>.순위</code> · <code>.나</code> · 시상식에 셈. 대화 기록·AI 기억·<code>.랭킹</code> 은 그대로예요.\n"
            "잘못 눌렀으면 [↩️ 초기화 취소] 로 원래대로 돌아가요.")
    return Screen(text, menu._kb([[B("✅ 0부터 다시", f"m:crk0y:{c.cid}"), B("취소", f"m:crk:{c.cid}")]]))


async def r_zero(c: PanelCtx) -> Screen:
    if g := await _reset_guard(c):
        return g
    now = int(datetime.now(c.svc.cfg.tz).timestamp())
    sc = await _change(c, "🔄 지금부터 0부터 다시 세요", reset_at=now)
    cr._CACHE.clear()
    return sc


async def r_zero_undo(c: PanelCtx) -> Screen:
    if g := await _reset_guard(c):
        return g
    sc = await _change(c, "↩️ 초기화를 취소했어요 (원래 숫자)", reset_at=0)
    cr._CACHE.clear()
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


async def in_me(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    return await _set_tpl(c, msg, "me", cr.ME_FIELDS, cr.LINE_MAX, need=("횟수",))


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
for _code in ("crkpz", "crkln", "crkic", "crkhd", "crkft", "crkme"):
    menu.register_screen(_code, s_crk)
for _code, _fn in (("crkp", r_period), ("crkn", r_top), ("crkm", r_names), ("crka", r_admin), ("crky", r_yday),
                   ("crkw", r_award), ("crkr", r_reset), ("crkv", r_preview), ("crkpost", r_post),
                   ("crkx", r_extra), ("crkxt", r_extra_toggle),
                   ("crk0", r_zero_ask), ("crk0y", r_zero), ("crk0x", r_zero_undo)):
    menu.register_route(_code, Route(_fn, ADMIN))
menu.register_input("crkpz", "💰 등수별 상품을 <b>한 줄에 하나씩</b> 1등부터 보내 주세요.\n예:\n<code>50,000원\n40,000원\n30,000원</code>\n"
                    f"(하나에 {cr.PRIZE_MAX}자 · 링크 X · 지우려면 <code>없음</code>)", "crk", in_prizes, s_crk)
menu.register_input("crkln", "✏️ 한 줄 모양을 보내 주세요. 칸: <code>{등수}</code> <code>{이름}</code> <code>{횟수}</code> <code>{상품}</code>\n"
                    "예: <code>{등수} {이름} — {횟수}회 {상품}</code>\n움직이는 이모지·굵게도 그대로 써요. 처음으로 = <code>기본</code>",
                    "crk", in_line, s_crk)
menu.register_input("crkic", "🏅 1등부터 등수 그림을 <b>띄어서</b> 보내 주세요 (움직이는 이모지 OK).\n예: <code>🥇 🥈 🥉 4️⃣ 5️⃣</code> · 처음으로 = <code>기본</code>",
                    "crk", in_icons, s_crk)
menu.register_input("crkme", "🙋 <code>.나</code> 를 쳤을 때 나올 한 줄을 보내 주세요. 칸: <code>{이름}</code> <code>{등수}</code> <code>{횟수}</code> <code>{기간}</code>\n"
                    "예: <code>{이름} 님 {기간} 채팅 {횟수}개 · {등수}위 🔥</code> · 처음으로 = <code>기본</code>", "crk", in_me, s_crk)
menu.register_input("crkhd", "📝 머리말을 보내 주세요. 칸: <code>{기간}</code> <code>{날짜}</code>\n예: <code>🦊 {기간}의 채팅 순위\n📅 {날짜}</code>\n"
                    "지우려면 <code>없음</code> · 처음으로 = <code>기본</code>", "crk", in_head, s_crk)
menu.register_input("crkft", f"📝 꼬리말을 보내 주세요 ({cr.FOOT_MAX}자 안). 예: <code>매일 자정 시상식 · 5글자 이상만 집계</code>\n"
                    "움직이는 이모지·굵게 OK, 링크는 X (🎁 1:1 메뉴의 바로가기 버튼이 아래에 붙어요). 지우려면 <code>없음</code>",
                    "crk", in_foot, s_crk)
hooks.add_callback_handler("rkp", on_paid)
hooks.add_tick_hook(_award_tick)


# ── AI 도구: 말로 꾸미기 (고객 2026-10-12 백악관 — 1:1 에서 '1등 5만원… 5글자 이상… 🥇🥈' 처럼 말하면 그대로) ──
AWARD_WORDS = {"off": -1, "daily0": 0, "daily9": 9, "daily12": 12, "weekly": cr.WEEKLY}


def _custom_emoji_tags(msg) -> list[str]:
    """요청 메시지의 움직이는 이모지(순서대로) — AI 는 그 숨은 번호를 못 옮기므로 코드가 메시지에서 직접."""
    html = rich_html(msg) if msg is not None else None
    return re.findall(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", html or "", re.S)


async def t_chat_rank(ctx, a: dict) -> str:
    from .. import tools
    room, head = await tools.room_scope(ctx, a)
    if room is None:
        return head
    cid, uid = room.chat_id, ctx.caller.id
    try:
        admin = await ctx.svc.perms.is_admin(ctx.bot, cid, uid)
    except TelegramError:
        admin = False
    if not admin:
        return head + "이 방 관리자만 채팅 순위 모양을 바꿀 수 있음. 관리자에게 부탁하라고 안내."
    cfg = await cr.get(ctx.svc.db, cid)
    if a.get("action") != "set":
        return head + _summary(cfg, await ctx.svc.db.get_settings(cid))
    done, bad = [], []

    def tpl(key, fields, limit, need=()):
        raw = a.get(key)
        if raw is None:
            return
        raw = str(raw).strip()
        if raw in ("", "없음") and key in ("head", "foot"):
            setattr(cfg, key, "")
            done.append(key)
            return
        try:
            setattr(cfg, key, cr.clean_tpl(esc(raw), fields, limit, need))
            done.append(key)
        except ValueError as e:
            bad.append(f"{key}: {e}")

    if a.get("period") in cr.PERIOD_CHOICES:
        cfg.period = a["period"]
        done.append("period")
    if isinstance(a.get("top"), int):
        cfg.top = max(cr.TOP_MIN, min(cr.TOP_MAX, a["top"]))
        done.append("top")
    if isinstance(a.get("prizes"), list):
        try:
            cfg.prizes = cr.clean_prizes("\n".join(str(p) for p in a["prizes"]))
            done.append("prizes")
        except ValueError as e:
            bad.append(f"prizes: {e}")
    tags = _custom_emoji_tags(ctx.request_msg) if a.get("icons_from_message") else []
    if tags:
        cfg.icons = tags[:cr.TOP_MAX]
        done.append(f"icons(움직이는 이모지 {len(cfg.icons)}개)")
    elif a.get("icons"):
        try:
            cfg.icons = cr.clean_icons(esc(str(a["icons"])))
            done.append("icons")
        except ValueError as e:
            bad.append(f"icons: {e}")
    tpl("head", cr.HEAD_FIELDS, cr.HEAD_MAX)
    tpl("foot", (), cr.FOOT_MAX)
    tpl("line", cr.LINE_FIELDS, cr.LINE_MAX, ("이름",))
    tpl("me", cr.ME_FIELDS, cr.LINE_MAX, ("횟수",))
    if a.get("names") in cr.NAME_MODES:
        cfg.names = a["names"]
        done.append("names")
    if isinstance(a.get("extra"), list):        # 한 카드에 같이 보일 추가 기간 (본 기간 제외·유효한 것만)
        seen, ok = set(), []
        for p in a["extra"]:
            if p in cr.PERIOD_CHOICES and p != cfg.period and p not in seen:
                seen.add(p)
                ok.append(p)
        cfg.extra = ok[:cr.EXTRA_MAX]
        done.append("extra")
    for k in ("no_admin", "yday"):
        if isinstance(a.get(k), bool):
            setattr(cfg, k, a[k])
            done.append(k)
    if a.get("reset") in ("now", "undo"):
        if not await can_reset_room(ctx.svc, ctx.bot, cid, uid):   # 전체 초기화는 방 소유자·운영자만 (관리자 남용 방지)
            bad.append("reset: 방 전체 채팅 수 초기화는 방 소유자(방 만든 사람) 또는 소담 운영자만 돼요")
        else:
            cfg.reset_at = int(datetime.now(ctx.svc.cfg.tz).timestamp()) if a["reset"] == "now" else 0
            cr._CACHE.clear()
            done.append("채팅 수 0부터 다시" if cfg.reset_at else "초기화 취소")
    if a.get("award") in AWARD_WORDS:
        cfg.award = AWARD_WORDS[a["award"]]
        if cfg.award >= 0 and not await ctx.svc.paid_features(cid):
            cfg.award = -1
            bad.append("award: 이용 기간이 끝난 방이라 시상식은 못 켬")
        else:
            done.append("award")
    await cr.save(ctx.svc.db, cid, cfg, uid)
    if isinstance(a.get("min_chars"), int):
        await ctx.svc.db.set_setting(cid, "chat_min_chars", max(0, min(50, a["min_chars"])))
        done.append("min_chars")
    if isinstance(a.get("no_repeat"), bool):
        await ctx.svc.db.set_setting(cid, "chat_no_repeat", a["no_repeat"])
        done.append("no_repeat")
    await ctx.svc.db.log_mod(cid, uid, None, "setting", ("chat_rank(AI) " + ",".join(done))[:200])
    out = head + (f"저장함: {', '.join(done)}." if done else "바뀐 것 없음.")
    if bad:
        out += " 못 넣은 것: " + " / ".join(bad) + " — 이유를 짧게 말하고 다시 보내 달라고 할 것."
    if done and ctx.chat_id > 0:                          # 1:1 이면 실제 모양을 바로 보여 줌
        try:
            await send_html(ctx.bot, uid, "👀 <b>미리보기</b>\n\n" + await board(ctx.svc, ctx.bot, cid, me=uid))
            out += " 1:1 에 실제 모양 미리보기를 보냈음 (그대로 맞는지 봐 달라고, 방에 올리려면 '.순위' 또는 🏆 화면 [📣 방에 올리기])."
        except TelegramError:
            pass
    return out + "\n" + _summary(await cr.get(ctx.svc.db, cid), await ctx.svc.db.get_settings(cid))


def _summary(cfg: cr.Cfg, s: dict) -> str:
    return (f"[지금 설정] 기간 {cr.PERIODS[cfg.period][1]}"
            + (f"(+{','.join(cr.PERIODS[p][0] for p in cfg.extra)})" if cfg.extra else "")
            + f" · {cfg.top}등까지 · 상품 {cfg.prizes or '없음'} · "
            f"등수 그림 {len(cfg.icons)}개(없으면 🥇🥈🥉…) · 제목 '{html_plain(cfg.head)[:60]}' · 꼬리말 '{html_plain(cfg.foot)[:80]}' · "
            f"이름 {cr.NAME_MODES[cfg.names]} · 관리자 빼기 {cfg.no_admin} · 지난/어제 당첨자 칸 {cfg.yday} · 시상식 {_hour(cfg.award)} · "
            f"{int(s.get('chat_min_chars') or 0)}글자 이상만(0=전부) · 도배 빼기 {bool(s.get('chat_no_repeat'))} · "
            f"채팅 수 초기화 {'있음' if cfg.reset_at else '없음'}")


def _register_tool() -> None:
    from .. import tools
    from ..permissions import Role
    tools.register_tool(tools.Tool(
        "chat_rank_setup",
        "채팅 순위(.순위·.나·시상식) 모양을 말로 꾸미기 — 관리자만. '1등 5만원…', '5글자 이상만', '매주 월요일 0시 추첨', '제목은 …', "
        "'등수 이모지 🥇 🥈…', '관리자 빼줘'. action=show 지금 설정 / set 바꿀 것만 넣기. 1:1 이면 room(방 이름), 바꾼 뒤 실제 모양 미리보기를 1:1 로 보냄. "
        "메시지에 움직이는 이모지가 있으면 icons_from_message=true (숨은 번호는 코드가 메시지에서 직접 가져감). "
        "제목·꼬리말 칸: {기간} {날짜}. 상품은 보여 주기만 (지급은 관리자가 직접).",
        {"action": {"type": "string", "enum": ["show", "set"]},
         "room": {"type": "string", "description": "1:1 에서 방 이름"},
         "period": {"type": "string", "enum": list(cr.PERIOD_CHOICES),
                    "description": "today 오늘 · yesterday 어제 · thisweek 이번 주(월요일 0시부터, 매주 0부터 다시) · week 최근 7일 · month 최근 30일"},
         "top": {"type": "integer", "description": "몇 등까지 3~20"},
         "prizes": {"type": "array", "items": {"type": "string"}, "description": "1등부터 상품 글 (예: ['50,000원','40,000원'])"},
         "icons": {"type": "string", "description": "보통 이모지 등수 그림, 띄어서 (예: '🥇 🥈 🥉 4️⃣ 5️⃣')"},
         "icons_from_message": {"type": "boolean", "description": "요청 메시지에 움직이는(프리미엄) 이모지가 있으면 true"},
         "head": {"type": "string", "description": "맨 위 제목 (줄바꿈 \\n, 칸 {기간} {날짜}, '없음' = 지우기)"},
         "foot": {"type": "string", "description": "맨 아래 문구 ('없음' = 지우기)"},
         "line": {"type": "string", "description": "한 줄 모양, 칸 {등수} {이름} {횟수} {상품} (이름 필수)"},
         "me": {"type": "string", "description": ".나 한 줄, 칸 {이름} {등수} {횟수} {기간} (횟수 필수)"},
         "names": {"type": "string", "enum": list(cr.NAME_MODES), "description": "user 아이디 · name 이름 · both 이름+아이디 · mask 가리기"},
         "extra": {"type": "array", "items": {"type": "string", "enum": list(cr.PERIOD_CHOICES)},
                   "description": f"한 카드에 본 기간과 같이 보일 추가 기간들 (일간+주간처럼, 최대 {cr.EXTRA_MAX}개, []=없음). 예: period=today 에 extra=['week']"},
         "no_admin": {"type": "boolean", "description": "관리자를 순위에서 빼기"},
         "yday": {"type": "boolean", "description": "어제(주 단위면 지난주) 당첨자 칸"},
         "award": {"type": "string", "enum": list(AWARD_WORDS),
                   "description": "시상식: off · daily0/daily9/daily12 매일(어제 순위) · weekly 매주 월요일 0시(지난 주 순위)"},
         "min_chars": {"type": "integer", "description": "몇 글자 이상만 셀지 (0=전부, 띄어쓰기 빼고)"},
         "no_repeat": {"type": "boolean", "description": "같은 글 10분 안 반복은 1번만"},
         "reset": {"type": "string", "enum": ["now", "undo"],
                   "description": "now = 채팅 수만 지금부터 0부터 다시 (글·기억은 안 지움) · undo = 초기화 취소"}},
        ["action"], t_chat_rank, room_role=Role.ADMIN))


_register_tool()
