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

import asyncio
import contextvars
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from .leagues import League, canon, same_team

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
# 브라우저 흉내 UA 는 쓰지 않는다: ESPN 이 데이터센터 IP(Hetzner) + 브라우저 UA 조합을 403 으로 막음 (실측 2026-10-01 서버에서
# 'Mozilla/…' 403 · httpx 기본 UA 200, 3번씩 같음). 브라우저 UA 가 꼭 필요한 곳(네이버)만 그 요청 헤더에 따로.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class Http:
    """진짜 네트워크. 테스트는 fetch 함수(url, params, headers → dict)를 대신 넣는다."""

    def __init__(self, timeout: float = 10):
        self.client = httpx.AsyncClient(timeout=timeout)   # httpx 기본 UA (위 설명)
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
NAVER_HEADERS = {"Referer": "https://m.sports.naver.com/", "User-Agent": UA}
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
        # 종합대회(아시안게임) 경기는 팀 이름·점수 없이 오고 점수 0:0 → 점수 없음으로 (제목만)
        scored = state in ("in", "post", "suspended") and bool(g.get("homeTeamName"))
        return Game(f"naver:{g.get('gameId')}", lg.code, start, g.get("homeTeamName") or "", g.get("awayTeamName") or "",
                    _int(g.get("homeTeamScore")) if scored else None, _int(g.get("awayTeamScore")) if scored else None,
                    state, "" if state == "post" else (g.get("statusInfo") or ""),
                    title="" if g.get("homeTeamName") else (g.get("title") or lg.name))

    async def _games(self, lg: League, a: date, b: date) -> list[Game]:
        params = {"fields": "basic", "categoryId": lg.naver, "fromDate": a.isoformat(), "toDate": b.isoformat()}
        if lg.naver_codes:      # 종합대회: 하루 수백 경기(전 종목) → 크게 받아서 종목 코드로 거름 (기본 50개면 잘림, 실측)
            params |= {"fields": "all", "size": 1000}
        data = await self.fetch(f"{NAVER}/schedule/games", params, NAVER_HEADERS)
        raw = (data.get("result") or {}).get("games") or []
        if lg.naver_codes:
            raw = [g for g in raw if str(g.get("gameId") or "")[4:7] in lg.naver_codes]
        games = [self.parse_game(lg, g) for g in raw]
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


# ── API-Sports (공식, APISPORTS_KEY — 국내 리그 KBO·K리그·KBL·WKBL·V리그 + NPB) ─────────────
# 실측 2026-10-01 (tests/fixtures/sports/apisports_*.json):
# - 키 하나로 종목 API 4개 (v3.football / v1.baseball / v1.basketball / v1.volleyball), 헤더 x-apisports-key.
# - 무료 등급: 종목마다 하루 100번 · 분당 10번 · 'season' 을 붙이면 2022~2024 만 → **date 만** 주면 이번 시즌도 나옴,
#   대신 날짜는 어제~내일만 ("Free plans do not have access to this date, try from …"). date 만 주면 그날 모든 리그가
#   한 번에 와서(축구 하루 ~200경기) 종목당 날짜 하나 = 요청 1번을 모든 리그·방·명령이 같이 씀.
# - 오류는 HTTP 200 + errors 에 담겨 옴 (plan / requests / access(정지) / token / season …). 성공은 errors = [].
# - 상태(short): 축구 NS·TBD·1H·HT·2H·ET·BT·P·LIVE·SUSP·INT·FT·AET·PEN·PST·CANC·ABD·AWD·WO /
#   야구 NS·IN1~9·POST·CANC·INTR·ABD·FT / 농구 NS·Q1~4·OT·BT·HT·FT·AOT·POST·CANC·SUSP·AWD·ABD /
#   배구 NS·S1~5·FT·AW·POST·CANC·INTR·ABD (API-Sports 문서 표 — 문서 사이트는 Cloudflare 로 막혀 검색 결과·실제 응답으로 확인).
# - 순위(standings)는 season 이 꼭 필요해서 무료 등급은 이번 시즌을 못 봄 → 유료 키에서만 (응답 샘플 확인 뒤 구현, 지금은 없음).
# 자동 알림 폴링 중인지 (alerts._poll_league 가 켬) — 그동안 API-Sports 는 명령용 예비 한도를 안 씀 (리뷰 2026-10-01:
# 일정 폴링이 사람 명령처럼 예비분까지 쓰던 것)
BACKGROUND: contextvars.ContextVar[bool] = contextvars.ContextVar("sports_background", default=False)
APS_HOST = {"football": "https://v3.football.api-sports.io", "baseball": "https://v1.baseball.api-sports.io",
            "basketball": "https://v1.basketball.api-sports.io", "volleyball": "https://v1.volleyball.api-sports.io"}
