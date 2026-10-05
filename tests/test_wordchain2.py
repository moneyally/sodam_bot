"""끝말잇기 리디자인: python tests/run_all.py wordchain2

판정 = 표준국어대사전 명사 목록 (AI 0). 자유 모드 = 선착순, 늦은·틀린 답엔 글 대신 반응.
차례 모드 = [🙋 참가] → 차례 → 시간 초과 탈락 → 마지막 1명 우승 (참고: On9 Word Chain 의 참가·차례·탈락 구조).
"""
import asyncio

from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import games, wordbot
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


def plays(word, line=""):
    async def move(*a, **k):
        await asyncio.sleep(0)          # 봇이 생각하는 사이에 다른 답이 끼어들 수 있게
        return word, line
    return move


async def text(svc, user, word, mid=None):
    m = FakeMsg(CHAT, user, word, message_id=mid or (user.id * 100 + len(word)))
    return await svc.games.on_text(m, word), m


@test
async def dictionary_judges_and_first_answer_wins_late_ones_get_reaction():
    db, svc, bot, g = await setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    orig, wordbot.move = wordbot.move, plays("표범")
    try:
        (ok_a, _), (ok_b, mb) = await asyncio.gather(text(svc, A, "차표"), text(svc, B, "차고"))  # 동시에
    finally:
        wordbot.move = orig
    assert ok_a and ok_b and reactions(bot) == [(mb.message_id, "🙈")], reactions(bot)
    assert any("차표 → 🤖 <b>표범</b>" in t for t in said(bot)) and g.last == "표범"
    assert (await text(svc, C, "범아가"))[0] and reactions(bot)[-1][1] == "🤔", "사전에 없는 말"
    ok, m = await text(svc, C, "나무")                                # 첫 글자 틀린 사전 낱말 → 한 번만 알려줌
    assert ok and "'표범'" in m.replies[-1], m.replies
    assert (await text(svc, C, "나무"))[0] is False, "두 번째부턴 평범한 채팅"
    assert (await text(svc, C, "ㅋㅋㅋ"))[0] is False
    assert (await text(svc, C, "표지"))[0] and reactions(bot)[-1][1] == "🙈", "차표에 늦게 이은 말"
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
    orig, wordbot.move = wordbot.move, plays(None)
    try:
        await text(svc, A, "차표")
    finally:
        wordbot.move = orig
    assert g.finished and "제가 졌어요" in said(bot)[-1] and await core.balance(db, CHAT, A.id) == 6


async def press(svc, user, data):
    g = svc.games.active.get(CHAT)
    if data.count(":") == 1 and getattr(g, "gid", None):             # 버튼엔 판 표시(gid)가 붙음
        data += ":" + g.gid
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


class Call:
    def __init__(self, name, args, cid="c1"):
        self.type, self.id = "function", cid
        self.function = type("F", (), {"name": name, "arguments": args})()


class ScriptLLM:
    """도구 호출을 대본대로 돌려주는 가짜 LLM (소담이 선수)."""
    enabled = True

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    async def chat(self, messages, **kw):
        self.seen.append((messages, kw))
        return type("M", (), {"content": "", "tool_calls": [self.steps.pop(0)] if self.steps else []})()


@test
async def ai_player_uses_tools_and_code_rejects_bad_words():
    """클로드코드식: LLM 이 find_words 로 후보를 보고 play → 코드가 검사, 틀리면 '안 됨' 돌려주고 다시."""
    db, svc, bot, g = await setup()
    g.cancel_timer()
    used = {"기차", "차표"}
    # 후보는 무작위로 섞임 → 코드 검사(흔한 말 3개↑)를 통과하는 것 중에서 (서버 배포 테스트에서 가끔 떨어지던 원인, 2026-10-05)
    good = wordbot.candidates("차표", used, min_common=wordbot.MIN_COMMON["normal"])[0][0]
    svc.llm = ScriptLLM([Call("find_words", "{}"), Call("play", '{"word": "표가나다라", "line": "x"}', "c2"),
                         Call("play", '{"word": "%s", "line": "이어보시죠 😏"}' % good, "c3")])
    # 서버 배포 테스트(CPU 1코어·병렬)에선 사전 후보 계산이 6초를 넘겨 코드 수로 빠진 적 있음 (2026-10-05) → 이 테스트만 넉넉히
    orig, wordbot.TIMEOUT = wordbot.TIMEOUT, 120.0
    try:
        word, line = await wordbot.move(svc, CHAT, "차표", used)
    finally:
        wordbot.TIMEOUT = orig
    assert (word, line) == (good, "이어보시죠 😏"), (word, line)
    msgs, kw = svc.llm.seen[-1]
    results = [m["content"] for m in msgs if m["role"] == "tool"]
    assert "이을 흔한 말" in results[0] and results[1].startswith("안 됨"), results
    assert kw["tools"] and kw["purpose"] == "wordchain"


