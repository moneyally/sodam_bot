"""게임 심층 감사(2026-09-27) 수정 재발 방지: python tests/run_all.py fix_games_audit
카드(블랙잭·하이로우) · 멀티(그래프·경마) · 돈(열린 베팅·파산 구제) · 끝말잇기 · 방별 전송 제한."""
import asyncio
import sqlite3
import time

from telegram.error import BadRequest, RetryAfter

import test_casino_cards as K
import test_casino_core as CC
import test_casino_multi as TM
import test_wordchain2 as W
from fakes import FakeQuery, add_member, fake_user, runner

from sodam import casino, games, wordbot
from sodam.casino import cards as C
from sodam.casino import core, multi
from sodam.ratelimit import ChatRateLimiter

test, run_all = runner()
TM_WINDOW = multi.CRASH_WINDOW


async def cb(svc, bot, q):
    await casino.on_callback(svc, bot, q, q.data.split(":")[1:])
    return q


class FailEditQ(FakeQuery):
    async def edit_message_text(self, text, **kw):
        raise RetryAfter(30)


# ── 카드 ──────────────────────────────────────────────────
@test
async def bj_hit_screen_fails_new_message_has_working_buttons():
    """히트 뒤 화면 수정이 429 → 새 메시지로 지금 카드+버튼 (예전: 화면은 옛 카드, 버튼은 옛 토큰이라 전부 '지난 버튼')."""
    svc, bot, (u,) = await K.setup()
    K.fix("♠5", "♠9", "♠6", "♠7", "♠2", "♠10")          # 나 5·6 → 히트 2 = 13 · 딜러 9·7 → 10 받고 버스트
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    await cb(svc, bot, FailEditQ(K.CHAT, u, K.btn(msg.kbs[-1], "히트")))
    sent = [c for c in bot.named("send_message") if c[3].get("reply_markup")]
    assert sent and "<b>13</b>" in sent[-1][2], sent
    await K.press(svc, bot, u, K.btn(sent[-1][3]["reply_markup"], "스탠드"))
    assert not C.HANDS and await core.balance(svc.db, K.CHAT, u.id) == 11_000
    await K.ledger_ok(svc, [u])
    K.unfix()


@test
async def bj_hit_screen_and_send_fail_old_buttons_still_work():
    svc, bot, (u,) = await K.setup()
    K.fix("♠5", "♠9", "♠6", "♠7", "♠2", "♠10")
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    kb = msg.kbs[-1]

    async def no_send(*a, **k):
        raise RetryAfter(30)
    bot.send_message = no_send
    q = await cb(svc, bot, FailEditQ(K.CHAT, u, K.btn(kb, "히트")))
    assert "합계 13" in q.answers[0][0], q.answers
    del bot.send_message
    await K.press(svc, bot, u, K.btn(kb, "스탠드"))            # 화면에 보이는 (옛) 버튼이 그대로 먹힘
    assert not C.HANDS and await core.balance(svc.db, K.CHAT, u.id) == 11_000
    K.unfix()


class DeadAnswerQ(FakeQuery):
    async def answer(self, *a, **k):
        raise BadRequest("Query is too old and response timeout expired")


@test
async def bj_result_shown_even_if_answer_fails():
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♠9", "♠7", "♠7", "♠K")                   # 나 17 · 딜러 16 → K 버스트
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    q = await cb(svc, bot, DeadAnswerQ(K.CHAT, u, K.btn(msg.kbs[-1], "스탠드")))
    assert q.edits and "딜러 버스트" in q.edits[-1], q.edits
    assert await core.balance(svc.db, K.CHAT, u.id) == 11_000
    K.unfix()


@test
async def sweep_does_not_wait_for_slow_screen():
    """만료 정리는 돈만 바로, 화면 수정은 뒤에서 (한 방의 429 대기가 다른 방 카드 명령·버튼을 30초 막던 것)."""
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♠9", "♠8", "♠7")                         # 나 18 · 딜러 16 → 스탠드하면 딜러 한 장 더
    K.fix("♠10", "♠9", "♠8", "♠7", "♠A")                   # 딜러 17 → 나 18 승
    await K.cmd(svc, bot, u, "!블랙잭 1000")

    async def slow(*a, **k):
        await asyncio.sleep(5)
    bot.edit_message_text = slow
    C.HANDS[("bj", K.CHAT, u.id)].expires = 0
    t = time.monotonic()
    assert await C.sweep() == 1
    assert time.monotonic() - t < 1.0
    assert await core.balance(svc.db, K.CHAT, u.id) == 11_000 and not C.HANDS
    for task in list(C._BG):
        task.cancel()
    K.unfix()


