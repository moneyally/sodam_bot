"""스포츠 데이터 소스 (provider). 소스를 바꾸거나 더할 땐 이 파일만.

공통 모양: Game(경기 하나) · Row(순위 한 줄). 소스마다 day(리그, 한국 날짜) / standings(리그) / team_games(리그, 팀).
실패는 SportsError → feed.py 가 다음 소스로 넘어감.

필드는 전부 실제 응답(tests/fixtures/sports/*.json, 2026-09-29 curl)에서 확인한 것만 읽는다. 참고한 공개 자료:
- ESPN 비공식 API 정리: gist akeaswaran/b48b02f1c94f873c6655e7129910fc3b · github pseudo-r/Public-ESPN-API
  (순위는 /apis/v2/… 경로 — /apis/site/v2/ 는 순위가 비어 옴, dates 는 하루씩 — 'A-B' 범위는 400 (실측 확인))
- ESPN 상태: type.state pre/in/post + completed, 이름이 POSTPONED/CANCELED/SUSPENDED/ABANDONED 면 state 보다 이름 우선
  (github alexander-bain/bainluck#3397: 연기된 경기가 'in' 으로 남아 라이브로 보인 사례)
- 네이버 statusCode(문서 없음, 실측): BEFORE·READY=전, STARTED=중, ENDED·RESULT=끝, cancel/suspended 플래그
  (github mini-05/NC-Baseball-alert 실측 표). upperCategoryId 를 붙이면 농구·배구가 0건 → categoryId 만.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from .leagues import League

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc


class SportsError(Exception):
    pass


@dataclass
class Game:
    key: str                 # 소스:ID (전 세계에서 하나)
    league: str              # 리그 code
    start: int               # 시작 시각 (epoch 초)
    home: str
    away: str
    home_score: int | None = None
    away_score: int | None = None
    state: str = "pre"       # pre / in / post / cancel / postponed / suspended
    detail: str = ""         # 57' · 5회초 · 하프타임 …
    goals: tuple = ()        # ((시간, 'home'|'away', 이름, 표시), …) — ESPN 축구 details 의 득점만
    src: str = ""            # 어떤 요청에서 왔는지 (ESPN 날짜) — 라이브 때 그 요청만 다시
    title: str = ""          # 대회 이름 (UFC 처럼 팀이 없는 경기)

    @property
    def scored(self) -> bool:
        return self.home_score is not None and self.away_score is not None


@dataclass
class Row:
    rank: int
    team: str
    played: int | None = None
    wins: int | None = None
    draws: int | None = None
    losses: int | None = None
    points: float | None = None
    pct: str = ""
    behind: str = ""
    group: str = ""


Fetch = Callable[[str, dict, dict], Awaitable[dict]]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class Http:
    """진짜 네트워크. 테스트는 fetch 함수(url, params, headers → dict)를 대신 넣는다."""

    def __init__(self, timeout: float = 10):
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA})
        self.calls = 0

    async def __call__(self, url: str, params: dict, headers: dict) -> dict:
        self.calls += 1
        try:
            r = await self.client.get(url, params=params, headers=headers)
            r.raise_for_status()
            return r.json() or {}
        except (httpx.HTTPError, ValueError) as e:
            log.warning("sports GET %s failed: %s", url, e)
            raise SportsError("경기 정보를 가져오지 못했어요. 잠시 후 다시 해주세요.") from e

    async def close(self) -> None:
        await self.client.aclose()


def _int(v) -> int | None:
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def kst_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, KST).date()


class Provider:
    name = ""

    def enabled(self) -> bool:
        return True

    def supports(self, lg: League) -> bool:
        return False

    async def day(self, lg: League, d: date, only: set[str] | None = None) -> list[Game]:
        raise SportsError("지원하지 않는 리그예요.")

    async def standings(self, lg: League) -> list[Row]:
        raise SportsError("이 리그는 순위를 볼 수 없어요.")

    async def team_games(self, lg: League, team: str) -> list[Game]:
        raise SportsError("이 팀 경기는 볼 수 없어요.")


# ── ESPN (키 없음, 해외 리그 기본) ─────────────────────────────
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/"
ESPN_STAND = "https://site.api.espn.com/apis/v2/sports/"
_STOPPED = (("POSTPONED", "postponed"), ("CANCEL", "cancel"), ("ABANDON", "cancel"), ("FORFEIT", "cancel"),
            ("SUSPENDED", "suspended"))
_INNING = re.compile(r"^(Top|Bot|Bottom|Mid|Middle|End)\s+(\d+)")


def espn_state(st: dict) -> str:
    t = st.get("type") or {}
    name = (t.get("name") or "").upper()
    for word, state in _STOPPED:
        if word in name:
            return state
    s = t.get("state")
    if s == "in":
        return "in"
    if s == "post":
        return "post" if t.get("completed", True) else "cancel"
    return "pre"


def espn_detail(st: dict, sport: str) -> str:
    t = st.get("type") or {}
    if "HALFTIME" in (t.get("name") or ""):
        return "하프타임"
    if sport == "soccer":
        return st.get("displayClock") or ""
    short = t.get("shortDetail") or ""
    m = _INNING.match(short)
    if m:
        return f"{m.group(2)}회{'초' if m.group(1) == 'Top' else '말'}"
    return short


class ESPN(Provider):
    name = "espn"

    def __init__(self, fetch: Fetch):
        self.fetch = fetch
        self._team_ids: dict[str, dict[str, str]] = {}

    def supports(self, lg: League) -> bool:
        return bool(lg.espn)

    def parse_event(self, lg: League, e: dict, src: str = "") -> Game | None:
        comps = e.get("competitions") or [{}]
        comp = comps[0]
        st = e.get("status") or comp.get("status") or {}
        try:
            start = int(datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp())
        except (KeyError, ValueError):
            return None
        state = espn_state(st)
        side = {c.get("homeAway"): c for c in comp.get("competitors") or [] if c.get("homeAway")}
        if lg.sport == "mma" or not side:     # UFC: 대회 하나를 한 경기로 (선수 대결 여러 개)
            return Game(f"espn:{e.get('id')}", lg.code, start, "", "", state=state, detail=espn_detail(st, lg.sport),
                        src=src, title=e.get("name") or e.get("shortName") or lg.name)
        home, away = side.get("home", {}), side.get("away", {})
        ids = {str((home.get("team") or {}).get("id")): "home", str((away.get("team") or {}).get("id")): "away"}
        hs, as_ = (_int(home.get("score")), _int(away.get("score"))) if state in ("in", "post", "suspended") else (None, None)
        goals = []
        for d in comp.get("details") or []:
            if not d.get("scoringPlay"):
                continue
            who = (d.get("athletesInvolved") or [{}])[0].get("displayName") or ""
            tag = "자책" if d.get("ownGoal") else "PK" if d.get("penaltyKick") else ""
            goals.append(((d.get("clock") or {}).get("displayValue") or "", ids.get(str((d.get("team") or {}).get("id")), ""),
                          who, tag))
        return Game(f"espn:{e.get('id')}", lg.code, start, (home.get("team") or {}).get("displayName") or "?",
                    (away.get("team") or {}).get("displayName") or "?", hs, as_, state,
                    espn_detail(st, lg.sport), tuple(goals), src)

    async def day(self, lg: League, d: date, only: set[str] | None = None) -> list[Game]:
        # ESPN 의 dates 는 미국 동부 날짜 → 한국 하루(0~24시)는 동부 전날·그날 두 날에 걸침 (실측: MLB dates=0929 = 18:00Z~02:00Z)
        srcs = [(d - timedelta(days=1)).strftime("%Y%m%d"), d.strftime("%Y%m%d")]
        out: dict[str, Game] = {}
        for src in srcs:
            if only is not None and src not in only:
                continue
            data = await self.fetch(f"{ESPN_SITE}{lg.espn}/scoreboard", {"dates": src}, {})
            for e in data.get("events") or []:
                g = self.parse_event(lg, e, src)
                if g and kst_day(g.start) == d:
                    out[g.key] = g
        return sorted(out.values(), key=lambda g: (g.start, g.key))

    async def standings(self, lg: League) -> list[Row]:
        data = await self.fetch(f"{ESPN_STAND}{lg.espn}/standings", {}, {})
        rows: list[Row] = []

        def walk(node: dict, group: str) -> None:
            entries = (node.get("standings") or {}).get("entries")
            if entries:
                part = []
                for i, en in enumerate(entries):
                    stats = {s.get("name"): s for s in en.get("stats") or []}
                    val = lambda k: (stats.get(k) or {}).get("value")  # noqa: E731
                    part.append(Row(_int(val("rank")) or 0, (en.get("team") or {}).get("displayName") or "?",
                                    _int(val("gamesPlayed")), _int(val("wins")), _int(val("ties")), _int(val("losses")),
                                    val("points"), (stats.get("winPercent") or {}).get("displayValue") or "",
                                    (stats.get("gamesBehind") or {}).get("displayValue") or "", group))
                if not any(r.rank for r in part):     # NBA 처럼 rank 가 없으면 승률 순
                    part.sort(key=lambda r: -float(r.pct or 0))
                    for i, r in enumerate(part):
                        r.rank = i + 1
                rows.extend(sorted(part, key=lambda r: r.rank))
            for ch in node.get("children") or []:
                walk(ch, ch.get("name") or group)

        walk(data, "")
        if not rows:
            raise SportsError("순위 정보가 아직 없어요.")
        return rows

    async def _team_id(self, lg: League, team: str) -> str | None:
        if lg.code not in self._team_ids:
            data = await self.fetch(f"{ESPN_SITE}{lg.espn}/teams", {}, {})
            teams = (((data.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams")) or []
            self._team_ids[lg.code] = {t["team"]["displayName"]: str(t["team"]["id"]) for t in teams if t.get("team")}
        ids = self._team_ids[lg.code]
        return ids.get(team) or next((v for k, v in ids.items() if team.lower() in k.lower()), None)

    async def team_games(self, lg: League, team: str) -> list[Game]:
        tid = await self._team_id(lg, team)
        if not tid:
            raise SportsError("그 팀을 못 찾았어요.")
        url = f"{ESPN_SITE}{lg.espn}/teams/{tid}/schedule"
        events = list((await self.fetch(url, {}, {})).get("events") or [])
        if lg.sport == "soccer":   # 축구는 schedule = 지난 경기, fixture=true = 남은 경기 (실측)
            events += (await self.fetch(url, {"fixture": "true"}, {})).get("events") or []
        games = {g.key: g for g in (self.parse_event(lg, e) for e in events) if g}
        return sorted(games.values(), key=lambda g: g.start)


# ── 네이버 스포츠 (국내 리그, 기본 꺼짐: SPORTS_NAVER=1) ─────────
NAVER = "https://api-gw.sports.naver.com"
NAVER_HEADERS = {"Referer": "https://m.sports.naver.com/"}
_NAVER_STATE = {"BEFORE": "pre", "READY": "pre", "STARTED": "in", "ENDED": "post", "RESULT": "post"}


class Naver(Provider):
    """robots.txt·약관상 자동 수집 금지 → 오너가 SPORTS_NAVER=1 로 켤 때만 (2026-09-29 결정 보류)."""
    name = "naver"

    def __init__(self, fetch: Fetch, on: bool | None = None):
        self.fetch = fetch
        self.on = os.getenv("SPORTS_NAVER", "0") == "1" if on is None else on
        self._unknown: set[str] = set()

    def enabled(self) -> bool:
        return self.on

    def supports(self, lg: League) -> bool:
        return bool(lg.naver)

    def parse_game(self, lg: League, g: dict) -> Game | None:
        try:
            start = int(datetime.fromisoformat(g["gameDateTime"]).replace(tzinfo=KST).timestamp())
        except (KeyError, ValueError):
            return None
        code = g.get("statusCode") or ""
        if g.get("cancel"):
            state = "cancel"
        elif g.get("suspended"):
            state = "suspended"
        else:
            state = _NAVER_STATE.get(code, "")
            if not state:
                if code not in self._unknown:     # 새 상태 코드는 한 번만 기록 (시즌마다 새 값이 생길 수 있음)
                    self._unknown.add(code)
                    log.warning("naver 모르는 statusCode %s", code)
                state = "in"
        scored = state in ("in", "post", "suspended")
        return Game(f"naver:{g.get('gameId')}", lg.code, start, g.get("homeTeamName") or "", g.get("awayTeamName") or "",
                    _int(g.get("homeTeamScore")) if scored else None, _int(g.get("awayTeamScore")) if scored else None,
                    state, "" if state == "post" else (g.get("statusInfo") or ""),
                    title="" if g.get("homeTeamName") else lg.name)

    async def _games(self, lg: League, a: date, b: date) -> list[Game]:
        data = await self.fetch(f"{NAVER}/schedule/games", {"fields": "basic", "categoryId": lg.naver,
                                                            "fromDate": a.isoformat(), "toDate": b.isoformat()},
                                NAVER_HEADERS)
        games = [self.parse_game(lg, g) for g in (data.get("result") or {}).get("games") or []]
        return sorted([g for g in games if g], key=lambda g: (g.start, g.key))

    async def day(self, lg: League, d: date, only: set[str] | None = None) -> list[Game]:
        return await self._games(lg, d, d)

    async def standings(self, lg: League) -> list[Row]:
        year = datetime.now(KST).year
        for y in (year, year - 1):
            data = await self.fetch(f"{NAVER}/statistics/categories/{lg.naver}/seasons/{y}/teams", {}, NAVER_HEADERS)
            stats = (data.get("result") or {}).get("seasonTeamStats") or []
            if stats:
                rows = [Row(_int(t.get("ranking") or t.get("rank")) or 0, t.get("teamName") or "?",
                            _int(t.get("gameCount") if t.get("gameCount") is not None else t.get("matchesPlayed")),
                            _int(t.get("winGameCount", t.get("wins"))), _int(t.get("drawnGameCount", t.get("draws"))),
                            _int(t.get("loseGameCount", t.get("losses"))), t.get("points"),
                            f"{t['wra']:.3f}".lstrip("0") if isinstance(t.get("wra"), (int, float)) else "",
                            str(t.get("gameBehind")) if t.get("gameBehind") is not None else "")
                        for t in stats]
                return sorted(rows, key=lambda r: r.rank)
        raise SportsError("순위 정보가 아직 없어요.")

    async def team_games(self, lg: League, team: str) -> list[Game]:
        today = datetime.now(KST).date()
        games = await self._games(lg, today - timedelta(days=10), today + timedelta(days=10))
        return [g for g in games if team in (g.home, g.away)]


# ── TheSportsDB (유료 키일 때만: 무료 '123' 은 검색 1개·일정 3개로 잘림 — 실측) ─
SDB_DONE = {"FT", "AET", "PEN", "AOT", "AP", "MATCH FINISHED", "FINISHED", "FINAL"}
SDB_PRE = {"", "NS", "NOT STARTED", "TBD", "SCHEDULED"}


class SportsDB(Provider):
    name = "thesportsdb"

    def __init__(self, fetch: Fetch, key: str):
        self.fetch = fetch
        self.key = (key or "").strip()

    def enabled(self) -> bool:
        return bool(self.key) and self.key != "123"

    def supports(self, lg: League) -> bool:
        return bool(lg.sdb)

    def _url(self, path: str) -> str:
        return f"https://www.thesportsdb.com/api/v1/json/{self.key}/{path}"

    def parse_event(self, lg: League, ev: dict) -> Game | None:
        ts = ev.get("strTimestamp")
        try:
            if ts:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(f"{ev['dateEvent']}T{(ev.get('strTime') or '00:00:00')[:8]}")
            dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (KeyError, ValueError, TypeError):
            return None
        status = (ev.get("strStatus") or "").strip().upper()
        hs, as_ = _int(ev.get("intHomeScore")), _int(ev.get("intAwayScore"))
        if "POSTPON" in status or status == "PST":
            state = "postponed"
        elif "CANC" in status or "ABANDON" in status:
            state = "cancel"
        elif status in SDB_DONE:
            state = "post"
        elif status in SDB_PRE and hs is None:
            state = "pre"
        else:
            state = "in" if hs is not None else "pre"
        return Game(f"sdb:{ev.get('idEvent')}", lg.code, int(dt.timestamp()), ev.get("strHomeTeam") or "?",
                    ev.get("strAwayTeam") or "?", hs if state != "pre" else None, as_ if state != "pre" else None,
                    state, ev.get("strProgress") or "")

    async def day(self, lg: League, d: date, only: set[str] | None = None) -> list[Game]:
        out: dict[str, Game] = {}
        for day in (d - timedelta(days=1), d):     # 날짜가 UTC 라 한국 하루는 두 날에 걸침
            data = await self.fetch(self._url("eventsday.php"), {"d": day.isoformat(), "l": lg.sdb}, {})
            for ev in data.get("events") or []:
                g = self.parse_event(lg, ev)
                if g and kst_day(g.start) == d:
                    out[g.key] = g
        return sorted(out.values(), key=lambda g: (g.start, g.key))

    async def team_games(self, lg: League, team: str) -> list[Game]:
        teams = (await self.fetch(self._url("searchteams.php"), {"t": team}, {})).get("teams") or []
        if not teams:
            raise SportsError("그 팀을 못 찾았어요.")
        tid = teams[0]["idTeam"]
        evs = (await self.fetch(self._url("eventslast.php"), {"id": tid}, {})).get("results") or []
        evs += (await self.fetch(self._url("eventsnext.php"), {"id": tid}, {})).get("events") or []
        return sorted([g for g in (self.parse_event(lg, e) for e in evs) if g], key=lambda g: g.start)


# ── API-Sports (유료 공식, APISPORTS_KEY) — 연결 전 자리 ──────────────
class APISports(Provider):
    """https://dashboard.api-football.com/register → 키 헤더 x-apisports-key. 종목마다 따로 구독
    (v3.football / v1.baseball / v1.basketball / v1.volleyball / v1.hockey .api-sports.io).
    TODO(오너 가입 뒤): 리그 ID 표(K리그1=292, KBO·KBL·V리그 ID 는 /leagues 로 확인) + fixtures?date= / ?live=all 파싱
    + 이벤트(fixtures/events)로 득점자. 실제 응답 샘플을 tests/fixtures/sports 에 저장한 뒤에 켤 것 (추측 파싱 금지).
    지금은 키가 있어도 supports()=False 라 쓰이지 않음 — 로그로만 알림."""
    name = "apisports"

    def __init__(self, fetch: Fetch, key: str | None = None):
        self.fetch = fetch
        self.key = (key if key is not None else os.getenv("APISPORTS_KEY", "")).strip()
        if self.key:
            log.info("APISPORTS_KEY 가 있지만 API-Sports 연결은 아직 준비 중 (sodam/sports/providers.py TODO)")

    def enabled(self) -> bool:
        return False
