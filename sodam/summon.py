"""📣 집합(소집) — 관리자가 '부르고 싶은 사람'만 골라서 한 번에 @태그.

선택형: 자동 규칙(이벤트·출석 등) 없이 관리자가 직접 고른다 (오너 결정 2026-10-21).
고르는 법 = ① 답장 ② @아이디/숫자ID ③ 이름 검색 ④ 🕐 최근 활동 목록에서 체크.
작업 중 선택은 관리자마다 chat_state(summon_pick:<관리자>) 에 (재시작해도 유지),
저장한 명단은 summon_lists/summon_members 표. 화면은 panels/summon.py.

500~1000명 방이라 전체 멤버를 버튼으로 깔지 않는다 — 검색·최근활동·답장·아이디로만 더한다.
"""
from __future__ import annotations

import asyncio
import json
import time

from telegram import Bot
from telegram.error import TelegramError

from .db import register_schema
from .util import display_name, esc, mention, send_retry

register_schema("""
CREATE TABLE IF NOT EXISTS summon_lists (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name    TEXT NOT NULL,
    by_id   INTEGER,
    ts      INTEGER NOT NULL
);
""", migrate={"summon_lists": "plain"})
register_schema("""
CREATE TABLE IF NOT EXISTS summon_members (
    list_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (list_id, user_id)
);
""")   # chat_id 칸 없음 → chat_id 이사(migrate_chat) 대상 아님. list_id 는 그대로라 저절로 따라감.

MAX_PICK = 200        # 한 번에 부를 수 있는 최대 인원
BATCH = 20            # 한 메시지에 넣는 멘션 수 (텔레그램 엔티티 100 제한 안)
GAP = 1.0            # 메시지 사이 간격(초) — 도배/레이트리밋 방지
MAX_LISTS = 30        # 방마다 저장 명단 상한


def _key(admin_id: int) -> str:
    return f"summon_pick:{admin_id}"


async def picked(db, chat_id: int, admin_id: int) -> list[int]:
    """관리자의 작업 중 선택(순서 유지, 중복 없음)."""
    raw = await db.get_state(chat_id, _key(admin_id), None)
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for x in raw:
        uid = int(x) if isinstance(x, int) or (isinstance(x, str) and x.lstrip("-").isdigit()) else None
        if uid is not None and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


async def _set(db, chat_id: int, admin_id: int, ids: list[int]) -> None:
    await db.set_state(chat_id, _key(admin_id), ids[:MAX_PICK])


async def toggle(db, chat_id: int, admin_id: int, user_id: int) -> bool:
    """있으면 빼고 없으면 넣는다. 결과(지금 선택됨?)를 돌려준다."""
    ids = await picked(db, chat_id, admin_id)
    if user_id in ids:
        ids = [u for u in ids if u != user_id]
        await _set(db, chat_id, admin_id, ids)
        return False
    if len(ids) >= MAX_PICK:
        return False
    ids.append(user_id)
    await _set(db, chat_id, admin_id, ids)
    return True


async def add_ids(db, chat_id: int, admin_id: int, user_ids: list[int]) -> int:
    """여러 명을 선택에 더한다. 실제로 새로 들어간 수."""
    ids = await picked(db, chat_id, admin_id)
    have = set(ids)
    added = 0
    for u in user_ids:
        if u not in have and len(ids) < MAX_PICK:
            ids.append(u)
            have.add(u)
            added += 1
    await _set(db, chat_id, admin_id, ids)
    return added


async def clear(db, chat_id: int, admin_id: int) -> None:
    await db.set_state(chat_id, _key(admin_id), None)


async def member_rows(db, chat_id: int, user_ids: list[int]) -> dict[int, dict]:
    """user_id → {first_name,last_name,username}. 멤버로 안 남아도 users 에 있으면 이름은 가져온다."""
    if not user_ids:
        return {}
    qs = ",".join("?" * len(user_ids))
    rows = await db._all(f"SELECT user_id, first_name, last_name, username FROM users WHERE user_id IN ({qs})",
                         tuple(user_ids))
    return {r["user_id"]: dict(r) for r in rows}


async def recent_members(db, chat_id: int, offset: int, limit: int) -> list[dict]:
    """최근 활동(last_seen) 순 멤버 (봇 제외)."""
    rows = await db._all(
        "SELECT u.user_id, u.first_name, u.last_name, u.username, m.last_seen "
        "FROM members m JOIN users u ON u.user_id=m.user_id "
        "WHERE m.chat_id=? AND u.is_bot=0 ORDER BY m.last_seen DESC, u.user_id "
        "LIMIT ? OFFSET ?", (chat_id, limit, offset))
    return [dict(r) for r in rows]


