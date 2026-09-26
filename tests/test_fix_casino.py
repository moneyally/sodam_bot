"""카지노 점검 버그 회귀 테스트: python tests/run_all.py fix_casino

1 주사위 전송 실패 환불 · 2 그래프 자동배수 overflow · 3 종료 때 하이로우 상금·만료 판 정리
4 선택지 숫자를 금액보다 먼저 써도 됨 · 5 끝말잇기 점수 원장 기록 · 6 설정 캐시 TTL (DB 공유)
"""
import asyncio
from types import SimpleNamespace

import test_casino_cards as K
import test_casino_core as T
from fakes import OPEN_DBS, FakeMsg, fake_user, make_db, make_svc, runner
from telegram.error import BadRequest, NetworkError

from sodam import casino, db as dbmod
from sodam.casino import basic, core, multi
from sodam.casino import cards as C
from sodam.db import DB
from sodam.games import WordChain

test, run_all = runner()


async def reasons(db, chat, uid):
    rows = await db._all("SELECT reason FROM casino_ledger WHERE chat_id=? AND user_id=? ORDER BY id", (chat, uid))
    return [r["reason"] for r in rows]


# ── 1. send_dice 실패 → 환불 ──────────────────────────────
@test
async def dice_send_failure_refunds_bet():
    db, svc, bot, ctx = await T.setup()
    await T.say(ctx, T.A, "!가입")

    async def fail(*a, **kw):
        raise NetworkError("connection reset")
    bot.send_dice = fail
    await T.say(ctx, T.A, "!홀짝 1000 홀")
    assert await core.balance(db, T.CHAT, T.A.id) == 10_000
    assert (await reasons(db, T.CHAT, T.A.id))[-2:] == ["bet:oddeven", "refund:oddeven"]
    assert await T.ledger_ok(db, T.A.id)
    assert any("돌려드렸어요" in c[2] for c in bot.named("send_message"))


@test
async def dice_sent_even_if_original_deleted():
    db, svc, bot, ctx = await T.setup()
    await T.say(ctx, T.A, "!가입")

    async def deleted_original(chat_id, emoji="🎲", **kw):   # 텔레그램처럼: 답장 대상이 없으면 거절
        rp = kw.get("reply_parameters")
        if not (kw.get("allow_sending_without_reply") or (rp and rp.allow_sending_without_reply)):
            raise BadRequest("Message to be replied not found")
        return SimpleNamespace(message_id=1, dice=SimpleNamespace(value=3, emoji=emoji))
    bot.send_dice = deleted_original
    await T.say(ctx, T.A, "!홀짝 1000 홀")
    assert await core.balance(db, T.CHAT, T.A.id) == 10_000 - 1000 + 1950
    assert await T.ledger_ok(db, T.A.id)


# ── 2. parse_auto overflow ────────────────────────────────
@test
async def crash_auto_huge_number_no_overflow():
    for raw in ("1e308", "1e308x", "inf", "nan", "-inf", "0", "-2", "100.01"):
        assert multi.parse_auto(raw) is None, raw
    assert multi.parse_auto("2.5") == 250 and multi.parse_auto("100") == 10_000
    db, svc, bot, ctx = await T.setup()
    await T.say(ctx, T.A, "!가입")
    r = await T.say(ctx, T.A, "!그래프 1000 1e308")
    assert "자동 내리기 배수" in r, r
    assert await core.balance(db, T.CHAT, T.A.id) == 10_000


# ── 3. 종료 훅: 하이로우 상금 · 만료 판 정상 처리 ─────────
@test
async def shutdown_pays_hilo_prize_once():
    svc, bot, (u,) = await K.setup()
    K.fix("♠7", "♥K")
    msg = await K.cmd(svc, bot, u, "!하이로우 1000")
    await K.press(svc, bot, u, K.btn(msg.kbs[-1], "하이"))
    K.unfix()
    assert C.HANDS[("hl", K.CHAT, u.id)].prize == 2060
    await casino.shutdown(svc)
    await casino.shutdown(svc)                                          # 두 번 불러도 한 번만
    assert await core.balance(svc.db, K.CHAT, u.id) == 9_000 + 2060 and not C.HANDS
    assert (await reasons(svc.db, K.CHAT, u.id))[-1] == "win:hilo"
    await K.ledger_ok(svc, [u])


