"""⚽ 스포츠 (sodam/sports): python tests/run_all.py sports

실제 요청 2026-09-29: '스포츠봇 되긴 하는데 축구만 돼서' (TheSportsDB 무료 키 = 검색 1개·일정 3개·영어 팀명만) +
방 관리자 '자동 알림 되는 스포츠봇'. 네트워크 없음: tests/fixtures/sports/*.json = 2026-09-29 실제 응답을 줄인 것.
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import commands, menu, tools
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.sports import Sports, alerts
from sodam.sports.alerts import Snap, diff, in_quiet
from sodam.sports.leagues import LEAGUES, find_league, find_team, ko_name, same_team
from sodam.sports.providers import ESPN, KST, Game, Naver, Provider, SportsDB, espn_state
from sodam.tools import ToolCtx

test, run_all = runner()
DATA = Path(__file__).parent / "fixtures" / "sports"
CHAT, CHAT2 = -100777, -100778
ADMIN, MEMBER = fake_user(10, "관리자"), fake_user(20, "멤버")


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=KST).timestamp())


class FakeFetch:
    """URL → 저장해 둔 실제 응답. 모르는 URL 은 빈 응답 (소스는 '경기 없음' 으로 봄)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, url, params, headers):
        self.calls.append((url, dict(params)))
        if url.endswith("soccer/eng.1/scoreboard"):
            return load("espn_eng1_scoreboard.json") if params.get("dates") == "20260920" else {"events": []}
        if url.endswith("soccer/uefa.nations/scoreboard"):   # 실제 2026-10-01 네이션스리그 (A매치 기간)
            return load("espn_unl_scoreboard.json") if params.get("dates") == "20261001" else {"events": []}
        if url.endswith("soccer/concacaf.nations.league/scoreboard"):   # 실제 2026-10-02 (미국 날짜) 북중미 네이션스리그, 1경기 진행 중
            return load("espn_concacaf_nl_scoreboard.json") if params.get("dates") == "20261002" else {"events": []}
        if url.endswith("basketball/wnba/scoreboard"):
            return load("espn_wnba_scoreboard.json") if params.get("dates") == "20261002" else {"events": []}
        if url.endswith("baseball/mlb/scoreboard"):
            return load("espn_mlb_scoreboard.json") if params.get("dates") == "20260929" else {"events": []}
        if url.endswith("mma/ufc/scoreboard"):
            return load("espn_ufc_scoreboard.json") if params.get("dates") == "20260929" else {"events": []}
        if url.endswith("soccer/eng.1/standings"):
            return load("espn_eng1_standings.json")
        if url.endswith("basketball/nba/standings"):
            return load("espn_nba_standings.json")
        if url.endswith("soccer/eng.1/teams"):
            return load("espn_eng1_teams.json")
        if url.endswith("/teams/367/schedule"):
            return load("espn_tot_fixture.json" if params.get("fixture") else "espn_tot_schedule.json")
        if "sports.naver.com/schedule/games" in url and params.get("categoryId") == "asiangames2026":
            assert params.get("size", 0) >= 500, "종합대회는 하루 수백 경기 → 크게 받아야 축구·배구가 안 잘림"
            return load("naver_asiangames_20261002.json") if params.get("fromDate") == "2026-10-02" else {"result": {"games": []}}
        if "sports.naver.com/schedule/games" in url:
            assert "upperCategoryId" not in params and headers.get("Referer"), "네이버는 categoryId 만 + Referer"
            return load("naver_kbo_games.json") if params.get("categoryId") == "kbo" else {"result": {"games": []}}
        if "sports.naver.com/statistics/categories/kbo" in url:
            return load("naver_kbo_standings.json")
        if "sports.naver.com/statistics/categories/kleague" in url:
            return load("naver_kleague_standings.json")
        return {}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def setup(now, naver=False, admins=(ADMIN.id,)):
    db = await make_db()
    svc = await make_svc(db, admins=admins)
    await db.ensure_chat(CHAT, "스포츠방")
    await db.ensure_chat(CHAT2, "둘째방")
    fetch, clock = FakeFetch(), Clock(now)
    svc.sports = Sports("123", db, svc.cfg.tz, fetch=fetch, naver=naver, clock=clock)
    return svc, db, fetch, clock


async def cmd(svc, text, user=ADMIN, chat=CHAT, role=None):
    parsed = commands.parse(text, "sodambot")
    cmd_, args, argstr = parsed
    msg = FakeMsg(chat, user, text)
    role = role if role is not None else (Role.ADMIN if user.id == ADMIN.id else Role.MEMBER)
    await commands.dispatch(CmdCtx(svc, FakeBot(), msg, chat, user, role, args, argstr), cmd_)
    return msg.replies[-1] if msg.replies else ""


