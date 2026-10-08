"""명령어. `.명령어` 와 `/command` 둘 다 받는다. 한글/영어 별칭 지원."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from telegram.error import BadRequest, TelegramError

from . import fedban, free, knowledge, menu, namehist, persist, stats, subscription
from .moderation import StillBanned
from .permissions import Role, may, no_right_text
from .services import Services
from .security import normalize_domain
from .settings import DEFAULTS, LABELS, coerce, over_cap, render
from .styles import STYLES, resolve_style, style_list
from .games import GAME_LIST
from .ai_settings import ROOM_TOKENS_MAX
from .llm import ROOM_TOKENS
from .util import (_DURATION, display_name, esc, fmt_time, human_minutes, iyeyo, mention, parse_duration, to_int,
                   rich_html, user_name)

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
    right: str | None = None  # "restrict"|"delete": 텔레그램 관리자 권한까지 확인 (permissions.Permissions.can)


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
    _delete_later(ctx.bot, ctx.chat_id, sent.message_id, seconds, ctx.svc.db)


def _delete_later(bot, chat_id: int, message_id: int, seconds: int, db=None) -> None:
    """seconds 뒤 지움 (DB 에도 적어 그 사이 재시작돼도 지움, sodam/persist.py)."""
    task = persist.delete_later(bot, chat_id, message_id, seconds, db=db)
    if task is not None:
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
def feature_lines(call: str, *, in_dm: bool = False) -> list[str]:
    """도움말 맨 위 '이런 것도 돼요' (AI·사진·그림·포인트 게임). menu.HELP 와 같은 내용."""
    ai = "그냥 말 걸기" if in_dm else f"<code>{esc(call)} …</code> 질문 (AI 답에 답장해도 돼요)"
    return [f"💬 AI: {ai} · 사진에 답장하며 <code>{esc(call)} 이거 뭐야</code> · <code>{esc(call)} ○○ 그려줘</code>",
            "🎰 포인트 게임 <code>!도움</code> · 🖼 결과표 <code>!그림장</code> · 🎡 <code>!룰렛 금액</code> 버튼 베팅판"
            + (" (그룹방에서)" if in_dm else "")]


HELP_ROOM_SECONDS = 120  # 방 관리자 도움말은 방에 잠깐만

# '.도움말' = 명령어보다 먼저 "소담에게 이렇게 말해보세요" (말로 하는 게 기본, 명령어 전체는 .명령어)
# (묶음, 예시들). 예시는 실제 도구로 되는 말만 (tools.py · panels/roomrule.py)
EXAMPLES_MEMBER: list[tuple[str, list[str]]] = [
    ("💬 대화", ["부가세 신고 언제까지야?", "이거 뭐야 (사진에 답장)", "고양이 그림 그려줘"]),
    ("📊 분석", ["오늘 방 분위기 어때?"]),
    ("📚 자료", ["우리 방 규칙 알려줘"]),
    ("🔎 검색", ["지난주에 USDT 얘기한 내용 찾아줘"]),
    ("🎮 게임", ["끝말잇기 시작"]),
]
EXAMPLES_ADMIN: list[tuple[str, list[str]]] = [
    ("👮 관리", ["철수 최근 경고 내역 보여줘"]),
    ("📚 자료 저장", ["광고는 관리자에게 먼저 말하기, 방 규칙으로 기억해"]),
    ("🕐 예약", ["매일 9시에 하루 요약해줘", "매주 월 10:00 신규 가입 통계"]),
    ("🔔 알림", ["홍길동 들어오면 알려줘"]),
]


def example_lines(call: str, *, admin: bool, in_dm: bool = False) -> list[str]:
    """역할별 말 예시 (멤버 = 멤버 예시만, 관리자 = + 관리자 예시). 예시는 그룹방 기준(호출어 붙임)."""
    def say(s: str) -> str:
        text, _, note = s.partition(" (")      # '이거 뭐야 (사진에 답장)' → 괄호는 코드 밖 설명
        return f"<code>{esc(call + ' ' + text)}</code>" + (f" ({esc(note)}" if note else "")

    def block(items):
        return [f"{label} · " + " / ".join(say(s) for s in says) for label, says in items]
    lines = ["🤖 <b>소담에게 이렇게 말해보세요</b>",
             "그룹방에선 <code>" + esc(call) + "</code> 로 부르거나 소담 답에 답장"
             + (" · 여기(1:1)에선 그냥 말하면 돼요" if in_dm else ""),
             *block(EXAMPLES_MEMBER)]
    if admin:
        lines += ["", "<b>관리자</b>", *block(EXAMPLES_ADMIN)]
    return lines


def help_text(call: str, *, admin: bool, in_dm: bool = False) -> str:
    return "\n".join(example_lines(call, admin=admin, in_dm=in_dm)
                     + ["", "📋 명령어 전체 <code>.명령어</code> · 🎰 포인트 게임 <code>!도움</code>"])


async def is_any_admin(svc: Services, bot: Bot, uid: int) -> bool:
    """1:1 에서 관리자 예시를 보여줄지: 오너이거나 소담이 있는 방 하나라도 관리자."""
    if uid in await svc.perms.owners():
        return True
    try:
        return bool(await menu.admin_groups(svc, bot, uid))
    except TelegramError:
        return False


async def c_help(ctx: CmdCtx) -> None:
    """말 예시 먼저 (짧게). 방 관리자에겐 관리자 예시까지 — 방엔 잠깐만 보였다 지움."""
    in_dm = ctx.chat_id > 0
    admin = ctx.role >= Role.ADMIN or (in_dm and await is_any_admin(ctx.svc, ctx.bot, ctx.user.id))
    sent = await ctx.reply(help_text(ctx.svc.cfg.call_names[0], admin=admin, in_dm=in_dm))
    if admin and not in_dm:
        _delete_later(ctx.bot, ctx.chat_id, sent.message_id, HELP_ROOM_SECONDS, ctx.svc.db)


async def c_commands(ctx: CmdCtx) -> None:
    """명령어 전체 목록 (예전 .도움말)."""
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
             *feature_lines(ctx.svc.cfg.call_names[0], in_dm=in_dm)]
    for group, lines in groups.items():
        parts.append(f"\n<b>[{group}]</b>\n" + "\n".join(lines))
    full = "\n".join(parts)
    if in_dm or ctx.role < Role.ADMIN:
        await ctx.reply(full)
        return
    # 방 관리자: 관리자 명령까지 60줄이 넘어서 방엔 짧게(잠깐 뒤 삭제), 전체 목록은 1:1 로
    try:
        await ctx.bot.send_message(ctx.user.id, full, parse_mode="HTML")
        text = ("📬 관리자 명령어 전체를 1:1 로 보냈어요.\n"
                "버튼 설정 <code>.설정</code> · 이용 기간 <code>.구독</code> · AI 켜고 끄기 <code>.AI대화</code>")
    except TelegramError:  # 봇과 1:1 을 시작 안 했으면 방에 잠깐 보여줌
        text = full + "\n\n(봇과 1:1 대화를 시작해두면 다음부턴 1:1 로 보내드려요)"
    sent = await ctx.reply(text)
    _delete_later(ctx.bot, ctx.chat_id, sent.message_id, HELP_ROOM_SECONDS, ctx.svc.db)


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
    remote = False
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
        if uid is None:   # 기록에 없는 @아이디 → MTProto 도우미로 지금 주인 확인 (켜져 있을 때만)
            uid, remote = await namehist.resolve_remote(ctx.svc, ctx.args[0], ctx.user.id)
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
    await ctx.reply(await namehist.history_text(db, uid, tz, mode=mode, title=title,
                                                note=namehist.REMOTE_NOTE if remote else None),
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
        await ctx.reply(f"🔗 말 게임: {GAME_LIST} (<code>.게임 끝말잇기</code> · <code>.게임 끝말잇기 차례</code>)\n\n"
                        "🎰 <b>포인트 게임</b>: 홀짝·슬롯·바카라·블랙잭·그래프·경마…\n"
                        "<code>!가입</code> 후 <code>!도움</code> 으로 전체 목록")
        return
    result = await ctx.svc.games.start(ctx.bot, ctx.chat_id, ctx.user.id, " ".join(ctx.args[:2]))
    if not result.endswith("시작했어요!"):     # 시작했으면 게임 안내가 이미 올라감 (같은 말 두 번 안 함)
        await ctx.reply(esc(result))


async def c_stop_game(ctx: CmdCtx) -> None:
    game = ctx.svc.games.active.get(ctx.chat_id)
    if not game:
        await ctx.reply("진행 중인 게임이 없어요.")
        return
    if game.starter_id != ctx.user.id and ctx.role < Role.ADMIN:
        await ctx.reply("게임을 시작한 분이나 관리자만 끝낼 수 있어요.")
        return
    await ctx.svc.games.stop(ctx.chat_id)


STYLE_EXAMPLES = {   # .말투 도움말 — 한 줄 맛보기 (AI 없음)
    "polite": "안녕하세요 대표님, 무엇을 도와드릴까요?",
    "friendly": "대표님 오셨어요? 오늘도 화이팅이에요 😊",
    "free": "야 대표야, 오늘 뭐 했어? ㅋㅋ",
    "savage": "시비 걸면 쫄지 않고 반말로 더 웃기게 받아침 (욕엔 장난 욕까지)",
    "brief": "핵심만 한두 문장으로.",
    "secretary": "네 대표님, 결론부터 말씀드리겠습니다.",
    "tsundere": "딱히 대표님을 위해서 알려주는 건 아니에요…",
    "girlfriend": "자기 오늘 하루 어땠어? 밥은 챙겨 먹었지? ♡",
    "boyfriend": "오늘 고생 많았지? 힘든 일 있으면 나한테 말해.",
}
HELP_WORDS = ("도움말", "도움", "help", "목록", "설명", "종류", "?")


async def c_style_help(ctx: CmdCtx) -> None:
    from .settings import render
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    lines = ["🎭 <b>소담 말투</b> — 나한테만: <code>.말투 이름</code> · 되돌리기: <code>.말투 기본</code>", ""]
    for key, st in STYLES.items():
        lines.append(f"• <b>{st.label}</b> — {STYLE_EXAMPLES.get(key, '')}")
    lines += ["", "🏠 <b>이 방 설정</b> (관리자)",
              f"• 방 기본 말투: {render('style', s.get('style'))} → <code>.설정변경 style 맞받아치기</code>",
              f"• 욕 받아치기: {render('ai_comeback', s.get('ai_comeback'))} → <code>.설정변경 ai_comeback 똑같이</code> / <code>센스</code>",
              f"• 19금 드립 받아치기: {render('ai_spicy', s.get('ai_spicy'))} → <code>.설정변경 ai_spicy on</code> / <code>off</code>",
              "말로 해도 돼요: '소담아 욕 받아치기 똑같이로 바꿔'"]
    await ctx.reply("\n".join(lines))


async def c_style(ctx: CmdCtx) -> None:
    if ctx.args and (ctx.args[0].lower() in HELP_WORDS or "도움" in ctx.argstr):
        await c_style_help(ctx)
        return
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
    """일정·스코어·순위·팀·알림 구독 (sodam/sports/ui.py). 배당·베팅 정보는 없음."""
    from .sports import ui as sports_ui
    await ctx.reply(await sports_ui.command(ctx.svc, ctx.chat_id, ctx.user.id, ctx.role >= Role.ADMIN, ctx.args))


def _music(fn_name: str):
    """🎵 뮤직봇 명령 → panels/music.py (늦게 import — commands → menu → panels 순환 방지)."""
    async def run(ctx: CmdCtx) -> None:
        from .panels import music
        await getattr(music, fn_name)(ctx)
    run.__name__ = f"music_{fn_name}"
    return run


async def c_news(ctx: CmdCtx) -> None:
    from .panels import news as news_panel   # 늦게 import (commands → menu → panels 순환 방지). 동작은 sodam/news.py
    await news_panel.c_news(ctx)


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


async def _to_dm(ctx: CmdCtx, text: str, kb=None, what: str = "결과") -> bool:
    """관리자 1:1 로 보내고, 방이면 명령을 지우고 '1:1 확인' 만 잠깐 (방엔 명단·프로필을 안 뿌림)."""
    try:
        await ctx.bot.send_message(ctx.user.id, text, parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        if ctx.chat_id < 0:
            await _private_notice(ctx, "관리자님, 먼저 소담과 1:1 대화를 시작해주세요.",
                                  InlineKeyboardMarkup([[InlineKeyboardButton(
                                      "▶️ 1:1 열기", url=f"https://t.me/{ctx.bot.username}?start=cfg_{ctx.chat_id}")]]),
                                  seconds=60)
        return False
    if ctx.chat_id < 0:
        await _private_notice(ctx, f"🔒 관리자님, 1:1 채팅에서 {what}를 확인해주세요.")
    return True


async def _room_arg(ctx: CmdCtx, words: list[str]) -> tuple[int | None, str]:
    """방에선 그 방. 오너 1:1 에선 남은 말 = 방 이름·ID (봇이 있는 방 하나로 맞을 때만)."""
    if ctx.chat_id < 0:
        return ctx.chat_id, ""
    q = " ".join(words).strip()
    if not q:
        return None, "1:1 에선 방 이름이나 방 ID 를 붙여주세요. 예: <code>.멤버정리 세컨드</code>"
    from .tools import _norm_title
    rows = await ctx.svc.db._all("SELECT chat_id, title FROM chats WHERE chat_id < 0 ORDER BY title")
    qn = _norm_title(q)
    hit = ([r for r in rows if str(r["chat_id"]) == q] or [r for r in rows if qn and _norm_title(r["title"]) == qn]
           or [r for r in rows if qn and qn in _norm_title(r["title"])])
    if len(hit) != 1:
        names = ", ".join(f"{esc((r['title'] or '')[:20])}(<code>{r['chat_id']}</code>)" for r in hit[:8])
        return None, (f"'{esc(q[:30])}' 방이 여러 개예요: {names}" if hit else f"'{esc(q[:30])}' 방을 못 찾았어요.")
    return hit[0]["chat_id"], ""


def _reply_user(msg):
    """답장한 사람 (handlers.reply_ref 와 같은 거름): 포럼 토픽 첫 글(토픽 안 모든 글이 그 글에 답장으로 옴)·
    채널·익명 관리자 글이면 None — 토픽을 만든 사람을 대상으로 착각하지 않게."""
    r = getattr(msg, "reply_to_message", None)
    if r is None or getattr(r, "forum_topic_created", None) or (
            getattr(msg, "is_topic_message", False) and r.message_id == getattr(msg, "message_thread_id", None)):
        return None
    if getattr(r, "sender_chat", None) is not None:
        return None
    return getattr(r, "from_user", None)


async def _who(ctx: CmdCtx, chat_id: int | None, word: str) -> tuple[int, str] | str:
    """인자(숫자 ID · @아이디(이 방 멤버 기록 → 모르면 MTProto 조회) · 이름)가 먼저, 없으면 답장 → (ID, @아이디 힌트)."""
    if not word:
        who = _reply_user(ctx.msg) if ctx.chat_id < 0 else None
        if who is not None:
            return who.id, who.username or ""
        return "대상 메시지에 답장하거나 @아이디·숫자 ID 를 적어주세요."
    if (uid := to_int(word)) is not None and uid > 0:
        return uid, ""
    rows = await ctx.svc.db.find_members(chat_id, word) if chat_id else []
    if not rows and word.startswith("@"):
        rows = await ctx.svc.db._all("SELECT user_id, username FROM users WHERE username=? COLLATE NOCASE LIMIT 2",
                                     (word[1:],))
    if len(rows) == 1:
        return rows[0]["user_id"], rows[0]["username"] or ""
    if len(rows) > 1:
        return "같은 이름이 여러 명이에요. @아이디나 숫자 ID 로 적어주세요."
    mt = getattr(ctx.svc, "mtproto", None)
    if word.startswith("@") and mt is not None:
        got = await mt.resolve_username(word)
        if got:
            return got["id"], got["username"]
    return f"'{esc(word[:32])}' 를 못 찾았어요. 답장이나 숫자 ID 로 해주세요."


async def c_profile(ctx: CmdCtx) -> None:
    """👤 한 사람 프로필 (텔레그램 정보 + 이 방 소담 기록). 결과는 관리자 1:1 로만 (sodam/profile.py)."""
    from . import profile
    word = ctx.args[0] if ctx.args else ""   # 인자가 먼저 (답장은 인자가 없을 때만 — _who)
    rest = ctx.args[1:]
    chat_id: int | None = ctx.chat_id
    if ctx.chat_id > 0:   # 오너 1:1: '.프로필 @user 세컨드' (방 없으면 텔레그램 정보만)
        chat_id = None
        if rest:
            chat_id, err = await _room_arg(ctx, rest)
            if chat_id is None:
                await ctx.reply(err)
                return
    who = await _who(ctx, chat_id, word)
    if isinstance(who, str):
        await (_private_notice(ctx, who) if ctx.chat_id < 0 else ctx.reply(who))
        return
    if not profile.allow(ctx.user.id):
        await (_private_notice(ctx, "프로필은 1분에 10번까지예요.") if ctx.chat_id < 0 else ctx.reply("프로필은 1분에 10번까지예요."))
        return
    uid, uname = who
    p = await profile.gather(ctx.svc, ctx.bot, chat_id, uid, uname)
    title = ""
    if chat_id:
        row = await ctx.svc.db._one("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
        title = (row["title"] if row else "") or ""
    await _to_dm(ctx, profile.card_html(uid, p, ctx.svc.cfg.tz, title), what="프로필")


CLEANUP_SUB = {"탈퇴": "d", "봇": "b", "잠수": "i", "모름": "u", "접속모름": "u", "가라": "f"}


async def c_cleanup(ctx: CmdCtx) -> None:
    """🧹 멤버 정리 (sodam/cleanup.py · panels/cleanup.py). 방: 관리자 + '사용자 차단' 권한. 오너 1:1: 방 이름·ID 를 붙여 다른 방."""
    from . import cleanup
    from .panels import cleanup as panel
    args = list(ctx.args)
    sub = args.pop(0) if args and (args[0] in CLEANUP_SUB or args[0] in ("중지", "제외", "제외해제", "제외목록")) else ""
    days = None
    if sub == "잠수":
        days = to_int(args[0]) if args else None
        if days is not None:
            args.pop(0)
        days = days or 30
        if not 1 <= days <= cleanup.IDLE_MAX:
            await ctx.reply(f"잠수 기준은 1~{cleanup.IDLE_MAX}일이에요.")
            return
    target = ""
    if sub in ("제외", "제외해제"):
        target = args.pop(0) if args else ""   # 인자가 먼저, 없으면 답장 (포럼 토픽 첫 글은 답장으로 안 봄)
    chat_id, err = await _room_arg(ctx, args)
    if chat_id is None:
        await ctx.reply(err)
        return
    if ctx.chat_id > 0 and not await may(ctx.svc.perms, ctx.bot, chat_id, ctx.user.id, "restrict"):
        await ctx.reply(no_right_text("restrict"))
        return
    svc, bot = ctx.svc, ctx.bot
    if sub == "중지":
        ok = await cleanup.stop_job(svc, chat_id, ctx.user.id)
        msg = "⏸ 멤버 정리를 멈출게요 (지금 사람까지 하고 멈춰요)." if ok else "진행 중인 멤버 정리가 없어요."
        await (_private_notice(ctx, msg) if ctx.chat_id < 0 else ctx.reply(msg))
        return
    if sub in ("제외", "제외해제"):
        who = await _who(ctx, chat_id, target)
        if isinstance(who, str):
            await (_private_notice(ctx, who) if ctx.chat_id < 0 else ctx.reply(who))
            return
        changed = await cleanup.exclude(svc.db, chat_id, who[0], ctx.user.id, sub == "제외")
        msg = (f"🚫 <code>{who[0]}</code> 멤버 정리 제외 명단에 {'넣었어요' if sub == '제외' else '뺐어요'}." if changed
               else f"<code>{who[0]}</code> 은(는) {'이미 제외 명단에 있어요' if sub == '제외' else '제외 명단에 없어요'}.")
        await (_private_notice(ctx, msg) if ctx.chat_id < 0 else ctx.reply(msg))
        return
    if sub == "제외목록":
        screen = await panel.s_excl(menu.PanelCtx(svc, bot, ctx.user.id, chat_id, []))
        await _to_dm(ctx, screen.text or "", screen.kb, what="제외 목록")
        return
    s, err = await cleanup.scan(svc, bot, chat_id, ctx.user.id)
    if s is None:
        await (_private_notice(ctx, esc(err), seconds=30) if ctx.chat_id < 0 else ctx.reply(esc(err)))
        return
    if sub:
        sel = cleanup.parse_sel(CLEANUP_SUB[sub] + (str(days) if sub == "잠수" else ""))
        screen = await panel.preview(svc, bot, chat_id, sel)
    else:
        screen = await panel.summary(svc, bot, chat_id, "(10분 안에 스캔한 결과 — 방마다 10분에 1번)" if s.get("reused") else "")
    await _to_dm(ctx, screen.text or "", screen.kb, what="멤버 정리")


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
    lines = ["⚙️ <b>방 설정</b> (바꾸기: <code>.설정변경 키 값</code>)"]
    for key in DEFAULTS:
        lines.append(f"<code>{key}</code> {esc(LABELS.get(key, ''))}: {esc(render(key, s[key]))}")
    chunk: list[str] = []   # 설정이 늘어 한 메시지(4096자)를 넘으면 나눠 보냄
    for line in lines:
        if chunk and sum(len(x) + 1 for x in chunk) + len(line) > 4000:
            await ctx.reply("\n".join(chunk))
            chunk = []
        chunk.append(line)
    await ctx.reply("\n".join(chunk))


async def c_set(ctx: CmdCtx) -> None:
    if len(ctx.args) < 2:
        await ctx.reply("사용법: <code>.설정변경 키 값</code> (예: <code>.설정변경 flood_count 5</code>, <code>.설정변경 style 자유분방</code>)\n"
                        "키 목록은 <code>.설정 전체</code>")
        return
    key, raw = ctx.args[0], ctx.argstr[len(ctx.args[0]):].strip()
    try:
        value = coerce(key, raw)
    except ValueError as e:
        await ctx.reply(f"❌ {esc(str(e))}")
        return
    if ctx.role < Role.OWNER and (why := over_cap(key, value)):
        await ctx.reply(f"❌ {esc(why)}")
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


async def c_free(ctx: CmdCtx) -> None:
    """자유 멤버 지정: 걸려 있던 채팅 금지·캡차 대기·경고를 풀고, 앞으로 자동 통제를 안 받게 (sodam/free.py)."""
    t = await _target(ctx, sanction=False)
    if not t:
        return
    uid, name, _ = t
    if await ctx.svc.perms.protected(ctx.bot, ctx.chat_id, uid):
        await ctx.reply("관리자·봇은 원래 자동 통제를 받지 않아요.")
        return
    await free.add(ctx.svc.db, ctx.chat_id, uid, ctx.user.id)
    await ctx.svc.db.clear_warnings(ctx.chat_id, uid)
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, uid, "free", "지정")
    note, lifted = "", "걸려 있던 채팅 금지·경고를 풀었고, "
    try:
        await ctx.svc.mod.unmute(ctx.bot, ctx.chat_id, uid, ctx.user.id)
    except StillBanned as e:
        note, lifted = f"\n⚠️ {e.message}", "경고를 지웠고, "     # 밴은 그대로라 '채팅 금지를 풀었다' 고 하지 않음
    except TelegramError as e:
        note = f"\n(채팅 금지는 못 풀었어요: {esc(e.message)} — 봇의 '사용자 차단' 권한을 확인해주세요)"
    await ctx.reply(f"🕊️ {mention(uid, name)}님을 자유 멤버로 지정했어요. {lifted}"
                    "앞으로 도배·링크·금지어·잠금·캡차 같은 자동 통제를 받지 않아요. (관리자 권한은 아니에요)" + note)


async def c_unfree(ctx: CmdCtx) -> None:
    t = await _target(ctx, sanction=False)
    if not t:
        return
    uid, name, _ = t
    if not await free.remove(ctx.svc.db, ctx.chat_id, uid):
        await ctx.reply(f"{mention(uid, name)}님은 자유 멤버가 아니에요.")
        return
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, uid, "free", "해제")
    await ctx.reply(f"{mention(uid, name)}님 자유 멤버를 해제했어요. 이제 다시 자동 통제를 받아요.")


async def c_freelist(ctx: CmdCtx) -> None:
    rows = await free.members(ctx.svc.db, ctx.chat_id)
    if not rows:
        await ctx.reply("자유 멤버가 없어요. <code>.free @아이디</code> 로 지정해요.")
        return
    names = [f"• {esc(display_name(r['first_name'], r['last_name'], r['username']) or str(r['user_id']))} "
             f"(<code>{r['user_id']}</code>)" for r in rows]
    await ctx.reply("🕊️ <b>자유 멤버</b> (자동 통제 안 받음)\n" + "\n".join(names))


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
                    f"🚫 {mention(uid, name)}님을 밴(영구 추방)했어요. (풀려면 <code>.밴해제</code>)")


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
    await ctx.reply(f"🛡️ <b>스팸 명단(CAS·lols) 차단</b>: {render('cas_enabled', s['cas_enabled'])}\n"
                    "입장 시, 그리고 봇이 처음 보는 멤버가 말할 때 조회해서 등록된 스팸 계정이면 밴해요.\n"
                    "<code>.스팸차단 켜기|끄기</code> · <code>.스팸차단 확인 @user|ID</code>")


# ── 백업 (오너) ───────────────────────────────────────────
async def c_botpic(ctx: CmdCtx) -> None:
    """🖼 봇 프로필 바꾸기 (sodam/botpic.py, Bot API 9.4 setMyProfilePhoto — @BotFather 는 사진만 받음)."""
    from . import botpic
    await botpic.run(ctx)


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
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.user.id, t[0], "bot_admin", "추가" if on else "해제")
        await ctx.reply(f"{esc(t[1])}님 봇 관리자 {'추가' if on else '해제'} (텔레그램 관리자가 아니어도 봇 관리 명령 사용 가능)")
        return
    ids = await ctx.svc.db.bot_admin_ids(ctx.chat_id)
    await ctx.reply("🛠️ 봇 관리자 ID: " + (", ".join(f"<code>{i}</code>" for i in ids) or "없음") +
                    "\n<code>.봇관리자 추가 @user</code> / <code>.봇관리자 삭제 @user</code>")


async def c_fedban(ctx: CmdCtx) -> None:
    """.공동차단 @user|답장 사유 → 공동 명단에 올리고 이 방에서 밴. .공동차단 해제 ID → 이 방 표시 빼기(오너는 완전 삭제)."""
    svc = ctx.svc
    if ctx.args and ctx.args[0] in ("해제", "삭제", "remove"):
        uid = to_int(ctx.args[1]) if len(ctx.args) > 1 else None
        if not uid or uid <= 0:
            await ctx.reply("사용법: <code>.공동차단 해제 숫자ID</code>")
            return
        owner = ctx.user.id in await svc.perms.owners()
        await ctx.reply(await fedban.remove(svc, ctx.chat_id, uid, ctx.user.id, owner=owner))
        return
    if not ctx.args and not ctx.msg.reply_to_message:
        await ctx.reply(fedban.USAGE)
        return
    # 이미 나간 계정도 올릴 수 있게 숫자 ID 는 바로 받는다 (답장이 없을 때)
    raw = to_int(ctx.args[0]) if ctx.args and not ctx.msg.reply_to_message else None
    if raw and raw > 0 and not await svc.db.find_members(ctx.chat_id, ctx.args[0]):
        if await svc.perms.protected(ctx.bot, ctx.chat_id, raw):
            await ctx.reply("관리자·봇·운영자는 공동 차단 명단에 올릴 수 없어요.")
            return
        uid, name, rest = raw, f"ID {raw}", ctx.args[1:]
    else:
        t = await _target(ctx)
        if not t:
            return
        uid, name, rest = t  # _target 이 관리자·봇·오너(protected)를 거른다
    reason = " ".join(rest).strip()
    if not reason:
        await ctx.reply("사유를 꼭 적어주세요. 예: <code>.공동차단 @아이디 코인 사기 DM</code>")
        return
    await ctx.reply(await fedban.add(svc, ctx.bot, ctx.chat_id, uid, name, reason, ctx.user.id))


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
    body = esc(ctx.argstr)
    rich = rich_html(ctx.msg)   # 움직이는 이모지·굵게 등 서식 보관 (명령어 뒤 부분만)
    if rich is not None and len(parts := rich.split(None, 1)) == 2:
        body = parts[1]
    try:
        try:
            sent = await ctx.bot.send_message(ctx.chat_id, f"📢 <b>공지</b>\n{body}", parse_mode="HTML")
        except BadRequest:
            if body == esc(ctx.argstr):
                raise
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
    """방 관리자: 그 방 사용량만 (봇 전체 토큰·예산은 다른 방 정보라 오너에게만)."""
    lines = []
    if ctx.chat_id < 0:
        day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
        used = await ctx.svc.db.counter(day, ctx.chat_id, ROOM_TOKENS)
        cap = min((await ctx.svc.db.get_settings(ctx.chat_id)).get("ai_room_daily_tokens") or ROOM_TOKENS_MAX,
                  ROOM_TOKENS_MAX)
        lines.append(f"🔋 오늘 이 방 AI 토큰: {used:,} / {cap:,} ({used * 100 // max(cap, 1)}%)")
    if ctx.role >= Role.OWNER:
        u = await ctx.svc.llm.usage_today()
        from . import costs
        hit = u["cached_tokens"] * 100 // max(u["prompt_tokens"], 1)
        spent, ub, tb = await ctx.svc.llm.usd_today(), ctx.svc.llm.usd_budget, ctx.svc.llm.token_budget
        cap = int(ub * costs.MICRO)
        lines += [f"💵 오늘 AI 요금: {costs.fmt_usd(spent)}" + (f" / {costs.fmt_usd(cap)} ({spent * 100 // max(cap, 1)}%)"
                                                            if ub > 0 else ""),
                  f"🌐 오늘 전체 AI 토큰: {u['tokens']:,}" + (f" / {tb:,} ({u['tokens'] * 100 // tb}%)" if tb else ""),
                  f"💾 프롬프트 캐시 적중: 입력 {u['prompt_tokens']:,} 중 {u['cached_tokens']:,} ({hit}%)",
                  "캐시로 읽은 입력은 요금이 크게 할인돼요. 대화가 이어질수록 적중률이 올라가요."]
        day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
        rows = await ctx.svc.db._all("SELECT key, n FROM counters WHERE day=? AND chat_id=0 AND "
                                     "(key LIKE 'prompt:%' OR key LIKE 'cached:%')", (day,))
        got = {r["key"]: r["n"] for r in rows}
        miss = sorted(((got[k] - got.get("cached:" + k[7:], 0), k[7:], got[k]) for k in got if k.startswith("prompt:")),
                      reverse=True)[:5]
        if miss:
            lines.append("캐시 안 된 입력이 많은 기능: " + " · ".join(
                f"{p} {m:,} ({(t - m) * 100 // max(t, 1)}% 적중)" for m, p, t in miss))
    await ctx.reply("\n".join(lines))


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
    if not await ctx.svc.db.has_chat(chat_id):   # 오타 ID 에 구독 줄을 만들고 '연장했어요' 하던 것 (오너 메뉴 _room_cid 와 같게)
        await ctx.reply(f"❌ 봇이 모르는 방 ID 예요: <code>{chat_id}</code>\n그 방에서 <code>.내아이디</code> 로 확인해주세요.")
        return
    until = await ctx.svc.billing.extend(chat_id, days)
    await ctx.svc.db.log_mod(chat_id, ctx.user.id, None, "sub_grant", f"{days}일")
    await ctx.reply(f"✅ {chat_id} 방 이용 기간을 {fmt_time(until, ctx.svc.cfg.tz, '%Y-%m-%d')} 까지로 연장했어요.")


async def c_route(ctx: CmdCtx) -> None:
    """🧭 오너: 방마다 AI 모델 길 (sodam/route.py). 방에선 이 방, 1:1 에선 방ID 또는 전체."""
    from . import route
    names = {"나눠": "hybrid", "나눠쓰기": "hybrid", "기본": "hybrid", "hybrid": "hybrid",
             "절약": "saver", "saver": "saver", "최고": "best", "best": "best"}
    in_dm = ctx.chat_id > 0
    if not ctx.svc.cfg.light_model:
        await ctx.reply("🧭 작은 모델(AGENT_LIGHT_MODEL)이 비어 있어서 지금은 모든 방이 큰 모델이에요.")
        return
    help_ = ("🧭 <b>AI 모델 길</b> (작은 모델: <code>" + esc(ctx.svc.cfg.light_model) + "</code>)\n"
             + "\n".join(f"• {esc(v)}" for v in route.MODES.values())
             + "\n사용법: " + ("<code>.AI모델 나눠|절약|최고 방ID|전체</code>" if in_dm else "<code>.AI모델 나눠|절약|최고</code>"))
    if not ctx.args:
        if in_dm:
            await ctx.reply(help_)
        else:
            cur = await route.room_mode(ctx.svc.db, ctx.chat_id)
            await ctx.reply(f"🧭 이 방: <b>{esc(route.MODES[cur])}</b>\n\n" + help_)
        return
    mode = names.get(ctx.args[0].lower())
    if mode is None:
        await ctx.reply(help_)
        return
    if in_dm:
        target = ctx.args[1] if len(ctx.args) > 1 else ""
        if target == "전체":
            rooms = [r["chat_id"] for r in await ctx.svc.db._all("SELECT chat_id FROM chats WHERE chat_id < 0")]
        elif (cid := to_int(target)) is not None and cid < 0 and await ctx.svc.db.has_chat(cid):
            rooms = [cid]
        else:
            await ctx.reply(help_)
            return
    else:
        rooms = [ctx.chat_id]
    for cid in rooms:
        await ctx.svc.db.set_state(cid, route.ROUTE_KEY, None if mode == route.DEFAULT_MODE else mode)
        await ctx.svc.db.log_mod(cid, ctx.user.id, None, "ai_route", mode)
    where = "이 방" if not in_dm else ("모든 방" if len(rooms) > 1 else f"방 {rooms[0]}")
    await ctx.reply(f"✅ {where}: <b>{esc(route.MODES[mode])}</b> — 다음 답부터 적용")


async def c_myid(ctx: CmdCtx) -> None:
    await ctx.reply(f"🪪 {esc(user_name(ctx.user))}님의 텔레그램 ID: <code>{ctx.user.id}</code>\n"
                    + (f"이 방 ID: <code>{ctx.chat_id}</code>" if ctx.chat_id < 0 else ""))


async def c_report(ctx: CmdCtx) -> None:
    await ctx.reply(await stats.summary_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, "오늘") + "\n\n" +
                    await stats.ranking_text(ctx.svc.db, ctx.chat_id, ctx.svc.cfg.tz, "오늘", 5))


async def c_tagall(ctx: CmdCtx) -> None:
    from .panels import tagall   # 늦게 import (panels → commands 순환)
    out = await tagall.offer(ctx.svc, ctx.bot, ctx.chat_id, ctx.user, ctx.argstr)
    if not out.startswith("확인 카드"):
        await ctx.reply(esc(out))


COMMANDS: list[Cmd] = [
    Cmd(("도움말", "help", "start", "사용법"), c_help, help="소담에게 말하는 법 (예시)", dm_ok=True),
    Cmd(("명령어", "commands", "명령어목록"), c_commands, help="명령어 전체 목록", dm_ok=True),
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
    Cmd(("프로필", "profile"), c_profile, Role.ADMIN, usage="@user|답장|ID",
        help="한 사람 프로필: 소개글·접속 상태·계정 생성 추정 + 이 방 기록 (1:1 로, 오너는 1:1 에서 뒤에 방 이름)",
        group="관리자", dm_ok=True),
    Cmd(("멤버정리", "cleanup"), c_cleanup, Role.ADMIN,
        usage="[탈퇴|봇|잠수 N|모름|가라|중지|제외 @user|제외해제 @user|제외목록]",
        help="탈퇴 계정·봇·잠수·가라 의심 멤버 스캔 → 1:1 미리보기·확인 카드 → 천천히 내보내기 (오너는 1:1 에서 뒤에 방 이름)",
        group="관리자", right="restrict", dm_ok=True),
    Cmd(("이름알림", "namealert"), c_name_notice, Role.ADMIN, usage="[켜기|끄기]", help="이름 변경 알림 설정",
        group="관리자"),
    Cmd(("랭킹", "rank"), c_rank, usage="[오늘|주간|월간|전체]", help="채팅 랭킹", group="집계"),
    Cmd(("통계", "stats"), c_stats, usage="[오늘|주간|월간]", help="방 통계", group="집계"),
    Cmd(("검색", "search"), c_search, usage="키워드", help="대화 검색 (최근 30일)", group="집계"),
    Cmd(("게임", "game"), c_game, usage="[종류]", help="게임 시작", group="게임"),
    Cmd(("게임종료", "stopgame"), c_stop_game, help="게임 끝내기", group="게임"),
    Cmd(("포인트", "points"), c_points, help="게임 포인트 랭킹", group="게임"),
    Cmd(("스포츠", "sports"), c_sports, usage="[오늘|내일 리그|라이브|순위 리그|팀 이름|구독 리그·팀|해제|목록]",
        help="경기 일정·스코어·순위·자동 알림", group="스포츠"),
    Cmd(("노래", "재생", "play", "음악"), _music("c_play"), usage="<제목|링크> (음악 파일 답장도)",
        help="음성채팅에 노래 틀기·대기열 추가", group="🎵 뮤직봇"),
    Cmd(("스킵", "다음곡", "skip", "next"), _music("c_skip"), help="다음 곡으로", group="🎵 뮤직봇"),
    Cmd(("일시정지", "pause"), _music("c_pause"), help="일시정지", group="🎵 뮤직봇"),
    Cmd(("다시재생", "resume"), _music("c_resume"), help="다시 재생", group="🎵 뮤직봇"),
    Cmd(("대기열", "queue"), _music("c_queue"), help="대기열 보기", group="🎵 뮤직봇"),
    Cmd(("빼기", "remove"), _music("c_remove"), usage="<번호>", help="대기열에서 곡 빼기", group="🎵 뮤직봇"),
    Cmd(("음소거", "mmute"), _music("c_mute"), help="노래 소리 끄기 (/mute 는 멤버 뮤트)", group="🎵 뮤직봇"),
    Cmd(("음소거해제", "munmute"), _music("c_unmute"), help="노래 소리 켜기", group="🎵 뮤직봇"),
    Cmd(("이동", "seek"), _music("c_seek"), usage="<초|+초|-초|분:초>", help="곡 안에서 위치 이동", group="🎵 뮤직봇"),
    Cmd(("노래끝", "end"), _music("c_end"), help="노래 끝내고 음성채팅 나가기", group="🎵 뮤직봇"),
    Cmd(("볼륨", "volume", "vol"), _music("c_volume"), usage="<0~200>", help="노래 음량 (%)", group="🎵 뮤직봇"),
    Cmd(("반복", "loop"), _music("c_loop"), usage="<0~10>", help="지금 곡 반복", group="🎵 뮤직봇"),
    Cmd(("지금곡", "np", "nowplaying"), _music("c_now"), help="지금 나오는 곡", group="🎵 뮤직봇"),
    Cmd(("도우미부르기", "userbotjoin"), _music("c_userbotjoin"), help="노래 도우미 계정을 방에 넣기 (관리자)", group="🎵 뮤직봇"),
    Cmd(("뉴스", "news", "세계뉴스"), c_news, usage="[세계|경제|기술|코인|스포츠]",
        help="여러 해외 언론이 함께 다룬 주요 뉴스 (방마다 10분에 1번)", group="뉴스"),
    Cmd(("말투도움말", "말투설명", "말투목록"), c_style_help, help="말투 종류·예시와 방 모드(욕 받아치기·19금)"),
    Cmd(("말투", "style"), c_style, usage="[" + style_list() + "|기본]", help="나에게 쓸 봇 말투", dm_ok=True),
    Cmd(("호칭", "callme"), c_nickname, usage="부를 이름", help="봇이 부를 호칭", dm_ok=True),
    Cmd(("봇정보", "about"), c_about, help="봇 소개", dm_ok=True),
    # 관리자
    Cmd(("설정", "settings"), c_settings, Role.ADMIN, usage="[전체]", help="버튼 설정 메뉴를 1:1로 받기 (전체: 글 목록)",
        group="관리자"),
    Cmd(("설정변경", "set"), c_set, Role.ADMIN, usage="키 값", help="설정 바꾸기", group="관리자"),
    Cmd(("경고", "warn"), c_warn, Role.ADMIN, usage="@user [사유]", help="경고 (누적 시 자동 제재)", group="관리자", right="restrict"),
    Cmd(("경고취소", "unwarn"), c_unwarn, Role.ADMIN, usage="@user", help="경고 1회 취소", group="관리자", right="restrict"),
    Cmd(("경고목록", "warns"), c_warns, Role.ADMIN, usage="@user", help="경고 내역", group="관리자"),
    Cmd(("경고초기화", "resetwarns"), c_resetwarns, Role.ADMIN, usage="@user", help="경고 전부 삭제", group="관리자", right="restrict"),
    Cmd(("뮤트", "mute", "채금", "채팅금지"), c_mute, Role.ADMIN, usage="@user [30m|2h|1d] [사유]", help="채팅 금지", group="관리자", right="restrict"),
    Cmd(("뮤트해제", "unmute", "채금해제", "채금풀기"), c_unmute, Role.ADMIN, usage="@user", help="채팅 금지 해제", group="관리자", right="restrict"),
    Cmd(("free", "프리"), c_free, Role.ADMIN, usage="@user", help="자유 멤버: 제재 풀고 자동 통제 안 받게",
        group="관리자", right="restrict"),
    Cmd(("free해제", "unfree", "프리해제"), c_unfree, Role.ADMIN, usage="@user", help="자유 멤버 해제", group="관리자",
        right="restrict"),
    Cmd(("free목록", "freelist", "프리목록"), c_freelist, Role.ADMIN, help="자유 멤버 목록", group="관리자"),
    Cmd(("밴", "ban", "벤"), c_ban, Role.ADMIN, usage="@user [사유]", help="영구 추방", group="관리자", right="restrict"),
    Cmd(("밴해제", "unban"), c_unban, Role.ADMIN, usage="@user|ID", help="추방 해제", group="관리자", right="restrict"),
    Cmd(("킥", "kick", "강퇴", "강제퇴장"), c_kick, Role.ADMIN, usage="@user", help="내보내기 (재입장 가능)", group="관리자", right="restrict"),
    Cmd(("삭제", "del"), c_del, Role.ADMIN, help="답장한 메시지 삭제", group="관리자", right="delete"),
    Cmd(("청소", "purge"), c_purge, Role.ADMIN, usage="개수", help="최근 메시지 일괄 삭제", group="관리자", right="delete"),
    Cmd(("잠금", "lock"), c_lock, Role.ADMIN, help="방 잠금", group="관리자", right="restrict"),
    Cmd(("잠금해제", "unlock"), c_unlock, Role.ADMIN, help="방 잠금 해제", group="관리자", right="restrict"),
    Cmd(("금지어", "filter"), c_filter, Role.ADMIN, usage="[추가|삭제] 단어", help="금지어 관리", group="관리자"),
    Cmd(("허용도메인", "whitelist"), c_whitelist, Role.ADMIN, usage="[추가|삭제] 도메인", help="링크 허용 목록", group="관리자"),
    Cmd(("AI대화", "ai"), c_ai, Role.ADMIN, usage="켜기|끄기", help="AI 대화 켜고 끄기", group="관리자"),
    Cmd(("인사", "greet"), c_greet, Role.ADMIN, usage="켜기|끄기|설정|테스트", help="입장 인사 설정", group="관리자"),
    Cmd(("규칙설정", "setrules"), c_setrules, Role.ADMIN, usage="내용", help="방 규칙 저장", group="관리자"),
    Cmd(("공지", "notice"), c_notice, Role.ADMIN, usage="내용", help="공지 올리고 고정", group="관리자"),
    Cmd(("예약공지", "schedule"), c_announce, Role.ADMIN, usage="[만들기|수정|미리보기|지금|켜기|끄기|삭제] [번호]",
        help="제목·사진/영상 포함 예약·반복 공지", group="관리자"),
    Cmd(("캡차", "captcha"), c_captcha, Role.ADMIN, usage="[켜기|끄기|시간 N|실패 킥|밴|뮤트]", help="입장 캡차 설정", group="관리자"),
    Cmd(("캡차통과", "approve"), c_captcha_pass, Role.ADMIN, usage="@user", help="캡차 수동 통과", group="관리자", right="restrict"),
    Cmd(("스팸차단", "cas"), c_cas, Role.ADMIN, usage="[켜기|끄기|확인 @user]", help="스팸 명단(CAS·lols) 차단", group="관리자"),
    Cmd(("관리기록", "modlog"), c_modlog, Role.ADMIN, help="최근 제재·설정 기록", group="관리자"),
    Cmd(("사용량", "usage"), c_usage, Role.ADMIN, help="오늘 AI 토큰 (방 관리자는 이 방만)", group="관리자", dm_ok=True),
    # 권한은 함수 안에서 판단: 방에선 관리자만, 1:1 에선 누구나(본인이 관리자인 방만 보여줌)
    Cmd(("구독", "설정하기", "subscribe"), c_subscribe, help="이용 기간 확인·연장 (방 관리자, 1:1 채팅으로 안내)",
        group="관리자", dm_ok=True),
    Cmd(("지식", "자료", "knowledge"), c_knowledge, Role.ADMIN, usage="[추가 제목|검색 단어|삭제 번호]",
        help="AI가 참고할 문서·자료 등록 (1:1 에선 모든 방 공통)", group="관리자", dm_ok=True),
    Cmd(("리포트", "report"), c_report, Role.ADMIN, help="오늘 집계 리포트", group="관리자"),
    Cmd(("전체태그", "tagall"), c_tagall, Role.ADMIN, usage="[할 말]",
        help="방 전체 멤버를 5명씩 태그 (확인 카드 · 6시간에 한 번)", group="관리자"),
    Cmd(("공동차단", "fedban"), c_fedban, Role.ADMIN, usage="@user 사유 | 해제 ID",
        help="사기·스팸 계정을 여러 방 공동 차단 명단에 올리고 이 방에서 내보내기", group="관리자", right="restrict"),
    Cmd(("봇관리자", "botadmin"), c_botadmin, Role.OWNER, usage="[추가|삭제] @user", help="봇 관리자 지정", group="오너"),
    Cmd(("봇프사", "botpic"), c_botpic, Role.OWNER, usage="(영상·사진에 답장) | 원래대로",
        help="소담 프로필을 움직이는 영상·사진으로 (1:1)", group="오너", dm_ok=True),
    Cmd(("백업", "backup"), c_backup, Role.OWNER, usage="[목록]", help="DB 지금 백업 / 백업 목록", group="오너", dm_ok=True),
    Cmd(("AI모델", "airoute"), c_route, Role.OWNER, usage="[나눠|절약|최고] [방ID|전체]",
        help="방마다 AI 모델 길 (작은 모델로 비용 절약)", group="오너", dm_ok=True),
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
    if cmd.right and ctx.chat_id < 0 and not await may(ctx.svc.perms, ctx.bot, ctx.chat_id, ctx.user.id, cmd.right):
        sender = getattr(ctx.msg, "sender_chat", None)
        if sender is not None and sender.id == ctx.chat_id:  # 익명 관리자: 누가 보냈는지 몰라 권한을 확인할 수 없음
            await ctx.reply("익명 관리자로는 이 명령을 쓸 수 없어요. 텔레그램에서 익명 모드를 끄고 다시 해주세요.")
        else:
            await ctx.reply(no_right_text(cmd.right))
        return
    try:
        await cmd.fn(ctx)
    except TelegramError as e:
        log.warning("command %s failed: %s", cmd.names[0], e)
        await ctx.reply(f"처리 중 텔레그램 오류: {esc(e.message)}")
