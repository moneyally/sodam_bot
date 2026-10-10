"""📅 출석 · 🎟 복권 화면·명령·버튼·AI 도구. 동작은 sodam/lottery.py (설계 docs/SPORTS_ENGAGE_DESIGN.md §3 D).

방: .출석(출첵) · .복권(긁기) · .복권당첨 · .출석순위 — 버튼 lot:s:<사람>(본인만 긁기) · lotp:<방>:<당첨 id>(관리자 1:1 '지급 완료')
허브: m:lot:<방> — 켜기·확률·주간 상한·자격 일수·경품 글(m:in:<방>:lotpz)·최근 당첨
AI 도구 attendance(action checkin/scratch/status/wins/rank) — 말로 '소담아 출석', '복권 긁어줘'.
"""
from __future__ import annotations

import logging
import time

from telegram import Message, ReplyParameters
from telegram.error import Forbidden, TelegramError

from .. import fortune, hooks, lottery, menu, tools
from ..menu import PUBLIC, B, HubItem, PanelCtx, Route, Screen
from ..permissions import Role
from ..util import esc, html_plain, mention
from .greet import _save

log = logging.getLogger(__name__)
NOTIFY_ADMINS = 5

menu.register_toggle("lotto_enabled", "lot")
menu.register_preset("lotto_odds", [("0.5", "0.5%"), ("1.0", "1%"), ("2.0", "2%"), ("5.0", "5%"), ("10.0", "10%")], "lot")
menu.register_preset("lotto_week_max", [("1", "주 1명"), ("2", "주 2명"), ("3", "주 3명"), ("5", "주 5명"), ("10", "주 10명")], "lot")
menu.register_preset("lotto_min_days", [("0", "바로"), ("1", "1일"), ("3", "3일"), ("7", "7일")], "lot")


def _checkin_kb(user_id: int, ticket: bool):
    row = ([B("🎟 복권 긁기", f"lot:s:{user_id}")] if ticket else []) + [B("🔮 오늘의 운세", "lot:f")]
    return menu._kb([row])


async def do_checkin(svc, bot, chat_id: int, user, role: Role) -> tuple[str, object]:
    """출석 처리 → (방에 보낼 글, 버튼). 명령·AI 도구가 같이 씀."""
    s = await svc.db.get_settings(chat_id)
    give, why = False, None
    if s.get("lotto_enabled"):
        if role >= Role.ADMIN:
            why = "관리자는 복권에서 빠져요 (출석은 됐어요)."
        else:
            why = await lottery.eligible(svc.db, chat_id, user.id, int(s.get("lotto_min_days", 3)))
        give = why is None
    r = await lottery.checkin(svc.db, svc.cfg.tz, chat_id, user.id, give_ticket=give)
    name = mention(user.id, user.first_name or "멤버")
    if not r["ok"]:
        text = f"📅 {name}님은 오늘 이미 출석했어요 · 연속 {r['streak']}일 · 이번 달 {r['month']}일"
    else:
        text = f"📅 {name}님 출석! 연속 <b>{r['streak']}일</b> · 이번 달 {r['month']}일"
        if r["ticket"]:
            left = await lottery.week_left(svc.db, svc.cfg.tz, chat_id, int(s["lotto_week_max"]))
            text += (f"\n🎟 복권 1장 받았어요 — 당첨 확률 {s['lotto_odds']:g}% · 경품 {esc(s['lotto_prize'])} · 이번 주 남은 경품 {left}개"
                     if left else "\n🎟 복권 1장 받았어요 — <b>이번 주 경품은 마감</b>이라 지금 긁으면 꽝이에요. 7일 안에 다음 주(월요일~) 긁어도 돼요.")
        elif why:
            text += f"\n<i>{esc(why)}</i>"
    left = await lottery.open_tickets(svc.db, chat_id, user.id) if s.get("lotto_enabled") else 0
    return text, _checkin_kb(user.id, bool(left))


