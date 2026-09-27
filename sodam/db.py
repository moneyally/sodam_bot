"""SQLite 저장소. 모든 SQL은 이 파일에만 둔다 (나중에 Postgres로 옮기기 쉽게)."""
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .settings import DEFAULTS
from .util import to_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    settings  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_name  TEXT,
    is_bot     INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS members (
    chat_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    joined_at INTEGER,
    last_seen INTEGER,
    points    INTEGER NOT NULL DEFAULT 0,
    style     TEXT,
    notes     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    msg_id   INTEGER,
    text     TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    is_bot   INTEGER NOT NULL DEFAULT 0,
    flagged  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(chat_id, user_id, ts);
CREATE TABLE IF NOT EXISTS requests (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    text    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests ON requests(chat_id, user_id, ts);
CREATE TABLE IF NOT EXISTS warnings (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    by_id   INTEGER NOT NULL,
    reason  TEXT,
    ts      INTEGER NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bot_admins (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS banned_words (
    chat_id INTEGER NOT NULL,
    word    TEXT NOT NULL,
    PRIMARY KEY (chat_id, word)
);
CREATE TABLE IF NOT EXISTS sports_subs (
    chat_id INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    name    TEXT NOT NULL,
    PRIMARY KEY (chat_id, team_id)
);
CREATE TABLE IF NOT EXISTS sports_sent (
    chat_id  INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    PRIMARY KEY (chat_id, event_id, kind)
);
CREATE TABLE IF NOT EXISTS counters (
    day     TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, chat_id, key)
);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE TABLE IF NOT EXISTS captcha (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    message_id INTEGER,
    answer     INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,          -- daily / interval
    at_time      TEXT,                   -- daily: HH:MM
    interval_min INTEGER,                -- interval: 분
    title        TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL DEFAULT '',
    media_type   TEXT,                   -- photo / video / animation / document
    media_id     TEXT,                   -- 텔레그램 file_id
    pin          INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER,
    last_sent    INTEGER,
    last_msg_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_schedules_chat ON schedules(chat_id);
CREATE TABLE IF NOT EXISTS owners (
    user_id  INTEGER PRIMARY KEY,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,          -- 0 = 모든 방 공통 (오너가 1:1 로 등록)
    title    TEXT NOT NULL,
    source   TEXT,                      -- 파일명 또는 '메시지'
    added_by INTEGER,
    chars    INTEGER NOT NULL,
    ts       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id  INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    idx     INTEGER NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_chat ON knowledge_chunks(chat_id);
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id     INTEGER PRIMARY KEY,
    trial_until INTEGER,
    paid_until  INTEGER,
    added_by    INTEGER,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    amount_units INTEGER NOT NULL,      -- USDT 최소단위 (1 USDT = 1,000,000)
    created      INTEGER NOT NULL,
    expires      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending / paid / expired / cancelled
    tx_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status, expires);
CREATE TABLE IF NOT EXISTS payments (
    tx_id        TEXT PRIMARY KEY,      -- 같은 거래를 두 번 쓰지 못하게
    invoice_id   INTEGER,               -- NULL = 청구서와 안 맞는 입금 (확인 필요)
    chat_id      INTEGER,
    amount_units INTEGER NOT NULL,
    from_addr    TEXT,
    block_ts     INTEGER,
    seen_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mod_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    actor_id  INTEGER,
    target_id INTEGER,
    action    TEXT NOT NULL,
    detail    TEXT,
    ts        INTEGER NOT NULL
);
"""


def now() -> int:
    return int(time.time())


# 기능 모듈(패널·태그 알림 등)이 자기 테이블을 따로 선언한다 (db.py 한 곳에 몰리지 않게).
# 반드시 CREATE TABLE/INDEX IF NOT EXISTS 만. 모듈 import 시점에 호출 → DB.open 때 실행.
EXTRA_SCHEMA: list[str] = []


def register_schema(sql: str, *, migrate: dict[str, str] | None = None) -> None:
    """migrate = {테이블: "composite"|"plain"|"drop"} — 그룹이 슈퍼그룹으로 바뀔 때 chat_id 를 어떻게 옮길지.
    composite: chat_id 가 PK 일부 (UPDATE OR IGNORE 후 옛 행 삭제) · plain: 그냥 UPDATE · drop: 옛 행 삭제(캐시)."""
    if sql not in EXTRA_SCHEMA:
        EXTRA_SCHEMA.append(sql)
    EXTRA_MIGRATE.update(migrate or {})


EXTRA_MIGRATE: dict[str, str] = {}


SETTINGS_TTL = 30.0   # 초. 메인·딜러 봇이 DB 를 같이 쓰면 상대가 바꾼 설정을 이 안에 반영


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._settings_cache: dict[int, dict[str, Any]] = {}
        self._settings_at: dict[int, float] = {}   # 읽은 시각 (다른 프로세스가 바꾼 설정도 TTL 뒤 반영)

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        pending = aiosqlite.connect(self.path)
        # 작업 스레드를 daemon 으로: Ctrl+C 로 정리가 끊겨도 프로세스가 멈춰 있지 않게.
        # (SQLite 는 트랜잭션 단위로 원자적이라 중간에 끊겨도 DB 는 깨지지 않음)
        worker = getattr(pending, "_thread", None)
        if worker is not None:
            worker.daemon = True
        self.conn = await pending
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")  # 딜러 봇과 같은 DB 를 쓸 때 잠깐 잠겨도 기다림
        await self.conn.executescript(SCHEMA)
        for extra in EXTRA_SCHEMA:  # 기능 모듈이 register_schema 로 추가한 테이블
            await self.conn.executescript(extra)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        """예전 버전 DB에 없는 컬럼 추가."""
        wanted = {"schedules": {"title": "TEXT NOT NULL DEFAULT ''", "media_type": "TEXT", "media_id": "TEXT"}}
        for table, cols in wanted.items():
            have = {r["name"] for r in await self._all(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    async def close(self) -> None:
        if self.conn:
            conn, self.conn = self.conn, None
            await conn.close()

    def stop_sync(self, timeout: float = 5.0) -> None:
        """이벤트 루프가 이미 끝난 뒤(Ctrl+C 로 정리가 끊긴 경우) 쓰는 동기 종료."""
        if not self.conn:
            return
        conn, self.conn = self.conn, None
        conn.stop()  # 작업 스레드에 'DB 닫고 끝내라' 전달 (커밋된 데이터는 이미 디스크에 있음)
        worker = getattr(conn, "_thread", None)
        if worker is not None:
            worker.join(timeout)

    async def _all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        return list(await self.conn.execute_fetchall(sql, params))

    async def _one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    async def _write(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid

    # ── 방 / 설정 ─────────────────────────────────────────
    async def ensure_chat(self, chat_id: int, title: str | None) -> None:
        await self._write(
            "INSERT INTO chats(chat_id, title) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
            (chat_id, title),
        )

    async def get_settings(self, chat_id: int) -> dict[str, Any]:
        t = time.monotonic()
        if chat_id not in self._settings_cache or t - self._settings_at.get(chat_id, t) > SETTINGS_TTL:
            self._settings_at[chat_id] = t
            row = await self._one("SELECT settings FROM chats WHERE chat_id=?", (chat_id,))
            stored = json.loads(row["settings"]) if row else {}
            self._settings_cache[chat_id] = {**DEFAULTS, **stored}
        return self._settings_cache[chat_id]

    async def set_setting(self, chat_id: int, key: str, value: Any) -> None:
        settings = dict(await self.get_settings(chat_id))
        settings[key] = value
        changed = {k: v for k, v in settings.items() if DEFAULTS.get(k) != v}
        await self._write(
            "INSERT INTO chats(chat_id, settings) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET settings=excluded.settings",
            (chat_id, json.dumps(changed, ensure_ascii=False)),
        )
        self._settings_cache[chat_id] = settings

    # ── 사용자 / 멤버 ─────────────────────────────────────
    async def upsert_user(self, user) -> None:
        await self.conn.execute(
            "INSERT INTO users(user_id, username, first_name, last_name, is_bot, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name, "
            "last_name=excluded.last_name, updated_at=excluded.updated_at",
            (user.id, user.username, user.first_name, user.last_name, int(user.is_bot), now()),
        )

    async def touch_member(self, chat_id: int, user_id: int, joined: bool = False) -> None:
        ts = now()
        await self.conn.execute(
            "INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET last_seen=excluded.last_seen"
            + (", joined_at=excluded.joined_at" if joined else ""),
            (chat_id, user_id, ts if joined else None, ts),
        )
        await self.conn.commit()

    async def get_member(self, chat_id: int, user_id: int) -> aiosqlite.Row | None:
        return await self._one(
            "SELECT m.*, u.username, u.first_name, u.last_name FROM members m "
            "JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND m.user_id=?",
            (chat_id, user_id),
        )

    async def find_members(self, chat_id: int, query: str) -> list[aiosqlite.Row]:
        """@username, 숫자 ID, 이름으로 방 멤버 검색."""
        q = query.strip().lstrip("@")
        if (uid := to_int(q)) is not None:  # '²' 같은 값은 isdigit() 이 True 라 int() 가 터짐
            return await self._all(
                "SELECT u.* FROM users u JOIN members m ON m.user_id=u.user_id "
                "WHERE m.chat_id=? AND u.user_id=?", (chat_id, uid))
        rows = await self._all(
            "SELECT u.* FROM users u JOIN members m ON m.user_id=u.user_id "
            "WHERE m.chat_id=? AND u.username=? COLLATE NOCASE", (chat_id, q))
        if rows:
            return rows
        return await self._all(
            "SELECT u.* FROM users u JOIN members m ON m.user_id=u.user_id WHERE m.chat_id=? "
            "AND (u.first_name=? OR TRIM(u.first_name || ' ' || COALESCE(u.last_name, ''))=?)",
            (chat_id, q, q))

    async def member_names(self, chat_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT u.user_id, u.username, u.first_name, u.last_name FROM users u "
            "JOIN members m ON m.user_id=u.user_id WHERE m.chat_id=? AND u.is_bot=0", (chat_id,))

    async def set_member_style(self, chat_id: int, user_id: int, style: str | None) -> None:
        await self._write("UPDATE members SET style=? WHERE chat_id=? AND user_id=?", (style, chat_id, user_id))

    async def set_member_note(self, chat_id: int, user_id: int, key: str, value: str) -> None:
        row = await self._one("SELECT notes FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        notes = json.loads(row["notes"]) if row else {}
        if value:
            notes[key] = value
        else:
            notes.pop(key, None)
        await self._write("UPDATE members SET notes=? WHERE chat_id=? AND user_id=?",
                          (json.dumps(notes, ensure_ascii=False), chat_id, user_id))

    async def add_points(self, chat_id: int, user_id: int, n: int) -> None:
        await self._write(
            "INSERT INTO members(chat_id, user_id, points, last_seen) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET points=points+excluded.points",
            (chat_id, user_id, n, now()))

    async def flag_message(self, chat_id: int, msg_id: int) -> None:
        await self._write("UPDATE messages SET flagged=1 WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))

    async def has_chat(self, chat_id: int) -> bool:
        return await self._one("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)) is not None

    async def all_chat_ids(self) -> list[int]:
        rows = await self._all("SELECT chat_id FROM chats")
        return [r["chat_id"] for r in rows]

    async def top_points(self, chat_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT m.user_id, m.points, u.username, u.first_name FROM members m "
            "JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND m.points>0 "
            "ORDER BY m.points DESC LIMIT ?", (chat_id, limit))

    # ── 메시지 기록 / 검색 / 집계 ─────────────────────────
    async def log_message(self, chat_id: int, user_id: int, msg_id: int | None, text: str,
                          is_bot: bool = False, flagged: bool = False, ts: int | None = None) -> None:
        """ts = 보낸 시각 (util.sent_at). 없으면 지금."""
        await self._write(
            "INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged) VALUES(?,?,?,?,?,?,?)",
            (chat_id, user_id, msg_id, text[:4000], ts or now(), int(is_bot), int(flagged)))

    async def log_join(self, chat_id: int, user_id: int, name: str, username: str | None) -> None:
        """AI가 '인사해' 때 누가 새로 왔는지 알 수 있게 입장 알림을 대화 기록에 남김 (집계 제외: is_bot).
        캡차·CAS·사칭 검사를 통과한 뒤에만 호출한다."""
        handle = f"@{username}, " if username else ""
        await self.log_message(chat_id, user_id, None, f"{name[:40]}({handle}{user_id}) 님이 방에 들어옴", is_bot=True)

    async def recent_messages(self, chat_id: int, limit: int = 30, since: int = 0) -> list[aiosqlite.Row]:
        rows = await self._all(
            "SELECT msg.*, u.username, u.first_name FROM messages msg "
            "LEFT JOIN users u ON u.user_id=msg.user_id "
            "WHERE msg.chat_id=? AND msg.flagged=0 AND msg.ts>=? ORDER BY msg.id DESC LIMIT ?",
            (chat_id, since, limit))
        return list(reversed(rows))

    async def search_messages(self, chat_id: int, keyword: str, since: int, limit: int = 10) -> list[aiosqlite.Row]:
        like = "%" + keyword.replace("%", "").replace("_", "") + "%"
        return await self._all(
            "SELECT msg.*, u.username, u.first_name FROM messages msg "
            "LEFT JOIN users u ON u.user_id=msg.user_id "
            "WHERE msg.chat_id=? AND msg.flagged=0 AND msg.is_bot=0 AND msg.ts>=? AND msg.text LIKE ? "
            "ORDER BY msg.id DESC LIMIT ?", (chat_id, since, like, limit))

    async def top_chatters(self, chat_id: int, since: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT msg.user_id, COUNT(*) AS n, u.username, u.first_name FROM messages msg "
            "LEFT JOIN users u ON u.user_id=msg.user_id "
            "WHERE msg.chat_id=? AND msg.ts>=? AND msg.is_bot=0 "
            "GROUP BY msg.user_id ORDER BY n DESC LIMIT ?", (chat_id, since, limit))

    async def chat_totals(self, chat_id: int, since: int) -> aiosqlite.Row:
        return await self._one(
            "SELECT COUNT(*) AS messages, COUNT(DISTINCT user_id) AS users FROM messages "
            "WHERE chat_id=? AND ts>=? AND is_bot=0", (chat_id, since))

    async def hourly_counts(self, chat_id: int, since: int, tz_offset_sec: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT ((ts + ?) / 3600) % 24 AS hour, COUNT(*) AS n FROM messages "
            "WHERE chat_id=? AND ts>=? AND is_bot=0 GROUP BY hour ORDER BY hour",
            (tz_offset_sec, chat_id, since))

    async def user_message_count(self, chat_id: int, user_id: int, since: int = 0) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND user_id=? AND ts>=? AND is_bot=0",
            (chat_id, user_id, since))
        return row["n"] if row else 0

    async def prune_messages(self, older_than: int) -> None:
        await self._write("DELETE FROM messages WHERE ts<?", (older_than,))

    # ── 봇 요청 기록 ──────────────────────────────────────
    async def log_request(self, chat_id: int, user_id: int, text: str) -> None:
        await self._write("INSERT INTO requests(chat_id, user_id, text, ts) VALUES(?,?,?,?)",
                          (chat_id, user_id, text[:500], now()))

    async def user_requests(self, chat_id: int, user_id: int, since: int, limit: int = 20) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT text, ts FROM requests WHERE chat_id=? AND user_id=? AND ts>=? ORDER BY id LIMIT ?",
            (chat_id, user_id, since, limit))

    # ── 경고 ──────────────────────────────────────────────
    async def add_warning(self, chat_id: int, user_id: int, by_id: int, reason: str) -> int:
        await self._write("INSERT INTO warnings(chat_id, user_id, by_id, reason, ts) VALUES(?,?,?,?,?)",
                          (chat_id, user_id, by_id, reason[:200], now()))
        return await self.warning_count(chat_id, user_id)

    async def warning_count(self, chat_id: int, user_id: int) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM warnings WHERE chat_id=? AND user_id=? AND active=1",
                              (chat_id, user_id))
        return row["n"]

    async def list_warnings(self, chat_id: int, user_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT reason, ts FROM warnings WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id",
            (chat_id, user_id))

    async def remove_last_warning(self, chat_id: int, user_id: int) -> None:
        await self._write(
            "UPDATE warnings SET active=0 WHERE id=(SELECT id FROM warnings "
            "WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 1)", (chat_id, user_id))

    async def clear_warnings(self, chat_id: int, user_id: int) -> None:
        await self._write("UPDATE warnings SET active=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    # ── 봇 관리자 / 금지어 ────────────────────────────────
    async def bot_admin_ids(self, chat_id: int) -> set[int]:
        rows = await self._all("SELECT user_id FROM bot_admins WHERE chat_id=?", (chat_id,))
        return {r["user_id"] for r in rows}

    async def set_bot_admin(self, chat_id: int, user_id: int, on: bool) -> None:
        if on:
            await self._write("INSERT OR IGNORE INTO bot_admins VALUES(?, ?)", (chat_id, user_id))
        else:
            await self._write("DELETE FROM bot_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def banned_words(self, chat_id: int) -> list[str]:
        rows = await self._all("SELECT word FROM banned_words WHERE chat_id=?", (chat_id,))
        return [r["word"] for r in rows]

    async def set_banned_word(self, chat_id: int, word: str, on: bool) -> None:
        if on:
            await self._write("INSERT OR IGNORE INTO banned_words VALUES(?, ?)", (chat_id, word.lower()))
        else:
            await self._write("DELETE FROM banned_words WHERE chat_id=? AND word=?", (chat_id, word.lower()))

    # ── 스포츠 구독 ───────────────────────────────────────
    async def sports_subs(self, chat_id: int | None = None) -> list[aiosqlite.Row]:
        if chat_id is None:
            return await self._all("SELECT * FROM sports_subs")
        return await self._all("SELECT * FROM sports_subs WHERE chat_id=?", (chat_id,))

    async def set_sports_sub(self, chat_id: int, team_id: str, name: str, on: bool) -> None:
        if on:
            await self._write("INSERT OR REPLACE INTO sports_subs VALUES(?, ?, ?)", (chat_id, team_id, name))
        else:
            await self._write("DELETE FROM sports_subs WHERE chat_id=? AND team_id=?", (chat_id, team_id))

    async def mark_sports_sent(self, chat_id: int, event_id: str, kind: str) -> bool:
        """처음 보내는 알림이면 True."""
        cur = await self.conn.execute("INSERT OR IGNORE INTO sports_sent VALUES(?, ?, ?)", (chat_id, event_id, kind))
        await self.conn.commit()
        return cur.rowcount > 0

    # ── 일일 카운터 (토큰, 웹검색 등) ─────────────────────
    async def bump(self, day: str, chat_id: int, key: str, n: int = 1) -> int:
        await self.conn.execute(
            "INSERT INTO counters(day, chat_id, key, n) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(day, chat_id, key) DO UPDATE SET n=n+excluded.n", (day, chat_id, key, n))
        await self.conn.commit()
        return await self.counter(day, chat_id, key)

    async def counter(self, day: str, chat_id: int, key: str) -> int:
        row = await self._one("SELECT n FROM counters WHERE day=? AND chat_id=? AND key=?", (day, chat_id, key))
        return row["n"] if row else 0

    # ── 방 상태 (잠금·야간모드 등 키-값) ──────────────────
    async def get_state(self, chat_id: int, key: str, default: Any = None) -> Any:
        row = await self._one("SELECT value FROM chat_state WHERE chat_id=? AND key=?", (chat_id, key))
        return json.loads(row["value"]) if row else default

    async def set_state(self, chat_id: int, key: str, value: Any) -> None:
        if value is None:
            await self._write("DELETE FROM chat_state WHERE chat_id=? AND key=?", (chat_id, key))
            return
        await self._write(
            "INSERT INTO chat_state(chat_id, key, value) VALUES(?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value",
            (chat_id, key, json.dumps(value, ensure_ascii=False)))

    # ── 캡차 ──────────────────────────────────────────────
    async def add_captcha(self, chat_id: int, user_id: int, message_id: int | None,
                          answer: int, expires_at: int) -> None:
        await self._write(
            "INSERT OR REPLACE INTO captcha(chat_id, user_id, message_id, answer, attempts, expires_at) "
            "VALUES(?, ?, ?, ?, 0, ?)", (chat_id, user_id, message_id, answer, expires_at))

    async def get_captcha(self, chat_id: int, user_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def captcha_attempt(self, chat_id: int, user_id: int) -> int:
        await self._write("UPDATE captcha SET attempts=attempts+1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = await self.get_captcha(chat_id, user_id)
        return row["attempts"] if row else 0

    async def delete_captcha(self, chat_id: int, user_id: int) -> None:
        await self._write("DELETE FROM captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def expired_captchas(self, now_ts: int) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM captcha WHERE expires_at<=?", (now_ts,))

    # ── 예약 공지 ─────────────────────────────────────────
    SCHEDULE_FIELDS = {"kind", "at_time", "interval_min", "title", "text", "media_type", "media_id", "pin"}

    async def add_schedule(self, chat_id: int, *, kind: str, at_time: str | None, interval_min: int | None,
                           title: str, text: str, media_type: str | None, media_id: str | None,
                           pin: bool, created_by: int) -> int:
        return await self._write(
            "INSERT INTO schedules(chat_id, kind, at_time, interval_min, title, text, media_type, media_id, "
            "pin, created_by, last_sent) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # 반복 공지는 등록 시점부터 간격을 센다 (등록하자마자 올라가지 않게)
            (chat_id, kind, at_time, interval_min, title[:100], text[:3500], media_type, media_id,
             int(pin), created_by, now() if kind == "interval" else None))

    async def update_schedule(self, chat_id: int, sid: int, **fields: Any) -> bool:
        fields = {k: v for k, v in fields.items() if k in self.SCHEDULE_FIELDS}
        if not fields:
            return False
        if fields.get("kind") == "interval":
            fields["last_sent"] = now()  # 주기를 바꾸면 지금부터 다시 센다
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = await self.conn.execute(f"UPDATE schedules SET {sets} WHERE chat_id=? AND id=?",
                                      (*fields.values(), chat_id, sid))
        await self.conn.commit()
        return cur.rowcount > 0

    async def count_schedules(self, chat_id: int) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM schedules WHERE chat_id=?", (chat_id,))
        return row["n"]

    async def schedules(self, chat_id: int | None = None) -> list[aiosqlite.Row]:
        if chat_id is None:
            return await self._all("SELECT * FROM schedules WHERE enabled=1")
        return await self._all("SELECT * FROM schedules WHERE chat_id=? ORDER BY id", (chat_id,))

    async def get_schedule(self, chat_id: int, sid: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM schedules WHERE chat_id=? AND id=?", (chat_id, sid))

    async def delete_schedule(self, chat_id: int, sid: int) -> bool:
        cur = await self.conn.execute("DELETE FROM schedules WHERE chat_id=? AND id=?", (chat_id, sid))
        await self.conn.commit()
        return cur.rowcount > 0

    async def set_schedule_enabled(self, chat_id: int, sid: int, on: bool) -> bool:
        cur = await self.conn.execute("UPDATE schedules SET enabled=? WHERE chat_id=? AND id=?", (int(on), chat_id, sid))
        await self.conn.commit()
        return cur.rowcount > 0

    async def mark_schedule_sent(self, sid: int, ts: int, msg_id: int | None) -> None:
        await self._write("UPDATE schedules SET last_sent=?, last_msg_id=? WHERE id=?", (ts, msg_id, sid))

    # ── 오너 (서버 로그의 등록 코드로 추가) ───────────────
    async def owner_ids(self) -> set[int]:
        return {r["user_id"] for r in await self._all("SELECT user_id FROM owners")}

    async def add_owner(self, user_id: int) -> None:
        await self._write("INSERT OR IGNORE INTO owners(user_id, added_at) VALUES(?, ?)", (user_id, now()))

    # ── 지식 베이스 ───────────────────────────────────────
    async def add_knowledge(self, chat_id: int, title: str, source: str, added_by: int,
                            chunks: list[str]) -> int:
        doc_id = await self._write(
            "INSERT INTO knowledge_docs(chat_id, title, source, added_by, chars, ts) VALUES(?, ?, ?, ?, ?, ?)",
            (chat_id, title[:100], source[:100], added_by, sum(len(c) for c in chunks), now()))
        await self.conn.executemany(
            "INSERT INTO knowledge_chunks(doc_id, chat_id, idx, content) VALUES(?, ?, ?, ?)",
            [(doc_id, chat_id, i, c) for i, c in enumerate(chunks)])
        await self.conn.commit()
        return doc_id

    async def knowledge_docs(self, chat_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM knowledge_docs WHERE chat_id IN (?, 0) ORDER BY chat_id DESC, id", (chat_id,))

    async def knowledge_total_chars(self, chat_id: int) -> int:
        row = await self._one("SELECT COALESCE(SUM(chars), 0) AS n FROM knowledge_docs WHERE chat_id=?", (chat_id,))
        return row["n"]

    async def delete_knowledge(self, chat_id: int, doc_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM knowledge_docs WHERE chat_id=? AND id=?", (chat_id, doc_id))
        if cur.rowcount:
            await self.conn.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (doc_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def knowledge_candidates(self, chat_id: int, terms: list[str], limit: int = 300) -> list[aiosqlite.Row]:
        """검색어 중 하나라도 들어간 조각 (이 방 + 공통). 점수는 knowledge.py 에서 매긴다."""
        if not terms:
            return []
        like = " OR ".join("c.content LIKE ?" for _ in terms)
        return await self._all(
            f"SELECT c.content, c.doc_id, c.idx, d.title FROM knowledge_chunks c "
            f"JOIN knowledge_docs d ON d.id=c.doc_id WHERE c.chat_id IN (?, 0) AND ({like}) LIMIT ?",
            (chat_id, *[f"%{t}%" for t in terms], limit))

    # ── 구독 / 결제 ───────────────────────────────────────
    async def start_subscription(self, chat_id: int, trial_until: int, added_by: int | None) -> None:
        await self._write(
            "INSERT OR IGNORE INTO subscriptions(chat_id, trial_until, added_by, updated_at) VALUES(?, ?, ?, ?)",
            (chat_id, trial_until, added_by, now()))

    async def get_subscription(self, chat_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM subscriptions WHERE chat_id=?", (chat_id,))

    async def set_paid_until(self, chat_id: int, until: int) -> None:
        await self._write(
            "INSERT INTO subscriptions(chat_id, paid_until, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET paid_until=excluded.paid_until, updated_at=excluded.updated_at",
            (chat_id, until, now()))

    async def is_ai_message(self, chat_id: int, msg_id: int) -> bool:
        """봇이 보낸 AI 답(ai_turns.bot_msg_id, memory.record_turn 이 기록)인지. 14일 지난 건 지워져서 False."""
        return await self._one("SELECT 1 FROM ai_turns WHERE chat_id=? AND bot_msg_id=? LIMIT 1",
                               (chat_id, msg_id)) is not None

    async def disable_schedules(self, chat_id: int) -> int:
        """그 방 예약공지를 전부 끈다 (봇이 강퇴·퇴장된 방). 끈 개수."""
        cur = await self.conn.execute("UPDATE schedules SET enabled=0 WHERE chat_id=? AND enabled=1", (chat_id,))
        await self.conn.commit()
        return cur.rowcount

    async def subscriptions_expiring(self, start: int, end: int) -> list[aiosqlite.Row]:
        """만료 시각(유료·체험 중 늦은 쪽)이 [start, end) 인 방."""
        return await self._all(
            "SELECT *, MAX(COALESCE(paid_until, 0), COALESCE(trial_until, 0)) AS until FROM subscriptions "
            "WHERE MAX(COALESCE(paid_until, 0), COALESCE(trial_until, 0)) >= ? "
            "AND MAX(COALESCE(paid_until, 0), COALESCE(trial_until, 0)) < ?", (start, end))

    async def add_invoice(self, chat_id: int, user_id: int, amount_units: int, created: int, expires: int) -> int:
        return await self._write(
            "INSERT INTO invoices(chat_id, user_id, amount_units, created, expires) VALUES(?, ?, ?, ?, ?)",
            (chat_id, user_id, amount_units, created, expires))

    async def open_invoice(self, chat_id: int, now_ts: int, user_id: int | None = None) -> aiosqlite.Row | None:
        if user_id is None:
            return await self._one(
                "SELECT * FROM invoices WHERE chat_id=? AND status='pending' AND expires>? ORDER BY id DESC LIMIT 1",
                (chat_id, now_ts))
        return await self._one(
            "SELECT * FROM invoices WHERE chat_id=? AND user_id=? AND status='pending' AND expires>? "
            "ORDER BY id DESC LIMIT 1", (chat_id, user_id, now_ts))

    _EXTEND_SQL = ("INSERT INTO subscriptions(chat_id, paid_until, updated_at) VALUES(?, ?, ?) "
                   "ON CONFLICT(chat_id) DO UPDATE SET paid_until = MAX(?, COALESCE(paid_until, 0), "
                   "COALESCE(trial_until, 0)) + ?, updated_at = ?")

    async def extend_paid(self, chat_id: int, seconds: int, now_ts: int) -> int:
        """paid_until = max(지금, 유료 만료, 체험 만료) + seconds 를 한 문장으로 (동시 실행에도 안전)."""
        await self._write(self._EXTEND_SQL, (chat_id, now_ts + seconds, now_ts, now_ts, seconds, now_ts))
        return (await self.get_subscription(chat_id))["paid_until"]

    async def migrate_chat(self, old: int, new: int) -> None:
        """일반 그룹 → 슈퍼그룹 전환 시 방 ID 가 바뀜: 구독·설정·자료·기록을 새 ID 로 옮긴다 (옛 데이터가 기준)."""
        single = ["chats", "subscriptions"]                        # chat_id 가 PK
        composite = ["members", "bot_admins", "banned_words", "sports_subs", "sports_sent",
                     "chat_state", "captcha", "counters"]           # chat_id 가 PK 일부
        plain = ["messages", "requests", "warnings", "invoices", "payments", "schedules",
                 "knowledge_docs", "knowledge_chunks", "mod_log"]
        for t in single:
            if await self._one(f"SELECT 1 FROM {t} WHERE chat_id=?", (old,)):
                await self.conn.execute(f"DELETE FROM {t} WHERE chat_id=?", (new,))
            await self.conn.execute(f"UPDATE {t} SET chat_id=? WHERE chat_id=?", (new, old))
        for t in composite:
            await self.conn.execute(f"UPDATE OR IGNORE {t} SET chat_id=? WHERE chat_id=?", (new, old))
            await self.conn.execute(f"DELETE FROM {t} WHERE chat_id=?", (old,))
        for t in plain:
            await self.conn.execute(f"UPDATE {t} SET chat_id=? WHERE chat_id=?", (new, old))
        for t, kind in EXTRA_MIGRATE.items():
            if kind == "composite":
                await self.conn.execute(f"UPDATE OR IGNORE {t} SET chat_id=? WHERE chat_id=?", (new, old))
            if kind in ("composite", "drop"):
                await self.conn.execute(f"DELETE FROM {t} WHERE chat_id=?", (old,))
            elif kind == "plain":
                await self.conn.execute(f"UPDATE {t} SET chat_id=? WHERE chat_id=?", (new, old))
        await self.conn.commit()
        self._settings_cache.pop(old, None)
        self._settings_cache.pop(new, None)

    async def get_invoice(self, invoice_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM invoices WHERE id=?", (invoice_id,))

    async def pending_invoices(self, expires_after: int) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM invoices WHERE status='pending' AND expires>?", (expires_after,))

    async def pending_amounts(self, expires_after: int) -> set[int]:
        """새 청구서가 피해야 할 금액. 취소한 청구서도 유효시간(+여유) 동안은 그 금액으로 입금이 올 수 있어서 포함."""
        return {r["amount_units"] for r in await self._all(
            "SELECT amount_units FROM invoices WHERE status IN ('pending', 'cancelled') AND expires>?", (expires_after,))}

    async def pay_invoice(self, invoice_id: int, tx_id: str, chat_id: int, seconds: int, now_ts: int) -> int | None:
        """청구서 결제 처리 + 구독 연장을 한 번에. 연결을 같이 쓰는 다른 코루틴의 commit 이 두 문장 사이에 끼면
        '결제됨인데 연장 안 됨'이 남을 수 있어서, DB 스레드에서 두 문장을 연달아 실행한다.
        이미 처리·취소된 청구서면 None, 성공하면 새 만료 시각."""
        def run(c) -> bool:
            c.execute("SAVEPOINT pay")
            try:
                ok = c.execute("UPDATE invoices SET status='paid', tx_id=? WHERE id=? AND status='pending'",
                               (tx_id, invoice_id)).rowcount > 0
                if ok:
                    c.execute(self._EXTEND_SQL, (chat_id, now_ts + seconds, now_ts, now_ts, seconds, now_ts))
            except BaseException:
                c.execute("ROLLBACK TO pay")   # 이 두 문장만 되돌림 (다른 코루틴의 쓰기는 건드리지 않음)
                raise
            finally:
                c.execute("RELEASE pay")
            c.commit()
            return ok
        if not await self.conn._execute(run, self.conn._conn):
            return None
        return (await self.get_subscription(chat_id))["paid_until"]

    async def payment_invoice_id(self, tx_id: str) -> int | None:
        row = await self._one("SELECT invoice_id FROM payments WHERE tx_id=?", (tx_id,))
        return row["invoice_id"] if row else None

    async def mark_invoice_paid(self, invoice_id: int, tx_id: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE invoices SET status='paid', tx_id=? WHERE id=? AND status='pending'", (tx_id, invoice_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def cancel_invoice(self, invoice_id: int) -> None:
        await self._write("UPDATE invoices SET status='cancelled' WHERE id=? AND status='pending'", (invoice_id,))

    async def expire_invoices(self, before: int) -> None:
        await self._write("UPDATE invoices SET status='expired' WHERE status='pending' AND expires<=?", (before,))

    async def record_payment(self, tx_id: str, invoice_id: int | None, chat_id: int | None,
                             amount_units: int, from_addr: str, block_ts: int) -> bool:
        """처음 보는 거래면 기록하고 True. 이미 기록된 거래면 False (이중 사용 방지)."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO payments(tx_id, invoice_id, chat_id, amount_units, from_addr, block_ts, seen_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)", (tx_id, invoice_id, chat_id, amount_units, from_addr, block_ts, now()))
        await self.conn.commit()
        return cur.rowcount > 0

    async def payment_history(self, chat_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM payments WHERE chat_id=? ORDER BY block_ts DESC LIMIT ?", (chat_id, limit))

    # ── 백업 ──────────────────────────────────────────────
    async def backup_to(self, path: str) -> None:
        """온라인 백업 (봇이 돌아가는 중에도 일관된 복사본)."""
        async with aiosqlite.connect(path) as target:
            await self.conn.backup(target)

    # ── 관리 로그 ─────────────────────────────────────────
    async def log_mod(self, chat_id: int, actor_id: int | None, target_id: int | None,
                      action: str, detail: str = "") -> None:
        await self._write(
            "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
            (chat_id, actor_id, target_id, action, detail[:300], now()))

    async def recent_mod_log(self, chat_id: int, limit: int = 15) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT l.*, u.first_name AS target_name FROM mod_log l "
            "LEFT JOIN users u ON u.user_id=l.target_id WHERE l.chat_id=? ORDER BY l.id DESC LIMIT ?",
            (chat_id, limit))

    # ── 활동 리포트·AI 하루 요약 (sodam/reports.py) ─────────
    async def first_name(self, user_id: int) -> str | None:
        row = await self._one("SELECT first_name FROM users WHERE user_id=?", (user_id,))
        return row["first_name"] if row else None

    async def mod_actions(self, chat_id: int, since: int, until: int) -> list[aiosqlite.Row]:
        """[since, until) 관리 기록의 (action, detail, actor_id) — 분류는 reports.py 에서."""
        return await self._all("SELECT action, detail, actor_id FROM mod_log WHERE chat_id=? AND ts>=? AND ts<?",
                               (chat_id, since, until))

    async def counter_sum(self, chat_id: int, key: str, day_from: str, day_to: str) -> int:
        """일일 카운터 합계 (day 는 'YYYY-MM-DD', 양쪽 포함)."""
        row = await self._one("SELECT COALESCE(SUM(n), 0) AS n FROM counters WHERE chat_id=? AND key=? "
                              "AND day>=? AND day<=?", (chat_id, key, day_from, day_to))
        return row["n"]

    async def joined_count(self, chat_id: int, since: int, until: int) -> int:
        """[since, until) 에 들어온 사람 수 (봇 제외, 관리자 포함 — 입장 때 members.joined_at 기록)."""
        row = await self._one("SELECT COUNT(*) AS n FROM members m JOIN users u ON u.user_id=m.user_id "
                              "WHERE m.chat_id=? AND m.joined_at>=? AND m.joined_at<? AND u.is_bot=0",
                              (chat_id, since, until))
        return row["n"]

    async def ai_turn_count(self, chat_id: int, since: int, until: int, vias: tuple[str, ...]) -> int:
        """AI 답 기록(ai_turns, memory.py 가 만든 테이블 · 14일 보관) 개수."""
        marks = ",".join("?" for _ in vias)
        row = await self._one(f"SELECT COUNT(*) AS n FROM ai_turns WHERE chat_id=? AND ts>=? AND ts<? "
                              f"AND via IN ({marks})", (chat_id, since, until, *vias))
        return row["n"]

    async def message_totals(self, chat_id: int, since: int, until: int) -> tuple[int, int]:
        """[since, until) 사람 메시지 수, 말한 사람 수."""
        row = await self._one("SELECT COUNT(*) AS m, COUNT(DISTINCT user_id) AS u FROM messages "
                              "WHERE chat_id=? AND ts>=? AND ts<? AND is_bot=0", (chat_id, since, until))
        return row["m"], row["u"]

    async def human_messages(self, chat_id: int, since: int, limit: int) -> list[aiosqlite.Row]:
        """사람이 쓴 최근 메시지 (봇 답·입장 기록·인젝션 판정(flagged) 제외), 오래된 것부터."""
        rows = await self._all(
            "SELECT msg.user_id, msg.text, msg.ts, u.first_name, u.username FROM messages msg "
            "LEFT JOIN users u ON u.user_id=msg.user_id "
            "WHERE msg.chat_id=? AND msg.ts>=? AND msg.is_bot=0 AND msg.flagged=0 ORDER BY msg.id DESC LIMIT ?",
            (chat_id, since, limit))
        return list(reversed(rows))
