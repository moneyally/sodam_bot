"""⚽ API-Sports 연결 (국내 KBO·K리그·KBL·WKBL·V리그 + NPB): python tests/run_all.py sports_apisports

오너 요청 2026-10-01: '스포츠 알림 준비 중이라 떠서' → API-Sports 무료 키. 실측(tests/fixtures/sports/apisports_*.json):
무료 = season 을 붙이면 2022~2024 만 → date 만 주면 이번 시즌·모든 리그가 한 번에 · 날짜는 어제~내일 · 종목마다 하루 100번.
네트워크 없음 — 저장해 둔 실제 응답만.
"""
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from fakes import FakeBot, make_db, make_svc, runner

from sodam.sports import Sports
from sodam.sports.leagues import LEAGUES, canon, find_team, ko_name, same_team
from sodam.sports.providers import APS_RESERVE, KST, APISports, SportsError

test, run_all = runner()
DATA = Path(__file__).parent / "fixtures" / "sports"
CHAT = -100991
KEY = "testkey0123456789abcdef"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=KST).timestamp())


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class ApsFetch:
    """API-Sports 가짜: (종목, 날짜) → 저장한 실제 응답. override 로 특정 응답(오류·라이브)을 바꿔 끼움."""

    FILES = {("baseball", "2026-09-30"): "apisports_baseball_20260930.json",
             ("baseball", "2026-10-01"): "apisports_baseball_20261001.json",
             ("football", "2026-09-30"): "apisports_football_20260930.json",
             ("basketball", "2026-10-01"): "apisports_basketball_20261001.json",
             ("volleyball", "2026-10-01"): "apisports_volleyball_20261001.json"}

    def __init__(self):
        self.calls = []
        self.override = {}

    async def __call__(self, url, params, headers):
        await asyncio.sleep(0)             # 진짜 네트워크처럼 다른 코루틴에 차례를 넘김 (동시 요청 검사용)
        if "api-sports.io" not in url:
            return {"events": []}          # ESPN 등은 이 테스트에서 빈 응답
        sport = url.split("//v")[1].split(".")[1]
        assert headers.get("x-apisports-key") == KEY, headers
        assert "season" not in params and "league" not in params, "무료 등급은 date 만 (season 붙이면 이번 시즌 막힘 — 실측)"
        assert params.get("timezone") == "Asia/Seoul"
        self.calls.append((sport, params["date"]))
        k = (sport, params["date"])
        if k in self.override:
            v = self.override[k]
            return v() if callable(v) else v
        if k in self.FILES:
            return load(self.FILES[k])
        return {"errors": [], "response": []}


def provider(now, daily=100):
    f = ApsFetch()
    return APISports(f, KEY, daily, clock=Clock(now)), f


# ── 파싱 (실제 응답) ──────────────────────────────────────
@test
async def kbo_finished_games_korean_names_scores():
    p, f = provider(ts(2026, 9, 30, 23, 0))
    games = await p.day(LEAGUES["kbo"], datetime(2026, 9, 30).date())
    assert len(games) == 5 and all(g.state == "post" for g in games), games
    g = next(g for g in games if g.key == "aps:baseball:181937")
    assert (g.home, g.away, g.home_score, g.away_score) == ("SSG", "LG", 4, 3), g
    assert g.start == ts(2026, 9, 30, 18, 30) and g.src == "aps:baseball:2026-09-30"
    kbo10 = {"KIA", "LG", "한화", "삼성", "두산", "롯데", "KT", "SSG", "NC", "키움"}
    assert all({x.home, x.away} <= kbo10 for x in games), [(x.home, x.away) for x in games]   # 영어 이름이 남지 않음
    # 같은 날 NPB 는 같은 응답을 나눠 씀 → 요청 1번
    npb = await p.day(LEAGUES["npb"], datetime(2026, 9, 30).date())
    assert npb and all(x.league == "npb" for x in npb) and f.calls == [("baseball", "2026-09-30")], f.calls
    # 라이브 재조회(only)가 다른 날짜 요청만 가리키면 이 날짜는 안 부름
    p._raw.clear()
    assert await p.day(LEAGUES["kbo"], datetime(2026, 10, 1).date(), only={"aps:baseball:2026-09-30"}) == []
    assert f.calls == [("baseball", "2026-09-30")], f.calls


