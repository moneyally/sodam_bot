"""🎚 게임 난이도 방마다 (2026-10-17 오너 '공정/60/70/80 메뉴·버튼, 지금은 모든 방 80 유지'): python tests/run_all.py game_difficulty

방 설정 casino_difficulty 기본 owner = 오너 전체 기본(chat_state 0) 따름 · 방에서 공정/60/70/80 고르면 그 값 ·
오너 메인 🎚 화면이 전체 기본을 바꿈 · 방 관리자 허브 🎚 프리셋 · 손님은 !난이도 로 봄.
"""
import asyncio

from fakes import FakeQuery, fake_user, runner
from test_casino_cards import CHAT, cmd, setup

import sodam.__main__  # noqa: F401  (실제 시작 순서로 모든 설정·화면 등록)
from sodam import menu
from sodam.casino import core

test, run_all = runner()


async def fresh(svc, glob=80):
    await svc.db.set_state(0, core.LOSE_KEY, glob)
    core._lose["at"] = -1e9
    core._room_pct.clear()


@test
async def rooms_follow_owner_default_until_they_pick():
    svc, bot, _ = await setup()
    await fresh(svc, 80)
    assert await core.lose_pct(svc.db, CHAT) == 80, "기본 = 오너 전체 기본"
    await svc.db.set_setting(CHAT, "casino_difficulty", "0")
    assert await core.lose_pct(svc.db, CHAT) == 0 and core.cached_lose_pct(CHAT) == 0
    await svc.db.set_setting(CHAT, "casino_difficulty", "60")
    assert await core.lose_pct(svc.db, CHAT) == 60
    assert await core.lose_pct(svc.db, -999) == 80, "다른 방은 그대로"


@test
async def fair_room_roulette_is_not_tilted():
    """룰렛·사다리·바카라가 부르는 core.tilt 에 방 번호가 넘어가야 방 값이 먹음 (그림 그리기는 느려서 tilt 로 직접)."""
    from sodam.casino import basic
    svc, bot, _ = await setup()
    await fresh(svc, 80)
    await svc.db.set_setting(CHAT, "casino_difficulty", "0")
    red = lambda v: basic.roulette_win(v, "빨강") == 0          # noqa: E731
    lost = sum([red(await core.tilt(svc.db, lambda: core.rng(37), red, CHAT)) for _ in range(1500)]) / 1500
    assert lost < 0.6, lost                 # 원래 ≈ 51% (전체 기본 80 이었으면 ~80%)
    lost80 = sum([red(await core.tilt(svc.db, lambda: core.rng(37), red, -999)) for _ in range(1500)]) / 1500
    assert lost80 > 0.72, lost80            # 방 설정 안 한 방 = 오너 기본 80
    await fresh(svc, 0)


@test
async def owner_screen_sets_global_and_admin_cannot():
    svc, bot, _ = await setup()
    await fresh(svc, 80)
    owner = fake_user(900, "오너")
    svc.perms.owner_ids = {900}
    assert await core.lose_pct(svc.db, CHAT) == 80          # 30초 기억에 80 이 들어 있는 상태에서
    q = FakeQuery(900, owner, "")
    await menu.on_callback(svc, bot, q, ["gdo", "70"])
    assert await svc.db.get_state(0, core.LOSE_KEY, 0) == 70 and await core.lose_pct(svc.db, CHAT) == 70
    other = FakeQuery(55, fake_user(55, "남"), "")
    await menu.on_callback(svc, bot, other, ["gdo", "0"])
    assert await svc.db.get_state(0, core.LOSE_KEY, 0) == 70, "오너만"
    await fresh(svc, 0)


@test
async def room_admin_preset_and_member_blocked():
    svc, bot, _ = await setup()
    await fresh(svc, 80)
    svc.perms.admins = {1}
    await svc.db.ensure_chat(CHAT, "게임방")
    q = FakeQuery(1, fake_user(1, "방장"), "")
    await menu.on_callback(svc, bot, q, ["n", str(CHAT), "casino_difficulty", "70"])
    assert (await svc.db.get_settings(CHAT))["casino_difficulty"] == "70"
    assert "70%" in q.edits[-1]
    m = FakeQuery(77, fake_user(77, "손님"), "")
    await menu.on_callback(svc, bot, m, ["n", str(CHAT), "casino_difficulty", "0"])
    assert (await svc.db.get_settings(CHAT))["casino_difficulty"] == "70"
    await fresh(svc, 0)


@test
async def guests_can_see_room_difficulty():
    svc, bot, (u,) = await setup()
    await fresh(svc, 80)
    msg = await cmd(svc, bot, u, "!난이도")
    assert "80%" in msg.replies[-1]
    await svc.db.set_setting(CHAT, "casino_difficulty", "60")
    msg = await cmd(svc, bot, u, "!난이도")
    assert "60%" in msg.replies[-1], "이 방 값"
    await svc.db.set_setting(CHAT, "casino_difficulty", "0")
    msg = await cmd(svc, bot, u, "!도움")
    assert "공정" in msg.replies[-1]
    await fresh(svc, 0)


@test
async def games_pass_the_room_to_difficulty():
    """룰렛 명령이 이 방 번호로 난이도를 묻는지 (방 번호를 빼면 모든 방이 오너 기본으로 돌아감)."""
    from sodam.casino import basic
    svc, bot, (u,) = await setup(start=1_000_000)
    seen = []
    real = basic.tilt

    async def spy(db, draw, lost, chat_id=None):
        seen.append(chat_id)
        return await real(db, draw, lost, chat_id)
    basic.tilt = spy
    try:
        await cmd(svc, bot, u, "!룰렛 1000 빨강")
        await cmd(svc, bot, u, "!사다리 1000 좌")
    finally:
        basic.tilt = real
    assert seen == [CHAT, CHAT], seen


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
