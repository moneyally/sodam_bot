"""🗳 AI 확인 카드: 누른 결과 기록 · '오늘은 확인 생략' (Codex ReviewDecision · ApprovedForSession).

1) 결과 기록: 제재 카드(handlers._confirm_action)·방 토큰 카드(예약·알림 규칙·방 자료·다른 봇 명령·연동 켜기·방 안내)를
   누르면 짧은 한 줄('✅ 뮤트 10분 실행됨 — 박준호 (처리: 방장)' / '❌ … 취소 (방장)')을 ai_card_log 에 → memory.context_for 가
   <card_results>(nonce 태그 데이터)로 넣음 → '아까 뮤트 됐어?' 에 근거가 생기고, 취소 직후 같은 카드를 또 묻지 않게.
2) 카드 한 장 = ai_cards 한 줄 (토큰들 묶음): 버튼 중 하나라도 먼저 누르면 claim 이 한 번만 성공하고 나머지 토큰은 지움
   (✅ 와 ❌·확인 생략을 거의 동시에 눌러도 한 번, 📥 운영 인박스에 '안 누른 카드'로 남지 않게).
3) 오늘은 확인 생략: LOW_RISK(예약·알림 규칙·다른 봇 명령) 카드에만 세 번째 버튼 → ai_approvals(방, 요청자, 도구, 오늘 자정까지).
   그동안 같은 사람의 같은 도구는 카드 없이 바로 실행 (권한은 매번 새로 확인, mod_log approval_skip·결과 한 줄은 그대로).
   **제재(경고·뮤트·밴·오너 1:1 제재)는 절대 없음** — 제품 규칙 (approved 가 도구 이름으로 한 번 더 막음).
전부 DB → 재시작해도 카드·생략 상태 그대로.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .db import register_schema
from .util import day_start

log = logging.getLogger(__name__)

register_schema("""
CREATE TABLE IF NOT EXISTS ai_cards (
    card    TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    tool    TEXT NOT NULL,
    toks    TEXT NOT NULL DEFAULT '[]',   -- 이 카드의 menu 토큰들 (하나 누르면 나머지 지움)
    created REAL NOT NULL,
    status  TEXT                          -- NULL 안 누름 · ok · day · no
);
CREATE TABLE IF NOT EXISTS ai_card_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,             -- 카드가 뜬 대화 (방, 오너 1:1 제재 카드면 오너 1:1)
    user_id INTEGER,                      -- 요청한 사람
    tool    TEXT NOT NULL,
    text    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_card_log_chat ON ai_card_log(chat_id, id);