@test
async def ai_player_falls_back_to_code_on_timeout_or_nonsense():
    db, svc, bot, g = await setup()
    g.cancel_timer()

    class Slow(ScriptLLM):
        async def chat(self, messages, **kw):
            await asyncio.sleep(10)
    svc.llm = Slow([])
    orig, wordbot.TIMEOUT = wordbot.TIMEOUT, 0.05
    try:
        word, line = await wordbot.move(svc, CHAT, "차표", {"기차", "차표"})
    finally:
        wordbot.TIMEOUT = orig
    assert word and wordbot.why_not(word, "차표", {"기차", "차표"}) is None and line == ""
    svc.llm = ScriptLLM([Call("play", '{"word": "없는말이다"}')] * 4)
    word, _ = await wordbot.move(svc, CHAT, "차표", {"기차", "차표"})
    assert word and wordbot.why_not(word, "차표", {"기차", "차표"}) is None, "끝까지 틀리면 코드가 둠"


@test
async def hard_level_picks_fewest_follow_ups():
    games.load_words()
    word = wordbot.code_move("차표", {"차표"}, "hard")
    counts = [n for _, n in wordbot.candidates("차표", {"차표"}, hard=True, n=10**6) if n >= 1]
    assert wordbot.common_follow(word) == min(counts) >= 1, (word, min(counts))   # 몰아붙여도 사람이 아는 말 1개는 남김


@test
async def bot_word_leaves_common_follow_ups_and_call_name_is_stripped():
    """실제 2026-10-03 베베: 소담이 이을 흔한 말이 거의 없는 글자로 끝냄 → '릇무꽃·릇다·읏듬' 지어내다 6번 재시작·욕.
    보통 난이도 = 흔한 말 3개 이상 남김 · '소담아 X' 도 게임 답 · 지어낸 말엔 예시 한 번."""
    games.load_words()
    for _ in range(30):
        w = wordbot.code_move("차표", {"차표"}, "normal")
        assert wordbot.common_follow(w) >= 3, (w, wordbot.common_follow(w))
    assert "흔한 말" in (wordbot.why_not("표퓰리즘", "차표", {"차표"}, "normal") or "")
    db, svc, bot, g = await setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    orig, wordbot.move = wordbot.move, plays("표범")
    try:
        ok, m = await text(svc, A, "소담아 차표")
    finally:
        wordbot.move = orig
    assert ok and g.last == "표범" and "차표" in g.used, (g.last, g.used)
    g.cancel_timer()
    ok, m = await text(svc, A, "범릇꽃 소담아")
    assert ok and reactions(bot)[-1] == (m.message_id, "🤔") and "예:" in m.replies[-1], m.replies
    ok, m2 = await text(svc, A, "범가나다")
    assert ok and not m2.replies, "같은 문제엔 예시 한 번만"


@test
async def answer_during_bot_thinking_is_late_not_a_move():
    """사람 A 가 '차표' → 소담이 생각 중에 C 가 '표지' (소담이 차례를 가로챔) → 🙈, 게임은 소담이 수로 진행."""
    db, svc, bot, g = await setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    orig, wordbot.move = wordbot.move, plays("표범")
    try:
        (ok_a, _), (ok_c, mc) = await asyncio.gather(text(svc, A, "차표"), text(svc, C, "표지"))
    finally:
        wordbot.move = orig
    assert ok_a and ok_c and reactions(bot) == [(mc.message_id, "🙈")] and g.last == "표범", (reactions(bot), g.last)
    g.cancel_timer()


# ── 게임 감사에서 재현된 것 ─────────────────────────────
@test
async def dueum_only_standard_syllables():
    """한글 맞춤법 제11·12항만: 레·러·렝·뤼는 바꾸지 않음 ('수레' 다음 '네거리'가 통과하던 것)."""
    assert [games.dueum(c) for c in "력라녀뉴리례뢰르"] == list("역나여유이예뇌느")
    assert [games.dueum(c) for c in "레러렝뤼롸냐"] == list("레러렝뤼롸냐")
    assert games.starts_for("수레") == {"레"}


