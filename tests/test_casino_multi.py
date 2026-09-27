"""같이 하는 게임(그래프·경마): python tests/run_all.py casino_multi

진짜로 기다리지 않는다: 가짜 시계(FakeClock)가 sleep 을 받으면 시간을 건너뛰고,
그 사이 예약된 행동(스톱·늦은 참가)을 그 시각에 실행한다.
"""
import asyncio
import random
import sys
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner
from harness import html_errors
from telegram.error import BadRequest, RetryAfter

from sodam import casino
from sodam.casino import core, multi
from sodam.permissions import Role

test, run_all = runner()
CHAT = -1009001


class FakeClock:
    """테스트가 go.set() 하기 전까지 판은 베팅 시간에서 멈춰 있다."""

    def __init__(self):
        self.t = 0.0
        self.events: list[tuple[float, object]] = []
        self.go = asyncio.Event()
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def at(self, t, fn):
        self.events.append((t, fn))

    async def sleep(self, d):
        await self.go.wait()
        self.sleeps.append(d)
        target = self.t + d
        while True:
            due = sorted((e for e in self.events if e[0] <= target), key=lambda e: e[0])
            if not due:
                break
            e = due[0]
            self.events.remove(e)
            self.t = max(self.t, e[0])
            await e[1]()
        self.t = target
        await asyncio.sleep(0)


class Bot(FakeBot):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.edits: list[tuple[float, str]] = []
        self.fail = None        # 다음 수정에서 던질 예외 (함수: 호출마다)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        if self.fail:
            err = self.fail(len(self.edits))
            if err:
                self.edits.append((self.clock.t, "<fail>"))
                raise err
        self.edits.append((self.clock.t, text))


async def setup(crash=None, n_users=6):
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "게임방")
    clock = FakeClock()
    multi._clock, multi._sleep, multi._crash_override = clock, clock.sleep, crash
    multi._rand = core.rng
    for r in multi._ROUNDS.values():      # 앞 테스트가 실패해 남긴 판
        r.task.cancel()
    multi._ROUNDS.clear()
    bot = Bot(clock)
    users = [fake_user(100 + i, f"유저{i}") for i in range(n_users)]
    for u in users:
        await add_member(db, CHAT, u)
    env = SimpleNamespace(db=db, svc=svc, bot=bot, clock=clock, users=users)
    for u in users:
        await say(env, u, "!가입")
    return env


async def say(env, u, text):
    core._last_bet.clear()
    m = FakeMsg(CHAT, u, text)
    await casino.dispatch(env.svc, env.bot, m, CHAT, u, Role.MEMBER, text)
    return m.replies


async def bal(env, u):
    return await core.balance(env.db, CHAT, u.id)


async def ledger(env, u):
    return await env.db._all("SELECT delta, reason FROM casino_ledger WHERE chat_id=? AND user_id=? ORDER BY id",
                             (CHAT, u.id))


async def ledger_consistent(env):
    for u in env.users:
        s = sum(r["delta"] for r in await ledger(env, u))
        assert s == await bal(env, u), (u.id, s, await bal(env, u))
    left = await env.db._all("SELECT * FROM casino_open_stakes")
    assert not left, [dict(r) for r in left]
    left = await env.db._all("SELECT * FROM casino_open")          # 끝난 판은 열린 베팅이 남지 않음
    assert not left, [dict(r) for r in left]


async def finish(env, game):
    r = multi.current(CHAT, game)
    env.clock.go.set()
    await asyncio.wait_for(r.task, 5)
    return r


def board(env):
    return [c[2] for c in env.bot.named("send_message") if c[1] == CHAT]


def restore():
    multi._clock, multi._sleep, multi._rand, multi._crash_override = \
        __import__("time").monotonic, asyncio.sleep, core.rng, None


