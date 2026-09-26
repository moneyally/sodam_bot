"""텔레그램 이벤트 처리.

그룹 메시지 흐름:
  기록 → (일반 멤버) CAS·사칭·도배·금지어·링크 검사 → 예약공지 마법사 → 명령어 → 봇 호출이면 AI, 아니면 게임

입장 흐름 (입장 메시지 / 멤버 상태 변경 둘 중 먼저 온 것 1번만):
  CAS 스팸DB → 관리자 사칭 → 캡차 (통과하면 인사) → 인사
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime
from datetime import time as dtime

from openai import OpenAIError
from telegram import Bot, BotCommand, ChatMember, Message, Update, User
from telegram.constants import ChatAction, ChatMemberStatus, ChatType
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

from . import commands, hooks, memory, menu, namehist, security, social, stats, subscription
from .agent import run_agent
from .commands import CmdCtx
from .llm import BudgetExceeded
from .permissions import Role
from .services import Services
from .tools import ToolCtx
from .util import RateLimiter, day_start, esc, iyeyo, mention, user_name  # noqa: F401 (RateLimiter: __main__ 에서 씀)

log = logging.getLogger(__name__)
HISTORY_HOURS = 6
HISTORY_LIMIT = 30
JOIN_DEDUPE_SECONDS = 20  # 입장 메시지와 상태 변경은 몇 초 안에 둘 다 온다


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["svc"]


async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def send_temp(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, seconds: int = 60) -> None:
    """잠깐 보였다가 사라지는 안내 (도배 경고 등으로 방이 지저분해지지 않게)."""
    try:
        sent = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramError as e:
        log.warning("notice send failed: %s", e)
        return
    context.job_queue.run_once(_delete_job, seconds, data=(chat_id, sent.message_id))


_LEADING_MENTIONS = re.compile(r"^(?:@\w{3,32}[\s,]*)+")


def _strip_call(text: str, start: int, name: str) -> str:
    """호출어만 빼고 앞뒤를 이어 붙인다. 문장 끝 물음표 등은 살린다."""
    before = text[:start].rstrip(" ,")
    after = text[start + len(name):].lstrip(" ,.!~?")
    return " ".join(p for p in (before, after) if p) or "(이름만 부름)"


def addressed_to_bot(msg: Message, text: str, call_names: tuple[str, ...], bot) -> tuple[bool, str]:
    """봇을 부른 메시지인지 + 호출어를 뗀 요청문.

    - '소담아 …' / '@kim 소담아 …' (앞에 멘션이 붙어도 OK, 멘션은 요청에 남김)
    - '… 소담아 …' 처럼 중간에 호격('아/야/이'로 끝나는 호출어)으로 부른 경우
    - '@봇아이디' 멘션, 봇 메시지에 답장
    """
    t = text.strip()
    lead = _LEADING_MENTIONS.match(t)
    body_start = lead.end() if lead else 0
    for name in call_names:  # 긴 이름부터
        if not t.startswith(name, body_start):
            continue
        nxt = t[body_start + len(name):body_start + len(name) + 1]
        # '소담스럽다' 처럼 이름 뒤에 글자가 바로 붙으면 부른 게 아님 (단, '소담아' 같은 호격은 OK)
        if nxt and nxt.isalnum() and name[-1] not in "아야이":
            continue
        return True, _strip_call(t, body_start, name)
    for name in call_names:
        if name[-1] not in "아야이":
            continue  # 문장 중간의 '소담'은 그냥 이름 언급일 수 있어서 호격만 인정
        m = re.search(rf"(?<![\w가-힣]){re.escape(name)}(?![\w가-힣])", t)
        if m:
            return True, _strip_call(t, m.start(), name)
    if bot.username and f"@{bot.username}".lower() in t.lower():
        return True, re.sub(re.escape("@" + bot.username), "", t, flags=re.I).strip() or "(이름만 부름)"
    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot.id:
        return True, t
    return False, ""


# ── 입장 처리 ─────────────────────────────────────────────
def _in_chat(member: ChatMember) -> bool:
    if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


def _first_join(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """입장 메시지와 멤버 상태 변경이 둘 다 오므로 한 번만 처리.
    나가거나 강퇴되면 on_chat_member / captcha 에서 즉시 지워서, 재입장 때 다시 검사받게 한다."""
    seen: dict = _svc(context).joins
    now = time.time()
    for key in [k for k, ts in seen.items() if now - ts > JOIN_DEDUPE_SECONDS]:
        del seen[key]
    if (chat_id, user_id) in seen:
        return False
    seen[(chat_id, user_id)] = now
    return True


async def handle_new_member(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str | None, user: User) -> None:
    if user.is_bot or not _first_join(context, chat_id, user.id):
        return
    svc, bot = _svc(context), context.bot
    await svc.db.ensure_chat(chat_id, title)
    await svc.db.upsert_user(user)
    await svc.db.touch_member(chat_id, user.id, joined=True)
    s = await svc.db.get_settings(chat_id)
    if await svc.perms.is_admin(bot, chat_id, user.id):
        return

    if s["cas_enabled"] and await svc.cas.is_banned(user.id):
        await _cas_ban(context, chat_id, user)
        return
    notice = await svc.mod.check_impersonation(bot, chat_id, user)
    if notice:
        await send_temp(context, chat_id, notice, 300)
        return
    if s["captcha_enabled"] and await svc.captcha.start(bot, chat_id, user):
        return  # 인사·입장 기록은 캡차 통과 후
    await svc.db.log_join(chat_id, user.id, user_name(user), user.username)
    if s["greet_enabled"]:
        svc.greeter.queue(bot, chat_id, user.id, user_name(user))


async def _cas_ban(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User) -> None:
    svc = _svc(context)
    try:
        await svc.mod.ban(context.bot, chat_id, user.id, None, "CAS 스팸DB 등록 계정")
    except TelegramError as e:
        log.warning("CAS ban failed: %s", e)
        return
    await send_temp(context, chat_id, f"🛡️ {esc(user_name(user))}님은 CAS 스팸DB에 등록된 계정이라 차단했어요.", 120)
    await svc.mod.report(context.bot, f"[CAS] chat {chat_id} / {esc(user_name(user))}({user.id}) 밴")


async def _cas_background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User) -> None:
    """봇 도입 전부터 있던 멤버도 처음 말할 때 한 번 조회 (메시지 처리를 막지 않게 백그라운드)."""
    svc = _svc(context)
    try:
        if await svc.cas.is_banned(user.id):
            await _cas_ban(context, chat_id, user)
    except Exception:
        log.exception("CAS background check failed")


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    svc = _svc(context)
    for u in msg.new_chat_members:
        await handle_new_member(context, msg.chat_id, msg.chat.title, u)
    if (await svc.db.get_settings(msg.chat_id))["delete_join_message"]:
        try:
            await msg.delete()
        except TelegramError:
            pass


async def _is_admin_safe(svc: Services, bot, chat_id: int, user_id: int, *, fresh: bool = False) -> bool:
    """fresh=True 는 결제 정보를 보여줄지 판단할 때만 (캐시 무시 → getChatAdministrators 호출).
    결제 정보는 텔레그램 관리자·오너에게만 (봇관리자 제외)."""
    try:
        if fresh:
            svc.perms.forget(chat_id)
            return await svc.perms.is_tg_admin(bot, chat_id, user_id)
        return await svc.perms.is_admin(bot, chat_id, user_id)
    except TelegramError:
        return False


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """봇이 방에 초대됐을 때: 무료 체험 시작 + 방엔 짧은 인사와 '⚙️ 봇 설정' 버튼만, 결제 정보는 초대한 사람 1:1 로."""
    cmu = update.my_chat_member
    if not cmu or cmu.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if _in_chat(cmu.old_chat_member) or not _in_chat(cmu.new_chat_member):
        return  # 새로 들어온 게 아님 (권한 변경·강퇴 등)
    svc, bot = _svc(context), context.bot
    chat, adder = cmu.chat, cmu.from_user
    await svc.db.ensure_chat(chat.id, chat.title)
    context.bot_data["chats"].add(chat.id)
    if svc.billing and svc.billing.enabled:
        await svc.billing.ensure_trial(chat.id, adder.id if adder else None)
    days = svc.cfg.trial_days
    intro = (f"👋 안녕하세요, 소통방 AI 비서 {iyeyo(svc.cfg.bot_name)}!\n"
             "원활한 동작을 위해 저를 <b>관리자</b>로 지정해주세요 (메시지 삭제·사용자 차단·고정 권한).\n"
             + (f"지금부터 {days}일 동안 모든 기능을 써보실 수 있어요. " if svc.billing and svc.billing.enabled and days else "")
             + "명령어는 <code>.도움말</code>")
    markup = subscription.setup_button(bot.username, chat.id) if svc.billing and svc.billing.enabled else None
    try:
        await bot.send_message(chat.id, intro, parse_mode="HTML", reply_markup=markup)
    except TelegramError as e:
        log.info("welcome send failed: %s", e)
    # 요금 안내는 초대한 사람이 실제 관리자일 때만 (일반 멤버가 초대한 경우 가격이 새지 않게)
    if adder and svc.billing and svc.billing.enabled and await _is_admin_safe(svc, bot, chat.id, adder.id, fresh=True):
        await subscription.send_panel_dm(svc, bot, chat.id, adder.id)  # 1:1 을 시작 안 했으면 조용히 실패
    await svc.mod.report(bot, f"[봇 초대] {esc(chat.title or '')} ({chat.id}) by "
                              f"{esc(user_name(adder)) if adder else '?'}({adder.id if adder else '?'})")


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """일반 그룹이 슈퍼그룹으로 바뀌면 방 ID 가 바뀐다 → 구독·설정·자료를 새 ID 로 옮김."""
    msg = update.message
    if not msg or not msg.migrate_to_chat_id:
        return  # 새 방 쪽의 migrate_from 메시지는 무시 (한 번만 처리)
    svc = _svc(context)
    old, new = msg.chat_id, msg.migrate_to_chat_id
    await svc.db.migrate_chat(old, new)
    context.bot_data["chats"].discard(old)
    context.bot_data["chats"].add(new)
    svc.perms.forget(old)
    log.info("그룹 전환: %s → %s (데이터 이전 완료)", old, new)
    await svc.mod.report(context.bot, f"[그룹 전환] {old} → {new} 구독·설정·자료를 옮겼어요.")


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """입장 메시지를 숨긴 방에서도 입장을 잡기 위해 멤버 상태 변경을 본다."""
    cmu = update.chat_member
    if not cmu or cmu.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    svc = _svc(context)
    old, new = cmu.old_chat_member, cmu.new_chat_member
    admin_states = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    if old.status != new.status and (old.status in admin_states or new.status in admin_states):
        svc.perms.forget(cmu.chat.id)  # 관리자 목록 캐시 갱신
    if not _in_chat(old) and _in_chat(new):
        await handle_new_member(context, cmu.chat.id, cmu.chat.title, new.user)
    elif _in_chat(old) and not _in_chat(new):
        svc.joins.pop((cmu.chat.id, new.user.id), None)  # 다시 들어오면 캡차·CAS·사칭 검사를 다시 받게
        hooks.member_left(svc, cmu.chat.id, new.user.id)
        await svc.captcha.cancel(context.bot, cmu.chat.id, new.user.id)


# ── 그룹 메시지 ───────────────────────────────────────────
# 관리 검사를 통과한 그룹 메시지마다 백그라운드로 불리는 함수들: hook(svc, bot, msg, role)
# 기능 모듈은 hooks.add_group_message_hook(...) 로 등록한다 (순환 import 방지).
GROUP_MESSAGE_HOOKS = hooks.GROUP_MESSAGE_HOOKS
hooks.add_group_message_hook(social.on_group_message)  # AI 기억 정리·끼어들기 (sodam/social.py)


async def _run_hook(hook, svc: Services, bot, msg: Message, role: Role) -> None:
    try:
        await hook(svc, bot, msg, role)
    except Exception:
        log.exception("group message hook %s failed", getattr(hook, "__name__", hook))


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.from_user:
        return
    svc, bot = _svc(context), context.bot
    chat_id, user = msg.chat_id, msg.from_user
    # 연결된 채널이 자동으로 올린 글 등, 방 자신이 아닌 채널 명의 글은 건너뜀
    if msg.sender_chat and msg.sender_chat.id != chat_id:
        return
    anonymous_admin = msg.sender_chat is not None

    seen: set = context.bot_data["chats"]
    if chat_id not in seen:
        await svc.db.ensure_chat(chat_id, msg.chat.title)
        seen.add(chat_id)
    await svc.db.upsert_user(user)
    await svc.db.touch_member(chat_id, user.id)

    text = msg.text or msg.caption or ""
    role = Role.ADMIN if anonymous_admin else await svc.perms.role(bot, chat_id, user.id)
    scan = security.scan(text)
    if text:
        await svc.db.log_message(chat_id, user.id, msg.message_id, text, flagged=scan.blocked)

    if role < Role.ADMIN:
        s = await svc.db.get_settings(chat_id)
        cas_seen: set = context.bot_data["cas_seen"]
        if s["cas_enabled"] and user.id not in cas_seen:
            cas_seen.add(user.id)
            tasks: set = context.bot_data["tasks"]
            task = asyncio.create_task(_cas_background(context, chat_id, user))
            tasks.add(task)  # 참조를 잡아둬야 도중에 가비지 컬렉션되지 않음
            task.add_done_callback(tasks.discard)
        notice = await svc.mod.check_impersonation(bot, chat_id, user)
        if not notice:
            notice = await svc.mod.check_message(bot, msg, text, game_active=svc.games.is_active(chat_id))
        if notice:
            await send_temp(context, chat_id, notice)
            return

    # 관리 검사를 통과한 메시지 → 백그라운드 후처리 (태그 알림 등). 실패해도 메시지 처리는 계속
    for hook in GROUP_MESSAGE_HOOKS:
        task = asyncio.create_task(_run_hook(hook, svc, bot, msg, role))
        bg: set = context.bot_data.setdefault("tasks", set())
        bg.add(task)
        task.add_done_callback(bg.discard)

    # 예약공지 만들기 진행 중인 관리자의 답변 (사진·영상만 보낸 경우도 여기서 받음)
    if role >= Role.ADMIN and await svc.announcer.handle_message(bot, msg):
        return
    if not text:
        return

    parsed = commands.parse(text, bot.username)
    if parsed:
        cmd, args, argstr = parsed
        await commands.dispatch(CmdCtx(svc, bot, msg, chat_id, user, role, args, argstr), cmd)
        return

    addressed, request = addressed_to_bot(msg, text, svc.cfg.call_names, bot)
    via = "call"
    if not addressed:  # 방금 봇과 얘기하던 사람이 이름 없이 이어서 말한 경우
        addressed, request = await social.follow_up(svc, bot, msg, text)
        via = "follow"
    if addressed:
        await ai_reply(context, msg, role, request, scan, via=via)
    else:
        await svc.games.on_text(msg, text)


async def _within_ai_quota(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, role: Role) -> bool:
    """구독 안 한 방 / 1:1 채팅의 하루 무료 AI 한도. 넘으면 안내하고 False (안내엔 금액을 넣지 않음)."""
    svc = _svc(context)
    billing = svc.billing
    if role >= Role.OWNER:
        return True
    if chat_id < 0:
        # 그룹: 결제 기능이 꺼졌거나 구독(체험) 중이면 제한 없음 (분당 호출 제한은 따로 있음)
        if billing is None or not billing.enabled or await billing.active(chat_id):
            return True
    # 1:1 은 결제 여부와 상관없이 하루 무료 한도 적용 → 낯선 사람이 전체 AI 예산을 다 쓰지 못하게
    key, scope = ("free_ai", chat_id) if chat_id < 0 else ("free_ai_dm", user_id)
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    if await svc.db.bump(day, scope, key) <= svc.cfg.free_ai_per_day:
        return True
    if chat_id < 0:
        try:
            await context.bot.send_message(
                chat_id, "🔒 오늘 무료 AI 이용량을 다 썼어요. 관리자님은 아래 버튼에서 이용 기간을 확인해주세요.",
                reply_markup=subscription.setup_button(context.bot.username, chat_id))
        except TelegramError:
            pass
    else:
        await send_temp(context, chat_id, "오늘 무료 대화량을 다 썼어요. 내일 다시 이야기해요 🙏", 30)
    return False


async def ai_reply(context: ContextTypes.DEFAULT_TYPE, msg: Message, role: Role,
                   request: str, scan: security.ScanResult, via: str = "call") -> None:
    svc, bot = _svc(context), context.bot
    chat_id, user = msg.chat_id, msg.from_user
    s = await svc.db.get_settings(chat_id)
    if not s["ai_enabled"]:
        return
    if not svc.llm.enabled:
        await send_temp(context, chat_id, "🔌 아직 AI 키가 설정되지 않아서 대화는 못 해요. 명령어(.도움말)는 쓸 수 있어요!", 30)
        return

    limiter: RateLimiter = context.bot_data["limiter"]
    if not (limiter.allow(("u", chat_id, user.id), s["user_rate_per_min"])
            and limiter.allow(("r", chat_id), s["room_rate_per_min"])):
        await send_temp(context, chat_id, "⏳ 조금만 천천히 불러주세요!", 10)
        return

    # 인젝션 방어: 1층 규칙 → 애매하거나 긴 요청만 2층 AI 판별
    if s["injection_guard"] and role < Role.OWNER:
        blocked, reason = scan.blocked, ", ".join(scan.hits)
        if not blocked and (scan.suspicious or len(request) > 150):
            blocked, reason = await svc.llm.classify_injection(request)
        if blocked:
            await svc.db.flag_message(chat_id, msg.message_id)  # 이후 AI 맥락에서 제외
            text = "🛡️ 그 요청은 들어드릴 수 없어요."
            if s["injection_warn"] and role < Role.ADMIN and chat_id < 0:  # 경고·제재는 그룹방에서만 (1:1 은 양수 ID)
                text += "\n" + await svc.mod.warn(bot, chat_id, user.id, user_name(user), bot.id,
                                                  f"봇 조작 시도 ({reason.split(',')[0].strip()[:20] or '규칙 위반'})")
            await msg.reply_text(text, parse_mode="HTML")
            await svc.mod.report(bot, f"[인젝션 차단] chat {chat_id} / {esc(user_name(user))}({user.id}): "
                                      f"{esc(request[:200])} / {esc(reason)}")
            return

    if not await _within_ai_quota(context, chat_id, user.id, role):
        return

    await svc.db.log_request(chat_id, user.id, request)
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except TelegramError:
        pass

    member = await svc.db.get_member(chat_id, user.id)
    style = (member["style"] if member else None) or s["style"]
    notes = json.loads(member["notes"]) if member else {}
    history = await svc.db.recent_messages(chat_id, HISTORY_LIMIT, since=int(time.time()) - HISTORY_HOURS * 3600)
    history = [h for h in history if h["msg_id"] != msg.message_id]  # 지금 요청은 <request> 로 따로 넣음

    reply_to = None
    r = msg.reply_to_message
    if r and r.from_user and (r.text or r.caption):  # 봇 답에 답장한 경우도 '어느 답'인지 알려줌
        who = f"{svc.cfg.bot_name}(봇)" if r.from_user.id == bot.id else f"{user_name(r.from_user)}({r.from_user.id})"
        reply_to = f"{who}: {(r.text or r.caption)[:500]}"
    if chat_id > 0 and s.get("ai_memory", True):
        memory.observe(svc, chat_id, user.id, request)  # 1:1 은 그룹 훅이 없어서 여기서 기억 후보 확인

    ctx = ToolCtx(svc, bot, chat_id, user, role, s)
    try:
        answer = await run_agent(ctx, style_key=style, notes=notes, history=history,
                                 reply_to=reply_to, request=request, mode=via)
    except BudgetExceeded:
        answer = "오늘 AI 사용량을 다 써서 내일 다시 불러주세요 🙏"
    except OpenAIError as e:
        log.warning("openai error: %s", e)
        answer = "AI 연결이 잠깐 불안정해요. 잠시 후 다시 불러주세요."

    usernames = {row["username"].lower() for row in await svc.db.member_names(chat_id) if row["username"]}
    out = security.filter_output(answer, max_chars=s["reply_max_chars"], allowed_usernames=usernames)
    body = esc(out)
    if ctx.mentions:
        body = " ".join(mention(uid, name) for uid, name in dict(ctx.mentions).items()) + " " + body
    sent = await msg.reply_text(body, parse_mode="HTML")
    await svc.db.log_message(chat_id, bot.id, sent.message_id, out, is_bot=True)
    await memory.record_turn(svc.db, chat_id, user.id, via, request, out, sent.message_id)  # 이어 말하기·'아까 그거'용


# ── 버튼 ──────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    svc, bot = _svc(context), context.bot
    data = q.data or ""
    prefix, _, rest = data.partition(":")
    parts = rest.split(":") if rest else []
    if prefix == "qz":
        await svc.db.upsert_user(q.from_user)
        await svc.games.on_callback(q, parts)
    elif prefix == "cap":
        await svc.captcha.on_callback(bot, q, parts)
    elif prefix == "an":
        await svc.announcer.on_callback(bot, q, parts)
    elif prefix == "pay":
        await subscription.on_callback(svc, bot, q, parts)
    elif prefix == "m":
        await menu.on_callback(svc, bot, q, parts)
    elif prefix == "nh":
        await namehist.on_callback(svc, bot, q, parts)
    elif prefix == "act":
        await _confirm_action(svc, bot, q, parts)
    else:
        await q.answer()


async def _confirm_action(svc: Services, bot: Bot, q, parts: list[str]) -> None:
    key, yn = (parts + ["", ""])[:2]
    action = svc.pending.get(key)
    if not action or action.expires < time.time():
        svc.pending.pop(key, None)
        await q.answer("만료된 요청이에요.")
        await q.edit_message_reply_markup(None)
        return
    if not await svc.perms.is_admin(bot, action.chat_id, q.from_user.id):
        await q.answer("관리자만 누를 수 있어요.", show_alert=True)
        return
    svc.pending.pop(key, None)
    await q.answer()
    if yn != "y":
        await q.edit_message_text("취소했어요.")
        return
    # 버튼이 떠 있는 동안 대상이 관리자가 됐을 수도 있으니 다시 확인
    if await svc.perms.protected(bot, action.chat_id, action.target_id):
        await q.edit_message_text("대상이 관리자라서 내보낼 수 없어요.")
        return
    try:
        await svc.mod.ban(bot, action.chat_id, action.target_id, q.from_user.id, action.reason)
        await q.edit_message_text(f"🚫 {mention(action.target_id, action.target_name)}님을 내보냈어요. "
                                  f"(처리: {esc(user_name(q.from_user))})", parse_mode="HTML")
    except TelegramError as e:
        await q.edit_message_text(f"실패했어요: {esc(e.message)}")


_LOOKUP_ONLY = re.compile(r"@[A-Za-z0-9_]{3,32}|\d{5,15}")
_OWNER_CMD = re.compile(r"^[./](owner|오너)(@\w+)?\s+(\d{8})\s*$", re.I)
_DEEP_LINK = re.compile(r"^/start\s+(sub|cfg)_(-\d{5,18})\s*$")


async def on_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """1:1 채팅 순서: 딥링크·/start·/owner → 예약공지 마법사 → 메뉴 글자 입력 → 명령어 → AI."""
    msg = update.message
    svc, bot = _svc(context), context.bot
    user = msg.from_user
    text = (msg.text or msg.caption or "").strip()
    if not user:
        return
    await svc.db.ensure_chat(msg.chat_id, None)
    await svc.db.upsert_user(user)
    await svc.db.touch_member(msg.chat_id, user.id)

    deep = _DEEP_LINK.match(text)
    if deep:  # 방의 '⚙️ 봇 설정' 버튼 / .설정 으로 들어온 경우 → 그 방 설정 패널 (관리자만)
        chat_id = int(deep.group(2))
        # 패널 자체엔 결제 정보가 없어서 캐시로 판단 (💳 버튼은 누를 때 새로 확인)
        if not await _is_admin_safe(svc, bot, chat_id, user.id):
            # 일반 멤버: 거절 대신 평범한 메인 메뉴 (결제·관리 관련 내용 없이)
            text_, kb = await menu.main_menu(svc, bot, user.id)
            await msg.reply_text("그 버튼은 방 관리자용이에요.\n\n" + text_, parse_mode="HTML", reply_markup=kb)
            return
        svc.inputs.pop(user.id, None)
        text_, kb = await menu.group_panel(svc, bot, chat_id, user.id)
        await menu.send_panel(svc, bot, user.id, lambda: msg.reply_text(text_, parse_mode="HTML", reply_markup=kb))
        return
    if re.match(r"^/start(@\w+)?\s*$", text) or text in ("/menu", ".메뉴"):
        svc.inputs.pop(user.id, None)
        text_, kb = await menu.main_menu(svc, bot, user.id)
        await menu.send_panel(svc, bot, user.id, lambda: msg.reply_text(text_, parse_mode="HTML", reply_markup=kb))
        return

    m = _OWNER_CMD.match(text)
    if m:
        if await svc.perms.claim(user.id, m.group(3)):
            await msg.reply_text("👑 오너로 등록됐어요!\n/start → 👑 오너 메뉴에서 전체 방 현황·매출을 볼 수 있고, "
                                 "자동 차단·신고 같은 알림도 여기로 와요.")
            log.info("오너 등록 완료: %s", user.id)
        else:
            await msg.reply_text("🔒 봇 운영자 전용 기능이에요.")
        return

    if re.match(r"^[./](owner|오너)(@\w+)?\s*$", text, re.I):
        if user.id in await svc.perms.owners():
            await msg.reply_text("👑 이미 오너로 등록돼 있어요. /start → 👑 오너 메뉴")
        else:
            await msg.reply_text("🔒 봇 운영자 전용 기능이에요.")
        return

    # 진행 중인 입력 흐름 (사진·영상만 온 메시지도 여기까지 전달)
    if await svc.announcer.handle_message(bot, msg):
        return
    if await menu.handle_input(svc, bot, msg):
        return
    if _LOOKUP_ONLY.fullmatch(text):  # @아이디나 숫자 ID 만 보내면 전체 기록
        role = await svc.perms.role(bot, msg.chat_id, user.id)
        await commands.name_lookup_forward(CmdCtx(svc, bot, msg, msg.chat_id, user, role, [text], text), mode="all")
        return
    if getattr(msg, "forward_origin", None) is not None:  # 전달된 메시지 → 보낸 사람 이름 기록
        role = await svc.perms.role(bot, msg.chat_id, user.id)
        await commands.name_lookup_forward(CmdCtx(svc, bot, msg, msg.chat_id, user, role, [], ""))
        return
    if not text:
        return

    role = await svc.perms.role(bot, msg.chat_id, user.id)
    parsed = commands.parse(text, bot.username)
    if parsed:
        cmd, args, argstr = parsed
        if cmd.dm_ok:
            await commands.dispatch(CmdCtx(svc, bot, msg, msg.chat_id, user, role, args, argstr), cmd)
        else:
            await msg.reply_text(f"'{cmd.names[0]}' 은(는) 봇을 넣은 그룹방에서 쓰는 명령어예요. 1:1 에선 .도움말 을 참고해주세요.")
        return
    if text.startswith(("/", ".")):
        await msg.reply_text(f"안녕하세요, 소통방 AI 비서 {iyeyo(svc.cfg.bot_name)} 🙌 그냥 말 걸면 대화할 수 있어요. 명령어는 .도움말")
        return

    scan = security.scan(text)
    await svc.db.log_message(msg.chat_id, user.id, msg.message_id, text, flagged=scan.blocked)
    await ai_reply(context, msg, role, text, scan)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 잠깐 끊겼다 다시 붙는 네트워크 오류는 봇이 자동 재시도하므로 한 줄만 남긴다
    if isinstance(context.error, (NetworkError, TimedOut)):
        log.warning("텔레그램 연결 일시 오류 (자동 재시도): %s", context.error)
        return
    log.error("unhandled error", exc_info=context.error)


async def on_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """모든 업데이트 맨 앞(그룹 -1): 보이는 사람 이름·아이디 기록 (이름 변경 기록 범위를 최대로)."""
    try:
        await namehist.observe(_svc(context), context.bot, update)
    except Exception:
        log.exception("name observe failed")


async def job_name_sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    """1분마다: 말 안 하는 멤버도 이름을 바꿨는지 조금씩 확인."""
    try:
        await namehist.sweep(_svc(context), context.bot)
    except Exception:
        log.exception("name sweep failed")


# ── 예약 작업 ─────────────────────────────────────────────
async def job_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """30초마다: 캡차 시간 초과 처리, 예약공지 발송, 대기 중인 결제 확인."""
    svc = _svc(context)
    jobs = [("captcha", svc.captcha.expire), ("announce", svc.announcer.run_due)]
    if svc.billing and svc.billing.enabled:
        jobs.append(("billing", lambda bot: subscription.run_check(svc, bot)))
    for name, fn in jobs:
        try:
            await fn(context.bot)
        except Exception:  # 한쪽 실패가 다른 쪽을 막지 않게
            log.exception("tick %s failed", name)


async def job_sports(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    await svc.sports.run_alerts(context.bot, is_active=svc.paid_features)


async def job_sub_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """매일 10시: 3일 안에 끝나는 방 / 어제 끝난 방에 안내 (방엔 금액 없이 '봇 설정' 버튼만)."""
    svc, bot = _svc(context), context.bot
    if not (svc.billing and svc.billing.enabled):
        return
    now = int(time.time())
    for row in await svc.db.subscriptions_expiring(now - 86400, now + 3 * 86400):
        chat_id, until = row["chat_id"], row["until"]
        if until > now:
            days = max(1, (until - now + 86399) // 86400)
            text = f"⏳ 이 방의 소담 이용 기간이 {days}일 남았어요. 관리자님은 아래 버튼에서 연장할 수 있어요."
        else:
            text = "⛔ 소담 이용 기간이 끝나서 AI 대화·게임·예약공지가 멈췄어요. 방 관리 기능은 계속 동작해요."
        try:
            await bot.send_message(chat_id, text, reply_markup=subscription.setup_button(bot.username, chat_id))
        except TelegramError as e:
            log.info("sub reminder failed %s: %s", chat_id, e)
        if row["added_by"] and await _is_admin_safe(svc, bot, chat_id, row["added_by"], fresh=True):
            await subscription.send_panel_dm(svc, bot, chat_id, row["added_by"])


async def job_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    for chat_id in await svc.db.all_chat_ids():
        s = await svc.db.get_settings(chat_id)
        if not s["daily_report"] or chat_id > 0 or not await svc.paid_features(chat_id):
            continue
        totals = await svc.db.chat_totals(chat_id, day_start(svc.cfg.tz))
        if not totals or totals["messages"] < 10:
            continue
        text = ("🌙 <b>오늘의 소통방 리포트</b>\n" +
                await stats.summary_text(svc.db, chat_id, svc.cfg.tz, "오늘") + "\n\n" +
                await stats.ranking_text(svc.db, chat_id, svc.cfg.tz, "오늘", 5) +
                "\n\n오늘도 수고 많으셨습니다, 대표님들!")
        try:
            await context.bot.send_message(chat_id, text, parse_mode="HTML")
        except TelegramError as e:
            log.warning("daily report failed for %s: %s", chat_id, e)


async def job_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    try:
        path = await svc.backup.run()
    except Exception as e:
        log.exception("backup failed")
        await svc.mod.report(context.bot, f"❌ DB 백업 실패: {esc(str(e))}")
        return
    log.info("backup ok: %s", path)
    if svc.cfg.backup_send_to_log and svc.cfg.log_chat_id:
        try:
            with open(path, "rb") as f:
                await context.bot.send_document(svc.cfg.log_chat_id, f, filename=path.name,
                                                caption="🗄️ 소담 DB 자동 백업")
        except TelegramError as e:
            log.warning("backup upload failed: %s", e)


async def job_prune(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _svc(context).db.prune_messages(int(time.time()) - 90 * 86400)


BOT_MENU = [
    BotCommand("help", "명령어 목록"),
    BotCommand("rank", "채팅 랭킹"),
    BotCommand("stats", "방 통계"),
    BotCommand("search", "대화 검색"),
    BotCommand("game", "게임 시작"),
    BotCommand("points", "게임 포인트 랭킹"),
    BotCommand("sports", "스포츠 일정·결과"),
    BotCommand("style", "봇 말투 바꾸기"),
    BotCommand("me", "내 정보"),
    BotCommand("history", "이름·아이디 변경 기록 (답장·@아이디·ID)"),
    BotCommand("allhistory", "변경 기록 전체"),
    BotCommand("rules", "방 규칙"),
]


def register(app: Application, tz, backup_time: str = "05:00") -> None:
    groups = filters.ChatType.GROUPS
    app.add_handler(TypeHandler(Update, on_any_update), group=-1)  # 이름 기록 (다른 처리보다 먼저, 막지 않음)
    # 반응·가입 요청·수정된 메시지는 기록만 하면 돼서 별도 처리 없음 (TypeHandler 가 봄)
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(MessageHandler(groups & filters.UpdateType.MESSAGE & ~filters.StatusUpdate.ALL, on_group_message))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, on_private))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    jq = app.job_queue
    hh, mm = map(int, backup_time.split(":"))
    jq.run_repeating(job_tick, interval=30, first=10, name="tick")
    jq.run_repeating(job_name_sweep, interval=60, first=90, name="name_sweep")
    jq.run_repeating(job_sports, interval=600, first=60, name="sports")
    jq.run_daily(job_daily_report, time=dtime(23, 50, tzinfo=tz), name="daily_report")
    jq.run_daily(job_backup, time=dtime(hh, mm, tzinfo=tz), name="backup")
    jq.run_daily(job_prune, time=dtime(4, 0, tzinfo=tz), name="prune")
    jq.run_daily(job_sub_reminders, time=dtime(10, 0, tzinfo=tz), name="sub_reminders")