@test
async def bj_settle_failure_keeps_hand_and_retry_pays():
    """정산 저장 실패(디스크 가득 등) → 판을 다시 열어 둠 → 같은 버튼 다시 누르면 지급 (예전: 판만 닫히고 이긴 돈 사라짐)."""
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♠9", "♠7", "♠7", "♠K")
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    stand = K.btn(msg.kbs[-1], "스탠드")
    real, fails = core.credit, [1]

    async def flaky(*a, **k):
        if k.get("close") and fails:
            fails.pop()
            raise sqlite3.OperationalError("database or disk is full")
        return await real(*a, **k)
    core.credit = flaky
    try:
        q = await K.press(svc, bot, u, stand)
    finally:
        core.credit = real
    assert "다시 눌러" in q.answers[0][0] and ("bj", K.CHAT, u.id) in C.HANDS
    await K.press(svc, bot, u, stand)
    assert not C.HANDS and await core.balance(svc.db, K.CHAT, u.id) == 11_000
    await K.ledger_ok(svc, [u])
    K.unfix()


@test
async def bailout_settles_expired_hand_first():
    """올인 블랙잭을 두고 자리 비움 → 만료된 판이 !파산 을 영영 막던 것. 이제 먼저 정산하고 판단."""
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♥10", "♠8", "♠9")                        # 나 18 · 딜러 19 → 짐
    await K.cmd(svc, bot, u, "!블랙잭 올인")
    C.HANDS[("bj", K.CHAT, u.id)].expires = 0
    msg = await K.cmd(svc, bot, u, "!파산")
    assert "지원금" in msg.replies[-1], msg.replies
    assert await core.balance(svc.db, K.CHAT, u.id) == core.BAILOUT_POINTS
    for task in list(C._BG):
        task.cancel()
    K.unfix()


@test
async def hilo_prize_exact_cents():
    svc, bot, (u,) = await K.setup()
    K.fix("♠2", "♠9")
    real = C._hl_mults
    C._hl_mults = lambda h: (1.15, 0.0)                    # 100 × 1.15 = 114.999… 이 114 로 깎이던 것
    try:
        msg = await K.cmd(svc, bot, u, "!하이로우 100")
        await K.press(svc, bot, u, K.btn(msg.kbs[-1], "하이"))
        assert C.HANDS[("hl", K.CHAT, u.id)].prize == 115
    finally:
        C._hl_mults = real
        K.unfix()


# ── 돈: 열린 베팅 · 파산 구제 · 차감 실패 ────────────────────
@test
async def failed_debit_is_not_refunded():
    db, svc, bot, ctx = await CC.setup()
    await CC.say(ctx, CC.A, "!가입")
    real = core.debit

    async def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    core.debit = boom
    try:
        await CC.say(ctx, CC.A, "!홀짝 5000 홀")
    except sqlite3.OperationalError:
        pass
    finally:
        core.debit = real
    assert await core.balance(db, CC.CHAT, CC.A.id) == core.START_POINTS, "안 뺀 돈을 환불해서 포인트가 생김"
    assert await CC.ledger_ok(db, CC.A.id) and not core._STAKED


@test
async def bailout_refused_when_bet_open_even_if_precheck_misses():
    """!파산 확인과 지급 사이에 당첨·열린 베팅이 바뀌어도 지급 문장이 다시 확인 (DB 한 문장)."""
    db, svc, bot, ctx = await CC.setup()
    await CC.say(ctx, CC.A, "!가입")
    assert await core.debit(db, CC.CHAT, CC.A.id, core.START_POINTS, "bet:dice")   # 올인 베팅이 열려 있음
    real = core.has_open_bet

    async def missed(*a):
        return False                                       # 앞 확인이 (경쟁으로) 못 봤다고 치고
    core.has_open_bet = missed
    try:
        r = await CC.say(ctx, CC.A, "!파산")
    finally:
        core.has_open_bet = real
    assert "진행 중인 판" in r and await core.balance(db, CC.CHAT, CC.A.id) == 0, r
    await core.credit(db, CC.CHAT, CC.A.id, 500, "win:dice", close=core.START_POINTS)   # 당첨이 먼저 들어옴
    real_bal = core.balance

    async def stale(db_, cid, uid):
        return 0                                           # 앞 확인은 옛 잔액(0)을 읽음
    core.balance = stale
    core.has_open_bet = missed
    try:
        await CC.say(ctx, CC.A, "!파산")
    finally:
        core.balance, core.has_open_bet = real_bal, real
    assert await core.balance(db, CC.CHAT, CC.A.id) == 500, "당첨금과 파산 구제를 둘 다 받음"


