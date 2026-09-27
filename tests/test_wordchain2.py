"""끝말잇기 리디자인: python tests/run_all.py wordchain2

판정 = 표준국어대사전 명사 목록 (AI 0). 자유 모드 = 선착순, 늦은·틀린 답엔 글 대신 반응.
차례 모드 = [🙋 참가] → 차례 → 시간 초과 탈락 → 마지막 1명 우승 (참고: On9 Word Chain 의 참가·차례·탈락 구조).
"""
import asyncio

from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import games
from sodam.casino import core

test, run_all = runner()
CHAT = -100777
A, B, C = fake_user(20, "가나"), fake_user(21, "다라"), fake_user(22, "마바")


async def setup(key="끝말잇기"):
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot()
    assert "시작" in await svc.games.start(bot, CHAT, A.id, key)
    return db, svc, bot, svc.games.active[CHAT]


def said(bot):
    return [c[2] for c in bot.named("send_message") if c[1] == CHAT]


def reactions(bot):
    return [(c[2], c[3]) for c in bot.named("reaction")]


async def text(svc, user, word, mid=None):
    m = FakeMsg(CHAT, user, word, message_id=mid or (user.id * 100 + len(word)))
    return await svc.games.on_text(m, word), m


@test
async def dictionary_judges_and_first_answer_wins_late_ones_get_reaction():
    db, svc, bot, g = await setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    orig, games.pick_next = games.pick_next, lambda w, used: "표범"
    try:
        (ok_a, _), (ok_b, mb) = await asyncio.gather(text(svc, A, "차표"), text(svc, B, "차고"))  # 동시에
    finally:
        games.pick_next = orig
    assert ok_a and ok_b and reactions(bot) == [(mb.message_id, "🙈")], reactions(bot)
    assert any("차표 → 🤖 <b>표범</b>" in t for t in said(bot)) and g.last == "표범"
    assert (await text(svc, C, "범아가"))[0] and reactions(bot)[-1][1] == "🤔", "사전에 없는 말"
    assert (await text(svc, C, "표범"))[0] is False, "표로 시작 안 함 → 평범한 채팅"
    g.used.add("범인")
    assert (await text(svc, C, "범인"))[0] and reactions(bot)[-1][1] == "🤨", "이미 나온 말"
    assert not await svc.games.on_text(FakeMsg(CHAT, C, "범 같은 얘기"), "범 같은 얘기")
    assert await core.balance(db, CHAT, A.id) == 1 and await core.balance(db, CHAT, B.id) == 0
    g.cancel_timer()


@test
async def bot_stuck_player_wins_six():
    db, svc, bot, g = await setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    orig, games.pick_next = games.pick_next, lambda w, used: None
    try:
        await text(svc, A, "차표")
    finally:
        games.pick_next = orig
    assert g.finished and "제가 졌어요" in said(bot)[-1] and await core.balance(db, CHAT, A.id) == 6


async def press(svc, user, data):
    q = FakeQuery(CHAT, user, data)
    q.message.chat_id = CHAT
    await svc.games.on_callback(q, data.split(":")[1:])
    return q


@test
async def turn_mode_join_turns_elimination_and_winner():
    db, svc, bot, g = await setup("끝말잇기 차례")
    assert "참가" in said(bot)[0]
    for u in (A, B, C):
        await press(svc, u, "wc:j")
    q = await press(svc, B, "wc:j")
    assert "이미" in q.answers[-1][0]
    q = await press(svc, B, "wc:go")                               # 시작한 사람·관리자만
    assert q.answers[-1][1] is True and g.joining
    await press(svc, A, "wc:go")
    assert not g.joining and len(g.players) == 3
    g.last, g.used = "기차", {"기차"}
    cur, other = g.players[0], g.players[1]
    users = {u.id: u for u in (A, B, C)}
    assert (await text(svc, users[other[0]], "차표"))[0] is False, "차례 아닌 사람 말은 평범한 채팅"
    assert (await text(svc, users[cur[0]], "차표"))[0] and g.players[0] == other and g.last == "차표"
    g.cancel_timer()
    await g._turn_timeout()                                        # other 탈락
    assert "탈락" in said(bot)[-1] and len(g.players) == 2
    g.cancel_timer()
    await g._turn_timeout()                                        # 한 명 남음 → 우승
    assert g.finished and "우승" in said(bot)[-1]
    winner = g.players[0][0]
    assert await core.balance(db, CHAT, winner) >= 5 + 2 * 3


@test
async def turn_mode_cancels_without_enough_players():
    db, svc, bot, g = await setup("끝말잇기 차례")
    await press(svc, A, "wc:j")
    g.cancel_timer()
    await g._start()
    assert g.finished and "취소" in said(bot)[-1]


@test
async def no_dead_end_start_or_bot_word():
    """'구름'·'여름'(름으로 시작하는 흔한 말 없음)으로 시작해 아무도 못 잇던 문제 · 봇이 한방 단어로 이기지 않게."""
    games.load_words()
    for w in games.WordChain.STARTERS:
        assert games.pick_next(w, {w}), w
    dead = next(w for ws in games._COMMON.values() for w in ws if not games.can_follow(w))   # 흔한 한방 단어 하나
    pools = []
    orig, games.random.choice = games.random.choice, lambda pool: pools.append(pool) or pool[0]
    try:
        games.pick_next("가" + dead[0] if dead[0] != "가" else "나" + dead[0], set())    # dead 로 이을 수 있는 자리
    finally:
        games.random.choice = orig
    assert pools and dead not in pools[0] and all(games.can_follow(w) for w in pools[0]), dead


@test
async def bot_falls_back_to_full_dictionary():
    """흔한 말로 못 이으면 사전 전체에서 (흔한 말만 쓰던 봇이 쉽게 지던 것)."""
    games.load_words()
    ch = next(c for c, ws in games._BY_FIRST.items() if c not in games._COMMON and any(games.can_follow(w) for w in ws))
    nxt = games.pick_next("가" + ch, set())
    assert nxt and nxt[0] == ch and games.can_follow(nxt), (ch, nxt)
