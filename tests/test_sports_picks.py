"""🎯 승부 맞히기 · 🧠 AI 경기 분석 (sodam/sports/picks.py · analysis.py · panels/sportsdm.py, 2026-10-10 고객 '후바오 스포츠봇처럼
유저 추천픽·랭킹·나의 통계·분석'). 돈·포인트 걸기 없음, 점수만: python tests/run_all.py sports_picks"""
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner
from test_sports import FakeFetch, Game, ts
from test_sports_watch import Clock, Script

import sodam.panels  # noqa: F401
from sodam.panels import sportsdm as D
from sodam.sports import Sports, analysis, picks
from sodam.sports.providers import Row

test, run_all = runner()
ROOM = -100901
A, B_, C = fake_user(31, "가나"), fake_user(32, "다라"), fake_user(33, "마바")
KICK = ts(2026, 10, 11, 20, 0)


def epl(state="pre", hs=None, as_=None, key="espn:501", home="Tottenham Hotspur", away="Arsenal", start=KICK):
    return Game(key, "epl", start, home, away, hs, as_, state, "", (), "20261011")


async def setup(now=KICK - 3600):
    db = await make_db()
    svc = await make_svc(db)
    clock = Clock(now)
    sp = Sports("123", db, svc.cfg.tz, fetch=FakeFetch(), clock=clock)
    src = Script()
    sp.feed.providers = [src]
    svc.sports = sp
    src.games["epl"] = [epl()]
    for u in (A, B_, C):
        await db.upsert_user(u)
    return SimpleNamespace(svc=svc, db=db, sp=sp, src=src, clock=clock)


@test
async def pick_before_start_change_allowed_after_start_refused():
    e = await setup()
    g = epl()
    assert await picks.pick(e.db, A.id, g, "h", now=e.clock.t) == "ok"
    assert await picks.pick(e.db, A.id, g, "d", now=e.clock.t) == "changed"
    assert await picks.mine(e.db, A.id, g.key) == "draw"
    assert await picks.pick(e.db, B_.id, g, "a", now=KICK) == "started", "시작 시각이 되면 못 고름"
    assert await picks.pick(e.db, B_.id, epl("in", 0, 0), "a", now=KICK - 60) == "started"
    nhl = Game("espn:9", "nhl", KICK, "Boston Bruins", "Philadelphia Flyers")
    assert await picks.pick(e.db, B_.id, nhl, "d", now=e.clock.t) == "nodraw", "하키는 무승부 없음"


@test
async def settle_pays_win_draw_and_voids_cancelled_once():
    e = await setup()
    g = epl()
    await picks.pick(e.db, A.id, g, "h", now=e.clock.t)
    await picks.pick(e.db, B_.id, g, "a", now=e.clock.t)
    await picks.pick(e.db, C.id, g, "d", now=e.clock.t)
    e.clock.t = KICK + 600
    e.src.games["epl"] = [epl("in", 1, 0)]
    assert await picks.settle(e.db, e.sp.feed, now=e.clock.t) == 0, "진행 중엔 정산 안 함"
    e.clock.t = KICK + 7200
    e.src.games["epl"] = [epl("post", 2, 1)]
    assert await picks.settle(e.db, e.sp.feed, now=e.clock.t) == 3
    assert await picks.settle(e.db, e.sp.feed, now=e.clock.t) == 0, "두 번 정산 안 함"
    sa, sb = await picks.stats(e.db, A.id, now=e.clock.t), await picks.stats(e.db, B_.id, now=e.clock.t)
    assert (sa["points"], sa["wins"], sa["rate"], sa["streak"]) == (picks.WIN, 1, 100, 1), sa
    assert (sb["points"], sb["rate"]) == (0, 0)
    # 무승부 맞히면 15점 · 취소는 무효
    g2, g3 = epl(key="espn:502", start=KICK + 86400), epl(key="espn:503", start=KICK + 86400)
    e.clock.t = KICK + 3600 * 5
    await picks.pick(e.db, C.id, g2, "d", now=e.clock.t)
    await picks.pick(e.db, A.id, g3, "h", now=e.clock.t)
    e.clock.t = KICK + 86400 + 7200
    e.src.games["epl"] = [epl("post", 1, 1, key="espn:502", start=KICK + 86400), epl("postponed", key="espn:503", start=KICK + 86400)]
    await picks.settle(e.db, e.sp.feed, now=e.clock.t)
    assert (await picks.stats(e.db, C.id, now=e.clock.t))["points"] == picks.DRAW_WIN
    row = await e.db._one("SELECT result, points FROM sports_picks WHERE user_id=? AND game='espn:503'", (A.id,))
    assert row["result"] == "void" and row["points"] == 0


@test
async def ranking_week_and_room_members_only():
    e = await setup()
    await e.db.ensure_chat(ROOM, "방")
    await e.db.touch_member(ROOM, A.id)
    for u, side in ((A, "h"), (B_, "h"), (C, "a")):
        await picks.pick(e.db, u.id, epl(), side, now=e.clock.t)
    e.clock.t = KICK + 7200
    e.src.games["epl"] = [epl("post", 1, 0)]
    await picks.settle(e.db, e.sp.feed, now=e.clock.t)
    allr = await picks.ranking(e.db, now=e.clock.t)
    assert {r[0] for r in allr} == {A.id, B_.id}, "0점은 순위에 안 나옴"
    room = await picks.ranking(e.db, chat_id=ROOM, now=e.clock.t)
    assert [r[0] for r in room] == [A.id], "방 순위 = 이 방 멤버만"
    assert await picks.ranking(e.db, now=e.clock.t + 8 * 86400) == [], "지난주 점수는 이번 주 순위에 없음"
    assert len(await picks.ranking(e.db, week=False, now=e.clock.t + 8 * 86400)) == 2
    st = await picks.stats(e.db, A.id, now=e.clock.t)
    assert st["week_rank"] == 1


