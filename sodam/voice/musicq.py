"""🎵 뮤직봇 대기열·세션 (DB) — 봇(panels/music.py)과 음성 담당(voice/music.py)이 같이 씀. 무거운 import 없음.

music_queue    한 줄 = 신청한 곡 하나. state: queued(기다림) → playing(지금) → done/skipped/removed/failed.
               '대기열 #n' = 이 방 queued 를 신청 순(id)으로 센 n번째. 재시작해도 남음 (음성 담당이 이어서 틂).
music_sessions 노래 틀기 한 번(도우미가 음성채팅에 들어가 있던 동안) = 한 줄. 끝난 이유·곡 수.
chat_state(0, music_health) = 음성 담당이 마지막으로 본 유튜브 상태 {ok_ts, err, err_ts} (오너 🎵 화면).
"""
from __future__ import annotations

import time

from .. import db as dbm

dbm.register_schema("""
CREATE TABLE IF NOT EXISTS music_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
    vid TEXT, duration INTEGER NOT NULL DEFAULT 0, by_id INTEGER, by_name TEXT, state TEXT NOT NULL DEFAULT 'queued',
    pos_ms INTEGER NOT NULL DEFAULT 0, path TEXT, msg_id INTEGER, ts INTEGER NOT NULL, started_ts INTEGER, end_ts INTEGER);
CREATE INDEX IF NOT EXISTS music_queue_chat ON music_queue(chat_id, state, id);
CREATE TABLE IF NOT EXISTS music_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, by_id INTEGER, start_ts INTEGER NOT NULL,
    end_ts INTEGER, reason TEXT, tracks INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS music_sessions_chat ON music_sessions(chat_id, start_ts);
""", migrate={"music_queue": "plain", "music_sessions": "plain"})

HEALTH_KEY = "music_health"
QUEUE_MAX = 30               # 방마다 기다리는 곡
PER_USER = 5                 # 한 사람이 동시에 걸어 둘 수 있는 곡
KEEP_DAYS = 7                # 끝난 곡 기록 보관


def _now() -> int:
    return int(time.time())


async def add(db, chat_id: int, *, title: str, url: str, vid: str | None, duration: int, by_id: int | None,
              by_name: str, path: str | None = None, msg_id: int | None = None,
              queue_max: int = QUEUE_MAX, per_user: int = PER_USER) -> tuple[int | None, int, str]:
    """대기열에 넣음 → (줄 id, 대기열 번호(1부터, 지금 아무것도 안 틀면 0), 이유). 꽉 찼으면 (None, 0, 'full'|'per_user')."""
    now = _now()

    def run(c):
        waiting = c.execute("SELECT COUNT(*) FROM music_queue WHERE chat_id=? AND state='queued'", (chat_id,)).fetchone()[0]
        if waiting >= queue_max:
            return None, 0, "full"
        if by_id is not None and per_user and c.execute(
                "SELECT COUNT(*) FROM music_queue WHERE chat_id=? AND by_id=? AND state='queued'",
                (chat_id, by_id)).fetchone()[0] >= per_user:
            return None, 0, "per_user"
        playing = c.execute("SELECT 1 FROM music_queue WHERE chat_id=? AND state='playing'", (chat_id,)).fetchone()
        rid = c.execute("INSERT INTO music_queue(chat_id, title, url, vid, duration, by_id, by_name, path, msg_id, ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (chat_id, title[:200], url[:500], vid, int(duration or 0), by_id, (by_name or "")[:60], path,
                         msg_id, now)).lastrowid
        return rid, (waiting + 1 if playing else waiting), "ok"   # 0 = 바로 틀 차례 (앞에 아무것도 없음)
    return await db.atomic(run)


async def start_next(db, chat_id: int):
    """다음 곡을 playing 으로 (한 번만). 이미 playing 인 게 있으면 그것 (재시작 뒤 이어 틀기)."""
    def run(c):
        cur = c.execute("SELECT * FROM music_queue WHERE chat_id=? AND state='playing' ORDER BY id LIMIT 1",
                        (chat_id,)).fetchone()
        if cur:
            return cur
        nxt = c.execute("SELECT * FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY id LIMIT 1",
                        (chat_id,)).fetchone()
        if not nxt:
            return None
        c.execute("UPDATE music_queue SET state='playing', started_ts=? WHERE id=? AND state='queued'", (_now(), nxt["id"]))
        return c.execute("SELECT * FROM music_queue WHERE id=?", (nxt["id"],)).fetchone()
    return await db.atomic(run)


async def finish(db, row_id: int, state: str = "done") -> None:
    await db._write("UPDATE music_queue SET state=?, end_ts=? WHERE id=? AND state IN ('playing','queued')",
                    (state, _now(), row_id))


async def requeue_front(db, row_id: int) -> None:
    """반복(loop): 같은 곡을 처음부터 다시 — playing 그대로 두고 위치만 0."""
    await db._write("UPDATE music_queue SET pos_ms=0 WHERE id=?", (row_id,))


async def save_pos(db, row_id: int, pos_ms: int) -> None:
    await db._write("UPDATE music_queue SET pos_ms=? WHERE id=?", (int(pos_ms), row_id))


async def set_path(db, row_id: int, path: str | None) -> None:
    await db._write("UPDATE music_queue SET path=? WHERE id=?", (path, row_id))


async def set_track(db, row_id: int, title: str, url: str, vid: str, duration: int) -> None:
    """유튜브가 막혀 다른 곳(SoundCloud)의 같은 노래로 바꿈."""
    await db._write("UPDATE music_queue SET title=?, url=?, vid=?, duration=? WHERE id=?",
                    (title[:200], url[:500], vid, int(duration or 0), row_id))


