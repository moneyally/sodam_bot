"""⚡ 라이브 카드·자동 라이브 (sodam/sports/cards.py, 설계 docs/SPORTS_ENGAGE_DESIGN.md §3 A, 2026-10-11 '말 안 해도 자동으로 오는 알림 + 버튼').
python tests/run_all.py sports_cards"""
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner
from test_sports import FakeFetch, Game, ts
from test_sports_watch import Clock, Script

import sodam.panels  # noqa: F401
from sodam.sports import Sports, cards

test, run_all = runner()
ROOM = -100950
ADMIN, MEMBER = fake_user(41, "방장"), fake_user(42, "멤버")
KICK = ts(2026, 10, 11, 20, 0)


def epl(state="pre", hs=None, as_=None, detail="", key="espn:601", goals=(), home="Tottenham Hotspur", away="Arsenal"):
    return Game(key, "epl", KICK, home, away, hs, as_, state, detail, tuple(goals), "20261011")


class Bot(FakeBot):
    username = "sodambot"

    async def send_chat_action(self, chat_id, action, **kw):
        pass


async def setup(auto="big", now=KICK - 3600):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN.id})
    clock = Clock(now)
    sp = Sports("123", db, svc.cfg.tz, fetch=FakeFetch(), clock=clock)
    src = Script()
    sp.feed.providers = [src]
    svc.sports = sp
    await db.ensure_chat(ROOM, "방")
    await db.set_setting(ROOM, "sports_auto", auto)
    src.games["epl"] = [epl()]
    return SimpleNamespace(svc=svc, db=db, sp=sp, src=src, clock=clock, bot=Bot(admins=[ADMIN]))


async def at(e, t, *games):
    e.clock.t = t
    e.src.games["epl"] = list(games)
    await e.sp.run_alerts(e.bot)


def cards_sent(e):
    return [c for c in e.bot.named("send_message") if c[1] == ROOM and c[3].get("reply_markup") is not None
            and "sgc:" in str(c[3]["reply_markup"].inline_keyboard)]


def edits(e):
    return [c for c in e.bot.named("edit_text") if c[1] == ROOM]


@test
async def auto_room_gets_one_card_that_updates_without_anyone_asking():
    e = await setup()
    await e.sp.run_alerts(e.bot)
    await at(e, KICK + 30, epl("in", 0, 0, "1'"))
    [card] = cards_sent(e)
    assert "🔴 LIVE" in card[2] and "토트넘 0-0 아스널" in card[2] and card[3]["disable_notification"]
    assert {b.text for b in card[3]["reply_markup"].inline_keyboard[0]} == {"📩 1:1로도", "🧠 분석", "🔕 끄기"}
    await at(e, KICK + 40, epl("in", 0, 0, "2'"))
    assert not edits(e), "시간만 바뀐 건 30초에 한 번"
    await at(e, KICK + 70, epl("in", 0, 0, "3'"))
    assert len(edits(e)) == 1
    await at(e, KICK + 80, epl("in", 1, 0, "4'", goals=[("4'", "home", "Son", "")]))
    assert len(edits(e)) == 2 and "Son 토트넘" in edits(e)[-1][2], ("골은 바로 고침", edits(e))
    assert any("골!" in c[2] for c in e.bot.named("send_message") if c[1] == ROOM), "골은 새 글로도 (폰 알림)"
    await at(e, KICK + 7000, epl("post", 2, 1, goals=[("4'", "home", "Son", "")]))
    last = edits(e)[-1]
    assert "🏁 종료" in last[2] and "최종" in last[2]
    assert [b.text for b in last[3]["reply_markup"].inline_keyboard[0]] == ["🧠 분석"]
    assert len(cards_sent(e)) == 1, "한 경기 = 카드 1장"


@test
async def at_most_four_cards_and_off_means_none_unless_a_game_was_asked_for():
    e = await setup()
    games = lambda st: [epl(st, 0 if st == "in" else None, 0 if st == "in" else None, key=f"espn:{700 + i}", home=f"H{i}", away=f"A{i}")
                        for i in range(6)]
    e.src.games["epl"] = games("pre")
    await e.sp.run_alerts(e.bot)
    await at(e, KICK + 30, *games("in"))
    assert len(cards_sent(e)) == cards.MAX_CARDS
    off = await setup(auto="off")
    await off.sp.run_alerts(off.bot)
    await at(off, KICK + 30, epl("in", 0, 0))
    assert not cards_sent(off), "자동 꺼짐 + 콕 집은 알림 없음 = 카드 없음"
    await off.sp.alerts.watch(ROOM, MEMBER.id, "epl", "", "espn:601", "x", expires=KICK + 99999)
    await at(off, KICK + 60, epl("in", 1, 0))
    assert len(cards_sent(off)) == 1, "콕 집은 경기는 자동 꺼져 있어도 카드"


def press(e, user, op, key="espn:601"):
    box = []

    async def answer(text=None, show_alert=False):
        box.append(text)

    async def edit_message_text(text, **kw):
        box.append(("edited", text))
    q = SimpleNamespace(from_user=user, message=SimpleNamespace(chat=SimpleNamespace(id=ROOM)), answer=answer,
                        edit_message_text=edit_message_text)
    return q, box


@test
async def buttons_dm_analysis_once_and_admin_only_off_stops_alerts():
    e = await setup()
    await e.sp.run_alerts(e.bot)
    await at(e, KICK + 30, epl("in", 0, 0))
    q, box = press(e, MEMBER, "dm")
    await cards.on_button(e.svc, e.bot, q, ["dm", "espn", "601"])
    assert "1:1" in box[0] and (await e.sp.alerts.watches(MEMBER.id))[0]["game"] == "espn:601"
    q, box = press(e, MEMBER, "an")
    await cards.on_button(e.svc, e.bot, q, ["an", "espn", "601"])
    q2, box2 = press(e, ADMIN, "an")
    await cards.on_button(e.svc, e.bot, q2, ["an", "espn", "601"])
    assert "위에" in box2[0], "분석은 방에 한 번만"
    assert sum("🧠" in c[2] for c in e.bot.named("send_message") if c[1] == ROOM) == 1
    q, box = press(e, MEMBER, "off")
    await cards.on_button(e.svc, e.bot, q, ["off", "espn", "601"])
    assert "관리자" in box[0]
    q, box = press(e, ADMIN, "off")
    await cards.on_button(e.svc, e.bot, q, ["off", "espn", "601"])
    n = len([c for c in e.bot.named("send_message") if c[1] == ROOM])
    await at(e, KICK + 600, epl("in", 1, 0))
    assert len([c for c in e.bot.named("send_message") if c[1] == ROOM]) == n, "끈 경기는 골 알림도 안 옴"


@test
async def quiet_hours_no_new_cards():
    e = await setup()
    await e.db.set_setting(ROOM, "sports_quiet", "00-23")
    await e.sp.run_alerts(e.bot)
    await at(e, KICK + 30, epl("in", 0, 0))
    assert not cards_sent(e)


if __name__ == "__main__":
    run_all()
