"""텔레그램 이벤트 처리.

그룹 메시지 흐름:
  기록 → (일반 멤버) CAS·사칭·도배·금지어·링크 검사 → 예약공지 마법사 → 명령어 → 봇 호출이면 AI, 아니면 게임

입장 흐름 (입장 메시지 / 멤버 상태 변경 둘 중 먼저 온 것 1번만):
  스팸 명단(CAS·lols) → 관리자 사칭 → 캡차 (통과하면 인사) → 인사
"""
import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from datetime import time as dtime

from openai import OpenAIError
from telegram import (Bot, BotCommand, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                      ReplyParameters, Update, User)
from telegram.constants import ChatAction, ChatMemberStatus, ChatType
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

from . import (accountage, addressee, anomaly, cards, casino, channel, cleanup, commands, diskguard, farewell, free, gametime, hooks, joinreq, memory, menu, namehist, news, persist, raid, reports, rules, security, semsearch, social,
               stats, subscription, vision)
from .cas import ALLOW_KEY, blocks as cas_blocks
from . import addguard, agent, aiqueue, apikeys, subgate
from . import autoreply, leavelock, mediastore, medialog, modactions

from .agent import run_agent
from .db import disk_full
from .moderation import owner_kb
from .panels import members as members_panel
from .commands import CmdCtx
from .llm import UNVERIFIED, BudgetExceeded, out_of_credit
from .permissions import Role, may, no_right_text
from .services import Services
from .tools import ToolCtx
from .util import RateLimiter, day_start, send_retry, surely_unsent, esc, human_minutes, is_stale, iyeyo, mention, sent_at, to_int, user_name  # noqa: F401 (RateLimiter: __main__ 에서 씀)

log = logging.getLogger(__name__)
autoreply.register_commands()   # .reply 명령 (commands → menu → panels 순환이라 여기서 등록)
HISTORY_HOURS = 6
HISTORY_LIMIT = 30
JOIN_DEDUPE_SECONDS = 20  # 입장 메시지와 상태 변경은 몇 초 안에 둘 다 온다


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["svc"]


async def _delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id, message_id = context.job.data
    svc = context.bot_data.get("svc") if isinstance(context.bot_data, dict) else None
    await persist.delete_now(svc.db if svc else None, context.bot, chat_id, message_id)


async def delete_after(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, seconds: float) -> None:
    """seconds 뒤 지움: job_queue 로 제때 + DB(temp_msgs)에도 적어 그 사이 재시작돼도 persist.sweep 이 지움."""
    svc = context.bot_data.get("svc") if isinstance(context.bot_data, dict) else None
    if svc is not None:
        try:
            await persist.remember_delete(svc.db, context.bot, chat_id, message_id, seconds)
        except Exception as e:   # 기록 실패(디스크 가득 참 등)여도 지우기는 예약
            log.warning("temp message not recorded: %s", e)
    context.job_queue.run_once(_delete_job, seconds, data=(chat_id, message_id))


BURST_SECONDS = 1.5      # 같은 사람이 이 안에 연달아 보낸 말은 한 번에 답 (마지막 메시지에, 앞의 말도 함께 읽고)
_BURSTS: dict[tuple[int, int], list[tuple[int, str]]] = {}
MUTE_NOTICE_TTL = 600  # 자동 채팅 금지 안내는 관리자가 [풀기] 버튼을 볼 수 있게 10분


def unmute_kb(notice: str) -> InlineKeyboardMarkup | None:
    """자동 채팅 금지 안내(moderation.Notice.muted)면 관리자용 [🔊 채팅 금지 풀기] 버튼."""
    muted = getattr(notice, "muted", None)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔊 채팅 금지 풀기 (관리자)", callback_data=f"um:{muted}")]]) \
        if muted else None


async def send_temp(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, seconds: int = 60,
                    reply_markup=None) -> None:
    """잠깐 보였다가 사라지는 안내 (도배 경고 등으로 방이 지저분해지지 않게).
    자동 채팅 금지 안내(moderation.Notice.muted)엔 관리자용 [🔊 채팅 금지 풀기] 버튼을 붙인다."""
    if reply_markup is None and (reply_markup := unmute_kb(text)):
        seconds = max(seconds, MUTE_NOTICE_TTL)
    try:
        sent = await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramError as e:
        log.warning("notice send failed: %s", e)
        return
    await delete_after(context, chat_id, sent.message_id, seconds)


_LEADING_MENTIONS = re.compile(r"^(?:@\w{3,32}[\s,]*)+")
# 이름 바로 뒤에 붙여 쓴 욕 ('소담이개보지련아'·'소담이씨발') — 3인칭 '소담이가' 와 구분해 호출로 봄
_INSULT_HEAD = re.compile(r"(개[^가-힣\s]?|개[가-힣]|씨|시발|병신|좆|존나|미친|ㅅㅂ|ㅆㅂ|ㅂㅅ|ㅄ|새끼|십|썅|지랄)")
# 문장 중간의 '소담이' 는 보통 3인칭('우리 소담이 최고') → 끝이 부탁일 때만 부른 걸로 본다
_ASK_TAIL = re.compile(r"(줘|줄래|주라|주세요|줄래요|해봐|봐봐|부탁(해|해요|드려요|합니다)?)[\s.!~?]*$")
# '소담이도 참여 ㄱㄱ'·'소담도 해봐' — '도' 가 붙어도 끝이 부탁·권유면 부른 것 (2026-10-06 베베 오너: 답 안 나옴, 3인칭으로 봤음)
_ALSO_TAIL = re.compile(r"(줘|줄래|주라|주세요|해봐|봐봐|해|해라|하자|가자|와|ㄱㄱ*|ㄲ|고고|참여|참가|ㄱㄱ해)[\s.!~?ㅋㅎ]*$")


def _strip_call(text: str, start: int, name: str) -> str:
    """호출어만 빼고 앞뒤를 이어 붙인다. 문장 끝 물음표 등은 살린다."""
    before = text[:start].rstrip(" ,")
    after = text[start + len(name):].lstrip(" ,.!~?")
    return " ".join(p for p in (before, after) if p) or "(이름만 부름)"


def addressed_to_bot(msg: Message, text: str, call_names: tuple[str, ...], bot,
                     reply_is_ai: bool = True) -> tuple[bool, str]:
    """봇을 부른 메시지인지 + 호출어를 뗀 요청문.

    - '소담아 …' / '@kim 소담아 …' (앞에 멘션이 붙어도 OK, 멘션은 요청에 남김)
    - '… 소담아 …' 처럼 중간에 호격('아/야')으로 부른 경우, 중간의 '소담이 …해줘' 처럼 끝이 부탁인 경우
    - '@봇아이디' 멘션, 봇 메시지에 답장 (reply_is_ai=False 면 AI 답이 아닌 봇 메시지라 호출 아님)
    - '소담이가 틀렸네'·'소담이는 왜 저래' 처럼 조사가 바로 붙은 건 3인칭 언급이라 호출 아님
    """
    t = text.strip()
    lead = _LEADING_MENTIONS.match(t)
    body_start = lead.end() if lead else 0
    for name in call_names:  # 긴 이름부터
        if not t.startswith(name, body_start):
            continue
        nxt = t[body_start + len(name):body_start + len(name) + 1]
        # '소담스럽다'·'소담이가/는/랑' 처럼 이름 뒤에 글자가 바로 붙으면 부른 게 아님 (단, '소담아…' 같은 호격은 OK)
        if nxt and nxt.isalnum() and name[-1] not in "아야" and not (name[-1] == "이" and nxt == "야") \
                and not _INSULT_HEAD.match(t, body_start + len(name)):   # '소담이개…'·'소담이씨발…' = 붙여 쓴 욕 호출 (실제 사례 일루왕)
            continue
        return True, _strip_call(t, body_start, name)
    if _ALSO_TAIL.search(t):
        for name in call_names:
            if name[-1] in "아야":
                continue
            m = re.search(rf"(?<![\w가-힣]){re.escape(name)}도(?![\w가-힣])", t)
            if m:
                return True, _strip_call(t, m.start(), name + "도")
    for name in call_names:
        if name[-1] not in "아야이":
            continue  # 문장 중간의 '소담'은 그냥 이름 언급일 수 있어서 호격만 인정
        m = re.search(rf"(?<![\w가-힣]){re.escape(name)}(?![\w가-힣])", t)
        if m and (name[-1] != "이" or _ASK_TAIL.search(t)):
            return True, _strip_call(t, m.start(), name)
    if bot.username and f"@{bot.username}".lower() in t.lower():
        return True, re.sub(re.escape("@" + bot.username), "", t, flags=re.I).strip() or "(이름만 부름)"
    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot.id and reply_is_ai:
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


def join_how(cmu) -> str:
    """멤버 상태 변경(chat_member)에서 어떻게 들어왔는지 (가입 신청 승인·초대링크 이름·폴더 링크)."""
    if getattr(cmu, "via_join_request", False):
        return "가입 신청 승인"
    link = getattr(cmu, "invite_link", None)
    if link is not None:
        return "초대링크 " + (getattr(link, "name", None) or (getattr(link, "invite_link", "") or "")[-10:] or "?")
    if getattr(cmu, "via_chat_folder_invite_link", False):
        return "폴더 링크"
    return ""


