"""🔞 음란물 자동 감지·삭제 (2026-11-06 오너 급비상: 누가 음란물 스티커·사진 테러).

스티커·사진·GIF·영상이 올라오면 OpenAI omni-moderation(무료·이미지 'sexual' 점수 지원, 하루 5000건)으로 재서
기준(THRESHOLD) 넘으면 바로 지운다. 방별 설정 nsfw_mode off(기본)/delete(지우고 알림)/notify(안 지우고 알림).
2코어 VPS 라 로컬 NSFW 모델(NudeNet·OpenNSFW 등 ONNX/torch)은 CPU 부담 커서 안 씀 — moderation API 가 무료·빠름.

학습 방어: 한번 걸린 조각(file_unique_id)은 nsfw_bad 에 남겨 다음엔 API 없이 **즉시 삭제**(같은 스티커 도배 테러).
오탐 복구: 관리자가 '괜찮음' 누르면 그 방에선 그 조각 다시 안 지움(nsfw_ok).
제외: **봇 오너만**(2026-11-06 오너 '오너 빼고는 못하게 — 나·소유자가 올릴 이유 없으니 좁혀') + 봇·익명/채널 글
(사람 특정·제재 불가라 기술적 제외). **관리자·자유 멤버도 검사함.** **구독과 무관하게 동작**(안전 기능 + moderation 무료).
알림: '사용자 차단' 권한 관리자 1:1 (incidents, 같은 사람 한 통) + 버튼. 자동 밴은 안 함(지우기만).
"""
from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from . import db as dbmod
from . import hooks, incidents, vision
from .permissions import Role
from .settings import register_setting
from .util import esc, mention, post_temp, user_name

log = logging.getLogger(__name__)

THRESHOLD = 0.70          # sexual 점수 이 이상이면 음란물로 봄 (실측상 포르노는 보통 0.9+, 0.7 은 안전margin)
CACHE_TTL = 3600
MODES = {"off": "꺼짐", "delete": "지우고 알림", "notify": "알림만(안 지움)"}
ROOM_NOTICE = "🔞 음란물로 판단되는 미디어를 지웠어요"
_CACHE: dict[str, tuple[float, float]] = {}   # file_unique_id -> (sexual, ts) — 같은 조각 API 중복 방지

register_setting("nsfw_mode", "off", "음란물 자동 차단",
                 choices={**{k: k for k in MODES}, "꺼짐": "off", "끄기": "off", "삭제": "delete", "지우기": "delete",
                          "지우고 알림": "delete", "알림": "notify", "알림만": "notify", "켜기": "delete", "on": "delete"},
                 choice_labels=MODES)

dbmod.register_schema(
    "CREATE TABLE IF NOT EXISTS nsfw_bad (file_uid TEXT PRIMARY KEY, chat_id INTEGER, score REAL, ts INTEGER)")
dbmod.register_schema(   # 관리자가 '괜찮음' 한 조각 — 그 방에선 다시 안 지움
    "CREATE TABLE IF NOT EXISTS nsfw_ok (chat_id INTEGER NOT NULL, file_uid TEXT NOT NULL, "
    "PRIMARY KEY (chat_id, file_uid))")
dbmod.register_schema(
    "CREATE TABLE IF NOT EXISTS nsfw_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, "
    "msg_id INTEGER, name TEXT, file_uid TEXT, score REAL, deleted INTEGER, done TEXT, ts INTEGER)")
MUTE_MINUTES = 1440


def media_obj(msg):
    """메시지의 사진·스티커·GIF·영상·동그라미·이미지 문서 (file_unique_id 있는 객체) 또는 None."""
    for attr in ("sticker", "animation", "video", "video_note"):
        o = getattr(msg, attr, None)
        if o is not None:
            return o
    if getattr(msg, "photo", None):
        return msg.photo[-1]
    doc = getattr(msg, "document", None)
    if doc is not None and (getattr(doc, "mime_type", "") or "").startswith("image/"):
        return doc
    return None


async def on_message(svc, bot, msg, role) -> None:
    chat_id, user = msg.chat_id, msg.from_user
    # 오너만 제외 (관리자·자유 멤버도 검사). 봇·익명/채널 글은 사람 특정·제재가 안 돼 기술적 제외.
    if role >= Role.OWNER or chat_id >= 0 or not user or getattr(user, "is_bot", False) or getattr(msg, "sender_chat", None):
        return
    obj = media_obj(msg)
    if obj is None:
        return
    mode = (await svc.db.get_settings(chat_id)).get("nsfw_mode", "off")
    if mode == "off":
        return
    uid = getattr(obj, "file_unique_id", None)
    if not uid:
        return
    if await svc.db._one("SELECT 1 FROM nsfw_ok WHERE chat_id=? AND file_uid=?", (chat_id, uid)):
        return
    score = await _score(svc, bot, msg, uid)
    if score is None or score < THRESHOLD:
        return
    await act(svc, bot, msg, user, uid, score, mode)