@test
async def game_not_registered_while_dictionary_loads():
    """재시작 뒤 첫 게임: 사전을 읽는 0.4초 사이 온 말이 반쯤 만든 게임에 닿아 AttributeError."""
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot()
    saved, orig = (games._WORDS, games._COMMON, games._BY_FIRST), games.load_words
    games._WORDS = set()

    def slow_load(lines=None):
        import time
        time.sleep(0.2)
        games._WORDS, games._COMMON, games._BY_FIRST = saved
    games.load_words = slow_load
    try:
        task = asyncio.create_task(svc.games.start(bot, CHAT, A.id, "끝말잇기"))
        await asyncio.sleep(0.05)
        assert not svc.games.is_active(CHAT), "읽는 동안엔 게임 없음"
        assert not await svc.games.on_text(FakeMsg(CHAT, B, "산책"), "산책")
        assert "시작" in await task and svc.games.is_active(CHAT)
    finally:
        games.load_words = orig
        games._WORDS, games._COMMON, games._BY_FIRST = saved
    svc.games.active[CHAT].cancel_timer()


@test
async def send_failure_after_bot_move_keeps_timer():
    db, svc, bot, g = await setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    from telegram.error import RetryAfter

    async def flood(*a, **k):
        raise RetryAfter(30)
    orig_move, wordbot.move = wordbot.move, plays("표범")
    g.say = flood
    try:
        assert (await text(svc, A, "차표"))[0]
    finally:
        wordbot.move = orig_move
    t = g._timer
    assert t and not t.done() and not t.cancelling() and not g.finished, "전송이 실패해도 새 타이머가 살아 있어야 (안 그러면 방이 영원히 게임 중)"
    g.cancel_timer()


@test
async def start_button_racing_join_timer_still_starts():
    """[▶️ 바로 시작]이 버튼 응답을 기다리는 사이 참가 타이머가 울려 시작 안내 중 → 버튼 쪽이 타이머를 끊어 게임이 멈추던 것."""
    db, svc, bot, g = await setup("끝말잇기 차례")
    for u in (A, B):
        await press(svc, u, "wc:j")
    g.cancel_timer()
    announced = []

    async def slow_announce(head=""):
        await asyncio.sleep(0.05)
        announced.append(head)
    g._announce = slow_announce
    q = FakeQuery(CHAT, A, "wc:go")
    q.message.chat_id = CHAT
    real_answer = q.answer

    async def slow_answer(*a, **k):
        await asyncio.sleep(0.02)
        return await real_answer(*a, **k)
    q.answer = slow_answer
    pressing = asyncio.create_task(svc.games.on_callback(q, ["go", g.gid]))
    await asyncio.sleep(0.005)                                        # 버튼은 응답 대기 중
    g._timer = asyncio.create_task(g._start())                        # 그때 참가 타이머가 울림
    await pressing
    await asyncio.sleep(0.1)
    assert announced and not g.joining, announced


@test
async def fired_timer_callback_not_cut_by_cancel():
    """울린 타이머의 콜백(안내 보내는 중)은 그 사이 들어온 답의 cancel_timer 에 끊기지 않는다."""
    db, svc, bot, g = await setup("끝말잇기 차례")
    done = []

    async def slow():
        await asyncio.sleep(0.05)
        done.append(1)
    g.set_timer(0, slow)
    await asyncio.sleep(0.01)
    g.cancel_timer()                                                  # 답 처리 쪽이 타이머를 정리
    await asyncio.sleep(0.1)
    assert done, "울린 뒤 콜백이 끊김"


@test
async def elimination_notice_not_cut_by_answer():
    db, svc, bot, g = await setup("끝말잇기 차례")
    for u in (A, B, C):
        await press(svc, u, "wc:j")
    await press(svc, A, "wc:go")
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    real_say = g.say

    async def slow_say(text, **kw):
        await asyncio.sleep(0.05)
        return await real_say(text, **kw)
    g.say = slow_say
    g.set_timer(0, g._turn_timeout)                                 # 차례 시간 끝 → 탈락 안내 보내는 중
    await asyncio.sleep(0.01)
    users = {u.id: u for u in (A, B, C)}
    await text(svc, users[g.players[0][0]], "차표")                  # 다음 사람이 그 사이 답함
    await asyncio.sleep(0.2)
    assert any("탈락" in t for t in said(bot)), said(bot)
    g.cancel_timer()
