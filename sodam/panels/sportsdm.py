"""⚽ 1:1 스포츠 메뉴 (누구나, 2026-10-10 고객 '후바오 스포츠봇처럼' + '라이브스코어보다 빠르게'). 알림 엔진은 sodam/sports/alerts.py.

m:sx                 처음 화면 — 🔴 라이브 · 📅 오늘/내일 · 🔔 내 알림 · 🏆 순위
m:sxl[:종목]         지금 진행 중 (전 세계: 세계 축구 한 요청 + 다른 종목 ESPN 리그 + KHL) · 🔄 새로고침 · 경기마다 [🔔] (1:1 알림)
m:sxd:<0|1>[:종목]   오늘·내일 경기 (축구는 주요 리그, 🌍 세계 축구 전체 따로)
m:sxw                내 경기 알림 목록 · 🗑 · ➕ (팀·리그·'NHL 보스턴 필라델피아')
m:sxs[:리그]         순위
m:sxp[:<0|1>:<리그>]  🎯 승부 맞히기 — 시작 전 경기마다 [홈 승][무][원정 승] + [🧠 분석] (돈·포인트 걸기 없음, sports/picks.py)
m:sxpx:<리그>:<소스>:<id>:<h|d|a>:<날>  고르기 → 같은 목록 다시
m:sxa:<리그>:<소스>:<id>:<날>  🧠 AI 분석 (sports/analysis.py, 경기마다 3시간 공유)
m:sxr[:all]          📊 내 기록 + 이번 주·전체 랭킹
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from telegram import Message

from .. import menu
from ..menu import PUBLIC, B, PanelCtx, Route, Screen
from ..services import PendingInput
from ..util import esc

LIVE_AGE = 15          # 라이브 화면 캐시 (초) — 알림 폴링(8초)이 같은 캐시를 채우면 그걸 씀
GAME_BTNS = 8          # 한 화면에 [🔔] 버튼을 붙일 경기 수
SHOW = 40              # 한 화면 줄 수
STANDING_LEAGUES = ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "mlb", "nba", "nhl", "nfl", "kbo", "kleague")


def _home_kb():
    return menu._kb([[B("🔴 지금 라이브 (전 세계)", "m:sxl")],
                     [B("📅 오늘 경기", "m:sxd:0"), B("📅 내일 경기", "m:sxd:1")],
                     [B("🎯 승부 맞히기", "m:sxp"), B("📊 내 기록·랭킹", "m:sxr")],
                     [B("🔔 내 경기 알림", "m:sxw"), B("🏆 리그 순위", "m:sxs")],
                     [B("⬅️ 처음으로", "m:home")]])


async def s_home(c: PanelCtx) -> Screen:
    if c.svc.sports is None:
        return Screen("⚽ 스포츠 기능이 꺼져 있어요.", menu._kb([[B("⬅️ 처음으로", "m:home")]]))
    n = len(await c.svc.sports.alerts.watches(c.uid))
    return Screen("⚽ <b>스포츠</b>\n"
                  "전 세계 경기 점수·일정을 보고, 원하는 경기·팀의 <b>골·결과를 1:1 로 바로</b> 받아요 "
                  "(경기 중엔 8초마다 확인).\n"
                  f"\n🔔 내 알림 {n}개 · 그룹방에선 '소담아 ○○ 경기 득점하면 알려줘' 도 돼요.", _home_kb())


def _live_leagues():
    from ..sports.leagues import LEAGUES
    out = [lg for lg in LEAGUES.values() if (lg.espn or lg.khl) and lg.sport != "soccer" and lg.sport not in ("golf", "racing")]
    if "world" in LEAGUES:
        out.insert(0, LEAGUES["world"])
    return out


async def _live(c: PanelCtx) -> list:
    """진행 중 경기 (오늘 + 새벽엔 어제 시작한 것)."""
    from ..sports.providers import KST, SportsError
    sp = c.svc.sports
    leagues = [lg for lg in _live_leagues() if sp.feed.available(lg)]
    today = datetime.fromtimestamp(sp.feed.clock(), KST).date()
    days = [today] + ([today - timedelta(days=1)] if datetime.fromtimestamp(sp.feed.clock(), KST).hour < 12 else [])

    async def one(lg, d):
        try:
            return await sp.feed.day(lg, d, max_age=LIVE_AGE)
        except SportsError:
            return []
    got = await asyncio.gather(*(one(lg, d) for lg in leagues for d in days))
    seen, out = set(), []
    for games in got:
        for g in games:
            if g.state == "in" and g.key not in seen:
                seen.add(g.key)
                out.append(g)
    return sorted(out, key=lambda g: (g.league, g.title, g.start))


def _sport_of(g) -> str:
    from ..sports.leagues import LEAGUES
    lg = LEAGUES.get(g.league)
    return lg.sport if lg else ""


async def _bell(c: PanelCtx, g) -> B:
    from ..sports import fmt
    from ..util import html_plain
    label = html_plain(fmt.matchup(g, score=False))[:24]
    tok = menu.token(c.svc, c.uid, c.uid, "spw_game", (g.key, g.league, html_plain(f"{fmt.tag(g)} {fmt.matchup(g, score=False)}")[:80],
                                                       int(g.start)), menu.LIST_TOKEN_TTL)
    return B(f"🔔 {label}", f"m:k:{tok}")


async def s_live(c: PanelCtx) -> Screen:
    from ..sports import fmt
    from ..sports.leagues import SPORT_EMOJI, SPORT_KO
    if c.svc.sports is None:
        return await s_home(c)
    sport = c.arg(0)
    games = await _live(c)
    back = [B("🔄 새로고침", f"m:sxl:{sport}" if sport else "m:sxl"), B("⬅️ 스포츠", "m:sx")]
    if not games:
        return Screen("🔴 지금 진행 중인 경기가 없어요.", menu._kb([back]))
    by: dict[str, list] = {}
    for g in games:
        by.setdefault(_sport_of(g), []).append(g)
    stamp = datetime.fromtimestamp(c.svc.sports.feed.clock(), c.svc.cfg.tz).strftime("%H:%M:%S")
    if not sport or sport not in by:
        lines = [f"🔴 <b>지금 진행 중 — 전 세계 {len(games)}경기</b> ({stamp})", ""]
        for sp_, gs in sorted(by.items(), key=lambda x: -len(x[1])):
            lines.append(f"{SPORT_EMOJI.get(sp_, '🏟️')} {SPORT_KO.get(sp_, sp_)} {len(gs)}경기")
        btns = [B(f"{SPORT_EMOJI.get(sp_, '🏟️')} {SPORT_KO.get(sp_, sp_)} ({len(gs)})", f"m:sxl:{sp_}") for sp_, gs in by.items()]
        return Screen("\n".join(lines) + "\n\n종목을 누르면 점수와 [🔔 알림] 버튼이 나와요.",
                      menu._kb(menu._chunks(btns, 2) + [back]))
    gs = by[sport]
    lines = [f"{SPORT_EMOJI.get(sport, '🏟️')} <b>{SPORT_KO.get(sport, sport)} 진행 중 {len(gs)}경기</b> ({stamp})"]
    head = None
    for g in gs[:SHOW]:
        h = fmt.tag(g)
        if h != head:
            lines.append(f"\n<b>{h}</b>")
            head = h
        lines.append(fmt.line(g))
    if len(gs) > SHOW:
        lines.append(f"… 외 {len(gs) - SHOW}경기")
    bells = [await _bell(c, g) for g in gs[:GAME_BTNS]]
    text = "\n".join(lines)
    if len(text) > 3700:                       # 텔레그램 글 4,096자 (세계 축구 40경기 + 대회 머리글 — 리뷰 2026-10-10)
        text = text[:3700].rsplit("\n", 1)[0] + "\n…"
    return Screen(text + "\n\n🔔 = 그 경기 골·결과를 1:1 로", menu._kb(menu._chunks(bells, 2) + [back]))


async def s_day(c: PanelCtx) -> Screen:
    from ..sports import ui as sports_ui
    from ..sports.leagues import SPORT_EMOJI, SPORT_KO
    if c.svc.sports is None:
        return await s_home(c)
    off = 1 if c.arg(0) == "1" else 0
    what = c.arg(1)
    ui = sports_ui.UI(c.svc.sports)
    d = ui.today() + timedelta(days=off)
    word = "내일" if off else "오늘"
    if not what:
        btns = [B(f"{SPORT_EMOJI.get(s, '')} {ko}", f"m:sxd:{off}:{s}") for s, ko in SPORT_KO.items()
                if s not in ("racing", "golf", "afootball")]
        rows = [[B("⭐ 주요 리그", f"m:sxd:{off}:top"), B("🌍 세계 축구 전체", f"m:sxd:{off}:world")]] + menu._chunks(btns, 3)
        return Screen(f"📅 <b>{word} 경기</b> — 무엇을 볼까요?", menu._kb(rows + [[B("⬅️ 스포츠", "m:sx")]]))
    query = {"top": "", "world": "세계축구"}.get(what, SPORT_KO.get(what, what))
    text = await ui.games_text(query, d)
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n…"
    return Screen(text, menu._kb([[B("⬅️ 종목", f"m:sxd:{off}"), B("⬅️ 스포츠", "m:sx")]]))


async def s_watches(c: PanelCtx) -> Screen:
    from ..sports.alerts import LEVELS, MAX_WATCHES
    if c.svc.sports is None:
        return await s_home(c)
    rows = await c.svc.sports.alerts.watches(c.uid)
    lines = [f"🔔 <b>내 경기 알림</b> ({len(rows)}/{MAX_WATCHES})", "골·결과를 이 1:1 로 보내요. 경기 하나짜리는 끝나면 자동으로 꺼져요.", ""]
    lines += [f"• {esc(r['label'])} — {LEVELS.get(r['level'], LEVELS['goals'])[0]}" + (" (경기 하나)" if r["game"] else "")
              for r in rows] or ["아직 없어요. 🔴 라이브에서 🔔 를 누르거나 ➕ 로 팀을 넣어 보세요."]
    dels = [B(f"🗑 {r['label'][:18]}", f"m:k:{menu.token(c.svc, c.uid, c.uid, 'spw_del', r['id'], menu.LIST_TOKEN_TTL)}") for r in rows]
    kb = menu._chunks(dels, 2) + [[B("➕ 팀·경기 추가", "m:sxwi")], [B("⬅️ 스포츠", "m:sx")]]
    return Screen("\n".join(lines), menu._kb(kb))


async def r_add(c: PanelCtx) -> Screen:
    c.svc.inputs[c.uid] = PendingInput("sxwi", 0)
    return Screen("➕ 알림 받을 <b>팀·리그·경기</b>를 보내주세요.\n"
                  "예: <code>토트넘</code> · <code>다저스</code> · <code>EPL</code> · <code>NHL 보스턴 필라델피아</code>\n\n그만두려면 <code>취소</code>",
                  menu._kb([[B("❌ 취소", "m:sxw")]]))


async def in_add(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    from ..sports import ui as sports_ui
    text = (msg.text or "").strip()[:60]
    if not text or c.svc.sports is None:
        return False, "팀이나 경기 이름을 보내주세요."
    out = await sports_ui.UI(c.svc.sports).watch(c.uid, c.uid, text)
    return out.startswith("🔔"), out


async def s_after(c: PanelCtx) -> Screen:
    return await s_watches(c)


async def t_game(c: PanelCtx, arg) -> Screen:
    from ..sports.alerts import MAX_WATCHES, WATCH_KEEP
    key, league, label, start = arg
    r = await c.svc.sports.alerts.watch(c.uid, c.uid, league, "", key, label, "goals", expires=int(start) + WATCH_KEEP)
    toast = {"added": "🔔 이 경기 골·결과를 1:1 로 보낼게요", "exists": "이미 알림 걸어 둔 경기예요",
             "full": f"알림은 {MAX_WATCHES}개까지예요 (🔔 내 알림에서 정리)"}[r]
    return Screen(None, toast=toast, alert=r == "full")


async def t_del(c: PanelCtx, arg) -> Screen:
    await c.svc.sports.alerts.unwatch(c.uid, int(arg))
    screen = await s_watches(c)
    screen.toast = "지웠어요."
    return screen


async def s_standings(c: PanelCtx) -> Screen:
    from ..sports import ui as sports_ui
    from ..sports.leagues import LEAGUES
    code = c.arg(0)
    if c.svc.sports is None:
        return await s_home(c)
    if code not in LEAGUES:
        btns = [B(LEAGUES[k].name, f"m:sxs:{k}") for k in STANDING_LEAGUES if k in LEAGUES and c.svc.sports.feed.available(LEAGUES[k])]
        return Screen("🏆 <b>순위</b> — 리그를 고르세요.", menu._kb(menu._chunks(btns, 3) + [[B("⬅️ 스포츠", "m:sx")]]))
    text = await sports_ui.UI(c.svc.sports).standings_text(LEAGUES[code].name)
    return Screen(text[:3900], menu._kb([[B("⬅️ 리그", "m:sxs"), B("⬅️ 스포츠", "m:sx")]]))


PICK_LEAGUES = ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "uel", "kleague", "jleague", "mlb", "kbo", "nba", "nhl",
                "nfl", "khl", "world")
PICK_SHOW = 8


async def _game(c: PanelCtx, league: str, key: str, off: str):
    """버튼의 경기 하나 다시 찾기 (그 날짜 일정, 캐시 우선)."""
    from ..sports.leagues import LEAGUES
    from ..sports.providers import SportsError
    from ..sports import ui as sports_ui
    lg = LEAGUES.get(league)
    if lg is None or c.svc.sports is None:
        return None
    d = sports_ui.UI(c.svc.sports).today() + timedelta(days=1 if off == "1" else 0)
    for day in (d, d - timedelta(days=1), d + timedelta(days=1)):
        games = c.svc.sports.feed.cached_day(lg, day)
        if games is None:
            try:
                games = await c.svc.sports.feed.day(lg, day)
            except SportsError:
                games = []
        hit = next((g for g in games if g.key == key), None)
        if hit:
            return hit
    return None


async def s_picks(c: PanelCtx) -> Screen:
    from ..sports import fmt, picks
    from ..sports import ui as sports_ui
    from ..sports.leagues import LEAGUES, ko_name
    from ..sports.providers import SportsError
    if c.svc.sports is None:
        return await s_home(c)
    off, code = (c.arg(0) or "0"), c.arg(1)
    if code not in LEAGUES:
        btns = [B(LEAGUES[k].name, f"m:sxp:{off}:{k}") for k in PICK_LEAGUES if k in LEAGUES and c.svc.sports.feed.available(LEAGUES[k])]
        day = [B(("● " if off == "0" else "") + "오늘", "m:sxp:0"), B(("● " if off == "1" else "") + "내일", "m:sxp:1")]
        return Screen("🎯 <b>승부 맞히기</b>\n시작 전 경기의 승패를 골라요. 맞히면 <b>10점</b>(무승부는 15점), 돈·포인트는 안 걸려요.\n"
                      "경기가 끝나면 1분 안에 점수가 들어가요. 리그를 고르세요.",
                      menu._kb([day] + menu._chunks(btns, 3) + [[B("📊 내 기록·랭킹", "m:sxr"), B("⬅️ 스포츠", "m:sx")]]))
    lg = LEAGUES[code]
    d = sports_ui.UI(c.svc.sports).today() + timedelta(days=1 if off == "1" else 0)
    try:
        games = await c.svc.sports.feed.day(lg, d)
    except SportsError as e:
        return Screen(esc(str(e)), menu._kb([[B("⬅️ 리그", f"m:sxp:{off}")]]))
    now = c.svc.sports.feed.clock()
    games = [g for g in games if g.state == "pre" and g.start > now and (g.home or g.away)][:PICK_SHOW]
    title = f"🎯 <b>{esc(lg.name)} {'내일' if off == '1' else '오늘'}</b>"
    if not games:
        return Screen(title + "\n시작 전 경기가 없어요.", menu._kb([[B("⬅️ 리그", f"m:sxp:{off}"), B("⬅️ 스포츠", "m:sx")]]))
    lines, rows = [title, ""], []
    for i, g in enumerate(games, 1):
        mine = await picks.mine(c.svc.db, c.uid, g.key)
        home, away = ko_name(g.home, g.league), ko_name(g.away, g.league)
        lines.append(f"{i}. {fmt.hhmm(g.start)} {esc(home)} vs {esc(away)}" + (f" — ✅ {esc({'home': home, 'away': away, 'draw': '무'}[mine])}" if mine else ""))
        base = f"{code}:{g.key}"
        if len(f"m:sxpx:{base}:h:0".encode()) > 64:          # 텔레그램 버튼 데이터 64바이트
            continue
        mk = lambda side, label: B(("✅" if mine == side else "") + label, f"m:sxpx:{base}:{side[0]}:{off}")
        row = [mk("home", f"{i} {home[:7]}"), *([mk("draw", "무")] if picks.draw_ok(g.league) else []), mk("away", away[:7]),
               B("🧠", f"m:sxa:{base}:{off}")]
        rows.append(row)
    return Screen("\n".join(lines) + "\n\n버튼 = 이길 팀 · 🧠 = AI 분석 (시작 전까지 바꿀 수 있어요)",
                  menu._kb(rows + [[B("⬅️ 리그", f"m:sxp:{off}"), B("📊 내 기록", "m:sxr")]]))


async def r_pick(c: PanelCtx) -> Screen:
    from ..sports import picks
    from ..sports.leagues import ko_name
    if len(c.args) < 4:
        return Screen(None, toast="다시 눌러 주세요.")
    code, key, side, off = c.args[0], ":".join(c.args[1:-2]), c.args[-2], c.args[-1]   # key 에 ':' 가 더 있어도 (aps:…)
    g = await _game(c, code, key, off)
    if g is None:
        return Screen(None, toast="경기를 못 찾았어요. 목록을 다시 열어 주세요.", alert=True)
    r = await picks.pick(c.svc.db, c.uid, g, side, 0, now=c.svc.sports.feed.clock())
    name = {"h": ko_name(g.home, g.league), "a": ko_name(g.away, g.league), "d": "무승부"}.get(side, "")
    toast = {"ok": f"🎯 {name} 골랐어요", "changed": f"🎯 {name}(으)로 바꿨어요", "started": "이미 시작한 경기예요",
             "nodraw": "이 종목은 무승부가 없어요", "full": f"하루 {picks.DAILY_MAX}경기까지예요", "bad": "다시 눌러 주세요"}[r]
    c.args = [off, code]
    screen = await s_picks(c)
    screen.toast = toast
    return screen


async def s_analysis(c: PanelCtx) -> Screen:
    from ..sports import analysis
    code, key, off = (c.args[0], ":".join(c.args[1:-1]), c.args[-1]) if len(c.args) >= 3 else ("", "", "0")
    g = await _game(c, code, key, off)
    back = [[B("⬅️ 맞히기", f"m:sxp:{off}:{code}"), B("⬅️ 스포츠", "m:sx")]]
    if g is None:
        return Screen("경기를 못 찾았어요.", menu._kb(back))
    return Screen((await analysis.analyze(c.svc, g, now=c.svc.sports.feed.clock()))[:3900], menu._kb(back))


async def s_record(c: PanelCtx) -> Screen:
    from ..sports import picks
    if c.svc.sports is None:
        return await s_home(c)
    now = c.svc.sports.feed.clock()
    st = await picks.stats(c.svc.db, c.uid, now=now)
    week = c.arg(0) != "all"
    top = await picks.ranking(c.svc.db, week=week, now=now)
    lines = ["📊 <b>내 기록</b>",
             f"승률 {st['rate']}% ({st['wins']}/{st['settled']}) · 누적 {st['points']}점 · 연속 {st['streak']}번 맞힘",
             f"이번 주 {st['week']}점" + (f" · {st['week_rank']}위" if st["week_rank"] else "") + f" · 결과 기다리는 경기 {st['open']}개",
             "", f"🏆 <b>{'이번 주' if week else '전체'} 랭킹</b>"]
    lines += [f"{i}. {esc(name)} — {pts}점 ({w}/{n})" for i, (_, name, pts, w, n) in enumerate(top, 1)] or ["아직 없어요."]
    tabs = [B(("● " if week else "") + "이번 주", "m:sxr"), B(("● " if not week else "") + "전체", "m:sxr:all")]
    return Screen("\n".join(lines), menu._kb([tabs, [B("🎯 맞히기", "m:sxp"), B("⬅️ 스포츠", "m:sx")]]))


_last_settle = [0.0]


async def settle_tick(svc, bot) -> None:
    """정산 (30초 tick 훅 → 1분에 한 번)."""
    from ..sports import picks
    if svc.sports is None:
        return
    now = svc.sports.feed.clock()
    if now - _last_settle[0] < picks.SETTLE_EVERY:
        return
    _last_settle[0] = now
    n = await picks.settle(svc.db, svc.sports.feed, now=now)
    if n:
        log.info("승부 맞히기 정산 %d개", n)


from .. import hooks  # noqa: E402
import logging  # noqa: E402

log = logging.getLogger(__name__)
hooks.add_tick_hook(settle_tick)

menu.register_main(25, "sx", "⚽ 스포츠")
for _code, _fn in (("sx", s_home), ("sxl", s_live), ("sxd", s_day), ("sxw", s_watches), ("sxwi", r_add), ("sxs", s_standings),
                   ("sxp", s_picks), ("sxpx", r_pick), ("sxa", s_analysis), ("sxr", s_record)):
    menu.register_route(_code, Route(_fn, PUBLIC, scoped=False))
menu.register_token_action("spw_game", t_game, need=PUBLIC)
menu.register_token_action("spw_del", t_del, need=PUBLIC)
menu.register_input("sxwi", "", "sxw", in_add, s_after, need=PUBLIC)


# ── 그룹방: .맞히기 [리그] 카드 · .맞히기순위 · .분석 팀 팀 ──────────────
ROOM_SHOW = 5
PRESS_GAP = 1.0
_press: dict[int, float] = {}


async def room_card(svc, chat_id: int, query: str) -> tuple[str, list]:
    """방에 올릴 맞히기 카드 (글, 버튼 줄). 버튼 sgp:<리그>:<경기 key>:<h|d|a>."""
    from ..sports import fmt, picks
    from ..sports import ui as sports_ui
    from ..sports.leagues import LEAGUES, ko_name
    from ..sports.providers import SportsError
    ui = sports_ui.UI(svc.sports)
    found = sports_ui.resolve(query) if query else None
    leagues = [found[1]] if found and found[0] == "league" else \
        [LEAGUES[found[1].league]] if found and found[0] == "team" else [LEAGUES[k] for k in PICK_LEAGUES[:6] if k in LEAGUES]
    now = svc.sports.feed.clock()
    games = []
    for d in (ui.today(), ui.today() + timedelta(days=1)):
        for lg in leagues:
            if not svc.sports.feed.available(lg):
                continue
            try:
                games += [g for g in await svc.sports.feed.day(lg, d) if g.state == "pre" and g.start > now and (g.home or g.away)]
            except SportsError:
                pass
        if len(games) >= ROOM_SHOW:
            break
    if found and found[0] == "team":
        from ..sports.leagues import same_team
        t = found[1]
        games = [g for g in games if same_team(g.home, t.src, t.league) or same_team(g.away, t.src, t.league)]
    games = sorted(games, key=lambda g: g.start)[:ROOM_SHOW]
    if not games:
        return "🎯 지금 고를 수 있는 시작 전 경기가 없어요. <code>.맞히기 EPL</code> 처럼 리그를 붙여 보세요.", []
    lines, rows = ["🎯 <b>승부 맞히기</b> — 이길 팀을 눌러요 (맞히면 10점, 무승부 15점 · 돈·포인트 걸기 없음)", ""], []
    for i, g in enumerate(games, 1):
        home, away = ko_name(g.home, g.league), ko_name(g.away, g.league)
        lines.append(f"{i}. {fmt.tag(g)} {fmt.mdhm(g.start)} {esc(home)} vs {esc(away)}")
        base = f"sgp:{g.league}:{g.key}"
        if len(f"{base}:h".encode()) > 64:
            continue
        rows.append([B(f"{i} {home[:8]}", f"{base}:h"), *([B("무", f"{base}:d")] if picks.draw_ok(g.league) else []),
                     B(away[:8], f"{base}:a")])
    lines.append("\n순위: <code>.맞히기순위</code> · 분석: <code>.분석 팀 팀</code> · 내 기록은 1:1 [⚽ 스포츠]")
    return "\n".join(lines), rows


async def on_room_pick(svc, bot, q, parts) -> None:
    from ..sports import picks
    from ..sports.leagues import ko_name
    if svc.sports is None or len(parts) < 3:
        await q.answer()
        return
    code, key, side = parts[0], ":".join(parts[1:-1]), parts[-1]
    now = time.monotonic()
    if now - _press.get(q.from_user.id, 0) < PRESS_GAP:     # 연타 (누를 때마다 일정 받기 — 리뷰 2026-10-10)
        await q.answer("천천히 눌러 주세요.")
        return
    _press[q.from_user.id] = now
    if len(_press) > 5000:
        _press.clear()
    c = PanelCtx(svc, bot, q.from_user.id, None, [])
    g = await _game(c, code, key, "0")
    if g is None:
        await q.answer("경기를 못 찾았어요.", show_alert=True)
        return
    await svc.db.upsert_user(q.from_user)
    r = await picks.pick(svc.db, q.from_user.id, g, side, q.message.chat.id if q.message else 0, now=svc.sports.feed.clock())
    name = {"h": ko_name(g.home, g.league), "a": ko_name(g.away, g.league), "d": "무승부"}.get(side, "")
    toast = {"ok": f"🎯 {name} 골랐어요! 결과는 1:1 [⚽ 스포츠] → 📊", "changed": f"🎯 {name}(으)로 바꿨어요",
             "started": "이미 시작한 경기예요", "nodraw": "이 종목은 무승부가 없어요", "full": f"하루 {picks.DAILY_MAX}경기까지예요",
             "bad": "다시 눌러 주세요"}[r]
    await q.answer(toast, show_alert=r in ("started", "full"))


async def c_picks(ctx) -> None:
    if ctx.svc.sports is None:
        await ctx.reply("스포츠 기능이 꺼져 있어요.")
        return
    text, rows = await room_card(ctx.svc, ctx.chat_id, ctx.argstr.strip())
    await ctx.reply(text, reply_markup=menu._kb(rows) if rows else None)


async def c_rank(ctx) -> None:
    from ..sports import picks
    if ctx.svc.sports is None:
        return
    now = ctx.svc.sports.feed.clock()
    week = "전체" not in ctx.argstr
    top = await picks.ranking(ctx.svc.db, week=week, chat_id=ctx.chat_id if ctx.chat_id < 0 else None, now=now)
    head = f"🏆 <b>승부 맞히기 {'이번 주' if week else '전체'} 순위</b>" + (" (이 방)" if ctx.chat_id < 0 else "")
    body = [f"{i}. {esc(n)} — {p}점 ({w}/{t})" for i, (_, n, p, w, t) in enumerate(top, 1)] or ["아직 없어요. <code>.맞히기</code> 로 시작!"]
    st = await picks.stats(ctx.svc.db, ctx.user.id, now=now)
    me = f"\n나: {st['week'] if week else st['points']}점 · 승률 {st['rate']}% ({st['wins']}/{st['settled']})"
    await ctx.reply("\n".join([head, *body]) + me)


async def c_analysis(ctx) -> None:
    from ..sports import analysis
    from ..sports import ui as sports_ui
    if ctx.svc.sports is None:
        return
    if ctx.chat_id < 0 and not await ctx.svc.paid_features(ctx.chat_id):   # AI 비용 — 이용 중인 방만 (1:1 은 하루 300개 전체 한도)
        await ctx.reply("경기 분석은 이용 기간 중인 방에서 쓸 수 있어요. 1:1 [⚽ 스포츠] → 🎯 의 🧠 도 돼요.")
        return
    q = ctx.argstr.strip()
    if not q:
        await ctx.reply("사용법: <code>.분석 토트넘 아스널</code> · <code>.분석 NHL 보스턴 필라델피아</code>")
        return
    games = await sports_ui.UI(ctx.svc.sports).find_games(q)
    if not games:
        await ctx.reply(f"'{esc(q)}' 경기를 어제~내일 일정에서 못 찾았어요. 리그를 붙여 주세요.")
        return
    await ctx.reply((await analysis.analyze(ctx.svc, games[0], now=ctx.svc.sports.feed.clock()))[:3900])


def register_commands() -> None:
    """handlers 가 부름 (commands → menu → panels 순환)."""
    from .. import commands
    for cmd in (commands.Cmd(("맞히기", "승부맞히기", "승부예측", "예측"), c_picks, usage="[리그·팀]", help="시작 전 경기 승부 맞히기 (점수만)",
                             group="스포츠"),
                commands.Cmd(("맞히기순위", "예측순위", "승부순위"), c_rank, usage="[전체]", help="승부 맞히기 순위", group="스포츠", dm_ok=True),
                commands.Cmd(("분석", "경기분석"), c_analysis, usage="팀 팀", help="AI 경기 분석 (기록 기반, 재미용)", group="스포츠",
                             dm_ok=True)):
        if any(c.fn is cmd.fn for c in commands.COMMANDS):
            continue
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)


hooks.add_callback_handler("sgp", on_room_pick)