@test
async def open_bets_closed_by_every_game_path():
    """홀짝·룰렛·바카라·블랙잭·하이로우 뒤 casino_open 이 비어 있음 (남으면 재시작 때 환불 = 포인트 생김)."""
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♥10", "♠8", "♠9")
    await K.cmd(svc, bot, u, "!바카라 1000 플")
    K.fix("♠10", "♥10", "♠8", "♠9")
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    await K.press(svc, bot, u, K.btn(msg.kbs[-1], "스탠드"))
    K.fix("♠7", "♠9")
    msg = await K.cmd(svc, bot, u, "!하이로우 1000")
    q = await K.press(svc, bot, u, K.btn(msg.kbs[-1], "하이"))
    await K.press(svc, bot, u, K.btn(q.kb, "그만"))
    K.unfix()
    assert not await svc.db._all("SELECT * FROM casino_open")
    await K.ledger_ok(svc, [u])


# ── 멀티 ──────────────────────────────────────────────────
@test
async def horse_paid_before_animation_so_shutdown_keeps_result():
    """경주 GIF(결과가 보임) 도중 봇 종료 → 예전엔 전원 환불(진 사람도). 이제 정산 먼저라 결과대로."""
    env = await TM.setup()
    multi._rand = lambda n: 0                              # 1번 우승
    a, b = env.users[:2]
    await TM.say(env, a, "!경마 1000 1")
    await TM.say(env, b, "!경마 1000 2")
    showing, hang = asyncio.Event(), asyncio.Event()

    async def gif(*a_, **k):
        showing.set()
        await hang.wait()
    env.bot.send_animation = gif
    env.clock.go.set()
    await asyncio.wait_for(showing.wait(), 5)
    await multi.abandon_all()                              # 봇 종료
    assert await TM.bal(env, a) == core.START_POINTS - 1000 + 4700
    assert await TM.bal(env, b) == core.START_POINTS - 1000
    await TM.ledger_consistent(env)
    TM.restore()


@test
async def crash_starts_when_chart_is_shown():
    """차트 보내기가 늦어도(10초) 배수는 차트가 뜬 뒤부터 오름. 뜨기 전 !스톱 은 '곧 출발'."""
    env = await TM.setup(crash=300)
    a = env.users[0]
    await TM.say(env, a, "!그래프 1000")
    r = multi.current(TM.CHAT, "crash")
    seen = []
    real = env.bot.send_message

    async def slow_send(chat_id, text, **kw):
        if "상승 중" in text:
            seen.append((await r.try_stop(a.id))[0])
            env.clock.t += 10
        return await real(chat_id, text, **kw)
    env.bot.send_message = slow_send

    async def stop():
        await r.try_stop(a.id)
    env.clock.at(TM_WINDOW + 10.0, stop)                   # 베팅 15초 + 보내기 10초 = 차트가 뜬 순간
    await TM.finish(env, "crash")
    assert seen and "곧 출발" in seen[0], seen
    assert r.players[a.id].cash_at == 100, r.players[a.id].cash_at
    await TM.ledger_consistent(env)
    TM.restore()


@test
async def stop_popup_shows_plain_name():
    env = await TM.setup(crash=300)
    u = fake_user(999, "A&B <3")
    await add_member(env.db, TM.CHAT, u)
    await TM.say(env, u, "!가입")
    env.users.append(u)
    await TM.say(env, u, "!그래프 1000")
    r = multi.current(TM.CHAT, "crash")
    q = FakeQuery(TM.CHAT, u, f"cs:cr:{r.rid}")

    async def press():
        await multi.cb_crash(env.svc, env.bot, q, [r.rid])
    env.clock.at(TM_WINDOW + 1.0, press)
    await TM.finish(env, "crash")
    assert "A&B <3" in q.answers[0][0] and "&amp;" not in q.answers[0][0], q.answers
    TM.restore()


