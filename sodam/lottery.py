"""📅 출석 → 🎟 복권 → 🎁 경품 (설계 docs/SPORTS_ENGAGE_DESIGN.md §3 D, 2026-10-11 FOX 고객 '.출석하면 출석되고 복권 지급 … 긁어서 당첨되면
커피 기프티콘·배민쿠폰 1만원 … 확률 1프로 … 일주일에 2명').

인과: 출석 = 매일 들어올 이유, 복권 = 불확실한 보상(같은 보상보다 오래감), 주간 상한 = 방장 경품 비용 상한, 자격 조건 = 부계정 쓸어가기 방지.
- 돈·포인트 걸기 없음(무료 참여). 경품은 **방장이 직접** 줌 — 소담은 당첨 기록·방 축하·관리자 1:1 알림·[✅ 지급 완료] 만.
- `.출석` = 하루 1번(한국 날짜) 출석 + 연속 일수. 복권 켠 방이면 복권 1장(7일 안 긁기).
- `.복권` / [🎟 긁기] = 내 복권 1장 긁기: 당첨 = secrets 난수 < 확률(방 설정 lotto_odds %), **이번 주(한국 월요일~) 당첨 수가 상한 미만일 때만** —
  당첨 줄을 넣는 SQL 한 문장이 상한을 다시 셈(동시에 긁어도 상한 초과 X). 상한이 찼으면 솔직히 '이번 주 경품 소진'.
- 자격: 이 방에 들어온 지 lotto_min_days 일↑ (입장 기록 없으면 처음 본 날) + 최근 7일 이 방에서 글 1개↑ (출석만 하러 오는 부계정 방지).
- 관리자·봇도 출석은 되지만 복권은 관리자 제외(lotto_admins=False 기본 — 방장이 자기 경품 타는 것 방지).
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta

from .db import register_schema
from .settings import register_setting

log = logging.getLogger(__name__)

TICKET_DAYS = 7
DAY_SEC = 86400
WIN_GAP_DAYS = 28        # 같은 사람은 4주에 한 번만 당첨 (조사: 소수가 경품을 쓸어가면 나머지가 떠남)

def _odds(raw: str) -> float:
    try:
        v = float(raw.replace("%", "").replace("프로", "").strip())
    except ValueError:
        raise ValueError("확률은 숫자로 (예: 1 = 1%)") from None
    if not 0.1 <= v <= 50:
        raise ValueError("확률은 0.1 ~ 50 % 사이로")
    return round(v, 2)


def _prize(raw: str) -> str:
    import re
    v = " ".join((raw or "").split())[:40]
    if not v or re.search(r"https?://|t\.me/|@\w", v, re.I):
        raise ValueError("경품은 40자 안 글로만 (링크·@아이디 X)")
    return v


register_setting("lotto_enabled", False, "출석 복권")
register_setting("lotto_odds", 1.0, "복권 당첨 확률(%)", validator=_odds, render_fn=lambda v: f"{v:g}%")
register_setting("lotto_week_max", 2, "복권 주간 당첨 수", range_=(0, 50))
register_setting("lotto_prize", "커피 기프티콘", "복권 경품", validator=_prize)
register_setting("lotto_min_days", 3, "복권 자격: 들어온 지 며칠", range_=(0, 30))

register_schema("""
CREATE TABLE IF NOT EXISTS attend (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    streak  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS lotto_tickets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    created   INTEGER NOT NULL,
    scratched INTEGER,
    result    TEXT                         -- win / lose / soldout
);
CREATE INDEX IF NOT EXISTS lotto_tickets_user ON lotto_tickets(chat_id, user_id, scratched);
CREATE TABLE IF NOT EXISTS lotto_wins (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    ticket   INTEGER NOT NULL,
    prize    TEXT NOT NULL,
    week     INTEGER NOT NULL,             -- 그 주 월요일 0시 (한국)
    created  INTEGER NOT NULL,
    paid     INTEGER NOT NULL DEFAULT 0,
    paid_by  INTEGER
);
CREATE INDEX IF NOT EXISTS lotto_wins_week ON lotto_wins(chat_id, week);
""", migrate={"attend": "composite", "lotto_tickets": "plain", "lotto_wins": "plain"})


def kst_day(tz, now: float) -> str:
    return datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")


def week_start(tz, now: float) -> int:
    d = datetime.fromtimestamp(now, tz)
    return int((d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


async def checkin(db, tz, chat_id: int, user_id: int, now: float | None = None, give_ticket: bool = False) -> dict:
    """{'ok': 처음이면 True, 'streak': 연속 일수, 'month': 이번 달 출석 수, 'ticket': 새 복권 id 또는 None}."""
    now = time.time() if now is None else now
    day = kst_day(tz, now)
    yday = (datetime.fromtimestamp(now, tz).date() - timedelta(days=1)).isoformat()   # 날짜로 하루 빼기 (서머타임 있는 시간대도 안전)
    month = day[:7]

    def run(c):
        prev = c.execute("SELECT streak FROM attend WHERE chat_id=? AND user_id=? AND day=?", (chat_id, user_id, yday)).fetchone()
        streak = (prev[0] + 1) if prev else 1
        n = c.execute("INSERT OR IGNORE INTO attend(chat_id, user_id, day, ts, streak) VALUES(?,?,?,?,?)",
                      (chat_id, user_id, day, int(now), streak)).rowcount
        ticket = None
        if n and give_ticket:
            ticket = c.execute("INSERT INTO lotto_tickets(chat_id, user_id, created) VALUES(?,?,?)",
                               (chat_id, user_id, int(now))).lastrowid
        cur = c.execute("SELECT streak FROM attend WHERE chat_id=? AND user_id=? AND day=?", (chat_id, user_id, day)).fetchone()[0]
        m = c.execute("SELECT COUNT(*) FROM attend WHERE chat_id=? AND user_id=? AND day LIKE ?",
                      (chat_id, user_id, month + "%")).fetchone()[0]
        return {"ok": bool(n), "streak": cur, "month": m, "ticket": ticket}
    return await db.atomic(run)


async def eligible(db, chat_id: int, user_id: int, min_days: int, now: float | None = None) -> str | None:
    """복권 자격이 없으면 이유 글, 있으면 None."""
    now = time.time() if now is None else now
    m = await db._one("SELECT joined_at FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    first = await db._one("SELECT MIN(ts) t FROM messages WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    since = (m["joined_at"] if m and m["joined_at"] else None) or (first["t"] if first and first["t"] else None)
    if min_days > 0 and (since is None or now - since < min_days * DAY_SEC):
        return f"복권은 이 방에 들어온 지 {min_days}일 지난 분부터 받을 수 있어요 (출석은 됐어요)."
    # 명령·소담 부르기는 대화로 안 셈 — 글은 명령보다 먼저 기록돼서 '.출석' 한 줄이 이 조건을 혼자 채웠음 (리뷰 2026-10-11)
    recent = await db._one("SELECT 1 FROM messages WHERE chat_id=? AND user_id=? AND ts>=? AND is_bot=0 "
                           "AND substr(ltrim(text),1,1) NOT IN ('.','!','/','。') AND text NOT LIKE '%소담%' "
                           "AND length(trim(text))>=2 LIMIT 1",
                           (chat_id, user_id, int(now) - 7 * DAY_SEC))
    if recent is None:
        return "복권은 최근 7일 안에 이 방에서 대화한 분만 받을 수 있어요 (출석은 됐어요)."
    won = await db._one("SELECT 1 FROM lotto_wins WHERE chat_id=? AND user_id=? AND created>=? LIMIT 1",
                        (chat_id, user_id, int(now) - WIN_GAP_DAYS * DAY_SEC))
    if won is not None:
        return f"최근 {WIN_GAP_DAYS // 7}주 안에 당첨되셔서 이번엔 다른 분 차례예요 (출석은 됐어요)."
    return None


async def open_tickets(db, chat_id: int, user_id: int, now: float | None = None) -> int:
    now = time.time() if now is None else now
    r = await db._one("SELECT COUNT(*) n FROM lotto_tickets WHERE chat_id=? AND user_id=? AND scratched IS NULL AND created>=?",
                      (chat_id, user_id, int(now) - TICKET_DAYS * DAY_SEC))
    return r["n"] if r else 0


async def scratch(db, tz, chat_id: int, user_id: int, odds_pct: float, week_max: int, prize: str,
                  now: float | None = None, roll: int | None = None) -> dict:
    """내 복권 1장 긁기 → {'result': win/lose/soldout/recent/none, 'left': 남은 복권, 'week_left': 이번 주 남은 경품, 'win_id'}."""
    now = time.time() if now is None else now
    ws = week_start(tz, now)
    roll = secrets.randbelow(10_000) if roll is None else roll          # 0~9999, 확률 1% = 100 미만
    hit = roll < int(round(max(0.0, min(100.0, float(odds_pct))) * 100))

    def run(c):
        t = c.execute("SELECT id FROM lotto_tickets WHERE chat_id=? AND user_id=? AND scratched IS NULL AND created>=? "
                      "ORDER BY id LIMIT 1", (chat_id, user_id, int(now) - TICKET_DAYS * DAY_SEC)).fetchone()
        if t is None:
            return {"result": "none", "left": 0, "week_left": None, "win_id": None}
        result, win_id = "lose", None
        recent = c.execute("SELECT 1 FROM lotto_wins WHERE chat_id=? AND user_id=? AND created>=? LIMIT 1",
                           (chat_id, user_id, int(now) - WIN_GAP_DAYS * DAY_SEC)).fetchone()
        if hit and recent:
            result = "recent"          # 받아 둔 복권이 당첨 뒤에 남아 있던 경우 — 솔직히 말함
        elif hit:
            cur = c.execute(   # 이번 주 당첨 수가 상한 미만일 때만 넣음 (한 문장 — 동시에 긁어도 상한 초과 X)
                "INSERT INTO lotto_wins(chat_id, user_id, ticket, prize, week, created) "
                "SELECT ?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM lotto_wins WHERE chat_id=? AND week=?) < ?",
                (chat_id, user_id, t[0], prize, ws, int(now), chat_id, ws, int(week_max)))
            if cur.rowcount:
                result, win_id = "win", cur.lastrowid
            else:
                result = "soldout"
        c.execute("UPDATE lotto_tickets SET scratched=?, result=? WHERE id=?", (int(now), result, t[0]))
        left = c.execute("SELECT COUNT(*) FROM lotto_tickets WHERE chat_id=? AND user_id=? AND scratched IS NULL AND created>=?",
                         (chat_id, user_id, int(now) - TICKET_DAYS * DAY_SEC)).fetchone()[0]
        used = c.execute("SELECT COUNT(*) FROM lotto_wins WHERE chat_id=? AND week=?", (chat_id, ws)).fetchone()[0]
        return {"result": result, "left": left, "week_left": max(0, int(week_max) - used), "win_id": win_id}
    return await db.atomic(run)


async def week_left(db, tz, chat_id: int, week_max: int, now: float | None = None) -> int:
    now = time.time() if now is None else now
    r = await db._one("SELECT COUNT(*) n FROM lotto_wins WHERE chat_id=? AND week=?", (chat_id, week_start(tz, now)))
    return max(0, int(week_max) - (r["n"] if r else 0))


async def wins(db, chat_id: int, limit: int = 10) -> list:
    return await db._all("SELECT w.*, COALESCE(u.first_name, '?') AS name FROM lotto_wins w LEFT JOIN users u ON u.user_id=w.user_id "
                         "WHERE w.chat_id=? ORDER BY w.id DESC LIMIT ?", (chat_id, limit))


async def mark_paid(db, chat_id: int, win_id: int, by: int) -> bool:
    return await db.atomic(lambda c: c.execute("UPDATE lotto_wins SET paid=1, paid_by=? WHERE id=? AND chat_id=? AND paid=0",
                                               (by, win_id, chat_id)).rowcount) > 0


async def month_rank(db, tz, chat_id: int, now: float | None = None, limit: int = 10) -> list:
    now = time.time() if now is None else now
    month = kst_day(tz, now)[:7]
    return await db._all("SELECT a.user_id, COALESCE(u.first_name,'?') AS name, COUNT(*) AS n, MAX(a.streak) AS best "
                         "FROM attend a LEFT JOIN users u ON u.user_id=a.user_id WHERE a.chat_id=? AND a.day LIKE ? "
                         "GROUP BY a.user_id ORDER BY n DESC, best DESC LIMIT ?", (chat_id, month + "%", limit))


async def prune(db, now: float) -> None:
    await db._write("DELETE FROM lotto_tickets WHERE created < ?", (int(now) - 60 * DAY_SEC,))
    await db._write("DELETE FROM attend WHERE ts < ?", (int(now) - 400 * DAY_SEC,))