# ── 소스 파싱 (실제 응답) ──────────────────────────────────
@test
async def espn_scoreboard_parses_scores_state_and_scorers():
    games = [ESPN(None).parse_event(LEAGUES["epl"], e) for e in load("espn_eng1_scoreboard.json")["events"]]
    city = next(g for g in games if g.home == "Manchester City")
    assert (city.home_score, city.away_score, city.state, city.away) == (5, 3, "post", "Sunderland"), city
    assert len(city.goals) == 8 and city.goals[-1] == ("81'", "home", "Erling Haaland", ""), city.goals
    bou = next(g for g in games if g.away == "Liverpool")
    assert bou.goals == (("57'", "away", "Alexander Isak", ""),) and bou.key == "espn:401879276"
    mlb = [ESPN(None).parse_event(LEAGUES["mlb"], e) for e in load("espn_mlb_scoreboard.json")["events"]]
    assert all(g.state == "pre" and g.home_score is None for g in mlb), "시작 전 '0' 점수는 점수 없음으로"
    ufc = ESPN(None).parse_event(LEAGUES["ufc"], load("espn_ufc_scoreboard.json")["events"][0])
    assert ufc.title.startswith("Dana White") and not ufc.home, ufc


@test
async def national_team_games_show_in_soccer_during_international_break():
    # 실제 2026-10-01 베베방: '.스포츠 내일 축구' → '축구 경기가 없어요' (클럽 리그만 봐서). 네이션스리그 경기가 있었음
    svc, db, fetch, clock = await setup(ts(2026, 10, 1, 23, 36))
    out = await cmd(svc, ".스포츠 내일 축구")
    assert "UEFA 네이션스리그" in out and "아제르바이잔" in out and "리히텐슈타인" in out and "독일" in out, out
    assert "01:00" in out, out                                     # 16:00Z = 한국 10/2 01:00
    out = await cmd(svc, ".스포츠 내일 네이션스리그")
    assert "덴마크" in out and "포르투갈" in out, out
    assert find_league("a매치").code == "friendly" and find_league("국대").code == "friendly"
    assert ko_name("South Korea") == "대한민국" and ko_name("Liverpool") == "리버풀"


@test
async def concacaf_nations_wnba_and_hockey_clock_requested_2026_10_03():
    """방 요청 2026-10-03: 하키·여자농구·북중미 네이션스리그 — 시간과 지금 점수."""
    from sodam.sports.providers import espn_detail
    svc, db, fetch, clock = await setup(ts(2026, 10, 3, 4, 55))
    out = await cmd(svc, ".스포츠 북중미네이션스리그")
    assert "북중미 네이션스리그" in out and "🔴 54&#x27; 세인트루시아 2-0 과들루프" in out, out
    assert "쿠바" in out and "06:00" in out, out                       # 21:00Z = 한국 06:00
    out = await cmd(svc, ".스포츠 여자농구")
    assert "WNBA" in out and "댈러스 윙스" in out and "골든스테이트 발키리스" in out and "10:00" in out, out
    assert find_league("콘카카프").code == "concacaf_nl" and find_league("wnba").code == "wnba"
    assert find_league("하키") is None and find_league("nhl").code == "nhl"
    st = lambda sd: {"type": {"name": "STATUS_IN_PROGRESS", "shortDetail": sd}}   # noqa: E731
    assert espn_detail(st("12:34 - 2nd"), "hockey") == "2피리어드 12:34"
    assert espn_detail(st("End of 1st"), "hockey") == "1피리어드 끝"
    assert espn_detail(st("3:21 - OT"), "hockey") == "연장 3:21"
    assert espn_detail(st("5:02 - 4th"), "basketball") == "4쿼터 5:02"
    assert espn_detail(st("Bot 7th"), "baseball") == "7회말"


