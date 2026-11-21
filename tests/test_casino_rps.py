"""✊✋✌ 가위바위보 (전목사 봇 → 소담 casino 한 게임): 승 ×1.95·무 환불·패 꽝 + 방 난이도(tilt) + 버튼 베팅판.

python tests/run_all.py casino_rps
"""
from fakes import FakeQuery, fake_user, runner
from test_casino_cards import CHAT, Msg, buttons, cmd, ledger_ok, setup

from sodam import casino
from sodam.casino import anim, core, rps

test, run_all = runner()


# ── 승·무·패 판정과 배당 (순수 로직) ─────────────────────────
@test
def result_and_payout():
    assert rps.result("rock", "scissors") == "win"     # 바위가 가위를 이김
    assert rps.result("rock", "paper") == "lose"        # 보에 짐
    assert rps.result("rock", "rock") == "draw"
    # 어떤 손이든 하나는 이기고 하나는 지고 하나는 비김 (삼항 완전성)
    for m in rps.SHAPES:
        got = sorted(rps.result(m, b) for b in rps.SHAPES)
        assert got == ["draw", "lose", "win"], (m, got)
    assert rps.payout_of(1000, "rock", "scissors") == 1950   # 승 ×1.95 (뮤테이션: WIN_MULT 1.95→1 이면 1000)
    assert rps.payout_of(1000, "rock", "rock") == 1000       # 무 = 환불
    assert rps.payout_of(1000, "rock", "paper") == 0         # 패 = 꽝


# ── 봇 손은 난이도(tilt)로: 지는 확률이 목표 % ───────────────
def _lose_rate(pct, pick="rock", n=3000):
    draw = lambda: rps.SHAPES[core.rng(3)]
    lost = lambda b: rps.payout_of(1, pick, b) == 0           # 유저가 지는 손 (무승부는 안 짐)
    return sum(1 for _ in range(n) if lost(core.tilt_pick(pct, draw, lost))) / n


@test
def tilt_sets_loss_rate():
    assert 0.27 < _lose_rate(0) < 0.40, "공정 방이면 지는 확률 ≈ 1/3"
    assert 0.76 < _lose_rate(80) < 0.84, "난이도 80 이면 약 80% 패"
    assert 0.27 < _lose_rate(0, "paper") < 0.40, "어떤 손으로 내도 공정"


# ── 명령 한 판: 돈이 정확히 빠지고 지급되고 열린 베팅이 닫힌다 ──
@test
async def command_plays_and_settles():
    svc, bot, (u,) = await setup(start=1_000_000)
    await svc.db.set_state(0, core.LOSE_KEY, 0)
    core._lose["at"] = -1e9
    for _ in range(40):
        await cmd(svc, bot, u, "!가위바위보 1000 바위")
    bets = (await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE reason LIKE 'bet:rps%'"))[0]["n"]
    assert bets == 40, bets
    await ledger_ok(svc, [u], start=1_000_000)               # 원장 합 = 잔액, 잔액 ≥ 0 (지급·차감 정합)
    opened = (await svc.db._all(
        "SELECT COALESCE(SUM(amount), 0) s FROM casino_open WHERE chat_id=? AND user_id=?", (CHAT, u.id)))[0]["s"]
    assert opened == 0, f"열린 베팅이 남으면 재시작 때 환불돼 포인트가 생김: {opened}"


@test
async def command_uses_room_difficulty():
    """난이도 80 방이면 대부분 짐 → 딴 판(승+무, payout>0 = win:rps 기록)이 적어야 한다."""
    svc, bot, (u,) = await setup(start=100_000_000)
    await svc.db.set_state(0, core.LOSE_KEY, 80)
    core._lose["at"] = -1e9
    for _ in range(150):
        await cmd(svc, bot, u, "!가위바위보 1000 가위")
    wins = (await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE reason LIKE 'win:rps%'"))[0]["n"]
    bets = (await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE reason LIKE 'bet:rps%'"))[0]["n"]
    assert bets == 150, bets
    assert wins < 150 * 0.4, f"난이도 80 인데 딴 판이 너무 많음: {wins}"   # 승+무 ≈ 20%
    await svc.db.set_state(0, core.LOSE_KEY, 0)
    core._lose["at"] = -1e9


# ── 버튼 베팅판: 금액만 치면 가위·바위·보 버튼, 판 주인만 누름 ──
async def _press(svc, bot, user, data):
    q = FakeQuery(CHAT, user, data)
    q.message = Msg(CHAT, user, "", message_id=777)           # reply·수정이 닿을 베팅판 메시지
    core._last_bet.clear()
    await casino.on_callback(svc, bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers                     # 누를 때마다 응답 정확히 한 번
    return q


@test
async def board_is_owner_only_and_plays():
    svc, bot, (u,) = await setup(start=100_000)
    m = await cmd(svc, bot, u, "!가위바위보 1000")            # 손을 안 적음 → 버튼판
    kb = m.kbs[-1]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons(kb))
    data = next(b.callback_data for b in buttons(kb) if "바위" in b.text)
    assert data.endswith(":rock"), data

    other = fake_user(999, "남")
    bal0 = await core.balance(svc.db, CHAT, u.id)
    q = await _press(svc, bot, other, data)
    assert "본인" in q.answers[0][0], q.answers
    assert await core.balance(svc.db, CHAT, u.id) == bal0, "남이 눌러도 내 돈이 빠지면 안 됨"

    await _press(svc, bot, u, data)                           # 본인 → 한 판 진행
    bets = (await svc.db._all("SELECT count(*) n FROM casino_ledger WHERE reason LIKE 'bet:rps%'"))[0]["n"]
    assert bets == 1, bets
    await ledger_ok(svc, [u], start=100_000)


# ── !도움 목록에 자동으로 뜸 + GIF 가 결과대로 그려짐 ──────────
@test
def help_and_gif():
    assert "가위바위보" in casino.help_text()
    g = anim.rps("rock", "scissors")
    assert g[:4] == b"GIF8" and len(g) > 100
    # 결과(테두리 색)가 다르면 그림도 다름 — 승(초록) vs 패(빨강)
    assert anim.rps("rock", "paper") != g


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
