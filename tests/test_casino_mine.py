"""⛏ 방별 채굴 끄기 (casino_mine, 2026-11-21 수리남 소통방 '채굴만 꺼 줘' — 다른 게임은 그대로).

python tests/run_all.py casino_mine
"""
from test_casino_cards import CHAT, cmd, setup

from sodam.casino import core

from fakes import runner

test, run_all = runner()


@test
async def mine_on_by_default_then_off_blocks():
    svc, bot, (u,) = await setup(start=0)
    await cmd(svc, bot, u, "!채굴")                       # 기본 켜짐 → 포인트 캠
    bal = await core.balance(svc.db, CHAT, u.id)
    assert bal > 0, "켜진 방은 채굴로 포인트 생김"

    await svc.db.set_setting(CHAT, "casino_mine", False)   # 이 방만 채굴 끔
    await svc.db._write("UPDATE casino_accounts SET last_mine=0 WHERE chat_id=? AND user_id=?", (CHAT, u.id))
    await cmd(svc, bot, u, "!채굴")                       # 쿨다운 리셋 → off 자체가 막는지 (뮤테이션: off 분기 지우면 캠 → FAIL)
    assert await core.balance(svc.db, CHAT, u.id) == bal, "꺼진 방은 쿨다운 아니어도 채굴 안 됨 (잔액 그대로)"


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