async def recent_count(db, chat_id: int) -> int:
    row = await db._one("SELECT COUNT(*) n FROM members m JOIN users u ON u.user_id=m.user_id "
                        "WHERE m.chat_id=? AND u.is_bot=0", (chat_id,))
    return row["n"] if row else 0


async def search_members(db, chat_id: int, query: str, limit: int = 20) -> list[dict]:
    """이름·@아이디 앞부분 검색 (봇 제외). find_members 보다 느슨(부분 일치)해서 고를 수 있게."""
    q = query.strip().lstrip("@")
    if not q:
        return []
    like = f"%{q}%"
    rows = await db._all(
        "SELECT u.user_id, u.first_name, u.last_name, u.username FROM members m "
        "JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND u.is_bot=0 AND ("
        "u.first_name LIKE ? OR u.last_name LIKE ? OR u.username LIKE ?) "
        "ORDER BY u.first_name LIMIT ?", (chat_id, like, like, like, limit))
    return [dict(r) for r in rows]


def name_of(row: dict) -> str:
    return display_name(row.get("first_name"), row.get("last_name"), row.get("username")) or str(row.get("user_id"))


# ── 저장 명단 ────────────────────────────────────────────
async def save_list(db, chat_id: int, name: str, user_ids: list[int], by_id: int) -> int | None:
    """작업 선택을 이름 붙여 저장. 같은 이름이면 덮어씀. 명단 id (상한 넘으면 None)."""
    name = name.strip()[:40]
    if not name or not user_ids:
        return None
    existing = await db._one("SELECT id FROM summon_lists WHERE chat_id=? AND name=? COLLATE NOCASE", (chat_id, name))
    if existing is None:
        cnt = await db._one("SELECT COUNT(*) n FROM summon_lists WHERE chat_id=?", (chat_id,))
        if cnt and cnt["n"] >= MAX_LISTS:
            return None

    def run(c):
        cur = c.execute("SELECT id FROM summon_lists WHERE chat_id=? AND name=? COLLATE NOCASE", (chat_id, name))
        row = cur.fetchone()
        if row:
            lid = row[0]
            c.execute("DELETE FROM summon_members WHERE list_id=?", (lid,))
            c.execute("UPDATE summon_lists SET ts=? WHERE id=?", (int(time.time()), lid))
        else:
            cur = c.execute("INSERT INTO summon_lists(chat_id, name, by_id, ts) VALUES(?,?,?,?)",
                            (chat_id, name, by_id, int(time.time())))
            lid = cur.lastrowid
        c.executemany("INSERT OR IGNORE INTO summon_members(list_id, user_id) VALUES(?,?)",
                      [(lid, u) for u in user_ids[:MAX_PICK]])
        return lid
    return await db.atomic(run)


async def lists(db, chat_id: int) -> list[dict]:
    rows = await db._all(
        "SELECT l.id, l.name, (SELECT COUNT(*) FROM summon_members sm WHERE sm.list_id=l.id) n "
        "FROM summon_lists l WHERE l.chat_id=? ORDER BY l.ts DESC", (chat_id,))
    return [dict(r) for r in rows]


async def list_ids(db, chat_id: int, list_id: int) -> list[int]:
    row = await db._one("SELECT id FROM summon_lists WHERE id=? AND chat_id=?", (list_id, chat_id))
    if not row:
        return []
    rows = await db._all("SELECT user_id FROM summon_members WHERE list_id=?", (list_id,))
    return [r["user_id"] for r in rows]


async def delete_list(db, chat_id: int, list_id: int) -> bool:
    row = await db._one("SELECT id FROM summon_lists WHERE id=? AND chat_id=?", (list_id, chat_id))
    if not row:
        return False

    def run(c):
        c.execute("DELETE FROM summon_members WHERE list_id=?", (list_id,))
        c.execute("DELETE FROM summon_lists WHERE id=?", (list_id,))
    await db.atomic(run)
    return True


# ── 집합 보내기 ──────────────────────────────────────────
async def fire(svc, bot: Bot, chat_id: int, user_ids: list[int], header: str) -> int:
    """고른 사람들을 @태그하는 글을 방에 올린다 (배치로). 태그된 수를 돌려준다."""
    ids = list(dict.fromkeys(user_ids))[:MAX_PICK]
    if not ids:
        return 0
    rows = await member_rows(svc.db, chat_id, ids)
    tagged = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        mentions = " ".join(mention(u, name_of(rows.get(u, {"user_id": u}))) for u in chunk)
        text = (f"{esc(header)}\n{mentions}" if i == 0 else mentions)
        try:
            await send_retry(lambda t=text: bot.send_message(chat_id, t, parse_mode="HTML",
                                                             disable_web_page_preview=True))
            tagged += len(chunk)
        except TelegramError:
            pass
        if i + BATCH < len(ids):
            await asyncio.sleep(GAP)
    return tagged
