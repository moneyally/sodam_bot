"""게임 연출(룰렛·사다리·바카라·블랙잭·하이로우·경마): python tests/run_all.py game_visual2

본코드 명령을 그대로 돌리고, 가짜 봇이 받은 보내기·수정·대기(sleep)를 순서대로 기록해서 검사한다.
연출 대기는 basic.sleep 을 기록만 하는 함수로 바꿔 끼워 진짜로 기다리지 않는다 (DICE_WAIT 는 실제 값 그대로 → 간격 검사).
"""
import asyncio
import random
import re
import sys

from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from harness import html_errors
from telegram.error import BadRequest, RetryAfter

from sodam import casino
from sodam.casino import basic, core, multi
from sodam.casino import cards as C

test, run_all = runner()
CHAT = -1007700
START = 10_000
SUIT = {"♠": 0, "♥": 1, "♦": 2, "♣": 3}
RANK = {"A": 1, "J": 11, "Q": 12, "K": 13}
MAX_OPS = 5            # 한 판(버튼 한 번) 보내기+수정 상한


class Env:
    def __init__(self, svc, bot, user):
        self.svc, self.bot, self.user = svc, bot, user
        self.ops: list[tuple] = []          # ("send"|"edit"|"sleep"|"answer", 값)


class Bot(FakeBot):
    def __init__(self, env_ref):
        super().__init__()
        self.env_ref = env_ref
        self.fail = None                    # fn(text) → 던질 예외 또는 None

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        if self.fail and (err := self.fail(text)):
            self.env_ref[0].ops.append(("edit_fail", text))
            raise err
        self.env_ref[0].ops.append(("edit", text))

    async def send_message(self, chat_id, text, **kw):
        self.env_ref[0].ops.append(("send", text))
        return await super().send_message(chat_id, text, **kw)


class Msg(FakeMsg):
    def __init__(self, env, *a, **kw):
        super().__init__(*a, **kw)
        self.env, self.kbs = env, []

    async def reply_text(self, text, **kw):
        self.env.ops.append(("send", text))
        self.kbs.append(kw.get("reply_markup"))
        return await super().reply_text(text, **kw)


class Query(FakeQuery):
    def __init__(self, env, *a, fail=None, **kw):
        super().__init__(*a, **kw)
        self.env, self.fail = env, fail

    async def answer(self, text=None, show_alert=False):
        self.env.ops.append(("answer", text))
        await super().answer(text, show_alert)

    async def edit_message_text(self, text, **kw):
        if self.fail and (err := self.fail(text)):
            self.env.ops.append(("edit_fail", text))
            raise err
        self.env.ops.append(("edit", text))
        await super().edit_message_text(text, **kw)


async def setup(name="사장"):
    db = await make_db()
    svc = await make_svc(db)
    ref = [None]
    bot = Bot(ref)
    u = fake_user(4242, name)
    env = Env(svc, bot, u)
    ref[0] = env
    await db.upsert_user(u)
    await db._write("INSERT INTO casino_accounts(chat_id, user_id, joined_at) VALUES(?,?,0)", (CHAT, u.id))
    await core.credit(db, CHAT, u.id, START, "join")
    C.HANDS.clear()
    core._last_bet.clear()

    async def rec_sleep(d):
        env.ops.append(("sleep", d))
    basic.sleep = rec_sleep
    basic.DICE_WAIT = 3.5
    return env


def restore():
    basic.sleep, basic.rng, basic.DICE_WAIT = asyncio.sleep, core.rng, 3.5
    unfix()


async def say(env, text):
    core._last_bet.clear()
    msg = Msg(env, CHAT, env.user, text)
    assert await casino.dispatch(env.svc, env.bot, msg, CHAT, env.user, None, text)
    return msg


def card(s):
    return C.Card(RANK.get(s[1:]) or int(s[1:]), SUIT[s[0]])


class FixedShoe(C.Shoe):
    def __init__(self, decks, order):
        super().__init__(decks)
        self.order = [card(x) for x in order]

    def draw(self):
        c = self.order.pop(0)
        self.cards.remove(c)
        return c


def fix(*names):
    C.make_shoe = lambda decks: FixedShoe(decks, names)


def unfix():
    C.make_shoe = lambda decks: C.Shoe(decks)