# ── 끝말잇기 ──────────────────────────────────────────────
@test
async def wordchain_error_mid_move_ends_game_not_stuck():
    db, svc, bot, g = await W.setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}

    async def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    g.award = broken
    ok, _ = await W.text(svc, W.A, "차표")
    assert ok and g.finished and not svc.games.is_active(W.CHAT)
    assert any("문제가 생겨서" in t for t in W.said(bot))
    assert "시작" in await svc.games.start(bot, W.CHAT, W.A.id, "끝말잇기")   # 방이 풀림


@test
async def late_answer_during_bot_message_gets_reaction_not_spoiler():
    """A 가 먼저 이은 뒤 B 가 같은 문제로 답 → 🙈 (예전: '지금은 표창→창' 힌트로 봇 다음 말이 미리 보임)."""
    db, svc, bot, g = await W.setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    real_say = g.say
    posting = asyncio.Event()

    async def slow_say(text, **kw):
        posting.set()
        await asyncio.sleep(0.05)
        return await real_say(text, **kw)
    g.say = slow_say
    orig, wordbot.move = wordbot.move, W.plays("표창")
    try:
        first = asyncio.create_task(W.text(svc, W.A, "차표"))
        await posting.wait()
        ok, mb = await W.text(svc, W.B, "차고")
        await first
    finally:
        wordbot.move = orig
    assert ok and (mb.message_id, "🙈") in W.reactions(bot) and not mb.replies, (W.reactions(bot), mb.replies)


@test
async def turn_timeout_and_answer_together_send_one_correct_notice():
    """탈락 안내가 간격을 기다리는 사이 다음 사람이 답함 → 두 안내가 옛 차례로 나가던 것. 이제 한 번에 맞게."""
    db, svc, bot, g = await W.setup("끝말잇기 차례")
    for u in (W.A, W.B, W.C):
        await W.press(svc, u, "wc:j")
    await W.press(svc, W.A, "wc:go")
    await asyncio.sleep(0)
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    users = {u.id: u for u in (W.A, W.B, W.C)}
    gap = games.GAP_SECONDS
    games.GAP_SECONDS = 0.1
    try:
        n = len(W.said(bot))
        g._said = time.monotonic()                          # 방금 글을 올림 → 다음 안내는 0.1초 기다림
        out = g.players[0][0]
        g.set_timer(0, g._turn_timeout)
        await asyncio.sleep(0.02)                           # 탈락 안내가 기다리는 중
        nxt = users[g.players[0][0]]
        await W.text(svc, nxt, "차표")
        await asyncio.sleep(0.3)
    finally:
        games.GAP_SECONDS = gap
    new = W.said(bot)[n:]
    assert len(new) == 1, new
    assert "탈락" in new[0] and "✅ 차표" in new[0] and "<b>차표</b>" in new[0], new
    assert str(g.players[0][0]) in new[0] and out not in [p for p, _ in g.players]
    g.cancel_timer()


@test
async def old_join_buttons_do_not_drive_new_game():
    db, svc, bot, g1 = await W.setup("끝말잇기 차례")
    old = g1.gid
    await svc.games.stop(W.CHAT)
    assert "시작" in await svc.games.start(bot, W.CHAT, W.A.id, "끝말잇기 차례")
    g2 = svc.games.active[W.CHAT]
    q = await W.press(svc, W.B, f"wc:j:{old}")
    assert "지난 게임" in q.answers[-1][0] and not g2.players
    q = await W.press(svc, W.B, f"wc:j:{g2.gid}")
    assert g2.players and g2.players[0][0] == W.B.id
    g2.cancel_timer()


# ── 방별 전송 제한 ────────────────────────────────────────
@test
async def rate_limit_pause_only_that_room():
    """한 방이 429 를 받아도 다른 방 요청은 안 멈춤 (PTB 기본은 봇 전체를 멈춰 '한쪽 할 때 다른 쪽이 안 됨')."""
    lim = ChatRateLimiter(overall_max_rate=0, group_max_rate=0, max_retries=1)
    hit = [1]

    async def room_a():
        if hit:
            hit.pop()
            raise RetryAfter(1)
        return "a"

    async def room_b():
        return "b"
    ta = asyncio.create_task(lim.process_request(room_a, (), {}, "sendMessage", {"chat_id": -1001}, None))
    await asyncio.sleep(0.05)                                # A 가 1초 쉬는 중
    t = time.monotonic()
    assert await lim.process_request(room_b, (), {}, "sendMessage", {"chat_id": -1002}, None) == "b"
    assert time.monotonic() - t < 0.3, "다른 방까지 멈춤"
    assert await ta == "a"


if __name__ == "__main__":
    run_all()