@test
async def asian_games_and_afc_show_by_title_and_alert_start_end():
    # 실제 2026-10-02 베베방 "축구는 피파아시안컵 배구도 아시아" → 아시안게임(네이버, 팀·점수 없이 제목만) + AFC(ESPN)
    svc, db, fetch, clock = await setup(ts(2026, 10, 2, 18, 40), naver=True)
    out = await cmd(svc, ".스포츠 아시안게임 배구")
    assert "남자 준결승" in out and "19:20" in out and "동메달전" in out, out      # 배구 + 비치발리볼
    assert "kg급" not in out and "0:0" not in out, out                          # 유도는 거르고 0:0 점수는 안 보임
    out = await cmd(svc, ".스포츠 축구")
    assert "아시안게임 축구" in out and "여자 금메달전" in out and "19:30" in out, out
    assert "AFC 아시안컵" not in out, "경기 없는 리그는 안 보임"
    asked = {u.split("/sports/")[-1] for u, _ in fetch.calls if "espn" in u}
    assert {"soccer/afc.asian.cup/scoreboard", "soccer/afc.champions/scoreboard"} <= asked, asked
    assert find_league("피파아시안컵").code == "asiancup" and find_league("아챔").code == "acl"
    assert find_league("아시안게임").code == "ag_volley" and find_league("아겜축구").code == "ag_soccer"
    # 알림: 제목뿐인 경기도 시작·종료 (점수 없음 → 골·점수 알림 없음)
    g = await svc.sports.feed.day(LEAGUES["ag_volley"], datetime(2026, 10, 2).date())
    semi = next(x for x in g if x.title == "남자 준결승")
    assert semi.home_score is None and semi.state == "pre"
    done = next(x for x in g if x.title == "남자 준결승 36경기")
    assert done.state == "post" and done.home_score is None and done.away_score is None, "종합대회 0:0 은 점수 아님"
    from dataclasses import replace
    live = replace(semi, state="in")
    [ev] = diff(Snap("pre", None, None, 0), live)
    assert "경기 시작" in ev.text and "남자 준결승" in ev.text and "아시안게임 배구" in ev.text, ev.text
    [ev] = diff(Snap("in", None, None, 0), replace(semi, state="post"))
    assert "경기 종료" in ev.text and "남자 준결승" in ev.text, ev.text


@test
async def espn_status_names_beat_state():
    st = lambda name, state, done=False: {"type": {"name": name, "state": state, "completed": done}}  # noqa: E731
    assert espn_state(st("STATUS_POSTPONED", "in")) == "postponed", "연기가 'in' 으로 와도 연기 (bainluck#3397 사례)"
    assert espn_state(st("STATUS_CANCELED", "post")) == "cancel"
    assert espn_state(st("STATUS_SUSPENDED", "in")) == "suspended"
    assert espn_state(st("STATUS_FULL_TIME", "post", True)) == "post"
    assert espn_state(st("STATUS_FIRST_HALF", "in")) == "in" and espn_state(st("STATUS_SCHEDULED", "pre")) == "pre"


@test
async def naver_parses_korean_games_and_status_codes():
    n = Naver(None, on=True)
    games = [n.parse_game(LEAGUES["kbo"], g) for g in load("naver_kbo_games.json")["result"]["games"]]
    assert games[0].state == "cancel" and games[0].home == "KIA", games[0]
    done = games[1]
    assert (done.home, done.away, done.home_score, done.away_score, done.state) == ("롯데", "한화", 2, 6, "post")
    base = load("naver_kbo_games.json")["result"]["games"][1] | {"cancel": False}
    assert n.parse_game(LEAGUES["kbo"], base | {"statusCode": "READY"}).state == "pre"
    assert n.parse_game(LEAGUES["kbo"], base | {"statusCode": "STARTED", "statusInfo": "5회초"}).detail == "5회초"
    assert n.parse_game(LEAGUES["kbo"], base | {"statusCode": "ENDED"}).state == "post"
    assert not Naver(None).enabled(), "네이버는 기본 꺼짐 (SPORTS_NAVER=1 일 때만)"


@test
async def sportsdb_only_with_paid_key():
    assert not SportsDB(None, "123").enabled() and not SportsDB(None, "").enabled() and SportsDB(None, "abc").enabled()
    g = SportsDB(None, "k").parse_event(LEAGUES["epl"], {"idEvent": "1", "strTimestamp": "2026-09-20T13:00:00",
                                                        "strHomeTeam": "Liverpool", "strAwayTeam": "Arsenal",
                                                        "intHomeScore": "2", "intAwayScore": "1", "strStatus": "FT"})
    assert (g.state, g.home_score) == ("post", 2)


# ── 별칭 ────────────────────────────────────────────────
@test
async def korean_aliases_find_leagues_and_teams():
    want = {"토트넘": "Tottenham Hotspur", "맨유": "Manchester United", "맨시티": "Manchester City", "레알": "Real Madrid",
            "바르사": "Barcelona", "다저스": "Los Angeles Dodgers", "레이커스": "Los Angeles Lakers", "기아": "KIA",
            "엘지": "LG", "Tottenham": "Tottenham Hotspur", "인터밀란": "Internazionale"}
    for q, src in want.items():
        assert find_team(q) and find_team(q).src == src, (q, find_team(q))
    assert {q: find_league(q).code for q in ("챔스", "프리미어리그", "크보", "라리가", "NBA", "k리그")} == \
        {"챔스": "ucl", "프리미어리그": "epl", "크보": "kbo", "라리가": "laliga", "NBA": "nba", "k리그": "kleague"}
    assert ko_name("Manchester City") == "맨시티" and ko_name("Somebody FC") == "Somebody FC"
    assert same_team("Tottenham Hotspur", "Tottenham Hotspur") and not same_team("Manchester City", "Manchester United")
    assert find_team("아무팀") is None


