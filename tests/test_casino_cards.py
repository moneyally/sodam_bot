"""카드 게임(바카라·블랙잭·하이로우) 오프라인 테스트: 태블로·배당·버튼 보안·연타·만료·환급률 시뮬레이션."""
import asyncio
import random
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import casino
from sodam.casino import core
from sodam.casino import basic
from sodam.casino import cards as C

test, run_all = runner()
CHAT = -100500
SUIT = {"♠": 0, "♥": 1, "♦": 2, "♣": 3}
RANK = {"A": 1, "J": 11, "Q": 12, "K": 13}


def card(s: str) -> C.Card:
    """'♠7', '♥K', '♦10'"""
    return C.Card(RANK.get(s[1:]) or int(s[1:]), SUIT[s[0]])


def cs(*names) -> list:
    return [card(n) for n in names]


class FixedShoe(C.Shoe):
    """진짜 슈(남은 카드 포함)인데 뽑는 순서만 정해 둔 것. 정해진 카드가 다 떨어지면 오류."""

    def __init__(self, decks, order):
        super().__init__(decks)
        self.order = list(order)

    def draw(self):
        c = self.order.pop(0)
        self.cards.remove(c)
        return c


def fix(*names):
    """다음에 만들 슈를 정해진 순서로."""
    C.make_shoe = lambda decks: FixedShoe(decks, cs(*names))


def unfix():
    C.make_shoe = lambda decks: C.Shoe(decks)


class Bot(FakeBot):
    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit_text", chat_id, message_id, text, kw))


