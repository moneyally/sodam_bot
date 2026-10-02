"""⚽ 스포츠 알림 화면 (그룹 허브, 방 관리자). 알림 엔진은 sodam/sports/alerts.py.

m:spt:<방>          구독 목록 · 알림 종류 · 조용한 시간 · 추가/지우기
m:sptl:<방>         리그 버튼 목록 (누르면 구독/해제)
m:spta:<방>:<리그>  리그 구독 켜고 끄기
m:in:<방>:sptt      팀 이름 입력 (한국어·영어)
m:k:<토큰>          구독 하나 지우기 (spt_del)
"""
from __future__ import annotations

from telegram import Message

from .. import menu
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..sports.alerts import LEVELS, MAX_FOLLOWS, QUIET
from ..sports.leagues import LEAGUES, SPORT_EMOJI, SPORT_KO, League
from ..util import esc

menu.register_preset("sports_alerts", [(k, v[0]) for k, v in LEVELS.items()], "spt")
menu.register_preset("sports_quiet", [(k, "🌙 " + v if k != "off" else "🔔 없음") for k, v in QUIET.items()], "spt")


def _usable(c: PanelCtx, lg: League) -> bool:
    sp = c.svc.sports
    return sp.feed.available(lg) if sp is not None else bool(lg.espn)


async def _follows(c: PanelCtx) -> list:
    return await c.svc.db._all("SELECT * FROM sports_follow WHERE chat_id=? ORDER BY added_at", (c.cid,))


async def s_spt(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    rows_db = await _follows(c)
    active = await c.svc.paid_features(c.cid)
    lines = ["⚽ <b>스포츠 알림</b>",
             "구독한 리그·팀의 경기 시작·골·결과를 이 방에 자동으로 알려요. 여러 경기는 한 메시지로 묶고, 배당 정보는 없어요.",
             "",
             f"상태: {'✅ 켜짐' if s['sports_enabled'] else '❌ 꺼짐 — 🧩 기능 켜기/끄기에서 켜기'}"
             + ("" if active else " · ⚠️ 이용 기간 중인 방만 알림이 가요"),
             f"구독 ({len(rows_db)}/{MAX_FOLLOWS}): " + (", ".join(esc(r["label"]) for r in rows_db) if rows_db else "없음"),
             f"알림 종류: {LEVELS.get(s['sports_alerts'], LEVELS['goals'])[0]} (야구·농구·배구는 '골' 대신 시작·결과)",
             f"조용한 시간(한국): {QUIET.get(s['sports_quiet'], s['sports_quiet'])} — 이때 시작·골은 안 보내고 결과는 아침에 모아서"]
    quiet = menu._preset_row(s, c.cid, "sports_quiet")
    kb = [menu._preset_row(s, c.cid, "sports_alerts")[:2], menu._preset_row(s, c.cid, "sports_alerts")[2:],
          quiet[:3], quiet[3:],
          [B("➕ 리그 추가", f"m:sptl:{c.cid}"), B("➕ 팀 추가", f"m:in:{c.cid}:sptt")]]
    dels = [B(f"🗑 {r['label'][:18]}", f"m:k:{menu.token(c.svc, c.uid, c.cid, 'spt_del', (r['league'], r['team']), menu.LIST_TOKEN_TTL)}")
            for r in rows_db]
    kb += menu._chunks(dels, 2) + [menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(kb))


async def s_leagues(c: PanelCtx) -> Screen:
    """m:sptl:<방> = 종목 고르기 · m:sptl:<방>:<종목> = 그 종목 리그 버튼 (리그가 80개 넘어 한 화면에 다 못 넣음)."""
    return await _league_screen(c, c.arg(0))


async def _league_screen(c: PanelCtx, sport: str | None) -> Screen:
    followed = {r["league"] for r in await _follows(c) if not r["team"]}
    usable = [lg for lg in LEAGUES.values() if _usable(c, lg)]
    if sport not in SPORT_KO:
        btns = []
        for sp, ko in SPORT_KO.items():
            lgs = [lg for lg in usable if lg.sport == sp]
            if lgs:
                on = sum(lg.code in followed for lg in lgs)
                btns.append(B(f"{SPORT_EMOJI.get(sp, '')} {ko} ({len(lgs)})" + (f" ✅{on}" if on else ""), f"m:sptl:{c.cid}:{sp}"))
        missing = [lg.name for lg in LEAGUES.values() if not _usable(c, lg)]
        text = ["➕ <b>리그 구독</b>", "종목을 고르세요."]
        if missing:
            text.append(f"<i>아직 준비 중: {esc(', '.join(missing))}</i>")
        return Screen("\n".join(text), menu._kb(menu._chunks(btns, 2) + [menu._back(c.cid, "spt")]))
    btns = [B(("✅ " if lg.code in followed else "") + lg.name, f"m:spta:{c.cid}:{lg.code}")
            for lg in usable if lg.sport == sport]
    text = [f"➕ <b>{SPORT_EMOJI.get(sport, '')} {SPORT_KO[sport]} 리그 구독</b>", "누르면 켜고, 한 번 더 누르면 꺼요 (✅ = 구독 중)."]
    return Screen("\n".join(text), menu._kb(menu._chunks(btns, 2) + [[B("⬅️ 종목", f"m:sptl:{c.cid}")]]))


async def r_league(c: PanelCtx) -> Screen:
    lg = LEAGUES.get(c.arg(0))
    if lg is None or not _usable(c, lg) or c.svc.sports is None:
        return Screen(None, toast="지금은 고를 수 없는 리그예요.")
    alerts = c.svc.sports.alerts
    if await alerts.unfollow(c.cid, lg.code, ""):
        toast = f"🔕 {lg.name} 구독 끔"
    else:
        r = await alerts.follow(c.cid, lg.code, "", lg.name, c.uid)
        toast = f"🔔 {lg.name} 구독" if r == "added" else f"구독은 {MAX_FOLLOWS}개까지예요."
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"sports_follow {lg.code}")
    screen = await _league_screen(c, lg.sport)
    screen.toast = toast
    return screen