# ── 명령 ────────────────────────────────────────────────
@test
async def today_command_shows_korean_names_kst_and_scores():
    svc, db, fetch, _ = await setup(ts(2026, 9, 20, 23, 0))
    out = await cmd(svc, ".스포츠 오늘 EPL")
    assert "맨시티 5-3 선덜랜드" in out and "본머스 0-1 리버풀" in out and "EPL" in out, out
    dates = sorted(p["dates"] for u, p in fetch.calls if "eng.1/scoreboard" in u)
    assert dates == ["20260919", "20260920"], "한국 하루 = ESPN(미국 동부) 두 날"
    n = len(fetch.calls)
    await cmd(svc, ".스포츠 오늘 프리미어리그", user=MEMBER)
    assert len(fetch.calls) == n, "같은 리그·날짜는 공유 캐시 (다시 안 부름)"
    out = await cmd(svc, ".스포츠 내일 EPL")
    assert "경기가 없어요" in out, out


@test
async def mlb_kst_times_and_unknown_query():
    svc, *_ = await setup(ts(2026, 9, 30, 9, 0))
    out = await cmd(svc, ".스포츠 MLB")
    # 18:00Z = 한국 03:00 (9/30), 원정 먼저
    assert "03:00 필라델피아 vs 애틀랜타" in out, out
    assert "못 찾았어요" in await cmd(svc, ".스포츠 오늘 쿼디치")


@test
async def standings_and_team_commands():
    svc, *_ = await setup(ts(2026, 9, 29, 12, 0))
    out = await cmd(svc, ".스포츠 순위 EPL")
    assert out.splitlines()[1].startswith("1. 맨시티 5승 0무 0패 · 15점"), out
    out = await cmd(svc, ".스포츠 순위 NBA")
    assert "Eastern Conference" in out and "Western Conference" in out, out
    out = await cmd(svc, ".스포츠 팀 토트넘")
    assert "토트넘" in out and "최근 결과" in out and "토트넘 2-3 애스턴 빌라" in out, out
    assert "다음 경기" in out and "맨유 vs 토트넘" in out and "10/11 01:30" in out, out   # 10-10 16:30Z = 한국 10/11 01:30
    assert "순위" in await cmd(svc, ".스포츠 순위")  # 리그 없으면 사용법


@test
async def korean_leagues_need_naver_switch():
    svc, *_ = await setup(ts(2026, 9, 27, 21, 0))
    out = await cmd(svc, ".스포츠 오늘 KBO")
    assert "준비 중" in out, out
    assert "준비 중" in await cmd(svc, ".스포츠 구독 KBO")
    svc, _, fetch, _ = await setup(ts(2026, 9, 27, 21, 0), naver=True)
    out = await cmd(svc, ".스포츠 오늘 KBO")
    assert "롯데 2-6 한화" not in out and "한화 6-2 롯데" in out and "(취소)" in out, out  # 야구는 원정 먼저
    out = await cmd(svc, ".스포츠 순위 KBO")
    assert "1. KT 82승 49패 4무 · 승률 .626" in out, out
    out = await cmd(svc, ".스포츠 순위 K리그")
    assert "1. FC서울 19승 5무 6패 · 62점" in out, out


@test
async def follow_commands_admin_only():
    svc, db, *_ = await setup(ts(2026, 9, 29, 12, 0))
    assert "관리자만" in await cmd(svc, ".스포츠 구독 EPL", user=MEMBER)
    out = await cmd(svc, ".스포츠 구독 EPL")
    assert "EPL 경기 알림을 켰어요" in out, out
    assert "이미" in await cmd(svc, ".스포츠 구독 프리미어리그")
    assert "토트넘 (EPL)" in await cmd(svc, ".스포츠 구독 팀 토트넘")
    out = await cmd(svc, ".스포츠 목록", user=MEMBER)
    assert "EPL" in out and "토트넘 (EPL)" in out and "⚽ 골까지" in out, out
    assert "종목 말고 리그" in await cmd(svc, ".스포츠 구독 축구")
    assert "껐어요" in await cmd(svc, ".스포츠 해제 토트넘")
    rows = await db._all("SELECT league, team FROM sports_follow WHERE chat_id=?", (CHAT,))
    assert [(r["league"], r["team"]) for r in rows] == [("epl", "")]
    assert "결과만" in await cmd(svc, ".스포츠 알림종류 결과만") and (await db.get_settings(CHAT))["sports_alerts"] == "final"
    assert "관리자만" in await cmd(svc, ".스포츠 조용 끔", user=MEMBER)
    await cmd(svc, ".스포츠 조용 23~07")
    assert (await db.get_settings(CHAT))["sports_quiet"] == "23-07"


