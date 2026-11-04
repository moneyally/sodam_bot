"""🎞 모션 배너(텔레모션) 재전송 — 관리자가 커스텀 이모지 배너를 소담 1:1 에 **전달**하면,
안 깨지게 고쳐서 고른 방에 다시 올린다. (2026-11-04 오너 '꼬꼬방 사신팀 텔레모션 배너가 계단으로 깨짐')

'텔레모션/모션 배너' = 큰 광고 그림을 100×100 프리미엄 커스텀 이모지 조각으로 쪼개 글 한 줄에 격자처럼 이어 친 것.
왜 깨지냐 (오픈소스 telegram-premium-emojis MIT · 공식 core.telegram.org/api/custom-emoji):
 1) 전체폭 스페이서가 없으면 격자가 좁게 짜부라져 줄이 어긋난다(계단) → 안 보이는 점자 공백(⠀) 한 줄을 끝에 붙여 꽉 찬 폭으로.
 2) 봇 주인이 프리미엄이어야 봇이 커스텀 이모지를 보낸다 (아니면 폴백 글자로 떨어짐 — 소담 주인은 프리미엄).
 3) 가로가 폰 폭보다 넓으면 줄바꿈돼 깨지는 건 인라인으론 못 고침 → 그땐 가로를 줄이거나(세로형) 영상으로. 경고만 한다.

PTB 가 text_html 로 이미 <tg-emoji> 격자를 다 주니 조각을 다시 뽑을 필요 없이 그 HTML 에 스페이서만 붙여 보낸다.
1:1 전달 → 미리보기(실제로 올림) + 방 고르기 버튼(mbn:) → 누르면 그 방에 재전송(선택 시 📌 고정).
"""
from __future__ import annotations

import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .. import emojilab, hooks, menu
from ..db import register_schema

SPACER = "⠀" * 30      # 안 보이는 점자 공백(U+2800) 한 줄 → 메시지를 꽉 찬 폭으로 (짜부라짐=계단 방지)
MIN_TILES = 4               # 커스텀 이모지 이만큼 이상이면 '모션 배너'로 본다 (평소 이모지 한두 개는 제외)
SAFE_COLS = 9               # 한 줄에 이보다 많으면 폰에서 줄바꿈돼 깨질 수 있다 → 경고
PICK_MAX = 12               # 방 고르기 버튼 최대
DRAFT_TTL = 3600            # 초안 보관 1시간

register_schema(
    "CREATE TABLE IF NOT EXISTS motion_drafts ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, html TEXT, cols INTEGER, pin INTEGER DEFAULT 0, ts INTEGER)")


def tile_count(msg) -> int:
    ents = (msg.entities if msg.text is not None else getattr(msg, "caption_entities", None)) or ()
    return sum(1 for e in ents
               if getattr(e, "type", "") == "custom_emoji" and getattr(e, "custom_emoji_id", None))


def grid_cols(msg) -> int:
    """가장 넓은 줄의 조각 수 (줄바꿈 기준). emojilab.tiles_of 재사용."""
    rows, _ = emojilab.tiles_of(msg)
    return max((len(r) for r in rows), default=0)


def fixed_html(msg) -> str | None:
    """배너 HTML 에 전체폭 스페이서 한 줄을 붙여 안 깨지게. 조각(커스텀 이모지)·순서는 그대로."""
    html = msg.text_html if msg.text is not None else getattr(msg, "caption_html", None)
    if not html:
        return None
    return html.rstrip("\n") + "\n" + SPACER


def is_forwarded(msg) -> bool:
    return bool(getattr(msg, "forward_origin", None) or getattr(msg, "forward_date", None)
                or getattr(msg, "forward_from", None) or getattr(msg, "forward_from_chat", None))


# ── 초안 ─────────────────────────────────────────────────
async def _save(db, uid: int, html: str, cols: int) -> int:
    now = int(time.time())
    await db._write("DELETE FROM motion_drafts WHERE ts < ?", (now - DRAFT_TTL,))

    def run(c):
        cur = c.execute("INSERT INTO motion_drafts(user_id, html, cols, pin, ts) VALUES(?,?,?,0,?)",
                        (uid, html, cols, now))
        return cur.lastrowid
    return await db.atomic(run)


async def _draft(db, did: int):
    return await db._one("SELECT * FROM motion_drafts WHERE id=?", (did,))


# ── 1:1 전달 받기 ────────────────────────────────────────
async def maybe_offer(svc, bot, msg, user) -> bool:
    """관리자가 1:1 에 **전달**한 커스텀 이모지 배너면 고쳐서 방 고르기를 띄우고 True."""
    if not is_forwarded(msg) or tile_count(msg) < MIN_TILES:
        return False
    html = fixed_html(msg)
    if not html:
        return False
    groups = await menu.admin_groups(svc, bot, user.id)
    if not groups and user.id not in await svc.perms.owners():
        return False
    cols = grid_cols(msg)
    did = await _save(svc.db, user.id, html, cols)
    try:   # 미리보기 = 고친 배너를 1:1 에 실제로 올려 본다 (주인 프리미엄이면 보임)
        await bot.send_message(user.id, html, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramError:
        pass
    warn = (f"\n⚠️ 가로 {cols}칸이라 폰에서 줄바꿈돼 깨질 수 있어 (가로 {SAFE_COLS}칸 이하 세로형 배너 권장)"
            if cols > SAFE_COLS else "")
    await bot.send_message(
        user.id,
        f"🎞 모션 배너 받았어 (이모지 조각 {tile_count(msg)}칸). 안 깨지게 폭을 고쳤어 — 위가 미리보기야.{warn}\n"
        f"어느 방에 올릴까?",
        reply_markup=_picker(did, groups, pin=False))
    return True


def _picker(did: int, groups, *, pin: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(("📌 고정 켜짐" if pin else "📌 올리고 고정"), callback_data=f"mbn:pin:{did}")]]
    for cid, title in groups[:PICK_MAX]:
        rows.append([InlineKeyboardButton(f"🏠 {title[:40]}", callback_data=f"mbn:go:{did}:{cid}")])
    rows.append([InlineKeyboardButton("✖️ 취소", callback_data=f"mbn:x:{did}")])
    return InlineKeyboardMarkup(rows)


# ── 버튼 ─────────────────────────────────────────────────
async def on_cb(svc, bot, q, parts) -> None:
    act = parts[0] if parts else ""
    did = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
    row = await _draft(svc.db, did)
    if not row or row["user_id"] != q.from_user.id:
        return await q.answer("이 버튼은 요청한 사람만 눌러요 (또는 만료됐어요).", show_alert=True)

    if act == "x":
        await svc.db._write("DELETE FROM motion_drafts WHERE id=?", (did,))
        return await q.answer("취소했어요.")

    if act == "pin":
        pin = 0 if row["pin"] else 1
        await svc.db._write("UPDATE motion_drafts SET pin=? WHERE id=?", (pin, did))
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
        try:
            sent = await bot.send_message(cid, row["html"], parse_mode="HTML", disable_web_page_preview=True)
        except TelegramError as e:
            return await q.answer(f"못 올렸어요: {e.message[:120]}", show_alert=True)
        pinned = ""
        if row["pin"]:
            try:
                await bot.pin_chat_message(cid, sent.message_id, disable_notification=True)
                pinned = " · 📌 고정"
            except TelegramError:
                pinned = " · (고정 권한 없음)"
        await svc.db._write("DELETE FROM motion_drafts WHERE id=?", (did,))
        return await q.answer(f"✅ 올렸어요{pinned}")

    await q.answer()


hooks.add_callback_handler("mbn", on_cb)
