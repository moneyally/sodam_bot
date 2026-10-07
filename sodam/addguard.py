"""🚫 강제 추가 막기 (add_guard, 기본 kick — 모든 방).

실제 2026-10-07 백악관: 관리자가 아닌 누군가가 연락처의 모르는 사람들을 5명씩 3번(같은 초에) 방에 '추가' →
끌려간 사람들의 '스팸 신고'가 쌓여 텔레그램이 방을 막음(Chat_restricted). 가입 신청 승인제여도 '추가'는 그대로 통과하고,
소담은 누가 추가했는지 기록하지 않아서 범인을 못 찾았음.

입장 때 텔레그램이 알려 주는 '추가한 사람'(입장 메시지 from_user / chat_member from_user)이 들어온 사람과 다르고
관리자·봇관리자·자유 멤버가 아니면:
- 언제나: mod_log 'forced_add' (actor = 추가한 사람, target = 추가된 사람) — 다음 사고 땐 DB 에서 바로 범인.
- kick(기본): 추가된 사람을 바로 내보냄(재입장 가능, 본인이 원하면 링크·신청으로 다시 오면 됨).
- 같은 사람이 WINDOW 안에 BURST 명↑ 추가: 추가한 사람 1일 뮤트 + 관리자·오너 알림(사건 묶기, incidents).
- notify: 내보내지 않고 기록·알림만. off: 기록만.
초대 링크·가입 신청으로 직접 들어온 사람(추가한 사람 = 본인)·승인한 관리자는 해당 없음.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from telegram.error import TelegramError

from . import free
from .settings import register_setting
from .util import mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

register_setting("add_guard", "kick", "강제 추가 막기",
                 choices={"off": "off", "끔": "off", "notify": "notify", "알림": "notify", "kick": "kick", "내보내기": "kick"},
                 choice_labels={"off": "끔 (기록만)", "notify": "알림만", "kick": "내보내기"})

WINDOW = 600          # 몰아서 추가 판단 창 (초)
BURST = 3             # 이만큼 추가하면 추가한 사람 뮤트
MUTE_MIN = 24 * 60


def _now() -> int:
    return int(time.time())


async def _recipients(svc: Services, bot, chat_id: int) -> list:
    from .anomaly import recipients   # 늦게 import (anomaly → handlers 순환 방지)
    return await recipients(svc, bot, chat_id)


async def check(svc: Services, bot, chat_id: int, user, by, s: dict) -> bool:
    """들어온 사람(user)을 다른 사람(by)이 추가했으면 처리. 내보냈으면 True (입장 처리 그만)."""
    if by is None or by.id == user.id or getattr(by, "is_bot", False):
        return False
    if await svc.perms.is_admin(bot, chat_id, by.id) or await free.is_free(svc.db, chat_id, by.id):
        return False
    mode = s.get("add_guard", "kick")
    await svc.db.log_mod(chat_id, by.id, user.id, "forced_add",
                         f"{user_name(by)}(@{by.username or '-'}) 가 {user_name(user)}(@{user.username or '-'}) 추가")
    if mode == "off":
        return False
    kicked = False
    if mode == "kick":
        try:
            await svc.mod.kick(bot, chat_id, user.id, None, f"강제 추가됨 (추가한 사람 {by.id})")
            kicked = True
        except TelegramError as e:
            log.warning("add_guard kick failed %s/%s: %s", chat_id, user.id, e)
    row = await svc.db._one("SELECT COUNT(*) AS n FROM mod_log WHERE chat_id=? AND actor_id=? AND action='forced_add' AND ts>=?",
                            (chat_id, by.id, _now() - WINDOW))
    n = row["n"] if row else 1
    if n >= BURST:
        muted = False
        if n == BURST:   # 처음 넘을 때 한 번만 뮤트
            try:
                await svc.mod.mute(bot, chat_id, by.id, MUTE_MIN, None, f"사람 강제 추가 {n}명 / {WINDOW // 60}분")
                muted = True
            except TelegramError as e:
                log.warning("add_guard mute failed %s/%s: %s", chat_id, by.id, e)
        from .moderation import owner_kb   # 늦게 import (순환 방지)
        text = (f"🚫 강제 추가 감지 — {mention(by.id, user_name(by))}(<code>{by.id}</code>)가 {WINDOW // 60}분에 "
                f"<b>{n}명</b>을 방에 추가했어요.\n"
                + ("추가된 사람은 바로 내보냈어요. " if mode == "kick" else "")
                + ("추가한 사람은 1일 채팅 금지했어요. " if muted else "")
                + "\n⚠️ 모르는 사람을 억지로 넣으면 그 사람들이 '스팸 신고'를 눌러 방이 막힐 수 있어요. "
                  "그룹 설정 → 권한 → '사용자 추가'를 꺼 두세요.")
        try:
            await svc.mod.incident(bot, chat_id, "forced_add", text, owner_kb(chat_id, by.id, "mute"), key=by.id,
                                   extra=await _recipients(svc, bot, chat_id))
        except Exception:
            log.exception("add_guard alert failed")
    return kicked

