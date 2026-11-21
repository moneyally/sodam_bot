"""💰 보유금 목표 달성 알림 — 손님 보유 포인트가 방 설정 목표(casino_goal)를 넘으면 그 방 운영자
(봇 넣은 등록 관리자, 또는 casino_goal_contact 로 지정)·오너에게 **게임 집계와 함께 1:1 알림** + 방 축하 카드.

특정인을 코드에 박지 않는다 — 방마다 '그 방 운영자' 가 받는다 (화이트라벨 클론도 그대로). 포인트는 안 건드림(정산은 운영자 수동).
전목사님 목사봇(클론)처럼 '게임 집계 + 목표 달성 알림' 을 쓰려는 방을 위한 일반 기능 (2026-11-21 오너).

흐름: core.credit(지급) → _on_credit 이 보유금≥목표면 DB 에 1회 표시(claim) → 30초 틱 tick 이 집계·알림 → status new→notified.
운영자가 [✅ 확인] 누르면 done. 한 사람 한 번만(PRIMARY KEY).  python tests/run_all.py casino_goal
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .. import hooks
from ..db import now, register_schema
from ..settings import register_setting
from ..util import esc, to_int
from . import core, register_callback
from .core import fmt

log = logging.getLogger(__name__)

register_setting("casino_goal", 0, "보유금 목표 달성 알림 (0=끔)", range_=(0, 1_000_000_000))
register_setting("casino_goal_contact", "", "목표 달성 문의 받을 @아이디·ID (비우면 봇 넣은 관리자)")

register_schema("""
CREATE TABLE IF NOT EXISTS casino_goal_hit (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    points  INTEGER NOT NULL,
    goal    INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    status  TEXT NOT NULL DEFAULT 'new',   -- new → notified → done
    PRIMARY KEY (chat_id, user_id)
);
""")


async def _on_credit(db, chat_id: int, user_id: int, after: int, reason: str) -> None:
    """지급 뒤: 보유금이 목표를 넘고 아직 알린 적 없으면 한 번만 표시(INSERT OR IGNORE).
    알림은 틱이 보냄(여기선 bot 이 없음). 목표 0=끔인 대부분 방은 설정 한 번 읽고 바로 끝."""
    try:
        goal = int((await db.get_settings(chat_id)).get("casino_goal") or 0)
    except Exception:
        return
    if goal <= 0 or after < goal:
        return
    try:
        await db._write(
            "INSERT OR IGNORE INTO casino_goal_hit(chat_id, user_id, points, goal, ts, status) "
            "VALUES (?,?,?,?,?, 'new')", (chat_id, user_id, after, goal, now()))
    except Exception:
        log.exception("goal claim failed")


core._AFTER_CREDIT.append(_on_credit)   # core.credit 이 지급 끝에 부름


async def _name(db, user_id: int) -> tuple[str, str | None]:
    row = await db._one("SELECT first_name, username FROM users WHERE user_id=?", (user_id,))
    return (row["first_name"] if row and row["first_name"] else str(user_id),
            row["username"] if row and row["username"] else None)


async def tally(db, chat_id: int, user_id: int) -> dict:
    """그 사람 게임 집계 — 코드가 기록에서만 (AI 0원). 총베팅·총획득·순익·판수·현재 보유금."""
    acc = await db._one("SELECT wagered, won FROM casino_accounts WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    wagered = acc["wagered"] if acc else 0
    won = acc["won"] if acc else 0
    plays = (await db._one("SELECT COUNT(*) n FROM casino_ledger WHERE chat_id=? AND user_id=? AND reason LIKE 'bet:%'",
                           (chat_id, user_id)))["n"]
    return {"wagered": wagered, "won": won, "net": won - wagered,
            "plays": plays, "balance": await core.balance(db, chat_id, user_id)}


async def _targets(svc, chat_id: int) -> tuple[set[int], str | None]:
    """알림 받을 사람(운영자 + 오너)과 멤버 문의 버튼용 @아이디. casino_goal_contact 지정 > 봇 넣은 관리자(added_by)."""
    contact = str((await svc.db.get_settings(chat_id)).get("casino_goal_contact") or "").strip().lstrip("@")
    owners = await svc.perms.owners()
    who: int | None = None
    link: str | None = None
    if contact:
        who = to_int(contact)                                      # 숫자 ID
        if who is None:                                            # @아이디 → 기록에서 찾기
            row = await svc.db._one("SELECT user_id FROM users WHERE lower(username)=lower(?)", (contact,))
            who, link = (row["user_id"], contact) if row else (None, contact)
    if who is None:                                                # 지정 없거나 못 찾음 → 봇 넣은 관리자
        sub = await svc.db._one("SELECT added_by FROM subscriptions WHERE chat_id=?", (chat_id,))
        who = sub["added_by"] if sub and sub["added_by"] else None
    if who is not None and link is None:
        _, un = await _name(svc.db, who)
        link = un
    return {p for p in ({who} | set(owners)) if p}, link


async def _announce(svc, bot, chat_id: int, user_id: int, points: int, goal: int) -> None:
    name, _ = await _name(svc.db, user_id)
    t = await tally(svc.db, chat_id, user_id)
    row = await svc.db._one("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
    room = row["title"] if row and row["title"] else str(chat_id)
    people, link = await _targets(svc, chat_id)

    dm = (f"💰 <b>{esc(name)}</b>(<code>{user_id}</code>)님이 보유 포인트 <b>{fmt(points)}</b> 달성!\n"
          f"🏠 {esc(room)} · 목표 {fmt(goal)}\n"
          f"📊 집계 — 총베팅 {fmt(t['wagered'])} · 총획득 {fmt(t['won'])} · 순익 {t['net']:+,} · {t['plays']}판")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ 확인", callback_data=f"cs:goal:ok:{chat_id}:{user_id}")]])
    for pid in people:
        try:
            await bot.send_message(pid, dm, parse_mode="HTML", reply_markup=kb)
        except TelegramError:
            pass                                                   # 1:1 막힘 → 건너뜀 (방 카드는 그대로)

    # 방 축하 카드: 정확 보유금은 방에 공개 안 함(운영자 1:1 에만) — 목표 달성 사실 + 문의 버튼만
    room_kb = (InlineKeyboardMarkup([[InlineKeyboardButton("💬 정산 문의", url=f"https://t.me/{link}")]])
               if link else None)
    try:
        await bot.send_message(chat_id, f"🎉 <b>{esc(name)}</b>님 목표 보유금 <b>{fmt(goal)}</b> 달성! 운영자에게 정산 문의하세요.",
                               parse_mode="HTML", reply_markup=room_kb)
    except TelegramError as e:
        log.warning("goal room card failed: %s", e)


async def tick(svc, bot) -> None:
    """30초: 새로 달성한 사람에게 운영자·오너 1:1 집계 알림 + 방 축하 카드 (한 번만)."""
    rows = await svc.db._all("SELECT chat_id, user_id, points, goal FROM casino_goal_hit WHERE status='new' LIMIT 20")
    for r in rows:
        cid, uid = r["chat_id"], r["user_id"]
        await svc.db._write("UPDATE casino_goal_hit SET status='notified' WHERE chat_id=? AND user_id=? AND status='new'",
                            (cid, uid))                            # 먼저 표시 → 재시작·중복 알림 방지
        try:
            await _announce(svc, bot, cid, uid, r["points"], r["goal"])
        except Exception:
            log.exception("goal announce failed")


hooks.add_tick_hook(tick)


async def cb(svc, bot, q, parts: list[str]) -> None:
    """운영자 1:1 [✅ 확인] → 이 달성 건 처리 완료(done)."""
    if len(parts) == 3 and parts[0] == "ok":
        await svc.db._write("UPDATE casino_goal_hit SET status='done' WHERE chat_id=? AND user_id=?",
                            (to_int(parts[1]) or 0, to_int(parts[2]) or 0))
        await q.answer("처리됨 ✅")
        try:
            await q.edit_message_reply_markup(None)   # 버튼만 치움 (PTB·테스트 모두 첫 positional)
        except TelegramError:
            pass
    else:
        await q.answer("지난 버튼이에요.")


register_callback("goal", cb)