@test
async def concurrent_leagues_share_one_request():
    p, f = provider(ts(2026, 9, 30, 23, 0))
    d = datetime(2026, 9, 30).date()
    a, b, c = await asyncio.gather(p.day(LEAGUES["kbo"], d), p.day(LEAGUES["npb"], d), p.day(LEAGUES["mlb"], d))
    assert a and b and c and f.calls == [("baseball", "2026-09-30")], f.calls


@test
async def kbo_not_started_has_no_score():
    p, _ = provider(ts(2026, 10, 1, 15, 0))
    games = await p.day(LEAGUES["kbo"], datetime(2026, 10, 1).date())
    assert len(games) == 4 and {g.state for g in games} == {"pre"} and all(g.home_score is None for g in games)
    dn = next(g for g in games if g.key == "aps:baseball:181942")
    assert (dn.home, dn.away, dn.start) == ("두산", "NC", ts(2026, 10, 1, 18, 30)), dn


@test
async def football_status_details_pen_aet_cancel_live():
    p, _ = provider(ts(2026, 9, 30, 23, 0))
    lg = LEAGUES["kleague"]
    by = {x["fixture"]["status"]["short"]: x for x in load("apisports_football_20260930.json")["response"]}
    pen = p.parse(lg, "football", by["PEN"])
    assert (pen.state, pen.home_score, pen.away_score, pen.detail) == ("post", 1, 1, "승부차기 8-9"), pen
    assert p.parse(lg, "football", by["AET"]).detail == "연장 끝"
    assert p.parse(lg, "football", by["CANC"]).state == "cancel"
    ns = p.parse(lg, "football", by["NS"])
    assert ns.state == "pre" and ns.home_score is None
    zero = json.loads(json.dumps(by["NS"]))
    zero["goals"] = {"home": 0, "away": 0}          # 시작 전인데 0-0 이 실려 와도 점수로 안 봄 (알림 '0-0' 방지)
    assert p.parse(lg, "football", zero).home_score is None
    canc = json.loads(json.dumps(by["CANC"]))
    canc["goals"] = {"home": 1, "away": 0}
    assert p.parse(lg, "football", canc).home_score is None
    live = [p.parse(lg, "football", x) for x in load("apisports_football_live.json")["response"]]
    assert [(g.state, g.detail, g.home_score, g.away_score) for g in live] == [("in", "24'", 0, 1), ("in", "73'", 0, 4)], live


@test
async def basketball_volleyball_states_and_set_scores():
    p, _ = provider(ts(2026, 10, 1, 12, 0))
    bk = {x["status"]["short"]: x for x in load("apisports_basketball_20261001.json")["response"]}
    aot = p.parse(LEAGUES["kbl"], "basketball", bk["AOT"])
    assert (aot.state, aot.home_score, aot.away_score, aot.detail) == ("post", 87, 92, "연장 끝"), aot
    assert p.parse(LEAGUES["kbl"], "basketball", bk["POST"]).state == "postponed"
    assert p.parse(LEAGUES["kbl"], "basketball", bk["NS"]).home_score is None
    vb = next(x for x in load("apisports_volleyball_20261001.json")["response"] if x["status"]["short"] == "FT")
    g = p.parse(LEAGUES["vleague"], "volleyball", vb)
    assert (g.state, g.home_score, g.away_score) == ("post", 3, 0), g     # 배구 점수 = 세트 수


