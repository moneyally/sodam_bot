"""🚪 가입 신청 1:1 확인 (join_verify, 기본 꺼짐).

방이 '가입 신청 승인' 방식이면(초대링크 승인제·공개방 가입 신청), 신청이 오면:
- 봇 계정 → 거절 · 자유 멤버(free.py) → 바로 승인 · 스팸 명단(CAS·lols)·공동 차단 명단(각 설정이 켜져 있을 때) → 거절
- 나머지는 신청자에게 1:1 로 그림 버튼 문제(captcha.puzzle). 맞히면 승인, 3번 틀리거나 시간이 지나면 거절.
텔레그램은 신청 뒤 5분 동안 봇이 신청자에게 1:1 을 보낼 수 있게 해준다 (ChatJoinRequest.user_chat_id).
1:1 을 못 보내면 그대로 두어 관리자가 직접 승인한다. 통과한 사람은 입장 뒤 방 캡차·대량 입장 내보내기를 건너뛴다(passed).
봇에게 '사용자 초대(can_invite_users)' 권한이 있어야 신청을 받고 처리할 수 있다.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

from . import fedban, free
from .captcha import MAX_ATTEMPTS, puzzle
from .db import register_schema
from .settings import register_setting
from .util import esc

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

register_setting("join_verify", False, "가입 신청 1:1 확인")
register_schema("""
CREATE TABLE IF NOT EXISTS join_requests (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    answer   INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    passed   INTEGER NOT NULL DEFAULT 0,
    expires  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"join_requests": "composite"})

PASS_KEEP = 3600   # 통과 후 이 시간 안에 들어오면 방 캡차 생략


async def _row(svc: Services, chat_id: int, user_id: int):
    return await svc.db._one("SELECT * FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id))


async def passed(svc: Services, chat_id: int, user_id: int) -> bool:
    row = await _row(svc, chat_id, user_id)
    return bool(row and row["passed"] and row["expires"] > time.time())


async def _decline(svc: Services, bot, chat_id: int, user_id: int, reason: str) -> None:
    await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    try:
        await bot.decline_chat_join_request(chat_id, user_id)
    except TelegramError as e:   # 이미 처리됨(관리자가 직접)·신청 취소
        log.info("decline join request failed %s/%s: %s", chat_id, user_id, e)
    await svc.db.log_mod(chat_id, None, user_id, "join_decline", reason)


async def on_request(svc: Services, bot, req) -> None:
    chat_id, user = req.chat.id, req.from_user
    s = await svc.db.get_settings(chat_id)
    if not s["join_verify"]:
        return
    if user.is_bot:
        return await _decline(svc, bot, chat_id, user.id, "봇 계정")
    if await free.is_free(svc.db, chat_id, user.id):
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except TelegramError as e:
            log.info("approve free member failed: %s", e)
        return
    if (s["cas_enabled"] and await svc.cas.is_banned(user.id)) or \
            (s["fedban_mode"] != "off" and await fedban.lookup(svc.db, user.id)):
        return await _decline(svc, bot, chat_id, user.id, "스팸·공동 차단 명단")
    from .subscription import chat_title  # 늦게 import (순환 방지)
    label, answer, rows = puzzle(f"jr:{chat_id}")
    minutes = s["captcha_minutes"]
    text = (f"🚪 <b>{esc(await chat_title(svc, chat_id))}</b> 입장 신청 확인이에요.\n"
            f"<b>{minutes}분 안에</b> 아래에서 <b>{label}</b> 버튼을 눌러주세요. 맞히면 바로 들어가요.")
    try:
        await bot.send_message(req.user_chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    except TelegramError as e:   # 1:1 을 못 보냄 → 관리자가 직접 승인하도록 그대로 둠
        log.info("join verify dm failed %s/%s: %s", chat_id, user.id, e)
        return
    await svc.db._write("INSERT OR REPLACE INTO join_requests(chat_id, user_id, answer, expires) VALUES(?, ?, ?, ?)",
                        (chat_id, user.id, answer, int(time.time()) + minutes * 60))


async def on_callback(svc: Services, bot, q, parts: list[str]) -> None:
    """jr:<방ID>:<번호> — 신청자 1:1 의 그림 버튼."""
    try:
        chat_id, choice = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return await q.answer()
    uid = q.from_user.id
    row = await _row(svc, chat_id, uid)
    if not row or row["passed"]:
        await q.answer("이미 끝난 확인이에요.")
        return
    if choice != row["answer"]:
        attempts = row["attempts"] + 1
        if attempts < MAX_ATTEMPTS:
            await svc.db._write("UPDATE join_requests SET attempts=? WHERE chat_id=? AND user_id=?",
                                (attempts, chat_id, uid))
            return await q.answer(f"틀렸어요! 남은 기회 {MAX_ATTEMPTS - attempts}번", show_alert=True)
        await _decline(svc, bot, chat_id, uid, f"가입 확인 {MAX_ATTEMPTS}회 실패")
        await q.answer()
        return await _edit(q, "❌ 확인에 실패해서 신청이 거절됐어요. 잠시 뒤 다시 신청해 주세요.")
    try:
        await bot.approve_chat_join_request(chat_id, uid)
    except TelegramError as e:   # 관리자가 이미 처리했거나 신청을 취소함
        await svc.db._write("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, uid))
        await q.answer()
        return await _edit(q, f"이 신청은 이미 처리됐어요. ({esc(e.message[:80])})")
    await svc.db._write("UPDATE join_requests SET passed=1, expires=? WHERE chat_id=? AND user_id=?",
                        (int(time.time()) + PASS_KEEP, chat_id, uid))
    await svc.db.log_mod(chat_id, None, uid, "join_pass", "가입 신청 1:1 확인")
    await q.answer("확인됐어요!")
    await _edit(q, "✅ 확인됐어요! 방에 들어갔어요. 환영합니다 🙌")


async def _edit(q, text: str) -> None:
    try:
        await q.edit_message_text(text)
    except TelegramError:
        pass


async def expire(svc: Services, bot) -> None:
    """30초마다: 시간이 지난 미확인 신청은 거절, 통과 기록은 정리."""
    now = int(time.time())
    for row in await svc.db._all("SELECT chat_id, user_id FROM join_requests WHERE passed=0 AND expires<=?", (now,)):
        await _decline(svc, bot, row["chat_id"], row["user_id"], "가입 확인 시간 초과")
    await svc.db._write("DELETE FROM join_requests WHERE passed=1 AND expires<=?", (now,))
