"""🏒 KHL (콘티넨탈 하키 리그): python tests/run_all.py sports_khl

실제 2026-10-08 벳블리: '.스포츠 KHL' · '.스포츠 콘티넨탈 하키 리그' → '리그·팀을 못 찾았어요' (ESPN·API-Sports 에 없음).
소스 = KHL 공식 앱 API (키 없음). 네트워크 없음 — tests/fixtures/sports/khl_events.json = 서버에서 받은 실제 응답.
"""
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import commands
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.sports import Sports
from sodam.sports.leagues import LEAGUES, find_league, find_team, ko_name
from sodam.sports.providers import KHL, KHL_URL, KST

test, run_all = runner()
RAW = json.loads((Path(__file__).parent / "fixtures" / "sports" / "khl_events.json").read_text(encoding="utf-8"))
NOW = RAW["fetched_at"]          # 2026-10-08 03:08 KST — 소치-스파르타크 0:3 · 디나모 M-드래곤스 1:1 진행 중
CHAT = -100881
ADMIN = fake_user(10, "관리자")


class Fetch:
    def __init__(self):
        self.calls = []
        self.events = RAW["events"]

    async def __call__(self, url, params, headers):
        self.calls.append((url, dict(params)))
        if url != KHL_URL:
            return {"events": []}
        assert params["order_direction"] == "desc" and params["q[start_at_lt_time_from_unixtime]"].isdigit()
        lt = int(params["q[start_at_lt_time_from_unixtime]"])
        evs = [e for e in self.events if e["event"]["start_at"] // 1000 < lt]
        page = int(params["page"])
        return evs[(page - 1) * 16: page * 16]


def provider():
    return KHL(Fetch(), True, clock=lambda: NOW)


@test
def league_and_teams_found_by_korean_english_russian():
    assert find_league("KHL").code == "khl" and find_league("콘티넨탈 하키 리그").code == "khl"
    assert find_league("러시아하키").code == "khl" and LEAGUES["khl"].sport == "hockey"
    assert find_team("SKA").src == "СКА" and find_team("악바르스").src == "Ак Барс" and find_team("cska").src == "ЦСКА"
    assert ko_name("Динамо Мн", "khl") == "디나모 민스크" and ko_name("ХК Сочи", "khl") == "소치"
    names = {e["event"][t]["name"] for e in RAW["events"] for t in ("team_a", "team_b")}
    assert all(ko_name(n, "khl") != n for n in names), [n for n in names if ko_name(n, "khl") == n]   # 실제 팀 전부 한국어


@test
async def live_finished_and_upcoming_games_parse():
    p = provider()
    live = await p.day(LEAGUES["khl"], date(2026, 10, 8))
    by = {(g.home, g.away): g for g in live}
    g = by[("ХК Сочи", "Спартак")]
    assert (g.state, g.home_score, g.away_score, g.detail) == ("in", 0, 3, "2피리어드"), g
    assert by[("Трактор", "Амур")].state == "pre" and by[("Трактор", "Амур")].home_score is None
    assert all(datetime.fromtimestamp(x.start, KST).date() == date(2026, 10, 8) for x in live), "다른 날 경기 섞임"
    old = {(g.home, g.away): g for g in await p.day(LEAGUES["khl"], date(2026, 10, 7))}
    assert (old[("Сибирь", "Салават Юлаев")].state, old[("Сибирь", "Салават Юлаев")].detail) == ("post", "연장 끝")
    assert old[("Металлург Мг", "Адмирал")].home_score == 3 and old[("Металлург Мг", "Адмирал")].detail == ""
    so = {(g.home, g.away): g for g in await p.day(LEAGUES["khl"], date(2026, 10, 6))}
    assert so[("Динамо Мн", "Торпедо")].detail == "슛아웃 끝"


@test
async def team_games_filters_by_team():
    games = await provider().team_games(LEAGUES["khl"], "СКА")
    assert games and all("СКА" in (g.home, g.away) for g in games), games


async def cmd(svc, text):
    cmd_, args, argstr = commands.parse(text, "sodambot")
    msg = FakeMsg(CHAT, ADMIN, text)
    await commands.dispatch(CmdCtx(svc, FakeBot(), msg, CHAT, ADMIN, Role.ADMIN, args, argstr), cmd_)
    return msg.replies[-1] if msg.replies else ""


@test
async def sports_command_shows_khl_live_scores():
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "벳블리")
    svc.sports = Sports("123", db, svc.cfg.tz, fetch=Fetch(), clock=lambda: NOW, khl=True)
    out = await cmd(svc, ".스포츠 KHL")
    assert "못 찾았어요" not in out and "소치" in out and "스파르타크" in out and "0" in out and "3" in out, out
    out = await cmd(svc, ".스포츠 콘티넨탈 하키 리그")
    assert "소치" in out, out
    out = await cmd(svc, ".스포츠 팀 SKA")
    assert "SKA" in out and "세베르스탈" in out, out


@test
async def tests_keep_khl_off_unless_asked():
    db = await make_db()
    sp = Sports("123", db, KST, fetch=Fetch())
    assert not [p for p in sp.providers if p.name == "khl" and p.enabled()], "가짜 fetch 테스트는 KHL 요청을 안 더함"


if __name__ == "__main__":
    run_all()