@test
async def live_period_details_inning_top_bottom_quarter_set():
    p, _ = provider(ts(2026, 10, 1, 19, 0))
    base = next(x for x in load("apisports_baseball_20261001.json")["response"] if x["id"] == 181942)   # KBO 시작 전 (회 점수 비어 있음)
    x = json.loads(json.dumps(base))
    x["status"] = {"long": "Inning 5", "short": "IN5"}
    x["scores"]["away"]["innings"]["5"] = 1
    x["scores"]["away"]["total"], x["scores"]["home"]["total"] = 3, 2
    g = p.parse(LEAGUES["kbo"], "baseball", x)
    assert (g.state, g.detail, g.home_score, g.away_score) == ("in", "5회초", 2, 3), g
    x["scores"]["home"]["innings"]["5"] = 0
    assert p.parse(LEAGUES["kbo"], "baseball", x).detail == "5회말"
    b = json.loads(json.dumps(load("apisports_basketball_20261001.json")["response"][0]))
    b["status"] = {"long": "Quarter 3", "short": "Q3", "timer": "4"}
    assert p.parse(LEAGUES["kbl"], "basketball", b).detail == "3쿼터 4'"
    v = json.loads(json.dumps(load("apisports_volleyball_20261001.json")["response"][0]))
    v["status"] = {"long": "Set 3", "short": "S3"}
    v["periods"]["third"] = {"home": 15, "away": 12}
    gv = p.parse(LEAGUES["vleague"], "volleyball", v)
    assert (gv.state, gv.detail) == ("in", "3세트 15-12"), gv


@test
async def unknown_status_falls_back_by_score():
    p, _ = provider(ts(2026, 10, 1, 19, 0))
    x = json.loads(json.dumps(load("apisports_baseball_20260930.json")["response"][0]))
    x["status"] = {"long": "Weird", "short": "ZZZ"}
    assert p.parse(LEAGUES["kbo"], "baseball", x).state == "in"   # 점수 있음 → 진행 중으로 (알림이 멈추지 않게)
    x["scores"]["home"]["total"] = x["scores"]["away"]["total"] = None
    assert p.parse(LEAGUES["kbo"], "baseball", x).state == "pre"


# ── 한도·캐시·오류 ───────────────────────────────────────
@test
async def free_budget_spreads_requests_until_utc_midnight():
    now = ts(2026, 10, 1, 9, 0)         # UTC 00:00 → 하루 전체가 남음
    p, f = provider(now)
    d = datetime(2026, 10, 1).date()
    await p.day(LEAGUES["kbo"], d)
    gap = p._gap("baseball")
    assert 86400 / (100 - 1 - APS_RESERVE) - 1 <= gap <= 86400 / (100 - 1 - APS_RESERVE) + 1, gap   # ≈ 16분
    p.clock.t = now + gap * 0.9
    await p.day(LEAGUES["kbo"], d)
    assert len(f.calls) == 1, "간격 안이면 다시 안 부름"
    p.clock.t = now + gap + 1
    await p.day(LEAGUES["kbo"], d)
    assert len(f.calls) == 2
    paid, _ = provider(now, daily=7500)
    assert paid._gap("baseball") == 60, "유료 한도면 1분마다"


@test
async def reserve_kept_for_commands_and_exhausted_stops_calls():
    now = ts(2026, 10, 1, 9, 0)
    p, f = provider(now, daily=APS_RESERVE + 1)
    d = datetime(2026, 10, 1).date()
    await p.day(LEAGUES["kbo"], d)                 # 사람 명령 = 예비분까지 씀
    p._raw.clear()
    try:
        await p.day(LEAGUES["kbo"], d, only={"aps:baseball:2026-10-01"})   # 알림 폴링 = 예비분은 남김
        raise AssertionError("예비분을 알림이 쓰면 안 됨")
    except SportsError:
        pass
    assert len(f.calls) == 1
    p2, f2 = provider(now)
    f2.override[("baseball", "2026-10-01")] = {"errors": {"requests": "You have reached the request limit for the day"},
                                               "response": []}
    for _ in range(3):
        try:
            await p2.day(LEAGUES["kbo"], d)
        except SportsError as e:
            assert "한도" in str(e)
        p2._raw.clear()
    assert len(f2.calls) == 1 and p2.remaining("baseball") == 0, f2.calls
    assert p2.remaining("football") > 0, "한도는 종목마다"


