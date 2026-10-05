"""방 데이터 사본 (room.db): 작업실 코드가 sqlite3·pandas·duckdb 로 자유 조회할 것만, 그 방 것만.

- 다른 방 데이터는 사본에 아예 없다 (방마다 따로 만듦).
- 메시지 원문(text)은 관리자·오너 요청일 때만. 멤버 요청이면 text 칸은 비어 있음 (숫자 통계만).
- 비밀·설정·토큰·결제 같은 표는 넣지 않는다. 원본 DB 는 작업실에서 안 보인다 (deploy/sodam-workshop.service).
메모리에서 만들어 bytes 로 넘김 (봇은 data/ 밖에 못 씀). 10분 캐시.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

DAYS = 90
MSG_MAX = 50_000
TTL = 600
_cache: dict[tuple[int, bool], tuple[float, bytes]] = {}

SCHEMA = """
CREATE TABLE people (user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined_at INTEGER, last_seen INTEGER, points INTEGER);
CREATE TABLE messages (ts INTEGER, time_kst TEXT, user_id INTEGER, name TEXT, is_bot INTEGER, reply_to_user INTEGER, text TEXT);
CREATE TABLE casino (user_id INTEGER, joined_at INTEGER, wagered INTEGER, won INTEGER);
CREATE TABLE moderation (ts INTEGER, time_kst TEXT, actor_id INTEGER, target_id INTEGER, action TEXT);
"""
GUIDE = ("room.db (이 방만, 읽기 전용 사본): people(user_id,name,username,joined_at,last_seen,points) · "
         "messages(ts,time_kst,user_id,name,is_bot,reply_to_user,text — 최근 90일, text 는 관리자 요청일 때만) · "
         "casino(user_id,joined_at,wagered,won) · moderation(ts,time_kst,actor_id,target_id,action — 관리자만). "
         "ts 는 유닉스 초, time_kst 는 'YYYY-MM-DD HH:MM' 한국시간.")


def _name(first, last, username) -> str:
    return " ".join(x for x in (first, last) if x).strip() or (f"@{username}" if username else "?")


async def build(db, chat_id: int, admin: bool, tz) -> bytes:
    key = (chat_id, admin)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    since = int(time.time()) - DAYS * 86400
    people = await db._all(
        "SELECT m.user_id, u.first_name, u.last_name, u.username, m.joined_at, m.last_seen, m.points "
        "FROM members m LEFT JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=?", (chat_id,))
    msgs = await db._all(
        "SELECT m.ts, m.user_id, m.is_bot, m.reply_to_user, m.text, u.first_name, u.last_name, u.username "
        "FROM messages m LEFT JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND m.ts>=? AND m.flagged=0 "
        "ORDER BY m.id DESC LIMIT ?", (chat_id, since, MSG_MAX))
    try:
        casino = await db._all("SELECT user_id, joined_at, wagered, won FROM casino_accounts WHERE chat_id=?", (chat_id,))
    except sqlite3.Error:
        casino = []
    mods = await db._all("SELECT ts, actor_id, target_id, action FROM mod_log WHERE chat_id=? AND ts>=? "
                         "AND action NOT LIKE 'ask!_%' ESCAPE '!' AND action NOT LIKE 'press!_%' ESCAPE '!'",
                         (chat_id, since)) if admin else []
    kst = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M") if ts else None  # noqa: E731
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    c.executemany("INSERT OR REPLACE INTO people VALUES(?,?,?,?,?,?)",
                  [(r["user_id"], _name(r["first_name"], r["last_name"], r["username"]), r["username"],
                    r["joined_at"], r["last_seen"], r["points"]) for r in people])
    c.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                  [(r["ts"], kst(r["ts"]), r["user_id"], _name(r["first_name"], r["last_name"], r["username"]),
                    r["is_bot"], r["reply_to_user"], r["text"] if admin else None) for r in reversed(msgs)])
    c.executemany("INSERT INTO casino VALUES(?,?,?,?)", [tuple(r) for r in casino])
    c.executemany("INSERT INTO moderation VALUES(?,?,?,?,?)",
                  [(r["ts"], kst(r["ts"]), r["actor_id"], r["target_id"], r["action"]) for r in mods])
    c.execute("CREATE INDEX i_msg_user ON messages(user_id)")
    c.execute("CREATE INDEX i_msg_ts ON messages(ts)")
    c.commit()
    data = c.serialize()
    c.close()
    _cache[key] = (time.time(), data)
    for k in [k for k, (ts, _) in _cache.items() if time.time() - ts > TTL]:
        _cache.pop(k, None)
    return data