@test
async def voided_game_rescheduled_can_be_picked_again():
    """리뷰 2026-10-10: 연기로 무효가 된 경기를 같은 key 로 다시 잡으면 고르기가 '골랐어요'만 하고 저장은 안 됐음."""
    e = await setup()
    await picks.pick(e.db, A.id, epl(), "h", now=e.clock.t)
    e.clock.t = KICK + 600
    e.src.games["epl"] = [epl("postponed")]
    await picks.settle(e.db, e.sp.feed, now=e.clock.t)
    later = epl(start=KICK + 7 * 86400)
    assert await picks.pick(e.db, A.id, later, "a", now=e.clock.t) == "changed"
    row = await e.db._one("SELECT pick, result, start FROM sports_picks WHERE user_id=?", (A.id,))
    assert (row["pick"], row["result"], row["start"]) == ("away", None, KICK + 7 * 86400)


@test
async def daily_limit():
    e = await setup()
    for i in range(picks.DAILY_MAX):
        assert await picks.pick(e.db, A.id, epl(key=f"espn:{1000 + i}"), "h", now=e.clock.t) == "ok"
    assert await picks.pick(e.db, A.id, epl(key="espn:9999"), "h", now=e.clock.t) == "full"


class LLM:
    def __init__(self):
        self.calls = []

    async def json(self, system, user, **kw):
        self.calls.append(user)
        return {"lines": ["토트넘 최근 3연승", "아스널 원정 약함", "맞대결 팽팽"], "lean": "home", "why": "최근 흐름 <b>"}


@test
async def analysis_uses_facts_only_caches_and_escapes():
    e = await setup()

    async def standings(lg):
        return [Row(1, "Arsenal", 7, 5, 1, 1, 16), Row(4, "Tottenham Hotspur", 7, 4, 1, 2, 13)]

    async def team_games(lg, team):
        return [epl("post", 2, 0, key="espn:401", start=KICK - 7 * 86400),
                Game("espn:402", "epl", KICK - 14 * 86400, "Chelsea", team, 1, 3, "post", "", (), "x")]
    e.sp.feed.standings = standings
    e.sp.feed.team_games = team_games
    await picks.pick(e.db, A.id, epl(), "h", now=e.clock.t)
    e.svc.llm = LLM()
    text = await analysis.analyze(e.svc, epl(), now=e.clock.t)
    facts = e.svc.llm.calls[0]
    assert "<facts>" in facts and "토트넘 4위" in facts and "아스널 1위" in facts and "승점 16" in facts, facts
    assert "맞대결" in facts and "우리 사용자 1명 예상: 토트넘 승 100%" in facts, facts
    assert "🎯 AI 예상: <b>토트넘 우세</b>" in text and "흐름 <b>" not in text and "재미용" in text, text
    again = await analysis.analyze(e.svc, epl(), now=e.clock.t + 60)
    assert again == text and len(e.svc.llm.calls) == 1, "경기마다 한 번만 AI"
    e.svc.llm = None
    plain = await analysis.analyze(e.svc, epl(key="espn:777"), now=e.clock.t)
    assert "AI 분석" not in plain and "기록" in plain, "AI 없으면 기록만"


@test
async def dm_menu_pick_flow_and_record_screen():
    e = await setup()
    bot = FakeBot()
    c = SimpleNamespace(svc=e.svc, bot=bot, uid=A.id, cid=None, args=["0", "epl"],
                        arg=lambda i: c.args[i] if len(c.args) > i else "")
    screen = await D.s_picks(c)
    data = [b.callback_data for row in screen.kb.inline_keyboard for b in row]
    home_btn = next(d for d in data if d.startswith("m:sxpx:") and ":h:" in d)
    assert all(len(d.encode()) <= 64 for d in data) and any(d.startswith("m:sxa:") for d in data)
    c.args = home_btn.split(":")[2:]
    out = await D.r_pick(c)
    assert out.toast.startswith("🎯 토트넘") and "✅ 토트넘" in out.text, (out.toast, out.text)
    rec = await D.s_record(c)
    assert "결과 기다리는 경기 1개" in rec.text


@test
async def room_card_and_button_press():
    e = await setup()
    text, rows = await D.room_card(e.svc, ROOM, "EPL")
    assert "토트넘 vs 아스널" in text and rows and rows[0][0].callback_data.startswith("sgp:epl:espn:501:")
    answered = []
    q = SimpleNamespace(from_user=B_, message=SimpleNamespace(chat=SimpleNamespace(id=ROOM)),
                        answer=lambda t=None, show_alert=False: _rec(answered, t))
    data = rows[0][-1].callback_data
    await D.on_room_pick(e.svc, FakeBot(), q, data.split(":")[1:])
    assert answered and "아스널" in answered[0] and await picks.mine(e.db, B_.id, "espn:501") == "away"
    row = await e.db._one("SELECT chat_id FROM sports_picks WHERE user_id=?", (B_.id,))
    assert row["chat_id"] == ROOM


async def _rec(box, t):
    box.append(t)


@test
async def settle_tick_hook_runs_once_a_minute():
    e = await setup()
    await picks.pick(e.db, A.id, epl(), "h", now=e.clock.t)
    D._last_settle[0] = 0
    e.clock.t = KICK + 7200
    e.src.games["epl"] = [epl("post", 1, 0)]
    await D.settle_tick(e.svc, None)
    assert (await picks.stats(e.db, A.id, now=e.clock.t))["points"] == picks.WIN


if __name__ == "__main__":
    run_all()
