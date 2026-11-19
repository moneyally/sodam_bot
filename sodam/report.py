"""📢 멤버 신고 (@관리자·.신고): 멤버가 나쁜 글에 **답장**하며 '@관리자'·'@admin'·'.신고' 치면
'사용자 차단' 권한 텔레그램 관리자 1:1 로 조용히 알림(원문·신고자·대상·글 링크). **자동 제재 없음** — 관리자가 보고 판단.

Rose `/report`·Combot 신고 (2026-11-19 경쟁조사). 받는 사람·사건 묶기·1:1 막힘 처리는 incidents.open_or_bump 재활용
(🧭 이상징후·🕵️ 사기 의심과 같은 받는 사람). 기본 꺼짐(report_enabled) — 자동 핑이라 방 관리자가 켤 때만.
한 사람 COOLDOWN 초 1번·방 하루 ROOM_DAILY 번. 같은 사람을 여러 번 신고하면 사건 하나로 묶여 +N.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from telegram.error import TelegramError

from . import incidents
from .hooks import add_group_message_hook
from .security import strip_unsafe
from .settings import register_setting
from .tagnotify import message_link
from .util import esc, mention, user_name

register_setting("report_enabled", False, "멤버 신고(@관리자) 받기")

# 답장하며 이 말이면 신고. '관리자님 고마워요' 같은 평범한 말까지 잡지 않게 신고 의도가 분명한 것만.
TRIGGER = re.compile(r"@관리자|@admin\b|(?:^|\s)[.．․]?\s*(?:신고|report)(?:\b|요|해|함|합니다|좀|할게|들어감|$)", re.I)
COOLDOWN = 60          # 한 사람 신고 간격(초) — 연타 방지
ROOM_DAILY = 30        # 방 하루 신고 수 상한
MAX_PREVIEW = 300
_last: dict[tuple[int, int], float] = {}   # (chat, user) → monotonic ts (메모리 쿨다운, 재시작돼도 됨)


def _preview(m) -> str:
    t = (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()
    if t:
        return esc(strip_unsafe(t)[:MAX_PREVIEW])
    kind = ("사진" if getattr(m, "photo", None) else "영상" if getattr(m, "video", None)
            else "스티커" if getattr(m, "sticker", None) else "파일" if getattr(m, "document", None)
            else "음성" if getattr(m, "voice", None) else "미디어")
    return f"({kind})"


async def handle(svc, bot, msg, role) -> None:
    """그룹 메시지 훅: 답장 + 신고 트리거면 관리자 1:1 로 신고 전달 (report_enabled 켠 방만)."""
    target = getattr(msg, "reply_to_message", None)
    reporter = getattr(msg, "from_user", None)
    if target is None or reporter is None:
        return
    text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    if not TRIGGER.search(text):
        return
    s = await svc.db.get_settings(msg.chat_id)
    if not s.get("report_enabled"):
        return
    reported = getattr(target, "from_user", None)
    if reported is None or reported.id == reporter.id or getattr(reported, "is_bot", False):
        return   # 본인·봇·시스템(익명 채널 등) 글 신고는 무시
    now = time.monotonic()
    key = (msg.chat_id, reporter.id)
    if now - _last.get(key, 0.0) < COOLDOWN:
        return
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    if await svc.db.counter(day, msg.chat_id, "reports") >= ROOM_DAILY:
        return
    from .anomaly import recipients as _recipients   # 늦게 import (순환 방지)
    admins = await _recipients(svc, bot, msg.chat_id)
    if not admins:
        return   # '사용자 차단' 권한 관리자가 없음 → 보낼 곳 없음
    _last[key] = now
    await svc.db.bump(day, msg.chat_id, "reports")
    from .subscription import chat_title   # 늦게 import (순환 방지)
    title = await chat_title(svc, msg.chat_id)
    # 머리말(🚨 감지 · 📢 멤버 신고)은 incidents 가 KINDS 로 붙임. URL 버튼은 incidents 가 버리므로(콜백만 유지) 링크는 본문에.
    link = message_link(msg.chat_id, getattr(getattr(msg, "chat", None), "username", None), target.message_id)
    body = (f"신고자: {mention(reporter.id, user_name(reporter))}\n"
            f"대상: {mention(reported.id, user_name(reported))}\n"
            f"그룹: <b>{esc(title)}</b>\n"
            f"<blockquote>{_preview(target)}</blockquote>"
            + (f'\n🔗 <a href="{esc(link)}">신고된 글 보기</a>' if link else ""))
    await incidents.open_or_bump(svc, bot, msg.chat_id, "report", reported.id, body, None, admins, sub=reported.id)
    try:   # 신고자에게 조용히 확인 (방 글은 안 늘림)
        await bot.set_message_reaction(msg.chat_id, msg.message_id, reaction="👀")
    except TelegramError:
        pass


add_group_message_hook(handle)
