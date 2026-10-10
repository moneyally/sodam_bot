"""자동 경기 알림: 구독(리그·팀) → 공유 폴링 → 경기별 스냅샷 비교 → 방마다 묶어서 한 통.

설계 (조사 보고서 2026-09-29):
- 받는 단위는 **리그** 하나 (방마다 따로 부르지 않음). 30초 틱이 리그마다 '지금 받을 때인지'만 판단:
  경기 [시작 15분 전, 끝]에 걸린 리그 = 60초마다, 아니면 일정만 6시간마다(하루 몇 번). 진행 중엔 그 경기가 있는 요청만 다시.
- 경기마다 마지막 스냅샷 (상태, 점수, 득점 수) → 달라진 것만 이벤트: 시작 · 골(축구·하키, ESPN 이면 득점자) · 득점 취소 ·
  점수 변화(야구·농구·배구는 '점수까지' 고른 방만) · 종료 · 취소/연기/중단.
- 스냅샷은 메모리 → 재시작 뒤 처음 본 경기는 **조용히 저장만** (재시작 알림 폭탄 없음. 대신 꺼져 있던 동안 끝난 경기는 못 알림).
- 중복 방지 = sports_alert_sent(방, 경기, 종류, sent_at) 먼저 차지 → 14일 지나면 지움.
- 방마다: 한 틱의 알림은 한 메시지로 묶음 · 시간당 MAX_PER_HOUR 통 · 조용한 시간(기본 01~07시 KST)엔 시작·골 버림,
  종료·취소는 모아 두었다가(sports_held, DB) 조용한 시간이 끝나면 '밤사이 경기 결과' 한 통.
- 이용 중인 방(구독·체험) + sports_enabled 인 방만.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from telegram import Bot
from telegram.error import Forbidden, TelegramError

from ..db import DB, register_schema
from ..settings import register_setting
from ..util import esc
from . import fmt
from .feed import Feed
from .leagues import GOAL_SPORTS, LEAGUES, ko_name, same_team
from .providers import BACKGROUND, KST, Game, SportsError

log = logging.getLogger(__name__)

LIVE_EVERY = 8             # 경기 중인 리그 다시 받는 간격(초) — ESPN 이 7초마다 새로 냄(cache-control max-age=7, 서버 실측 2026-10-10).
                           # 예전 60초 = 골 알림이 평균 30초↑ 늦음 ('라이브스코어보다 빠르게' 오너 요청)
SCHEDULE_EVERY = 6 * 3600  # 경기 없을 때 일정 새로 받는 간격
PRE_WINDOW = 15 * 60       # 시작 15분 전부터 '경기 중' 취급
MAX_GAME_LEN = 6 * 3600    # 이보다 오래 '시작 전/진행 중'이면 멈춘 데이터로 보고 60초 폴링 안 함 (ESPN 이 SCHEDULED 로 남는 사례)
MAX_PER_HOUR = 12          # 방마다 시간당 알림 메시지
DM_PER_HOUR = 30           # 개인 1:1 알림 (본인이 고른 것이라 넉넉히)
MAX_WATCHES = 10           # 한 사람(또는 한 방)의 콕 집은 경기·개인 구독 수
FAIL_PAUSE = 60            # 보내기 실패한 곳은 이만큼 쉼 (틱이 몇 초라 막힌 방을 계속 두드리지 않게)
WATCH_KEEP = 12 * 3600     # 경기 하나 알림은 시작 뒤 이 시간 지나면 자동으로 지움
KEEP_DAYS = 14
MAX_FOLLOWS = 20
MAX_LINES = 40             # 한 메시지 줄 수 (넘치는 결과는 다음 틱으로)
RETRY_EVERY = 300          # 일정 받기 실패 뒤 다시 시도 간격
EURO_LEAGUES = {"epl", "laliga", "seriea", "bundesliga", "ligue1"}


def cups_for(league: str) -> tuple[str, ...]:
    """팀 구독이 같이 보는 대회."""
    return tuple(c for c in ("ucl", "uel") if c in LEAGUES) if league in EURO_LEAGUES else ()

register_schema("""
CREATE TABLE IF NOT EXISTS sports_follow (
    chat_id  INTEGER NOT NULL,
    league   TEXT NOT NULL,
    team     TEXT NOT NULL DEFAULT '',
    label    TEXT NOT NULL,
    added_by INTEGER,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, league, team)
);
CREATE TABLE IF NOT EXISTS sports_alert_sent (
    chat_id INTEGER NOT NULL,
    game    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, game, kind)
);
CREATE INDEX IF NOT EXISTS sports_alert_sent_at ON sports_alert_sent(sent_at);
CREATE TABLE IF NOT EXISTS sports_watch (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,          -- 받을 곳: 방(음수) 또는 사람 1:1(양수)
    user_id  INTEGER NOT NULL,          -- 건 사람
    league   TEXT NOT NULL,
    team     TEXT NOT NULL DEFAULT '',  -- 팀 소스 이름 ('' = 리그 전체)
    game     TEXT NOT NULL DEFAULT '',  -- 경기 key (콕 집은 경기 하나, '' = 팀·리그 계속)
    label    TEXT NOT NULL,
    level    TEXT NOT NULL DEFAULT 'goals',
    created  INTEGER NOT NULL,
    expires  INTEGER NOT NULL DEFAULT 0 -- 0 = 계속
);
CREATE INDEX IF NOT EXISTS sports_watch_chat ON sports_watch(chat_id);
CREATE TABLE IF NOT EXISTS sports_held (
    chat_id INTEGER NOT NULL,
    game    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (chat_id, game, kind)
);
""", migrate={"sports_follow": "composite", "sports_alert_sent": "composite", "sports_held": "composite"})

# 알림 종류 고르기 (방 설정): 어떤 이벤트를 보낼지
LEVELS = {"final": ("🏁 결과만", {"final", "cancel"}),
          "basic": ("🔔 시작·결과", {"start", "final", "cancel"}),
          "goals": ("⚽ 골까지", {"start", "goal", "undo", "final", "cancel"}),
          "all": ("📈 점수 변화까지", {"start", "goal", "undo", "score", "final", "cancel"})}
QUIET = {"off": "없음", "00-07": "0~7시", "01-07": "1~7시", "01-08": "1~8시", "02-09": "2~9시", "23-07": "23~7시"}
HOLD_KINDS = {"final", "cancel"}   # 조용한 시간에도 버리지 않고 아침에 모아 보냄

register_setting("sports_alerts", "goals", "스포츠 알림 종류",
                 choices={"final": "final", "결과": "final", "결과만": "final", "basic": "basic", "시작": "basic",
                          "시작결과": "basic", "goals": "goals", "골": "goals", "골까지": "goals", "all": "all",
                          "점수": "all", "전부": "all"},
                 choice_labels={k: v[0] for k, v in LEVELS.items()})
register_setting("sports_quiet", "01-07", "스포츠 알림 조용한 시간(한국)",
                 choices={**{k: k for k in QUIET}, "끔": "off", "없음": "off"},
                 choice_labels=QUIET)


def in_quiet(value: str, hour: int) -> bool:
    if not value or value == "off" or "-" not in value:
        return False
    a, b = (int(x) for x in value.split("-", 1))
    return a <= hour < b if a < b else (hour >= a or hour < b)


@dataclass
class Snap:
    state: str
    hs: int | None
    as_: int | None
    ngoals: int
    gen: int = 0                      # 득점 취소가 확정될 때마다 +1 → 같은 점수로 다시 넣은 골도 알림 (감사 2026-09-30)
    drop: tuple | None = None         # 점수가 줄어든 걸 한 번 봄 (두 번 연속이어야 취소 확정 — ESPN 이 한 틱 틀리게 줄 때 가짜 취소 X)


def next_snap(prev: Snap | None, g: Game) -> Snap:
    """다음 스냅샷. 점수가 비어 온 틱은 이전 점수 유지, 점수가 줄면 한 번은 보류(drop) 후 두 번째에 확정(gen+1)."""
    if prev is None:
        return Snap(g.state, g.home_score, g.away_score, len(g.goals))
    if not g.scored or g.home_score is None or g.away_score is None:
        return Snap(g.state, prev.hs, prev.as_, max(prev.ngoals, len(g.goals)), prev.gen, prev.drop)
    cur = (g.home_score, g.away_score)
    if prev.hs is not None and prev.as_ is not None and sum(cur) < prev.hs + prev.as_:
        if prev.drop != cur:
            return Snap(g.state, prev.hs, prev.as_, prev.ngoals, prev.gen, cur)     # 보류 (아직 알림 X)
        return Snap(g.state, *cur, len(g.goals), prev.gen + 1)                       # 확정된 취소
    return Snap(g.state, *cur, len(g.goals), prev.gen)


@dataclass
class Event:
    game: Game
    kind: str       # start / goal / undo / score / final / cancel
    dedupe: str     # sports_alert_sent.kind (점수가 들어가서 같은 골은 한 번만)
    text: str


def _score(g: Game) -> str:
    return f"{g.home_score}-{g.away_score}"


def _scorers(g: Game, new: tuple) -> str:
    parts = []
    for clock, side, who, tag in new:
        team = ko_name(g.home if side == "home" else g.away, g.league) if side else ""
        who = who + (f"({tag})" if tag else "")
        parts.append(" ".join(x for x in (clock, esc(who)) if x) + (f" {esc(team)}" if team and not who else ""))
    return ", ".join(p for p in parts if p)


def diff(prev: Snap | None, g: Game) -> list[Event]:
    """이전 스냅샷 → 지금 경기. 처음 본 경기(prev None)는 조용히 (재시작·새 구독 폭탄 없음)."""
    if prev is None:
        return []
    lg = LEAGUES.get(g.league)
    sport = lg.sport if lg else ""
    em = fmt.emoji(g.league)
    tag = fmt.tag(g)
    out: list[Event] = []
    if g.state != prev.state:
        if g.state == "in" and prev.state == "pre":
            out.append(Event(g, "start", "start", f"🔔 경기 시작 {tag} {fmt.matchup(g, score=False)}"))
        elif g.state == "post":
            return out + [Event(g, "final", "final", f"🏁 경기 종료 {tag} {fmt.matchup(g)}")]
        elif g.state in fmt.STATE_KO:
            icon = {"cancel": "🚫", "postponed": "📅", "suspended": "⏸"}[g.state]
            return [Event(g, "cancel", g.state, f"{icon} 경기 {fmt.STATE_KO[g.state]} {tag} {fmt.matchup(g, score=g.state == 'suspended')}")]
    if g.state == "in" and g.scored and g.home_score is not None and g.away_score is not None \
            and prev.hs is not None and prev.as_ is not None:
        before, now = prev.hs + prev.as_, g.home_score + g.away_score
        if now < before and prev.drop != (g.home_score, g.away_score):
            return out                     # 줄어든 점수는 한 틱 더 보고 확정 (next_snap 이 보류)
        gen = prev.gen + (1 if now < before else 0)
        if now != before or (g.home_score, g.away_score) != (prev.hs, prev.as_):
            if sport in GOAL_SPORTS:
                if now > before:
                    who = _scorers(g, g.goals[prev.ngoals:]) if len(g.goals) > prev.ngoals else ""
                    out.append(Event(g, "goal", f"goal:{_score(g)}:{gen}",
                                     f"{em} 골! {tag} {fmt.matchup(g)}" + (f" · {who}" if who else "")))
                else:
                    out.append(Event(g, "undo", f"undo:{_score(g)}:{gen}", f"↩️ 득점 취소 {tag} {fmt.matchup(g)}"))
            else:
                detail = f" ({esc(g.detail)})" if g.detail else ""
                out.append(Event(g, "score", f"score:{_score(g)}:{gen}", f"{em} {tag} {fmt.matchup(g)}{detail}"))
    elif g.state == "in" and g.scored and prev.state == "pre" and sport in GOAL_SPORTS and (g.home_score or g.away_score):
        # 시작 전 → (한 틱 사이) 이미 골: 시작과 골을 같이
        out.append(Event(g, "goal", f"goal:{_score(g)}:{prev.gen}", f"{em} 골! {tag} {fmt.matchup(g)}"))
    return out


class Alerts:
    def __init__(self, feed: Feed, db: DB, clock=time.time):
        self.feed = feed
        self.db = db
        self.clock = clock
        self.snap: dict[str, Snap] = {}
        self.last_fetch: dict[str, float] = {}      # 리그 → 마지막으로 받은 시각
        self.last_sched: dict[str, float] = {}      # 리그 → 마지막으로 하루 전체 일정 받은 시각
        self.sent_times: dict[int, deque] = {}      # 방 → 최근 보낸 알림 시각 (시간당 상한)
        self.fail_until: dict[int, float] = {}      # 방·사람 → 보내기 실패 뒤 쉬는 끝 시각
        self.seen_in: dict[str, set] = {}           # 경기 key → 그 경기를 받아 온 리그들
        self.last_prune = 0.0

    # ── 구독 ─────────────────────────────────────────────
    async def follows(self, chat_id: int | None = None) -> list:
        if chat_id is None:
            return await self.db._all("SELECT * FROM sports_follow ORDER BY chat_id, added_at")
        return await self.db._all("SELECT * FROM sports_follow WHERE chat_id=? ORDER BY added_at", (chat_id,))

    async def follow(self, chat_id: int, league: str, team: str, label: str, by: int | None) -> str:
        """'added' / 'exists' / 'full'."""
        def run(c):
            if c.execute("SELECT 1 FROM sports_follow WHERE chat_id=? AND league=? AND team=?",
                         (chat_id, league, team)).fetchone():
                return "exists"
            if c.execute("SELECT COUNT(*) FROM sports_follow WHERE chat_id=?", (chat_id,)).fetchone()[0] >= MAX_FOLLOWS:
                return "full"
            c.execute("INSERT INTO sports_follow VALUES(?, ?, ?, ?, ?, ?)",
                      (chat_id, league, team, label, by, int(self.clock())))
            return "added"
        return await self.db.atomic(run)

    async def unfollow(self, chat_id: int, league: str, team: str) -> bool:
        return await self.db.atomic(lambda c: c.execute(
            "DELETE FROM sports_follow WHERE chat_id=? AND league=? AND team=?", (chat_id, league, team)).rowcount) > 0

    # ── 콕 집은 경기 · 개인 1:1 알림 ───────────────────────
    async def watch(self, chat_id: int, user_id: int, league: str, team: str, game: str, label: str,
                    level: str = "goals", expires: int = 0) -> str:
        """'added' / 'exists' / 'full'. chat_id = 받을 곳 (방 또는 그 사람 1:1)."""
        level = level if level in LEVELS else "goals"

        def run(c):
            row = c.execute("SELECT id FROM sports_watch WHERE chat_id=? AND league=? AND team=? AND game=?",
                            (chat_id, league, team, game)).fetchone()
            if row:
                c.execute("UPDATE sports_watch SET level=?, expires=? WHERE id=?", (level, expires, row[0]))
                return "exists"
            if c.execute("SELECT COUNT(*) FROM sports_watch WHERE chat_id=?", (chat_id,)).fetchone()[0] >= MAX_WATCHES:
                return "full"
            c.execute("INSERT INTO sports_watch(chat_id, user_id, league, team, game, label, level, created, expires) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (chat_id, user_id, league, team, game, label, level, int(self.clock()), expires))
            return "added"
        return await self.db.atomic(run)

    async def watches(self, chat_id: int | None = None) -> list:
        if chat_id is None:
            return await self.db._all("SELECT * FROM sports_watch ORDER BY id")
        return await self.db._all("SELECT * FROM sports_watch WHERE chat_id=? ORDER BY id", (chat_id,))

    async def unwatch(self, chat_id: int, watch_id: int) -> bool:
        return await self.db.atomic(lambda c: c.execute(
            "DELETE FROM sports_watch WHERE chat_id=? AND id=?", (chat_id, watch_id)).rowcount) > 0

    # ── 폴링 ─────────────────────────────────────────────
    def _window(self, g: Game, now: float) -> bool:
        if g.state == "in":
            return now - g.start < MAX_GAME_LEN * 2
        return g.state == "pre" and g.start - PRE_WINDOW <= now <= g.start + MAX_GAME_LEN

    async def _poll_league(self, code: str, now: float) -> list[Game]:
        lg = LEAGUES[code]
        if not self.feed.available(lg):
            return []
        today = datetime.fromtimestamp(now, KST).date()
        days = [today]
        yday = today - timedelta(days=1)
        old = self.feed.cached_day(lg, yday)
        if old and any(self._window(g, now) for g in old):   # 자정 넘긴 경기
            days.insert(0, yday)
        cached = [g for d in days for g in (self.feed.cached_day(lg, d) or [])]
        live = [g for g in cached if self._window(g, now)]
        # 일정이 비었을 때도 최소 5분 간격 (ESPN 장애 때 30초 틱마다 두드리지 않게 — 감사 2026-09-30)
        sched_due = now - self.last_sched.get(code, 0) >= SCHEDULE_EVERY or (
            self.feed.cached_day(lg, today) is None and now - self.last_fetch.get(code, 0) >= RETRY_EVERY)
        live_due = bool(live) and now - self.last_fetch.get(code, 0) >= LIVE_EVERY
        if not (sched_due or live_due):
            return []
        self.last_fetch[code] = now
        games: list[Game] = []
        bg = BACKGROUND.set(True)          # 자동 폴링 = 명령용 예비 한도는 안 씀 (API-Sports)
        try:
            for d in days:
                if sched_due or not any(g.src for g in live):
                    games += await self.feed.day(lg, d, max_age=0)
                else:
                    only = {g.src for g in live if g.src and datetime.fromtimestamp(g.start, KST).date() == d}
                    if only:
                        games += await self.feed.day(lg, d, only=only)
        except SportsError as e:
            log.warning("sports poll %s: %s", code, e)
            if sched_due:   # 실패해도 다음 일정 받기는 RETRY_EVERY 뒤로
                self.last_sched[code] = now - SCHEDULE_EVERY + RETRY_EVERY
            return []
        finally:
            BACKGROUND.reset(bg)
        if sched_due:
            self.last_sched[code] = now
        return games

    async def tick(self, bot: Bot, is_active=None) -> int:
        """몇 초마다 (handlers.job_sports). 보낸 메시지 수를 돌려줌.
        받는 곳 = 방(구독 sports_follow + 콕 집은 경기 sports_watch) · 사람 1:1(sports_watch). 방은 sports_enabled + 이용 중일 때만."""
        now = self.clock()
        targets: dict[int, list] = {}
        for f in await self.follows():
            targets.setdefault(f["chat_id"], []).append(dict(f))
        for w in await self.watches():
            if w["expires"] and w["expires"] < now:
                continue
            targets.setdefault(w["chat_id"], []).append(dict(w))
        active: dict[int, tuple[dict | None, list]] = {}
        for cid, fl in targets.items():
            if cid > 0:                                   # 개인 1:1 — 본인이 건 것 (방 설정·이용 기간과 무관)
                active[cid] = (None, fl)
                continue
            s = await self.db.get_settings(cid)
            if s.get("sports_enabled") and (is_active is None or await is_active(cid)):
                active[cid] = (s, fl)
        events: list[Event] = []
        codes = {f["league"] for _, fl in active.values() for f in fl if f["league"] in LEAGUES}
        codes |= {c for _, fl in active.values() for f in fl if f["team"] for c in cups_for(f["league"])}
        for code in sorted(codes):
            for g in await self._poll_league(code, now):
                self.seen_in.setdefault(g.key, set()).add(code)    # 같은 경기가 'EPL'·'세계 축구' 두 곳에서 와도 구독 둘 다 맞게
                prev = self.snap.get(g.key)
                events += diff(prev, g)
                self.snap[g.key] = next_snap(prev, g)
        sent = 0
        hour = datetime.fromtimestamp(now, KST).hour
        done_games: set[tuple[int, str]] = set()
        for cid, (s, fl) in active.items():
            quiet = s is not None and in_quiet(s.get("sports_quiet", "01-07"), hour)
            level = (s or {}).get("sports_alerts") or "goals"
            mine = [e for e in events if self._wants(fl, level, e)]
            fresh = []
            for e in mine:
                if e.kind in HOLD_KINDS:
                    done_games.add((cid, e.game.key))
                if not await self._claim(cid, e, now):      # 이미 보냈거나 모아 둔 것
                    continue
                if quiet:
                    if e.kind in HOLD_KINDS:
                        await self._hold(cid, e, now)
                    continue
                fresh.append(e)
            held = [] if quiet else await self.db._all(
                "SELECT * FROM sports_held WHERE chat_id=? ORDER BY created LIMIT ?", (cid, MAX_LINES - 1))
            lines = (["🌙 <b>밤사이 경기 결과</b>"] + [h["text"] for h in held] + ([""] if fresh else [])) if held else []
            room = MAX_LINES - len(lines)
            now_lines, later = fresh[:room], fresh[room:]
            lines += [e.text for e in now_lines]
            ok = bool(lines) and await self._send(bot, cid, "\n".join(lines), now)
            if ok:
                sent += 1
                for h in held:   # 보낸 뒤에만 지움 (상한·오류면 다음 틱에 다시 — 감사 2026-09-30)
                    await self.db._write("DELETE FROM sports_held WHERE chat_id=? AND game=? AND kind=?",
                                         (cid, h["game"], h["kind"]))
            # 못 보낸 결과·취소는 버리지 않고 모아 둠 (상한에 걸려 '경기 종료'가 사라지던 문제). 골·시작은 늦으면 의미 없어 버림
            for e in (later if ok else now_lines + later):
                if e.kind in HOLD_KINDS:
                    await self._hold(cid, e, now)
        for cid, key in done_games:                        # 끝난 경기 하나짜리 알림은 정리
            await self.db._write("DELETE FROM sports_watch WHERE chat_id=? AND game=?", (cid, key))
        if now - self.last_prune > 3600:
            self.last_prune = now
            await self.prune(now)
            self.feed.forget_old(datetime.fromtimestamp(now, KST).date() - timedelta(days=2))
            for k in [k for k, v in self.snap.items() if v.state in ("post", "cancel", "postponed") and len(self.snap) > 5000]:
                self.snap.pop(k, None)
                self.seen_in.pop(k, None)
        return sent

    async def _hold(self, cid: int, e: Event, now: float) -> None:
        await self.db._write("INSERT OR IGNORE INTO sports_held VALUES(?, ?, ?, ?, ?)",
                             (cid, e.game.key, e.dedupe, e.text, int(now)))

    def _wants(self, follows: list, level: str, e: Event) -> bool:
        """구독·콕 집은 경기 중 하나라도 이 이벤트를 원하면. 콕 집은 줄은 자기 알림 종류(level), 방 구독은 방 설정."""
        g = e.game
        for f in follows:
            kinds = LEVELS.get(f.get("level") or level, LEVELS["goals"])[1]
            if e.kind not in kinds:
                continue
            if f.get("game"):
                if f["game"] == g.key:
                    return True
                continue
            # 팀 구독은 그 팀이 나가는 유럽 대항전(챔스·유로파) 경기도 (감사 2026-09-30: 토트넘 구독인데 챔스 알림 없음)
            seen = self.seen_in.get(g.key) or {g.league}
            if f["league"] not in seen and not (f["team"] and seen & set(cups_for(f["league"]))):
                continue
            if not f["team"] or same_team(g.home, f["team"], f["league"]) or same_team(g.away, f["team"], f["league"]):
                return True
        return False

    async def _claim(self, cid: int, e: Event, now: float) -> bool:
        return await self.db.atomic(lambda c: c.execute(
            "INSERT OR IGNORE INTO sports_alert_sent VALUES(?, ?, ?, ?)", (cid, e.game.key, e.dedupe, int(now))).rowcount) > 0

    async def _send(self, bot: Bot, cid: int, text: str, now: float) -> bool:
        if self.fail_until.get(cid, 0) > now:            # 막 실패한 곳은 잠깐 쉼 (몇 초 틱마다 두드리지 않게)
            return False
        q = self.sent_times.setdefault(cid, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        cap = DM_PER_HOUR if cid > 0 else MAX_PER_HOUR
        if len(q) >= cap:
            log.info("sports alert cap %s (시간당 %d통)", cid, cap)
            return False
        q.append(now)
        try:
            await bot.send_message(cid, text, parse_mode="HTML", disable_notification=False)
            return True
        except Forbidden as e:
            log.warning("sports alert send %s forbidden: %s", cid, e)
            if cid > 0:                                   # 1:1 을 막은 사람 → 그 사람 알림은 지움 (계속 못 받음)
                await self.db._write("DELETE FROM sports_watch WHERE chat_id=?", (cid,))
                await self.db._write("DELETE FROM sports_held WHERE chat_id=?", (cid,))
            self.fail_until[cid] = now + FAIL_PAUSE
            return False
        except TelegramError as e:
            log.warning("sports alert send %s failed: %s", cid, e)
            self.fail_until[cid] = now + FAIL_PAUSE
            return False

    async def prune(self, now: float) -> None:
        cut = int(now) - KEEP_DAYS * 86400
        await self.db._write("DELETE FROM sports_alert_sent WHERE sent_at < ?", (cut,))
        await self.db._write("DELETE FROM sports_held WHERE created < ?", (int(now) - 2 * 86400,))
        await self.db._write("DELETE FROM sports_watch WHERE expires > 0 AND expires < ?", (int(now),))
