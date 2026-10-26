"""🔥 시크릿 최고 수위 욕받이 (오너만, 2026-10-26 오너 요청 '최고수위 욕·패드립 시크릿 설정').

방 설정(settings)이 아니라 chat_state 에 숨긴다 → `.설정`·`.설정변경`·AI change_setting·
room_checkup·📒 어디에서도 안 보이고 못 바꾼다. 그래서 **대화 속 어떤 지시문(인젝션)으로도
안 켜지고 안 꺼진다** — 오너가 `.비욕` 명령으로만 (commands.c_savage). '나만 알 수 있게.'

켜진 방: prompt.COMEBACK_SAVAGE 를 세 번째 system + (욕/패드립이면) SAVAGE_TAIL (agent._run).
지킬 선(풀지 않음): 미성년 금지 · 그만·싫다는·안 낀 멤버 X · 상대 선공 때만 · 실제 신상·협박 X.
"""
from __future__ import annotations

from .db import DB

KEY = "ai_savage_max"


async def on(db: DB, chat_id: int) -> bool:
    """매 AI 호출마다 물어서 방마다 캐시 (set_on 이 비움)."""
    async def load() -> bool:
        return bool(await db.get_state(chat_id, KEY))
    return await db.cached(("savage", chat_id), load)


async def set_on(db: DB, chat_id: int, value: bool) -> None:
    await db.set_state(chat_id, KEY, True if value else None)
    db.uncache(("savage", chat_id))
