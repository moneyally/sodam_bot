"""🛡️ 보호 멤버 (.보호): 소담을 통한 제재(밴·뮤트·경고·강퇴)를 거절하는 멤버.

`.free`(자유 멤버)는 **자동** 통제만 면제지 관리자의 수동 제재는 그대로였음 → 2026-10-20 오너 '밴 못하게 처리'.
보호 멤버는 관리자가 소담한테 밴/뮤트/경고/강퇴를 시켜도 `permissions.shield` 가 거절한다.
- **오너는 보호 멤버도 제재 가능** (우선권). 그 밖 관리자는 소담으로 제재 불가.
- **단, 텔레그램 앱에서 관리자가 직접 밴하는 건 못 막는다** (소담을 안 거치므로 — 그건 관리자 승격뿐).
넣고 빼기 = '사용자 차단' 권한 관리자 + 오너 (`.free` 와 같은 권한). 명령 `.보호/.보호해제/.보호목록`,
AI 도구 `protect_member`, 버튼 화면 🛡️(`panels/protect.py`). 구조는 free.py 를 그대로 따른다 (방별 캐시·즉시 반영).
"""
from __future__ import annotations

import time

from .db import DB, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS protected_members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    by_id INTEGER,
    ts INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"protected_members": "composite"})


async def is_protected(db: DB, chat_id: int, user_id: int) -> bool:
    """제재 검사(shield)가 물어서 방마다 목록을 캐시 (add/remove 가 지움)."""
    async def load():
        return frozenset(r["user_id"] for r in
                         await db._all("SELECT user_id FROM protected_members WHERE chat_id=?", (chat_id,)))
    return user_id in await db.cached(("protected_members", chat_id), load)


async def add(db: DB, chat_id: int, user_id: int, by_id: int) -> None:
    await db._write("INSERT OR REPLACE INTO protected_members(chat_id, user_id, by_id, ts) VALUES(?, ?, ?, ?)",
                    (chat_id, user_id, by_id, int(time.time())))
    db.uncache(("protected_members", chat_id))


async def remove(db: DB, chat_id: int, user_id: int) -> bool:
    if not await is_protected(db, chat_id, user_id):
        return False
    await db._write("DELETE FROM protected_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    db.uncache(("protected_members", chat_id))
    return True


async def members(db: DB, chat_id: int) -> list:
    return await db._all("SELECT p.user_id, u.first_name, u.last_name, u.username FROM protected_members p "
                         "LEFT JOIN users u ON u.user_id=p.user_id WHERE p.chat_id=? ORDER BY p.ts", (chat_id,))