def btn(kb, part):
    return next(b.callback_data for row in kb.inline_keyboard for b in row if part in b.text)


async def press(env, data, fail=None):
    q = Query(env, CHAT, env.user, data, fail=fail)
    await casino.on_callback(env.svc, env.bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers
    return q


async def bal(env):
    return await core.balance(env.svc.db, CHAT, env.user.id)


async def ledger_ok(env):
    s = (await env.svc.db._one("SELECT COALESCE(SUM(delta),0) AS s FROM casino_ledger WHERE chat_id=? AND user_id=?",
                               (CHAT, env.user.id)))["s"]
    assert s == await bal(env)


def screens(env):
    """보내기·수정 텍스트만 (순서대로)."""
    return [(k, t) for k, t in env.ops if k in ("send", "edit")]


def check_timeline(env, n_edits):
    """보내기 1 + 수정 n_edits, 수정마다 앞에 ≥1초 대기, HTML 유효."""
    sc = screens(env)
    assert [k for k, _ in sc] == ["send"] + ["edit"] * n_edits, [k for k, _ in sc]
    assert len(sc) <= MAX_OPS
    kinds = [k for k, _ in env.ops if k in ("send", "edit", "sleep")]
    for i, k in enumerate(kinds):
        if k == "edit":
            assert kinds[i - 1] == "sleep", kinds
    assert all(d >= 1.0 for k, d in env.ops if k == "sleep"), env.ops
    for _, t in sc:
        assert not html_errors(t), (html_errors(t), t)
    return [t for _, t in sc]


def dump(title, env):
    print(f"===== {title} =====")
    for k, t in screens(env):
        print(f"[{'보내기' if k == 'send' else '수정'}]\n{t}\n")


def center(text):
    return re.search(r"【([^】]+)】", text).group(1)


# ── 🎡 룰렛 ───────────────────────────────────────────────
@test
async def roulette_wheel_spins_and_stops_on_result():
    env = await setup()
    basic.rng = lambda n: 16                               # 🔴16
    await say(env, "!룰렛 1000 빨강")
    shots = check_timeline(env, 3)
    assert [center(t) for t in shots].count("🔴16") == 1 and center(shots[-1]) == "🔴16"   # 멈출 때만 결과 칸
    assert len({center(t) for t in shots}) == 4                                            # 띠가 흘러감
    assert all("💨" in t and "잔액" not in t for t in shots[:-1])                           # 도는 중엔 결과 없음
    final = shots[-1]
    assert "멈춤" in final and "빨강 · 짝 · 로우" in final and "적중! ×2" in final and "+1,000P" in final
    assert "(빨강 선택)" in shots[0]
    assert await bal(env) == START + 1000
    await ledger_ok(env)

    env.ops.clear()
    basic.rng = lambda n: 0                                # 🟢0 → 빨강 꽝
    await say(env, "!룰렛 1000 빨강")
    final = check_timeline(env, 3)[-1]
    assert center(final) == "🟢0" and "초록" in final and "빗나감" in final and "-1,000P" in final
    assert await bal(env) == START
    await ledger_ok(env)
    restore()


@test
def roulette_strip_follows_real_wheel():
    s = re.sub(r"<[^>]+>", "", basic.wheel_strip(basic.WHEEL.index(0)))
    assert s == "🔴3 ⚫26 【🟢0】 🔴32 ⚫15", s
    assert sorted(basic.WHEEL) == list(range(37))
    assert all((basic.pocket(n)[0] == "🔴") == (n in basic.REDS) for n in range(1, 37))


# ── 🪜 사다리 ─────────────────────────────────────────────
def pole_ends(art):
    """<pre> 그림 → (가로줄 수, 마지막 기둥 줄에서 굵은 쪽 '홀'|'짝')."""
    rows = art.split("\n")
    rungs = sum("━" in r for r in rows)
    last = [r for r in rows if r.startswith(("┃", "│"))][-1]
    return rungs, "홀" if last[0] == "┃" else "짝"


@test
async def ladder_path_goes_down_to_the_result():
    for i, (start, lines, end) in enumerate(basic.LADDER):
        env = await setup()
        basic.rng = lambda n, i=i: i
        await say(env, f"!사다리 1000 {start}{lines}{end}")
        shots = check_timeline(env, 2)
        arts = [re.search(r"<pre>(.*?)</pre>", t, re.S).group(1) for t in shots]
        assert "━" not in arts[0] and "░" in arts[0]                     # 처음엔 가림 (출발만)
        assert sum("━" in r for r in arts[1].split("\n")) == 2 and "░" in arts[1]   # 절반 내려감
        assert pole_ends(arts[-1]) == (lines, end), (start, lines, end, arts[-1])   # 그림이 결과대로 도착
        first_pole = [r for r in arts[-1].split("\n") if r.startswith(("┃", "│"))][0]
        assert (first_pole[0] == "┃") == (start == "좌")                   # 출발 쪽이 굵은 선
        assert "도착" in shots[0] and "❔" in shots[0] and "잔액" not in shots[1]
        assert f"도착 <b>{end}</b>" in shots[-1] and "+2,800P" in shots[-1]
        assert await bal(env) == START + 2800
        await ledger_ok(env)
    # 가려진 그림 높이는 3줄·4줄이 같음 (줄 수가 미리 안 샘)
    h = {lines: len(basic.ladder_art("좌", lines, 0).split("\n")) for lines in (3, 4)}
    h1 = {lines: len(basic.ladder_art("좌", lines, 2).split("\n")) for lines in (3, 4)}
    assert h[3] == h[4] and h1[3] == h1[4], (h, h1)
    restore()


# ── 🃏 바카라 ─────────────────────────────────────────────
@test
async def baccarat_flips_cards_step_by_step():
    env = await setup()
    fix("♠4", "♥K", "♦4", "♣5")                             # 플 8 내추럴 → 3번째 카드 없음
    await say(env, "!바카라 1000 플")
    shots = check_timeline(env, 2)
    assert "「" not in shots[0] and shots[0].count(C.HIDDEN) == 4               # 전부 덮음
    assert "「4♠️」「4♦️」" in shots[1] and "「K♥️」" not in shots[1] and "<code>████████░</code> <b>8</b>" in shots[1]
    final = shots[-1]
    assert "「K♥️」「5♣️」" in final and "플레이어 승" in final and "내추럴" in final and "+1,000P" in final
    assert await bal(env) == START + 1000

    env.ops.clear()
    fix("♠2", "♥3", "♦3", "♣K", "♠8")                       # 플 5 → 3번째(8) · 뱅 3 멈춤 → 3:3 타이
    await say(env, "!바카라 1000 뱅")
    shots = check_timeline(env, 3)
    assert "「3♥️」「K♣️」" in shots[2] and "「8♠️」" not in shots[2] and "3번째 카드" in shots[2]
    assert "「2♠️」「3♦️」「8♠️」" in shots[-1] and "타이" in shots[-1] and "본전" in shots[-1]
    rd = C.BacRound([card("♠2"), card("♦3"), card("♠8")], [card("♥3"), card("♣K")])
    assert C.bac_payout(rd, "banker", 1000) == 1000 and await bal(env) == START + 1000   # 정산 불변
    await ledger_ok(env)
    restore()


@test
def card_faces_are_boxed_and_red_suits_are_red_emoji():
    assert C.face(card("♠A")) == "「A♠️」" and C.face(card("♥10")) == "「10♥️」" and C.face(card("♦Q")) == "「Q♦️」"
    assert C.cards_str([card("♣2"), card("♥K")]) == "「2♣️」「K♥️」"
    assert C.bar(0) == "░" * 9 and C.bar(9) == "█" * 9


# ── 🃏 블랙잭 ─────────────────────────────────────────────
@test
async def blackjack_dealer_reveals_one_card_at_a_time():
    env = await setup()
    fix("♠10", "♥2", "♦8", "♣Q", "♠3", "♥4")               # 나 18 · 딜러 12 → +3 → +4 = 19 (딜러 승)
    msg = await say(env, "!블랙잭 1000")
    assert "「2♥️」" in msg.replies[-1] and C.HIDDEN in msg.replies[-1] and "「Q♣️」" not in msg.replies[-1]
    env.ops.clear()
    bals = []
    orig = Query.edit_message_text

    async def spy(self, text, **kw):                       # 수정될 때마다 잔액 기록 → 정산이 연출보다 먼저
        bals.append(await bal(env))
        await orig(self, text, **kw)
    Query.edit_message_text = spy
    try:
        q = await press(env, btn(msg.kbs[-1], "스탠드"))
    finally:
        Query.edit_message_text = orig
    kinds = [k for k, _ in env.ops]
    assert kinds[0] == "answer" and kinds.count("edit") == 3, kinds          # 버튼 응답 먼저, 수정 3회
    assert all(d >= 1.0 for k, d in env.ops if k == "sleep")
    e = q.edits
    assert "「2♥️」「Q♣️」 (<b>12</b>)" in e[0] and "공개 중" in e[0] and "━━" not in e[0]
    assert "「2♥️」「Q♣️」「3♠️」 (<b>15</b>)" in e[1] and "「4♥️」" not in e[1] and "━━" not in e[1]
    assert "「3♠️」「4♥️」 (<b>19</b>)" in e[2] and "딜러 승" in e[2] and "-1,000P" in e[2]
    assert bals == [START - 1000] * 3 and not any(html_errors(t) for t in e)
    await ledger_ok(env)

    env.ops.clear()                                          # 딜러가 안 받는 판: 뒤집기 → 결과 (2회)
    fix("♠10", "♥10", "♦9", "♣7")
    msg = await say(env, "!블랙잭 1000")
    q = await press(env, btn(msg.kbs[-1], "스탠드"))
    assert len(q.edits) == 2 and "공개 중" in q.edits[0] and "승리" in q.edits[1]
    assert await bal(env) == START

    env.ops.clear()                                          # 버스트: 딜러 공개 연출 없이 바로 결과 (1회)
    fix("♠10", "♥9", "♦6", "♣7", "♠K")
    msg = await say(env, "!블랙잭 1000")
    q = await press(env, btn(msg.kbs[-1], "히트"))
    assert len(q.edits) == 1 and "버스트" in q.edits[0] and not [k for k, _ in env.ops if k == "sleep"]
    assert await bal(env) == START - 1000
    await ledger_ok(env)
    restore()


@test
async def blackjack_reveal_is_capped_and_final_survives_429():
    assert C.bj_reveal_steps(2) == [2] and C.bj_reveal_steps(3) == [2] and C.bj_reveal_steps(4) == [2, 3]
    assert C.bj_reveal_steps(6) == [2, 5]                 # 딜러가 4장 받아도 중간 화면 2개
    env = await setup()
    fix("♠10", "♥2", "♦8", "♣2", "♠2", "♥2", "♦2", "♣A")   # 딜러 2,2 → +2 +2 +A = 소프트 19 (5장) → 나 18 짐
    msg = await say(env, "!블랙잭 1000")
    hits = {"n": 0}

    def fail(text):                                        # 결과 화면이 처음엔 429
        if "━━" in text and not hits["n"]:
            hits["n"] = 1
            return RetryAfter(4)
        return None
    q = await press(env, btn(msg.kbs[-1], "스탠드"), fail=fail)
    assert len(q.edits) == 3 and "딜러 승" in q.edits[-1], q.edits
    assert ("sleep", 4.0) in env.ops and hits["n"] == 1
    assert await bal(env) == START - 1000
    await ledger_ok(env)
    restore()


# ── 🔼🔽 하이로우 ──────────────────────────────────────────
@test
async def hilo_shows_card_faces_and_step_bar():
    env = await setup()
    fix("♠7", "♥K", "♦2")
    msg = await say(env, "!하이로우 1000")
    first = msg.replies[-1]
    assert "「7♠️」" in first and "⬜" * C.HL_MAX_STEPS in first
    q = await press(env, btn(msg.kbs[-1], "하이"))
    t = q.edits[-1]
    assert "「7♠️」 → <b>「K♥️」</b>" in t and "지난 카드: 「7♠️」" in t and "🟩" + "⬜" * 9 in t and not html_errors(t)
    restore()


# ── 공통: 수정 실패해도 정산·결과 ───────────────────────────
@test
async def failed_edits_never_change_money_and_result_still_shows():
    env = await setup()
    basic.rng = lambda n: 7                                # 🔴7 → 숫자 7 적중 ×36
    env.bot.fail = lambda text: BadRequest("Message to edit not found")   # 연출 수정 전부 실패
    await say(env, "!룰렛 1000 7")
    kinds = [k for k, _ in env.ops if k != "sleep"]
    assert kinds == ["send", "edit_fail", "edit_fail", "edit_fail", "send"], kinds   # 결과는 새 메시지로
    assert "+35,000P" in env.ops[-1][1] and await bal(env) == START + 35_000

    env.ops.clear()
    n = {"i": 0}

    def flaky(text):                                       # 중간 화면 429 는 건너뛰고, 결과 화면은 한 번 더
        n["i"] += 1
        return RetryAfter(2) if n["i"] in (1, 2) else None
    env.bot.fail = flaky
    basic.rng = lambda n: 2
    await say(env, "!사다리 1000 우")                      # 우3홀 → 적중
    kinds = [k for k, _ in env.ops if k != "sleep"]
    assert kinds == ["send", "edit_fail", "edit_fail", "edit"], kinds
    assert ("sleep", 2.0) in env.ops
    assert "도착 <b>홀</b>" in env.ops[-1][1] and await bal(env) == START + 35_000 + 950
    await ledger_ok(env)
    restore()


@test
async def names_are_escaped_in_every_frame():
    env = await setup(name="<b>&사장")
    basic.rng = lambda n: 1
    await say(env, "!룰렛 1000 검정")
    await say(env, "!사다리 1000 좌")
    fix("♠4", "♥K", "♦4", "♣5")
    await say(env, "!바카라 1000 플")
    for _, t in screens(env):
        assert "&lt;b&gt;&amp;사장" in t and not html_errors(t), t
    restore()


# ── 🏇 경마 ──────────────────────────────────────────────
@test
async def horse_track_ranks_leader_and_finish():
    import test_casino_multi as TM
    env = await TM.setup()
    for u, h in zip(env.users[:3], (3, 3, 1)):
        await TM.say(env, u, f"!경마 1000 {h}")
    r = multi.current(TM.CHAT, "horse")
    r.winner = 2
    r.frames = multi.race_frames(2, random.Random(5).randrange)
    await TM.finish(env, "horse")
    assert len(env.bot.edits) == multi.FRAMES and env.clock.sleeps[-multi.FRAMES:] == [multi.TICK] * multi.FRAMES
    start = [c[2] for c in env.bot.named("send_message")][0]
    shots = [start] + [t for _, t in env.bot.edits]
    for t, pos in zip(shots, [[0] * 5] + r.frames):
        assert not html_errors(t), t
        rows = re.search(r"<pre>(.*?)</pre>", t, re.S).group(1).split("\n")
        assert len(rows) == 5
        for h, row in enumerate(rows):
            assert row.startswith(f"{h + 1} " + "·" * pos[h] + "🏇" + "·" * (multi.TRACK - pos[h]) + "🏁")
        assert "👥2" in rows[2] and "👥1" in rows[0] and "👥" not in rows[1]
        if max(pos):
            top = max(pos)
            assert all(("🥇" in row) == (pos[h] == top) for h, row in enumerate(rows)) or t is shots[-1]
    mid = shots[3]
    assert "선두" in mid and "결승까지" in mid
    last = shots[-1]
    rows = re.search(r"<pre>(.*?)</pre>", last, re.S).group(1).split("\n")
    assert "3번 우승" in last and "🏆" in rows[2] and sum("🏆" in x for x in rows) == 1 and "💨" not in last
    near = [x for x, p in zip(re.search(r"<pre>(.*?)</pre>", shots[-2], re.S).group(1).split("\n"), r.frames[-2])
            if 0 < multi.TRACK - p <= 2]
    assert all("💨" in x for x in near)
    TM.restore()


@test
def track_text_marks():
    t = multi.track_text([12, 11, 11, 5, 0], "🏁", [0, 1, 0, 0, 0], final=True)
    rows = re.search(r"<pre>(.*?)</pre>", t, re.S).group(1).split("\n")
    assert rows[0].endswith("🏁🏆") and rows[1].endswith("🥈 👥1") and rows[2].endswith("🥈") and rows[3].endswith("🥉")
    t = multi.track_text([10, 7, 3, 3, 1], "🏇")
    assert "선두 <b>1번</b> · 결승까지 2칸 🔥" in t and "🥇💨" in t


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
