"""🎮 e스포츠 경기·알림 (bo3.gg, sodam/sports/providers.Bo3). python tests/run_all.py sports_esports

실제 2026-10-11 벳블리: 'CS2 European Pro League · United21 · LoL LCP 승강전 · VCS 도 중계·알림 되게 — 전세계 이스포츠'.
네트워크 없음 — tests/fixtures/sports/bo3_20261011.json = 서버에서 받은 실제 응답 (CS2·LoL 진행 중·예정·끝 각 25개).
"""
import json
from datetime import date, datetime
from pathlib import Path

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import commands
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.sports import Sports, fmt
from sodam.sports.alerts import Snap, diff
from sodam.sports.leagues import LEAGUES, find_league
from sodam.sports.providers import BO3_URL, KST, Bo3

test, run_all = runner()
RAW = json.loads((Path(__file__).parent / "fixtures" / "sports" / "bo3_20261011.json").read_text(encoding="utf-8"))
NOW = int(datetime(2026, 10, 11, 17, 50, tzinfo=KST).timestamp())     # 받은 시각 (진행 중 4경기)
CHAT = -100991
ADMIN = fake_user(10, "관리자")


class Fetch:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, params, headers):
        self.calls.append((url, dict(params)))
        if url != BO3_URL:
            return {"events": []}
        st = params["filter[matches.status][in]"].split(",")[0]
        data = RAW.get(f"{params['filter[matches.discipline_id][eq]']}:{st}") or {"results": []}
        return data if params["page[offset]"] == "0" else {"results": []}


@test
def games_are_found_by_korean_and_english_names():
    assert find_league("카스").code == "cs2" and find_league("CS2").code == "cs2" and find_league("롤").code == "lol"
    assert find_league("VCS").code == "lol" and find_league("발로란트").code == "valorant" and find_league("도타").code == "dota2"
    assert LEAGUES["cs2"].sport == "esports"


@test
async def live_upcoming_and_finished_parse_with_real_tournament_names():
    p = Bo3(Fetch(), True, clock=lambda: NOW)
    games = await p.day(LEAGUES["cs2"], date(2026, 10, 11))
    by = {(g.home, g.away): g for g in games}
    g = by[("Vitality Academy", "Elite Klan")]                       # 사진 속 경기
    assert g.state == "in" and g.title == "European Pro League Regular Season 7" and g.detail.startswith("Bo3"), g
    assert ("STATE", "PCIFIC") in by and by[("STATE", "PCIFIC")].title.startswith("United21")
    assert any(x.state == "pre" for x in games) and any(x.state == "post" and x.scored for x in games)
    assert all(datetime.fromtimestamp(x.start, KST).date() == date(2026, 10, 11) for x in games), "다른 날 경기 섞임"
    lol = {(g.home, g.away): g for g in await p.day(LEAGUES["lol"], date(2026, 10, 11))}
    assert ("West Point Esports Philippines", "Nothin'Here") in lol, list(lol)
    assert "VCS 2026 Finals" in fmt.tag(next(x for x in lol.values() if "VCS" in x.title))


@test
async def cached_by_status_so_live_polling_does_not_hammer_the_site():
    f = Fetch()
    t = [NOW]
    p = Bo3(f, True, clock=lambda: t[0])
    await p.day(LEAGUES["cs2"], date(2026, 10, 11))
    n = len(f.calls)
    t[0] += 10
    await p.day(LEAGUES["cs2"], date(2026, 10, 11))
    assert len(f.calls) == n, "20초 안엔 다시 안 물음"
    t[0] += 15
    await p.day(LEAGUES["cs2"], date(2026, 10, 11))
    assert [c[1]["filter[matches.status][in]"] for c in f.calls[n:]] == ["current"], "진행 중만 다시 (예정 5분·끝 1분)"


@test
def a_won_map_is_alerted_like_a_goal():
    p = Bo3(Fetch(), True, clock=lambda: NOW)
    m = next(x for x in RAW["1:current"]["results"] if x["team1"]["name"] == "Vitality Academy")
    before = p.parse(LEAGUES["cs2"], dict(m, team1_score=0, team2_score=0))
    after = p.parse(LEAGUES["cs2"], dict(m, team1_score=1, team2_score=0))
    evs = diff(Snap(before.state, before.home_score, before.away_score, 0), after)
    assert [e.kind for e in evs] == ["goal"] and "Vitality Academy 세트 승리" in evs[0].text, evs


async def cmd(svc, text):
    cmd_, args, argstr = commands.parse(text, "sodambot")
    msg = FakeMsg(CHAT, ADMIN, text)
    await commands.dispatch(CmdCtx(svc, FakeBot(), msg, CHAT, ADMIN, Role.ADMIN, args, argstr), cmd_)
    return msg.replies[-1] if msg.replies else ""


@test
async def sports_command_shows_esports():
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "벳블리")
    svc.sports = Sports("123", db, svc.cfg.tz, fetch=Fetch(), clock=lambda: NOW, esports=True)
    out = await cmd(svc, ".스포츠 카스")
    assert "못 찾았어요" not in out and "Vitality Academy" in out and "European Pro League" in out, out
    assert out.index("Vitality Academy") < out.index("종료"), "진행 중 먼저"
    out = await cmd(svc, ".스포츠 롤")
    assert "West Point" in out, out
    # 배팅 내역 화면에 적힌 이름 그대로 (2026-10-11 백악관 '내 경기가 안 보여요' — 사이트마다 이름이 조금 다름)
    out = await cmd(svc, ".스포츠 State Wraith Pacific")
    assert "STATE" in out and "PCIFIC" in out and "United21" in out, out
    from sodam.sports import ui as sports_ui
    games = await sports_ui.UI(svc.sports).find_games("Vitality Academy Elite Klan Vella")
    assert [(g.home, g.away) for g in games][:1] == [("Vitality Academy", "Elite Klan")], games


if __name__ == "__main__":
    run_all()