# ── 📈 그래프 ─────────────────────────────────────────────
@test
async def crash_full_round():
    env = await setup(crash=300)                       # 3.00x 에서 터짐
    a, b, c, late, nobet = env.users[:5]
    START = core.START_POINTS
    r1 = await say(env, a, "!그래프 1000")
    assert "새 판" in r1[0] and "15초" in r1[0]
    r2 = await say(env, b, "!그래프 2000 2")           # 자동 2.00x
    assert r2[0].startswith("🎫") and "자동 2.00x" in r2[0] and "2명" in r2[0]
    await say(env, c, "!그래프 1000")
    assert "이미" in (await say(env, a, "!그래프 1000"))[0]            # 한 판에 한 번
    assert "출발 전" in (await say(env, a, "!스톱"))[0]
    assert await bal(env, a) == START - 1000

    out = {}
    t_stop = 15 + multi.time_to(150) + 0.05             # 1.50x 조금 넘어서
    async def stop_twice():
        out["a"] = await asyncio.gather(say(env, a, "!스톱"), say(env, a, "!스톱"))
    env.clock.at(t_stop, stop_twice)
    async def late_join():
        out["late"] = await say(env, late, "!그래프 1000")
        out["nobet"] = await say(env, nobet, "!멈춰")
    env.clock.at(20, late_join)
    r = await finish(env, "crash")

    replies = [x for rs in out["a"] for x in rs]
    assert len([x for x in replies if "내림" in x]) == 1, replies        # 두 번 쳐도 한 번
    a_cash = r.players[a.id].cash_at
    assert 150 <= a_cash < 155, a_cash
    assert await bal(env, a) == START - 1000 + 1000 * a_cash // 100
    assert await bal(env, b) == START - 2000 + 4000                      # 자동 2.00x
    assert await bal(env, c) == START - 1000                             # 같이 터짐
    assert "출발" in out["late"][0] and await bal(env, late) == START    # 늦은 참가 거절, 차감 없음
    assert "안 타셨어요" in out["nobet"][0]
    wins = [x for x in await ledger(env, a) if x["reason"] == "win:crash"]
    assert len(wins) == 1
    await ledger_consistent(env)

    # 메시지 수정: 1.5초 간격 이상, 배수는 오르기만, 마지막은 터짐
    times = [t for t, _ in env.bot.edits]
    assert all(t2 - t1 >= multi.TICK - 0.02 for t1, t2 in zip(times, times[1:])), times
    assert "💥" in env.bot.edits[-1][1] and "3.00x" in env.bot.edits[-1][1]
    final = board(env)[-1]
    assert "3.00x" in final and "유저0" in final and "(자동)" in final and "같이 터짐 1명" in final
    assert not html_errors(final)
    assert multi.current(CHAT, "crash") is None
    assert r.task.done() and not multi._TASKS
    assert "한 발 늦었어요" in (await say(env, c, "!스톱"))[0]           # 터진 직후 !스톱
    assert "없어요" in (await say(env, a, "!스톱"))[0]                    # 내린 사람은 일반 안내
    restore()


@test
async def two_users_stop_same_moment():
    env = await setup(crash=500)
    a, b = env.users[:2]
    await say(env, a, "!그래프 1000")
    await say(env, b, "!그래프 1000")
    out = {}
    async def both():
        out["r"] = await asyncio.gather(say(env, a, "!스톱"), say(env, b, "!stop"), say(env, a, "!멈춰"))
    env.clock.at(15 + multi.time_to(200), both)
    r = await finish(env, "crash")
    ca, cb = r.players[a.id].cash_at, r.players[b.id].cash_at
    assert ca == cb and 199 <= ca <= 201, (ca, cb)
    assert await bal(env, a) == await bal(env, b) == core.START_POINTS - 1000 + 10 * ca
    await ledger_consistent(env)
    restore()


@test
async def crash_instant_bust_and_auto_above_crash():
    env = await setup(crash=100)                       # 출발하자마자 1.00x 펑
    a, b = env.users[:2]
    await say(env, a, "!그래프 1000 1.5")
    await say(env, b, "!그래프 1000")
    await finish(env, "crash")
    assert await bal(env, a) == await bal(env, b) == core.START_POINTS - 1000
    assert "1.00x" in board(env)[-1]
    await ledger_consistent(env)
    restore()


@test
async def crash_everyone_out_ends_early():
    env = await setup(crash=5000)
    a = env.users[0]
    await say(env, a, "!그래프 1000 1.2")
    r = await finish(env, "crash")
    assert r.players[a.id].cash_at == 120 and await bal(env, a) == core.START_POINTS + 200
    assert env.clock.t < 15 + multi.time_to(200)      # 50x 까지 안 기다림
    restore()


@test
async def crash_bad_args():
    env = await setup(crash=300)
    a = env.users[0]
    assert "1.01" in (await say(env, a, "!그래프 1000 0.5"))[0]
    assert "1.01" in (await say(env, a, "!그래프 1000 nan"))[0]
    assert "탑승" in (await say(env, a, "!그래프"))[0]
    assert "최소" in (await say(env, a, "!그래프 10"))[0]
    assert "모자라" in (await say(env, a, "!그래프 99999"))[0]
    assert multi.current(CHAT, "crash") is None and await bal(env, a) == core.START_POINTS
    assert "없어요" in (await say(env, a, "!스톱"))[0]
    restore()


