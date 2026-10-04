"""🗂️ 미디어 보관 — 봇이 바뀌어도 예약공지·인사 사진/영상이 안 깨지게.

실제 사례 2026-10-04: @sodam_ai_bot 삭제 → 새 봇 토큰. DB 의 예약·인사 설정은 그대로였는데 텔레그램 file_id 는
**봇마다 달라서** 미디어 든 예약공지(백악관 #6 영상·#10 GIF)·입장 인사가 'Wrong file identifier' 로 전부 실패.
- 미디어를 저장할 때 원본도 `data/media/<sha256>` 에 받아 둠(remember, 봇 getFile 한도 20MB).
- 보낼 때 파일 id 오류면 보관 원본을 올리고, 새 봇의 file_id 를 media_alias(봇 ID, 원래 id → 새 id)에 기억 →
  다음부터는 새 id 로 바로. 예약·설정에 저장된 원래 file_id 는 안 바꿈 (여러 표를 고칠 필요 없고, 또 바뀌어도 원본으로 다시).
- 원본이 없으면 MediaLost (BadRequest 하위라 기존 '미디어 실패면 글만' 처리를 그대로 탐).
- backfill: 아직 보관 안 된 미디어(지금 봇 것)를 하루 몇 번 받아 둠, 안 쓰는 보관 파일은 30일 뒤 정리.
테스트 tests/test_mediastore.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from telegram import InputFile
from telegram.error import BadRequest, TelegramError

from . import db as dbmod
from . import persist

log = logging.getLogger(__name__)

dbmod.register_schema("""
CREATE TABLE IF NOT EXISTS media_store (
    file_id TEXT PRIMARY KEY,
    kind    TEXT NOT NULL,
    path    TEXT NOT NULL,           -- data/media 안 파일 이름 (sha256)
    size    INTEGER NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS media_alias (
    bot_id  INTEGER NOT NULL,
    file_id TEXT NOT NULL,           -- 저장된 원래 file_id (다른 봇 것일 수 있음)
    new_id  TEXT NOT NULL,           -- 이 봇이 쓰는 file_id
    ts      INTEGER NOT NULL,
    PRIMARY KEY (bot_id, file_id)
);
""")

KINDS = ("photo", "video", "animation", "document")
MAX_BYTES = 20 * 1024 * 1024        # 봇 getFile 한도
TOTAL_MAX = 500 * 1024 * 1024       # 보관 전체 상한
KEEP_UNUSED = 30 * 86400            # 아무 데서도 안 쓰는 보관 파일
BACKFILL_BATCH = 20
BACKFILL_EVERY = 6 * 3600
_NAMES = {"photo": "photo.jpg", "video": "video.mp4", "animation": "animation.mp4", "document": "file"}
_BAD = ("wrong file identifier", "wrong remote file", "file reference", "file_id_invalid", "invalid file id",
        "wrong padding in the string")
MEDIA_DIR: Path | None = None       # 테스트가 바꿀 수 있음
_failed: set[str] = set()           # 받기 실패한 file_id (다른 봇 것 등) — 프로세스 동안 다시 안 시도
_last_backfill = 0.0


class MediaLost(BadRequest):
    """파일 id 가 이 봇에서 안 먹고 보관 원본도 없음."""


def is_bad_file(e: Exception) -> bool:
    return isinstance(e, BadRequest) and any(m in str(e).lower() for m in _BAD)


def media_dir(db) -> Path:
    global MEDIA_DIR
    if MEDIA_DIR is None:
        path = getattr(db, "path", "") or ""
        base = Path(tempfile.gettempdir()) / f"sodam_media_{os.getpid()}" if path in ("", ":memory:") \
            else Path(path).resolve().parent / "media"
        MEDIA_DIR = base
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR


def _bot_id(bot) -> int:
    return persist._bot_id(bot)


async def stored(db, file_id: str) -> bool:
    return bool(file_id) and await db._one("SELECT 1 FROM media_store WHERE file_id=?", (file_id,)) is not None


async def load(db, file_id: str) -> bytes | None:
    row = await db._one("SELECT path FROM media_store WHERE file_id=?", (file_id,))
    if not row:
        return None
    try:
        return (media_dir(db) / row["path"]).read_bytes()
    except OSError:
        return None


async def _put(db, file_id: str, kind: str, data: bytes) -> None:
    name = hashlib.sha256(data).hexdigest()
    p = media_dir(db) / name
    if not p.exists():
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    await db._write("INSERT OR REPLACE INTO media_store(file_id, kind, path, size, ts) VALUES(?,?,?,?,?)",
                    (file_id, kind, name, len(data), int(time.time())))


async def remember(bot, db, kind: str | None, file_id: str | None) -> bool:
    """이 봇의 file_id 원본을 받아 보관. 이미 있거나 받으면 True. 실패는 로그만 (보내기·저장은 그대로)."""
    if not file_id or kind not in KINDS or db is None:
        return False
    if await stored(db, file_id):
        return True
    if file_id in _failed:
        return False
    try:
        total = (await db._one("SELECT COALESCE(SUM(size),0) AS n FROM media_store"))["n"]
        f = await bot.get_file(file_id)
        size = getattr(f, "file_size", None)
        if isinstance(size, int) and size > MAX_BYTES:
            raise ValueError("too big")
        data = bytes(await f.download_as_bytearray())
        if len(data) > MAX_BYTES or not data:
            raise ValueError("too big or empty")
        if total + len(data) > TOTAL_MAX:
            log.warning("media store full (%d bytes), not keeping %s", total, kind)
            return False
        await _put(db, file_id, kind, data)
        return True
    except Exception as e:   # 다른 봇 file_id·20MB 넘음·네트워크 — 보관만 못 함
        _failed.add(file_id)
        log.info("media keep skipped (%s): %s", kind, e)
        return False


def remember_soon(bot, db, kind: str | None, file_id: str | None) -> None:
    """저장 화면이 느려지지 않게 뒤에서."""
    if file_id and kind in KINDS and db is not None:
        persist.spawn(remember(bot, db, kind, file_id))


def new_file_id(msg, kind: str) -> str | None:
    try:
        if kind == "photo":
            return msg.photo[-1].file_id
        return getattr(msg, kind).file_id
    except (AttributeError, IndexError, TypeError):
        return None


async def _send(bot, kind: str, chat_id: int, media, **kw):
    fn = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation,
          "document": bot.send_document}[kind]
    return await fn(chat_id, media, **kw)


async def send(bot, db, kind: str, chat_id: int, file_id: str, **kw):
    """미디어 보내기. 이 봇에서 file_id 가 안 먹으면 보관 원본으로 다시 올리고 새 id 를 기억."""
    if db is None:
        return await _send(bot, kind, chat_id, file_id, **kw)
    bid = _bot_id(bot)
    row = await db._one("SELECT new_id FROM media_alias WHERE bot_id=? AND file_id=?", (bid, file_id))
    use = row["new_id"] if row else file_id
    try:
        return await _send(bot, kind, chat_id, use, **kw)
    except BadRequest as e:
        if not is_bad_file(e):
            raise
        data = await load(db, file_id)
        if data is None:
            raise MediaLost(str(e)) from e
        log.warning("media id refused by this bot (%s) — re-uploading kept original %s", e, kind)
    sent = await _send(bot, kind, chat_id, InputFile(data, filename=_NAMES[kind]), **kw)
    new = new_file_id(sent, kind)
    if new and new != file_id:
        await db._write("INSERT OR REPLACE INTO media_alias(bot_id, file_id, new_id, ts) VALUES(?,?,?,?)",
                        (bid, file_id, new, int(time.time())))
    return sent


async def notify_lost(bot, db, key: str, user_id: int | None, text: str) -> bool:
    """원본이 없어 미디어를 못 붙였을 때 관리자 1:1 에 하루 한 번 (key 마다)."""
    if not user_id or db is None:
        return False
    if not await persist.claim(db, f"medialost:{key}:{time.strftime('%Y%m%d')}", 86400):
        return False
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
        return True
    except TelegramError as e:
        log.info("media lost notice not delivered to %s: %s", user_id, e)
        return False


# ── 아직 보관 안 된 것 받아 두기 · 정리 ──────────────────────
async def references(db) -> list[tuple[str, str]]:
    """지금 쓰이는 (kind, file_id) 전부: 예약·인사(미디어·글 복사 스냅숏)·채널 글 초안."""
    out: list[tuple[str, str]] = []
    for r in await db._all("SELECT media_type, media_id FROM schedules WHERE media_id IS NOT NULL AND media_id<>''"):
        out.append((r["media_type"], r["media_id"]))
    for r in await db._all("SELECT settings FROM chats WHERE settings LIKE '%greet_%'"):
        try:
            s = json.loads(r["settings"] or "{}")
        except ValueError:
            continue
        if s.get("greet_media_id"):
            out.append((s.get("greet_media_type"), s["greet_media_id"]))
        snap = s.get("greet_copy_snap")
        if isinstance(snap, dict) and snap.get("file_id"):
            out.append((snap.get("kind"), snap["file_id"]))
    try:
        for r in await db._all("SELECT media_type, media_id FROM composer_drafts WHERE media_id IS NOT NULL AND media_id<>''"):
            out.append((r["media_type"], r["media_id"]))
    except Exception:   # 채널 기능 표가 없는 옛 DB
        pass
    return [(k, f) for k, f in out if k in KINDS and isinstance(f, str) and f]


async def backfill(bot, db) -> int:
    """보관 안 된 미디어를 받아 둠 (한 번에 BACKFILL_BATCH 개) + 안 쓰는 보관 정리. 받은 개수."""
    refs = await references(db)
    got = 0
    tried = 0
    for kind, fid in dict.fromkeys(refs):
        if tried >= BACKFILL_BATCH:
            break
        if fid in _failed or await stored(db, fid):
            continue
        tried += 1
        got += await remember(bot, db, kind, fid)
    await _gc(db, {f for _, f in refs})
    return got


async def _gc(db, used: set[str]) -> None:
    cut = int(time.time()) - KEEP_UNUSED
    old = [r for r in await db._all("SELECT file_id, path FROM media_store WHERE ts<?", (cut,)) if r["file_id"] not in used]
    for r in old:
        await db._write("DELETE FROM media_store WHERE file_id=?", (r["file_id"],))
        await db._write("DELETE FROM media_alias WHERE file_id=?", (r["file_id"],))
        if not await db._one("SELECT 1 FROM media_store WHERE path=?", (r["path"],)):
            try:
                (media_dir(db) / r["path"]).unlink()
            except OSError:
                pass


async def tick(bot, db) -> None:
    """handlers 30초 틱에서: 시작 뒤 첫 틱, 그 뒤 BACKFILL_EVERY 마다."""
    global _last_backfill
    now = time.monotonic()
    if _last_backfill and now - _last_backfill < BACKFILL_EVERY:
        return
    _last_backfill = now
    try:
        n = await backfill(bot, db)
        if n:
            log.info("media kept: %d", n)
    except Exception:
        log.exception("media backfill failed")
