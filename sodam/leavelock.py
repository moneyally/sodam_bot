"""🚪 나간 사람 재입장 막기 (2026-10-09 뉴월드 '한 번 나가면 못 들어오게').

스스로 나간 사람만(관리자·자동 관리가 내보낸 건 아님) 나간 순간 그 방에서 밴 → 링크로도 못 들어옴.
기간 leave_lock_hours (0 = 영구) — 텔레그램 밴 until_date 로 걸어서 기간이 끝나면 텔레그램이 알아서 풂.
중간에 풀기 = .밴해제 @아이디 (보통 밴과 같음). 자유 멤버·오너·봇은 빼. 관리 기록 mod_log 'leave_lock'.
소담에게 '사용자 차단' 권한이 없으면 못 막음 → chat_state leave_lock_fail 에 남겨 🚪 화면에 보임.
"""
from __future__ import annotations

import logging
import re
import time

from telegram.error import TelegramError

from . import free, persist
from .settings import register_setting

log = logging.getLogger(__name__)

CLAIM_SECONDS = 60      # 같은 나감이 서비스 메시지·멤버 상태 두 길로 와도 한 번만
FAIL_KEY = "leave_lock_fail"

register_setting("leave_lock", "off", "나간 사람 재입장 막기",
                 choices={"on": "on", "켜기": "on", "켬": "on", "off": "off", "끄기": "off", "끔": "off"},
                 choice_labels={"on": "켜짐", "off": "꺼짐"})
register_setting("leave_lock_hours", 168, "재입장 막는 시간(시간, 0=영구)", range_=(0, 8760))


def period_label(hours: int) -> str:
    hours = int(hours or 0)
    if hours <= 0:
        return "영구"
    if hours % 24 == 0:
        return f"{hours // 24}일"
    return f"{hours}시간"


def parse_period(text: str) -> int:
    """'영구'·'0' → 0, '3일' → 72, '12시간'·'12' → 12. 범위 밖·모르는 말이면 ValueError."""
    t = re.sub(r"\s+", "", (text or "").strip())
    if t in ("영구", "영원히", "무기한", "forever", "0"):
        return 0
    m = re.fullmatch(r"(\d{1,4})(일|d|시간|h)?", t, re.I)
    if not m:
        raise ValueError("숫자(시간) 또는 '3일'·'12시간'·'영구' 로 보내 주세요.")
    n = int(m.group(1)) * (24 if (m.group(2) or "").lower() in ("일", "d") else 1)
    if not 0 <= n <= 8760:
        raise ValueError("최대 365일까지예요 (더 길게는 '영구').")
    return n


async def on_leave(context, chat_id: int, user, by=None, *, kicked: bool = False) -> None:
    """handlers 가 나감을 볼 때마다 (farewell.on_leave 와 같은 자리). 실패해도 나감 처리는 계속."""
    try:
        svc = context.bot_data["svc"]
        if not user or user.is_bot or kicked or not by or by.id != user.id:
            return                                    # 관리자·자동 관리가 내보냄 → 여기 일 아님
        s = await svc.db.get_settings(chat_id)
        if s.get("leave_lock", "off") != "on":
            return
        if not await persist.claim(svc.db, f"leavelock:{chat_id}:{user.id}", CLAIM_SECONDS):
            return
        if user.id in await svc.perms.owners() or await free.is_free(svc.db, chat_id, user.id):
            return
        hours = int(s.get("leave_lock_hours") or 0)
        until = int(time.time()) + hours * 3600 if hours > 0 else None
        try:
            await context.bot.ban_chat_member(chat_id, user.id, until_date=until)
        except TelegramError as e:
            log.warning("재입장 막기 실패 %s/%s: %s", chat_id, user.id, e)
            await svc.db.set_state(chat_id, FAIL_KEY, {"ts": int(time.time()), "why": str(e)[:120]})
            return
        await svc.db.log_mod(chat_id, context.bot.id, user.id, "leave_lock", period_label(hours))
    except Exception:
        log.exception("leave lock failed in %s", chat_id)
