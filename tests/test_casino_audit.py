"""카지노 감사에서 나온 돈 버그 재발 방지: python tests/test_casino_audit.py"""
import asyncio
from types import SimpleNamespace

from fakes import FakeMsg, runner
from test_casino_core import CHAT, A, ledger_ok, say, setup

from sodam import casino, handlers
from sodam.casino import basic, cards, core

test, run_all = runner()


async def raw(ctx, u, text):
    """say 와 같지만 2초 간격 기록을 지우지 않음 (동시 요청 검사용)."""
    m = FakeMsg(CHAT, u, text)
    m.chat = SimpleNamespace(id=CHAT, title="테스트방", type="supergroup")
    m.sender_chat = None
    await handlers.on_group_message(SimpleNamespace(message=m), ctx)
    return m.replies[-1] if m.replies else ""


@test
async def bang_with_only_spaces_is_not_a_command():
    for t in ("! ", "!　", "!\xa0", "!\n"):
        assert casino.parse(t) is None, t
    db, svc, bot, ctx = await setup()
    assert await say(ctx, A, "! ") == ""


@test
async def concurrent_bets_respect_gap():
    db, svc, bot, ctx = await setup([2, 2, 2])
    await say(ctx, A, "!가입")
    core._last_bet.clear()
    await asyncio.gather(*(raw(ctx, A, "!홀짝 1000 홀") for _ in range(3)))
    assert len(bot.named("send_dice")) == 1, "동시에 온 세 판이 2초 간격 검사를 다 지남"
    assert await ledger_ok(db, A.id)


@test
async def rejected_bet_does_not_start_gap():
    db, svc, bot, ctx = await setup([2])
    await say(ctx, A, "!가입")
    core._last_bet.clear()
    assert "최소 베팅" in await raw(ctx, A, "!홀짝 1 홀")
    await raw(ctx, A, "!홀짝 1000 홀")                         # 금액을 고쳐 바로 다시 → 막히면 안 됨
    assert len(bot.named("send_dice")) == 1


@test
async def no_bailout_while_allin_is_rolling():
    db, svc, bot, ctx = await setup([6])
    await say(ctx, A, "!가입")
    core._last_bet.clear()
    wait, slp = basic.DICE_WAIT, basic.sleep
    basic.DICE_WAIT, basic.sleep = 0.2, asyncio.sleep
    try:
        async def later():
            await asyncio.sleep(0.05)
            return await raw(ctx, A, "!파산")
        _, r = await asyncio.gather(raw(ctx, A, "!주사위 10000 6"), later())
    finally:
        basic.DICE_WAIT, basic.sleep = wait, slp
    assert "진행 중인 판" in r, r
    assert await core.balance(db, CHAT, A.id) == 57_000 and await ledger_ok(db, A.id)
    assert not core._STAKED


@test
async def no_bailout_while_card_hand_open():
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    await db._write("UPDATE members SET points=0 WHERE chat_id=? AND user_id=?", (CHAT, A.id))
    cards.HANDS[("bj", CHAT, A.id)] = SimpleNamespace(done=False, expires=float("inf"), lock=asyncio.Lock())
    try:
        assert "진행 중인 판" in await say(ctx, A, "!파산")
    finally:
        cards.HANDS.pop(("bj", CHAT, A.id))
    assert "지원금" in await say(ctx, A, "!파산")


@test
async def daily_flag_and_points_saved_together():
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    real = db.atomic

    async def broken(fn):
        raise RuntimeError("disk full")
    db.atomic = broken
    try:
        await say(ctx, A, "!출석")
    except RuntimeError:
        pass
    db.atomic = real
    assert "출석 완료" in await say(ctx, A, "!출석"), "지급은 실패했는데 출석 표시만 남음"
    assert await core.balance(db, CHAT, A.id) == 15_000


@test
async def error_after_bet_refunds():
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    real = basic.record

    async def boom(*a, **k):
        raise RuntimeError("db error")
    basic.record = boom
    try:
        await say(ctx, A, "!룰렛 1000 빨강")
    except RuntimeError:
        pass
    finally:
        basic.record = real
    assert await core.balance(db, CHAT, A.id) == 10_000 and await ledger_ok(db, A.id)
    assert not core._STAKED


@test
async def stats_follow_bets_and_wins():
    db, svc, bot, ctx = await setup([2])
    await say(ctx, A, "!가입")
    await say(ctx, A, "!홀짝 1000 짝")
    acc = await core.account(db, CHAT, A.id)
    assert acc["wagered"] == 1000 and acc["won"] == 1950, dict(acc)


if __name__ == "__main__":
    run_all()
