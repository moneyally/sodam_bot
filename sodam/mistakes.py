"""🧠 오늘 소담 실수 — 오너 1:1 로 하루 한 번 (오전 9시, 어제 하루). AI 호출 0원, 판정은 whyfail.analyze.

오너 결정 2026-10-03: '어디서 꼬였는지 / 왜' 만 쉬운 말로. 자세한 건 Claude 가 점검 창구 `why run=<번호>` 로 본다.
실수가 하나도 없으면 안 보냄. 날짜 claim → 재시작해도 하루 한 번.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from . import hooks, persist
from .whyfail import COMPLAINT_SEC, REDO_SEC, Finding, analyze

log = logging.getLogger(__name__)

HOUR = 9
SHOW = 5
# 먼저 보여 줄 순서 (거짓 주장·다시 요청·불만이 제일 중요)
WEIGHT = {"claim": 9, "redo": 8, "complaint": 7, "crash": 6, "error": 5, "cap": 4, "soft": 3, "off": 2, "empty": 1}


async def _context(db, row) -> tuple[list, list[str]]:
    later = await db._all("SELECT id, ts, trigger FROM agent_runs WHERE chat_id=? AND user_id=? AND id>? AND ts<=? "
                          "ORDER BY id LIMIT 5", (row["chat_id"], row["user_id"], row["id"], row["ts"] + REDO_SEC))
    replies = [r["text"] for r in await db._all(
        "SELECT text FROM messages WHERE chat_id=? AND user_id=? AND is_bot=0 AND ts>? AND ts<=? AND "
        "(text LIKE '%소담%' OR reply_to_user IN (SELECT DISTINCT user_id FROM messages WHERE chat_id=? AND is_bot=1)) "
        "ORDER BY id LIMIT 10", (row["chat_id"], row["user_id"], row["ts"], row["ts"] + COMPLAINT_SEC, row["chat_id"]))]
    return later, replies


async def collect(db, since: int, until: int) -> tuple[int, list[tuple[object, list[Finding]]]]:
    rows = await db._all("SELECT * FROM agent_runs WHERE ts>=? AND ts<? ORDER BY id", (since, until))
    bad = []
    for row in rows:
        later, replies = await _context(db, row)
        found = analyze(row, later, replies)
        if found:
            bad.append((row, found))
    bad.sort(key=lambda x: -max(WEIGHT.get(f.code, 0) for f in x[1]))
    return len(rows), bad


async def report(db, since: int, until: int, label: str) -> str | None:
    total, bad = await collect(db, since, until)
    if not bad:
        return None
    titles = {}
    lines = [f"🧠 <b>{label} 소담 실수 {len(bad)}건</b> (AI 답 {total}번 중)"]
    for i, (row, found) in enumerate(bad[:SHOW], 1):
        cid = row["chat_id"]
        if cid not in titles:
            t = await db._one("SELECT title FROM chats WHERE chat_id=?", (cid,))
            titles[cid] = (t["title"] if t and t["title"] else ("1:1" if cid > 0 else str(cid)))[:20]
        top = max(found, key=lambda f: WEIGHT.get(f.code, 0))
        lines.append(f"\n{i}. {html.escape(titles[cid])} · \"{html.escape((row['trigger'] or '')[:40])}\"  <code>#{row['id']}</code>\n"
                     f"   → 꼬인 곳: {top.stage}\n   → 이유: {html.escape(top.why[:90])}")
    if len(bad) > SHOW:
        lines.append(f"\n…외 {len(bad) - SHOW}건")
    lines.append("\n고칠 거 있으면 클로드한테 <code>#번호</code> 랑 같이 \"고쳐\" 라고 하면 돼요.")
    return "\n".join(lines)


async def tick(svc, bot) -> None:
    now = datetime.now(svc.cfg.tz)
    if now.hour < HOUR:
        return
    if not await persist.claim(svc.db, f"mistakes:{now:%Y-%m-%d}", 36 * 3600):
        return
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    try:
        text = await report(svc.db, int(yesterday.timestamp()), int(today.timestamp()), f"어제({yesterday:%m/%d})")
    except Exception:
        log.exception("mistakes report failed")
        return
    if not text:
        return
    for uid in await svc.perms.owners():
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:   # 1:1 막힘 등
            log.info("mistakes owner dm %s: %s", uid, e)


hooks.add_tick_hook(tick)