@test
async def free_plan_date_window_saves_requests():
    now = ts(2026, 10, 1, 15, 0)
    p, f = provider(now, daily=100)
    try:
        await p.day(LEAGUES["kbo"], datetime(2026, 10, 5).date())
        raise AssertionError
    except SportsError as e:
        assert "어제~내일" in str(e)
    assert f.calls == [], "무료 등급 날짜 창 밖은 요청을 안 씀"
    paid, f2 = provider(now, daily=7500)
    f2.override[("baseball", "2026-10-05")] = load("apisports_error_plan_date.json")
    try:
        await paid.day(LEAGUES["kbo"], datetime(2026, 10, 5).date())
    except SportsError:
        pass
    assert paid.free and len(f2.calls) == 1, "유료라 믿었는데 'Free plans' 오류 → 무료로 기억"
    try:
        await paid.day(LEAGUES["kbo"], datetime(2026, 10, 6).date())
    except SportsError:
        pass
    assert len(f2.calls) == 1


@test
async def suspended_account_disables_until_key_changes():
    now = ts(2026, 10, 1, 15, 0)
    f = ApsFetch()
    f.override[("baseball", "2026-10-01")] = load("apisports_error_suspended.json")
    p = APISports(f, None, 100, clock=Clock(now))
    os.environ["APISPORTS_KEY"] = KEY
    try:
        assert p.enabled()
        try:
            await p.day(LEAGUES["kbo"], datetime(2026, 10, 1).date())
        except SportsError as e:
            assert "운영자" in str(e)
        assert not p.enabled() and "suspended" in p.blocked
        os.environ["APISPORTS_KEY"] = "otherkey0123456789abc"
        assert p.enabled(), "키를 바꾸면 (오너 '.키') 재시작 없이 다시 시도"
        os.environ.pop("APISPORTS_KEY")
        assert not p.enabled(), "키 없으면 꺼짐"
    finally:
        os.environ.pop("APISPORTS_KEY", None)


@test
async def transient_error_serves_last_good_data():
    now = ts(2026, 10, 1, 9, 0)
    p, f = provider(now)
    d = datetime(2026, 10, 1).date()
    first = await p.day(LEAGUES["kbo"], d)
    f.override[("baseball", "2026-10-01")] = {"errors": {"rateLimit": "Too many requests"}, "response": []}
    p.clock.t = now + p._gap("baseball") + 1
    again = await p.day(LEAGUES["kbo"], d)
    assert [g.key for g in again] == [g.key for g in first] and len(f.calls) == 2


@test
async def test_sports_never_uses_server_env_key():
    os.environ["APISPORTS_KEY"] = KEY
    try:
        db = await make_db()
        sp = Sports("123", db, KST, fetch=ApsFetch(), clock=Clock(ts(2026, 10, 1, 12)))
        assert not sp.feed.available(LEAGUES["kbo"]), "가짜 fetch 를 준 Sports 는 환경변수 키를 안 씀"
    finally:
        os.environ.pop("APISPORTS_KEY", None)


@test
async def overseas_leagues_still_prefer_espn():
    db = await make_db()
    f = ApsFetch()
    sp = Sports("123", db, KST, fetch=f, clock=Clock(ts(2026, 10, 1, 12)), aps_key=KEY)
    assert [p.name for p in sp.feed.sources(LEAGUES["epl"])][0] == "espn"
    assert [p.name for p in sp.feed.sources(LEAGUES["kbo"])] == ["apisports"]
    await sp.feed.day(LEAGUES["epl"], datetime(2026, 10, 1).date())
    assert f.calls == [], "ESPN 이 되면 API-Sports 한도를 안 씀"


# ── 팀 이름표 ─────────────────────────────────────────────
@test
async def team_names_per_league():
    assert canon("kbo", "KT Wiz Suwon") == "KT" and canon("kbl", "Suwon KT") == "KT"
    assert canon("kleague", "FC Anyang") == "안양" and canon("kleague2", "Gimcheon Sangmu FC") == "김천", "K리그 승강 가족"
    assert canon("kbo", "Unknown Team") == "Unknown Team"
    assert ko_name("삼성", "kbl") == "서울 삼성" and ko_name("삼성", "kbo") == "삼성" and ko_name("삼성") == "삼성"
    assert ko_name("정관장", "wvleague") == "정관장" and ko_name("정관장", "kbl") == "안양 정관장"
    assert same_team("LG", "LG", "kbo") and not same_team("SSG", "LG", "kbo")
    assert find_team("현대캐피탈").league == "vleague" and find_team("하나은행").league == "wkbl"
    assert find_team("서울 SK").league == "kbl" and find_team("LG").league == "kbo"


