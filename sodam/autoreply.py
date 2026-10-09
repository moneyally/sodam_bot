"""💬 자동 답글 — 낱말을 치면 저장해 둔 글이 나옴 (2026-10-09 NASA 방 '.reply 공지' 요청).

등록: 관리자가 아무 글(사진·영상·GIF·스티커·음성·파일, 굵게·움직이는 이모지 포함)에 답장으로 `.reply 공지`
      (또는 `.reply 공지 바로 쓸 글`, 1:1 메뉴 방 → 💬 자동 답글, AI 도구 auto_reply).
울림: 누가 그 낱말**만** 치면(앞뒤 공백·대소문자·끝 ?!. 무시) 그 사람 글에 답장으로 저장한 글을 그대로.
보내기: copy_message(원본 글) 먼저 — 서식·움직이는 이모지·미디어가 원본 그대로. 원본이 지워졌거나 복사가 안 되면
       저장해 둔 사본(텔레그램 HTML + 미디어 file_id; 사진·영상·GIF·파일은 mediastore 보관 원본)으로.
도배 막기: 같은 낱말은 방 설정 autoreply_gap 초(기본 30)에 1번(DB 한 문장 UPDATE 로 차지 — 재시작해도 그대로),
       한 사람은 USER_GAP 초에 1번, 방 하루 DAILY_MAX 번. 막힌 건 조용히 넘어감.
지우기: `.reply 취소 공지` · `.답글취소 공지` · 메뉴 🗑 · AI 도구.
낱말이 소담 명령어(.공지 등)·포인트 게임(!…)과 겹치면 저장 안 함 (명령이 먼저라 울릴 수 없음).
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime

from telegram import ReplyParameters
from telegram.error import BadRequest, TelegramError

from . import mediastore
from .announce import extract_media
from .db import register_schema
from .settings import register_setting
from .util import esc, html_plain, rich_html

log = logging.getLogger(__name__)

MAX_PER_ROOM = 50
WORD_MAX = 30
USER_GAP = 10            # 한 사람이 연달아 쳐도 10초에 1번
DAILY_MAX = 200          # 방 하루
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
SNAP_KINDS = ("photo", "video", "animation", "document", "sticker", "voice", "audio", "video_note")
KIND_NAMES = {"photo": "사진", "video": "영상", "animation": "GIF", "document": "파일", "sticker": "스티커",
              "voice": "음성", "audio": "음악", "video_note": "동그라미 영상"}
CANCEL_WORDS = ("취소", "삭제", "지우기", "해제", "끄기", "del", "delete", "remove", "off")
LIST_WORDS = ("목록", "list", "보기")

register_setting("autoreply_gap", 30, "자동 답글 같은 낱말 간격(초)", range_=(5, 600))

register_schema("""
CREATE TABLE IF NOT EXISTS auto_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,                 -- norm(낱말)
    word       TEXT NOT NULL,                 -- 관리자가 쓴 그대로 (목록 표시)
    src_chat   INTEGER,                       -- 원본 글 (copy_message)
    src_msg    INTEGER,
    snap       TEXT NOT NULL DEFAULT '{}',    -- 사본 {html, kind, file_id}
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    last_used  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, key)
);
""")

_ZW = re.compile(r"[​-‏⁠﻿ㅤᅟᅠ]")
_TRAIL = "?!.~ ？！。…"
_EMOJI_TAG = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", re.S)

_cache: dict[int, dict[str, int]] = {}          # 방 → {key: id}  (이 프로세스, 바꿀 때 비움)
_user_last: dict[tuple[int, int], float] = {}
_daily: dict[tuple[int, str], int] = {}


def norm(text: str) -> str:
    t = _ZW.sub("", unicodedata.normalize("NFKC", text or ""))
    t = re.sub(r"\s+", " ", t).strip().rstrip(_TRAIL).strip()
    return t.casefold()


def check_word(word: str) -> str | None:
    """저장할 수 있는 낱말이면 None, 아니면 이유."""
    key = norm(word)
    if not key or not any(ch.isalnum() for ch in key):
        return "낱말에 글자나 숫자가 하나는 있어야 해요."
    if len(key) > WORD_MAX:
        return f"낱말은 {WORD_MAX}자까지예요."
    from . import casino, commands   # 늦게 (commands → … 순환)
    if commands.parse(word.strip(), "") or (key[:1] in "./" and commands._INDEX.get(key[1:].split(" ")[0])):
        return f"'{esc(word.strip())}' 는 소담 명령어라 자동 답글로 못 써요 (명령이 먼저 나가요). 다른 낱말로 해 주세요."
    if key.startswith("!") and casino.parse(word.strip()):
        return f"'{esc(word.strip())}' 는 포인트 게임 명령이라 못 써요."
    return None


def split_words(text: str) -> list[str]:
    return list(dict.fromkeys(w.strip() for w in re.split(r"[,\n]", text or "") if w.strip()))[:5]


def snapshot(msg) -> dict:
    """원본이 지워져도 같은 글을 올리기 위한 사본 (움직이는 이모지·서식은 텔레그램 HTML 로)."""
    kind, fid = extract_media(msg)
    if not kind:
        for k in ("sticker", "voice", "audio", "video_note"):
            obj = getattr(msg, k, None)
            if obj is not None:
                kind, fid = k, obj.file_id
                break
    plain = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    try:
        body = rich_html(msg) or esc(plain)
    except (AttributeError, TypeError):
        body = esc(plain)
    return {"html": body, "kind": kind, "file_id": fid}


def describe(snap: dict) -> str:
    kind = snap.get("kind")
    text = html_plain(snap.get("html") or "").strip().replace("\n", " ")
    head = f"[{KIND_NAMES.get(kind, kind)}] " if kind else ""
    return head + (text[:40] + ("…" if len(text) > 40 else "") if text else ("" if kind else "(빈 글)"))


async def rows(db, chat_id: int) -> list:
    return await db._all("SELECT * FROM auto_replies WHERE chat_id=? ORDER BY word", (chat_id,))


async def get(db, chat_id: int, rid: int):
    return await db._one("SELECT * FROM auto_replies WHERE id=? AND chat_id=?", (rid, chat_id))


async def save(db, chat_id: int, word: str, *, by: int, src: tuple[int, int] | None, snap: dict,
               bot=None) -> tuple[bool, str]:
    """(성공?, 안내문 HTML). 같은 낱말이면 바꿈."""
    why = check_word(word)
    if why:
        return False, why
    if not snap.get("html") and not snap.get("kind"):
        return False, "저장할 글이 비어 있어요."
    if len(html_plain(snap.get("html") or "")) > TEXT_LIMIT:
        return False, "글이 너무 길어요 (4096자까지)."
    key = norm(word)

    def run(conn):
        old = conn.execute("SELECT id FROM auto_replies WHERE chat_id=? AND key=?", (chat_id, key)).fetchone()
        if old is None and conn.execute("SELECT COUNT(*) FROM auto_replies WHERE chat_id=?",
                                        (chat_id,)).fetchone()[0] >= MAX_PER_ROOM:
            return "full"
        conn.execute(
            "INSERT INTO auto_replies(chat_id, key, word, src_chat, src_msg, snap, created_by, created_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chat_id, key) DO UPDATE SET word=excluded.word, "
            "src_chat=excluded.src_chat, src_msg=excluded.src_msg, snap=excluded.snap, created_by=excluded.created_by, "
            "created_at=excluded.created_at, uses=0, last_used=0",
            (chat_id, key, word.strip()[:60], src[0] if src else None, src[1] if src else None,
             json.dumps(snap, ensure_ascii=False), by, int(time.time())))
        return "new" if old is None else "changed"

    res = await db.atomic(run)
    if res == "full":
        return False, f"자동 답글은 방마다 {MAX_PER_ROOM}개까지예요. 안 쓰는 걸 먼저 지워 주세요."
    _cache.pop(chat_id, None)
    if snap.get("kind") in mediastore.KINDS and bot is not None:
        mediastore.remember_soon(bot, db, snap["kind"], snap["file_id"])   # 봇이 바뀌어도 사본이 살게
    await db.log_mod(chat_id, by, None, "auto_reply", f"{'바꿈' if res == 'changed' else '추가'}: {word.strip()[:30]}")
    verb = "바꿨어요" if res == "changed" else "저장했어요"
    return True, (f"💬 이제 누가 <b>{esc(word.strip())}</b> 라고 치면 이 글로 답해요 ({verb}). "
                  f"지우기: <code>.reply 취소 {esc(word.strip())}</code>")


async def remove(db, chat_id: int, word: str, by: int) -> bool:
    key = norm(word)
    n = await db.atomic(lambda c: c.execute("DELETE FROM auto_replies WHERE chat_id=? AND key=?", (chat_id, key)).rowcount)
    _cache.pop(chat_id, None)
    if n:
        await db.log_mod(chat_id, by, None, "auto_reply_del", word.strip()[:30])
    return bool(n)


async def remove_id(db, chat_id: int, rid: int, by: int) -> str | None:
    r = await get(db, chat_id, rid)
    if r is None:
        return None
    await db._write("DELETE FROM auto_replies WHERE id=? AND chat_id=?", (rid, chat_id))
    _cache.pop(chat_id, None)
    await db.log_mod(chat_id, by, None, "auto_reply_del", r["word"][:30])
    return r["word"]


async def _keys(db, chat_id: int) -> dict[str, int]:
    got = _cache.get(chat_id)
    if got is None:
        got = {r["key"]: r["id"] for r in await db._all("SELECT id, key FROM auto_replies WHERE chat_id=?", (chat_id,))}
        _cache[chat_id] = got
    return got


def _no_emoji(html_text: str) -> str:
    """움직이는 이모지를 못 쓰는 봇이면(거절) 보이는 기본 이모지로."""
    return _EMOJI_TAG.sub(r"\1", html_text)


async def _send_text(bot, chat_id: int, html_text: str, rp):
    try:
        return await bot.send_message(chat_id, html_text, parse_mode="HTML", reply_parameters=rp)
    except BadRequest:
        plain = _no_emoji(html_text)
        if plain == html_text:
            raise
        return await bot.send_message(chat_id, plain, parse_mode="HTML", reply_parameters=rp)


async def send_snap(bot, db, chat_id: int, snap: dict, reply_to: int | None = None):
    rp = ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
    kind, fid, body = snap.get("kind"), snap.get("file_id"), snap.get("html") or ""
    if not kind or not fid:
        return await _send_text(bot, chat_id, body, rp)
    fits = body and kind not in ("sticker", "video_note") and len(html_plain(body)) <= CAPTION_LIMIT
    cap = {"caption": body, "parse_mode": "HTML"} if fits else {}
    if kind in mediastore.KINDS:
        try:
            sent = await mediastore.send(bot, db, kind, chat_id, fid, reply_parameters=rp, **cap)
        except BadRequest:
            if not cap or _no_emoji(body) == body:
                raise
            sent = await mediastore.send(bot, db, kind, chat_id, fid, reply_parameters=rp,
                                         caption=_no_emoji(body), parse_mode="HTML")
    else:
        fn = getattr(bot, "send_" + kind)          # send_sticker · send_voice · send_audio · send_video_note
        sent = await fn(chat_id, fid, reply_parameters=rp, **cap)
    if body and not fits:
        sent = await _send_text(bot, chat_id, body, None)
    return sent


async def send(bot, db, chat_id: int, row, reply_to: int | None = None):
    """원본 복사 → 안 되면 사본."""
    if row["src_chat"] and row["src_msg"]:
        try:
            rp = ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
            return await bot.copy_message(chat_id, row["src_chat"], row["src_msg"], reply_parameters=rp)
        except TelegramError as e:
            log.info("자동 답글 원본 복사 안 됨 %s #%s: %s — 사본으로", chat_id, row["id"], e)
    snap = json.loads(row["snap"] or "{}")
    return await send_snap(bot, db, chat_id, snap, reply_to)


async def maybe_reply(svc, bot, msg, text: str) -> bool:
    """그룹 글이 등록한 낱말과 같으면 답하고 True (AI·게임은 안 탐). 도배로 막힌 것도 True (조용히)."""
    if not text or len(text) > WORD_MAX * 2 + 10:
        return False
    chat_id = msg.chat_id
    keys = await _keys(svc.db, chat_id)
    if not keys:
        return False
    rid = keys.get(norm(text))
    if rid is None:
        return False
    user = msg.from_user
    now = time.time()
    uk = (chat_id, user.id if user else 0)
    if now - _user_last.get(uk, 0) < USER_GAP:
        return True
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    if _daily.get((chat_id, day), 0) >= DAILY_MAX:
        return True
    gap = int((await svc.db.get_settings(chat_id)).get("autoreply_gap") or 30)
    claimed = await svc.db.atomic(lambda c: c.execute(
        "UPDATE auto_replies SET uses=uses+1, last_used=? WHERE id=? AND chat_id=? AND ?-last_used >= ?",
        (int(now), rid, chat_id, int(now), gap)).rowcount)
    if not claimed:
        return True
    _user_last[uk] = now
    if len(_daily) > 2000:
        _daily.clear()
    _daily[(chat_id, day)] = _daily.get((chat_id, day), 0) + 1
    row = await get(svc.db, chat_id, rid)
    if row is None:
        return True
    try:
        await send(bot, svc.db, chat_id, row, msg.message_id)
    except TelegramError as e:
        log.warning("자동 답글 못 보냄 %s #%s: %s", chat_id, rid, e)
    return True


def register_commands() -> None:
    """.reply 명령 등록 — handlers 가 import 뒤 부름 (panels/autoreply 는 commands 가 덜 읽혔을 때 import 됨)."""
    from .panels import autoreply as panel
    panel.register_commands()