APS_PATH = {"football": "fixtures", "baseball": "games", "basketball": "games", "volleyball": "games"}
APS_RESERVE = 8            # 하루 한도 중 명령('.스포츠 내일' 등)용으로 남겨 둘 몫 — 알림 폴링은 이만큼 남으면 쉼
APS_MIN_GAP = 60           # 같은 (종목, 날짜) 다시 받는 최소 간격(초). 무료 등급은 남은 한도로 더 늘어남 (_gap)
APS_OTHER_TTL = 3600       # 오늘·어제가 아닌 날(내일 일정 등)은 1시간
APS_CMD_TTL = 1800         # 알림용 한도를 다 쓴 뒤 사람 명령이 새로 받는 간격
_FB = {"NS": "pre", "TBD": "pre", "1H": "in", "HT": "in", "2H": "in", "ET": "in", "BT": "in", "P": "in", "LIVE": "in",
       "SUSP": "suspended", "INT": "suspended", "FT": "post", "AET": "post", "PEN": "post", "AWD": "post", "WO": "post",
       "PST": "postponed", "CANC": "cancel", "ABD": "cancel"}
_COMMON = {"NS": "pre", "FT": "post", "AOT": "post", "AW": "post", "AWD": "post", "POST": "postponed", "CANC": "cancel",
           "ABD": "cancel", "INTR": "suspended", "SUSP": "suspended", "HT": "in", "BT": "in", "OT": "in"}
_PERIOD = re.compile(r"^(IN|Q|S)(\d+)$")
_SET_KEYS = ("first", "second", "third", "fourth", "fifth")