async def set_msg(db, row_id: int, msg_id: int | None) -> None:
    await db._write("UPDATE music_queue SET msg_id=? WHERE id=?", (msg_id, row_id))


async def current(db, chat_id: int):
    return await db._one("SELECT * FROM music_queue WHERE chat_id=? AND state='playing' ORDER BY id LIMIT 1", (chat_id,))


async def waiting(db, chat_id: int, limit: int = QUEUE_MAX) -> list:
    return await db._all("SELECT * FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY id LIMIT ?",
                         (chat_id, limit))


async def remove_nth(db, chat_id: int, n: int, uid: int, admin: bool):
    """대기열 n번째(1부터)를 뺌. 관리자는 아무 곡, 아니면 자기가 신청한 곡만 → (줄, 이유 ok|none|not_yours)."""
    def run(c):
        rows = c.execute("SELECT * FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY id LIMIT 1 OFFSET ?",
                         (chat_id, max(0, n - 1))).fetchall() if n >= 1 else []
        if not rows:
            return None, "none"
        r = rows[0]
        if not admin and r["by_id"] != uid:
            return r, "not_yours"
        c.execute("UPDATE music_queue SET state='removed', end_ts=? WHERE id=? AND state='queued'", (_now(), r["id"]))
        return r, "ok"
    return await db.atomic(run)


async def clear(db, chat_id: int, reason: str = "removed") -> int:
    """대기열·지금 곡 전부 끝 (끝내기). 지운 곡 수."""
    cur = await db.conn.execute("UPDATE music_queue SET state=?, end_ts=? WHERE chat_id=? AND state IN ('queued','playing')",
                                (reason, _now(), chat_id))
    await db.conn.commit()
    return cur.rowcount or 0


async def chats_with_queue(db, since: int) -> list[int]:
    """재시작 뒤 이어 틀 방: 최근(since 이후)에 쓴 곡이 남아 있는 방."""
    rows = await db._all("SELECT DISTINCT chat_id FROM music_queue WHERE state IN ('queued','playing') "
                         "AND COALESCE(started_ts, ts)>=?", (since,))
    return [r["chat_id"] for r in rows]


async def drop_stale(db, before: int) -> int:
    """오래 묵은 대기열(재시작 뒤 이어 틀지 않은 것) 정리 + 끝난 곡 기록 KEEP_DAYS."""
    now = _now()

    def run(c):
        n = c.execute("UPDATE music_queue SET state='removed', end_ts=? WHERE state IN ('queued','playing') "
                      "AND COALESCE(started_ts, ts)<?", (now, before)).rowcount
        c.execute("DELETE FROM music_queue WHERE state NOT IN ('queued','playing') AND COALESCE(end_ts, ts)<?",
                  (now - KEEP_DAYS * 86400,))
        return n
    return await db.atomic(run)


# ── 세션 ──────────────────────────────────────────────
async def session_start(db, chat_id: int, by: int | None) -> int:
    return await db._write("INSERT INTO music_sessions(chat_id, by_id, start_ts) VALUES(?,?,?)", (chat_id, by, _now()))


async def session_end(db, sid: int, reason: str, tracks: int) -> None:
    await db._write("UPDATE music_sessions SET end_ts=?, reason=?, tracks=? WHERE id=? AND end_ts IS NULL",
                    (_now(), reason[:40], tracks, sid))


async def active_session(db, chat_id: int):
    return await db._one("SELECT * FROM music_sessions WHERE chat_id=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1",
                         (chat_id,))


async def close_orphans(db) -> list[int]:
    """음성 담당이 다시 켜짐 → 끝 표시 없는 세션은 restart 로 닫고 그 방들 (이어 틀기·남은 통화 정리용)."""
    rows = await db._all("SELECT DISTINCT chat_id FROM music_sessions WHERE end_ts IS NULL")
    await db._write("UPDATE music_sessions SET end_ts=?, reason='restart' WHERE end_ts IS NULL", (_now(),))
    return [r["chat_id"] for r in rows]


async def ended_unnotified(db) -> list:
    return await db._all("SELECT * FROM music_sessions WHERE end_ts IS NOT NULL AND notified=0 ORDER BY id LIMIT 20")


async def mark_notified(db, sid: int) -> bool:
    cur = await db.conn.execute("UPDATE music_sessions SET notified=1 WHERE id=? AND notified=0", (sid,))
    await db.conn.commit()
    return bool(cur.rowcount)


def fmt_dur(sec: int | float | None) -> str:
    s = int(sec or 0)
    if s <= 0:
        return "?"
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── 방에 보일 글 (봇·음성 담당 같이) ─────────────────────────
def _esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card_text(kind: str, row: dict, *, pos: int = 0, why: str = "") -> str:
    title, dur = _esc(row.get("title"))[:120], fmt_dur(row.get("duration"))
    by = _esc(row.get("by_name") or "")[:40]
    src = " · SoundCloud" if "soundcloud" in str(row.get("url") or "") else ""
    tail = f"⏱ {dur}{src}" + (f" · 신청: {by}" if by else "")
    if kind == "now":
        return f"🎶 <b>재생 시작</b>\n<b>{title}</b>\n{tail}"
    if kind == "queued":
        return f"➕ <b>대기열 추가</b> (#{pos})\n{title}\n{tail}"
    return f"⚠️ 못 틀었어요: {title}\n{_esc(why)}"


def card_kb():
    from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[B("⏸ 일시정지", callback_data="mu:pause"), B("▶️ 다시", callback_data="mu:resume"),
                                  B("⏭ 다음", callback_data="mu:skip")],
                                 [B("📃 대기열", callback_data="mu:queue"), B("⏹ 끝내기", callback_data="mu:end")]])
