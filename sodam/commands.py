"""명령어. `.명령어` 와 `/command` 둘 다 받는다. 한글/영어 별칭 지원."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from telegram.error import TelegramError

from . import knowledge, menu, namehist, stats, subscription
from .permissions import Role
from .services import Services
from .security import normalize_domain
from .settings import DEFAULTS, LABELS, coerce, render
from .sports import SportsError
from .styles import STYLES, resolve_style, style_list
from .games import GAME_LIST
from .util import (_DURATION, display_name, esc, fmt_time, human_minutes, iyeyo, mention, parse_duration, to_int,
                   user_name)

log = logging.getLogger(__name__)


@dataclass
class CmdCtx:
    svc: Services
    bot: Bot
    msg: Message
    chat_id: int
    user: User
    role: Role
    args: list[str]
    argstr: str

    async def reply(self, text: str, **kw) -> Message:
        return await self.msg.reply_text(text, parse_mode="HTML", **kw)


@dataclass
class Cmd:
    names: tuple[str, ...]
    fn: Callable[[CmdCtx], Awaitable[None]]
    role: Role = Role.MEMBER
    usage: str = ""
    help: str = ""
    group: str = "일반"
    dm_ok: bool = False  # 1:1 채팅에서도 쓸 수 있는 명령


async def _target(ctx: CmdCtx, *, sanction: bool = True) -> tuple[int, str, list[str]] | None:
    """답장 대상 또는 첫 인자(@username/ID/이름)로 대상 찾기. (id, 이름, 남은 인자)"""
    reply = ctx.msg.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        uid, name, rest = reply.from_user.id, user_name(reply.from_user), ctx.args
    elif ctx.args:
        rows = await ctx.svc.db.find_members(ctx.chat_id, ctx.args[0])
        if not rows:
            await ctx.reply(f"'{esc(ctx.args[0])}' 멤버를 못 찾았어요. 답장으로 지정하거나 @username/ID를 써주세요.")
            return None
        if len(rows) > 1:
            await ctx.reply("같은 이름이 여러 명이에요: " + ", ".join(
                f"{esc(display_name(r['first_name'], r['last_name'], r['username']))}(<code>{r['user_id']}</code>)" for r in rows[:5]))
            return None
        r = rows[0]
        uid, name, rest = r["user_id"], display_name(r["first_name"], r["last_name"], r["username"]), ctx.args[1:]
    else:
        await ctx.reply("대상 메시지에 답장하거나 @username 을 적어주세요.")
        return None
    if sanction and await ctx.svc.perms.protected(ctx.bot, ctx.chat_id, uid):
        await ctx.reply("관리자나 봇은 제재할 수 없어요.")
        return None
    return uid, name, rest


async def _private_notice(ctx: CmdCtx, text: str, reply_markup=None, seconds: int = 15) -> None:
    """관리자용 명령(.구독/.설정)은 방에 흔적이 남지 않게: 명령 메시지는 지우고 안내는 잠깐만 보였다 삭제."""
    try:
        await ctx.msg.delete()
    except (TelegramError, AttributeError):
        pass
    try:
        sent = await ctx.bot.send_message(ctx.chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramError:
        return

    async def _later():
        await asyncio.sleep(seconds)
        try:
            await ctx.bot.delete_message(ctx.chat_id, sent.message_id)
        except TelegramError:
            pass

    task = asyncio.create_task(_later())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set = set()


async def _safe(ctx: CmdCtx, coro, ok_text: str) -> None:
    try:
        await coro
        await ctx.reply(ok_text)
    except TelegramError as e:
        await ctx.reply(f"실패했어요: {esc(e.message)}\n(봇에게 관리자 권한이 있는지 확인해주세요)")


# ── 일반 명령 ─────────────────────────────────────────────
async def c_help(ctx: CmdCtx) -> None:
    groups: dict[str, list[str]] = {}
    in_dm = ctx.chat_id > 0
    for cmd in COMMANDS:
        if cmd.role > ctx.role or (in_dm and not cmd.dm_ok):
            continue
        if cmd.fn is c_subscribe and not in_dm and ctx.role < Role.ADMIN:
            continue  # 결제 관련은 방의 일반 멤버에게 보이지 않게
        line = f"<code>.{cmd.names[0]}</code>{(' ' + esc(cmd.usage)) if cmd.usage else ''} — {esc(cmd.help)}"
        groups.setdefault(cmd.group, []).append(line)
    parts = [f"🤖 <b>{esc(ctx.svc.cfg.bot_name)}</b> 명령어 (. 또는 / 로 시작)",
             f"AI와 대화: <code>{esc(ctx.svc.cfg.call_names[-1])}아 …</code> 또는 봇 메시지에 답장"]
    for group, lines in groups.items():
        parts.append(f"\n<b>[{group}]</b>\n" + "\n".join(lines))
    await ctx.reply("\n".join(parts))


async def c_rules(ctx: CmdCtx) -> None:
    rules = (await ctx.svc.db.get_settings(ctx.chat_id))["rules"]
    await ctx.reply(f"📜 <b>방 규칙</b>\n{esc(rules)}" if rules else "등록된 규칙이 없어요.")


async def c_me(ctx: CmdCtx) -> None:
    if ctx.args or ctx.msg.reply_to_message:
        t = await _target(ctx, sanction=False)
        if not t:
            return
        uid, name, _ = t
    else:
        uid, name = ctx.user.id, user_name(ctx.user)
    db, tz = ctx.svc.db, ctx.svc.cfg.tz
    m = await db.get_member(ctx.chat_id, uid)
    if not m:
        await ctx.reply("기록이 아직 없어요.")
        return
    total = await db.user_message_count(ctx.chat_id, uid)
    lines = [f"👤 <b>{esc(name)}</b> (<code>{uid}</code>)",
             f"메시지 {total}개 · 포인트 {m['points']}점"]
    if m["joined_at"]:
        lines.append(f"입장: {fmt_time(m['joined_at'], tz, '%Y-%m-%d')}")
    style = STYLES.get(m["style"] or "")
    if style:
        lines.append(f"봇 말투: {style.label}")
    if ctx.role >= Role.ADMIN or uid == ctx.user.id:
        lines.append(f"경고: {await db.warning_count(ctx.chat_id, uid)}회")
    await ctx.reply("\n".join(lines))


async def _name_lookup(ctx: CmdCtx, mode: str, *, self_only: bool = False) -> None:
    """이름·아이디 변경 기록. 대상: 답장 > 인자(@아이디·ID·이름) > 전달된 메시지 > 나.
    권한은 namehist.can_view (지금은 누구나 전부)."""
    db, tz = ctx.svc.db, ctx.svc.cfg.tz
    group = ctx.chat_id < 0
    uid: int | None = ctx.user.id
    reply = ctx.msg.reply_to_message
    if self_only:
        pass
    elif reply is not None and getattr(reply, "forward_origin", None) is not None:
        uid, err = namehist.forwarded_user(reply)
        if uid is None:
            await ctx.reply(err)
            return
    elif reply is not None and reply.from_user:
        if reply.from_user.is_bot:
            await ctx.reply("봇 계정은 기록하지 않아요.")
            return
        uid = reply.from_user.id
    elif ctx.args:
        uid = await namehist.resolve(db, ctx.args[0], ctx.chat_id if group else None)
        if uid is None and group:
            rows = await db.find_members(ctx.chat_id, ctx.argstr)
            uid = rows[0]["user_id"] if len(rows) == 1 else None
        if uid is None:
            await ctx.reply(f"'{esc(ctx.argstr[:40])}' 기록을 못 찾았어요. @아이디(예전 아이디도 됨)·숫자 ID·답장으로 해주세요.")
            return
    elif getattr(ctx.msg, "forward_origin", None) is not None:
        uid, err = namehist.forwarded_user(ctx.msg)
        if uid is None:
            await ctx.reply(err)
            return
    if not await namehist.can_view(db, ctx.user.id, uid, ctx.chat_id, ctx.role >= Role.OWNER):
        await ctx.reply("🔒 볼 수 없는 기록이에요.")
        return
    title = "내 이름 기록" if uid == ctx.user.id else None
    await ctx.reply(await namehist.history_text(db, uid, tz, mode=mode, title=title),
                    reply_markup=namehist.buttons(uid, mode, ctx.bot.username))


async def name_lookup_forward(ctx: CmdCtx, mode: str = "recent") -> None:
    """1:1 에 전달된 메시지 → 원래 보낸 사람의 기록 / @아이디·ID 만 보낸 경우 → 그 사람 기록."""
    await _name_lookup(ctx, mode)


async def c_history(ctx: CmdCtx) -> None:
    await _name_lookup(ctx, "recent")


async def c_allhistory(ctx: CmdCtx) -> None:
    await _name_lookup(ctx, "all")


async def c_check_name(ctx: CmdCtx) -> None:
    await _name_lookup(ctx, "names")


async def c_check_username(ctx: CmdCtx) -> None:
    await _name_lookup(ctx, "usernames")


async def c_myhistory(ctx: CmdCtx) -> None:
    await _name_lookup(ctx, "recent", self_only=True)


async def c_name_notice(ctx: CmdCtx) -> None:
    sub = ctx.args[0] if ctx.args else ""
    if sub in ("켜기", "on", "끄기", "off"):
        await ctx.svc.db.set_setting(ctx.chat_id, "name_change_notice", coerce("name_change_notice", sub))
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, None, "setting", f"name_change_notice={sub}")
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    await ctx.reply(f"🔄 <b>이름 변경 알림</b>: {render('name_change_notice', s['name_change_notice'])}\n"
                    "멤버가 이름·@아이디를 바꾸면 방에 알려요. <code>.이름알림 켜기|끄기</code>")


async def c_rank(ctx: CmdCtx) -> None:
    period = ctx.args[0] if ctx.args else "오늘"
    await ctx.reply(await stats.ranking_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, period))


async def c_stats(ctx: CmdCtx) -> None:
    period = ctx.args[0] if ctx.args else "오늘"
    await ctx.reply(await stats.summary_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, period))


async def c_search(ctx: CmdCtx) -> None:
    if len(ctx.argstr) < 2:
        await ctx.reply("사용법: <code>.검색 키워드</code>")
        return
    await ctx.reply(await stats.search_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, ctx.argstr[:30], days=30))


async def c_points(ctx: CmdCtx) -> None:
    rows = await ctx.svc.db.top_points(ctx.chat_id, 10)
    if not rows:
        await ctx.reply("아직 포인트가 없어요. <code>.게임</code> 으로 시작해보세요!")
        return
    lines = ["🏆 <b>게임 포인트 랭킹</b>"]
    lines += [f"{stats.MEDALS[i] if i < 3 else str(i + 1) + '.'} {esc(display_name(r['first_name'], None, r['username']))} {r['points']}점"
              for i, r in enumerate(rows)]
    await ctx.reply("\n".join(lines))


async def c_game(ctx: CmdCtx) -> None:
    if not ctx.args:
        await ctx.reply(f"🔗 말 게임: {GAME_LIST} (<code>.게임 끝말잇기</code>)\n\n"
                        "🎰 <b>포인트 게임</b>: 홀짝·슬롯·바카라·블랙잭·그래프·경마…\n"
                        "<code>!가입</code> 후 <code>!도움</code> 으로 전체 목록")
        return
    await ctx.reply(esc(await ctx.svc.games.start(ctx.bot, ctx.chat_id, ctx.user.id, ctx.args[0])))


async def c_stop_game(ctx: CmdCtx) -> None:
    game = ctx.svc.games.active.get(ctx.chat_id)
    if not game:
        await ctx.reply("진행 중인 게임이 없어요.")
        return
    if game.starter_id != ctx.user.id and ctx.role < Role.ADMIN:
        await ctx.reply("게임을 시작한 분이나 관리자만 끝낼 수 있어요.")
        return
    await ctx.svc.games.stop(ctx.chat_id)


async def c_style(ctx: CmdCtx) -> None:
    if not ctx.args:
        m = await ctx.svc.db.get_member(ctx.chat_id, ctx.user.id)
        current = STYLES.get((m and m["style"]) or "")
        await ctx.reply(f"🎭 지금 말투: {current.label if current else '방 기본'}\n"
                        f"바꾸기: <code>.말투 종류</code> ({style_list()}) / 방 기본으로: <code>.말투 기본</code>")
        return
    if ctx.args[0] in ("기본", "초기화", "reset"):
        await ctx.svc.db.set_member_style(ctx.chat_id, ctx.user.id, None)
        await ctx.reply("말투를 방 기본으로 돌렸어요.")
        return
    style = resolve_style(ctx.args[0])
    if not style:
        await ctx.reply(f"말투는 {style_list()} 중에서 골라주세요.")
        return
    await ctx.svc.db.set_member_style(ctx.chat_id, ctx.user.id, style)
    await ctx.reply(f"🎭 이제 대표님께는 <b>{STYLES[style].label}</b> 말투로 대답할게요!")


async def c_nickname(ctx: CmdCtx) -> None:
    value = ctx.argstr.replace("\n", " ")[:20]
    await ctx.svc.db.set_member_note(ctx.chat_id, ctx.user.id, "호칭", value)
    await ctx.reply(f"앞으로 <b>{esc(value)}</b>(으)로 불러드릴게요." if value else "호칭을 지웠어요.")


async def c_sports(ctx: CmdCtx) -> None:
    sp, db = ctx.svc.sports, ctx.svc.db
    sub = ctx.args[0] if ctx.args else "오늘"
    rest = " ".join(ctx.args[1:])
    try:
        if sub in ("오늘", "today"):
            await ctx.reply(await sp.today_text(rest or "축구"))
        elif sub in ("팀", "다음", "next"):
            await ctx.reply(await sp.team_text(rest, "next") if rest else "사용법: <code>.스포츠 팀 Tottenham</code>")
        elif sub in ("결과", "last"):
            await ctx.reply(await sp.team_text(rest, "last") if rest else "사용법: <code>.스포츠 결과 Tottenham</code>")
        elif sub in ("구독", "알림", "sub"):
            if ctx.role < Role.ADMIN:
                await ctx.reply("알림 구독은 관리자만 설정할 수 있어요.")
                return
            team = await sp.find_team(rest) if rest else None
            if not team:
                await ctx.reply("팀을 못 찾았어요. 영어 팀명으로 해주세요 (예: <code>.스포츠 구독 Tottenham</code>)")
                return
            await db.set_sports_sub(ctx.chat_id, team["idTeam"], team["strTeam"], True)
            await ctx.reply(f"🔔 {esc(team['strTeam'])} 경기 시작 전·결과 알림을 켰어요.")
        elif sub in ("해제", "unsub"):
            if ctx.role < Role.ADMIN:
                await ctx.reply("알림 해제는 관리자만 할 수 있어요.")
                return
            for s in await db.sports_subs(ctx.chat_id):
                if s["name"].lower() == rest.lower():
                    await db.set_sports_sub(ctx.chat_id, s["team_id"], s["name"], False)
                    await ctx.reply(f"🔕 {esc(s['name'])} 알림을 껐어요.")
                    return
            await ctx.reply("구독 중인 팀이 아니에요. <code>.스포츠 목록</code> 으로 확인해보세요.")
        elif sub in ("목록", "list"):
            subs = await db.sports_subs(ctx.chat_id)
            await ctx.reply("🔔 알림 팀: " + (", ".join(esc(s["name"]) for s in subs) if subs else "없음"))
        else:
            await ctx.reply(await sp.today_text(sub))
    except SportsError as e:
        await ctx.reply(esc(str(e)))


async def c_about(ctx: CmdCtx) -> None:
    name = ctx.svc.cfg.bot_name
    await ctx.reply(f"🤖 저는 이 소통방 AI 비서 <b>{esc(name)}</b>{iyeyo(name)[len(name):]}.\n"
                    "인사, 질문 답변, 채팅 집계, 게임, 스포츠 알림, 방 관리를 도와드려요. <code>.도움말</code>")


# ── 관리자 명령 ───────────────────────────────────────────
async def c_members(ctx: CmdCtx) -> None:
    """👥 멤버 목록을 관리자 1:1 로 (방에 명단을 뿌리지 않게)."""
    from .panels.members import s_members
    screen = await s_members(menu.PanelCtx(ctx.svc, ctx.bot, ctx.user.id, ctx.chat_id, []))
    if screen.text is None:
        await ctx.reply(screen.toast or "잠시 후 다시 해주세요.")
        return
    try:
        await menu.send_panel(ctx.svc, ctx.bot, ctx.user.id, lambda: ctx.bot.send_message(
            ctx.user.id, screen.text, parse_mode="HTML", reply_markup=screen.kb))
        await _private_notice(ctx, "🔒 관리자님, 1:1 채팅에서 멤버 목록을 확인해주세요.")
    except TelegramError:
        await _private_notice(ctx, "관리자님, 먼저 봇과 1:1 대화를 시작해주세요.",
                              InlineKeyboardMarkup([[InlineKeyboardButton(
                                  "👥 열기", url=f"https://t.me/{ctx.bot.username}?start=cfg_{ctx.chat_id}")]]), seconds=60)


async def c_settings(ctx: CmdCtx) -> None:
    """그룹헬프처럼 버튼 설정 패널을 관리자 1:1 로 보낸다. '.설정 전체' 는 글 목록."""
    if not (ctx.args and ctx.args[0] in ("전체", "all", "목록")):
        text, kb = await menu.group_panel(ctx.svc, ctx.bot, ctx.chat_id, ctx.user.id)
        try:
            await menu.send_panel(ctx.svc, ctx.bot, ctx.user.id,
                                  lambda: ctx.bot.send_message(ctx.user.id, text, parse_mode="HTML", reply_markup=kb))
            await _private_notice(ctx, "🔒 관리자님, 봇과의 1:1 채팅에서 설정 메뉴를 확인해주세요.")
        except TelegramError:
            await _private_notice(ctx, "관리자님, 아래 버튼으로 설정 메뉴를 열어주세요.",
                                  InlineKeyboardMarkup([[InlineKeyboardButton(
                                      "⚙️ 설정 열기", url=f"https://t.me/{ctx.bot.username}?start=cfg_{ctx.chat_id}")]]),
                                  seconds=60)
        return
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    lines = ["⚙️ <b>방 설정</b> (바꾸기: <code>.set 키 값</code>)"]
    for key in DEFAULTS:
        lines.append(f"<code>{key}</code> {esc(LABELS.get(key, ''))}: {esc(render(key, s[key]))}")
    await ctx.reply("\n".join(lines))


async def c_set(ctx: CmdCtx) -> None:
    if len(ctx.args) < 2:
        await ctx.reply("사용법: <code>.set 키 값</code> (예: <code>.set flood_count 5</code>, <code>.set style 자유분방</code>)\n"
                        "키 목록은 <code>.settings</code>")
        return
    key, raw = ctx.args[0], ctx.argstr[len(ctx.args[0]):].strip()
    try:
        value = coerce(key, raw)
    except ValueError as e:
        await ctx.reply(f"❌ {esc(str(e))}")
        return
    await ctx.svc.db.set_setting(ctx.chat_id, key, value)
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, None, "setting", f"{key}={value}")
    await ctx.reply(f"✅ {esc(LABELS.get(key, key))} → {esc(render(key, value))}")


async def c_warn(ctx: CmdCtx) -> None:
    t = await _target(ctx)
    if t:
        uid, name, rest = t
        await ctx.reply(await ctx.svc.mod.warn(ctx.bot, ctx.chat_id, uid, name, ctx.user.id, " ".join(rest) or "관리자 경고"))


async def c_unwarn(ctx: CmdCtx) -> None:
    t = await _target(ctx, sanction=False)
    if t:
        uid, name, _ = t
        await ctx.svc.db.remove_last_warning(ctx.chat_id, uid)
        count = await ctx.svc.db.warning_count(ctx.chat_id, uid)
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, uid, "unwarn")
        await ctx.reply(f"경고 1회 취소했어요. {esc(name)}님 현재 {count}회")


async def c_warns(ctx: CmdCtx) -> None:
    t = await _target(ctx, sanction=False)
    if t:
        uid, name, _ = t
        rows = await ctx.svc.db.list_warnings(ctx.chat_id, uid)
        if not rows:
            await ctx.reply(f"{esc(name)}님은 경고가 없어요.")
            return
        lines = [f"⚠️ {esc(name)}님 경고 {len(rows)}회"]
        lines += [f"- {fmt_time(r['ts'], ctx.svc.cfg.tz)} {esc(r['reason'] or '')}" for r in rows]
        await ctx.reply("\n".join(lines))


async def c_resetwarns(ctx: CmdCtx) -> None:
    t = await _target(ctx, sanction=False)
    if t:
        uid, name, _ = t
        await ctx.svc.db.clear_warnings(ctx.chat_id, uid)
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, uid, "resetwarns")
        await ctx.reply(f"{esc(name)}님 경고를 모두 지웠어요.")


async def c_mute(ctx: CmdCtx) -> None:
    t = await _target(ctx)
    if not t:
        return
    uid, name, rest = t
    minutes = parse_duration(rest[0]) if rest else None
    if minutes is None and rest and _DURATION.match(rest[0]):  # 시간 형식인데 366일 초과
        await ctx.reply("뮤트 시간은 최대 366일까지예요.")
        return
    reason = " ".join(rest[1:] if minutes else rest) or "관리자 판단"
    minutes = minutes or 30
    await _safe(ctx, ctx.svc.mod.mute(ctx.bot, ctx.chat_id, uid, minutes, ctx.user.id, reason),
                f"🔇 {mention(uid, name)}님 {human_minutes(minutes)} 채팅 금지")


async def c_unmute(ctx: CmdCtx) -> None:
    t = await _target(ctx, sanction=False)
    if t:
        uid, name, _ = t
        await _safe(ctx, ctx.svc.mod.unmute(ctx.bot, ctx.chat_id, uid, ctx.user.id), f"🔊 {mention(uid, name)}님 채팅 금지 해제")


async def c_ban(ctx: CmdCtx) -> None:
    t = await _target(ctx)
    if t:
        uid, name, rest = t
        await _safe(ctx, ctx.svc.mod.ban(ctx.bot, ctx.chat_id, uid, ctx.user.id, " ".join(rest) or "관리자 판단"),
                    f"🚫 {mention(uid, name)}님을 내보냈어요.")


async def c_unban(ctx: CmdCtx) -> None:
    # 밴된 사람은 이름 검색이 안 될 수 있어서 숫자 ID도 바로 받는다
    arg = ctx.args[0] if ctx.args else ""
    if (uid := to_int(arg)) is not None and uid > 0:
        name = arg
    else:
        t = await _target(ctx, sanction=False)
        if not t:
            return
        uid, name = t[0], t[1]
    await _safe(ctx, ctx.svc.mod.unban(ctx.bot, ctx.chat_id, uid, ctx.user.id), f"✅ {esc(name)}님 밴 해제 (다시 들어올 수 있어요)")


async def c_kick(ctx: CmdCtx) -> None:
    t = await _target(ctx)
    if t:
        uid, name, rest = t
        await _safe(ctx, ctx.svc.mod.kick(ctx.bot, ctx.chat_id, uid, ctx.user.id, " ".join(rest) or "관리자 판단"),
                    f"👢 {mention(uid, name)}님을 내보냈어요 (다시 들어올 수 있어요).")


async def c_del(ctx: CmdCtx) -> None:
    reply = ctx.msg.reply_to_message
    if not reply:
        await ctx.reply("지울 메시지에 답장하면서 <code>.삭제</code> 해주세요.")
        return
    try:
        await ctx.bot.delete_messages(ctx.chat_id, [reply.message_id, ctx.msg.message_id])
    except TelegramError as e:
        await ctx.reply(f"삭제 실패: {esc(e.message)}")


async def c_purge(ctx: CmdCtx) -> None:
    n = (to_int(ctx.args[0]) or 0) if ctx.args else 0
    if not 1 <= n <= 200:
        await ctx.reply("사용법: <code>.청소 개수</code> (1~200, 최근 메시지부터)")
        return
    top = ctx.msg.message_id
    ids = list(range(top, top - n - 1, -1))
    try:
        for i in range(0, len(ids), 100):
            await ctx.bot.delete_messages(ctx.chat_id, ids[i:i + 100])
    except TelegramError as e:
        await ctx.reply(f"일부 삭제 실패: {esc(e.message)} (48시간 지난 메시지는 못 지워요)")
        return
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, None, "purge", str(n))


async def c_lock(ctx: CmdCtx) -> None:
    try:
        done = await ctx.svc.mod.lock(ctx.bot, ctx.chat_id, ctx.user.id)
    except TelegramError as e:
        await ctx.reply(f"실패했어요: {esc(e.message)}")
        return
    await ctx.reply("🔒 방을 잠갔어요. 관리자만 채팅할 수 있어요. (<code>.잠금해제</code>)" if done else "이미 잠겨 있어요.")


async def c_unlock(ctx: CmdCtx) -> None:
    await _safe(ctx, ctx.svc.mod.unlock(ctx.bot, ctx.chat_id, ctx.user.id), "🔓 방 잠금을 풀었어요 (잠그기 전 권한으로 복원).")


# ── 예약 공지 ─────────────────────────────────────────────
async def c_announce(ctx: CmdCtx) -> None:
    an, db = ctx.svc.announcer, ctx.svc.db
    sub = ctx.args[0] if ctx.args else "목록"
    sid = int(ctx.args[1].lstrip("#")) if len(ctx.args) > 1 and ctx.args[1].lstrip("#").isdecimal() else None

    if sub in ("목록", "list"):
        await ctx.reply(await an.list_text(ctx.chat_id))
        return
    if sub in ("만들기", "추가", "new", "add"):
        await an.start(ctx.bot, ctx.msg)
        return
    if sid is None:
        await ctx.reply("공지 번호가 필요해요. 예: <code>.예약공지 수정 3</code> (번호는 <code>.예약공지</code> 목록에서)")
        return
    row = await db.get_schedule(ctx.chat_id, sid)
    if not row:
        await ctx.reply(f"#{sid} 예약공지가 없어요.")
        return
    if sub in ("수정", "edit"):
        await an.start(ctx.bot, ctx.msg, edit_row=row)
    elif sub in ("미리보기", "preview"):
        await an.send(ctx.bot, ctx.chat_id, title=row["title"], text=row["text"],
                      media_type=row["media_type"], media_id=row["media_id"])
    elif sub in ("지금", "now"):
        await an.publish(ctx.bot, row)
    elif sub in ("켜기", "on", "끄기", "off"):
        on = sub in ("켜기", "on")
        await db.set_schedule_enabled(ctx.chat_id, sid, on)
        await ctx.reply(f"#{sid} 예약공지를 {'켰' if on else '껐'}어요.")
    elif sub in ("삭제", "del"):
        await db.delete_schedule(ctx.chat_id, sid)
        await db.log_mod(ctx.chat_id, ctx.user.id, None, "schedule_del", f"#{sid}")
        await ctx.reply(f"#{sid} 예약공지를 삭제했어요.")
    else:
        await ctx.reply(await an.list_text(ctx.chat_id))


# ── 입장 보안 ─────────────────────────────────────────────
async def c_captcha(ctx: CmdCtx) -> None:
    db = ctx.svc.db
    sub = ctx.args[0] if ctx.args else ""
    try:
        if sub in ("켜기", "on", "끄기", "off"):
            await db.set_setting(ctx.chat_id, "captcha_enabled", coerce("captcha_enabled", sub))
        elif sub in ("시간", "time") and len(ctx.args) > 1:
            await db.set_setting(ctx.chat_id, "captcha_minutes", coerce("captcha_minutes", ctx.args[1]))
        elif sub in ("실패", "action") and len(ctx.args) > 1:
            await db.set_setting(ctx.chat_id, "captcha_action", coerce("captcha_action", ctx.args[1]))
        elif sub:
            raise ValueError("사용법을 확인해주세요")
    except ValueError as e:
        await ctx.reply(f"❌ {esc(str(e))}")
        return
    s = await db.get_settings(ctx.chat_id)
    await ctx.reply(
        f"🤖 <b>입장 캡차</b>: {render('captcha_enabled', s['captcha_enabled'])} · "
        f"제한시간 {s['captcha_minutes']}분 · 실패 시 {render('captcha_action', s['captcha_action'])}\n"
        "<code>.캡차 켜기|끄기</code> · <code>.캡차 시간 5</code> · <code>.캡차 실패 킥|밴|뮤트</code>\n"
        "통과 처리: <code>.캡차통과 @user</code> (캡차 메시지의 '관리자 승인' 버튼도 가능)")


async def c_captcha_pass(ctx: CmdCtx) -> None:
    t = await _target(ctx, sanction=False)
    if not t:
        return
    uid, name, _ = t
    if not await ctx.svc.captcha.pending(ctx.chat_id, uid):
        await ctx.reply(f"{esc(name)}님은 캡차 대기 중이 아니에요. 제한을 풀려면 <code>.뮤트해제</code>")
        return
    await ctx.svc.captcha.approve(ctx.bot, ctx.chat_id, uid, name, ctx.user.id)
    await ctx.reply(f"✅ {esc(name)}님 캡차를 통과 처리했어요.")


async def c_cas(ctx: CmdCtx) -> None:
    sub = ctx.args[0] if ctx.args else ""
    if sub in ("켜기", "on", "끄기", "off"):
        await ctx.svc.db.set_setting(ctx.chat_id, "cas_enabled", coerce("cas_enabled", sub))
    elif sub in ("확인", "check") and len(ctx.args) > 1:
        arg = ctx.args[1].lstrip("@")
        if (uid := to_int(arg)) is not None and uid > 0:
            name = arg
        else:
            ctx.args = ctx.args[1:]
            t = await _target(ctx, sanction=False)
            if not t:
                return
            uid, name = t[0], t[1]
        banned = await ctx.svc.cas.is_banned(uid)
        await ctx.reply(f"🔎 {esc(name)}: " + ("⚠️ CAS 스팸 DB에 등록된 계정이에요." if banned else "등록 기록 없음 (또는 조회 실패)"))
        return
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    await ctx.reply(f"🛡️ <b>CAS 스팸DB 차단</b>: {render('cas_enabled', s['cas_enabled'])}\n"
                    "입장 시, 그리고 봇이 처음 보는 멤버가 말할 때 조회해서 등록된 스팸 계정이면 밴해요.\n"
                    "<code>.cas 켜기|끄기</code> · <code>.cas 확인 @user|ID</code>")


# ── 백업 (오너) ───────────────────────────────────────────
async def c_backup(ctx: CmdCtx) -> None:
    bk = ctx.svc.backup
    if ctx.args and ctx.args[0] in ("목록", "list"):
        files = bk.list()
        if not files:
            await ctx.reply("백업 파일이 없어요.")
            return
        lines = [f"🗄️ 백업 {len(files)}개 (최근 {ctx.svc.cfg.backup_keep}개 보관)"]
        lines += [f"- {esc(f.name)} ({f.stat().st_size / 1024:.0f}KB)" for f in files[:10]]
        await ctx.reply("\n".join(lines))
        return
    try:
        path = await bk.run()
    except Exception as e:  # 백업 실패 원인을 오너에게 그대로 보여준다
        log.exception("manual backup failed")
        await ctx.reply(f"❌ 백업 실패: {esc(str(e))}")
        return
    await ctx.reply(f"✅ 백업 완료: <code>{esc(path.name)}</code> ({path.stat().st_size / 1024:.0f}KB)\n"
                    f"보관 위치: <code>{esc(str(path.parent))}</code>")


async def c_filter(ctx: CmdCtx) -> None:
    db = ctx.svc.db
    sub = ctx.args[0] if ctx.args else "목록"
    word = " ".join(ctx.args[1:]).strip().lower()
    if sub in ("추가", "add") and word:
        await db.set_banned_word(ctx.chat_id, word, True)
        await ctx.reply(f"🚫 금지어 추가: {esc(word)}")
    elif sub in ("삭제", "del") and word:
        await db.set_banned_word(ctx.chat_id, word, False)
        await ctx.reply(f"금지어 삭제: {esc(word)}")
    else:
        words = await db.banned_words(ctx.chat_id)
        await ctx.reply("🚫 금지어: " + (", ".join(esc(w) for w in words) if words else "없음") +
                        "\n<code>.금지어 추가 단어</code> / <code>.금지어 삭제 단어</code>")


async def c_whitelist(ctx: CmdCtx) -> None:
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    domains = list(s["whitelist_domains"])
    sub = ctx.args[0] if ctx.args else "목록"
    raw = ctx.args[1] if len(ctx.args) > 1 else ""
    dom = raw.lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
    if sub in ("추가", "add") and dom:
        dom = normalize_domain(raw)
        if not dom:  # '<b>' 같은 값이 저장되면 이후 목록 출력이 깨짐
            await ctx.reply("도메인 형식이 아니에요. 예: <code>youtube.com</code>")
            return
        domains = sorted(set(domains) | {dom})
    elif sub in ("삭제", "del") and dom:
        domains = [d for d in domains if d != dom]
    else:
        await ctx.reply("✅ 허용 도메인: " + (esc(", ".join(domains)) or "없음") +
                        "\n<code>.허용도메인 추가 youtube.com</code>")
        return
    await ctx.svc.db.set_setting(ctx.chat_id, "whitelist_domains", domains)
    await ctx.reply("✅ 허용 도메인: " + (esc(", ".join(domains)) or "없음"))



async def c_botadmin(ctx: CmdCtx) -> None:
    sub = ctx.args[0] if ctx.args else "목록"
    if sub in ("추가", "add", "삭제", "del"):
        ctx.args = ctx.args[1:]
        t = await _target(ctx, sanction=False)
        if not t:
            return
        on = sub in ("추가", "add")
        await ctx.svc.db.set_bot_admin(ctx.chat_id, t[0], on)
        await ctx.reply(f"{esc(t[1])}님 봇 관리자 {'추가' if on else '해제'} (텔레그램 관리자가 아니어도 봇 관리 명령 사용 가능)")
        return
    ids = await ctx.svc.db.bot_admin_ids(ctx.chat_id)
    await ctx.reply("🛠️ 봇 관리자 ID: " + (", ".join(f"<code>{i}</code>" for i in ids) or "없음") +
                    "\n<code>.봇관리자 추가 @user</code> / <code>.봇관리자 삭제 @user</code>")


async def c_ai(ctx: CmdCtx) -> None:
    on = not ctx.args or ctx.args[0] in ("켜기", "on", "켬")
    await ctx.svc.db.set_setting(ctx.chat_id, "ai_enabled", on)
    await ctx.reply(f"🤖 AI 대화를 {'켰' if on else '껐'}어요.")


async def c_greet(ctx: CmdCtx) -> None:
    sub = ctx.args[0] if ctx.args else ""
    db = ctx.svc.db
    if sub in ("켜기", "on"):
        await db.set_setting(ctx.chat_id, "greet_enabled", True)
        await ctx.reply("👋 입장 인사를 켰어요.")
    elif sub in ("끄기", "off"):
        await db.set_setting(ctx.chat_id, "greet_enabled", False)
        await ctx.reply("입장 인사를 껐어요.")
    elif sub in ("설정", "set"):
        tpl = ctx.argstr[len(sub):].strip()
        await db.set_setting(ctx.chat_id, "greet_template", tpl)
        await ctx.reply("인사 템플릿을 " + (f"설정했어요:\n{esc(tpl)}" if tpl else "지웠어요 (AI 인사로 돌아가요)."))
    elif sub in ("테스트", "test"):
        ctx.svc.greeter.queue(ctx.bot, ctx.chat_id, ctx.user.id, user_name(ctx.user))
    else:
        await ctx.reply("사용법: <code>.인사 켜기|끄기|테스트</code>\n"
                        "<code>.인사 설정 {names} 대표님 환영합니다!</code> (비우면 AI가 인사)")


async def c_setrules(ctx: CmdCtx) -> None:
    await ctx.svc.db.set_setting(ctx.chat_id, "rules", ctx.argstr[:2000])
    await ctx.reply("📜 방 규칙을 저장했어요." if ctx.argstr else "방 규칙을 지웠어요.")


async def c_notice(ctx: CmdCtx) -> None:
    if not ctx.argstr:
        await ctx.reply("사용법: <code>.공지 내용</code> (봇이 올리고 고정해요)")
        return
    try:
        sent = await ctx.bot.send_message(ctx.chat_id, f"📢 <b>공지</b>\n{esc(ctx.argstr)}", parse_mode="HTML")
        await ctx.bot.pin_chat_message(ctx.chat_id, sent.message_id, disable_notification=False)
    except TelegramError as e:
        await ctx.reply(f"공지 실패: {esc(e.message)}")


async def c_modlog(ctx: CmdCtx) -> None:
    rows = await ctx.svc.db.recent_mod_log(ctx.chat_id, 15)
    if not rows:
        await ctx.reply("관리 기록이 없어요.")
        return
    lines = ["🗂️ <b>최근 관리 기록</b>"]
    for r in rows:
        who = esc(r["target_name"] or (str(r["target_id"]) if r["target_id"] else "-"))
        lines.append(f"{fmt_time(r['ts'], ctx.svc.cfg.tz)} {r['action']} {who} {esc(r['detail'] or '')}")
    await ctx.reply("\n".join(lines))


async def c_usage(ctx: CmdCtx) -> None:
    u = await ctx.svc.llm.usage_today()
    budget = ctx.svc.cfg.daily_token_budget
    hit = u["cached_tokens"] * 100 // max(u["prompt_tokens"], 1)
    await ctx.reply(
        f"🔋 오늘 AI 토큰: {u['tokens']:,} / {budget:,} ({u['tokens'] * 100 // max(budget, 1)}%)\n"
        f"💾 프롬프트 캐시 적중: 입력 {u['prompt_tokens']:,} 중 {u['cached_tokens']:,} ({hit}%)\n"
        "캐시로 읽은 입력은 요금이 크게 할인돼요. 대화가 이어질수록 적중률이 올라가요.")


# ── 지식 베이스 ───────────────────────────────────────────
async def c_knowledge(ctx: CmdCtx) -> None:
    """방에서는 그 방 자료, 오너 1:1 채팅에서는 모든 방 공통 자료(chat_id 0)."""
    scope = 0 if ctx.chat_id > 0 else ctx.chat_id
    scope_name = "모든 방 공통" if scope == 0 else "이 방"
    db = ctx.svc.db
    sub = ctx.args[0] if ctx.args else "목록"

    if sub in ("추가", "add"):
        if scope != 0 and not await ctx.svc.paid_features(ctx.chat_id):
            await ctx.reply("자료 등록은 이용 기간 중인 방에서 할 수 있어요. <code>.구독</code> 으로 확인해주세요.")
            return
        title_line = ctx.argstr[len(sub):].strip()
        title, _, inline = title_line.partition("\n")
        reply = ctx.msg.reply_to_message
        try:
            if reply and reply.document:
                doc = reply.document
                if (doc.file_size or 0) > knowledge.MAX_FILE_BYTES:
                    raise knowledge.KnowledgeError("파일이 너무 커요 (5MB 이하)")
                data = bytes(await (await ctx.bot.get_file(doc.file_id)).download_as_bytearray())
                text, source = knowledge.extract_text(data, doc.file_name or "file.txt"), doc.file_name or "파일"
            elif reply and (reply.text or reply.caption):
                text, source = reply.text or reply.caption, "메시지"
            elif inline.strip():
                text, source = inline, "직접 입력"
            else:
                await ctx.reply("📚 자료 등록 방법\n"
                                "• 파일(txt·md·csv·pdf)이나 글에 <b>답장</b>하면서 <code>.지식 추가 제목</code>\n"
                                "• 또는 <code>.지식 추가 제목</code> 다음 줄부터 내용을 바로 써서 보내기")
                return
            doc_id, n, suspicious = await knowledge.add_document(
                db, scope, title.strip() or source, text, source, ctx.user.id)
        except knowledge.KnowledgeError as e:
            await ctx.reply(f"❌ {esc(str(e))}")
            return
        await db.log_mod(ctx.chat_id, ctx.user.id, None, "knowledge_add", f"#{doc_id} {title}")
        note = "\n⚠️ 지시문처럼 보이는 문장이 있어요. AI는 이 자료를 정보로만 참고하고 지시로 따르지는 않아요." if suspicious else ""
        await ctx.reply(f"📚 {scope_name} 자료 <code>#{doc_id}</code> 등록 ({n}조각, {len(text):,}자). "
                        f"이제 AI가 관련 질문에 이 자료를 찾아 답해요.{note}")
        return

    if sub in ("삭제", "del") and len(ctx.args) > 1 and ctx.args[1].lstrip("#").isdecimal():
        ok = await db.delete_knowledge(scope, int(ctx.args[1].lstrip("#")))
        await ctx.reply("🗑️ 삭제했어요." if ok else f"{scope_name} 자료 중에 그 번호가 없어요.")
        return

    if sub in ("검색", "search") and len(ctx.args) > 1:
        results = await knowledge.search(db, ctx.chat_id if ctx.chat_id < 0 else 0, " ".join(ctx.args[1:]))
        if not results:
            await ctx.reply("관련 자료를 못 찾았어요.")
            return
        await ctx.reply("🔎 " + "\n\n".join(f"<b>#{r['doc_id']} {esc(r['title'])}</b>\n{esc(r['content'][:300])}"
                                            for r in results[:3]))
        return

    docs = await db.knowledge_docs(ctx.chat_id if ctx.chat_id < 0 else 0)
    if not docs:
        await ctx.reply(f"📚 등록된 자료가 없어요. <code>.지식 추가 제목</code> 으로 등록해보세요 ({scope_name}).")
        return
    lines = ["📚 <b>등록 자료</b> (AI가 질문에 답할 때 찾아봐요)"]
    for d in docs:
        tag = "공통" if d["chat_id"] == 0 else "이 방"
        lines.append(f"<code>#{d['id']}</code> [{tag}] {esc(d['title'])} · {d['chars']:,}자")
    lines.append("\n<code>.지식 검색 단어</code> · <code>.지식 삭제 번호</code>")
    await ctx.reply("\n".join(lines))


async def c_subscribe(ctx: CmdCtx) -> None:
    """방: 관리자 1:1 로 설정 화면 전송 (방에는 금액·결제 문구 없음). 1:1: 내가 관리하는 방 목록."""
    svc = ctx.svc
    if not svc.billing.enabled:
        await ctx.reply("지금은 모든 방이 무료로 이용 중이에요.")
        return
    if ctx.chat_id < 0 and ctx.role < Role.ADMIN:
        await ctx.reply("관리자만 쓸 수 있는 명령어예요.")
        return
    if ctx.chat_id < 0:
        try:  # 결제 화면(금액)은 텔레그램 관리자·오너만 (봇관리자 제외), 캐시 말고 지금 상태로
            svc.perms.forget(ctx.chat_id)
            tg_admin = await svc.perms.is_tg_admin(ctx.bot, ctx.chat_id, ctx.user.id)
        except TelegramError:
            tg_admin = False
        if not tg_admin:
            await _private_notice(ctx, "이용 기간·연장은 텔레그램 방 관리자만 볼 수 있어요.")
            return
        if await subscription.send_panel_dm(svc, ctx.bot, ctx.chat_id, ctx.user.id):
            await _private_notice(ctx, "🔒 관리자님, 봇과의 1:1 채팅을 확인해주세요.")
        else:
            await _private_notice(ctx, "관리자님, 아래 버튼으로 봇과 1:1 채팅을 열어주세요.",
                                  subscription.setup_button(ctx.bot.username, ctx.chat_id), seconds=60)
        return
    rows = []
    for chat_id in await svc.perms.candidate_chats(ctx.user.id):  # 후보 방만 (방 전체에 관리자 조회 안 돌게)
        if chat_id >= 0:
            continue
        try:
            if not await svc.perms.is_tg_admin(ctx.bot, chat_id, ctx.user.id):
                continue
        except TelegramError:
            continue  # 봇이 나간 방
        st = await svc.billing.status(chat_id)
        rows.append((chat_id, await subscription.chat_title(svc, chat_id), st))
    if not rows:
        await ctx.reply("관리 중인 방이 없어요. 봇을 방에 초대하고 관리자로 지정해주세요.")
        return
    for chat_id, title, st in rows[:10]:
        text, kb = await subscription.panel(svc, chat_id)
        await ctx.reply(text, reply_markup=kb)


async def c_grant(ctx: CmdCtx) -> None:
    """오너: 결제 없이 기간 부여 (지인 방, 잘못 보낸 입금 처리 등)."""
    if len(ctx.args) < 2 or to_int(ctx.args[0]) is None or to_int(ctx.args[1]) is None:
        await ctx.reply("사용법: <code>.구독부여 방ID 일수</code> (방 ID 는 그 방에서 .내아이디)")
        return
    chat_id, days = int(ctx.args[0]), int(ctx.args[1])
    if chat_id >= 0 or not 1 <= days <= 3650:
        await ctx.reply("방 ID(음수)와 1~3650 일수를 확인해주세요.")
        return
    until = await ctx.svc.billing.extend(chat_id, days)
    await ctx.svc.db.log_mod(chat_id, ctx.user.id, None, "sub_grant", f"{days}일")
    await ctx.reply(f"✅ {chat_id} 방 이용 기간을 {fmt_time(until, ctx.svc.cfg.tz, '%Y-%m-%d')} 까지로 연장했어요.")


async def c_myid(ctx: CmdCtx) -> None:
    await ctx.reply(f"🪪 {esc(user_name(ctx.user))}님의 텔레그램 ID: <code>{ctx.user.id}</code>\n"
                    + (f"이 방 ID: <code>{ctx.chat_id}</code>" if ctx.chat_id < 0 else ""))


async def c_report(ctx: CmdCtx) -> None:
    await ctx.reply(await stats.summary_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, "오늘") + "\n\n" +
                    await stats.ranking_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, "오늘", 5))


COMMANDS: list[Cmd] = [
    Cmd(("도움말", "help", "명령어", "start"), c_help, help="명령어 목록", dm_ok=True),
    Cmd(("내아이디", "id", "myid"), c_myid, help="내 텔레그램 숫자 ID (방에선 방 ID도)", dm_ok=True),
    Cmd(("규칙", "rules"), c_rules, help="방 규칙 보기"),
    Cmd(("내정보", "me", "정보", "info"), c_me, usage="[@user]", help="활동 정보"),
    Cmd(("기록", "이름기록", "history"), c_history, usage="[@user|ID|답장]",
        help="이름·아이디 변경 기록 (최근)", group="이름 기록", dm_ok=True),
    Cmd(("전체기록", "allhistory"), c_allhistory, usage="[@user|ID|답장]", help="변경 기록 전체",
        group="이름 기록", dm_ok=True),
    Cmd(("이름조회", "check_name", "names"), c_check_name, usage="[@user|ID|답장]", help="이름 변경만",
        group="이름 기록", dm_ok=True),
    Cmd(("아이디조회", "check_username", "usernames"), c_check_username, usage="[@user|ID|답장]",
        help="@아이디 변경만", group="이름 기록", dm_ok=True),
    Cmd(("내기록", "myhistory"), c_myhistory, help="내 이름·아이디 기록", group="이름 기록", dm_ok=True),
    Cmd(("멤버", "멤버목록", "members"), c_members, Role.ADMIN, help="방 멤버 목록 (1:1 로)", group="관리자"),
    Cmd(("이름알림", "namealert"), c_name_notice, Role.ADMIN, usage="[켜기|끄기]", help="이름 변경 알림 설정",
        group="관리자"),
    Cmd(("랭킹", "rank"), c_rank, usage="[오늘|주간|월간|전체]", help="채팅 랭킹", group="집계"),
    Cmd(("통계", "stats"), c_stats, usage="[오늘|주간|월간]", help="방 통계", group="집계"),
    Cmd(("검색", "search"), c_search, usage="키워드", help="대화 검색 (최근 30일)", group="집계"),
    Cmd(("게임", "game"), c_game, usage="[종류]", help="게임 시작", group="게임"),
    Cmd(("게임종료", "stopgame"), c_stop_game, help="게임 끝내기", group="게임"),
    Cmd(("포인트", "points"), c_points, help="게임 포인트 랭킹", group="게임"),
    Cmd(("스포츠", "sports"), c_sports, usage="[오늘 축구|팀 이름|결과 이름|구독 이름|해제 이름|목록]",
        help="경기 일정·결과·알림", group="스포츠"),
    Cmd(("말투", "style"), c_style, usage="[" + style_list() + "|기본]", help="나에게 쓸 봇 말투", dm_ok=True),
    Cmd(("호칭", "callme"), c_nickname, usage="부를 이름", help="봇이 부를 호칭", dm_ok=True),
    Cmd(("봇정보", "about"), c_about, help="봇 소개", dm_ok=True),
    # 관리자
    Cmd(("settings", "설정"), c_settings, Role.ADMIN, usage="[전체]", help="버튼 설정 메뉴를 1:1로 받기 (전체: 글 목록)",
        group="관리자"),
    Cmd(("set", "설정변경"), c_set, Role.ADMIN, usage="키 값", help="설정 바꾸기", group="관리자"),
    Cmd(("경고", "warn"), c_warn, Role.ADMIN, usage="@user [사유]", help="경고 (누적 시 자동 제재)", group="관리자"),
    Cmd(("경고취소", "unwarn"), c_unwarn, Role.ADMIN, usage="@user", help="경고 1회 취소", group="관리자"),
    Cmd(("경고목록", "warns"), c_warns, Role.ADMIN, usage="@user", help="경고 내역", group="관리자"),
    Cmd(("경고초기화", "resetwarns"), c_resetwarns, Role.ADMIN, usage="@user", help="경고 전부 삭제", group="관리자"),
    Cmd(("뮤트", "mute"), c_mute, Role.ADMIN, usage="@user [30m|2h|1d] [사유]", help="채팅 금지", group="관리자"),
    Cmd(("뮤트해제", "unmute"), c_unmute, Role.ADMIN, usage="@user", help="채팅 금지 해제", group="관리자"),
    Cmd(("밴", "ban"), c_ban, Role.ADMIN, usage="@user [사유]", help="영구 추방", group="관리자"),
    Cmd(("밴해제", "unban"), c_unban, Role.ADMIN, usage="@user|ID", help="추방 해제", group="관리자"),
    Cmd(("킥", "kick"), c_kick, Role.ADMIN, usage="@user", help="내보내기 (재입장 가능)", group="관리자"),
    Cmd(("삭제", "del"), c_del, Role.ADMIN, help="답장한 메시지 삭제", group="관리자"),
    Cmd(("청소", "purge"), c_purge, Role.ADMIN, usage="개수", help="최근 메시지 일괄 삭제", group="관리자"),
    Cmd(("잠금", "lock"), c_lock, Role.ADMIN, help="방 잠금", group="관리자"),
    Cmd(("잠금해제", "unlock"), c_unlock, Role.ADMIN, help="방 잠금 해제", group="관리자"),
    Cmd(("금지어", "filter"), c_filter, Role.ADMIN, usage="[추가|삭제] 단어", help="금지어 관리", group="관리자"),
    Cmd(("허용도메인", "whitelist"), c_whitelist, Role.ADMIN, usage="[추가|삭제] 도메인", help="링크 허용 목록", group="관리자"),
    Cmd(("ai",), c_ai, Role.ADMIN, usage="켜기|끄기", help="AI 대화 켜고 끄기", group="관리자"),
    Cmd(("인사", "greet"), c_greet, Role.ADMIN, usage="켜기|끄기|설정|테스트", help="입장 인사 설정", group="관리자"),
    Cmd(("규칙설정", "setrules"), c_setrules, Role.ADMIN, usage="내용", help="방 규칙 저장", group="관리자"),
    Cmd(("공지", "notice"), c_notice, Role.ADMIN, usage="내용", help="공지 올리고 고정", group="관리자"),
    Cmd(("예약공지", "schedule"), c_announce, Role.ADMIN, usage="[만들기|수정|미리보기|지금|켜기|끄기|삭제] [번호]",
        help="제목·사진/영상 포함 예약·반복 공지", group="관리자"),
    Cmd(("캡차", "captcha"), c_captcha, Role.ADMIN, usage="[켜기|끄기|시간 N|실패 킥|밴|뮤트]", help="입장 캡차 설정", group="관리자"),
    Cmd(("캡차통과", "approve"), c_captcha_pass, Role.ADMIN, usage="@user", help="캡차 수동 통과", group="관리자"),
    Cmd(("cas",), c_cas, Role.ADMIN, usage="[켜기|끄기|확인 @user]", help="CAS 스팸DB 차단", group="관리자"),
    Cmd(("관리기록", "modlog"), c_modlog, Role.ADMIN, help="최근 제재·설정 기록", group="관리자"),
    Cmd(("사용량", "usage"), c_usage, Role.ADMIN, help="오늘 AI 토큰·캐시 적중률", group="관리자", dm_ok=True),
    # 권한은 함수 안에서 판단: 방에선 관리자만, 1:1 에선 누구나(본인이 관리자인 방만 보여줌)
    Cmd(("구독", "설정하기", "subscribe"), c_subscribe, help="이용 기간 확인·연장 (방 관리자, 1:1 채팅으로 안내)",
        group="관리자", dm_ok=True),
    Cmd(("지식", "자료", "knowledge"), c_knowledge, Role.ADMIN, usage="[추가 제목|검색 단어|삭제 번호]",
        help="AI가 참고할 문서·자료 등록 (1:1 에선 모든 방 공통)", group="관리자", dm_ok=True),
    Cmd(("리포트", "report"), c_report, Role.ADMIN, help="오늘 집계 리포트", group="관리자"),
    Cmd(("봇관리자", "botadmin"), c_botadmin, Role.OWNER, usage="[추가|삭제] @user", help="봇 관리자 지정", group="오너"),
    Cmd(("백업", "backup"), c_backup, Role.OWNER, usage="[목록]", help="DB 지금 백업 / 백업 목록", group="오너", dm_ok=True),
    Cmd(("구독부여", "grant"), c_grant, Role.OWNER, usage="방ID 일수", help="결제 없이 이용 기간 부여", group="오너", dm_ok=True),
]
_INDEX = {name.lower(): cmd for cmd in COMMANDS for name in cmd.names}


def parse(text: str, bot_username: str) -> tuple[Cmd, list[str], str] | None:
    """'.경고 @a 도배' → (Cmd, ['@a', '도배'], '@a 도배'). 명령어가 아니면 None."""
    if not text or text[0] not in "./" or len(text) < 2 or text[1] in "./ ":
        return None
    head, _, rest = text[1:].partition(" ")
    name, _, at = head.partition("@")
    if at and bot_username and at.lower() != bot_username.lower():
        return None  # 다른 봇에게 보낸 /command
    cmd = _INDEX.get(name.lower())
    if not cmd:
        return None
    rest = rest.strip()
    return cmd, rest.split(), rest


async def dispatch(ctx: CmdCtx, cmd: Cmd) -> None:
    if ctx.role < cmd.role:
        await ctx.reply("관리자만 쓸 수 있는 명령어예요." if cmd.role == Role.ADMIN else "봇 오너만 쓸 수 있어요.")
        return
    try:
        await cmd.fn(ctx)
    except TelegramError as e:
        log.warning("command %s failed: %s", cmd.names[0], e)
        await ctx.reply(f"처리 중 텔레그램 오류: {esc(e.message)}")