class APISports(Provider):
    name = "apisports"

    def __init__(self, fetch: Fetch, key: str | None = None, daily: int | None = None, clock=None):
        self.fetch = fetch
        self._key = key                       # None = 부를 때마다 환경변수 (오너가 '.키' 로 넣으면 재시작 없이 켜짐)
        self._daily = daily
        self.clock = clock or time.time
        self.used: dict[tuple[str, str], int] = {}       # (종목, UTC 날짜) → 이 프로세스가 보낸 요청 수
        self.exhausted: dict[str, str] = {}               # 종목 → 한도를 다 쓴 UTC 날짜 (API 가 requests 오류를 줌)
        self.free = False                                 # 'Free plans …' 오류를 한 번이라도 봄 → 날짜 창 검사
        self.blocked = ""                                 # 계정 정지·키 오류 (그 키로는 더 안 부름)
        self._blocked_key = ""
        self._last_key = ""                               # 키가 바뀌면 한도·무료 표시·막힘을 새로 (유료 키로 바꾼 직후 등)
        self._raw: dict[tuple[str, date], tuple[float, list]] = {}
        self._locks: dict[tuple[str, date], asyncio.Lock] = {}   # '.스포츠 야구' 처럼 KBO·NPB 를 동시에 물어도 요청 1번
        self._season: dict[str, object] = {}              # 리그 → 응답에 실린 이번 시즌 (순위용)
        self._unknown: set[str] = set()

    @property
    def key(self) -> str:
        return (self._key if self._key is not None else os.getenv("APISPORTS_KEY", "")).strip()

    @property
    def daily(self) -> int:
        if self._daily is not None:
            return self._daily
        try:
            return max(10, int(os.getenv("APISPORTS_DAILY", "100")))
        except ValueError:
            return 100

    def enabled(self) -> bool:
        k = self.key
        if k != self._last_key:          # 오너가 '.키' 로 키를 바꿈 → 이 키 기준으로 처음부터 (재시작 없이)
            self._last_key = k
            self.used.clear()
            self.exhausted.clear()
            self.free = False
            self._raw.clear()
            if k != self._blocked_key:
                self.blocked = ""
        return bool(k) and not self.blocked

    def supports(self, lg: League) -> bool:
        return bool(lg.aps) and lg.aps[0] in APS_HOST

    # ── 한도·간격 ─────────────────────────────────────────
    def _utc_day(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).date().isoformat()

    def remaining(self, sport: str) -> int:
        if self.exhausted.get(sport) == self._utc_day():
            return 0
        return max(0, self.daily - self.used.get((sport, self._utc_day()), 0))

    def _gap(self, sport: str) -> float:
        """같은 날짜를 다시 받기까지 기다릴 초. 한도가 넉넉하면(유료) 60초, 무료면 남은 한도를 UTC 자정까지 나눔
        (한도가 하루 안에 바닥나지 않게 — 남은 게 많으면 짧아지고 적으면 길어짐)."""
        now = self.clock()
        left = self.remaining(sport) - APS_RESERVE
        if left <= 0:
            return float("inf")
        midnight = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return max(APS_MIN_GAP, (midnight.timestamp() - now) / left)

    async def _get(self, sport: str, params: dict, *, essential: bool) -> list:
        """요청 1번. essential=False(알림 폴링)는 예비분을 남기고 멈춤, True(사람 명령)는 예비분까지 씀."""
        if not self.enabled():
            raise SportsError("API-Sports 키가 없거나 막혔어요.")
        rem = self.remaining(sport)
        if rem <= 0 or (not essential and rem <= APS_RESERVE):
            raise SportsError("오늘 경기 정보 조회 한도를 다 써서 내일 다시 볼 수 있어요.")
        day = self._utc_day()
        self.used[(sport, day)] = self.used.get((sport, day), 0) + 1
        key = self.key
        data = await self.fetch(f"{APS_HOST[sport]}/{APS_PATH[sport]}", params, {"x-apisports-key": key})
        errs = data.get("errors")
        if errs:
            text = " ".join(str(v) for v in (errs.values() if isinstance(errs, dict) else errs))
            kinds = set(errs) if isinstance(errs, dict) else set()
            if "requests" in kinds:
                self.exhausted[sport] = day
                raise SportsError("오늘 경기 정보 조회 한도를 다 써서 내일 다시 볼 수 있어요.")
            if kinds & {"access", "token"} or "suspend" in text.lower():
                self.blocked, self._blocked_key = text[:200], key
                log.error("API-Sports 막힘 (키 확인 필요): %s", text[:200])
                raise SportsError("경기 정보 소스에 접속이 막혔어요 (운영자 확인 필요).")
            if "plan" in kinds or "Free plan" in text:
                self.free = True
                raise SportsError("무료 등급이라 이 날짜·시즌은 볼 수 없어요.")
            log.warning("API-Sports %s %s 오류: %s", sport, params, text[:200])
            raise SportsError("경기 정보를 가져오지 못했어요. 잠시 후 다시 해주세요.")
        return list(data.get("response") or [])

    def _today_utc(self) -> date:
        return datetime.fromtimestamp(self.clock(), UTC).date()

    def _in_window(self, d: date) -> bool:
        """무료 등급(한도 1000 미만이거나 'Free plans' 오류를 봄)은 어제~내일만 — 밖의 날짜는 요청을 쓰지 않고 거절."""
        return not (self.free or self.daily < 1000) or abs((d - self._today_utc()).days) <= 1

    async def _raw_day(self, sport: str, d: date, essential: bool = True) -> list:
        """(종목, 한국 날짜) 하나 = 요청 1번, 모든 리그가 같이 씀."""
        async with self._locks.setdefault((sport, d), asyncio.Lock()):
            return await self._raw_day_locked(sport, d, essential)

    async def _raw_day_locked(self, sport: str, d: date, essential: bool) -> list:
        now = self.clock()
        k = (sport, d)
        hit = self._raw.get(k)
        today = datetime.fromtimestamp(now, KST).date()
        ttl = self._gap(sport) if d in (today, today - timedelta(days=1)) else APS_OTHER_TTL
        if essential and ttl == float("inf"):
            ttl = APS_CMD_TTL      # 알림 몫은 다 썼어도 사람 명령은 예비분으로 가끔 새로 (영원히 얼어 있지 않게)
        if hit and (now - hit[0] < ttl or not self._in_window(d)):   # 날짜 창을 벗어난 날은 마지막 것을 그대로
            return hit[1]
        if not self._in_window(d):
            raise SportsError("무료 등급이라 어제~내일 경기만 볼 수 있어요.")
        try:
            items = await self._get(sport, {"date": d.isoformat(), "timezone": "Asia/Seoul"}, essential=essential)
        except SportsError:
            if hit:          # 한도·일시 오류면 마지막으로 받은 것 (조금 늦어도 빈 화면보다 나음)
                return hit[1]
            raise
        self._raw[k] = (now, items)
        for kk in [kk for kk in self._raw if (kk[1] - today).days < -2 or (kk[1] - today).days > 3]:
            self._raw.pop(kk, None)
            self._locks.pop(kk, None)
        return items

    # ── 파싱 ─────────────────────────────────────────────
    def _state(self, sport: str, short: str) -> str:
        if sport == "football" and short in _FB:
            return _FB[short]
        if short in _COMMON:
            return _COMMON[short]
        if _PERIOD.match(short):
            return "in"
        if short not in self._unknown:
            self._unknown.add(short)
            log.warning("API-Sports %s 모르는 상태 %s", sport, short)
        return ""

    def parse(self, lg: League, sport: str, x: dict, src: str = "") -> Game | None:
        fx = x.get("fixture") if sport == "football" else x
        if not isinstance(fx, dict):
            return None
        try:
            start = int(fx.get("timestamp") or datetime.fromisoformat(fx["date"]).timestamp())
        except (KeyError, ValueError, TypeError):
            return None
        st = fx.get("status") or {}
        short = (st.get("short") or "").upper()
        teams = x.get("teams") or {}
        home = canon(lg.code, (teams.get("home") or {}).get("name") or "?")
        away = canon(lg.code, (teams.get("away") or {}).get("name") or "?")
        if sport == "football":
            hs, as_ = _int((x.get("goals") or {}).get("home")), _int((x.get("goals") or {}).get("away"))
        elif sport == "volleyball":                          # 배구 점수 = 이긴 세트 수
            sc = x.get("scores") or {}
            hs, as_ = _int(sc.get("home")), _int(sc.get("away"))
        else:
            sc = x.get("scores") or {}
            hs, as_ = _int((sc.get("home") or {}).get("total")), _int((sc.get("away") or {}).get("total"))
        state = self._state(sport, short) or ("in" if hs is not None else "pre")
        scored = state in ("in", "post", "suspended")
        return Game(f"aps:{sport}:{x.get('id') if sport != 'football' else fx.get('id')}", lg.code, start, home, away,
                    hs if scored else None, as_ if scored else None, state, self._detail(sport, short, state, st, x), src=src)

    @staticmethod
    def _detail(sport: str, short: str, state: str, st: dict, x: dict) -> str:
        if state == "post":
            if sport == "football" and short == "PEN":
                pen = (x.get("score") or {}).get("penalty") or {}
                return f"승부차기 {pen.get('home')}-{pen.get('away')}" if pen.get("home") is not None else "승부차기"
            return {"AET": "연장 끝", "AOT": "연장 끝"}.get(short, "")
        if state != "in":
            return ""
        if short == "HT":
            return "하프타임"
        if short == "BT":
            return "쉬는 시간"
        if sport == "football":
            if short == "P":
                return "승부차기"
            el, ex = st.get("elapsed"), st.get("extra")
            if el is None:
                return ""
            return ("연장 " if short == "ET" else "") + (f"{el}+{ex}'" if ex else f"{el}'")
        if short == "OT":
            return "연장"
        m = _PERIOD.match(short)
        if not m:
            return ""
        n = int(m.group(2))
        if m.group(1) == "IN":                              # 원정만 그 회 점수가 있으면 초, 홈까지 있으면 말
            col = str(n) if n <= 9 else "extra"           # 10회부터는 'extra' 한 칸 (실측 응답의 innings 키)
            inn = lambda side: ((((x.get("scores") or {}).get(side) or {}).get("innings")) or {}).get(col)  # noqa: E731
            return f"{n}회{'말' if inn('home') is not None else '초'}"
        if m.group(1) == "Q":
            timer = st.get("timer")
            return f"{n}쿼터" + (f" {timer}'" if timer not in (None, "") else "")
        per = ((x.get("periods") or {}).get(_SET_KEYS[n - 1]) or {}) if 1 <= n <= 5 else {}
        pts = f" {per.get('home')}-{per.get('away')}" if per.get("home") is not None else ""
        return f"{n}세트{pts}"

    # ── Provider ─────────────────────────────────────────
    def _src(self, sport: str, d: date) -> str:
        return f"aps:{sport}:{d.isoformat()}"

    def _games(self, lg: League, items: list, src: str, d: date | None = None) -> list[Game]:
        sport, lid = lg.aps
        out = []
        for x in items:
            if ((x.get("league") or {}).get("id")) != lid:
                continue
            season = (x.get("league") or {}).get("season")
            if season is not None:
                self._season[lg.code] = season
            g = self.parse(lg, sport, x, src)
            if g and (d is None or kst_day(g.start) == d):
                out.append(g)
        return sorted(out, key=lambda g: (g.start, g.key))

    async def day(self, lg: League, d: date, only: set[str] | None = None) -> list[Game]:
        sport = lg.aps[0]
        src = self._src(sport, d)
        if only is not None and src not in only:
            return []
        # 자동 알림 폴링(BACKGROUND)은 명령용 예비분을 남김. 사람 명령·AI 도구는 예비분까지.
        return self._games(lg, await self._raw_day(sport, d, essential=not BACKGROUND.get()), src, d)

    async def standings(self, lg: League) -> list[Row]:
        # 순위는 season 이 꼭 필요 → 무료 등급은 이번 시즌 불가. 유료 키가 생기면 실제 응답 샘플을 받아 구현 (추측 파싱 금지).
        raise SportsError(f"{lg.name} 순위는 아직 못 봐요 (경기 일정·점수·알림은 돼요).")

    async def team_games(self, lg: League, team: str) -> list[Game]:
        sport = lg.aps[0]
        today = datetime.fromtimestamp(self.clock(), KST).date()
        span = 1 if self.free or self.daily < 1000 else 7   # 유료면 앞뒤 7일
        games: dict[str, Game] = {}
        got = False
        for i in range(-span, span + 1):
            d = today + timedelta(days=i)
            if not self._in_window(d):
                continue
            try:
                items = await self._raw_day(sport, d)
            except SportsError:
                continue
            got = True
            for g in self._games(lg, items, self._src(sport, d), d):
                if same_team(g.home, team, lg.code) or same_team(g.away, team, lg.code):
                    games[g.key] = g
        if not got:
            raise SportsError("경기 정보를 가져오지 못했어요. 잠시 후 다시 해주세요.")
        return sorted(games.values(), key=lambda g: g.start)