async def do_scratch(svc, bot, chat_id: int, user) -> tuple[str, str, tuple | None]:
    """복권 긁기 → (방에 올릴 글, 결과 종류, 당첨이면 (당첨 id, 경품) — 관리자 알림은 부른 쪽이 응답 뒤에 announce_win)."""
    s = await svc.db.get_settings(chat_id)
    if not s.get("lotto_enabled"):
        return "이 방은 복권이 꺼져 있어요. (관리자: 1:1 메뉴 [🎟 출석·복권])", "off", None
    r = await lottery.scratch(svc.db, svc.cfg.tz, chat_id, user.id, float(s["lotto_odds"]), int(s["lotto_week_max"]),
                              str(s["lotto_prize"]))
    name = mention(user.id, user.first_name or "멤버")
    kind = r["result"]
    if kind == "none":
        return f"🎟 {name}님은 긁을 복권이 없어요. <code>.출석</code> 하면 하루 1장!", kind, None
    if kind == "win":
        return (f"🎉🎉 <b>당첨!</b> {name}님 — 경품 <b>{esc(s['lotto_prize'])}</b>\n"
                f"관리자가 1:1 로 연락드려요. (이번 주 남은 경품 {r['week_left']}개)"), kind, (r["win_id"], str(s["lotto_prize"]))
    if kind == "recent":
        return f"🎟 {name}님 당첨 칸이었는데 최근 {lottery.WIN_GAP_DAYS // 7}주 안에 이미 당첨되셔서 다른 분께 양보했어요 🙏", kind, None
    if kind == "soldout":
        return f"🎟 {name}님 … 당첨 칸이었는데 <b>이번 주 경품이 다 나갔어요</b> 😭 다음 주 월요일에 다시 열려요.", kind, None
    return f"🎟 {name}님 꽝! 내일 또 출석하면 1장 더 (남은 복권 {r['left']}장 · 확률 {s['lotto_odds']:g}%)", kind, None


async def _post(bot, chat_id: int, text: str, reply_to: int | None, **kw) -> bool:
    """방에 올림 — 답장할 글이 지워졌어도 보냄 (긁은 결과가 사라지면 안 됨)."""
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_parameters=rp, **kw)
        return True
    except TelegramError as e:
        log.warning("출석·복권 글 보내기 실패 %s: %s", chat_id, e)
        return False


async def announce_win(svc, bot, chat_id: int, user, win: tuple | None) -> None:
    """당첨 → 관리자 1:1. 아무에게도 못 보냈으면 방에 한 줄 (관리자 전원이 1:1 을 안 연 경우)."""
    if not win:
        return
    if not await _notify_admins(svc, bot, chat_id, user, win[0], win[1]):
        await _post(bot, chat_id, f"📮 관리자님, 당첨 알림을 1:1 로 못 보냈어요 → @{bot.username} 와 1:1 을 열고 "
                                  "방 설정 [🎟 출석·복권] 에서 당첨자를 확인해 주세요.", None)