@test
async def ai_tool_answers_korean_questions():
    svc, *_ = await setup(ts(2026, 9, 20, 23, 0))
    c = ToolCtx(svc, FakeBot(), CHAT, MEMBER, Role.MEMBER, {})
    out = await tools.t_sports(c, {"action": "today", "query": "EPL"})
    assert "맨시티 5-3 선덜랜드" in out and "<b>" not in out, out
    out = await tools.t_sports(c, {"action": "standings", "query": "NBA"})
    assert "NBA 순위" in out, out
    out = await tools.t_sports(c, {"action": "team", "query": "토트넘"})
    assert "토트넘 2-3 애스턴 빌라" in out, out
    tool = next(t for t in tools.TOOLS if t.name == "sports")
    assert set(tool.params["action"]["enum"]) >= {"today", "live", "standings", "team"}
    assert "배당" in tool.description


# ── 이벤트 감지 ────────────────────────────────────────────
def game(state="pre", hs=None, as_=None, goals=(), league="epl", home="Tottenham Hotspur", away="Arsenal", start=None):
    return Game("espn:1", league, start or ts(2026, 9, 29, 20), home, away, hs, as_, state, "57'" if state == "in" else "",
                tuple(goals), "20260929")


def snap(g):
    return Snap(g.state, g.home_score, g.away_score, len(g.goals))


@test
async def diff_detects_kickoff_goal_final_postpone():
    pre, live0 = game(), game("in", 0, 0)
    assert diff(None, live0) == [], "처음 본 경기는 조용히"
    [e] = diff(snap(pre), live0)
    assert e.kind == "start" and "경기 시작 [EPL] 토트넘 vs 아스널" in e.text
    one = game("in", 1, 0, goals=[("57'", "home", "Son Heung-min", "")])
    [e] = diff(snap(live0), one)
    assert e.kind == "goal" and e.dedupe == "goal:1-0:0" and "토트넘 1-0 아스널" in e.text and "57' Son Heung-min" in e.text, e
    assert diff(snap(one), one) == [], "같은 상태는 이벤트 없음"
    assert diff(snap(one), game("in", 0, 0)) == [], "줄어든 점수 한 틱은 보류 (ESPN 이 잠깐 틀리게 줄 때)"
    [e] = diff(Snap("in", 1, 0, 1, 0, (0, 0)), game("in", 0, 0))
    assert e.kind == "undo" and "득점 취소" in e.text and e.dedupe == "undo:0-0:1"
    [e] = diff(snap(one), game("post", 2, 1))
    assert e.kind == "final" and "경기 종료 [EPL] 토트넘 2-1 아스널" in e.text, "종료는 골보다 종료 한 줄"
    [e] = diff(snap(pre), game("postponed"))
    assert e.kind == "cancel" and e.dedupe == "postponed" and "경기 연기" in e.text
    kbo0 = game("in", 0, 0, league="kbo", home="삼성", away="한화")
    [e] = diff(snap(kbo0), game("in", 2, 0, league="kbo", home="삼성", away="한화"))
    assert e.kind == "score" and "한화 0-2 삼성" in e.text, "야구는 골이 아니라 점수 변화 (원정 먼저)"


@test
async def quiet_hours_cross_midnight():
    assert in_quiet("01-07", 1) and in_quiet("01-07", 6) and not in_quiet("01-07", 7) and not in_quiet("01-07", 0)
    assert in_quiet("23-07", 23) and in_quiet("23-07", 3) and not in_quiet("23-07", 12) and not in_quiet("off", 3)


# ── 자동 알림 엔진 (가짜 소스) ────────────────────────────────
class Script(Provider):
    """리그마다 '지금 경기 목록'을 테스트가 바꿔 끼움."""
    name = "script"

    def __init__(self):
        self.games = {}
        self.calls = []

    def supports(self, lg):
        return lg.code in ("epl", "kbo", "mlb", "ucl")

    async def day(self, lg, d, only=None):
        self.calls.append((lg.code, d, frozenset(only) if only else None))
        return [g for g in self.games.get(lg.code, []) if datetime.fromtimestamp(g.start, KST).date() == d]


async def engine(now, follows=((CHAT, "epl", ""),), **settings):
    db = await make_db()
    svc = await make_svc(db)
    clock = Clock(now)
    sp = Sports("123", db, svc.cfg.tz, fetch=FakeFetch(), clock=clock)
    src = Script()
    sp.feed.providers = [src]
    for cid, lg, team in follows:
        await db.ensure_chat(cid, "방")
        await sp.alerts.follow(cid, lg, team, team or lg, 1)
        for k, v in settings.items():
            await db.set_setting(cid, k, v)
    svc.sports = sp
    return SimpleNamespace(svc=svc, db=db, sp=sp, src=src, clock=clock, bot=FakeBot())


