"""📤 전달 글 그대로 올리기 — 완성된 채널/방 글(사진+글+버튼)을 소담 1:1 에 **전달**하면
`copy_message` 로 고른 방에 그대로 재전송한다 (전달 머리말 없이 · 사진·글·서식·URL 버튼 유지).

리모컨(composer)으로 본문·사진·버튼을 하나하나 안 만들어도 됨 (2026-11-12 오너 '채널전달하면 인식돼서
그 글 바뀌는 거 없나, 존나 어렵게 돼있노'). 모션배너(motionbanner) 전달 흐름을 일반 글로 넓힌 것.

- 미디어(사진·영상·GIF·문서·스티커) 있는 전달글만 제안 (텍스트만 전달은 기존 '누구 글인지' 이름 조회 유지).
- 커스텀 이모지 배너(조각 MIN_TILES↑)는 motionbanner 가 먼저 처리하므로 제외.
- URL 버튼은 전달 메시지의 reply_markup 을 그대로 복사해 copy_message 에 다시 붙인다
  (텔레그램은 전달된 채널 글에 인라인 URL 버튼을 포함해서 봇에 준다).
"""
from __future__ import annotations

import json
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .. import hooks, menu
from ..db import register_schema
from . import motionbanner

PICK_MAX = 12
DRAFT_TTL = 3600

register_schema(
    "CREATE TABLE IF NOT EXISTS repost_drafts ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, from_chat INTEGER, msg_id INTEGER, "
    "markup TEXT, pin INTEGER DEFAULT 0, ts INTEGER)")


def _has_media(msg) -> bool:
    return bool(msg.photo or msg.video or msg.animation or msg.document or msg.sticker or msg.audio or msg.voice)


async def _save(db, uid: int, from_chat: int, msg_id: int, markup: str | None) -> int:
    now = int(time.time())
    await db._write("DELETE FROM repost_drafts WHERE ts < ?", (now - DRAFT_TTL,))

    def run(c):
        cur = c.execute("INSERT INTO repost_drafts(user_id, from_chat, msg_id, markup, pin, ts) VALUES(?,?,?,?,0,?)",
                        (uid, from_chat, msg_id, markup, now))
        return cur.lastrowid
    return await db.atomic(run)


async def _draft(db, did: int):
    return await db._one("SELECT * FROM repost_drafts WHERE id=?", (did,))


# ── 1:1 전달 받기 ────────────────────────────────────────
async def maybe_offer(svc, bot, msg, user) -> bool:
    """관리자가 1:1 에 전달한 완성 글(미디어 있음·배너 아님)이면 방 고르기를 띄우고 True."""
    if not motionbanner.is_forwarded(msg) or not _has_media(msg):
        return False
    if motionbanner.tile_count(msg) >= motionbanner.MIN_TILES:
        return False   # 커스텀 이모지 배너는 motionbanner 가 처리
    groups = await menu.admin_groups(svc, bot, user.id)
    if not groups and user.id not in await svc.perms.owners():
        return False
    markup = msg.reply_markup.to_json() if msg.reply_markup else None
    did = await _save(svc.db, user.id, user.id, msg.message_id, markup)
    btn = " · 🔗 버튼도 그대로" if markup else ""
    await bot.send_message(
        user.id,
        f"📤 이 글 받았어. 리모컨 안 거치고 <b>그대로</b> 올릴게 (사진·글·서식{btn} 유지).\n어느 방에 올릴까?",
        parse_mode="HTML", reply_markup=_picker(did, groups, pin=False))
    return True


def _picker(did: int, groups, *, pin: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(("📌 고정 켜짐" if pin else "📌 올리고 고정"), callback_data=f"fwd:pin:{did}")]]
    for cid, title in groups[:PICK_MAX]:
        rows.append([InlineKeyboardButton(f"🏠 {title[:40]}", callback_data=f"fwd:go:{did}:{cid}")])
    rows.append([InlineKeyboardButton("✖️ 취소", callback_data=f"fwd:x:{did}")])
    return InlineKeyboardMarkup(rows)


# ── 버튼 ─────────────────────────────────────────────────
async def on_cb(svc, bot, q, parts) -> None:
    act = parts[0] if parts else ""
    did = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
    row = await _draft(svc.db, did)
    if not row or row["user_id"] != q.from_user.id:
        return await q.answer("이 버튼은 요청한 사람만 눌러요 (또는 만료됐어요).", show_alert=True)

    if act == "x":
        await svc.db._write("DELETE FROM repost_drafts WHERE id=?", (did,))
        return await q.answer("취소했어요.")

    if act == "pin":
        pin = 0 if row["pin"] else 1
        await svc.db._write("UPDATE repost_drafts SET pin=? WHERE id=?", (pin, did))
        groups = await menu.admin_groups(svc, bot, q.from_user.id)
        try:
            await q.edit_message_reply_markup(_picker(did, groups, pin=bool(pin)))
        except TelegramError:
            pass
        return await q.answer("📌 고정 켬" if pin else "📌 고정 끔")

    if act == "go":
        cid = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
        if not await svc.perms.is_admin(bot, cid, q.from_user.id):
            return await q.answer("그 방 관리자만 올릴 수 있어요.", show_alert=True)
        markup = None
        if row["markup"]:
            try:
                markup = InlineKeyboardMarkup.de_json(json.loads(row["markup"]), bot)
            except Exception:
                markup = None
        try:   # copy_message = 전달 머리말 없이 원본 그대로, 버튼은 따로 붙여 유지
            sent = await bot.copy_message(cid, from_chat_id=row["from_chat"], message_id=row["msg_id"],
                                          reply_markup=markup)
        except TelegramError as e:
            return await q.answer(f"못 올렸어요: {e.message[:120]}", show_alert=True)
        pinned = ""
        if row["pin"]:
            try:
                await bot.pin_chat_message(cid, sent.message_id, disable_notification=True)
                pinned = " · 📌 고정"
            except TelegramError:
                pass
        await svc.db._write("DELETE FROM repost_drafts WHERE id=?", (did,))
        return await q.answer(f"✅ 올렸어요{pinned}")


hooks.add_callback_handler("fwd", on_cb)