async def _notify_admins(svc, bot, chat_id: int, user, win_id: int, prize: str) -> int:
    title = ""
    try:
        chat = await svc.db._one("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
        title = (chat["title"] if chat else "") or ""
    except Exception:
        pass
    try:
        admins = [u.id for u in await svc.perms.admin_users(bot, chat_id) if not getattr(u, "is_bot", False)][:NOTIFY_ADMINS]
    except TelegramError:
        admins = []
    who = f"{esc(user.first_name or '?')}" + (f" @{esc(user.username)}" if getattr(user, "username", None) else "") + f" (ID {user.id})"
    sent = 0
    for aid in admins:
        try:
            await bot.send_message(aid, f"🎁 <b>복권 당첨</b> — {esc(title) or chat_id}\n당첨자: {who}\n경품: {esc(prize)}\n"
                                        "경품을 보낸 뒤 아래 버튼을 눌러 주세요.", parse_mode="HTML",
                                   reply_markup=menu._kb([[B("✅ 지급 완료", f"lotp:{chat_id}:{win_id}")]]))
            sent += 1
        except Forbidden:
            continue
        except TelegramError as e:
            log.warning("복권 당첨 관리자 알림 실패 %s: %s", aid, e)
    return sent


async def on_button(svc, bot, q, parts) -> None:
    """lot:s:<사람> — 본인만 긁기 · lot:f — 누른 사람 운세 (팝업, 방에 안 올림)."""
    if parts[:1] == ["f"]:
        await q.answer(fortune.short(fortune.today(svc.cfg.tz, q.from_user.id)), show_alert=True)
        return
    if len(parts) < 2 or parts[0] != "s" or not q.message:
        await q.answer()
        return
    if str(q.from_user.id) != parts[1]:
        await q.answer("본인 복권만 긁을 수 있어요. .출석 하면 1장!", show_alert=True)
        return
    chat_id = q.message.chat.id
    text, kind, win = await do_scratch(svc, bot, chat_id, q.from_user)
    if kind in ("none", "off"):                       # 방에 안 올림 (복권 없는 사람이 연타하면 도배)
        await q.answer(html_plain(text)[:200], show_alert=True)
        return
    await q.answer("🎟")
    if not await _post(bot, chat_id, text, q.message.message_id):
        try:
            await q.answer(html_plain(text)[:200], show_alert=True)
        except TelegramError:
            pass
    await announce_win(svc, bot, chat_id, q.from_user, win)


async def on_paid(svc, bot, q, parts) -> None:
    """lotp:<방>:<당첨 id> — 그 방 텔레그램 관리자만."""
    try:
        chat_id, win_id = int(parts[0]), int(parts[1])
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
    ok = await lottery.mark_paid(svc.db, chat_id, win_id, q.from_user.id)
    await q.answer("✅ 지급 완료로 표시했어요" if ok else "이미 지급 완료예요")
    if ok:
        try:
            await q.edit_message_text((getattr(q.message, "text_html", None) or "🎁 복권 당첨") + "\n\n✅ 지급 완료", parse_mode="HTML")
        except (TelegramError, AttributeError):
            pass


async def wins_text(svc, chat_id: int) -> str:
    rows = await lottery.wins(svc.db, chat_id)
    if not rows:
        return "🎁 아직 복권 당첨자가 없어요."
    tz = svc.cfg.tz
    from datetime import datetime
    return "🎁 <b>최근 복권 당첨</b>\n" + "\n".join(
        f"• {datetime.fromtimestamp(r['created'], tz).strftime('%m/%d')} {esc(r['name'])} — {esc(r['prize'])} "
        f"{'✅ 지급' if r['paid'] else '⏳ 지급 전'}" for r in rows)


async def rank_text(svc, chat_id: int) -> str:
    rows = await lottery.month_rank(svc.db, svc.cfg.tz, chat_id)
    if not rows:
        return "📅 이번 달 출석 기록이 아직 없어요. <code>.출석</code>!"
    return "📅 <b>이번 달 출석 순위</b>\n" + "\n".join(f"{i}. {esc(r['name'])} — {r['n']}일 (최고 연속 {r['best']}일)"
                                                     for i, r in enumerate(rows, 1))


# ── 명령 ──────────────────────────────────────────────────
async def c_checkin(ctx) -> None:
    text, kb = await do_checkin(ctx.svc, ctx.bot, ctx.chat_id, ctx.user, ctx.role)
    await _post(ctx.bot, ctx.chat_id, text, ctx.msg.message_id, reply_markup=kb)


async def c_scratch(ctx) -> None:
    text, _, win = await do_scratch(ctx.svc, ctx.bot, ctx.chat_id, ctx.user)
    await _post(ctx.bot, ctx.chat_id, text, ctx.msg.message_id)
    await announce_win(ctx.svc, ctx.bot, ctx.chat_id, ctx.user, win)


async def c_fortune(ctx) -> None:
    await ctx.reply(fortune.text(fortune.today(ctx.svc.cfg.tz, ctx.user.id), mention(ctx.user.id, ctx.user.first_name or "멤버")))


async def c_wins(ctx) -> None:
    await ctx.reply(await wins_text(ctx.svc, ctx.chat_id))


async def c_rank(ctx) -> None:
    await ctx.reply(await rank_text(ctx.svc, ctx.chat_id))


def register_commands() -> None:
    from .. import commands
    for cmd in (commands.Cmd(("출석", "출첵", "attend"), c_checkin, help="하루 한 번 출석 (복권 켠 방은 복권 1장)", group="출석·복권"),
                commands.Cmd(("복권", "긁기", "복권긁기"), c_scratch, help="내 복권 긁기", group="출석·복권"),
                commands.Cmd(("운세", "오늘운세", "오늘의운세", "fortune"), c_fortune, help="오늘의 운세 (하루 동안 같음, 재미로)",
                             group="출석·복권", dm_ok=True),
                commands.Cmd(("복권당첨", "당첨자"), c_wins, help="최근 복권 당첨", group="출석·복권"),
                commands.Cmd(("출석순위", "출석랭킹"), c_rank, help="이번 달 출석 순위", group="출석·복권")):
        if any(c.fn is cmd.fn for c in commands.COMMANDS):
            continue
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)