async def handle_new_member(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str | None, user: User,
                            by: User | None = None, how: str = "") -> None:
    """by = 텔레그램이 알려 준 '들어오게 한 사람' (직접 입장이면 본인, 추가·승인이면 그 사람). how = join_how."""
    if user.is_bot or not _first_join(context, chat_id, user.id):
        return
    svc, bot = _svc(context), context.bot
    # 입장 기록: 누가·언제·어떻게 (2026-10-07 백악관 — 강제 추가·구독 확인을 나중에 DB 로 추적)
    parts = [how] if how else []
    if by is not None and by.id != user.id:
        parts.append(f"{'승인' if 'request' in how or '신청' in how else '추가'}: {user_name(by)}({by.id})")
    elif not how:
        parts.append("직접 입장 (공개 링크·아이디)")
    try:
        await svc.db.log_mod(chat_id, by.id if by else None, user.id, "join_info",
                             f"{user_name(user)} @{user.username or '-'} · " + " · ".join(parts))
    except Exception:
        log.exception("join info log failed")
    await svc.db.ensure_chat(chat_id, title)
    await svc.db.upsert_user(user)
    await svc.db.touch_member(chat_id, user.id, joined=True)
    await members_panel.mark(svc.db, chat_id, user.id, left=False)
    s = await svc.db.get_settings(chat_id)
    if await svc.perms.is_admin(bot, chat_id, user.id):
        return
    if await addguard.check(svc, bot, chat_id, user, by, s):   # 관리자 아닌 사람이 강제로 추가 (sodam/addguard.py)
        return
    # 관리 권한 없는 방·자유 멤버(sodam/free.py): 검사 없이 기록·인사만
    if not await svc.perms.bot_can_moderate(bot, chat_id) or await free.is_free(svc.db, chat_id, user.id):
        await svc.db.log_join(chat_id, user.id, user_name(user), user.username)
        if s["greet_enabled"]:
            svc.greeter.queue(bot, chat_id, user.id, user_name(user))
        return

    if await cas_blocks(svc, chat_id, user.id, s):
        await _cas_ban(context, chat_id, user)
        return
    if await hooks.member_joined(svc, bot, chat_id, user):   # 공동 차단 명단·대량 입장 방어 등 (sodam/hooks.py)
        return
    notice = await svc.mod.check_impersonation(bot, chat_id, user)
    if notice:
        await send_temp(context, chat_id, notice, 300)
        return
    # 대량 입장 방어 중(sodam/raid.py)·최근 만든 계정(sodam/accountage.py)은 캡차 설정과 상관없이 캡차.
    # 가입 신청 1:1 확인을 통과하고 들어온 사람(sodam/joinreq.py)은 이미 확인했으니 생략
    if not await joinreq.passed(svc, chat_id, user.id) and (s["captcha_enabled"] or await raid.active(svc, chat_id)
            or (s["recent_account_captcha"] and accountage.is_recent(user.id))) \
            and await svc.captcha.start(bot, chat_id, user):
        return  # 인사·입장 기록은 캡차 통과 후
    await svc.db.log_join(chat_id, user.id, user_name(user), user.username)
    if s["greet_enabled"]:
        svc.greeter.queue(bot, chat_id, user.id, user_name(user))
    await subgate.on_join(svc, bot, chat_id, user, s)   # 채널 구독 필수 (선택) — 구독 전엔 채팅 금지 + 안내


async def _cas_ban(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User) -> None:
    svc = _svc(context)
    try:
        await svc.mod.ban(context.bot, chat_id, user.id, None, "CAS·lols 스팸 명단 등록 계정")
    except TelegramError as e:
        log.warning("CAS ban failed: %s", e)
        return
    await send_temp(context, chat_id, f"🛡️ {esc(user_name(user))}님은 스팸 계정 명단(CAS·lols)에 등록된 계정이라 차단했어요.", 600,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ 차단 풀기", callback_data=f"cas:{user.id}"),
                                                        InlineKeyboardButton("🔕 스팸 명단 차단 끄기", callback_data="cas:off")]]))
    await svc.mod.incident(context.bot, chat_id, "cas", f"[CAS] chat {chat_id} / {esc(user_name(user))}({user.id}) 밴",
                           owner_kb(chat_id, user.id, "ban"), sub=user.id, label=user_name(user))


async def _cas_background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User) -> None:
    """봇 도입 전부터 있던 멤버도 처음 말할 때 한 번 조회 (메시지 처리를 막지 않게 백그라운드)."""
    svc = _svc(context)
    try:
        if await cas_blocks(svc, chat_id, user.id, await svc.db.get_settings(chat_id)):
            await _cas_ban(context, chat_id, user)
    except Exception:
        log.exception("CAS background check failed")


