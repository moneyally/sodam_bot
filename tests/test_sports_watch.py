"""🔔 경기 콕 집은 알림·개인 1:1 알림·세계 경기·8초 폴링 (2026-10-10 벳블리 '소담아 NHL 보스턴 필라델피아 경기 득점하면 말해줄래'
→ 낱말 알림 규칙이 생기고 '실시간 득점 알림은 안 돼' + 고객 '라이브스코어보다 빠르게·전 세계 경기'): python tests/run_all.py sports_watch"""
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner
from telegram.error import Forbidden
from test_sports import KST, FakeFetch, Game, Provider, ts

import sodam.panels  # noqa: F401
from sodam import tools
from sodam.panels import sportsdm as D
from sodam.permissions import Role
from sodam.sports import Sports, alerts, fmt
from sodam.sports.providers import league_title
from sodam.tools import ToolCtx

test, run_all = runner()
ROOM = -100900
ADMIN, MEMBER = fake_user(10, "방장"), fake_user(20, "멤버")
KICK = ts(2026, 10, 11, 8, 0)          # 한국 아침 8시 (NHL 저녁 경기)


def nhl(state="pre", hs=None, as_=None, key="espn:77"):
    return Game(key, "nhl", KICK, "Philadelphia Flyers", "Boston Bruins", hs, as_, state, "2피리어드" if state == "in" else "",
                (), "20261010")


class Script(Provider):
    name = "script"

    def __init__(self):
        self.games = {}

    def supports(self, lg):
        return lg.code in ("nhl", "epl", "world", "mlb", "nba")

    async def day(self, lg, d, only=None):
        from datetime import datetime
        return [g for g in self.games.get(lg.code, []) if datetime.fromtimestamp(g.start, KST).date() == d]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Bot(FakeBot):
    blocked: set = set()

    async def send_chat_action(self, chat_id, action, **kw):
        if chat_id in self.blocked:
            raise Forbidden("bot can't initiate conversation with a user")

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        return await super().send_message(chat_id, text, **kw)


async def setup(now=KICK - 3600):
    db = await make_db()
    svc = await make_svc(db)
    clock = Clock(now)
    sp = Sports("123", db, svc.cfg.tz, fetch=FakeFetch(), clock=clock)
    src = Script()
    sp.feed.providers = [src]
    svc.sports = sp
    await db.ensure_chat(ROOM, "벳블리")
    await db.set_setting(ROOM, "sports_enabled", True)
    src.games["nhl"] = [nhl()]
    return SimpleNamespace(svc=svc, db=db, sp=sp, src=src, clock=clock, bot=Bot())


def sent(e, chat):
    return [c[2] for c in e.bot.named("send_message") if c[1] == chat]


async def ask(e, user, role, chat, **a):
    ctx = ToolCtx(e.svc, e.bot, chat, user, role, {})
    return await tools.t_sports(ctx, {"action": "alert", **a})


async def step(e, t, g):
    e.clock.t = t
    e.src.games["nhl"] = [g]
    await e.sp.run_alerts(e.bot)


@test
async def admin_in_room_gets_goal_and_final_for_that_game_then_it_turns_off():
    e = await setup()
    out = await ask(e, ADMIN, Role.ADMIN, ROOM, query="NHL 보스턴 필라델피아")
    assert "알림" in out and "이 방에" in out, out
    [w] = await e.sp.alerts.watches(ROOM)
    assert w["game"] == "espn:77" and w["expires"] == KICK + alerts.WATCH_KEEP
    await e.sp.run_alerts(e.bot)                                   # 처음엔 저장만
    await step(e, KICK + 30, nhl("in", 0, 0))
    await step(e, KICK + 600, nhl("in", 0, 1))
    assert any("골!" in t and "보스턴 브루인스 1-0 필라델피아 플라이어스" in t for t in sent(e, ROOM)), sent(e, ROOM)
    await step(e, KICK + 9000, nhl("post", 2, 3))
    assert "경기 종료" in sent(e, ROOM)[-1]
    assert not await e.sp.alerts.watches(ROOM), "끝난 경기 하나짜리 알림은 정리"


@test
async def member_asks_in_room_and_gets_it_in_their_own_dm():
    e = await setup()
    out = await ask(e, MEMBER, Role.MEMBER, ROOM, query="보스턴 필라델피아")      # 리그 없이도 NHL 경기 찾음
    assert "1:1 로" in out and not await e.sp.alerts.watches(ROOM), out
    await e.sp.run_alerts(e.bot)
    await step(e, KICK + 30, nhl("in", 0, 0))
    await step(e, KICK + 600, nhl("in", 1, 0))
    assert any("골!" in t for t in sent(e, MEMBER.id)) and not sent(e, ROOM)
    e.bot.blocked = {MEMBER.id}
    await step(e, KICK + 900, nhl("in", 2, 0))
    assert not await e.sp.alerts.watches(MEMBER.id), "1:1 을 막으면 그 사람 알림은 지움"
    e.bot.blocked = set()


@test
async def member_who_never_opened_dm_is_told_how_and_nothing_saved():
    e = await setup()
    e.bot.blocked = {MEMBER.id}
    out = await ask(e, MEMBER, Role.MEMBER, ROOM, query="NHL 보스턴 필라델피아")
    assert "1:1 대화를 한 번 열어야" in out and not await e.sp.alerts.watches(MEMBER.id)
    e.bot.blocked = set()