# ── 관리자 화면 ───────────────────────────────────────────
async def s_lot(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    week = await c.svc.db._one("SELECT COUNT(*) n FROM lotto_wins WHERE chat_id=? AND week=?",
                               (c.cid, lottery.week_start(c.svc.cfg.tz, time.time())))
    lines = ["🎟 <b>출석 · 복권</b>",
             "멤버가 <code>.출석</code> 하면 하루 1장 복권을 받고 [🎟 복권 긁기]로 바로 긁어요. 당첨되면 방에 축하 + 관리자 1:1 로 알림이 가고, "
             "<b>경품은 관리자가 직접</b> 보내 주면 돼요(돈·포인트 걸기 없음).",
             "", f"지금: <b>{'✅ 켜짐' if s['lotto_enabled'] else '❌ 꺼짐 (출석만 됨)'}</b>",
             f"확률 <b>{s['lotto_odds']:g}%</b> · 주간 당첨 상한 <b>{s['lotto_week_max']}명</b> (이번 주 {week['n'] if week else 0}명) · "
             f"자격: 들어온 지 {s['lotto_min_days']}일↑ + 최근 7일 대화",
             f"경품: <b>{esc(s['lotto_prize'])}</b>", "", await wins_text(c.svc, c.cid)]
    rows = menu._toggle_rows(s, c.cid, ["lotto_enabled"]) + [
        menu._preset_row(s, c.cid, "lotto_odds"), menu._preset_row(s, c.cid, "lotto_week_max"),
        menu._preset_row(s, c.cid, "lotto_min_days"), [B("✏️ 경품 글 바꾸기", f"m:in:{c.cid}:lotpz")], menu._back(c.cid)]
    return Screen("\n".join(lines)[:3900], menu._kb(rows))


async def in_prize(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    try:
        v = lottery._prize(msg.text or "")
    except ValueError as e:
        return False, f"❌ {e}"
    await _save(c, "lotto_prize", lotto_prize=v)
    return True, f"✅ 경품을 <b>{esc(v)}</b>(으)로 정했어요."


async def s_fortune(c: PanelCtx) -> Screen:
    """1:1 🔮 (스포츠 처음 화면 버튼)."""
    return Screen(fortune.text(fortune.today(c.svc.cfg.tz, c.uid)), menu._kb([[B("⬅️ 스포츠", "m:sx"), B("⬅️ 처음으로", "m:home")]]))


menu.register_route("fx", Route(s_fortune, PUBLIC, scoped=False))
menu.register_hub(HubItem(53, "lot", "🎟 출석·복권"))
menu.register_screen("lot", s_lot)
menu.register_screen("lotpz", s_lot)
menu.register_input("lotpz", "🎁 경품을 글로 보내 주세요 (40자 안, 링크 X).\n예: <code>커피 기프티콘</code> · <code>배민 1만원 쿠폰</code>",
                    "lot", in_prize, s_lot)
hooks.add_callback_handler("lot", on_button)
hooks.add_callback_handler("lotp", on_paid)
_pruned = [0.0]


async def _prune_tick(svc, bot) -> None:
    now = time.time()
    if now - _pruned[0] >= 3600:
        _pruned[0] = now
        await lottery.prune(svc.db, now)


hooks.add_tick_hook(_prune_tick)


# ── AI 도구 ───────────────────────────────────────────────
async def t_attendance(ctx: tools.ToolCtx, a: dict) -> str:
    action = str(a.get("action") or "checkin")
    reply_to = getattr(ctx.request_msg, "message_id", None)
    if action == "fortune":     # 1:1 에서도 됨
        f = fortune.today(ctx.svc.cfg.tz, ctx.caller.id)
        if not await _post(ctx.bot, ctx.chat_id, fortune.text(f, mention(ctx.caller.id, ctx.caller.first_name or "멤버")), reply_to):
            return fortune.text(f)
        ctx.quiet = True
        return "운세를 방에 올렸음 (하루 동안 같음)."
    if ctx.chat_id > 0:
        return "출석·복권은 그룹방에서만 됨."
    if action == "checkin":
        text, kb = await do_checkin(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller, ctx.role)
        if not await _post(ctx.bot, ctx.chat_id, text, reply_to, reply_markup=kb):
            return "출석 처리함: " + text
        ctx.quiet = True
        return "출석 처리하고 결과(버튼 포함)를 방에 올렸음."
    if action == "scratch":
        text, _, win = await do_scratch(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller)
        ok = await _post(ctx.bot, ctx.chat_id, text, reply_to)
        await announce_win(ctx.svc, ctx.bot, ctx.chat_id, ctx.caller, win)
        if not ok:
            return text
        ctx.quiet = True
        return "복권 결과를 방에 올렸음."
    if action == "wins":
        return await wins_text(ctx.svc, ctx.chat_id)
    if action == "rank":
        return await rank_text(ctx.svc, ctx.chat_id)
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    left = await lottery.open_tickets(ctx.svc.db, ctx.chat_id, ctx.caller.id)
    return (f"복권 {'켜짐' if s['lotto_enabled'] else '꺼짐'} · 확률 {s['lotto_odds']:g}% · 주 {s['lotto_week_max']}명 · "
            f"경품 {s['lotto_prize']} · 말한 사람 남은 복권 {left}장 (출석은 .출석 또는 이 도구 checkin)")


tools.register_tool(tools.Tool(
    "attendance", "출석·복권·운세: '출석', '출첵', '복권 긁어', '당첨자', '출석 순위', '복권 몇 장 남았어', '오늘 운세'. 말한 사람 것만. "
                  "checkin=출석(복권 켠 방은 복권 1장+긁기 버튼) · scratch=복권 긁기 · status=설정·남은 복권 · wins=최근 당첨 · "
                  "rank=이번 달 출석 순위 (여기까지 그룹방) · fortune=오늘의 운세(1:1 도 됨, 하루 동안 같음). "
                  "경품은 관리자가 직접 줌 (소담이 경품·돈을 주지 않음).",
    {"action": {"type": "string", "enum": ["checkin", "scratch", "status", "wins", "rank", "fortune"]}}, ["action"], t_attendance),
    read_only=False)