@test
async def crash_refund_on_exception():
    env = await setup(crash=900)
    a, b, c = env.users[:3]
    await say(env, a, "!그래프 1000")
    await say(env, b, "!그래프 3000 1.3")
    await say(env, c, "!그래프 500")
    env.bot.fail = lambda i: RuntimeError("boom") if i >= 3 else None   # 텔레그램 오류 아닌 버그
    await finish(env, "crash")
    assert await bal(env, a) == await bal(env, c) == core.START_POINTS   # 전액 환불
    assert await bal(env, b) == core.START_POINTS - 3000 + 3900          # 이미 내린 사람은 그대로
    refunds = [x for x in await ledger(env, a) if x["reason"] == "refund:crash"]
    assert len(refunds) == 1
    assert "돌려드렸어요" in board(env)[-1]
    await ledger_consistent(env)
    restore()


@test
async def crash_cancel_refunds():
    env = await setup(crash=300)
    a, b = env.users[:2]
    await say(env, a, "!그래프 1000")
    await say(env, b, "!경마 2000 4")
    assert await multi.abandon_all() == 2
    assert await bal(env, a) == await bal(env, b) == core.START_POINTS
    assert not multi._ROUNDS
    await ledger_consistent(env)
    restore()


@test
async def telegram_edit_failures_are_skipped():
    env = await setup(crash=400)
    a = env.users[0]
    await say(env, a, "!그래프 1000")
    errs = {1: BadRequest("Message is not modified: specified new message content..."),
            2: RetryAfter(3), 5: BadRequest("message to edit not found")}
    env.bot.fail = lambda i: errs.get(i)
    await finish(env, "crash")
    assert await bal(env, a) == core.START_POINTS - 1000
    times = [t for t, x in env.bot.edits]
    retry_at = times[2]
    assert not any(retry_at < t < retry_at + 3 for t in times), times   # RetryAfter 동안 쉼
    assert "4.00x" in board(env)[-1]
    await ledger_consistent(env)
    restore()


@test
async def concurrent_joins_and_escaping():
    env = await setup(crash=250)
    a = env.users[0]
    evil = fake_user(777, "<b>관리자</b>&")
    await add_member(env.db, CHAT, evil)
    env.users.append(evil)
    await say(env, evil, "!가입")
    rs = await asyncio.gather(say(env, a, "!그래프 1000"), say(env, a, "!그래프 1000"))
    assert await bal(env, a) == core.START_POINTS - 1000, rs          # 동시에 두 번 → 한 번만
    await say(env, evil, "!그래프 1000")
    st = (await say(env, a, "!라운드"))[0]
    assert "탑승 받는 중" in st and "2명" in st
    await finish(env, "crash")
    for text in board(env) + [x for _, x in env.bot.edits]:
        assert "<b>관리자</b>" not in text and not html_errors(text), text
    assert "&lt;b&gt;관리자&lt;/b&gt;&amp;" in board(env)[-1]
    await ledger_consistent(env)
    restore()


@test
async def stale_stakes_refunded_after_restart():
    """kill -9·컨테이너 회수로 판이 정산 없이 사라짐 → 다음 시작 때 casino.startup 이 환불 (한 번만)."""
    env = await setup(crash=300)
    a, b = env.users[:2]
    await say(env, a, "!그래프 1500")
    await say(env, b, "!그래프 2000")
    r = multi.current(CHAT, "crash")
    assert await bal(env, a) == core.START_POINTS - 1500
    multi._ROUNDS.clear()                                  # 강제 종료 흉내: 판이 정산·환불 없이 사라짐
    r.players.clear()
    r.task.cancel()
    assert await casino.startup(env.svc) == 2
    assert await casino.startup(env.svc) == 0
    assert await bal(env, a) == core.START_POINTS and await bal(env, b) == core.START_POINTS
    await ledger_consistent(env)
    restore()