async def _cas_button(svc: Services, bot: Bot, q, parts: list[str]) -> None:
    """스팸 명단 차단 안내의 [↩️ 차단 풀기] (이 방에선 다시 안 막음) · [🔕 스팸 명단 차단 끄기]. '사용자 차단' 권한 관리자만."""
    chat_id, presser, arg = q.message.chat_id, q.from_user.id, (parts or [""])[0]
    if not await may(svc.perms, bot, chat_id, presser):
        await q.answer(no_right_text(), show_alert=True)
        return
    if arg == "off":
        await svc.db.set_setting(chat_id, "cas_enabled", False)
        await svc.db.log_mod(chat_id, presser, None, "setting", "cas_enabled=False")
        await q.answer("🔕 껐어요. 다시 켜기: .스팸차단 켜기 또는 1:1 메뉴 🚪 입장·인사", show_alert=True)
        done = "🔕 스팸 명단 차단을 껐어요"
    elif uid := to_int(arg):
        try:
            await svc.mod.unban(bot, chat_id, uid, presser)
        except TelegramError as e:
            await q.answer(f"실패했어요: {e.message[:80]}", show_alert=True)
            return
        await svc.db.set_state(chat_id, ALLOW_KEY.format(uid), 1)
        await q.answer("↩️ 풀었어요. 이 방에선 다시 막지 않아요.")
        done = "↩️ 차단을 풀었어요"
    else:
        await q.answer()
        return
    try:
        await q.edit_message_text(f"{q.message.text_html}\n{done} (처리: {esc(user_name(q.from_user))})", parse_mode="HTML")
    except TelegramError:
        pass


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """가입 신청 → 1:1 그림 버튼 확인 (sodam/joinreq.py)."""
    try:
        req = update.chat_join_request
        if req.chat.type == ChatType.CHANNEL:   # 채널 가입 신청 (sodam/channel.py)
            return await channel.on_join_request(_svc(context), context.bot, req)
        await joinreq.on_request(_svc(context), context.bot, req)
    except Exception:
        log.exception("join request failed")


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None:   # 입장 서비스 메시지가 '수정됨'으로 다시 오면(edited_message) 필터는 통과하지만 이미 처리한 입장 → 무시
        return
    svc = _svc(context)
    for u in msg.new_chat_members:
        await handle_new_member(context, msg.chat_id, msg.chat.title, u, msg.from_user)
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
    if cmu and cmu.chat.type == ChatType.CHANNEL:   # 채널: 등록·권한 체크리스트 (sodam/channel.py)
        return await channel.on_bot_status(_svc(context), context.bot, cmu)
    if not cmu or cmu.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    _svc(context).perms.forget_bot(cmu.chat.id)  # 봇 권한이 바뀌었을 수 있음 (관리자 지정·해제)
    if _in_chat(cmu.old_chat_member):
        if _in_chat(cmu.new_chat_member):
            await _bot_rights_changed(context, cmu)
        else:
            await _bot_removed(context, cmu)
        return
    if not _in_chat(cmu.new_chat_member):
        return
    svc, bot = _svc(context), context.bot
    chat, adder = cmu.chat, cmu.from_user
    await svc.db.ensure_chat(chat.id, chat.title)
    context.bot_data["chats"].add(chat.id)
    if svc.billing and svc.billing.enabled:
        await svc.billing.ensure_trial(chat.id, adder.id if adder else None)
    days = svc.cfg.trial_days
    # 다시 초대한 방엔 체험이 다시 생기지 않음 → 지금 막 시작한 체험일 때만 '3일 무료' (끝난 방에 약속하던 것)
    st = await svc.billing.status(chat.id) if svc.billing and svc.billing.enabled else None
    in_trial = bool(st and st.state == "trial" and st.until and st.until - time.time() > (days - 1) * 86400)
    intro = (f"👋 안녕하세요, 소통방 AI 비서 {iyeyo(svc.cfg.bot_name)}!\n"
             "원활한 동작을 위해 저를 <b>관리자</b>로 지정해주세요 (메시지 삭제·사용자 차단·고정 권한).\n"
             "🕵️ 이제 멤버가 이름·@아이디를 바꾸면 알려드려요. 누구든 <code>.기록</code> 으로 변경 기록을 볼 수 있어요.\n"
             + (f"지금부터 {days}일 동안 모든 기능을 써보실 수 있어요. " if in_trial and days else "")
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


_NEEDED_RIGHTS = (("can_delete_messages", "메시지 삭제"), ("can_restrict_members", "사용자 차단"))


async def _bot_rights_changed(context: ContextTypes.DEFAULT_TYPE, cmu) -> None:
    """관리자로 지정됐거나 관리자 권한이 바뀌면: 방 관리에 필요한 권한이 다 있는지 잠깐 알려줌."""
    old, new = cmu.old_chat_member, cmu.new_chat_member
    if new.status != ChatMemberStatus.ADMINISTRATOR:
        return
    have = [bool(getattr(new, k, False)) for k, _ in _NEEDED_RIGHTS]
    if old.status == ChatMemberStatus.ADMINISTRATOR and have == [bool(getattr(old, k, False)) for k, _ in _NEEDED_RIGHTS]:
        return  # 방 관리와 상관없는 권한만 바뀜
    missing = [label for (_, label), ok in zip(_NEEDED_RIGHTS, have) if not ok]
    if missing:
        text = (f"⚠️ 관리자 권한 확인! 그런데 {'·'.join(missing)} 권한이 빠져 있어서 도배·링크 정리와 제재를 못 해요. "
                "관리자 설정에서 켜주세요.")
    else:
        text = "✅ 관리자 권한 확인! 이제 도배·링크 정리, 캡차, 경고·뮤트 같은 방 관리를 할게요."
    await send_temp(context, cmu.chat.id, text, 120)


async def _bot_removed(context: ContextTypes.DEFAULT_TYPE, cmu) -> None:
    """봇이 강퇴되거나 나가면: 그 방 예약공지를 끄고(보낼 수 없는 방에 계속 시도하지 않게) 오너에게 한 줄 알림."""
    svc, chat, by = _svc(context), cmu.chat, cmu.from_user
    n = await svc.db.disable_schedules(chat.id)
    how = "강퇴" if cmu.new_chat_member.status == ChatMemberStatus.BANNED else "나감"
    await svc.mod.report(context.bot, f"[봇 {how}] {esc(chat.title or '')} ({chat.id}) by "
                                      f"{esc(user_name(by)) if by else '?'}({by.id if by else '?'})"
                                      + (f" · 예약공지 {n}개 껐어요" if n else ""))


async def on_left(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'OO님이 나갔습니다' 메시지 (관리 권한 없는 방에서도 옴) → 멤버 목록에서 뺌."""
    msg = update.message
    if not msg or not msg.left_chat_member:
        return
    svc = _svc(context)
    # 🧹 멤버 정리가 낸 퇴장: '~님을 내보냄' 서비스 메시지는 지우고 작별 인사는 건너뜀 (봇 계정을 내보낸 것도)
    by_job = await cleanup.job_leave(svc, msg.chat_id, msg.left_chat_member.id,
                                     getattr(msg.from_user, "id", None), context.bot.id)
    if by_job:
        try:
            await msg.delete()
        except TelegramError:
            pass
    if not msg.left_chat_member.is_bot:
        hooks.member_left(svc, msg.chat_id, msg.left_chat_member.id)
        await members_panel.mark(svc.db, msg.chat_id, msg.left_chat_member.id, left=True)
        if not by_job:
            await farewell.on_leave(context, msg.chat_id, msg.left_chat_member, msg.from_user)   # 👋 스스로 나간 사람만
            await leavelock.on_leave(context, msg.chat_id, msg.left_chat_member, msg.from_user)  # 🚪 재입장 막기 (켠 방만)


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
    if cmu and cmu.chat.type == ChatType.CHANNEL:   # 채널 구독자 들어옴·나감, 관리자 변경
        return await channel.on_member(_svc(context), context.bot, cmu)
    if not cmu or cmu.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    svc = _svc(context)
    old, new = cmu.old_chat_member, cmu.new_chat_member
    admin_states = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    if old.status != new.status and (old.status in admin_states or new.status in admin_states):
        svc.perms.forget(cmu.chat.id)  # 관리자 목록 캐시 갱신
    if not _in_chat(old) and _in_chat(new):
        await handle_new_member(context, cmu.chat.id, cmu.chat.title, new.user, getattr(cmu, "from_user", None), join_how(cmu))
    elif _in_chat(old) and not _in_chat(new):
        svc.joins.pop((cmu.chat.id, new.user.id), None)  # 다시 들어오면 캡차·CAS·사칭 검사를 다시 받게
        hooks.member_left(svc, cmu.chat.id, new.user.id)
        await members_panel.mark(svc.db, cmu.chat.id, new.user.id, left=True)
        by = getattr(cmu, "from_user", None)
        if not await cleanup.job_leave(svc, cmu.chat.id, new.user.id, getattr(by, "id", None), context.bot.id):
            await farewell.on_leave(context, cmu.chat.id, new.user, by,  # cancel 전에 (캡차 대기 확인)
                                    kicked=new.status == ChatMemberStatus.BANNED)
            await leavelock.on_leave(context, cmu.chat.id, new.user, by, kicked=new.status == ChatMemberStatus.BANNED)
        await svc.captcha.cancel(context.bot, cmu.chat.id, new.user.id)


# ── 그룹 메시지 ───────────────────────────────────────────
# 관리 검사를 통과한 그룹 메시지마다 백그라운드로 불리는 함수들: hook(svc, bot, msg, role)
# 기능 모듈은 hooks.add_group_message_hook(...) 로 등록한다 (순환 import 방지).
GROUP_MESSAGE_HOOKS = hooks.GROUP_MESSAGE_HOOKS
hooks.add_group_message_hook(social.on_group_message)  # AI 기억 정리·끼어들기 (sodam/social.py)


TOUCH_SECONDS = 60   # 같은 사람의 이름·마지막 활동(last_seen) 기록 간격 (이름이 바뀌면 바로)
_touched: dict[tuple, tuple[float, tuple]] = {}


def _stale(db, chat_id: int, user) -> bool:
    """이 사람 기록을 지금 써야 하는지 (처음·이름 바뀜·TOUCH_SECONDS 지남). True 면 쓴 것으로 표시."""
    key, sig, t = (db.path, chat_id, user.id), (user.username, user.first_name, user.last_name), time.monotonic()
    hit = _touched.get(key)
    if hit and hit[1] == sig and t - hit[0] < TOUCH_SECONDS:
        return False
    if len(_touched) > 100_000:
        _touched.clear()
    _touched[key] = (t, sig)
    return True


async def _run_hook(hook, svc: Services, bot, msg: Message, role: Role) -> None:
    try:
        await hook(svc, bot, msg, role)
    except Exception:
        log.exception("group message hook %s failed", getattr(hook, "__name__", hook))


def _bot_hooks(context: ContextTypes.DEFAULT_TYPE, fns: list, msg: Message) -> None:
    """다른 봇 글: 등록된 봇 훅(sodam/botlink.py 기록)만 백그라운드로. 실패해도 계속."""
    svc, bot = _svc(context), context.bot

    async def run(fn) -> None:
        try:
            await fn(svc, bot, msg)
        except Exception:
            log.exception("bot message hook %s failed", getattr(fn, "__name__", fn))
    for fn in fns:
        task = asyncio.create_task(run(fn))
        bg: set = context.bot_data.setdefault("tasks", set())
        bg.add(task)
        task.add_done_callback(bg.discard)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.from_user:
        return
    svc, bot = _svc(context), context.bot
    chat_id, user = msg.chat_id, msg.from_user
    # 연결된 채널이 자동으로 올린 글 등, 방 자신이 아닌 채널 명의 글은 건너뜀
    if msg.sender_chat and msg.sender_chat.id != chat_id:
        return
    # 다른 봇 글 (Bot-to-Bot 모드: 상대 봇이나 소담 중 하나만 켜도 '/명령@sodam'·소담 글에 단 답장은 옴):
    # 기록 훅만 (botlink) — 관리·명령·게임·AI·끼어들기·태그 알림·알림 규칙은 절대 안 탐 (봇끼리 무한 주고받기 방지)
    if user.is_bot:
        if user.id != bot.id:
            await _record(svc.db.upsert_user(user, commit=True))
            _bot_hooks(context, hooks.BOT_MESSAGE_HOOKS, msg)
        return
    anonymous_admin = msg.sender_chat is not None
    if apikeys.LOOKS_SECRET.search(msg.text or msg.caption or ""):   # 방에 붙인 API 키: 기록·AI 전에 지움 (저장은 1:1 에서만)
        await _drop_secret(bot, msg, "🔑 API 키처럼 보이는 글이라 지웠어요. 키는 소담 1:1 에서 봇 운영자만 넣을 수 있어요.")
        return

    seen: set = context.bot_data["chats"]
    if chat_id not in seen:
        await _record(svc.db.ensure_chat(chat_id, msg.chat.title))
        seen.add(chat_id)
    reply_msg, reply_user = reply_ref(msg)
    # 이름·마지막 활동은 바뀌었거나 TOUCH_SECONDS 지났을 때만 (메시지마다 쓰기 2번 + commit 이던 것).
    # upsert_user 는 commit 을 안 해서 둘 중 하나라도 쓰면 touch_member(commit) 까지 같이
    if _stale(svc.db, chat_id, user) | bool(reply_user and _stale(svc.db, 0, msg.reply_to_message.from_user)):
        await _record(svc.db.upsert_user(user))
        if reply_user:   # 답장받은 사람 이름도 (말 안 한 사람·봇이라도 '↩이름' 이 보이게)
            await _record(svc.db.upsert_user(msg.reply_to_message.from_user))
        await _record(svc.db.touch_member(chat_id, user.id))

    text = msg.text or msg.caption or ""
    try:
        role = Role.ADMIN if anonymous_admin else await svc.perms.role(bot, chat_id, user.id)
    except TelegramError as e:  # 관리자 목록을 처음 받는 중 연결 오류: 일반 멤버로 보고 계속 (명령·게임·AI 는 동작)
        log.warning("role lookup failed in %s: %s", chat_id, e)
        role = Role.MEMBER
    scan = security.scan(text)
    if text:
        await _record(svc.db.log_message(chat_id, user.id, msg.message_id, text, flagged=scan.blocked, ts=sent_at(msg),
                                         reply_to_msg_id=reply_msg, reply_to_user=reply_user))
    vision.remember(msg)        # 사진·영상 올리고 답장 없이 '소담아 이거 어때' 해도 그걸 보게 (3분)
    try:   # 스티커·사진·영상 기록 (사흘, 파일 번호만) — '내가 올린 스티커처럼' 의 그 스티커를 찾게. 실패해도 관리·대화는 계속
        await medialog.record(svc.db, msg)
    except Exception:
        log.exception("media log failed")

    # 봇이 관리 권한 없이 일반 멤버로만 있는 방: 지우지도 막지도 못하니 관리 검사는 건너뛰고 대화·게임·기록만
    exempt = role >= Role.ADMIN or await free.is_free(svc.db, chat_id, user.id)   # 자유 멤버는 자동 통제 없음
    if not exempt and await svc.perms.bot_can_moderate(bot, chat_id):
        s = await svc.db.get_settings(chat_id)
        cas_seen: set = context.bot_data["cas_seen"]
        if s["cas_enabled"] and (chat_id, user.id) not in cas_seen:  # 방마다 (밴은 그 방에서만 하니까)
            cas_seen.add((chat_id, user.id))
            tasks: set = context.bot_data["tasks"]
            task = asyncio.create_task(_cas_background(context, chat_id, user))
            tasks.add(task)  # 참조를 잡아둬야 도중에 가비지 컬렉션되지 않음
            task.add_done_callback(tasks.discard)
        try:
            notice = await svc.mod.check_impersonation(bot, chat_id, user)
            if not notice:
                notice = await svc.mod.check_message(bot, msg, text, game_active=svc.games.is_active(chat_id))
        except (TimedOut, NetworkError) as e:  # 연결이 잠깐 끊겨도 메시지 처리(명령·게임·AI)는 계속
            log.warning("moderation check skipped (network): %s", e)
            notice = None
        if notice is not None:  # "" = 지웠지만 안내는 생략 (잠긴 종류 안내는 10분에 한 번)
            if notice:
                await send_temp(context, chat_id, notice)
            return
        if await subgate.gate(svc, bot, msg, s):   # 채널 구독 필수 (선택, sodam/subgate.py)
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

    if text.startswith("!") and svc.cfg.bot_role != "main" and \
            await casino.dispatch(svc, bot, msg, chat_id, user, role, text):
        return  # 포인트 게임 (! 명령)

    parsed = commands.parse(text, bot.username)
    if parsed:
        cmd, args, argstr = parsed
        if cmd.fn is commands.c_style and await _style_for_ai(svc, msg, chat_id, args):
            # '.말투 여친 @누구 한테…' 처럼 대상·설명이 붙으면 누구 말투인지 AI 가 판단
            await ai_reply(context, msg, role, f"말투 변경 부탁: {argstr}", scan)
            return
        await commands.dispatch(CmdCtx(svc, bot, msg, chat_id, user, role, args, argstr), cmd)
        return
    if await autoreply.maybe_reply(svc, bot, msg, text):   # 💬 등록한 낱말만 친 글 → 저장한 글로 답장 (게임·AI 안 탐)
        return
    if await _prefix_hint(context, chat_id, text):
        return

    # 게임 중이면 게임 답이 먼저 (AI 답에 답장으로 단 단어도 게임이 받음 → AI 가 대신 진행하지 않게)
    if svc.games.is_active(chat_id) and await svc.games.on_text(msg, text):
        return
    # 봇 메시지에 답장: AI 답(ai_turns 에 기록된 메시지)일 때만 호출. 게임 결과·경고·인사에 단 답장은 호출 아님
    r = msg.reply_to_message
    reply_is_ai = not (r and r.from_user and r.from_user.id == bot.id) or \
        await svc.db.is_ai_message(chat_id, r.message_id)
    addressed, request = addressed_to_bot(msg, text, svc.cfg.call_names, bot, reply_is_ai)
    via = "call"
    if not addressed:  # 방금 봇과 얘기하던 사람이 이름 없이 이어서 말한 경우
        addressed, request = await social.follow_up(svc, bot, msg, text)
        via = "follow"
    if addressed:
        await ai_reply(context, msg, role, request, scan, via=via)


async def on_group_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """수정된 메시지(캡션 포함): 링크·금지어·외부 @아이디만 다시 검사. 도배 수·AI·명령·게임은 안 함
    (멀쩡한 글을 올렸다가 나중에 광고로 고치는 우회를 막음)."""
    msg = update.edited_message
    # 채널 명의 글·익명 관리자(sender_chat)는 검사 대상 아님
    if not msg or not msg.from_user or msg.sender_chat is not None:
        return
    if msg.from_user.is_bot:   # 다른 봇이 고친 글: 기록만 고침 (botlink), 검사·답 없음
        if msg.from_user.id != context.bot.id:
            _bot_hooks(context, hooks.BOT_EDIT_HOOKS, msg)
        return
    svc, bot = _svc(context), context.bot
    text = msg.text or msg.caption or ""
    if not text:
        return
    try:
        role = await svc.perms.role(bot, msg.chat_id, msg.from_user.id)
        if role >= Role.ADMIN or await free.is_free(svc.db, msg.chat_id, msg.from_user.id):   # 자유 멤버는 자동 통제 없음
            return
        for hook in hooks.GROUP_EDIT_HOOKS:   # 수정 함정 검사 등 (sodam/spamshield.py) — 백그라운드, 실패해도 계속
            task = asyncio.create_task(_run_hook(hook, svc, bot, msg, role))
            context.bot_data.setdefault("tasks", set()).add(task)
            task.add_done_callback(context.bot_data["tasks"].discard)
        if not await svc.perms.bot_can_moderate(bot, msg.chat_id):
            return
        notice = await svc.mod.check_edited(bot, msg, text)
    except (TimedOut, NetworkError) as e:
        log.warning("edited message check skipped (network): %s", e)
        return
    if notice:
        await send_temp(context, msg.chat_id, notice)


async def _prefix_hint(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> bool:
    """'.가입'·'!설정' 처럼 . 과 ! 를 헷갈려 아무 반응이 없던 명령: 반대쪽에 있으면 짧게 알려줌."""
    if text[:1] not in ".!" or len(text) < 2:
        return False
    head = text[1:].split(maxsplit=1)[0].partition("@")[0].lower() if text[1:].strip() else ""
    if not head:
        return False
    if text[0] == "." and casino.parse("!" + head):
        other = "!" + head
    elif text[0] == "!" and head in commands._INDEX and not casino.parse(text):  # 게임 명령이면 딜러 봇 몫
        other = "." + head
    else:
        return False
    await send_temp(context, chat_id, f"혹시 <code>{esc(other)}</code> 인가요? (<code>.</code> 봇 명령 · "
                                      f"<code>!</code> 포인트 게임)", 15)
    return True


EXPIRED_NOTICE_GAP = 3600   # 끝난 방에서 AI 를 부를 때 안내는 방마다 1시간에 1번
EXPIRED_AI_NOTICE = "🔒 이 방의 {name} 이용 기간이 끝나서 AI 대화는 쉬고 있어요. 관리자님은 아래 버튼에서 연장할 수 있어요."
ENDED_NOTICE = ("⛔ {name} {what}이 끝났어요. AI 대화·게임·예약공지 같은 기능은 멈추고, "
                "방 관리(캡차·도배·금지어·경고)는 계속 무료로 동작해요. 관리자님은 아래 버튼에서 연장할 수 있어요.")


async def _keep_typing(bot, chat_id: int, every: float = 4.0) -> None:
    """AI 가 생각하는 동안 '입력 중…' 을 계속 (한 번 보내면 5초만 보임 — 긴 작업이 멈춘 것처럼 보이던 것)."""
    while True:
        await asyncio.sleep(every)
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass


async def _within_ai_quota(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, role: Role) -> bool:
    """구독 안 한 방 / 1:1 채팅의 하루 무료 AI 한도. 넘으면 안내하고 False (안내엔 금액을 넣지 않음)."""
    svc = _svc(context)
    billing = svc.billing
    if role >= Role.OWNER:
        return True
    if chat_id < 0:
        # 그룹: 결제 기능이 꺼졌거나 구독(체험) 중이면 제한 없음 (분당 호출 제한은 따로 있음).
        # 끝난 방은 AI 답 없이 안내만 (오너 결정 2026-09-28: 무료 하루 N번 없앰) — 방마다 EXPIRED_NOTICE_GAP 에 1번, 잠깐 보였다 사라짐
        if billing is None or not billing.enabled or await billing.active(chat_id):
            return True
        if await persist.claim(svc.db, f"expired_notice:{chat_id}", EXPIRED_NOTICE_GAP):
            await send_temp(context, chat_id, EXPIRED_AI_NOTICE.format(name=svc.cfg.bot_name), 120,
                            reply_markup=subscription.setup_button(context.bot.username, chat_id))
        return False
    # 1:1 은 결제 여부와 상관없이 하루 무료 한도 적용 → 낯선 사람이 전체 AI 예산을 다 쓰지 못하게
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    if await svc.db.bump(day, user_id, "free_ai_dm") <= svc.cfg.free_ai_per_day:
        return True
    await send_temp(context, chat_id, "오늘 무료 대화량을 다 썼어요. 내일 다시 이야기해요 🙏", 30)
    return False


_credit_reported = 0.0


async def _report_credit(svc: Services, bot: Bot) -> None:
    """OpenAI 크레딧 소진: 운영자에게 한 시간에 한 번만 알림."""
    global _credit_reported
    if time.time() - _credit_reported < 3600:
        return
    _credit_reported = time.time()
    await svc.mod.report(bot, "⚠️ OpenAI 크레딧이 바닥났어요. 모든 방의 AI 대화가 멈춘 상태예요.\n"
                              "충전: platform.openai.com → Settings → Billing")


async def _style_for_ai(svc: Services, msg: Message, chat_id: int, args: list[str]) -> bool:
    """'.말투 여친' 한 단어면 본인 말투(명령 그대로). 태그·답장·설명이 붙었고 AI 를 쓸 수 있으면 AI 에게."""
    r = msg.reply_to_message
    other = r is not None and r.from_user is not None and not r.from_user.is_bot and r.from_user.id != msg.from_user.id
    tagged = any(e.type in ("mention", "text_mention") for e in (msg.entities or ()))
    if not (len(args) > 1 or other or tagged):
        return False
    s = await svc.db.get_settings(chat_id)
    return bool(s["ai_enabled"] and getattr(svc.llm, "enabled", False))


async def ai_reply(context: ContextTypes.DEFAULT_TYPE, msg: Message, role: Role,
                   request: str, scan: security.ScanResult, via: str = "call", *, followup: bool = False) -> None:
    """AI 답. followup=True = 앞 실행이 끝내 못 본 이어 보낸 말(agent.close_steer) — 속도 제한·검사·요청 기록은 이미 함."""
    svc, bot = _svc(context), context.bot
    chat_id, user = msg.chat_id, msg.from_user
    if user is None or user.is_bot:   # 봇 글엔 AI 답 없음 (on_group_message 가 이미 막지만 다른 경로 대비)
        return
    if is_stale(msg):
        log.info("늦게 받은 메시지라 AI 답 생략 chat=%s msg=%s", chat_id, msg.message_id)
        return
    s = await svc.db.get_settings(chat_id)
    if not s["ai_enabled"]:
        return
    if not svc.llm.enabled:
        await send_temp(context, chat_id, "🔌 아직 AI 키가 설정되지 않아서 대화는 못 해요. 명령어(.도움말)는 쓸 수 있어요!", 30)
        return

    key = (chat_id, user.id)
    burst = _BURSTS.setdefault(key, [])
    burst.append((msg.message_id, request))
    await asyncio.sleep(BURST_SECONDS)
    # 가장 늦게 보낸 말(메시지 ID)이 한꺼번에 답함 — 처리 순서(동시 처리)는 보낸 순서와 다를 수 있어서 ID 로
    if _BURSTS.get(key) is not burst or max(burst)[0] != msg.message_id:
        return
    del _BURSTS[key]
    if len(burst) > 1:
        request = "\n".join(r for _, r in sorted(burst) if r)
        scan = security.scan(request)

    checked = followup
    if not followup:
        limiter: RateLimiter = context.bot_data["limiter"]
        if not (limiter.allow(("u", chat_id, user.id), s["user_rate_per_min"])
                and limiter.allow(("r", chat_id), s["room_rate_per_min"])):
            if limiter.allow(("slow", chat_id, user.id), 1):   # 안내는 1분에 한 번만 (안내가 도배가 되지 않게)
                await send_temp(context, chat_id, "⏳ 조금만 천천히 불러주세요! 1분 뒤에 다시 불러주세요.", 10)
            return
        # 이 사람의 답이 아직 만들어지는 중 (Codex inject_if_running): 두 번째 답 대신 그 실행에 넣는다.
        # 분당 호출 제한엔 세고(위), 하루 무료 한도는 한 번 더 안 씀(실행 1번 = 1). 사진이 붙은 말은 따로 실행 (사진을 읽어야 함)
        if agent.running(svc, chat_id, user.id) and not vision.has_image(msg):
            if await _injection_blocked(context, msg, role, request, scan, s):
                return
            await svc.db.log_request(chat_id, user.id, request)
            target = agent.running_msg(svc, chat_id, user.id)
            if agent.steer_into(svc, chat_id, user.id, _steer_text(svc, bot, msg, request), msg):
                if target is not None:   # 재시작으로 그 실행이 끊겨도 다시 돌 때 이 말까지 (DB 줄에 덧붙임)
                    try:
                        await aiqueue.append(svc.db, bot, chat_id, target, request)
                    except Exception as e:
                        log.debug("ai queue append failed: %r", e)
                return
            checked = True   # 검사하는 사이 그 실행이 끝남 → 말을 잃지 않게 보통 새 실행으로 (검사·기록은 이미 함)

    # 무료 한도 먼저: 한도를 넘은 사람의 긴 메시지가 2층 AI 판별(유료 호출)을 무제한으로 부르지 않게
    if not await _within_ai_quota(context, chat_id, user.id, role):
        return
    if not checked:
        if await _injection_blocked(context, msg, role, request, scan, s):
            return
        await svc.db.log_request(chat_id, user.id, request)

    await aiqueue.put(svc.db, bot, msg, role, via, request)   # 재시작·끊김에도 답이 사라지지 않게 (sweep 이 이어서)
    steer = agent.open_steer(svc, chat_id, user.id, msg.message_id)   # 여기부터 이 사람이 이어 보낸 말은 이 실행으로
    try:
        await _answer(context, msg, role, request, s, via, steer)
    except asyncio.CancelledError:   # 종료(배포) 중 끊김 → 줄을 남겨 다시 켜진 봇이 답함
        aiqueue.RUNNING.discard((bot.id, chat_id, msg.message_id))
        raise
    except BaseException:
        await aiqueue.done(svc.db, bot, chat_id, msg.message_id)
        raise
    else:
        await aiqueue.done(svc.db, bot, chat_id, msg.message_id)
    finally:
        left = agent.close_steer(svc, chat_id, user.id, steer)
    if left:   # 마무리 답 뒤에 온 말 (드묾) → 잃지 않게 새 실행
        text = "\n".join(t for t, _ in left)
        await ai_reply(context, left[-1][1] or msg, role, text, security.scan(text), via, followup=True)


def _steer_text(svc: Services, bot: Bot, msg: Message, request: str) -> str:
    """실행에 넣을 이어 보낸 말. 다른 글에 답장했으면 그 글도 짧게 (새 실행이면 <reply_to> 로 갈 정보)."""
    r = msg.reply_to_message
    if not (r and r.from_user and (r.text or r.caption)):
        return request
    who = f"{svc.cfg.bot_name}(봇)" if r.from_user.id == bot.id else f"{user_name(r.from_user)}({r.from_user.id})"
    return f"{request}\n(↩ 답장한 글 — {who}: {(r.text or r.caption)[:200]})"


_UNVERIFIED: set[tuple[int, int]] = set()   # (방, 메시지) 인젝션 2층 판별이 실패한 요청 → 답할 때 카드 없는 쓰기 막음


async def _injection_blocked(context: ContextTypes.DEFAULT_TYPE, msg: Message, role: Role, request: str,
                             scan: security.ScanResult, s: dict) -> bool:
    """인젝션 방어: 1층 규칙 → 애매하거나 긴 요청만 2층 AI 판별. 막았으면 안내·기록하고 True."""
    svc, bot = _svc(context), context.bot
    chat_id, user = msg.chat_id, msg.from_user
    if not s["injection_guard"] or role >= Role.OWNER:
        return False
    blocked, reason = scan.blocked, ", ".join(scan.hits)
    if not blocked and (scan.suspicious or len(request) > 150):
        blocked, reason = await svc.llm.classify_injection(request, chat_id=chat_id)
        if not blocked and reason == UNVERIFIED:      # 판별 실패 = 통과는 시키되 카드 없는 쓰기는 막음 (fail-closed, 읽기·카드는 됨)
            if len(_UNVERIFIED) > 1000:              # 답까지 안 간 요청(속도 한도 등)이 남아도 무한히 쌓이지 않게
                _UNVERIFIED.clear()
            _UNVERIFIED.add((chat_id, msg.message_id))
    if not blocked:
        return False
    await svc.db.flag_message(chat_id, msg.message_id)  # 이후 AI 맥락에서 제외
    text, kb = "🛡️ 그 요청은 들어드릴 수 없어요.", None
    # 경고·제재는 그룹방에서만 (1:1 은 양수 ID), 자유 멤버는 경고 없이 거절만
    if s["injection_warn"] and role < Role.ADMIN and chat_id < 0 and not await free.is_free(svc.db, chat_id, user.id):
        warned = await svc.mod.warn(bot, chat_id, user.id, user_name(user), bot.id,
                                    f"봇 조작 시도 ({reason.split(',')[0].strip()[:20] or '규칙 위반'})")
        text, kb = text + "\n" + warned, unmute_kb(warned)
    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
    await svc.mod.report(bot, f"[인젝션 차단] chat {chat_id} / {esc(user_name(user))}({user.id}): "
                              f"{esc(request[:200])} / {esc(reason)}")
    return True


async def _answer(context: ContextTypes.DEFAULT_TYPE, msg: Message, role: Role, request: str, s: dict, via: str,
                  steer: agent.Steer) -> None:
    """검사를 통과한 요청: 맥락을 모아 에이전트를 돌리고 답을 보낸다."""
    svc, bot = _svc(context), context.bot
    chat_id, user = msg.chat_id, msg.from_user
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
    media = vision.describe(r) if r else None      # 글 없는 영상·사진에 답장해도 '무엇에 답장했는지' 알게 (예전엔 모름)
    if r and r.from_user and (r.text or r.caption or media):  # 봇 답에 답장한 경우도 '어느 답'인지 알려줌
        who = f"{svc.cfg.bot_name}(봇)" if r.from_user.id == bot.id else f"{user_name(r.from_user)}({r.from_user.id})"
        reply_to = f"{who}: {' '.join(x for x in (media, (r.text or r.caption or '')[:500]) if x)}"
    if chat_id > 0 and s.get("ai_memory", True):
        memory.observe(svc, chat_id, user.id, request)  # 1:1 은 그룹 훅이 없어서 여기서 기억 후보 확인
    try:   # '팽부장 떠오르게 하지마' → 이번 답부터 그 호칭을 안 쓰게 답 만들기 전에 지움
        await memory.drop_disliked_nickname(svc.db, chat_id, user.id, request)
    except Exception:
        log.exception("drop nickname failed")

    try:
        hints = await addressee.collect(svc, bot, msg, chat_id, user, request)
    except Exception:  # 단서가 없어도 대답은 한다
        log.exception("addressee hints failed")
        hints = []
    if svc.games.is_active(chat_id):   # 게임 중엔 AI 가 게임을 대신 진행하지 않게
        hints = [*hints, svc.games.active[chat_id].ai_hint()]
    elif (recent := svc.games.status(chat_id)) != "진행 중인 게임 없음":   # 방금 끝난 게임 ('고장났어?' 에 이유 설명·다시 시작)
        hints = [*hints, "게임 단서: " + recent + " (필요하면 game_control 로 다시 시작)"]
    image = await vision.fetch(bot, msg)   # 요청·답장한 메시지의 사진·영상 (영상은 장면 여러 장, 고쳐 달라면 대표 장면을 원본으로)
    ctx = ToolCtx(svc, bot, chat_id, user, role, s, image=image, reply_msg_id=reply_ref(msg)[0], request_msg=msg)
    if (chat_id, msg.message_id) in _UNVERIFIED:      # 인젝션 판별을 못 한 요청 → 처음부터 room_read 와 같은 제한
        _UNVERIFIED.discard((chat_id, msg.message_id))
        ctx.room_read = True
    typing = asyncio.create_task(_keep_typing(bot, chat_id))   # 텔레그램 '입력 중'은 5초면 꺼짐 → 답이 나올 때까지 4초마다
    try:
        answer = await run_agent(ctx, style_key=style, notes=notes, history=history, reply_to=reply_to,
                                 request=request or "(사진만 보냄)", mode=via, hints=hints,
                                 images=image.parts() if image else None, steer=steer)
    except BudgetExceeded:
        answer = "오늘 AI 사용량을 다 써서 내일 다시 불러주세요 🙏"
    except OpenAIError as e:
        log.warning("openai error: %s", e)
        if out_of_credit(e):
            answer = "지금 AI 사용량이 바닥나서 잠깐 쉬고 있어요. 운영자에게 알렸어요 🙏"
            await _report_credit(svc, bot)
        else:
            answer = "AI 연결이 잠깐 불안정해요. 잠시 후 다시 불러주세요."
    finally:
        typing.cancel()

    if ctx.quiet:
        return
    usernames = {row["username"].lower() for row in await svc.db.member_names(chat_id) if row["username"]}
    out = security.filter_output(answer, max_chars=s["reply_max_chars"], allowed_usernames=usernames)
    body = esc(out)
    if ctx.mentions:
        body = " ".join(mention(uid, name) for uid, name in dict(ctx.mentions).items()) + " " + body
    # 기다리는 동안 원본이 지워져도 답은 가게 (1:1 은 원래대로 인용 없이)
    # 방 설정 ai_quote 끄면 인용 없이 (do_quote=False 가 없으면 PTB 가 그룹에선 알아서 인용함)
    group = chat_id < 0
    reply = ReplyParameters(msg.message_id, allow_sending_without_reply=True) if group and s["ai_quote"] else None
    try:
        sent = await send_retry(lambda: msg.reply_text(body, parse_mode="HTML", reply_parameters=reply,
                                                       do_quote=None if reply else False,
                                                       link_preview_options=security.NO_PREVIEW))
    except NetworkError as e:
        if not surely_unsent(e):   # 응답만 끊김 = 이미 올라갔을 수 있음 → 다시 안 보냄 (중복 방지)
            raise
        await aiqueue.keep_answer(svc.db, bot, chat_id, msg.message_id, body)   # 연결이 돌아오면 sweep 이 보냄
        log.warning("답 전송 실패(연결 끊김) → 대기열에 보관 chat=%s msg=%s", chat_id, msg.message_id)
        return
    # 누구에게 한 답인지 — 인용을 껐어도 기록은 남김 (aiqueue '이미 답함'·이어 말하기가 이걸 봄)
    await _record(svc.db.log_message(chat_id, bot.id, sent.message_id, out, is_bot=True,
                                     reply_to_msg_id=msg.message_id if group else None,
                                     reply_to_user=user.id if group else None))
    asked = "\n".join([request, *steer.taken])   # 실행 중 이어 보낸 말까지 (한 답이 둘 다 반영)
    await memory.record_turn(svc.db, chat_id, user.id, via, asked, out, sent.message_id)  # 이어 말하기·'아까 그거'용


# ── 버튼 ──────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    svc, bot = _svc(context), context.bot
    data = q.data or ""
    prefix, _, rest = data.partition(":")
    parts = rest.split(":") if rest else []
    if prefix == "cap":
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
    elif prefix == "cs":
        await casino.on_callback(svc, bot, q, parts)
    elif prefix == "ow":
        await _owner_action(svc, bot, q, parts)
    elif prefix == "jr":
        await joinreq.on_callback(svc, bot, q, parts)
    elif prefix == "um":
        await _unmute_button(svc, bot, q, parts)
    elif prefix == "cas":
        await _cas_button(svc, bot, q, parts)
    elif prefix == "wc":
        await svc.games.on_callback(q, parts)
    elif prefix in hooks.CALLBACK_HANDLERS:   # 기능 모듈이 등록한 버튼 (hooks.add_callback_handler)
        await hooks.CALLBACK_HANDLERS[prefix](svc, bot, q, parts)
    else:
        await q.answer()


OWNER_ACTIONS = {"u": "채팅 금지를 풀었어요", "x": "1일 채팅 금지로 바꿨어요", "b": "밴(영구 추방)했어요", "n": "밴을 풀었어요"}


async def _owner_action(svc: Services, bot: Bot, q, parts: list[str]) -> None:
    """오너 보고의 바로가기 버튼 (moderation.owner_kb): ow:<방>:<사람>:u|x|b|n. 오너만 (관리 로그방의 다른 사람은 못 누름)."""
    chat_id, uid = to_int(parts[0] if parts else ""), to_int(parts[1] if len(parts) > 1 else "")
    act = parts[2] if len(parts) > 2 else ""
    if not chat_id or not uid or act not in OWNER_ACTIONS:
        return await q.answer()
    if q.from_user.id not in await svc.perms.owners():
        log.info("owner action refused: user %s", q.from_user.id)
        return await q.answer("오너만 누를 수 있어요.", show_alert=True)
    mod, me = svc.mod, q.from_user.id
    try:
        if act == "u":
            await mod.unmute(bot, chat_id, uid, me)
        elif act == "x":
            await mod.mute(bot, chat_id, uid, 1440, me, "오너 연장")
        elif act == "b":
            await mod.ban(bot, chat_id, uid, me, "오너 판단")
        else:
            await mod.unban(bot, chat_id, uid, me)
    except TelegramError as e:
        log.warning("owner action %s failed: chat %s user %s: %s", act, chat_id, uid, e)
        return await q.answer(f"실패했어요: {e.message[:100]}", show_alert=True)
    await q.answer(OWNER_ACTIONS[act])
    try:
        text = getattr(q.message, "text_html", None) or ""
        await q.edit_message_text(f"{text}\n→ ✅ {OWNER_ACTIONS[act]}", parse_mode="HTML")
    except TelegramError:
        pass


async def _unmute_button(svc: Services, bot: Bot, q, parts: list[str]) -> None:
    """자동 채팅 금지 안내의 [🔊 채팅 금지 풀기]: '사용자 차단' 권한 있는 관리자만."""
    uid = to_int(parts[0] if parts else "")
    if not uid or not q.message:  # 너무 오래된 메시지면 message 가 없음
        await q.answer()
        return
    chat_id = q.message.chat_id
    if not await may(svc.perms, bot, chat_id, q.from_user.id):
        await q.answer(no_right_text(), show_alert=True)
        return
    try:
        await svc.mod.unmute(bot, chat_id, uid, q.from_user.id)
    except TelegramError as e:
        await q.answer(f"풀지 못했어요: {e.message[:100]}", show_alert=True)
        return
    await q.answer("채팅 금지를 풀었어요.")
    try:
        who = mention(uid, await svc.db.first_name(uid) or "멤버")
        await q.edit_message_text(f"🔊 관리자 {esc(user_name(q.from_user))}님이 {who}님 채팅 금지를 풀었어요.",
                                  parse_mode="HTML")
    except TelegramError:
        pass


async def _confirm_action(svc: Services, bot: Bot, q, parts: list[str]) -> None:
    """경고·뮤트·밴·내보내기·밴 해제·경고 취소·자유 멤버·캡차 통과 확인 버튼 (tools._ask_sanction, 종류는 modactions.KINDS).
    y = 실행, p = 실행 + 방에 안내(오너 1:1 요청), n = 취소. 대상이 여러 명이면 한 번에 처리하고 사람마다 결과를 보여 준다."""
    key, yn = (parts + ["", ""])[:2]
    # 재시작(배포) 뒤엔 메모리에 없음 → DB 에서 (sodam/persist.py, 카드 유효시간은 그대로)
    action = svc.pending.get(key) or (await persist.load_pending(svc.db, key) if key else None)
    presser = q.from_user.id
    if not action or action.expires < time.time() or action.kind not in modactions.KINDS:
        svc.pending.pop(key, None)
        await q.answer("만료된 요청이에요.")
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:  # 이미 버튼이 없거나 메시지가 지워진 경우
            pass
        return
    allowed = (presser in await svc.perms.owners() if action.from_dm   # 1:1 카드는 오너만
               else await svc.perms.is_admin(bot, action.chat_id, presser))
    pressed = lambda what: svc.db.audit(action.chat_id, presser, action.target_id, f"press_{action.kind}",  # noqa: E731
                                        f"{what} ({len(action.targets)}명)")
    if not allowed:
        log.info("sanction button refused (not admin): chat %s user %s", action.chat_id, presser)
        if presser not in action.refused:          # 아무나 연타해도 기록은 한 줄
            action.refused.add(presser)
            await pressed("거절(관리자 아님)")
        await q.answer("관리자만 누를 수 있어요.", show_alert=True)
        return
    # 실행은 누른 사람에게 텔레그램 '사용자 차단' 권한이 있어야 (취소는 관리자 누구나)
    if yn in ("y", "p") and not await may(svc.perms, bot, action.chat_id, presser):
        log.info("sanction button refused (no right): chat %s user %s", action.chat_id, presser)
        await pressed("거절(차단 권한 없음)")
        await q.answer(no_right_text(), show_alert=True)
        return
    # 한 번만 실행: 메모리에서 꺼냈거나 DB 줄을 지운 쪽만 (재시작 뒤 두 번 빨리 누르면 둘 다 DB 에서 읽음)
    mine = svc.pending.pop(key, None) is not None
    if not (await persist.take_pending(svc.db, key) or mine):
        await q.answer("이미 처리된 요청이에요.")
        return
    await q.answer()
    # AI 맥락용 결과 한 줄 (cards.record): 카드가 뜬 대화 = 오너 1:1 요청이면 오너 1:1, 아니면 그 방
    kind = modactions.KINDS[action.kind]
    label = modactions.record_label(action.kind, action.minutes)
    names = ", ".join(name for _, name in action.targets)[:60]
    card_chat = action.requested_by if action.from_dm else action.chat_id
    if yn not in ("y", "p"):
        await pressed("취소")
        await cards.record(svc, card_chat, action.requested_by, action.kind,
                           f"❌ {label} 취소 — {names} (누른 사람: {user_name(q.from_user)})")
        await q.edit_message_text("취소했어요.")
        return
    by, lines, done = esc(user_name(q.from_user)), [], []
    for uid, name in action.targets:
        who = mention(uid, name)
        if kind.punitive and await svc.perms.protected(bot, action.chat_id, uid):  # 버튼이 떠 있는 동안 관리자가 됐을 수도
            lines.append(f"⛔ {who}님은 관리자라서 제재할 수 없어요.")
            continue
        try:
            ok, line = await kind.run(svc, bot, action.chat_id, uid, name, presser, action)
            lines.append(line)
            if ok:
                done.append(who)
        except TelegramError as e:
            log.warning("sanction %s failed: chat %s user %s: %s", action.kind, action.chat_id, uid, e)
            lines.append(f"❌ {who}님 실패: {esc(e.message)} (봇에게 '사용자 차단' 권한이 있는지 확인해주세요)")
    await q.edit_message_text("\n".join(lines) + f"\n(처리: {by})", parse_mode="HTML")
    failed = len(action.targets) - len(done)
    await cards.record(svc, card_chat, action.requested_by, action.kind,
                       (f"✅ {label} 실행됨 — {len(done)}명" if done else f"⚠️ {label} 실행 안 됨") + f" — {names}"
                       + (f" (못 한 사람 {failed}명)" if done and failed else "") + f" (처리: {user_name(q.from_user)})")
    if yn == "p" and done:   # 오너 1:1 요청: 방에도 짧게 안내 (정해진 문구 + 사유만, AI 문장 아님)
        notice = modactions.notice(action.kind, action.minutes)
        try:
            await bot.send_message(action.chat_id, f"📢 관리자 조치: {', '.join(done)}님 {notice}\n사유: {esc(action.reason)}",
                                   parse_mode="HTML")
        except TelegramError as e:
            log.warning("sanction notice failed in %s: %s", action.chat_id, e)


_LOOKUP_ONLY = re.compile(r"@[A-Za-z0-9_]{3,32}|\d{5,15}")
_OWNER_CMD = re.compile(r"^[./](owner|오너)(@\w+)?\s+(\d{8})\s*$", re.I)
_DEEP_LINK = re.compile(r"^/start\s+(sub|cfg)_(-\d{5,18})\s*$")


async def _drop_secret(bot: Bot, msg, note: str) -> None:
    try:
        await msg.delete()
    except TelegramError as e:
        log.info("secret message delete failed %s: %s", msg.chat_id, e)
        note += " (메시지를 못 지웠어요 — 직접 지워 주세요)"
    try:
        await bot.send_message(msg.chat_id, note)
    except TelegramError:
        pass


async def _handle_key(svc: Services, bot: Bot, msg, user, got) -> None:
    """1:1 에 온 API 키 (sodam/apikeys.py): 메시지는 늘 지움, 오너 + 허용된 이름·모양이면 data/keys.env 에 저장."""
    if user.id not in await svc.perms.owners():
        return await _drop_secret(bot, msg, "🔒 API 키는 봇 운영자만 넣을 수 있어요. 방금 메시지는 지웠어요.")
    if got is None or got[1] is None:
        names = " · ".join(apikeys.ALLOWED)
        return await _drop_secret(bot, msg, f"🔑 키 이름이나 모양이 안 맞아서 저장 안 했어요 (메시지는 지웠어요).\n"
                                            f"형식: .키 이름 값 — 이름: {names}")
    name, value = got
    try:
        await asyncio.to_thread(apikeys.save, svc.cfg.db_path, name, value)
    except (OSError, ValueError) as e:
        log.warning("api key save failed %s: %s", name, type(e).__name__)
        return await _drop_secret(bot, msg, f"🔑 {name} 저장에 실패했어요 (메시지는 지웠어요). 서버 data 폴더 권한을 확인해 주세요.")
    log.info("api key saved by owner: %s (%s)", name, apikeys.masked(value))
    await _drop_secret(bot, msg, f"🔑 {name} 저장했어요 (끝자리 {apikeys.masked(value)}). 바로 적용돼요 — 키 메시지는 지웠어요.")


async def on_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """1:1 채팅 순서: 딥링크·/start·/owner → 예약공지 마법사 → 메뉴 글자 입력 → 명령어 → AI."""
    msg = update.message
    svc, bot = _svc(context), context.bot
    user = msg.from_user
    text = (msg.text or msg.caption or "").strip()
    if not user or user.is_bot:   # 봇이 보낸 1:1 (양쪽 다 Bot-to-Bot 모드면 옴): 답하지 않음 (봇끼리 무한 대화 방지)
        return
    got = apikeys.detect(text)
    if got is not None or apikeys.LOOKS_SECRET.search(text):   # API 키: 맨 먼저 — 기록(messages)·AI 로 새지 않게
        await _handle_key(svc, bot, msg, user, got)
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
    hook = re.match(r"^/start\s+([a-z]+)_(\w{1,40})\s*$", text)
    if hook and hook.group(1) in hooks.DEEP_LINKS:   # 기능 모듈이 등록한 딥링크 (hooks.add_deep_link)
        await hooks.DEEP_LINKS[hook.group(1)](svc, bot, msg, hook.group(2))
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
    vision.remember(msg)                  # 영상·스티커만 보내고 다음 말로 '이건어떰' 해도 그걸 보게
    if not text and not vision.has_photo(msg):
        return
    text = text or "이 사진 봐줘"          # 1:1 에 사진만 보내면 사진을 읽고 답함

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
    reply_msg, reply_user = reply_ref(msg)
    await _record(svc.db.log_message(msg.chat_id, user.id, msg.message_id, text, flagged=scan.blocked, ts=sent_at(msg),
                                     reply_to_msg_id=reply_msg, reply_to_user=reply_user))
    await ai_reply(context, msg, role, text, scan)


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """채널 새 글·고친 글 → 기록 (연결된 방 AI 참고·새 글 알림, sodam/channel.py)."""
    msg = update.channel_post or update.edited_channel_post
    try:
        await channel.on_post(_svc(context), context.bot, msg, edited=update.edited_channel_post is not None)
    except Exception:
        log.exception("channel post failed")


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
    mr = getattr(update, "message_reaction", None)
    if mr is not None and hooks.REACTION_HOOKS:                       # 스티커 👍❤️🔥 학습 (sodam/stickerlearn.py)
        await hooks.reaction(_svc(context), context.bot, mr)


async def job_name_sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    """1분마다: 말 안 하는 멤버도 이름을 바꿨는지 조금씩 확인."""
    try:
        await namehist.sweep(_svc(context), context.bot)
    except Exception:
        log.exception("name sweep failed")


# ── 예약 작업 ─────────────────────────────────────────────
TICK_SLOW = 10


async def job_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """30초마다: 캡차 시간 초과 처리, 예약공지 발송, 대기 중인 결제 확인."""
    svc = _svc(context)
    jobs = [("captcha", svc.captcha.expire), ("announce", svc.announcer.run_due),
            ("raid", lambda bot: raid.tick(svc, bot)),  # 끝난 대량 입장 방어 모드 해제
            ("anomaly", lambda bot: anomaly.tick(svc, bot)),  # 이상징후 '보안 강화' 시간 끝나면 설정 되돌림
            ("joinreq", lambda bot: joinreq.expire(svc, bot)),  # 시간 지난 가입 신청 거절
            ("hooks", lambda bot: hooks.tick(svc, bot)),  # 채널 예약 글·구독자 수 등 (hooks.add_tick_hook)
            ("media", lambda bot: mediastore.tick(bot, svc.db)),  # 예약·인사 미디어 원본 보관 (봇이 바뀌어도 다시 올림)
            ("sub_end", lambda bot: _notify_ended(context))]  # 이용 기간이 끝난 그 시각에 안내
    if svc.billing and svc.billing.enabled:
        jobs.append(("billing", lambda bot: subscription.run_check(svc, bot)))
    for name, fn in jobs:
        t0 = time.monotonic()
        try:
            await fn(context.bot)
        except Exception:  # 한쪽 실패가 다른 쪽을 막지 않게
            log.exception("tick %s failed", name)
        if time.monotonic() - t0 > TICK_SLOW:   # 30초 틱이 밀리는 원인 찾기 (재시작 직후 'skipped: maximum instances')
            log.warning("tick %s 느림 %.1f초", name, time.monotonic() - t0)


async def job_sports(context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _svc(context)
    await svc.sports.run_alerts(context.bot, is_active=svc.paid_features)


REMINDER_TTL = 6 * 3600  # 방에 올린 기간 안내는 이 시간 뒤 자동 삭제


async def job_sub_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """매일 10시: 유료는 3일 안에 끝나는 방, 체험은 마지막 날만 / 어제 끝난 방에 1번 (방엔 금액 없이 '봇 설정' 버튼만)."""
    svc, bot = _svc(context), context.bot
    if not (svc.billing and svc.billing.enabled):
        return
    now, tz, name = int(time.time()), svc.cfg.tz, svc.cfg.bot_name
    for row in await svc.db.subscriptions_expiring(now + 1, now + 3 * 86400):   # 끝난 방 안내는 끝난 그 시각에 (job_tick → _notify_ended)
        chat_id, until = row["chat_id"], row["until"]
        trial = not (row["paid_until"] and row["paid_until"] >= until)
        if trial:
            if until - now > 86400:
                continue  # 체험(3일)은 첫날부터 매일 말고 마지막 날 한 번만
            when = "오늘" if datetime.fromtimestamp(until, tz).date() == datetime.fromtimestamp(now, tz).date() else "내일"
            text = (f"⏳ {name} 무료 체험이 {when}({datetime.fromtimestamp(until, tz):%H:%M}) 끝나요. "
                    "관리자님은 아래 버튼에서 연장할 수 있어요.")
        else:
            days = max(1, (until - now + 86399) // 86400)
            text = f"⏳ 이 방의 {name} 이용 기간이 {days}일 남았어요. 관리자님은 아래 버튼에서 연장할 수 있어요."
        await send_temp(context, chat_id, text, REMINDER_TTL,
                        reply_markup=subscription.setup_button(bot.username, chat_id))
        got_report: set[int] = set()
        if trial and until > now:  # 체험 마지막 날: 텔레그램 관리자들 1:1 로 체험 동안 활동 리포트 + 결제 화면 (1번만)
            try:
                got_report = await reports.send_trial_report(svc, bot, chat_id, until)
            except Exception:
                log.exception("trial report failed for %s", chat_id)
        if row["added_by"] and row["added_by"] not in got_report and \
                await _is_admin_safe(svc, bot, chat_id, row["added_by"], fresh=True):
            await subscription.send_panel_dm(svc, bot, chat_id, row["added_by"])


async def _notify_ended(context: ContextTypes.DEFAULT_TYPE) -> None:
    """30초 틱: 체험·이용 기간이 방금 끝난 방에 그 시각에 바로 안내 1번 + 초대한 관리자 1:1 결제 화면 (예전엔 다음 날 10시).
    끝난 지 하루 안 된 방만, (방, 끝난 시각) claim 으로 한 번 — 재시작해도 두 번 안 감."""
    svc, bot = _svc(context), context.bot
    if not (svc.billing and svc.billing.enabled):
        return
    now = int(time.time())
    for row in await svc.db.subscriptions_expiring(now - 86400, now):
        chat_id, until = row["chat_id"], row["until"]
        if until > now or not await persist.claim(svc.db, f"sub_ended:{chat_id}:{until}", 3 * 86400):
            continue
        trial = not (row["paid_until"] and row["paid_until"] >= until)
        await send_temp(context, chat_id, ENDED_NOTICE.format(name=svc.cfg.bot_name, what="무료 체험" if trial else "이용 기간"),
                        REMINDER_TTL, reply_markup=subscription.setup_button(bot.username, chat_id))
        if row["added_by"] and await _is_admin_safe(svc, bot, chat_id, row["added_by"], fresh=True):
            await subscription.send_panel_dm(svc, bot, chat_id, row["added_by"])


async def job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """10분마다: 관리자 AI 하루 요약 시각이 된 방에 하루 1번 (sodam/reports.py)."""
    try:
        await reports.run_digests(_svc(context), context.bot)
    except Exception:
        log.exception("digest job failed")


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
    await _svc(context).db.prune(int(time.time()))


RIGHTS_KEY = "rights_alert"   # counters: 하루 1번만 알림


async def job_rights(context: ContextTypes.DEFAULT_TYPE) -> None:
    """하루 1번: 봇에게 관리 권한(메시지 삭제·사용자 차단)이 없는 방을 오너에게 알림 — 그 방에선 도배 정리·캡차·뮤트가 안 됨."""
    svc, bot = _svc(context), context.bot
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    if await svc.db.counter(day, 0, RIGHTS_KEY):
        return
    missing = []
    for chat_id in await svc.db.all_chat_ids():
        if chat_id >= 0:
            continue
        svc.perms.forget_bot(chat_id)   # 캐시 말고 지금 권한으로
        try:
            if not await svc.perms.bot_can_moderate(bot, chat_id):
                missing.append(chat_id)
        except Exception:
            log.exception("rights check failed for %s", chat_id)
    await svc.db.bump(day, 0, RIGHTS_KEY)
    if missing:
        titles = [esc(await subscription.chat_title(svc, c)) for c in missing]
        await svc.mod.report(bot, "⚠️ 봇에게 관리 권한이 없는 방: " + ", ".join(titles) + "\n"
                                  "이 방에선 도배·링크 정리, 캡차, 경고·뮤트가 동작하지 않아요. 방장이 봇을 관리자로 올리고 "
                                  "'메시지 삭제'·'사용자 차단' 권한을 켜 주세요.")


async def job_quiet_rules(context: ContextTypes.DEFAULT_TYPE) -> None:
    """10분마다: 알림 규칙 '방이 N시간 조용하면' (sodam/rules.py)."""
    await rules.check_quiet(_svc(context), context.bot)


async def job_gametime(context: ContextTypes.DEFAULT_TYPE) -> None:
    """10분마다: 장시간 게임 알림 (sodam/gametime.py)."""
    await gametime.check(_svc(context), context.bot)


async def job_news(context: ContextTypes.DEFAULT_TYPE) -> None:
    """1분마다: 세계 뉴스 알림 (sodam/news.py — 켠 방이 있을 때만 10분마다 피드 가져오기, 정리 시각·속보)."""
    try:
        await news.run(_svc(context), context.bot)
    except Exception:
        log.exception("news job failed")


async def job_disk(context: ContextTypes.DEFAULT_TYPE) -> None:
    """1시간마다: 디스크 여유 공간이 모자라면 정리·오너 알림 (sodam/diskguard.py)."""
    try:
        await diskguard.check(_svc(context), context.bot)
    except Exception:
        log.exception("disk check failed")


def reply_ref(msg) -> tuple[int | None, int | None]:
    """(답장한 메시지 ID, 답장받은 사람 ID). 답장 아님·포럼 토픽 첫 글(모든 글이 거기에 답장으로 옴)이면 (None, None).
    채널·익명 관리자 글에 답장하면 사람은 모름 → (메시지 ID, None)."""
    r = getattr(msg, "reply_to_message", None)
    if r is None or getattr(r, "forum_topic_created", None) or (
            getattr(msg, "is_topic_message", False) and r.message_id == getattr(msg, "message_thread_id", None)):
        return None, None
    who = getattr(r, "from_user", None) if getattr(r, "sender_chat", None) is None else None
    return getattr(r, "message_id", None), (who.id if who else None)


async def _record(coro) -> None:
    """대화·멤버 기록. 디스크가 가득 차서 못 써도 관리(스팸 삭제·제재)·명령·AI 는 계속 (diskguard 가 오너에게 알림)."""
    try:
        await coro
    except sqlite3.OperationalError as e:
        if not disk_full(e):
            raise
        log.warning("기록 못 함 (디스크 가득 참): %s", e)


BOT_MENU = [
    BotCommand("help", "소담 사용법 (말 예시)"),
    BotCommand("commands", "명령어 전체 목록"),
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
    BotCommand("play", "🎵 노래 틀기 / 대기열 추가 (제목·링크)"),
    BotCommand("skip", "🎵 다음 곡"),
    BotCommand("pause", "🎵 일시정지"),
    BotCommand("resume", "🎵 다시 재생"),
    BotCommand("queue", "🎵 대기열"),
    BotCommand("remove", "🎵 대기열에서 곡 빼기 (번호)"),
    BotCommand("seek", "🎵 위치 이동 (초)"),
    BotCommand("volume", "🎵 노래 음량 (0~200)"),
    BotCommand("end", "🎵 노래 끝 (음성채팅 나가기)"),
    BotCommand("userbotjoin", "🎵 노래 도우미 계정 방에 부르기"),
    BotCommand("shuffle", "🎵 대기열 섞기"),
    BotCommand("autoplay", "🎵 자동 재생 켜기·끄기"),
    BotCommand("lyrics", "🎵 지금 곡 가사"),
    BotCommand("topsongs", "🎵 이 방 인기곡"),
]


# ── 게임 전용 딜러 봇 (BOT_ROLE=dealer) ──────────────────
async def on_dealer_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """딜러 봇: 그룹의 '!' 명령만 처리 (관리·AI·인사는 메인 봇 몫)."""
    msg = update.message
    if not msg or not msg.from_user or msg.sender_chat or msg.from_user.is_bot:   # 다른 봇의 '!' 에 게임 진행 안 함 (봇끼리 반복)
        return
    svc, bot = _svc(context), context.bot
    text = (msg.text or "").strip()
    if not text.startswith("!"):
        return
    await svc.db.ensure_chat(msg.chat_id, msg.chat.title)
    await svc.db.upsert_user(msg.from_user)
    await svc.db.touch_member(msg.chat_id, msg.from_user.id)
    await casino.dispatch(svc, bot, msg, msg.chat_id, msg.from_user, Role.MEMBER, text)


async def on_dealer_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or (msg.from_user and msg.from_user.is_bot):   # 봇의 1:1 엔 안내도 안 보냄
        return
    bot = context.bot
    await msg.reply_text(
        "🃏 <b>딜러 소담</b>이에요. 그룹에서 포인트 게임을 진행해요.\n"
        "그룹에 추가하고 <code>!가입</code> → <code>!도움</code>\n"
        "(P는 게임 포인트예요. 돈으로 바꿀 수 없어요)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "➕ 그룹에 딜러 추가", url=f"https://t.me/{bot.username}?startgroup=true")]]))


def register_dealer(app: Application) -> None:
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & filters.Regex(r"^!"), on_dealer_group))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, on_dealer_private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)


def register(app: Application, tz, backup_time: str = "05:00", role: str = "all") -> None:
    if role == "dealer":
        register_dealer(app)
        return
    groups = filters.ChatType.GROUPS
    app.add_handler(TypeHandler(Update, on_any_update), group=-1)  # 이름 기록 (다른 처리보다 먼저, 막지 않음)
    # 반응·가입 요청·수정된 메시지는 기록만 하면 돼서 별도 처리 없음 (TypeHandler 가 봄)
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left))
    app.add_handler(MessageHandler(groups & filters.UpdateType.MESSAGE & ~filters.StatusUpdate.ALL, on_group_message))
    app.add_handler(MessageHandler(groups & filters.UpdateType.EDITED_MESSAGE, on_group_edit))  # 고쳐서 광고 넣기 막기
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, on_private))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_post))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    jq = app.job_queue
    hh, mm = map(int, backup_time.split(":"))
    jq.run_repeating(job_tick, interval=30, first=10, name="tick")
    jq.run_repeating(job_name_sweep, interval=60, first=90, name="name_sweep")
    jq.run_repeating(job_sports, interval=30, first=45, name="sports")   # 리그별로 60초(경기 중)·6시간(일정)만 실제로 받음 (sports/alerts.py)
    jq.run_daily(job_daily_report, time=dtime(23, 50, tzinfo=tz), name="daily_report")
    jq.run_daily(job_backup, time=dtime(hh, mm, tzinfo=tz), name="backup")
    jq.run_daily(job_prune, time=dtime(4, 0, tzinfo=tz), name="prune")
    jq.run_repeating(job_disk, interval=3600, first=300, name="disk")
    jq.run_repeating(job_rights, interval=3600, first=600, name="rights")   # 안에서 하루 1번만 알림
    jq.run_repeating(job_gametime, interval=600, first=180, name="gametime")
    jq.run_repeating(job_news, interval=60, first=150, name="news")
    jq.run_repeating(job_quiet_rules, interval=600, first=240, name="quiet_rules")
    jq.run_daily(job_sub_reminders, time=dtime(10, 0, tzinfo=tz), name="sub_reminders")
    jq.run_repeating(job_digest, interval=600, first=120, name="digest")  # 관리자 AI 하루 요약 (reports.py)