async def t_delete(c: PanelCtx, arg) -> Screen:
    league, team = arg
    if c.svc.sports is not None:
        await c.svc.sports.alerts.unfollow(c.cid, league, team)
    else:
        await c.svc.db._write("DELETE FROM sports_follow WHERE chat_id=? AND league=? AND team=?", (c.cid, league, team))
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"sports_unfollow {league}:{team}"[:80])
    screen = await s_spt(c)
    screen.toast = "지웠어요."
    return screen


async def in_team(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    from ..sports.leagues import find_team
    from ..sports.ui import UI
    text = (msg.text or "").strip()[:40]
    if c.svc.sports is None:
        return False, "스포츠 기능이 꺼져 있어요."
    team = find_team(text)
    if not team:
        return False, f"'{esc(text)}' 팀을 못 찾았어요. 다시 보내주세요 (예: 토트넘, 레알, 다저스, 레이커스)."
    out = await UI(c.svc.sports).follow(c.cid, team.ko, c.uid)
    return out.startswith("🔔") or out.startswith("이미"), out


async def features_extra(c: PanelCtx) -> list:
    return [[B("⚽ 스포츠 알림 설정", f"m:spt:{c.cid}")]]


menu.register_hub(HubItem(52, "spt", "⚽ 스포츠 알림"))
menu.register_screen("spt", s_spt)
menu.register_screen("sptl", s_leagues)
menu.register_screen("sptt", s_spt)
menu.register_route("spta", Route(r_league))
menu.register_token_action("spt_del", t_delete)
menu.register_screen_extra("f", features_extra)
menu.register_input("sptt", "⚽ 알림 받을 <b>팀 이름</b>을 보내주세요. 한국어·영어 다 돼요.\n예: <code>토트넘</code> · <code>레알</code> · "
                    "<code>다저스</code> · <code>레이커스</code>", "spt", in_team, s_spt)
