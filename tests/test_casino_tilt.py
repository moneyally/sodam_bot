"""🎚 게임 난이도 (chat_state 0 casino_lose_pct): 손님이 질 확률을 목표 % 로 (오너 결정 2026-10-16 '10판 중 8판 지게')."""
import asyncio
import random

from fakes import runner

from sodam.casino import basic, cards, core, multi

test, run_all = runner()


def _rate(pct, draw, lost, n=3000):
    return sum(1 for _ in range(n) if lost(core.tilt_pick(pct, draw, lost))) / n


@test
def tilt_hits_target_loss_rate():
    rl = lambda: core.rng(37)
    red = lambda v: basic.roulette_win(v, "빨강") == 0
    assert 0.45 < _rate(0, rl, red) < 0.56, "0 이면 원래대로"
    assert 0.76 < _rate(80, rl, red) < 0.84
    lad = lambda: basic.LADDER[core.rng(4)]
    assert 0.76 < _rate(80, lad, lambda r: basic.ladder_payout(r, "좌") == 0) < 0.84
    deal = lambda: cards.deal_baccarat(cards.make_shoe(8))
    assert 0.76 < _rate(80, deal, lambda r: cards.bac_payout(r, "player", 100) < 100, n=1500) < 0.84


@test
def tilt_never_raises_win_rate():
    """숫자 한 칸(원래 97% 짐)은 그대로 — 이길 확률을 올리면 ×36 을 퍼 줌."""
    rl = lambda: core.rng(37)
    num = lambda v: basic.roulette_win(v, "7") == 0
    assert _rate(80, rl, num) > 0.94


@test
async def roulette_command_uses_room_difficulty():
    from test_casino_cards import setup, cmd
    svc, bot, (u,) = await setup(start=100_000_000)
    await svc.db.set_state(0, core.LOSE_KEY, 80)
    core._lose["at"] = -1e9
    for _ in range(200):
        await cmd(svc, bot, u, "!룰렛 1000 빨강")
    await svc.db.set_state(0, core.LOSE_KEY, 0)
    core._lose["at"] = -1e9
    led = await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE reason LIKE 'win:roulette%'")
    wins = led[0]["n"]
    bets = (await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE reason LIKE 'bet:roulette%'"))[0]["n"]
    assert bets == 200, bets
    assert wins < 200 * 0.3, wins


@test
def hilo_and_blackjack_tilt():
    random.seed(1)
    # 하이로우: 7 에서 '하이' (원래 ~46% 맞힘) → 80% 면 맞히는 건 ~20%
    shoe = cards.Shoe(1)
    seven = next(c for c in shoe.cards if c.rank == 7)
    shoe.cards.remove(seven)
    cs = shoe.cards
    hit = sum(1 for _ in range(3000)
              if cs[core.tilt_pick(80, lambda: shoe.randbelow(len(cs)), lambda j: not cs[j].rank > 7)].rank > 7) / 3000
    assert 0.16 < hit < 0.24, hit

    class H:
        pass
    wins = 0
    for _ in range(600):
        h = H()
        h.shoe = cards.Shoe(6)
        h.player = [cards.Card(10, 0), cards.Card(9, 1)]          # 19 로 스탠드
        h.dealer = [h.shoe.draw(), h.shoe.draw()]
        if cards.is_blackjack(h.dealer):
            continue
        d, s = core.tilt_pick(80, lambda: cards._bj_dealer_draw(h),
                              lambda r: cards.bj_payout(h.player, r[0], 100)[0] < 100)
        wins += cards.bj_payout(h.player, d, 100)[0] >= 100
    assert wins / 600 < 0.3, wins


@test
def crash_mixes_early_busts_only_when_set():
    core._lose["pct"] = 0
    low = sum(1 for _ in range(4000) if multi.crash_point() < 130) / 4000
    core._lose["pct"] = 80
    low80 = sum(1 for _ in range(4000) if multi.crash_point() < 130) / 4000
    core._lose["pct"] = 0
    assert low < 0.3 and low80 > 0.6, (low, low80)


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