@test
async def finished_or_unknown_game_is_said_plainly_and_team_falls_back_to_following():
    e = await setup(KICK + 9000)
    e.src.games["nhl"] = [nhl("post", 2, 3)]
    out = await ask(e, ADMIN, Role.ADMIN, ROOM, query="NHL 보스턴 필라델피아")
    assert "이미 끝났" in out and not await e.sp.alerts.watches(ROOM), out
    out = await ask(e, ADMIN, Role.ADMIN, ROOM, query="토트넘")
    [w] = await e.sp.alerts.watches(ROOM)
    assert w["team"] == "Tottenham Hotspur" and not w["game"] and "계속" in out, out


@test
async def same_game_seen_through_world_soccer_still_reaches_epl_followers():
    e = await setup(KICK - 3600)
    await e.sp.alerts.follow(ROOM, "epl", "", "EPL", 1)
    g = lambda st, h=None, a=None, lg="epl": Game("espn:5", lg, KICK, "Tottenham Hotspur", "Arsenal", h, a, st, "", (), "20261010",
                                                  title="EPL" if lg == "world" else "")
    await e.sp.alerts.watch(MEMBER.id, MEMBER.id, "world", "", "", "세계 축구", "goals")
    e.src.games.update(epl=[g("pre")], world=[g("pre", lg="world")])
    await e.sp.run_alerts(e.bot)
    e.clock.t = KICK + 30
    e.src.games.update(epl=[g("in", 0, 0)], world=[g("in", 0, 0, "world")])
    await e.sp.run_alerts(e.bot)
    assert sent(e, ROOM) and sent(e, MEMBER.id), (sent(e, ROOM), sent(e, MEMBER.id))


@test
async def game_watch_ignores_other_games_of_the_same_league():
    e = await setup()
    other = lambda st, h=None, a=None: Game("espn:78", "nhl", KICK, "New York Rangers", "Toronto Maple Leafs", h, a, st, "", (), "20261010")
    await ask(e, ADMIN, Role.ADMIN, ROOM, query="NHL 보스턴 필라델피아")
    e.src.games["nhl"] = [nhl(), other("pre")]
    await e.sp.run_alerts(e.bot)
    for t, sc in ((KICK + 30, (0, 0)), (KICK + 600, (1, 0))):
        e.clock.t = t
        e.src.games["nhl"] = [nhl("in", 0, 0), other("in", *sc)]
        await e.sp.run_alerts(e.bot)
    assert not any("레인저스" in t for t in sent(e, ROOM)), sent(e, ROOM)


@test
def world_titles_read_from_season_slug():
    assert league_title("2026-27-scottish-championship") == "Scottish Championship"
    assert league_title("") == ""
    g = Game("espn:1", "world", KICK, "A", "B", title="Saudi Pro League")
    assert fmt.tag(g) == "[Saudi Pro League]"


@test
async def dm_menu_live_screen_bell_and_my_alerts():
    e = await setup(KICK + 60)
    e.src.games["nhl"] = [nhl("in", 1, 0)]
    c = SimpleNamespace(svc=e.svc, bot=e.bot, uid=MEMBER.id, cid=None, args=[], arg=lambda i: c.args[i] if len(c.args) > i else "")
    home = await D.s_home(c)
    assert "8초" in home.text and "m:spl" in str(home.kb.inline_keyboard)
    live = await D.s_live(c)
    assert "전 세계 1경기" in live.text and "m:spl:hockey" in str(live.kb.inline_keyboard), live.text
    c.args = ["hockey"]
    live = await D.s_live(c)
    assert "보스턴 브루인스 0-1" in live.text or "1-0" in live.text, live.text
    bell = [b for row in live.kb.inline_keyboard for b in row if b.text.startswith("🔔")][0]
    tok = bell.callback_data.split(":")[2]
    t = e.svc.menu_tokens[tok]
    assert (await D.t_game(c, t.arg)).toast.startswith("🔔")
    [w] = await e.sp.alerts.watches(MEMBER.id)
    assert w["game"] == "espn:77"
    mine = await D.s_watches(c)
    assert "보스턴" in mine.text and "🗑" in str(mine.kb.inline_keyboard)
    await D.t_del(c, w["id"])
    assert not await e.sp.alerts.watches(MEMBER.id)


@test
async def dm_menu_add_by_text():
    e = await setup()
    c = SimpleNamespace(svc=e.svc, bot=e.bot, uid=MEMBER.id, cid=0, args=[], arg=lambda i: "")
    ok, out = await D.in_add(c, SimpleNamespace(text="NHL 보스턴 필라델피아"))
    assert ok and "1:1 로" in out, out
    ok, out = await D.in_add(c, SimpleNamespace(text="없는팀이름123"))
    assert not ok


@test
async def polls_every_8_seconds_while_live():
    assert alerts.LIVE_EVERY <= 10, "경기 중 확인 간격 (ESPN 은 7초마다 새로 냄)"
    e = await setup(KICK + 60)
    await e.sp.alerts.watch(ROOM, ADMIN.id, "nhl", "", "espn:77", "x", expires=KICK + 99999)
    calls = []
    real = e.src.day

    async def count(lg, d, only=None):
        calls.append(e.clock.t)
        return await real(lg, d, only)
    e.src.day = count
    e.src.games["nhl"] = [nhl("in", 0, 0)]
    for _ in range(8):                                  # 4초 틱 8번 = 32초
        await e.sp.run_alerts(e.bot)
        e.clock.t += 4
    assert 4 <= len(calls) <= 6, calls


if __name__ == "__main__":
    run_all()
