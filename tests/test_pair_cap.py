"""바카라 페어 최대 베팅 (방 설정 casino_pair_max_bet, 2026-10-16 벳띵 '페어는 50으로'): 페어만 따로 상한, 플·뱅은 일반 최대."""
import asyncio

from fakes import runner

from sodam.casino import core

test, run_all = runner()


async def _setup():
    from test_casino_cards import CHAT, setup
    svc, bot, (u,) = await setup(start=10_000_000)
    await svc.db.set_setting(CHAT, "casino_max_bet", 3_000_000)
    return svc, bot, u, CHAT


async def _bets(svc, chat, game):
    rows = await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE chat_id=? AND reason=?", (chat, f"bet:{game}"))
    return rows[0]["n"]


@test
async def pair_over_cap_is_refused_without_taking_money():
    from test_casino_cards import cmd
    svc, bot, u, chat = await _setup()
    before = await core.balance(svc.db, chat, u.id)
    msg = await cmd(svc, bot, u, "!바카라 600000 플페어")
    assert any("페어는 한 판 최대" in t for t in msg.replies), msg.replies
    assert await core.balance(svc.db, chat, u.id) == before
    assert await _bets(svc, chat, "baccarat") == 0


@test
async def pair_at_cap_and_player_over_cap_still_play():
    from test_casino_cards import cmd
    svc, bot, u, chat = await _setup()
    await cmd(svc, bot, u, "!바카라 500000 뱅페어")
    await cmd(svc, bot, u, "!바카라 2000000 플")          # 플·뱅은 페어 상한과 무관 (일반 최대 300만)
    assert await _bets(svc, chat, "baccarat") == 2


@test
async def table_baccarat_pair_also_capped():
    from test_casino_cards import cmd
    svc, bot, u, chat = await _setup()
    msg = await cmd(svc, bot, u, "!회차 900000 플페어")
    assert any("페어는 한 판 최대" in t for t in msg.replies), msg.replies
    assert await _bets(svc, chat, "bactable") == 0


@test
async def zero_means_same_as_general_max():
    from test_casino_cards import cmd
    svc, bot, u, chat = await _setup()
    await svc.db.set_setting(chat, "casino_pair_max_bet", 0)
    await cmd(svc, bot, u, "!바카라 2000000 플페어")
    assert await _bets(svc, chat, "baccarat") == 1


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