def msgs(e, chat=CHAT):
    return [c[2] for c in e.bot.named("send_message") if c[1] == chat]


KICK = ts(2026, 9, 29, 20, 0)   # 한국 저녁 8시 (조용한 시간 아님)


@test
async def engine_seeds_then_alerts_kickoff_goal_final_once():
    e = await engine(KICK - 3600)
    e.src.games["epl"] = [game(start=KICK)]
    assert await e.sp.run_alerts(e.bot) == 0 and not msgs(e), "처음엔 저장만"
    e.clock.t = KICK + 30
    e.src.games["epl"] = [game("in", 0, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert ["🔔 경기 시작 [EPL] 토트넘 vs 아스널"] == msgs(e), msgs(e)
    e.clock.t += 60
    e.src.games["epl"] = [game("in", 1, 0, goals=[("12'", "home", "Son", "")], start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert "⚽ 골! [EPL] 토트넘 1-0 아스널 · 12' Son" in msgs(e)[-1]
    e.clock.t += 60
    await e.sp.run_alerts(e.bot)
    assert len(msgs(e)) == 2, "같은 점수 다시 받아도 한 번"
    e.clock.t += 60
    e.src.games["epl"] = [game("post", 1, 0, goals=[("12'", "home", "Son", "")], start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert "🏁 경기 종료 [EPL] 토트넘 1-0 아스널" in msgs(e)[-1] and len(msgs(e)) == 3
    rows = await e.db._all("SELECT kind FROM sports_alert_sent WHERE chat_id=? ORDER BY kind", (CHAT,))
    assert [r["kind"] for r in rows] == ["final", "goal:1-0:0", "start"]


@test
async def restart_seeds_silently_and_dedupe_survives():
    e = await engine(KICK + 60)
    e.src.games["epl"] = [game("in", 1, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert not msgs(e), "재시작 직후 진행 중 경기 = 조용히"
    e.clock.t += 60
    e.src.games["epl"] = [game("in", 2, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert len(msgs(e)) == 1
    again = Sports("123", e.db, e.svc.cfg.tz, fetch=FakeFetch(), clock=e.clock)   # 봇 재시작
    again.feed.providers = [e.src]
    await again.run_alerts(e.bot)
    for sc in ((1, 0), (2, 0)):                            # ESPN 이 한 틱만 1-0 으로 틀리게 줌 → 아무 알림 없음
        e.clock.t += 60
        e.src.games["epl"] = [game("in", *sc, start=KICK)]
        await again.run_alerts(e.bot)
    assert len(msgs(e)) == 1, msgs(e)
    e.clock.t += 60
    e.src.games["epl"] = [game("in", 2, 0, start=KICK)]
    await again.run_alerts(e.bot)
    assert len(msgs(e)) == 1, "이미 보낸 골은 재시작 뒤에도 한 번 (DB 중복 방지)"


@test
async def rescored_same_score_after_confirmed_undo_alerts_again():
    """감사 2026-09-30: 1-1 골 → VAR 취소 1-0 → 다시 1-1 이면 두 번째 골도 알림 (예전엔 goal:1-1 중복으로 버림)."""
    e = await engine(KICK + 60)
    e.src.games["epl"] = [game("in", 1, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    for sc in ((1, 1), (1, 0), (1, 0), (1, 1)):
        e.clock.t += 60
        e.src.games["epl"] = [game("in", *sc, start=KICK)]
        await e.sp.run_alerts(e.bot)
    texts = msgs(e)
    assert sum("골!" in t for t in texts) == 2 and sum("득점 취소" in t for t in texts) == 1, texts


@test
async def missing_score_tick_keeps_previous_score():
    """점수가 빈 채로 온 틱 뒤에도 그 사이 골을 알림 (예전엔 스냅샷에 None 이 들어가 골이 영영 안 감)."""
    e = await engine(KICK + 60)
    e.src.games["epl"] = [game("in", 0, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    import dataclasses
    blank = dataclasses.replace(game("in", 0, 0, start=KICK), home_score=None, away_score=None)
    for g in (blank, game("in", 1, 0, start=KICK)):
        e.clock.t += 60
        e.src.games["epl"] = [g]
        await e.sp.run_alerts(e.bot)
    assert any("골!" in t and "1-0" in t for t in msgs(e)), msgs(e)


@test
async def hourly_cap_keeps_final_for_later_and_team_follow_sees_cups():
    e = await engine(KICK + 60, follows=((CHAT, "epl", "Tottenham Hotspur"),))
    q = e.sp.alerts.sent_times.setdefault(CHAT, alerts.deque())
    q.extend([e.clock.t] * alerts.MAX_PER_HOUR)                              # 이번 시간 상한 다 참
    e.src.games["ucl"] = [game("in", 1, 0, league="ucl", start=KICK)]       # 토트넘 챔스 경기
    await e.sp.run_alerts(e.bot)
    e.clock.t += 60
    e.src.games["ucl"] = [game("post", 1, 0, league="ucl", start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert not msgs(e), "상한에 걸려 못 보냄"
    held = await e.db._all("SELECT * FROM sports_held WHERE chat_id=?", (CHAT,))
    assert len(held) == 1 and "경기 종료" in held[0]["text"], "결과는 버리지 않고 모아 둠"
    e.clock.t += 3700                                                       # 한 시간 지나 상한 풀림
    await e.sp.run_alerts(e.bot)
    assert any("경기 종료" in t and "챔스" in t for t in msgs(e)), msgs(e)
    assert not await e.db._all("SELECT * FROM sports_held WHERE chat_id=?", (CHAT,)), "보낸 뒤에만 지움"


@test
async def failed_schedule_fetch_backs_off():
    e = await engine(KICK - 6 * 3600)

    async def boom(lg, d, only=None):
        e.src.calls.append((lg.code, d, None))
        raise alerts.SportsError("down")
    e.src.day = boom
    for _ in range(6):                                                     # 30초 틱 6번 = 3분
        await e.sp.run_alerts(e.bot)
        e.clock.t += 30
    assert len(e.src.calls) == 1, f"장애 때 30초마다 두드리지 않음: {len(e.src.calls)}"
    e.clock.t += alerts.RETRY_EVERY
    await e.sp.run_alerts(e.bot)
    assert len(e.src.calls) == 2


@test
async def one_fetch_per_league_for_many_rooms_and_cadence():
    e = await engine(KICK - 6 * 3600, follows=((CHAT, "epl", ""), (CHAT2, "epl", ""), (CHAT2, "epl", "Tottenham Hotspur")))
    e.src.games["epl"] = [game(start=KICK)]
    epl = lambda: [c for c in e.src.calls if c[0] == "epl"]   # 팀 구독은 챔스도 따로 받음 → epl 요청만 셈
    await e.sp.run_alerts(e.bot)
    assert len(epl()) == 1, "두 방 + 팀 구독이어도 리그 한 번"
    e.clock.t += 30
    await e.sp.run_alerts(e.bot)
    e.clock.t = KICK - 20 * 60         # 경기 20분 전: 아직 일정 모드
    await e.sp.run_alerts(e.bot)
    assert len(epl()) == 1, "경기 없는 시간엔 6시간마다만"
    e.clock.t = KICK - 10 * 60         # 15분 전 창 → 60초 폴링
    await e.sp.run_alerts(e.bot)
    e.clock.t += 30
    await e.sp.run_alerts(e.bot)
    e.clock.t += 31
    await e.sp.run_alerts(e.bot)
    assert len(epl()) == 3, epl()
    assert epl()[-1][2] == frozenset({"20260929"}), "라이브 땐 경기가 있는 요청만"
    e.src.games["epl"] = [game("in", 0, 0, start=KICK)]
    e.clock.t += 61
    await e.sp.run_alerts(e.bot)
    assert len(msgs(e, CHAT)) == 1 and len(msgs(e, CHAT2)) == 1, "방마다 한 통 (리그·팀 구독 겹쳐도)"


@test
async def team_follow_filters_games():
    e = await engine(KICK - 60, follows=((CHAT, "epl", "Manchester City"),))
    e.src.games["epl"] = [game(start=KICK)]
    await e.sp.run_alerts(e.bot)
    e.clock.t = KICK + 30
    e.src.games["epl"] = [game("in", 0, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert not msgs(e), "맨시티만 구독 → 토트넘 경기 알림 없음"


@test
async def quiet_hours_drop_goals_hold_final_until_morning():
    night = ts(2026, 9, 30, 2, 0)
    e = await engine(night - 3600)
    e.src.games["epl"] = [game(start=night)]
    await e.sp.run_alerts(e.bot)
    for t, g in ((night + 30, game("in", 0, 0, start=night)), (night + 120, game("in", 1, 0, start=night)),
                 (night + 7200, game("post", 1, 0, start=night))):
        e.clock.t = t
        e.src.games["epl"] = [g]
        await e.sp.run_alerts(e.bot)
    assert not msgs(e), "01~07시엔 아무것도 안 보냄"
    held = await e.db._all("SELECT kind FROM sports_held WHERE chat_id=?", (CHAT,))
    assert [h["kind"] for h in held] == ["final"]
    e.clock.t = ts(2026, 9, 30, 7, 0)
    await e.sp.run_alerts(e.bot)
    [m] = msgs(e)
    assert "밤사이 경기 결과" in m and "토트넘 1-0 아스널" in m and "골!" not in m, m
    assert not await e.db._all("SELECT * FROM sports_held")
    e.clock.t += 60
    await e.sp.run_alerts(e.bot)
    assert len(msgs(e)) == 1


@test
async def gating_enabled_and_paid():
    e = await engine(KICK - 60, sports_enabled=False)
    e.src.games["epl"] = [game(start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert not e.src.calls, "꺼진 방만 있으면 받지도 않음"

    async def unpaid(cid):
        return False
    e = await engine(KICK - 60)
    e.src.games["epl"] = [game(start=KICK)]
    await e.sp.run_alerts(e.bot, is_active=unpaid)
    assert not e.src.calls, "이용 기간 아닌 방은 알림 없음"


@test
async def level_setting_and_hourly_cap():
    e = await engine(KICK - 60, sports_alerts="final")
    e.src.games["epl"] = [game(start=KICK)]
    await e.sp.run_alerts(e.bot)
    e.clock.t = KICK + 30
    e.src.games["epl"] = [game("in", 0, 0, start=KICK)]
    await e.sp.run_alerts(e.bot)
    assert not msgs(e), "결과만 → 시작 알림 없음"
    e = await engine(KICK - 60)
    e.src.games["epl"] = [Game(f"espn:{i}", "epl", KICK, "Liverpool", "Arsenal", state="pre") for i in range(15)]
    await e.sp.run_alerts(e.bot)
    for i in range(15):
        e.clock.t = KICK + 61 * (i + 1)
        e.src.games["epl"] = [Game(f"espn:{j}", "epl", KICK, "Liverpool", "Arsenal", 0, 0, "in" if j <= i else "pre")
                              for j in range(15)]
        await e.sp.run_alerts(e.bot)
    assert len(msgs(e)) == alerts.MAX_PER_HOUR, len(msgs(e))


@test
async def prune_after_14_days():
    e = await engine(KICK)
    await e.db._write("INSERT INTO sports_alert_sent VALUES(?, 'g', 'final', ?)", (CHAT, KICK - 15 * 86400))
    await e.db._write("INSERT INTO sports_alert_sent VALUES(?, 'h', 'final', ?)", (CHAT, KICK - 13 * 86400))
    await e.sp.alerts.prune(KICK)
    assert [r["game"] for r in await e.db._all("SELECT game FROM sports_alert_sent")] == ["h"]


# ── 1:1 설정 화면 ─────────────────────────────────────────
async def press(svc, bot, user, data):
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


def buttons(q):
    return [(b.text, b.callback_data) for row in q.kb.inline_keyboard for b in row]


@test
async def panel_adds_league_team_changes_settings_and_deletes():
    svc, db, *_ = await setup(ts(2026, 9, 29, 12, 0))
    bot = FakeBot()
    q = await press(svc, bot, ADMIN, f"m:g:{CHAT}")
    assert ("⚽ 스포츠 알림", f"m:spt:{CHAT}") in buttons(q)
    q = await press(svc, bot, MEMBER, f"m:spt:{CHAT}")
    assert not q.edits and q.answers[-1][1], "멤버는 못 엶"
    q = await press(svc, bot, ADMIN, f"m:sptl:{CHAT}")
    labels = [t for t, _ in buttons(q)]
    assert "EPL" in labels and "KBO" not in labels and "준비 중" in q.edits[-1], "네이버 꺼짐 → 국내 리그 버튼 없음"
    q = await press(svc, bot, ADMIN, f"m:spta:{CHAT}:epl")
    assert ("✅ EPL", f"m:spta:{CHAT}:epl") in buttons(q)
    q = await press(svc, bot, ADMIN, f"m:spta:{CHAT}:kbo")
    assert "고를 수 없는" in q.answers[-1][0]
    q = await press(svc, bot, ADMIN, f"m:in:{CHAT}:sptt")
    msg = FakeMsg(ADMIN.id, ADMIN, "다저스")
    assert await menu.handle_input(svc, bot, msg) and "다저스 (MLB)" in msg.replies[-1], msg.replies
    q = await press(svc, bot, ADMIN, f"m:n:{CHAT}:sports_quiet:off")
    assert (await db.get_settings(CHAT))["sports_quiet"] == "off"
    q = await press(svc, bot, ADMIN, f"m:n:{CHAT}:sports_alerts:all")
    assert (await db.get_settings(CHAT))["sports_alerts"] == "all" and "EPL, 다저스 (MLB)" in q.edits[-1], q.edits[-1]
    dels = [d for t, d in buttons(q) if t.startswith("🗑 다저스")]
    q = await press(svc, bot, ADMIN, dels[0])
    rows = await db._all("SELECT league FROM sports_follow WHERE chat_id=?", (CHAT,))
    assert [r["league"] for r in rows] == ["epl"]
    for _, data in buttons(q):
        assert len(data.encode()) <= 64


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