@test
async def shutdown_settles_expired_blackjack_normally():
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♠7", "♥Q", "♥10")                                    # 플 20 vs 딜 17
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    K.unfix()
    C.HANDS[("bj", K.CHAT, u.id)].expires = 0                           # 2분 지남 → 자동 스탠드 대상
    await casino.shutdown(svc)
    assert await core.balance(svc.db, K.CHAT, u.id) == 9_000 + 2000 and not C.HANDS
    assert (await reasons(svc.db, K.CHAT, u.id))[-1] == "win:blackjack"


@test
async def shutdown_open_blackjack_still_refunded():
    svc, bot, (u,) = await K.setup()
    K.fix("♠10", "♠7", "♥Q", "♥10")
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    K.unfix()
    await casino.shutdown(svc)
    assert await core.balance(svc.db, K.CHAT, u.id) == 10_000 and not C.HANDS
    assert (await reasons(svc.db, K.CHAT, u.id))[-1] == "refund:bj"


@test
async def shutdown_edit_after_bot_closed_settles_all():
    """실제 종료 때 봇 HTTP 가 닫혀 edit 이 RuntimeError → 그래도 모든 판 정산."""
    svc, bot, users = await K.setup(n_users=2)
    for u in users:
        await K.cmd(svc, bot, u, "!하이로우 1000")
    for h in C.HANDS.values():
        h.expires = 0

    async def closed(*a, **kw):
        raise RuntimeError("This HTTPXRequest is not initialized!")
    bot.edit_message_text = closed
    await casino.shutdown(svc)
    for u in users:
        assert await core.balance(svc.db, K.CHAT, u.id) == 10_000, u.id
    assert not C.HANDS


# ── 4. 선택지 숫자 먼저 ───────────────────────────────────
@test
async def number_pick_before_amount():
    assert core.split_bet(["6", "1000"], 0) == (1000, ["6"])
    assert core.split_bet(["1000", "6"], 0) == (1000, ["6"])
    assert core.split_bet(["홀", "1000"], 0) == (1000, ["홀"])
    assert core.split_bet(["50", "홀"], 0) == (50, ["홀"])             # 최소 미만도 금액 (→ 최소 베팅 안내)
    db, svc, bot, ctx = await T.setup([6, 6])
    await T.say(ctx, T.A, "!가입")
    await T.say(ctx, T.A, "!주사위 6 1000")
    assert await core.balance(db, T.CHAT, T.A.id) == 10_000 - 1000 + 5700
    await T.say(ctx, T.A, "!주사위 1000 6")
    assert await core.balance(db, T.CHAT, T.A.id) == 10_000 + 2 * 4700
    old = basic.rng
    basic.rng = lambda n: 7
    try:
        r = await T.say(ctx, T.A, "!룰렛 7 1000")
    finally:
        basic.rng = old
    assert "7 선택" in r, r
    assert await core.balance(db, T.CHAT, T.A.id) == 10_000 + 2 * 4700 + 35_000
    assert await T.ledger_ok(db, T.A.id)


# ── 5. 끝말잇기 점수 원장 ─────────────────────────────────
@test
async def wordchain_points_go_through_ledger():
    db = await make_db()
    svc = await make_svc(db)
    chat, u = -100777, fake_user(777, "끝말러")

    async def judge(system, user, **kw):
        return {"valid": True, "next": ""}                             # 봇이 못 이음 → 1 + 5점
    svc.llm = SimpleNamespace(json=judge)
    bot = K.Bot()
    g = WordChain(svc.games, bot, chat, u.id)
    g.last, g.used, g.busy = "기차", {"기차"}, False
    assert await g.on_text(FakeMsg(chat, u, "차표"), "차표")
    assert await core.balance(db, chat, u.id) == 6
    assert await reasons(db, chat, u.id) == ["game:끝말잇기", "game:끝말잇기"]


# ── 6. 설정 캐시 TTL (메인·딜러 봇이 DB 공유) ─────────────
@test
async def settings_cache_expires_for_other_process():
    main = await make_db()
    dealer = DB(main.path)
    await dealer.open()
    OPEN_DBS.append(dealer)
    chat = -100888
    await main.ensure_chat(chat, "방")
    assert (await dealer.get_settings(chat))["casino_max_bet"] == 100_000
    await main.set_setting(chat, "casino_max_bet", 5_000)
    assert (await dealer.get_settings(chat))["casino_max_bet"] == 100_000   # TTL 안에서는 캐시
    dealer._settings_at[chat] -= dbmod.SETTINGS_TTL + 1                   # 시간 경과
    assert (await dealer.get_settings(chat))["casino_max_bet"] == 5_000
    assert (await main.get_settings(chat))["casino_max_bet"] == 5_000       # 자기 변경은 바로


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
