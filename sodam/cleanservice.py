"""🧹 서비스 메시지 청소 (clean_service): 입장/퇴장 + 핀·제목·사진·영상채팅 같은 텔레그램 회색 시스템 메시지를 자동으로 지운다.

Rose `/cleanservice`·GroupHelp 가 가진 기능 (2026-11-19 경쟁조사 — 활발한 방 1순위 민원: 입퇴장·핀 알림이 채팅을 더럽힘).
소담엔 **입장 메시지 삭제(delete_join_message)** 만 있었음 → 퇴장·기타 서비스 메시지는 안 지웠음. 이걸 clean_service 로 확장.
동작은 핸들러(on_join·on_left·on_service)가 이 모듈의 wants() 로 판단해서 msg.delete(). UI 는 panels/cleanservice.py.

기본 꺼짐(off). 봇에 '메시지 삭제' 권한 필요(없으면 조용히 실패 — 다른 청소와 같게 try/except).
"""
from __future__ import annotations

from telegram.ext import filters

from .settings import register_setting

MODES = {"off": "끔", "basic": "입장·퇴장만", "all": "전부 (핀·영상채팅 등)"}
register_setting("clean_service", "off", "서비스 메시지 청소",
                 choices={k: k for k in MODES}, choice_labels=MODES)

# '전부(all)' 모드에서 지우는 기타 서비스 메시지 (clutter). 입장·퇴장·방이사(MIGRATE)는 각자 핸들러가 처리하니 제외.
# 뜻 있는 건(선물·공유·웹앱 데이터 등)은 안 건드리고, 방을 더럽히는 알림만.
OTHER = (
    filters.StatusUpdate.PINNED_MESSAGE
    | filters.StatusUpdate.NEW_CHAT_TITLE
    | filters.StatusUpdate.NEW_CHAT_PHOTO
    | filters.StatusUpdate.DELETE_CHAT_PHOTO
    | filters.StatusUpdate.VIDEO_CHAT_STARTED
    | filters.StatusUpdate.VIDEO_CHAT_ENDED
    | filters.StatusUpdate.VIDEO_CHAT_SCHEDULED
    | filters.StatusUpdate.VIDEO_CHAT_PARTICIPANTS_INVITED
    | filters.StatusUpdate.MESSAGE_AUTO_DELETE_TIMER_CHANGED
)


def wants(settings: dict, kind: str) -> bool:
    """이 방이 kind('join'|'leave'|'other') 서비스 메시지를 지우나.
    basic = 입장·퇴장 · all = 거기에 핀·영상채팅 등 기타까지. join 은 옛 delete_join_message 로도 켜짐(핸들러에서 OR)."""
    mode = settings.get("clean_service", "off")
    if kind == "other":
        return mode == "all"
    return mode in ("basic", "all")   # join · leave
