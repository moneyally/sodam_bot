"""🗂️ 방에 올라온 스티커·사진·영상 기록 (누가·언제·무엇·텔레그램 파일 번호) — 사흘만.

왜: 소담은 글만 messages 에 남겨서 '내가 올린 스티커처럼 만들어줘' 의 그 스티커를 몰랐음 (2026-10-06 21:33 루피 '안녕하세요' 스티커 →
'답장해서 다시 말해줘'). vision.remember 는 메모리 3분짜리라 재시작·조금 늦은 요청엔 없음.
파일은 안 받고 file_id 만 (같은 봇이면 나중에 getFile 로 받음). messages 표·채팅 순위 같은 통계는 안 건드림 (따로 표).
"""
from __future__ import annotations

import sqlite3
import time

from .db import register_schema

KEEP_SEC = 3 * 86400
_PRUNE_EVERY = 3600
_last_prune = 0.0

register_schema("""
CREATE TABLE IF NOT EXISTS media_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    kind      TEXT NOT NULL,              -- sticker · photo · video · gif · video_note · image
    file_id   TEXT NOT NULL,
    unique_id TEXT NOT NULL DEFAULT '',
    fmt       TEXT NOT NULL DEFAULT '',   -- 스티커: static · video · animated (tgs)
    emoji     TEXT NOT NULL DEFAULT '',
    set_name  TEXT NOT NULL DEFAULT '',
    caption   TEXT NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS media_log_who ON media_log(chat_id, user_id, ts);
CREATE INDEX IF NOT EXISTS media_log_ts ON media_log(ts);
""")


def describe(msg) -> dict | None:
    """메시지의 미디어 한 개 (없으면 None). 사진은 가장 큰 것."""
    st = getattr(msg, "sticker", None)
    if st:
        fmt = "video" if getattr(st, "is_video", False) else "animated" if getattr(st, "is_animated", False) else "static"
        return {"kind": "sticker", "file_id": st.file_id, "unique_id": st.file_unique_id or "", "fmt": fmt,
                "emoji": st.emoji or "", "set_name": st.set_name or ""}
    photos = getattr(msg, "photo", None)
    if photos:
        p = photos[-1]
        return {"kind": "photo", "file_id": p.file_id, "unique_id": p.file_unique_id or ""}
    for attr, kind in (("animation", "gif"), ("video", "video"), ("video_note", "video_note")):
        m = getattr(msg, attr, None)
        if m:
            return {"kind": kind, "file_id": m.file_id, "unique_id": m.file_unique_id or ""}
    doc = getattr(msg, "document", None)
    mime = (getattr(doc, "mime_type", None) or "") if doc else ""
    if doc and mime.startswith(("image/", "video/")):
        return {"kind": "image" if mime.startswith("image/") else "video", "file_id": doc.file_id,
                "unique_id": doc.file_unique_id or ""}
    return None


async def record(db, msg, now: float | None = None) -> bool:
    """사람이 올린 미디어면 한 줄 (사흘 지난 건 한 시간에 한 번 같이 지움). 기록하면 True."""
    global _last_prune
    user = getattr(msg, "from_user", None)
    if user is None or getattr(user, "is_bot", False):
        return False
    md = describe(msg)
    if md is None:
        return False
    now = time.time() if now is None else now
    prune = now - _last_prune > _PRUNE_EVERY
    row = (msg.chat_id, user.id, msg.message_id, md["kind"], md["file_id"], md.get("unique_id", ""), md.get("fmt", ""),
           md.get("emoji", ""), md.get("set_name", ""), (getattr(msg, "caption", None) or "")[:200], int(now))

    def tx(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO media_log (chat_id, user_id, msg_id, kind, file_id, unique_id, fmt, emoji, set_name,"
                     " caption, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        if prune:
            conn.execute("DELETE FROM media_log WHERE ts < ?", (int(now - KEEP_SEC),))

    await db.atomic(tx)
    if prune:
        _last_prune = now
    return True


async def recent(db, chat_id: int, user_id: int | None = None, kind: str | None = None, within: int = 600,
                 now: float | None = None) -> list:
    """최근 미디어 (최신 먼저, 최대 5개). user_id·kind 를 주면 그 사람·종류만."""
    now = time.time() if now is None else now
    sql, args = "SELECT * FROM media_log WHERE chat_id=? AND ts>=?", [chat_id, int(now - within)]
    if user_id is not None:
        sql, args = sql + " AND user_id=?", [*args, user_id]
    if kind:
        sql, args = sql + " AND kind=?", [*args, kind]
    return await db._all(sql + " ORDER BY ts DESC, id DESC LIMIT 5", tuple(args))
