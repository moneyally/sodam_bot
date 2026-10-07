"""🧠 소담이 교훈 노트: 관리자가 정정해 준 '일하는 법'을 방마다 적어 두고 다음 답에 데이터로 넣는다.

(Claude 의 CLAUDE.md 처럼 — 모델은 그대로, 적어 둔 걸 다음에 읽음.) 역할 나눔 — 겹치지 않게:
- 📝 AI 방 안내(ai_instructions) = 어떻게 말할지(캐릭터·말투·호칭), 확인 카드 · 📚 자료(knowledge) = 멤버에게 전할 사실
- 🧠 교훈(여기) = 일하는 법 정정 ('케테르 플레이어 배팅은 /플', '예약은 한국 시각으로'). 멤버에게 말하는 사실·규칙 아님.
안전: 텔레그램 관리자·오너만, 이 답변에서 기록·봇 글을 읽었으면 안 됨(관리자 본인 말에서만), 제재·권한·링크·지갑·지시문 거절,
글자 수·방마다 개수 상한(오래된 것부터 밀림), AI 엔 system 이 아닌 nonce 태그 안 데이터로만. 저장·삭제는 mod_log.
"""
from __future__ import annotations

import re
import time

from . import security
from .db import register_schema

MAX_CHARS = 150
MAX_PER_ROOM = 20
IN_PROMPT = 12
# 제재·권한은 교훈으로 못 바꿈 (확인 카드·권한 규칙을 말로 우회하지 않게)
SANCTION = re.compile(r"밴|뮤트|강퇴|추방|경고|차단|제재|내보내|채팅\s*금지|권한|관리자\s*(로|를|추가)|\b(ban|mute|kick|warn)\b", re.I)

register_schema("""
CREATE TABLE IF NOT EXISTS ai_lessons (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text    TEXT NOT NULL,
    by_user INTEGER NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_lessons_chat ON ai_lessons(chat_id, ts);
""", migrate={"ai_lessons": "plain"})   # 방 ID 가 바뀌면 교훈도 새 방으로 (2026-10-07 백악관 이전 때 빠져 있었음)


def clean(text: str) -> str:
    return " ".join(str(text or "").split())


def check(text: str) -> str | None:
    """저장해도 되는지. 안 되면 이유."""
    if len(text) < 4:
        return "내용이 비었음."
    if len(text) > MAX_CHARS:
        return f"{MAX_CHARS}자까지 (지금 {len(text)}자)."
    if SANCTION.search(text):
        return "제재·권한에 관한 건 교훈으로 저장하지 않음 (제재는 정해진 규칙대로 — 밴·강퇴는 확인 버튼)."
    if security.scan(text).blocked or security.defang(text) != text:
        return "봇 규칙을 바꾸려는 지시문 같은 문장이라 안 됨."
    if security.find_links(text) or any(w.search(text) for w in security.WALLETS):
        return "링크·지갑 주소는 넣을 수 없음."
    return None


async def list_(db, chat_id: int) -> list:
    return await db._all("SELECT id, text, by_user, ts FROM ai_lessons WHERE chat_id=? ORDER BY ts DESC, id DESC",
                         (chat_id,))


async def add(db, chat_id: int, text: str, by: int) -> bool:
    """새로 넣으면 True, 같은 글이 있으면 시각만 새로 False. 상한 넘으면 오래된 것부터 지움. check 는 부르는 쪽이 먼저."""
    now = int(time.time())

    def work(c):
        if c.execute("UPDATE ai_lessons SET ts=?, by_user=? WHERE chat_id=? AND text=?", (now, by, chat_id, text)).rowcount:
            return False
        c.execute("INSERT INTO ai_lessons(chat_id, text, by_user, ts) VALUES(?,?,?,?)", (chat_id, text, by, now))
        c.execute("DELETE FROM ai_lessons WHERE chat_id=? AND id NOT IN (SELECT id FROM ai_lessons WHERE chat_id=? "
                  "ORDER BY ts DESC, id DESC LIMIT ?)", (chat_id, chat_id, MAX_PER_ROOM))
        c.execute("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                  (chat_id, by, None, "ai_lesson", text[:100], now))
        return True
    return await db.atomic(work)


async def delete(db, chat_id: int, lesson_id: int, by: int) -> bool:
    def work(c):
        row = c.execute("SELECT text FROM ai_lessons WHERE id=? AND chat_id=?", (lesson_id, chat_id)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM ai_lessons WHERE id=?", (lesson_id,))
        c.execute("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                  (chat_id, by, None, "ai_lesson_del", row[0][:100], int(time.time())))
        return True
    return await db.atomic(work)


async def for_prompt(db, chat_id: int) -> list[str]:
    return [r["text"] for r in (await list_(db, chat_id))[:IN_PROMPT]]
