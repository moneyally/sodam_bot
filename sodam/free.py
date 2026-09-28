"""🕊️ 자유 멤버 (.free): 관리자가 지정한 멤버는 소담의 자동 통제를 받지 않는다.

빠지는 것: 도배·반복·금지어·링크·@아이디·종류 잠금·전달 검사, 사칭 검사, 입장 캡차·CAS·공동 차단 검사,
사기 의심 검사, 봇 조작 시도 자동 경고. 관리자 권한은 아니다 (관리 명령·설정 불가, 관리자의 수동 제재는 그대로).
지정할 때 걸려 있던 채팅 금지·캡차 대기·경고는 풀어준다 (commands.c_free).
"""
from __future__ import annotations

import time

from .db import DB, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS free_members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    by_id INTEGER,
    ts INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"free_members": "composite"})


async def is_free(db: DB, chat_id: int, user_id: int) -> bool:
    """메시지마다 여러 곳(관리 검사·훅들)이 물어서 방마다 목록을 캐시 (add/remove 가 지움)."""
    async def load():
        return frozenset(r["user_id"] for r in await db._all("SELECT user_id FROM free_members WHERE chat_id=?", (chat_id,)))
    return user_id in await db.cached(("free_members", chat_id), load)


async def add(db: DB, chat_id: int, user_id: int, by_id: int) -> None:
    await db._write("INSERT OR REPLACE INTO free_members(chat_id, user_id, by_id, ts) VALUES(?, ?, ?, ?)",
                    (chat_id, user_id, by_id, int(time.time())))
    db.uncache(("free_members", chat_id))


async def remove(db: DB, chat_id: int, user_id: int) -> bool:
    if not await is_free(db, chat_id, user_id):
        return False
    await db._write("DELETE FROM free_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    db.uncache(("free_members", chat_id))
    return True


async def members(db: DB, chat_id: int) -> list:
    return await db._all("SELECT f.user_id, u.first_name, u.last_name, u.username FROM free_members f "
                         "LEFT JOIN users u ON u.user_id=f.user_id WHERE f.chat_id=? ORDER BY f.ts", (chat_id,))
