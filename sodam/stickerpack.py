"""📦 소담이 스티커 팩에 바로 넣기 (Bot API createNewStickerSet / addStickerToSet).

왜: 예전엔 스티커 파일 + '@Stickers → /newvideo → … /publish' 안내만 보냄 → 사람이 6단계를 직접 해야 했음.
봇은 사람 대신 팩을 만들 수 있음 (팩 주인 = 그 사람, 이름 끝 '_by_<봇 아이디>', 팩당 120장).
- 스티커 밑 [📦 내 팩에 넣기] 버튼(spk:<항목>) → 누른 사람의 팩에 (누가 만든 스티커든 누른 사람 것).
- AI 도구 sticker_pack: 답장한 스티커 > 소담이 이 사람에게 최근 만든 스티커 > 이 사람이 최근 올린 스티커(media_log).
- 텔레그램은 봇과 1:1 을 한 번도 안 연 사람의 팩을 못 만듦 → [▶️ 1:1 열고 넣기](?start=spk_<항목>) 로 이어서.
항목(sticker_items)은 텔레그램 file_id 만 (파일 안 받음). 다른 팩 스티커가 file_id 로 안 들어가면 한 번 내려받아 올림.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputSticker
from telegram.error import BadRequest, Forbidden, TelegramError

from . import hooks
from .db import register_schema

log = logging.getLogger(__name__)
PER_SET = 120                       # 텔레그램 일반 스티커 팩 한도
ITEM_KEEP = 30 * 86400              # 버튼 항목 보관 (누를 수 있는 기간)
FORMATS = ("static", "video", "animated")

register_schema("""
CREATE TABLE IF NOT EXISTS sticker_items (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL DEFAULT '',
    fmt     TEXT NOT NULL DEFAULT 'video',
    emoji   TEXT NOT NULL DEFAULT '😀',
    chat_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL DEFAULT 0,   -- 만들어 달라고 한 사람
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sticker_items_who ON sticker_items(user_id, ts);
CREATE TABLE IF NOT EXISTS sticker_packs (
    user_id INTEGER NOT NULL,
    vol     INTEGER NOT NULL,
    name    TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (user_id, vol)
);
""")


def pack_name(user_id: int, vol: int, bot_username: str) -> str:
    """영문·숫자·밑줄, 글자로 시작, 밑줄 연속 금지, '_by_<봇>' 로 끝 (Bot API createNewStickerSet name)."""
    return f"sodam{user_id}" + (f"v{vol}" if vol > 1 else "") + f"_by_{bot_username}"


def add_url(name: str) -> str:
    return f"https://t.me/addstickers/{name}"


def keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📦 내 팩에 넣기", callback_data=f"spk:{item_id}")]])


async def new_item(db, *, fmt: str, emoji: str, chat_id: int, user_id: int, file_id: str = "") -> int:
    """버튼에 걸 항목 (보내기 전에 만들고 보낸 뒤 file_id 를 채움)."""
    now = int(time.time())

    def tx(conn: sqlite3.Connection) -> int:
        cur = conn.execute("INSERT INTO sticker_items (file_id, fmt, emoji, chat_id, user_id, ts) VALUES (?,?,?,?,?,?)",
                           (file_id, fmt if fmt in FORMATS else "video", (emoji or "😀")[:8], chat_id, user_id, now))
        conn.execute("DELETE FROM sticker_items WHERE ts < ?", (now - ITEM_KEEP,))
        return cur.lastrowid
    return await db.atomic(tx)


async def set_file(db, item_id: int, file_id: str) -> None:
    await db._write("UPDATE sticker_items SET file_id=? WHERE id=?", (file_id, item_id))


async def item(db, item_id: int):
    return await db._one("SELECT * FROM sticker_items WHERE id=? AND file_id != ''", (item_id,))


async def packs(db, user_id: int) -> list:
    return await db._all("SELECT * FROM sticker_packs WHERE user_id=? ORDER BY vol", (user_id,))


class NeedsDM(Exception):
    """이 사람이 봇과 1:1 을 안 열어서 팩을 못 만듦."""


def _err(e: Exception) -> str:
    return (getattr(e, "message", "") or str(e)).lower()


_NO_USER = re.compile(r"peer_id_invalid|user not found|user_id_invalid|bot was blocked|chat not found")


async def _sticker_input(bot, row, refetch: bool) -> InputSticker:
    src = row["file_id"]
    if refetch:                       # 다른 팩 스티커 file_id 가 거절되면 파일로 올림
        f = await bot.get_file(row["file_id"])
        ext = {"static": "webp", "video": "webm", "animated": "tgs"}[row["fmt"]]
        src = InputFile(bytes(await f.download_as_bytearray()), filename=f"sticker.{ext}")
    return InputSticker(src, [row["emoji"] or "😀"], row["fmt"])


async def add(bot, db, user_id: int, title_name: str, row) -> tuple[str, int]:
    """row(sticker_items 같은 모양) 를 user_id 의 팩에 → (팩 이름, 그 팩 장 수). 1:1 안 열었으면 NeedsDM."""
    username = bot.username
    have = await packs(db, user_id)
    cur = have[-1] if have else None
    vol = cur["vol"] if cur else 1
    exists = bool(cur)
    if cur and cur["count"] >= PER_SET:
        vol, exists = vol + 1, False
    for refetch in (False, True):
        for _ in range(3):            # 팩 상태가 DB 와 다를 때(지워짐·꽉 참·이미 있음) 고쳐서 다시
            name = pack_name(user_id, vol, username)
            try:
                sticker = await _sticker_input(bot, row, refetch)
                if exists:
                    await bot.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker)
                else:
                    title = f"{title_name[:40]} · 소담 스티커" + (f" {vol}" if vol > 1 else "")
                    await bot.create_new_sticker_set(user_id=user_id, name=name, title=title, stickers=[sticker])
            except Forbidden as e:
                raise NeedsDM() from e
            except BadRequest as e:
                m = _err(e)
                if _NO_USER.search(m):
                    raise NeedsDM() from e
                if "occupied" in m and not exists:          # 팩은 있는데 DB 에 없음 (DB 초기화 등)
                    exists = True
                    continue
                if "stickerset_invalid" in m and exists:    # 사람이 팩을 지움 → 새로
                    exists = False
                    continue
                if "too_much" in m or "too much" in m:     # 꽉 참 → 다음 권
                    vol, exists = vol + 1, False
                    continue
                if not refetch and ("file" in m or "sticker" in m or "wrong" in m):
                    break                                   # file_id 로 안 됨 → 내려받아 올리기
                raise
            await _bump(db, user_id, vol, name)
            got = await db._one("SELECT count FROM sticker_packs WHERE user_id=? AND vol=?", (user_id, vol))
            return name, got["count"]
        else:
            raise BadRequest("sticker pack state kept changing")
    raise BadRequest("sticker upload failed")


async def _bump(db, user_id: int, vol: int, name: str) -> None:
    await db._write("INSERT INTO sticker_packs (user_id, vol, name, count, ts) VALUES (?,?,?,1,?) "
                    "ON CONFLICT(user_id, vol) DO UPDATE SET count=count+1, name=excluded.name, ts=excluded.ts",
                    (user_id, vol, name, int(time.time())))


def _who(user) -> str:
    return (user.first_name or user.username or "내").strip()


def _dm_kb(bot, item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ 1:1 열고 넣기", url=f"https://t.me/{bot.username}?start=spk_{item_id}")]])


def _done_kb(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("➕ 내 텔레그램에 팩 추가", url=add_url(name))]])


async def on_button(svc, bot, q, parts) -> None:
    """[📦 내 팩에 넣기] — 누른 사람 팩에. 결과는 누른 사람 1:1 (막히면 방에 1:1 열기 버튼)."""
    user = q.from_user
    row = await item(svc.db, int(parts[0])) if parts and parts[0].isdecimal() else None   # parts = 접두어 뒤 (spk:<항목>)
    if not row:
        await q.answer("오래된 스티커라 못 넣어요. 다시 만들어 달라고 해 주세요.", show_alert=True)
        return
    try:
        name, n = await add(bot, svc.db, user.id, _who(user), row)
    except NeedsDM:
        await q.answer("먼저 소담 1:1 을 한 번 열어야 팩을 만들 수 있어요 (텔레그램 규칙).", show_alert=True)
        try:
            await bot.send_message(q.message.chat_id, f"📦 {_who(user)}님, 아래 버튼으로 소담 1:1 을 열면 바로 팩에 넣어 드려요.",
                                   reply_markup=_dm_kb(bot, row["id"]))
        except TelegramError:
            pass
        return
    except TelegramError as e:
        log.warning("sticker pack add failed: %s", e)
        await q.answer(f"팩에 못 넣었어요: {getattr(e, 'message', e)}"[:190], show_alert=True)
        return
    await q.answer(f"📦 내 팩에 넣었어요 ({n}장)")
    try:
        await bot.send_message(user.id, f"📦 팩에 넣었어요 — 지금 {n}장.\n처음이면 아래 버튼으로 내 텔레그램에 추가하세요.",
                               reply_markup=_done_kb(name))
    except TelegramError:
        pass


async def on_deep_link(svc, bot, msg, arg: str) -> None:
    """1:1 /start spk_<항목> — 1:1 을 열었으니 이제 팩에 넣음."""
    row = await item(svc.db, int(arg)) if arg.isdecimal() else None
    if not row:
        await msg.reply_text("오래된 스티커라 못 넣어요. 방에서 다시 만들어 달라고 해 주세요.")
        return
    try:
        name, n = await add(bot, svc.db, msg.from_user.id, _who(msg.from_user), row)
    except (NeedsDM, TelegramError) as e:
        await msg.reply_text(f"팩에 못 넣었어요: {getattr(e, 'message', '') or '텔레그램이 거절'}")
        return
    await msg.reply_text(f"📦 팩에 넣었어요 — 지금 {n}장.", reply_markup=_done_kb(name))


hooks.add_callback_handler("spk", on_button)
hooks.add_deep_link("spk", on_deep_link)