# ── 명령·알림 끝까지 ─────────────────────────────────────
async def room(now):
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "야구방")
    f = ApsFetch()
    clock = Clock(now)
    svc.sports = Sports("123", db, svc.cfg.tz, fetch=f, clock=clock, aps_key=KEY)
    await db.set_setting(CHAT, "sports_enabled", True)
    await db.set_setting(CHAT, "sports_quiet", "off")
    await db.set_setting(CHAT, "sports_alerts", "all")
    return svc, db, f, clock


@test
async def kbo_command_shows_korean_lineup():
    from sodam.sports.ui import UI
    svc, *_ = await room(ts(2026, 10, 1, 15, 0))
    text = await UI(svc.sports).games_text("KBO")
    assert "NC vs 두산" in text and "18:30" in text and "준비 중" not in text, text   # 야구는 원정 먼저


def sent(bot):
    return [c[2] for c in bot.calls if c[0] == "send_message"]


def live(base, short, h, a):
    def make():
        d = json.loads(json.dumps(base))
        for x in d["response"]:
            if x["id"] == 181942:
                x["status"] = {"long": short, "short": short}
                x["scores"]["home"]["total"], x["scores"]["away"]["total"] = h, a
        return d
    return make


@test
async def kbo_team_follow_alerts_start_score_final_once():
    start = ts(2026, 10, 1, 18, 20)
    svc, db, f, clock = await room(start)
    al = svc.sports.alerts
    assert await al.follow(CHAT, "kbo", "두산", "두산 (KBO)", 1) == "added"
    await al.follow(CHAT, "kbl", "LG", "LG (KBL)", 1)
    bot = FakeBot()
    base = load("apisports_baseball_20261001.json")
    await svc.sports.run_alerts(bot)                      # 처음 = 조용히 저장 (재시작 폭탄 없음)
    assert not sent(bot), sent(bot)
    aps = next(p for p in svc.sports.providers if p.name == "apisports")
    steps = [("IN1", 0, 0), ("IN3", 2, 0), ("IN9", 2, 1), ("FT", 2, 1)]
    for short, h, a in steps:
        f.override[("baseball", "2026-10-01")] = live(base, short, h, a)
        clock.t += aps._gap("baseball") + 61
        await svc.sports.run_alerts(bot)
    texts = sent(bot)
    joined = "\n".join(texts)
    assert "경기 시작" in joined and "NC vs 두산" in joined, texts
    assert "NC 0-2 두산" in joined and "NC 1-2 두산" in joined, texts           # 점수 변화 (원정 먼저)
    assert "경기 종료" in joined and joined.count("경기 종료") == 1, texts
    assert "SSG" not in joined and "KIA" not in joined, "구독한 팀 경기만"
    n = len(sent(bot))
    clock.t += aps._gap("baseball") + 61
    await svc.sports.run_alerts(bot)
    assert len(sent(bot)) == n, "같은 결과 두 번 안 보냄"
    calls = [c for c in f.calls if c[0] == "baseball"]
    assert len(calls) <= len(steps) + 2, calls                                # 60초 틱이어도 한도 간격대로만
    assert not [c for c in f.calls if c[0] == "basketball" and c[1] != "2026-10-01"], f.calls


@test
async def alert_polling_stops_at_reserve_but_command_works():
    start = ts(2026, 10, 1, 18, 20)
    svc, db, f, clock = await room(start)
    aps = next(p for p in svc.sports.providers if p.name == "apisports")
    aps._daily = APS_RESERVE + 2
    await svc.sports.alerts.follow(CHAT, "kbo", "", "KBO", 1)
    bot = FakeBot()
    for _ in range(30):
        clock.t += 600          # 18:20 → 23:20 (같은 날)
        await svc.sports.run_alerts(bot)
    assert aps.remaining("baseball") >= APS_RESERVE, aps.remaining("baseball")
    from sodam.sports.ui import UI
    clock.t += 60
    svc.sports.feed._cache.clear()
    aps._raw.clear()
    text = await UI(svc.sports).games_text("KBO")
    assert "두산" in text or "한도" in text, text


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
