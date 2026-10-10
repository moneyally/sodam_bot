"""🎯 승부 맞히기 (2026-10-10 고객 '후바오 스포츠봇처럼 — 유저 추천픽·랭킹·나의 통계'). 돈·포인트 걸기·배당 없음, 맞히면 점수만.

- 고르기: 시작 전 경기만 (서버 시각 < 시작 시각), 시작 전엔 바꿀 수 있음, 한 사람 하루 DAILY_MAX 경기.
- 정산(settle, 1분마다 tick 훅): 시작한 미정산 경기 → 그 리그·날짜 일정(캐시 우선) → 끝났으면 맞힘 WIN(무승부 맞힘 DRAW_WIN) / 틀림 0 /
  취소·연기 = 무효. 이길 팀 = 점수 높은 쪽 (같으면 draw).
- 통계: 고른 수·맞힌 수·승률·누적 점수·이번 주(한국 월요일 0시부터) 점수·순위·지금 연속 맞힘. 랭킹 = 전체 또는 방 멤버만.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from ..db import register_schema
from .leagues import LEAGUES
from .providers import BACKGROUND, KST, SportsError

log = logging.getLogger(__name__)

WIN = 10
DRAW_WIN = 15
DAILY_MAX = 50
SETTLE_EVERY = 60
GIVE_UP = 3 * 86400          # 시작 뒤 이만큼 지나도 결과를 못 보면 무효
SIDES = {"h": "home", "a": "away", "d": "draw"}
DRAW_SPORTS = {"soccer", "rugby"}   # 무승부가 있는 종목 (하키·농구·야구는 연장으로 끝냄)

register_schema("""
CREATE TABLE IF NOT EXISTS sports_picks (
    user_id   INTEGER NOT NULL,
    game      TEXT NOT NULL,
    league    TEXT NOT NULL,
    pick      TEXT NOT NULL,              -- home / away / draw
    home      TEXT NOT NULL,
    away      TEXT NOT NULL,
    start     INTEGER NOT NULL,
    chat_id   INTEGER NOT NULL DEFAULT 0, -- 어디서 골랐나 (방 랭킹은 멤버로 셈)
    created   INTEGER NOT NULL,
    result    TEXT,                       -- NULL=아직 / win / lose / void
    points    INTEGER NOT NULL DEFAULT 0,
    settled   INTEGER,
    PRIMARY KEY (user_id, game)
);
CREATE INDEX IF NOT EXISTS sports_picks_open ON sports_picks(result, start);
CREATE INDEX IF NOT EXISTS sports_picks_game ON sports_picks(game);
""")


def draw_ok(league: str) -> bool:
    lg = LEAGUES.get(league)
    return bool(lg and lg.sport in DRAW_SPORTS)


def week_start(now: float) -> int:
    d = datetime.fromtimestamp(now, KST)
    monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp())


async def pick(db, user_id: int, g, side: str, chat_id: int = 0, now: float | None = None) -> str:
    """'ok' / 'changed' / 'started' / 'nodraw' / 'full' / 'bad'."""
    now = time.time() if now is None else now
    side = SIDES.get(side, side)
    if side not in ("home", "away", "draw"):
        return "bad"
    if side == "draw" and not draw_ok(g.league):
        return "nodraw"
    if g.state != "pre" or now >= g.start:
        return "started"
    day0 = int(datetime.fromtimestamp(now, KST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def run(c):
        old = c.execute("SELECT pick, result FROM sports_picks WHERE user_id=? AND game=?", (user_id, g.key)).fetchone()
        if old and old[1] in ("win", "lose"):
            return "started"
        if old:   # 아직 결과 전 · 또는 연기돼 무효였는데 같은 경기가 다시 잡힘 → 새 시각으로 다시 (리뷰 2026-10-10)
            c.execute("UPDATE sports_picks SET pick=?, created=?, start=?, result=NULL, points=0, settled=NULL "
                      "WHERE user_id=? AND game=?", (side, int(now), int(g.start), user_id, g.key))
            return "changed" if old[0] != side or old[1] == "void" else "ok"
        if c.execute("SELECT COUNT(*) FROM sports_picks WHERE user_id=? AND created>=?", (user_id, day0)).fetchone()[0] >= DAILY_MAX:
            return "full"
        c.execute("INSERT INTO sports_picks(user_id, game, league, pick, home, away, start, chat_id, created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (user_id, g.key, g.league, side, g.home, g.away, int(g.start), chat_id, int(now)))
        return "ok"
    return await db.atomic(run)


async def mine(db, user_id: int, game: str) -> str | None:
    row = await db._one("SELECT pick FROM sports_picks WHERE user_id=? AND game=?", (user_id, game))
    return row["pick"] if row else None


async def split(db, game: str) -> dict:
    """이 경기를 고른 사람 비율 {'home': n, 'away': n, 'draw': n}."""
    rows = await db._all("SELECT pick, COUNT(*) n FROM sports_picks WHERE game=? GROUP BY pick", (game,))
    return {r["pick"]: r["n"] for r in rows}


def winner(g) -> str | None:
    if g.state in ("cancel", "postponed"):
        return "void"
    if g.state != "post" or g.home_score is None or g.away_score is None:
        return None
    if g.home_score == g.away_score:
        return "draw"
    return "home" if g.home_score > g.away_score else "away"


async def settle(db, feed, now: float | None = None) -> int:
    """시작한 미정산 경기를 결과대로 점수 매김. 정산한 고르기 수."""
    now = time.time() if now is None else now
    rows = await db._all("SELECT DISTINCT game, league, start FROM sports_picks WHERE result IS NULL AND start <= ? LIMIT 200",
                         (int(now),))
    done = 0
    for r in rows:
        lg = LEAGUES.get(r["league"])
        res = None
        if lg is not None and feed.available(lg):
            d = datetime.fromtimestamp(r["start"], KST).date()
            games = feed.cached_day(lg, d)
            found = next((g for g in games or [] if g.key == r["game"]), None)
            if found is None or winner(found) is None:
                bg = BACKGROUND.set(True)          # 자동 정산 = 명령용 예비 한도는 안 씀 (API-Sports)
                try:
                    games = await feed.day(lg, d, max_age=300)
                except SportsError:
                    games = []
                finally:
                    BACKGROUND.reset(bg)
                found = next((g for g in games if g.key == r["game"]), None)
            res = winner(found) if found else None
        if res is None and now - r["start"] > GIVE_UP:
            res = "void"
        if res is None:
            continue
        done += await db.atomic(lambda c, r=r, res=res: _apply(c, r["game"], res, int(now)))
    return done


def _apply(c, game: str, res: str, now: int) -> int:
    if res == "void":
        return c.execute("UPDATE sports_picks SET result='void', points=0, settled=? WHERE game=? AND result IS NULL",
                         (now, game)).rowcount
    n = c.execute("UPDATE sports_picks SET result='win', points=?, settled=? WHERE game=? AND result IS NULL AND pick=?",
                  (DRAW_WIN if res == "draw" else WIN, now, game, res)).rowcount
    return n + c.execute("UPDATE sports_picks SET result='lose', points=0, settled=? WHERE game=? AND result IS NULL",
                         (now, game)).rowcount


async def stats(db, user_id: int, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ws = week_start(now)
    r = await db._one("SELECT COUNT(*) n, SUM(result='win') w, SUM(result IN ('win','lose')) s, COALESCE(SUM(points),0) p, "
                      "COALESCE(SUM(CASE WHEN settled>=? THEN points END),0) wp, SUM(result IS NULL) open "
                      "FROM sports_picks WHERE user_id=?", (ws, user_id))
    seq = await db._all("SELECT result FROM sports_picks WHERE user_id=? AND result IN ('win','lose') ORDER BY settled DESC, start DESC LIMIT 50",
                        (user_id,))
    streak = 0
    for x in seq:
        if x["result"] != "win":
            break
        streak += 1
    rank = await db._one("SELECT COUNT(*)+1 r FROM (SELECT user_id, SUM(points) p FROM sports_picks WHERE settled>=? GROUP BY user_id) "
                         "WHERE p > ?", (ws, r["wp"] or 0))
    s = r["s"] or 0
    return {"picks": r["n"] or 0, "settled": s, "wins": r["w"] or 0, "rate": round(100 * (r["w"] or 0) / s) if s else 0,
            "points": r["p"] or 0, "week": r["wp"] or 0, "week_rank": rank["r"] if (r["wp"] or 0) > 0 else None,
            "open": r["open"] or 0, "streak": streak}


async def ranking(db, *, week: bool = True, chat_id: int | None = None, limit: int = 10, now: float | None = None) -> list:
    """[(user_id, 이름, 점수, 맞힘, 고름)] — chat_id 를 주면 그 방 멤버(나간 사람 빼고)만."""
    now = time.time() if now is None else now
    where, args = ["result IN ('win','lose')"], []
    if week:
        where.append("p.settled >= ?")
        args.append(week_start(now))
    join = ""
    if chat_id is not None:
        join = ("JOIN members m ON m.user_id=p.user_id AND m.chat_id=? "
                "AND NOT EXISTS (SELECT 1 FROM member_left l WHERE l.chat_id=m.chat_id AND l.user_id=m.user_id "
                "AND l.ts >= COALESCE(m.last_seen, m.joined_at, 0))")
        args.insert(0, chat_id)
    rows = await db._all(
        f"SELECT p.user_id, COALESCE(u.first_name,'?') name, SUM(p.points) pts, SUM(p.result='win') w, COUNT(*) n "
        f"FROM sports_picks p {join} LEFT JOIN users u ON u.user_id=p.user_id WHERE {' AND '.join(where)} "
        f"GROUP BY p.user_id HAVING pts > 0 ORDER BY pts DESC, w DESC LIMIT ?", (*args, limit))
    return [(r["user_id"], r["name"], r["pts"], r["w"], r["n"]) for r in rows]


async def prune(db, now: float) -> None:
    """1년 지난 정산 기록 정리 (통계용으로 넉넉히)."""
    await db._write("DELETE FROM sports_picks WHERE result IS NOT NULL AND settled < ?", (int(now) - 365 * 86400,))