async def _score(svc, bot, msg, uid: str) -> float | None:
    """음란도(sexual 0~1). 이미 '나쁨' 기록·메모리 캐시면 API 없이. 못 받으면 None."""
    bad = await svc.db._one("SELECT score FROM nsfw_bad WHERE file_uid=?", (uid,))
    if bad:
        return float(bad["score"])
    hit = _CACHE.get(uid)
    if hit and time.time() - hit[1] < CACHE_TTL:
        return hit[0]
    att = None
    try:
        md = vision._media_of(msg)
        att = await vision._fetch(bot, msg, md) if md else None
    except Exception as e:
        log.warning("nsfw 이미지 받기 실패: %s", e)
    if not att or not getattr(att, "data", None):
        return None
    v = await svc.llm.moderate_image(att.data, att.mime or "image/jpeg")
    if v is None:
        return None
    score = v["sexual"]
    _CACHE[uid] = (score, time.time())
    if len(_CACHE) > 5000:
        _CACHE.clear()
    if score >= THRESHOLD:
        await svc.db._write("INSERT OR REPLACE INTO nsfw_bad(file_uid, chat_id, score, ts) VALUES(?,?,?,?)",
                            (uid, msg.chat_id, score, dbmod.now()))
    return score


async def act(svc, bot, msg, user, uid: str, score: float, mode: str) -> None:
    chat_id = msg.chat_id
    deleted = False
    if mode == "delete" and await svc.perms.bot_can_moderate(bot, chat_id):
        try:
            await msg.delete()
            deleted = True
        except TelegramError as e:
            log.info("nsfw delete failed %s: %s", chat_id, e)
    if deleted:
        post_temp(bot, chat_id, ROOM_NOTICE, 30)
    await svc.db.log_mod(chat_id, None, user.id, "nsfw_del" if deleted else "nsfw_alert", f"sexual {int(score * 100)}%")
    name = user_name(user)
    alert_id = await svc.db._write(
        "INSERT INTO nsfw_alerts(chat_id, user_id, msg_id, name, file_uid, score, deleted, ts) VALUES(?,?,?,?,?,?,?,?)",
        (chat_id, user.id, msg.message_id, name[:64], uid, score, int(deleted), dbmod.now()))
    from .subscription import chat_title   # 늦게 import (순환 방지)
    body = alert_text(await chat_title(svc, chat_id), user.id, name, score, deleted, mode)
    kb = alert_kb(chat_id, alert_id, deleted)
    admins = [a for a in await svc.perms.admin_users(bot, chat_id) if not getattr(a, "is_bot", False)]
    await incidents.open_or_bump(svc, bot, chat_id, "nsfw", f"nsfw:{user.id}", body, kb, admins, sub=user.id)


def alert_text(title: str, user_id: int, name: str, score: float, deleted: bool, mode: str) -> str:
    if deleted:
        status = "방에서 지웠어요."
    elif mode == "delete":
        status = "⚠️ 봇에게 '메시지 삭제' 권한이 없어서 못 지웠어요 — 방에서 직접 지워주세요."
    else:
        status = "메시지는 방에 그대로 있어요 (알림만 모드)."
    return (f"🔞 <b>음란물로 판단된 미디어</b>\n"
            f"방: <b>{esc(title)}</b>\n"
            f"보낸 사람: {mention(user_id, name)} (ID <code>{user_id}</code>)\n"
            f"음란도: 약 {int(score * 100)}%\n"
            f"{status}\n자동 밴은 안 했어요. 어떻게 할까요?")


def alert_kb(chat_id: int, alert_id: int, deleted: bool) -> InlineKeyboardMarkup:
    cb = f"m:nsx:{chat_id}:{alert_id}:"
    b = InlineKeyboardButton
    first = [b("🚫 밴", callback_data=cb + "b"), b("🔇 뮤트 1일", callback_data=cb + "m")]
    if not deleted:
        first.insert(0, b("🗑 지우기", callback_data=cb + "d"))
    return InlineKeyboardMarkup([first, [b("✅ 오탐(이 조각 통과)", callback_data=cb + "t")]])


hooks.add_group_message_hook(on_message)
