"""카드 게임: 바카라(푼토 방코) · 블랙잭(버튼) · 하이로우(버튼).

카드는 매 판 새로 섞은 슈(바카라 8덱 · 블랙잭 6덱 · 하이로우 1덱)에서 secrets 난수로 한 장씩 뽑는다.
테스트는 `make_shoe` 를 바꿔 끼워 정해진 순서로 돌린다.

버튼 게임(블랙잭·하이로우)
- 콜백: `cs:<bj|hl>:<user_id>:<토큰>:<동작>` (64바이트 이내). 토큰은 누를 때마다 새로 바뀐다
  → 지난 버튼·위조 버튼·연타(같은 버튼 두 번)는 거절, 정산은 정확히 한 번.
- 시작한 사람만 누를 수 있다. 한 방에서 한 사람당 게임별로 한 판만.
- 2분 동안 안 누르면 만료: 블랙잭은 자동 스탠드, 하이로우는 자동 그만(현재 상금 지급).
  따로 타이머 태스크는 없고, 다음 카드 게임 명령·버튼 때 만료된 판을 정리한다(lazy).
- 판 상태는 메모리에만 있다 (봇 재시작 시 진행 중인 판은 사라짐).
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from ..util import esc, user_name
from . import SHUTDOWN_HOOKS, Ctx, register, register_callback
from .core import balance, credit, dealer_tail, debit, fmt, result_line, rng, settle, split_bet, take_bet

SUITS = "♠♥♦♣"
RANKS = {1: "A", 11: "J", 12: "Q", 13: "K"}
HAND_TTL = 120          # 버튼 게임 만료(초)
HIDDEN = "🂠"


# ── 카드 · 슈 ─────────────────────────────────────────────
@dataclass(frozen=True)
class Card:
    rank: int   # 1(A) ~ 13(K)
    suit: int   # 0~3

    def __str__(self) -> str:
        return f"{SUITS[self.suit]}{RANKS.get(self.rank, str(self.rank))}"


class Shoe:
    """n 덱을 합친 슈. 뽑을 때마다 남은 카드 중 하나를 secrets 난수로 고른다 (= 완전히 섞은 것과 같음)."""

    def __init__(self, decks: int, randbelow=rng):
        self.cards = [Card(r, s) for _ in range(decks) for s in range(4) for r in range(1, 14)]
        self.randbelow = randbelow

    def draw(self) -> Card:
        i = self.randbelow(len(self.cards))
        self.cards[i], self.cards[-1] = self.cards[-1], self.cards[i]
        return self.cards.pop()

    def remaining(self) -> list[Card]:
        return self.cards


def make_shoe(decks: int) -> Shoe:
    return Shoe(decks)


def cards_str(cards: list[Card]) -> str:
    return " ".join(str(c) for c in cards)


def _name(user) -> str:
    return esc(user_name(user))


# ── 🃏 바카라 ─────────────────────────────────────────────
def bac_point(c: Card) -> int:
    return c.rank if c.rank < 10 else 0


def bac_total(cards: list[Card]) -> int:
    return sum(bac_point(c) for c in cards) % 10


def banker_draws(banker: int, player_third: Card | None) -> bool:
    """뱅커 세 번째 카드 규칙 (표준 태블로)."""
    if player_third is None:          # 플레이어가 멈췄으면 뱅커는 0~5 에서 받는다
        return banker <= 5
    x = bac_point(player_third)
    if banker <= 2:
        return True
    if banker == 3:
        return x != 8
    if banker == 4:
        return 2 <= x <= 7
    if banker == 5:
        return 4 <= x <= 7
    if banker == 6:
        return x in (6, 7)
    return False                      # 7 은 멈춤


@dataclass
class BacRound:
    player: list[Card]
    banker: list[Card]

    @property
    def p(self) -> int:
        return bac_total(self.player)

    @property
    def b(self) -> int:
        return bac_total(self.banker)

    @property
    def winner(self) -> str:
        return "tie" if self.p == self.b else ("player" if self.p > self.b else "banker")

    @property
    def natural(self) -> bool:
        return bac_total(self.player[:2]) >= 8 or bac_total(self.banker[:2]) >= 8

    def pair(self, side: str) -> bool:
        cs = self.player if side == "player" else self.banker
        return cs[0].rank == cs[1].rank


def deal_baccarat(shoe: Shoe) -> BacRound:
    p1, b1, p2, b2 = shoe.draw(), shoe.draw(), shoe.draw(), shoe.draw()
    rd = BacRound([p1, p2], [b1, b2])
    if rd.natural:
        return rd
    third = None
    if rd.p <= 5:
        third = shoe.draw()
        rd.player.append(third)
    if banker_draws(bac_total(rd.banker), third):
        rd.banker.append(shoe.draw())
    return rd


# 배당 (원금 포함, 100분율). 무승부 9:1 · 페어 12:1 — 모든 베팅 환급률 90~100% 안 (tests 의 시뮬레이션 참고)
BAC_PAY = {"player": 200, "banker": 195, "tie": 1000, "ppair": 1300, "bpair": 1300}
BAC_PICKS = {"플": "player", "플레이어": "player", "p": "player", "player": "player",
             "뱅": "banker", "뱅커": "banker", "b": "banker", "banker": "banker",
             "타이": "tie", "무승부": "tie", "t": "tie", "tie": "tie",
             "플페어": "ppair", "플레이어페어": "ppair", "pp": "ppair",
             "뱅페어": "bpair", "뱅커페어": "bpair", "bp": "bpair"}
BAC_LABEL = {"player": "플레이어", "banker": "뱅커", "tie": "타이", "ppair": "플레이어 페어", "bpair": "뱅커 페어"}


def bac_payout(rd: BacRound, pick: str, bet: int) -> int:
    if pick in ("ppair", "bpair"):
        return bet * BAC_PAY[pick] // 100 if rd.pair("player" if pick == "ppair" else "banker") else 0
    w = rd.winner
    if w == "tie" and pick != "tie":
        return bet                    # 타이면 플레이어·뱅커 베팅은 원금 돌려줌
    return bet * BAC_PAY[pick] // 100 if w == pick else 0


def render_baccarat(rd: BacRound, name: str, pick: str, bet: int) -> str:
    head = {"player": "🔵 <b>플레이어 승</b>", "banker": "🔴 <b>뱅커 승</b>", "tie": "🟢 <b>타이</b>"}[rd.winner]
    head += f" (플 {rd.p} : {rd.b} 뱅)"
    tags = []
    if rd.natural:
        tags.append("내추럴")
    if rd.pair("player"):
        tags.append("플레이어 페어")
    if rd.pair("banker"):
        tags.append("뱅커 페어")
    return (f"🃏 <b>바카라</b> · {name}님 {BAC_LABEL[pick]}에 {fmt(bet)}\n"
            f"🔵 플레이어 {cards_str(rd.player)} → <b>{rd.p}</b>\n"
            f"🔴 뱅커 {cards_str(rd.banker)} → <b>{rd.b}</b>\n"
            f"{head}" + (f" · {' · '.join(tags)}" if tags else "") + "\n"
            + ("타이라서 플레이어·뱅커 베팅은 원금을 돌려드려요.\n"
               if rd.winner == "tie" and pick in ("player", "banker") else ""))


BAC_USAGE = ("🃏 <b>바카라</b>: <code>!바카라 1000 플</code>\n"
             "플(플레이어) ×2 · 뱅(뱅커) ×1.95 · 타이 ×10 (타이면 플·뱅은 원금 돌려받음)\n"
             "플페어·뱅페어 ×13 (처음 두 장이 같은 숫자)")


async def g_baccarat(ctx: Ctx) -> None:
    await sweep()
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    amount, rest = split_bet(ctx.args, bal)
    pick = next((BAC_PICKS[a.lower()] for a in rest if a.lower() in BAC_PICKS), None)
    if pick is None:
        await ctx.reply(BAC_USAGE + f"\n내 잔액: <b>{fmt(bal)}</b>")
        return
    bet = await take_bet(ctx, amount, "baccarat")
    if bet is None:
        return
    rd = deal_baccarat(make_shoe(8))
    payout = bac_payout(rd, pick, bet)
    bal = await settle(ctx, bet, payout, "baccarat")
    await ctx.reply(render_baccarat(rd, _name(ctx.user), pick, bet) + result_line(bet, payout, bal) + await dealer_tail(ctx, bet, payout, bal))


# ── 버튼 게임 공통 ────────────────────────────────────────
@dataclass
class Hand:
    game: str                  # "bj" | "hl"
    ctx: Ctx                   # 정산용 (svc·bot·chat_id·user)
    bet: int                   # 걸린 금액 (블랙잭 더블이면 두 배)
    shoe: Shoe
    stake: int = 0             # 처음 베팅
    tok: str = ""
    message_id: int | None = None
    expires: float = 0.0
    done: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 블랙잭
    player: list[Card] = field(default_factory=list)
    dealer: list[Card] = field(default_factory=list)
    # 하이로우
    card: Card | None = None
    prize: int = 0
    step: int = 0
    history: list[Card] = field(default_factory=list)

    def touch(self) -> None:
        self.tok = secrets.token_urlsafe(6)   # 8글자, 누를 때마다 바뀜
        self.expires = time.monotonic() + HAND_TTL

    def data(self, act: str) -> str:
        return f"cs:{self.game}:{self.ctx.user.id}:{self.tok}:{act}"


HANDS: dict[tuple[str, int, int], Hand] = {}   # (게임, 방, 사람) → 진행 중인 판


def _key(game: str, chat_id: int, uid: int) -> tuple[str, int, int]:
    return (game, chat_id, uid)


async def _edit(h: Hand, text: str, kb=None, q=None) -> None:
    try:
        if q is not None:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif h.message_id is not None:
            await h.ctx.bot.edit_message_text(text=text, chat_id=h.ctx.chat_id, message_id=h.message_id,
                                              parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        pass


async def _expire(h: Hand, q=None) -> None:
    """만료된 판 정리: 블랙잭 자동 스탠드 · 하이로우 자동 그만. lock 안에서 부른다."""
    if h.game == "bj":
        await _bj_finish(h, q, note="⏰ 2분이 지나 자동 스탠드했어요.")
    else:
        await _hl_cashout(h, q, note="⏰ 2분이 지나 자동으로 그만했어요.")


async def sweep(now: float | None = None) -> int:
    """만료된 판을 모두 정리 (명령·버튼 때마다 호출). 정리한 수."""
    now = time.monotonic() if now is None else now
    n = 0
    for h in [h for h in HANDS.values() if not h.done and h.expires <= now]:
        async with h.lock:
            if not h.done and h.expires <= now:
                await _expire(h)
                n += 1
    return n


def _finish(h: Hand) -> None:
    h.done = True
    if HANDS.get(_key(h.game, h.ctx.chat_id, h.ctx.user.id)) is h:
        del HANDS[_key(h.game, h.ctx.chat_id, h.ctx.user.id)]


async def _start_check(ctx: Ctx, game: str, label: str) -> bool:
    """이미 진행 중인 판이 있으면 안내하고 False."""
    h = HANDS.get(_key(game, ctx.chat_id, ctx.user.id))
    if h and not h.done:
        await ctx.reply(f"진행 중인 {label}이 있어요. 위 버튼으로 마저 해주세요! (2분 지나면 자동 정리)")
        return False
    return True


async def _callback(game: str, svc, bot, q, parts: list[str], act_fn) -> None:
    """버튼 공통 검사: 주인 · 토큰 · 만료 · 연타. act_fn(h, act, q) 는 토스트 문구(또는 None)를 돌려준다."""
    uid_s, tok, act = (parts + ["", "", ""])[:3]
    if not uid_s.isdecimal():
        await q.answer("지난 버튼이에요.")
        return
    if q.from_user.id != int(uid_s):
        await q.answer("다른 사람 게임이에요. 직접 해보려면 명령어로 시작!", show_alert=False)
        return
    await sweep()
    h = HANDS.get(_key(game, q.message.chat_id, int(uid_s)))
    if h is None or h.done or not secrets.compare_digest(h.tok, tok):
        await q.answer("이미 끝났거나 지난 버튼이에요.")
        return
    toast = None
    async with h.lock:
        if h.done or not secrets.compare_digest(h.tok, tok):   # 연타: 앞의 누름이 토큰을 바꿨음
            toast = "이미 처리됐어요."
        elif h.expires <= time.monotonic():
            await _expire(h, q)
            toast = "시간이 지나 자동으로 정리했어요."
        else:
            toast = await act_fn(h, act, q)
    await q.answer(toast)


# ── 🂡 블랙잭 ─────────────────────────────────────────────
def bj_value(cards: list[Card]) -> tuple[int, bool]:
    """(합계, 소프트 여부). A 는 1 또는 11."""
    total = sum(min(c.rank, 10) for c in cards)
    if any(c.rank == 1 for c in cards) and total + 10 <= 21:
        return total + 10, True
    return total, False


def bj_total(cards: list[Card]) -> int:
    return bj_value(cards)[0]


def is_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and bj_total(cards) == 21


def dealer_play(dealer: list[Card], shoe: Shoe) -> None:
    """딜러는 17 이상(소프트 17 포함)에서 멈춘다."""
    while bj_total(dealer) < 17:
        dealer.append(shoe.draw())


def bj_payout(player: list[Card], dealer: list[Card], bet: int) -> tuple[int, str]:
    """(받을 돈(원금 포함), 결과 문구). bet 은 더블이면 두 배 된 금액."""
    pb, db_ = is_blackjack(player), is_blackjack(dealer)
    if pb and db_:
        return bet, "둘 다 블랙잭 · 푸시"
    if pb:
        return bet * 5 // 2, "🂡 <b>블랙잭!</b> (×2.5)"
    if db_:
        return 0, "딜러 블랙잭"
    p, d = bj_total(player), bj_total(dealer)
    if p > 21:
        return 0, "버스트 (21 초과)"
    if d > 21:
        return bet * 2, "딜러 버스트! 승리"
    if p > d:
        return bet * 2, "승리!"
    if p == d:
        return bet, "푸시 (동점)"
    return 0, "딜러 승 😭"


def _bj_text(h: Hand, reveal: bool, footer: str = "") -> str:
    dealer = cards_str(h.dealer) + f"  ({bj_total(h.dealer)})" if reveal else f"{h.dealer[0]} {HIDDEN}"
    p, soft = bj_value(h.player)
    ptot = f"소프트 {p}" if soft and p < 21 and not reveal else str(p)
    doubled = " (더블)" if h.bet > h.stake else ""
    return (f"🂡 <b>블랙잭</b> · {_name(h.ctx.user)}님 · 베팅 {fmt(h.bet)}{doubled}\n"
            f"딜러: {dealer}\n"
            f"내 카드: {cards_str(h.player)}  (<b>{ptot}</b>)" + (f"\n{footer}" if footer else ""))


def _bj_kb(h: Hand) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("👊 히트", callback_data=h.data("h")),
           InlineKeyboardButton("✋ 스탠드", callback_data=h.data("s"))]
    if len(h.player) == 2:
        row.append(InlineKeyboardButton("💰 더블", callback_data=h.data("d")))
    return InlineKeyboardMarkup([row])


async def _bj_finish(h: Hand, q=None, note: str = "") -> None:
    """딜러 진행 → 정산 → 메시지. lock 안에서, 한 판에 한 번만."""
    if h.done:
        return
    _finish(h)
    if bj_total(h.player) <= 21 and not is_blackjack(h.player):
        dealer_play(h.dealer, h.shoe)
    payout, verdict = bj_payout(h.player, h.dealer, h.bet)
    bal = await settle(h.ctx, h.bet, payout, "blackjack")
    footer = (note + "\n" if note else "") + f"━━━━━━━━\n{verdict}\n" + result_line(h.bet, payout, bal) + await dealer_tail(h.ctx, h.bet, payout, bal)
    await _edit(h, _bj_text(h, True, footer), None, q)


async def _bj_act(h: Hand, act: str, q) -> str | None:
    if act == "d":
        if len(h.player) != 2:
            return "더블은 처음 두 장일 때만 돼요."
        extra = h.bet
        if not await debit(h.ctx.svc.db, h.ctx.chat_id, h.ctx.user.id, extra, "bet:blackjack"):
            return f"잔액이 모자라서 더블을 못 해요. (더블엔 {fmt(extra)} 더 필요)"
        await h.ctx.svc.db._write("UPDATE casino_accounts SET wagered=wagered+? WHERE chat_id=? AND user_id=?",
                                  (extra, h.ctx.chat_id, h.ctx.user.id))
        h.bet += extra
        h.player.append(h.shoe.draw())
        await _bj_finish(h, q)
        return None
    if act == "s":
        await _bj_finish(h, q)
        return None
    if act == "h":
        h.player.append(h.shoe.draw())
        if bj_total(h.player) >= 21:          # 버스트 또는 21 → 자동으로 끝
            await _bj_finish(h, q)
            return None
        h.touch()
        await _edit(h, _bj_text(h, False, "히트·스탠드 중에 골라주세요."), _bj_kb(h), q)
        return None
    return "지난 버튼이에요."


async def g_blackjack(ctx: Ctx) -> None:
    await sweep()
    if not ctx.args:
        bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
        await ctx.reply("🂡 <b>블랙잭</b>: <code>!블랙잭 1000</code>\n"
                        "21에 가까우면 승리 ×2 · 블랙잭 ×2.5 · 동점 원금 · 딜러는 17 이상에서 멈춤\n"
                        f"버튼: 히트(한 장 더) · 스탠드(멈춤) · 더블(베팅 두 배, 한 장만)\n내 잔액: <b>{fmt(bal)}</b>")
        return
    if not await _start_check(ctx, "bj", "블랙잭"):
        return
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    amount, _ = split_bet(ctx.args, bal)
    bet = await take_bet(ctx, amount, "blackjack")
    if bet is None:
        return
    # take_bet 이 await 하는 동안 같은 사람이 한 판 더 시작했을 수 있음 → 한 번 더 확인
    if HANDS.get(_key("bj", ctx.chat_id, ctx.user.id)):
        await credit(ctx.svc.db, ctx.chat_id, ctx.user.id, bet, "refund:blackjack")
        await ctx.reply("진행 중인 블랙잭이 있어서 베팅을 돌려드렸어요.")
        return
    shoe = make_shoe(6)
    h = Hand("bj", Ctx(ctx.svc, ctx.bot, None, ctx.chat_id, ctx.user, ctx.role, []), bet, shoe, stake=bet)
    h.player = [shoe.draw()]
    h.dealer = [shoe.draw()]
    h.player.append(shoe.draw())
    h.dealer.append(shoe.draw())
    if is_blackjack(h.player) or is_blackjack(h.dealer):   # 딜러 피크: 바로 결과
        h.done = True
        payout, verdict = bj_payout(h.player, h.dealer, bet)
        bal = await settle(ctx, bet, payout, "blackjack")
        await ctx.reply(_bj_text(h, True, f"━━━━━━━━\n{verdict}\n" + result_line(bet, payout, bal) + await dealer_tail(ctx, bet, payout, bal)))
        return
    h.touch()
    HANDS[_key("bj", ctx.chat_id, ctx.user.id)] = h
    sent = await ctx.reply(_bj_text(h, False, "히트·스탠드·더블 중에 골라주세요. (2분 안에)"), reply_markup=_bj_kb(h))
    h.message_id = getattr(sent, "message_id", None)


async def cb_blackjack(svc, bot, q, parts: list[str]) -> None:
    await _callback("bj", svc, bot, q, parts, _bj_act)


# ── 🔼🔽 하이로우 ──────────────────────────────────────────
HL_EDGE = 0.97          # 한 번 맞힐 때마다 기대 환급 97%
HL_MAX_STEPS = 10


def hl_odds(card: Card, remaining: list[Card]) -> tuple[float, float]:
    """(더 높을 확률, 더 낮을 확률). 같은 숫자는 꽝이라 둘 다에 안 들어감. A 가 가장 낮고 K 가 가장 높다."""
    n = len(remaining) or 1
    hi = sum(1 for c in remaining if c.rank > card.rank)
    lo = sum(1 for c in remaining if c.rank < card.rank)
    return hi / n, lo / n


def hl_mult(p: float) -> float:
    """확률 p 에 맞는 배수 (97% / p, 소수 둘째 자리에서 버림). p=0 이면 0."""
    if p <= 0:
        return 0.0
    return int(HL_EDGE / p * 100) / 100


def _hl_mults(h: Hand) -> tuple[float, float]:
    ph, pl = hl_odds(h.card, h.shoe.remaining())
    return hl_mult(ph), hl_mult(pl)


def _hl_text(h: Hand, footer: str = "") -> str:
    trail = " → ".join(str(c) for c in h.history[-6:])
    return (f"🔼🔽 <b>하이로우</b> · {_name(h.ctx.user)}님 · 베팅 {fmt(h.bet)}\n"
            + (f"지난 카드: {trail}\n" if trail else "")
            + f"지금 카드: <b>{h.card}</b>  ({h.step}/{HL_MAX_STEPS}단계)\n"
            f"현재 상금: <b>{fmt(h.prize)}</b>" + (f"\n{footer}" if footer else ""))


def _hl_kb(h: Hand) -> InlineKeyboardMarkup:
    mh, ml = _hl_mults(h)
    row = []
    if mh:
        row.append(InlineKeyboardButton(f"⬆️ 하이 ×{mh:g}", callback_data=h.data("h")))
    if ml:
        row.append(InlineKeyboardButton(f"⬇️ 로우 ×{ml:g}", callback_data=h.data("l")))
    row.append(InlineKeyboardButton("💰 그만", callback_data=h.data("c")))
    return InlineKeyboardMarkup([row])


async def _hl_cashout(h: Hand, q=None, note: str = "") -> None:
    if h.done:
        return
    _finish(h)
    bal = await settle(h.ctx, h.bet, h.prize, "hilo")
    footer = (note + "\n" if note else "") + f"━━━━━━━━\n💰 {fmt(h.prize)} 받고 그만!\n" + result_line(h.bet, h.prize, bal) + await dealer_tail(h.ctx, h.bet, h.prize, bal)
    await _edit(h, _hl_text(h, footer), None, q)


async def _hl_act(h: Hand, act: str, q) -> str | None:
    if act == "c":
        await _hl_cashout(h, q)
        return None
    if act not in ("h", "l"):
        return "지난 버튼이에요."
    mh, ml = _hl_mults(h)
    mult = mh if act == "h" else ml
    if not mult:
        return "그쪽은 나올 카드가 없어요."
    nxt = h.shoe.draw()
    prev = h.card
    ok = nxt.rank > prev.rank if act == "h" else nxt.rank < prev.rank
    h.history.append(prev)
    h.card = nxt
    pick = "하이" if act == "h" else "로우"
    if not ok:
        _finish(h)
        why = "같은 숫자라 꽝" if nxt.rank == prev.rank else "틀렸어요"
        bal = await settle(h.ctx, h.bet, 0, "hilo")
        await _edit(h, _hl_text(h, f"━━━━━━━━\n{prev} → <b>{nxt}</b> ({pick} 선택) · {why} 😭\n"
                                   + result_line(h.bet, 0, bal) + await dealer_tail(h.ctx, h.bet, 0, bal)), None, q)
        return None
    h.prize = int(h.prize * mult)
    h.step += 1
    msg = f"{prev} → <b>{nxt}</b> {pick} 적중! ×{mult:g}"
    if h.step >= HL_MAX_STEPS:
        await _hl_cashout(h, q, note=msg + f"\n🏁 {HL_MAX_STEPS}단계 완주!")
        return None
    h.touch()
    await _edit(h, _hl_text(h, msg + "\n계속 갈까요, 그만할까요? (같은 숫자는 꽝)"), _hl_kb(h), q)
    return None


async def g_hilo(ctx: Ctx) -> None:
    await sweep()
    if not ctx.args:
        bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
        await ctx.reply("🔼🔽 <b>하이로우</b>: <code>!하이로우 1000</code>\n"
                        "다음 카드가 높을지 낮을지 맞히면 상금이 불어나요 (A가 가장 낮고 K가 가장 높음, 같은 숫자는 꽝)\n"
                        f"언제든 💰 그만 · 최대 {HL_MAX_STEPS}단계\n내 잔액: <b>{fmt(bal)}</b>")
        return
    if not await _start_check(ctx, "hl", "하이로우"):
        return
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    amount, _ = split_bet(ctx.args, bal)
    bet = await take_bet(ctx, amount, "hilo")
    if bet is None:
        return
    if HANDS.get(_key("hl", ctx.chat_id, ctx.user.id)):
        await credit(ctx.svc.db, ctx.chat_id, ctx.user.id, bet, "refund:hilo")
        await ctx.reply("진행 중인 하이로우가 있어서 베팅을 돌려드렸어요.")
        return
    shoe = make_shoe(1)
    h = Hand("hl", Ctx(ctx.svc, ctx.bot, None, ctx.chat_id, ctx.user, ctx.role, []), bet, shoe,
             stake=bet, prize=bet)
    h.card = shoe.draw()
    h.touch()
    HANDS[_key("hl", ctx.chat_id, ctx.user.id)] = h
    sent = await ctx.reply(_hl_text(h, "다음 카드는 더 높을까요, 낮을까요? (같은 숫자는 꽝 · 2분 안에)"),
                           reply_markup=_hl_kb(h))
    h.message_id = getattr(sent, "message_id", None)


async def cb_hilo(svc, bot, q, parts: list[str]) -> None:
    await _callback("hl", svc, bot, q, parts, _hl_act)


register(("바카라", "baccarat", "바카"), g_baccarat, usage="금액 플|뱅|타이|플페어|뱅페어",
         help="🃏 플 ×2 · 뱅 ×1.95 · 타이 ×10 · 페어 ×13", group="카드")
register(("블랙잭", "blackjack", "bj"), g_blackjack, usage="금액", help="🂡 버튼으로 히트·스탠드·더블 · 블랙잭 ×2.5",
         group="카드")
register(("하이로우", "hilo", "하이로"), g_hilo, usage="금액", help="🔼🔽 높을까 낮을까, 맞힐수록 상금 ↑", group="카드")
register_callback("bj", cb_blackjack)
register_callback("hl", cb_hilo)


async def refund_open_hands(svc) -> int:
    """봇이 꺼질 때: 아직 안 끝난 판의 걸린 금액(더블 포함)을 돌려준다. 돌려준 판 수."""
    n = 0
    for key, h in list(HANDS.items()):
        if h.done:
            continue
        h.done = True
        await credit(svc.db, h.ctx.chat_id, h.ctx.user.id, h.bet, f"refund:{h.game}")
        HANDS.pop(key, None)
        n += 1
    return n


SHUTDOWN_HOOKS.append(refund_open_hands)
