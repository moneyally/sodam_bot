"""⚽ 1:1 스포츠 메뉴 (누구나, 2026-10-10 고객 '후바오 스포츠봇처럼' + '라이브스코어보다 빠르게'). 알림 엔진은 sodam/sports/alerts.py.

m:sp                 처음 화면 — 🔴 라이브 · 📅 오늘/내일 · 🔔 내 알림 · 🏆 순위
m:spl[:종목]         지금 진행 중 (전 세계: 세계 축구 한 요청 + 다른 종목 ESPN 리그 + KHL) · 🔄 새로고침 · 경기마다 [🔔] (1:1 알림)
m:spd:<0|1>[:종목]   오늘·내일 경기 (축구는 주요 리그, 🌍 세계 축구 전체 따로)
m:spw                내 경기 알림 목록 · 🗑 · ➕ (팀·리그·'NHL 보스턴 필라델피아')
m:sps[:리그]         순위
"""
from __future__ import annotations

import asyncio
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
    return menu._kb([[B("🔴 지금 라이브 (전 세계)", "m:spl")],
                     [B("📅 오늘 경기", "m:spd:0"), B("📅 내일 경기", "m:spd:1")],
                     [B("🔔 내 경기 알림", "m:spw"), B("🏆 순위", "m:sps")],
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
    back = [B("🔄 새로고침", f"m:spl:{sport}" if sport else "m:spl"), B("⬅️ 스포츠", "m:sp")]
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
        btns = [B(f"{SPORT_EMOJI.get(sp_, '🏟️')} {SPORT_KO.get(sp_, sp_)} ({len(gs)})", f"m:spl:{sp_}") for sp_, gs in by.items()]
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
    return Screen("\n".join(lines) + "\n\n🔔 = 그 경기 골·결과를 1:1 로", menu._kb(menu._chunks(bells, 2) + [back]))


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
        btns = [B(f"{SPORT_EMOJI.get(s, '')} {ko}", f"m:spd:{off}:{s}") for s, ko in SPORT_KO.items()
                if s not in ("racing", "golf", "afootball")]
        rows = [[B("⭐ 주요 리그", f"m:spd:{off}:top"), B("🌍 세계 축구 전체", f"m:spd:{off}:world")]] + menu._chunks(btns, 3)
        return Screen(f"📅 <b>{word} 경기</b> — 무엇을 볼까요?", menu._kb(rows + [[B("⬅️ 스포츠", "m:sp")]]))
    query = {"top": "", "world": "세계축구"}.get(what, SPORT_KO.get(what, what))
    text = await ui.games_text(query, d)
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n…"
    return Screen(text, menu._kb([[B("⬅️ 종목", f"m:spd:{off}"), B("⬅️ 스포츠", "m:sp")]]))


async def s_watches(c: PanelCtx) -> Screen:
    from ..sports.alerts import LEVELS, MAX_WATCHES
    if c.svc.sports is None:
        return await s_home(c)
    rows = await c.svc.sports.alerts.watches(c.uid)
    lines = [f"🔔 <b>내 경기 알림</b> ({len(rows)}/{MAX_WATCHES})", "골·결과를 이 1:1 로 보내요. 경기 하나짜리는 끝나면 자동으로 꺼져요.", ""]
    lines += [f"• {esc(r['label'])} — {LEVELS.get(r['level'], LEVELS['goals'])[0]}" + (" (경기 하나)" if r["game"] else "")
              for r in rows] or ["아직 없어요. 🔴 라이브에서 🔔 를 누르거나 ➕ 로 팀을 넣어 보세요."]
    dels = [B(f"🗑 {r['label'][:18]}", f"m:k:{menu.token(c.svc, c.uid, c.uid, 'spw_del', r['id'], menu.LIST_TOKEN_TTL)}") for r in rows]
    kb = menu._chunks(dels, 2) + [[B("➕ 팀·경기 추가", "m:spwi")], [B("⬅️ 스포츠", "m:sp")]]
    return Screen("\n".join(lines), menu._kb(kb))


async def r_add(c: PanelCtx) -> Screen:
    c.svc.inputs[c.uid] = PendingInput("spwi", 0)
    return Screen("➕ 알림 받을 <b>팀·리그·경기</b>를 보내주세요.\n"
                  "예: <code>토트넘</code> · <code>다저스</code> · <code>EPL</code> · <code>NHL 보스턴 필라델피아</code>\n\n그만두려면 <code>취소</code>",
                  menu._kb([[B("❌ 취소", "m:spw")]]))


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
        btns = [B(LEAGUES[k].name, f"m:sps:{k}") for k in STANDING_LEAGUES if k in LEAGUES and c.svc.sports.feed.available(LEAGUES[k])]
        return Screen("🏆 <b>순위</b> — 리그를 고르세요.", menu._kb(menu._chunks(btns, 3) + [[B("⬅️ 스포츠", "m:sp")]]))
    text = await sports_ui.UI(c.svc.sports).standings_text(LEAGUES[code].name)
    return Screen(text[:3900], menu._kb([[B("⬅️ 리그", "m:sps"), B("⬅️ 스포츠", "m:sp")]]))


menu.register_main(25, "sp", "⚽ 스포츠")
for _code, _fn in (("sp", s_home), ("spl", s_live), ("spd", s_day), ("spw", s_watches), ("spwi", r_add), ("sps", s_standings)):
    menu.register_route(_code, Route(_fn, PUBLIC, scoped=False))
menu.register_token_action("spw_game", t_game, need=PUBLIC)
menu.register_token_action("spw_del", t_del, need=PUBLIC)
menu.register_input("spwi", "", "spw", in_add, s_after, need=PUBLIC)