CREATE TABLE IF NOT EXISTS ai_approvals (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    tool    TEXT NOT NULL,
    until   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id, tool)
);
""", migrate={"ai_cards": "plain", "ai_card_log": "plain", "ai_approvals": "composite"})

# 확인 생략을 줄 수 있는 도구 (되돌리기 쉽고 사람에게 직접 영향 없는 것). 도구 이름 → 사람이 읽는 이름
LOW_RISK = {"schedule_task": "예약", "alert_rule": "알림 규칙", "bot_command": "다른 봇 명령"}
NEVER = frozenset({"warn", "mute", "ban", "kick", "warn_member", "mute_member", "ban_member", "owner_sanction"})
DAY_LABEL = "✅ + 오늘은 확인 생략"
DAY = "_day"               # 확인 생략 버튼의 토큰 동작 = <ok 동작>_day (패널이 같은 함수로 등록 — 📥 인박스는 ok 동작만 셈)
CARD_KEEP = 86400          # ai_cards 정리
LOG_KEEP = 86400           # 결과 한 줄 보관 (AI 맥락엔 최근 LOG_WINDOW 만)
LOG_WINDOW = 3600
LOG_LINES = 4
LINE_CHARS = 160
ALREADY = "이미 처리된 카드예요."


def _now() -> int:
    return int(time.time())


async def card(svc, uid: int, cid: int, tool: str, ok_action: str, no_action: str, spec: dict, *,
               ok_label: str, no_label: str = "❌ 취소", ttl: int = 1800) -> InlineKeyboardMarkup:
    """방에 올릴 확인 카드 버튼 (menu.lasting_token, 요청한 사람만·재시작 뒤에도). 첫 줄 [ok][no],
    LOW_RISK 도구면 둘째 줄 [✅ + 오늘은 확인 생략] (<ok 동작>_day, spec 에 day). 토큰은 카드 한 장으로 묶어 한 번만."""
    from . import menu   # 늦게 import (menu → panels → cards)
    card_id = secrets.token_urlsafe(6)
    ok = await menu.lasting_token(svc, uid, cid, ok_action, {**spec, "card": card_id}, ttl)
    no = await menu.lasting_token(svc, uid, cid, no_action, {"card": card_id}, ttl)
    toks, rows = [ok, no], [[InlineKeyboardButton(ok_label, callback_data=f"m:k:{ok}"),
                             InlineKeyboardButton(no_label, callback_data=f"m:k:{no}")]]
    if tool in LOW_RISK and tool not in NEVER:
        day = await menu.lasting_token(svc, uid, cid, ok_action + DAY, {**spec, "card": card_id, "day": 1}, ttl)
        toks.append(day)
        rows.append([InlineKeyboardButton(DAY_LABEL, callback_data=f"m:k:{day}")])
    now = time.time()

    def run(c):
        c.execute("DELETE FROM ai_cards WHERE created<?", (now - CARD_KEEP,))
        c.execute("INSERT INTO ai_cards(card, chat_id, user_id, tool, toks, created) VALUES(?,?,?,?,?,?)",
                  (card_id, cid, uid, tool, json.dumps(toks), now))
    await svc.db.atomic(run)
    return InlineKeyboardMarkup(rows)


async def claim(svc, spec, status: str) -> bool:
    """카드의 버튼 하나를 처음 누른 쪽만 True (나머지 토큰은 지움). 카드 묶음이 없는 예전 토큰은 True (토큰 1회용만)."""
    card_id = spec.get("card") if isinstance(spec, dict) else None
    if not card_id:
        return True

    def run(c):
        if not c.execute("UPDATE ai_cards SET status=? WHERE card=? AND status IS NULL", (status, card_id)).rowcount:
            return None
        toks = json.loads(c.execute("SELECT toks FROM ai_cards WHERE card=?", (card_id,)).fetchone()[0])
        c.executemany("DELETE FROM menu_tokens WHERE tok=?", [(t,) for t in toks])
        return toks
    toks = await svc.db.atomic(run)
    if toks is None:
        return False
    for t in toks:
        svc.menu_tokens.pop(t, None)
    return True


async def record(svc, chat_id: int, user_id: int | None, tool: str, text: str) -> None:
    """누른 결과 한 줄 (AI 맥락용). 실패해도 카드 흐름은 그대로."""
    now = _now()

    def run(c):
        c.execute("DELETE FROM ai_card_log WHERE ts<?", (now - LOG_KEEP,))
        c.execute("INSERT INTO ai_card_log(chat_id, user_id, tool, text, ts) VALUES(?,?,?,?,?)",
                  (chat_id, user_id, tool, " ".join(text.split())[:LINE_CHARS], now))
    try:
        await svc.db.atomic(run)
    except Exception:   # 디스크 가득 등
        log.warning("card outcome record failed", exc_info=True)


async def recent_lines(svc, chat_id: int) -> list[str]:
    """이 대화에서 최근 LOG_WINDOW 안에 눌린 카드 결과 (오래된 것부터, 최대 LOG_LINES 줄)."""
    rows = await svc.db._all("SELECT text, ts FROM ai_card_log WHERE chat_id=? AND ts>=? ORDER BY id DESC LIMIT ?",
                             (chat_id, _now() - LOG_WINDOW, LOG_LINES))
    tz = svc.cfg.tz
    return [f"[{datetime.fromtimestamp(r['ts'], tz):%H:%M}] {r['text']}" for r in reversed(rows)]


async def presser_name(svc, uid: int) -> str:
    return (await svc.db.first_name(uid) or str(uid))[:20]


async def approve_day(svc, chat_id: int, uid: int, tool: str) -> bool:
    """오늘(한국 시각 자정까지) 같은 사람·같은 도구는 확인 카드 생략. 제재·목록 밖 도구는 거절 (False)."""
    if tool not in LOW_RISK or tool in NEVER:
        return False
    until = day_start(svc.cfg.tz, -1)   # 내일 0시
    await svc.db._write("INSERT OR REPLACE INTO ai_approvals(chat_id, user_id, tool, until) VALUES(?,?,?,?)",
                        (chat_id, uid, tool, until))
    await svc.db.audit(chat_id, uid, None, "approval_day", f"{LOW_RISK[tool]} 오늘은 확인 생략")
    return True


async def approved(svc, chat_id: int, uid: int, tool: str) -> bool:
    if tool not in LOW_RISK or tool in NEVER:   # 제재는 어떤 경우에도 카드
        return False
    row = await svc.db._one("SELECT until FROM ai_approvals WHERE chat_id=? AND user_id=? AND tool=?", (chat_id, uid, tool))
    return bool(row and row["until"] > _now())


async def skip_card(svc, bot, chat_id: int, uid: int, tool: str) -> bool:
    """이번 요청을 카드 없이 바로 해도 되는지: 오늘 확인 생략 + 지금도 그 방 관리자 (텔레그램에 새로 확인)."""
    if not await approved(svc, chat_id, uid, tool):
        return False
    from . import menu
    if not await menu._allowed(svc, bot, chat_id, uid, menu.ADMIN, fresh=True):
        return False
    await svc.db.audit(chat_id, uid, None, "approval_skip", LOW_RISK[tool])
    return True


async def pressed(svc, cid: int, uid: int, tool: str, spec, text: str, *, done: bool) -> None:
    """토큰 카드를 누른 결과 기록 ('✅ 예약 #3 저장' + 누른 사람) + [✅ + 오늘은 확인 생략] 으로 실행됐으면 승인 저장."""
    await record(svc, cid, uid, tool, f"{text} ({await presser_name(svc, uid)})")
    if done and isinstance(spec, dict) and spec.get("day"):
        await approve_day(svc, cid, uid, tool)


def day_note(spec) -> str:
    return "\n(오늘은 같은 요청이면 확인 없이 바로 해요)" if isinstance(spec, dict) and spec.get("day") else ""
