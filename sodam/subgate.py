"""📢 채널 구독 필수 (subgate, 선택 기능 — 기본 꺼짐). 2026-10-07 백악관 요청 '채팅 치기 전 채팅 제한 + 구독 확인 버튼·채널 입장 링크'.

켜면 (subgate_mode=on, subgate_channel=@채널 또는 -100… ID):
- 새로 들어온 사람: 그 채널 구독자가 아니면 바로 채팅 금지 + 방에 안내 [📢 채널 들어가기][✅ 구독 확인].
- 이미 있던 사람: 말할 때 구독자가 아니면 그 글을 지우고 채팅 금지 + 같은 안내 (사람당 60초에 한 번).
- [✅ 구독 확인]은 본인만. 텔레그램에 실제로 물어봐서(getChatMember) 구독했으면 채팅 금지 풀고 안내 지움.
관리자·봇관리자·자유 멤버·봇은 제외. 소담이 그 채널 관리자가 아니면(구독 여부를 못 봄) 막지 않고 관리자에게 하루 한 번 알림.
구독 확인된 사람은 30분 캐시 (말할 때마다 텔레그램에 묻지 않게).
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError

from . import free, hooks, persist
from .settings import register_setting
from .util import mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)


def _channel(v) -> str:
    """'@name' · 't.me/name' · '-100…' → 저장 꼴. 아니면 ValueError."""
    t = str(v or "").strip()
    if not t:
        return ""
    m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})/?", t) or re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,31})", t)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"-100\d{6,15}", t):
        return t
    raise ValueError("채널은 @아이디, t.me/아이디 또는 -100으로 시작하는 숫자 ID")


register_setting("subgate_mode", "off", "채널 구독 필수",
                 choices={"off": "off", "끔": "off", "끄기": "off", "on": "on", "켬": "on", "켜기": "on"},
                 choice_labels={"off": "끔", "on": "켬"})
register_setting("subgate_channel", "", "구독할 채널", validator=_channel)

OK_TTL = 30 * 60      # 구독 확인된 사람 캐시
NAG_GAP = 60          # 같은 사람 안내 간격
MEMBER = ("member", "administrator", "creator", "owner")
_ok: dict[tuple, float] = {}
_nag: dict[tuple, float] = {}


def active(s: dict) -> bool:
    return s.get("subgate_mode") == "on" and bool(s.get("subgate_channel"))


async def subscribed(bot, channel: str, user_id: int) -> bool | None:
    """True/False, 확인 못 하면 None (소담이 채널 관리자가 아님·채널 없음)."""
    target = int(channel) if channel.lstrip("-").isdigit() else channel
    try:
        m = await bot.get_chat_member(target, user_id)
    except BadRequest as e:
        if "user not found" in (e.message or "").lower() or "participant" in (e.message or "").lower():
            return False
        log.warning("subgate check failed %s: %s", channel, e)
        return None
    except (Forbidden, TelegramError) as e:
        log.warning("subgate check failed %s: %s", channel, e)
        return None
    st = getattr(m, "status", "")
    return st in MEMBER or (st == "restricted" and bool(getattr(m, "is_member", False)))


async def link(svc: Services, bot, channel: str) -> str | None:
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    key = f"subgate_link:{channel}"
    saved = await svc.db.get_state(0, key)
    if saved:
        return saved
    try:
        url = (await bot.create_chat_invite_link(int(channel), name="sodam 구독 확인")).invite_link
    except TelegramError as e:
        log.warning("subgate link failed %s: %s", channel, e)
        return None
    await svc.db.set_state(0, key, url)
    return url


async def _exempt(svc: Services, bot, chat_id: int, user) -> bool:
    return user.is_bot or await svc.perms.is_admin(bot, chat_id, user.id) or await free.is_free(svc.db, chat_id, user.id)


async def _warn_admins(svc: Services, bot, chat_id: int, channel: str) -> None:
    if not await persist.claim(svc.db, f"subgate_blind:{chat_id}", 86400):
        return
    try:
        await svc.mod.incident(bot, chat_id, "subgate", f"📢 채널 구독 필수가 켜져 있는데 소담이 {channel} 구독 여부를 확인 못 해요.\n"
                               "소담을 그 채널의 <b>관리자</b>로 넣어 주세요. 그 전엔 아무도 막지 않아요.")
    except Exception:
        log.exception("subgate warn failed")


async def _prompt(svc: Services, bot, chat_id: int, user, channel: str) -> None:
    key = (chat_id, user.id)
    if time.monotonic() - _nag.get(key, -1e9) < NAG_GAP:
        return
    _nag[key] = time.monotonic()
    url = await link(svc, bot, channel)
    rows = ([[InlineKeyboardButton("📢 채널 들어가기", url=url)]] if url else []) + \
           [[InlineKeyboardButton("✅ 구독 확인", callback_data=f"sg:{user.id}")]]
    try:
        sent = await bot.send_message(chat_id, f"👋 {mention(user.id, user_name(user))}님, 이 방은 <b>채널 구독</b> 후 채팅할 수 있어요.\n"
                                      "아래 채널에 들어간 뒤 [✅ 구독 확인]을 눌러 주세요.",
                                      parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        await svc.db.set_state(chat_id, f"subgate_msg:{user.id}", sent.message_id)
    except TelegramError as e:
        log.warning("subgate prompt failed %s: %s", chat_id, e)


async def _block(svc: Services, bot, chat_id: int, user, channel: str) -> None:
    try:
        await bot.restrict_chat_member(chat_id, user.id, ChatPermissions.no_permissions())
        await svc.db.log_mod(chat_id, None, user.id, "subgate", f"채널 {channel} 구독 전 채팅 금지")
    except TelegramError as e:
        log.warning("subgate restrict failed %s/%s: %s", chat_id, user.id, e)
    await _prompt(svc, bot, chat_id, user, channel)


async def check(svc: Services, bot, chat_id: int, user, s: dict) -> bool | None:
    """구독자면 True, 아니면 False, 확인 못 하면 None (막지 않음)."""
    key = (chat_id, user.id)
    if time.monotonic() - _ok.get(key, -1e9) < OK_TTL:
        return True
    got = await subscribed(bot, s["subgate_channel"], user.id)
    if got:
        _ok[key] = time.monotonic()
    elif got is None:
        await _warn_admins(svc, bot, chat_id, s["subgate_channel"])
    return got


async def on_join(svc: Services, bot, chat_id: int, user, s: dict) -> None:
    """handlers.handle_new_member (캡차를 안 띄운 경우) 끝에서."""
    if not active(s) or await _exempt(svc, bot, chat_id, user):
        return
    if await check(svc, bot, chat_id, user, s) is False:
        await _block(svc, bot, chat_id, user, s["subgate_channel"])


async def gate(svc: Services, bot, msg, s: dict) -> bool:
    """그룹 메시지 관리 검사 안에서 (관리자·자유 멤버는 이미 빠짐). 막았으면 True (그 글은 지움)."""
    user = msg.from_user
    if not active(s) or user is None or user.is_bot:
        return False
    if await check(svc, bot, msg.chat_id, user, s) is not False:
        return False
    try:
        await msg.delete()
    except TelegramError:
        pass
    await _block(svc, bot, msg.chat_id, user, s["subgate_channel"])
    return True


async def on_button(svc, bot, q, parts) -> None:
    """[✅ 구독 확인] sg:<사람> — 본인만."""
    uid = int(parts[0]) if parts and parts[0].isdecimal() else 0
    chat_id = q.message.chat_id
    if q.from_user.id != uid:
        await q.answer("본인만 누를 수 있어요.", show_alert=True)
        return
    s = await svc.db.get_settings(chat_id)
    got = await subscribed(bot, s.get("subgate_channel") or "", uid) if active(s) else True
    if got is False:
        await q.answer("아직 구독이 확인 안 돼요. 채널에 들어간 뒤 다시 눌러 주세요.", show_alert=True)
        return
    _ok[(chat_id, uid)] = time.monotonic()
    try:
        await bot.restrict_chat_member(chat_id, uid, ChatPermissions.all_permissions())
    except TelegramError as e:
        log.warning("subgate unrestrict failed %s/%s: %s", chat_id, uid, e)
    await svc.db.log_mod(chat_id, uid, uid, "subgate_ok", "구독 확인")
    await q.answer("✅ 확인됐어요! 이제 채팅할 수 있어요.")
    try:
        await q.message.delete()
    except TelegramError:
        pass


hooks.add_callback_handler("sg", on_button)