@test
async def legacy_open_stakes_refunded_once():
    """옛 버전이 casino_open_stakes 에 남긴 걸린 돈 (casino_open 은 없음) → 시작 때 한 번 환불."""
    env = await setup(crash=300)
    a = env.users[0]
    await env.db._write("UPDATE members SET points=points-1500 WHERE chat_id=? AND user_id=?", (CHAT, a.id))
    await env.db._write("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                        (CHAT, a.id, -1500, "bet:crash", 0))
    await env.db._write("INSERT INTO casino_open_stakes VALUES(?,?,?,?,?)", (CHAT, a.id, "crash", 1500, 0))
    assert await multi.recover_stale(env.db) == 1
    assert await multi.recover_stale(env.db) == 0
    assert await bal(env, a) == core.START_POINTS
    await ledger_consistent(env)
    restore()


# ── 🏇 경마 ──────────────────────────────────────────────
@test
async def horse_race_winners_and_losers():
    env = await setup()
    a, b, c = env.users[:3]
    assert "출발 20초 전" in (await say(env, a, "!경마 1000 3"))[0]
    assert "→ 2번" in (await say(env, b, "!경마 2번 2000"))[0]
    await say(env, c, "!경마 5천 3")
    assert "이미" in (await say(env, a, "!경마 1000 1"))[0]
    assert "1~5" in (await say(env, a, "!경마 1000 7"))[0]
    r = multi.current(CHAT, "horse")
    r.winner = 2                                          # 3번 우승
    r.frames = multi.race_frames(2, random.Random(5).randrange)
    st = (await say(env, a, "!라운드"))[0]
    assert "2번 1명" in st and "3번 2명" in st
    out = {}
    async def late():
        out["late"] = await say(env, env.users[3], "!경마 1000 1")
    env.clock.at(22, late)
    await finish(env, "horse")
    assert "출발" in out["late"][0]
    assert await bal(env, a) == core.START_POINTS - 1000 + 4700
    assert await bal(env, b) == core.START_POINTS - 2000
    assert await bal(env, c) == core.START_POINTS - 5000 + 23500
    gifs = env.bot.named("send_animation")                          # 결과 애니메이션 1개 + 캡션 1번 (글자 수정 없음)
    assert len(gifs) == 1 and gifs[0][2][:6] == b"GIF89a" and not env.bot.edits
    last = env.bot.named("edit_caption")[-1][3]
    assert "3번 우승" in last and "3 " + "·" * multi.TRACK + "🏇🏁" in last
    assert sum("🏇🏁" in line for line in last.splitlines()) == 1
    res = board(env)[-1]
    assert "적중 2명" in res and "꽝 1명" in res and not html_errors(res)
    for u in (a, c):
        assert len([x for x in await ledger(env, u) if x["reason"] == "win:horse"]) == 1
    await ledger_consistent(env)
    restore()


@test
async def horse_frames_are_sane():
    rnd = random.Random(1)
    for _ in range(300):
        w = rnd.randrange(5)
        fr = multi.race_frames(w, rnd.randrange)
        assert len(fr) == multi.FRAMES
        for h in range(5):
            path = [f[h] for f in fr]
            assert path == sorted(path)
        assert fr[-1][w] == multi.TRACK and all(p < multi.TRACK for h, p in enumerate(fr[-1]) if h != w)


# ── 기대 환급률 ───────────────────────────────────────────
@test
def rtp_simulation():
    rnd = random.Random(20260926)
    N = 40_000
    bet_in = bet_out = 0
    for i in range(N):
        crash = multi.crash_point(rnd.randrange)
        target = rnd.choice([101, 110, 150, 200, 300, 500, 1000])
        bet_in += 1000
        bet_out += multi.crash_payout(1000, target) if target <= crash else 0
    crash_rtp = bet_out / bet_in
    h_in = h_out = 0
    for i in range(N):
        win = rnd.randrange(5)
        pick = rnd.randrange(5)
        h_in += 1000
        h_out += 1000 * multi.HORSE_PAY_X10 // 10 if pick == win else 0
    horse_rtp = h_out / h_in
    print(f"  RTP crash={crash_rtp:.4f} horse={horse_rtp:.4f} (N={N} each)")
    assert 0.90 <= crash_rtp <= 1.00 and 0.90 <= horse_rtp <= 1.00
    # 분포 확인: 1.00x 즉시 터짐 약 3%, 100x 상한
    pts = [multi.crash_point(rnd.randrange) for _ in range(N)]
    assert 0.02 < sum(p == 100 for p in pts) / N < 0.04 and max(pts) <= multi.CRASH_CAP


@test
def mult_curve():
    assert multi.mult_at(0) == 100
    assert 195 <= multi.mult_at(8.7) <= 205
    assert multi.mult_at(1000) == multi.CRASH_CAP and multi.time_to(multi.CRASH_CAP) < 60
    assert multi.parse_auto("2.5x") == 250 and multi.parse_auto("3배") == 300 and multi.parse_auto("a") is None


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