class Msg(FakeMsg):
    """reply 의 버튼까지 기록."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.kbs = []

    async def reply_text(self, text, **kw):
        self.kbs.append(kw.get("reply_markup"))
        return await super().reply_text(text, **kw)


async def setup(n_users=1, start=10_000):
    db = await make_db()
    svc = await make_svc(db)
    bot = Bot()
    users = [fake_user(100 + i, f"사장{i}") for i in range(n_users)]
    for u in users:
        await db.upsert_user(u)
        await db._write("INSERT INTO casino_accounts(chat_id, user_id, joined_at) VALUES(?,?,0)", (CHAT, u.id))
        await core.credit(db, CHAT, u.id, start, "join")
    C.HANDS.clear()
    core._last_bet.clear()
    basic.sleep = _no_wait                  # 카드 뒤집기·딜러 공개 연출을 실제로 기다리지 않게
    C.IMAGES = False                        # 이 파일은 글자 화면을 검사 (카드 그림 경로는 test_games_all)
    return svc, bot, users


async def _no_wait(_):
    await asyncio.sleep(0)


def ctx(svc, bot, user, text):
    msg = Msg(CHAT, user, text)
    return casino.Ctx(svc, bot, msg, CHAT, user, None, text.split()[1:]), msg


async def cmd(svc, bot, user, text):
    core._last_bet.clear()
    msg = Msg(CHAT, user, text)
    assert await casino.dispatch(svc, bot, msg, CHAT, user, None, text)
    return msg


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row] if kb else []


def btn(kb, label_part):
    return next(b.callback_data for b in buttons(kb) if label_part in b.text)


async def press(svc, bot, user, data):
    q = FakeQuery(CHAT, user, data)
    prefix, _, rest = data.partition(":")
    assert prefix == "cs"
    await casino.on_callback(svc, bot, q, rest.split(":"))
    assert len(q.answers) == 1, q.answers   # 콜백마다 answer 정확히 한 번
    return q


async def ledger_ok(svc, users, start=10_000):
    """원장 합 = 잔액, 잔액 ≥ 0."""
    for u in users:
        bal = await core.balance(svc.db, CHAT, u.id)
        s = (await svc.db._one("SELECT COALESCE(SUM(delta),0) AS s FROM casino_ledger WHERE chat_id=? AND user_id=?",
                               (CHAT, u.id)))["s"]
        assert bal == s, (bal, s)
        assert bal >= 0


# ── 바카라 ────────────────────────────────────────────────
@test
def bac_banker_table():
    # 표준 태블로 전체 (뱅커 합계 × 플레이어 셋째 카드 점수)
    draw_on = {0: range(10), 1: range(10), 2: range(10), 3: [0, 1, 2, 3, 4, 5, 6, 7, 9],
               4: [2, 3, 4, 5, 6, 7], 5: [4, 5, 6, 7], 6: [6, 7], 7: []}
    for b in range(8):
        for x in range(10):
            third = C.Card(x if x else 10, 0)
            assert C.banker_draws(b, third) == (x in draw_on[b]), (b, x)
    for b in range(10):
        assert C.banker_draws(b, None) == (b <= 5)


@test
def bac_fixed_deals():
    def deal(*names):
        return C.deal_baccarat(FixedShoe(8, cs(*names)))
    # 순서: P1 B1 P2 B2 (P3) (B3)
    r = deal("♠4", "♥K", "♦4", "♣5")                       # 플 8 내추럴 → 아무도 안 받음
    assert (len(r.player), len(r.banker), r.p, r.b, r.winner, r.natural) == (2, 2, 8, 5, "player", True)
    r = deal("♠2", "♥3", "♦3", "♣K", "♠8")                 # 플 5 → 받음(8), 뱅 3 + 셋째 8 → 멈춤
    assert (len(r.player), len(r.banker), r.p, r.b) == (3, 2, 3, 3) and r.winner == "tie"
    r = deal("♠K", "♥2", "♦6", "♣3", "♠4")                 # 플 6 멈춤 → 뱅 5 받음 → 9
    assert (len(r.player), len(r.banker), r.p, r.b, r.winner) == (2, 3, 6, 9, "banker")
    r = deal("♠A", "♥6", "♦2", "♣K", "♠5")                 # 플 3 → 5 받아 8, 뱅 6 + 셋째 5 → 멈춤
    assert (len(r.banker), r.p, r.b, r.winner) == (2, 8, 6, "player")
    r = deal("♠A", "♥6", "♦2", "♣K", "♠6", "♥3")           # 뱅 6 + 셋째 6 → 받음 → 9:9 타이
    assert (len(r.banker), r.p, r.b, r.winner) == (3, 9, 9, "tie")
    r = deal("♠A", "♥4", "♦2", "♣K", "♠A")                 # 뱅 4 + 셋째 A(1) → 멈춤
    assert len(r.banker) == 2 and (r.p, r.b) == (4, 4)
    r = deal("♠K", "♥7", "♦7", "♣K")                        # 플 7 멈춤, 뱅 7 멈춤
    assert (len(r.player), len(r.banker), r.winner) == (2, 2, "tie")
    r = deal("♠2", "♥9", "♦2", "♣K", "♠3")                 # 뱅 9 내추럴이면 플레이어도 안 받음
    assert (len(r.player), r.winner) == (2, "banker")


@test
def bac_payouts():
    pw = C.BacRound(cs("♠4", "♦4"), cs("♥K", "♣5"))      # 플 승
    bw = C.BacRound(cs("♠K", "♦6"), cs("♥2", "♣7"))      # 뱅 승
    tie = C.BacRound(cs("♠K", "♦7"), cs("♥7", "♣K"))
    assert C.bac_payout(pw, "player", 1000) == 2000
    assert C.bac_payout(pw, "banker", 1000) == 0
    assert C.bac_payout(pw, "tie", 1000) == 0
    assert C.bac_payout(bw, "banker", 1000) == 1950
    assert C.bac_payout(bw, "player", 1000) == 0
    assert C.bac_payout(tie, "tie", 1000) == 10000
    assert C.bac_payout(tie, "player", 1000) == 1000 and C.bac_payout(tie, "banker", 1000) == 1000  # 푸시
    pair = C.BacRound(cs("♠8", "♦8"), cs("♥Q", "♣Q"))
    assert C.bac_payout(pair, "ppair", 1000) == 13000 and C.bac_payout(pair, "bpair", 1000) == 13000
    assert C.bac_payout(pw, "ppair", 1000) == 13000        # ♠4 ♦4 (무늬 달라도 숫자 같으면 페어)
    assert C.bac_payout(pw, "bpair", 1000) == 0
    kq = C.BacRound(cs("♠K", "♦Q"), cs("♥2", "♣7"))        # K·Q 는 둘 다 0점이지만 숫자가 달라 페어 아님
    assert C.bac_payout(kq, "ppair", 1000) == 0


@test
async def bac_command_flow():
    svc, bot, (u,) = await setup()
    fix("♠4", "♥K", "♦4", "♣5")
    msg = await cmd(svc, bot, u, "!바카라 1000 플")
    assert await core.balance(svc.db, CHAT, u.id) == 11_000
    out = bot.named("edit_text")[-1][3]                          # 카드 뒤집는 연출 → 마지막 수정이 결과 화면
    assert "「4♠️」「4♦️」" in out and "플레이어 승" in out and "+1,000P" in out, out
    fix("♠K", "♥7", "♦7", "♣K")
    msg = await cmd(svc, bot, u, "!바카라 뱅 2000")
    out = bot.named("edit_text")[-1][3]
    assert await core.balance(svc.db, CHAT, u.id) == 11_000 and "타이" in out and "본전" in out
    fix("♠K", "♥7", "♦7", "♣K")
    await cmd(svc, bot, u, "!바카라 1000 T")
    assert await core.balance(svc.db, CHAT, u.id) == 20_000
    fix("♠9", "♥Q", "♦9", "♣Q")                            # 플 8 내추럴, 뱅 Q·Q 페어
    await cmd(svc, bot, u, "!바카라 1000 뱅페어")
    assert await core.balance(svc.db, CHAT, u.id) == 32_000
    msg = await cmd(svc, bot, u, "!바카라 1000")                 # 선택 없음 → 사용법, 차감 없음
    assert "플페어" in msg.replies[-1] and await core.balance(svc.db, CHAT, u.id) == 32_000
    msg = await cmd(svc, bot, u, "!바카라 99999999 플")          # 잔액 부족
    assert await core.balance(svc.db, CHAT, u.id) == 32_000
    unfix()
    await ledger_ok(svc, [u])


# ── 블랙잭 ────────────────────────────────────────────────
def bj_start_order(p1, d1, p2, d2, *rest):
    fix(p1, d1, p2, d2, *rest)


@test
def bj_values():
    assert C.bj_value(cs("♠A", "♥6")) == (17, True)
    assert C.bj_value(cs("♠A", "♥6", "♦K")) == (17, False)
    assert C.bj_value(cs("♠A", "♥A", "♦9")) == (21, True)
    assert C.is_blackjack(cs("♠A", "♥K")) and not C.is_blackjack(cs("♠7", "♥7", "♦7"))
    d = cs("♠A", "♥6")                                        # 소프트 17 에서도 멈춤
    C.dealer_play(d, FixedShoe(6, []))
    assert len(d) == 2


@test
async def bj_natural_and_dealer_bj():
    svc, bot, (u,) = await setup()
    bj_start_order("♠A", "♥9", "♦K", "♣7")
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    assert await core.balance(svc.db, CHAT, u.id) == 11_500 and "블랙잭!" in msg.replies[-1]
    assert not C.HANDS and msg.kbs[-1] is None
    bj_start_order("♠9", "♥A", "♦7", "♣K")                  # 딜러 블랙잭
    await cmd(svc, bot, u, "!블랙잭 1000")
    assert await core.balance(svc.db, CHAT, u.id) == 10_500
    bj_start_order("♠A", "♥A", "♦K", "♣Q")                  # 둘 다 → 푸시
    await cmd(svc, bot, u, "!블랙잭 1000")
    assert await core.balance(svc.db, CHAT, u.id) == 10_500
    unfix()
    await ledger_ok(svc, [u])


@test
async def bj_bust_push_win():
    svc, bot, (u,) = await setup()
    bj_start_order("♠10", "♥9", "♦6", "♣7", "♠K")           # 16 히트 K → 버스트
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    assert await core.balance(svc.db, CHAT, u.id) == 9_000
    q = await press(svc, bot, u, btn(msg.kbs[-1], "히트"))
    assert "버스트" in q.edits[-1] and q.kb is None and not C.HANDS
    assert await core.balance(svc.db, CHAT, u.id) == 9_000

    bj_start_order("♠10", "♥10", "♦8", "♣8")                 # 18 vs 18 → 푸시
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    q = await press(svc, bot, u, btn(msg.kbs[-1], "스탠드"))
    assert "푸시" in q.edits[-1] and await core.balance(svc.db, CHAT, u.id) == 9_000

    bj_start_order("♠10", "♥10", "♦9", "♣6", "♠2", "♥7")     # 히트 2 → 21 자동 스탠드, 딜러 16+7 버스트
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    q = await press(svc, bot, u, btn(msg.kbs[-1], "히트"))
    assert "딜러 버스트" in q.edits[-1] and await core.balance(svc.db, CHAT, u.id) == 10_000, q.edits
    unfix()
    await ledger_ok(svc, [u])


@test
async def bj_double():
    svc, bot, (u,) = await setup()
    bj_start_order("♠5", "♥10", "♦6", "♣6", "♠10", "♥9")    # 11 더블 → 21, 딜러 16+9 버스트
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    q = await press(svc, bot, u, btn(msg.kbs[-1], "더블"))
    assert "(더블)" in q.edits[-1] and "베팅 2,000P" in q.edits[-1]
    assert await core.balance(svc.db, CHAT, u.id) == 12_000
    acc = await core.account(svc.db, CHAT, u.id)
    assert acc["wagered"] == 2000 and acc["won"] == 4000
    unfix()
    await ledger_ok(svc, [u])


@test
async def bj_double_insufficient():
    svc, bot, (u,) = await setup(start=1000)
    bj_start_order("♠5", "♥10", "♦6", "♣6", "♠10")
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    assert await core.balance(svc.db, CHAT, u.id) == 0
    q = await press(svc, bot, u, btn(msg.kbs[-1], "더블"))
    assert "모자라" in (q.answers[0][0] or "") and not q.edits
    h = next(iter(C.HANDS.values()))
    assert h.bet == 1000 and not h.done and await core.balance(svc.db, CHAT, u.id) == 0
    q = await press(svc, bot, u, btn(msg.kbs[-1], "스탠드"))   # 토큰이 안 바뀌었으니 같은 판 계속
    assert q.edits and not C.HANDS
    unfix()
    await ledger_ok(svc, [u], start=1000)


@test
async def bj_forged_old_other():
    svc, bot, (u, v) = await setup(2)
    bj_start_order("♠10", "♥9", "♦2", "♣7", "♠3", "♥4")
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    kb = msg.kbs[-1]
    hit = btn(kb, "히트")
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons(kb))
    q = await press(svc, bot, v, hit)                                 # 다른 사람
    assert "다른 사람" in q.answers[0][0] and not q.edits
    q = await press(svc, bot, v, hit.replace(f":{u.id}:", f":{v.id}:"))   # 남의 판을 자기 id 로 위조 → 판 없음
    assert "지난 버튼" in q.answers[0][0]
    parts = hit.split(":")
    q = await press(svc, bot, u, ":".join(parts[:3] + ["AAAAAAAA", "h"]))  # 위조 토큰
    assert "지난 버튼" in q.answers[0][0] and not q.edits
    q = await press(svc, bot, u, "cs:bj:abc:x:h")
    assert "지난 버튼" in q.answers[0][0]
    q = await press(svc, bot, u, "cs:zz:1:2:3")
    q = await press(svc, bot, u, hit)                                 # 진짜 히트
    assert q.edits and len(C.HANDS[("bj", CHAT, u.id)].player) == 3
    q = await press(svc, bot, u, hit)                                 # 옛 버튼 다시
    assert "지난 버튼" in q.answers[0][0] and not q.edits
    assert len(C.HANDS[("bj", CHAT, u.id)].player) == 3
    msg2 = await cmd(svc, bot, u, "!블랙잭 1000")                     # 한 사람 한 판
    assert "진행 중" in msg2.replies[-1] and await core.balance(svc.db, CHAT, u.id) == 9_000
    unfix()


@test
async def bj_double_press_pays_once():
    svc, bot, (u,) = await setup()
    bj_start_order("♠10", "♥10", "♦9", "♣7")                 # 19 vs 17 → 승
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    stand = btn(msg.kbs[-1], "스탠드")
    dbl = btn(msg.kbs[-1], "더블")
    qs = await asyncio.gather(press(svc, bot, u, stand), press(svc, bot, u, stand), press(svc, bot, u, dbl))
    assert sum(1 for q in qs if q.edits) == 1
    assert await core.balance(svc.db, CHAT, u.id) == 11_000
    n = (await svc.db._one("SELECT COUNT(*) AS n FROM casino_ledger WHERE reason='win:blackjack'"))["n"]
    assert n == 1
    unfix()
    await ledger_ok(svc, [u])


@test
async def bj_expiry_auto_stand():
    svc, bot, (u, v) = await setup(2)
    bj_start_order("♠10", "♥10", "♦9", "♣7")
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    stand = btn(msg.kbs[-1], "스탠드")
    C.HANDS[("bj", CHAT, u.id)].expires = time.monotonic() - 1
    # 다른 사람이 아무 카드 게임 명령만 해도 lazy 정리
    await cmd(svc, bot, v, "!하이로우")
    edits = bot.named("edit_text")
    assert edits and "자동 스탠드" in edits[-1][3] and edits[-1][4].get("reply_markup") is None
    assert not C.HANDS and await core.balance(svc.db, CHAT, u.id) == 11_000
    q = await press(svc, bot, u, stand)                              # 끝난 판 버튼
    assert "이미 끝났" in q.answers[0][0]
    # 누른 순간 만료된 경우: 그 버튼으로 자동 스탠드
    bj_start_order("♠10", "♥10", "♦5", "♣7")
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    C.HANDS[("bj", CHAT, u.id)].expires = time.monotonic() - 1
    await C.sweep(now=time.monotonic() - 10)                          # 아직 안 지난 시각 기준 → 그대로
    assert C.HANDS
    q = await press(svc, bot, u, btn(msg.kbs[-1], "히트"))
    assert not C.HANDS
    assert await core.balance(svc.db, CHAT, u.id) == 10_000           # 15 vs 17 스탠드 → 짐 (히트 안 됨)
    unfix()
    await ledger_ok(svc, [u, v])


# ── 하이로우 ──────────────────────────────────────────────
@test
async def hilo_cashout_math():
    svc, bot, (u,) = await setup()
    fix("♠7", "♥K", "♦2")
    msg = await cmd(svc, bot, u, "!하이로우 1000")
    h = C.HANDS[("hl", CHAT, u.id)]
    rest = h.shoe.remaining()
    assert len(rest) == 51
    ph, pl = C.hl_odds(card("♠7"), rest)
    assert (ph, pl) == (24 / 51, 24 / 51)
    m = C.hl_mult(ph)
    assert m == int(0.97 * 51 / 24 * 100) / 100 == 2.06
    kb = msg.kbs[-1]
    assert "×2.06" in btn_text(kb, "하이")
    q = await press(svc, bot, u, btn(kb, "하이"))
    assert h.prize == 2060 and h.step == 1 and "적중" in q.edits[-1]
    # K 다음: 하이 없음(나올 카드 없음), 로우 = 남은 50장 중 K 3장 뺀 47장
    ph, pl = C.hl_odds(card("♥K"), h.shoe.remaining())
    assert ph == 0 and pl == 47 / 50
    assert not any("하이" in b.text for b in buttons(q.kb))
    m2 = C.hl_mult(pl)
    q = await press(svc, bot, u, btn(q.kb, "로우"))
    assert h.prize == int(2060 * m2)
    q = await press(svc, bot, u, btn(q.kb, "그만"))
    assert await core.balance(svc.db, CHAT, u.id) == 9_000 + h.prize and not C.HANDS
    assert "받고 그만" in q.edits[-1]
    unfix()
    await ledger_ok(svc, [u])


def btn_text(kb, part):
    return next(b.text for b in buttons(kb) if part in b.text)


@test
async def hilo_equal_rank_loses_and_refund_at_zero():
    svc, bot, (u,) = await setup()
    fix("♠7", "♥7")
    msg = await cmd(svc, bot, u, "!하이로우 1000")
    q = await press(svc, bot, u, btn(msg.kbs[-1], "하이"))
    assert "같은 숫자라 꽝" in q.edits[-1] and not C.HANDS
    assert await core.balance(svc.db, CHAT, u.id) == 9_000
    fix("♠7")
    msg = await cmd(svc, bot, u, "!하이로우 1000")                   # 바로 그만 → 원금
    await press(svc, bot, u, btn(msg.kbs[-1], "그만"))
    assert await core.balance(svc.db, CHAT, u.id) == 9_000
    unfix()
    await ledger_ok(svc, [u])


@test
async def hilo_max_steps_and_expiry():
    svc, bot, (u,) = await setup()
    fix("♠A", "♠2", "♠3", "♠4", "♠5", "♠6", "♠7", "♠8", "♠9", "♠10", "♠J")
    msg = await cmd(svc, bot, u, "!하이로우 100")
    kb = msg.kbs[-1]
    for i in range(10):
        q = await press(svc, bot, u, btn(kb, "하이"))
        kb = q.kb
    assert "완주" in q.edits[-1] and not C.HANDS
    bal = await core.balance(svc.db, CHAT, u.id)
    assert bal > 10_000 - 100
    fix("♠7")
    await cmd(svc, bot, u, "!하이로우 1000")
    C.HANDS[("hl", CHAT, u.id)].expires = 0
    assert await C.sweep() == 1 and not C.HANDS
    assert "자동으로 그만" in bot.named("edit_text")[-1][3]
    assert await core.balance(svc.db, CHAT, u.id) == bal
    unfix()
    await ledger_ok(svc, [u])


@test
async def hilo_double_press_once():
    svc, bot, (u,) = await setup()
    fix("♠7", "♥K")
    msg = await cmd(svc, bot, u, "!하이로우 1000")
    c = btn(msg.kbs[-1], "그만")
    qs = await asyncio.gather(*(press(svc, bot, u, c) for _ in range(3)))
    assert sum(1 for q in qs if q.edits) == 1
    assert await core.balance(svc.db, CHAT, u.id) == 10_000
    unfix()
    await ledger_ok(svc, [u])


@test
async def disabled_room_gate():
    svc, bot, (u,) = await setup()
    await svc.db.set_setting(CHAT, "casino_enabled", False)
    msg = await cmd(svc, bot, u, "!블랙잭 1000")
    assert "꺼져" in msg.replies[-1] and not C.HANDS


@test
def callback_data_short():
    h = C.Hand("bj", SimpleNamespace(user=SimpleNamespace(id=9_999_999_999)), 1, None)
    h.touch()
    assert len(h.data("d").encode()) <= 64


# ── 환급률 시뮬레이션 (고정 시드) ─────────────────────────
def _sim_shoe(rnd, decks, shoe=None, low=60):
    if shoe is None or len(shoe.cards) < low:
        shoe = C.Shoe(decks, rnd.randrange)
    return shoe


RTP = {}


@test
def rtp_baccarat():
    rnd = random.Random(20260926)
    n, bet = 200_000, 100
    ret = dict.fromkeys(C.BAC_PAY, 0)
    shoe = None
    for _ in range(n):
        shoe = _sim_shoe(rnd, 8, shoe)
        rd = C.deal_baccarat(shoe)
        for k in ret:
            ret[k] += C.bac_payout(rd, k, bet)
    for k, v in ret.items():
        RTP[f"바카라 {C.BAC_LABEL[k]}"] = r = v / (n * bet)
        assert 0.90 <= r <= 1.00, (k, r)


def _bj_strategy(player, up):
    """평범한 사람 전략: 17 까지 받고, 처음 11 이면 더블."""
    t, soft = C.bj_value(player)
    if len(player) == 2 and not soft and t == 11:
        return "d"
    return "h" if t < 17 else "s"


@test
def rtp_blackjack():
    rnd = random.Random(7)
    n, stake = 50_000, 100
    wagered = paid = 0
    shoe = None
    for _ in range(n):
        shoe = _sim_shoe(rnd, 6, shoe)
        p = [shoe.draw()]
        d = [shoe.draw()]
        p.append(shoe.draw())
        d.append(shoe.draw())
        bet = stake
        if not (C.is_blackjack(p) or C.is_blackjack(d)):
            while C.bj_total(p) < 21:
                a = _bj_strategy(p, d[0])
                if a == "s":
                    break
                p.append(shoe.draw())
                if a == "d":
                    bet *= 2
                    break
            if C.bj_total(p) <= 21:
                C.dealer_play(d, shoe)
        wagered += bet                       # 더블한 판은 두 배 건 것으로
        paid += C.bj_payout(p, d, bet)[0]
    RTP["블랙잭 (단순 전략)"] = r = paid / wagered
    assert 0.90 <= r <= 1.00, r


@test
def rtp_hilo():
    rnd = random.Random(11)
    n, bet, paid = 50_000, 1000, 0
    for i in range(n):
        shoe = C.Shoe(1, rnd.randrange)
        cur, prize = shoe.draw(), bet
        stop = 1 + i % 3                      # 1~3번 맞히고 그만
        for _ in range(stop):
            ph, pl = C.hl_odds(cur, shoe.remaining())
            hi = ph >= pl
            mult = C.hl_mult(ph if hi else pl)
            nxt = shoe.draw()
            if (nxt.rank > cur.rank) if hi else (nxt.rank < cur.rank):
                prize = int(prize * mult)
                cur = nxt
            else:
                prize = 0
                break
        paid += prize
    RTP["하이로우 (1~3단계)"] = r = paid / (n * bet)
    assert 0.90 <= r <= 1.00, r
    print("RTP:", {k: round(v, 4) for k, v in RTP.items()})


@test
async def shutdown_refunds_open_hands():
    svc, bot, (u,) = await setup()
    await cmd(svc, bot, u, "!하이로우 1000")
    assert await core.balance(svc.db, CHAT, u.id) == 9_000 and C.HANDS
    await casino.shutdown(svc)
    assert await core.balance(svc.db, CHAT, u.id) == 10_000 and not C.HANDS
    await casino.shutdown(svc)                                          # 두 번 불러도 한 번만
    assert await core.balance(svc.db, CHAT, u.id) == 10_000


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
