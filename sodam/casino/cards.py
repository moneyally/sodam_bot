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
import logging
import secrets
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import TelegramError

from ..util import esc, user_name
from . import SHUTDOWN_HOOKS, Ctx, basic, cardimg, register, register_callback
from .basic import edit_live, show, show_anim
from .board import record
from .core import OPEN_CHECKS, balance, credit, dealer_tail, debit, fmt, handoff, result_line, rng, settle, split_bet, take_bet

SUITS = "♠♥♦♣"
RANKS = {1: "A", 11: "J", 12: "Q", 13: "K"}
HAND_TTL = 120          # 버튼 게임 만료(초)
HIDDEN = "🎴"
SUIT_FACE = ("♠️", "♥️", "♦️", "♣️")   # 이모지 무늬: ♥️♦️ 는 빨강, ♠️♣️ 는 검정으로 보임
IMAGES = True           # 카드 그림(cardimg): 바카라 GIF · 블랙잭·하이로우 사진. 끄거나 그리기·보내기 실패면 글자 화면
log = logging.getLogger(__name__)


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


def face(c: Card) -> str:
    """화면용 카드: 「A♠️」 「10♥️」 (기본 문자 + 흔한 이모지만)."""
    return f"「{RANKS.get(c.rank, str(c.rank))}{SUIT_FACE[c.suit]}」"


def cards_str(cards: list[Card]) -> str:
    return "".join(face(c) for c in cards)


def bar(p: int, top: int = 9) -> str:
    """점수 막대 (바카라 0~9)."""
    return "█" * p + "░" * (top - p)


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


def _bac_side(icon: str, label: str, cards: list[Card], shown: int) -> str:
    """한 쪽 카드 줄 + 점수 막대. shown = 뒤집힌 카드 수 (나머지 처음 두 장은 🎴)."""
    if not shown:
        return f"{icon} <b>{label}</b> {HIDDEN}{HIDDEN}\n<code>{bar(0)}</code> ?"
    cs = cards[:shown]
    return (f"{icon} <b>{label}</b> {cards_str(cs)}{HIDDEN * max(0, 2 - shown)}\n"
            f"<code>{bar(bac_total(cs))}</code> <b>{bac_total(cs)}</b>")


def _bac_head(name: str, pick: str, bet: int) -> str:
    return f"🃏 <b>바카라</b> · {name}님 {BAC_LABEL[pick]}에 {fmt(bet)}"


def bac_frames(rd: BacRound, name: str, pick: str, bet: int) -> list[str]:
    """카드 뒤집는 중간 화면: 다 덮음 → 플레이어 2장 → (3번째 카드가 있으면) 뱅커 2장. 마지막은 render_baccarat."""
    head = _bac_head(name, pick, bet)
    frames = [f"{head}\n{_bac_side('🔵', '플레이어', rd.player, 0)}\n{_bac_side('🔴', '뱅커', rd.banker, 0)}\n🃏 카드를 나눠요…",
              f"{head}\n{_bac_side('🔵', '플레이어', rd.player, 2)}\n{_bac_side('🔴', '뱅커', rd.banker, 0)}\n🔴 뱅커 카드 뒤집는 중…"]
    if len(rd.player) > 2 or len(rd.banker) > 2:
        who = " · ".join(w for w, cs in (("플레이어", rd.player), ("뱅커", rd.banker)) if len(cs) > 2)
        frames.append(f"{head}\n{_bac_side('🔵', '플레이어', rd.player, 2)}\n{_bac_side('🔴', '뱅커', rd.banker, 2)}\n"
                      f"➕ {who} 3번째 카드 받는 중…")
    return frames


def render_baccarat(rd: BacRound, name: str, pick: str, bet: int, compact: bool = False) -> str:
    """결과 화면. compact = 카드 그림(GIF) 캡션용: 카드·막대 줄 없이 승자·점수만 (그림에 다 있음)."""
    head = {"player": "🔵 <b>플레이어 승</b>", "banker": "🔴 <b>뱅커 승</b>", "tie": "🟢 <b>타이</b>"}[rd.winner]
    head += f" (플 {rd.p} : {rd.b} 뱅)"
    tags = []
    if rd.natural:
        tags.append("내추럴")
    if rd.pair("player"):
        tags.append("플레이어 페어")
    if rd.pair("banker"):
        tags.append("뱅커 페어")
    sides = "" if compact else (f"{_bac_side('🔵', '플레이어', rd.player, len(rd.player))}\n"
                                f"{_bac_side('🔴', '뱅커', rd.banker, len(rd.banker))}\n")
    return (f"{_bac_head(name, pick, bet)}\n{sides}"
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
    await record(ctx.svc.db, ctx.chat_id, "baccarat",                # 🖼 그림장: P/B/T + 페어(p·b)
                 rd.winner[0].upper() + ("p" if rd.pair("player") else "") + ("b" if rd.pair("banker") else ""))
    payout = bac_payout(rd, pick, bet)
    bal = await settle(ctx, bet, payout, "baccarat")                 # 정산 먼저 → 연출은 보여주기만
    tail = result_line(bet, payout, bal) + await dealer_tail(ctx, bet, payout, bal)
    name = _name(ctx.user)
    final = render_baccarat(rd, name, pick, bet) + tail
    frames = bac_frames(rd, name, pick, bet)
    if not IMAGES:
        await show(ctx, frames, final)
        return
    pl, bk = list(rd.player), list(rd.banker)                        # 카드가 한 장씩 놓이는 GIF → 끝나면 캡션이 결과
    await show_anim(ctx, lambda: cardimg.baccarat_gif(pl, bk, rd.winner, rd.pair("player"), rd.pair("banker")),
                    cardimg.baccarat_seconds(pl, bk), f"{_bac_head(name, pick, bet)}\n🃏 카드를 나눠요…",
                    render_baccarat(rd, name, pick, bet, compact=True) + tail, frames, fallback_final=final)


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
    anim: object = None        # 버튼 응답(answer) 뒤에 돌릴 연출 (블랙잭 딜러 공개)
    photo: bool = False        # 카드 그림 사진 메시지 (수정 = 그림 교체 + 캡션). False 면 글자 메시지

    def touch(self) -> None:
        self.tok = secrets.token_urlsafe(6)   # 8글자, 누를 때마다 바뀜
        self.expires = time.monotonic() + HAND_TTL

    def data(self, act: str) -> str:
        return f"cs:{self.game}:{self.ctx.user.id}:{self.tok}:{act}"


HANDS: dict[tuple[str, int, int], Hand] = {}   # (게임, 방, 사람) → 진행 중인 판


def _key(game: str, chat_id: int, uid: int) -> tuple[str, int, int]:
    return (game, chat_id, uid)


OPEN_CHECKS.append(lambda cid, uid: any((h := HANDS.get(_key(g, cid, uid))) is not None and not h.done for g in ("bj", "hl")))


# 화면 = (글자 화면, 사진 캡션, 그림 그리는 함수) — 글자 화면은 카드 글자까지 다 있고, 캡션은 그림에 있는 건 뺀 짧은 글
View = tuple[str, str, object]


async def _render(img) -> bytes | None:
    try:
        return await asyncio.to_thread(img)            # 약 0.05초: 그리는 동안 다른 메시지 처리가 안 멈추게
    except Exception as e:                             # Pillow·폰트 문제 등 → 글자로
        log.warning("card image failed: %r", e)
        return None


async def _edit(h: Hand, view: View | str, kb=None, q=None, final: bool = False) -> bool:
    """오류는 삼킨다 (봇 종료 뒤 HTTP 닫힘 포함). final(결과 화면)은 429 면 기다렸다 한 번 더.
    사진 판: 그림 교체(edit_message_media) → 안 되면 캡션만 카드 글자 화면으로 (정보는 맞게)."""
    text, cap, img = (view, view, None) if isinstance(view, str) else view
    bot, cid, mid = h.ctx.bot, h.ctx.chat_id, h.message_id
    if not h.photo:
        return await edit_live(bot, cid, mid, text, final=final, kb=kb, q=q)
    if img is not None and (data := await _render(img)) is not None:
        if await edit_live(bot, cid, mid, cap, final=final, kb=kb, media=data):
            return True
    return await edit_live(bot, cid, mid, text, final=final, kb=kb, caption=True)


async def _send(ctx: Ctx, h: Hand, view: View, kb=None) -> None:
    """판 시작 화면: 카드 그림 사진(+캡션·버튼). 그리기·보내기가 안 되면 글자 답장."""
    text, cap, img = view
    if IMAGES and (data := await _render(img)) is not None:
        try:
            sent = await ctx.bot.send_photo(ctx.chat_id, photo=data, caption=cap, parse_mode="HTML", reply_markup=kb,
                                            reply_parameters=ReplyParameters(ctx.msg.message_id,
                                                                             allow_sending_without_reply=True))
            h.photo, h.message_id = True, sent.message_id
            return
        except Exception as e:
            log.warning("card photo send failed, text fallback: %r", e)
    sent = await ctx.reply(text, reply_markup=kb)
    h.message_id = getattr(sent, "message_id", None)


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
    toast, anim = None, None
    async with h.lock:
        if h.done or not secrets.compare_digest(h.tok, tok):   # 연타: 앞의 누름이 토큰을 바꿨음
            toast = "이미 처리됐어요."
        elif h.expires <= time.monotonic():
            await _expire(h, q)
            toast = "시간이 지나 자동으로 정리했어요."
        else:
            toast = await act_fn(h, act, q)
            anim, h.anim = h.anim, None       # 이 누름이 만든 연출만 (정산은 이미 끝남)
    await q.answer(toast)                     # 버튼 로딩 표시는 바로 끝내고
    if anim:
        await anim()                          # 딜러 카드 공개 연출


# ── 🃏 블랙잭 ─────────────────────────────────────────────
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
        return bet * 5 // 2, "🃏 <b>블랙잭!</b> (×2.5)"
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


def _bj_text(h: Hand, reveal: bool, footer: str = "", upto: int | None = None, compact: bool = False) -> str:
    """upto: 딜러 카드를 앞에서 몇 장까지 보일지 (공개 연출 중간 화면). compact: 사진 캡션 (카드는 그림에, 합계만)."""
    shown = h.dealer[:upto] if upto else h.dealer
    p, soft = bj_value(h.player)
    ptot = f"소프트 {p}" if soft and p < 21 and not reveal else str(p)
    doubled = " (더블)" if h.bet > h.stake else ""
    head = f"🃏 <b>블랙잭</b> · {_name(h.ctx.user)}님 · 베팅 {fmt(h.bet)}{doubled}\n"
    if compact:
        dealer = f"<b>{bj_total(shown)}</b>" if reveal else f"<b>{bj_total(h.dealer[:1])}</b> + {HIDDEN}"
        return head + f"🎩 딜러 {dealer} · 🙋 나 <b>{ptot}</b>" + (f"\n{footer}" if footer else "")
    dealer = (cards_str(shown) + f" (<b>{bj_total(shown)}</b>)") if reveal else f"{face(h.dealer[0])}{HIDDEN}"
    return (head + f"🎩 딜러 {dealer}\n"
            f"🙋 나 {cards_str(h.player)} (<b>{ptot}</b>)" + (f"\n{footer}" if footer else ""))


def _bj_banner(h: Hand, payout: int) -> tuple[str, int]:
    """그림 아래 결과 띠 (영문만) · 1 이김 0 비김 -1 짐."""
    if payout > h.bet:
        return ("BLACKJACK!" if is_blackjack(h.player) else
                "DEALER BUST - WIN" if bj_total(h.dealer) > 21 else "YOU WIN"), 1
    if payout == h.bet:
        return "PUSH", 0
    return ("BUST" if bj_total(h.player) > 21 else
            "DEALER BLACKJACK" if is_blackjack(h.dealer) else "DEALER WINS"), -1


def _bj_view(h: Hand, reveal: bool, footer: str = "", upto: int | None = None, result: tuple[str, int] | None = None) -> View:
    dealer, player = list(h.dealer[:upto] if upto else h.dealer), list(h.player)
    banner, win = result or ("", None)
    return (_bj_text(h, reveal, footer, upto), _bj_text(h, reveal, footer, upto, compact=True),
            lambda: cardimg.blackjack_png(dealer, player, not reveal, banner, win))


def _bj_kb(h: Hand) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("👊 히트", callback_data=h.data("h")),
           InlineKeyboardButton("✋ 스탠드", callback_data=h.data("s"))]
    if len(h.player) == 2:
        row.append(InlineKeyboardButton("💰 더블", callback_data=h.data("d")))
    return InlineKeyboardMarkup([row])


BJ_REVEAL_MAX = 2      # 딜러 공개 중간 화면 최대 수 (+ 결과 화면 1 = 버튼 한 번에 수정 3회 이하)


def bj_reveal_steps(n_dealer: int) -> list[int]:
    """공개 연출에서 보여줄 딜러 카드 장수: 숨긴 카드 뒤집기(2장) → … → 마지막 한 장 전. 최대 BJ_REVEAL_MAX 개."""
    steps = list(range(2, n_dealer))                  # 2장, 3장, … (마지막 장은 결과 화면)
    if not steps:
        return [2]                                    # 안 받고 멈춤: 뒤집기 → 결과
    return steps if len(steps) <= BJ_REVEAL_MAX else [steps[0], steps[-1]]


async def _bj_finish(h: Hand, q=None, note: str = "", animate: bool = False) -> None:
    """딜러 진행 → 정산 → 메시지. lock 안에서, 한 판에 한 번만.
    animate: 딜러가 카드를 까는 판이면 연출(h.anim)을 남기고 끝 — _callback 이 버튼 응답 뒤에 돌린다."""
    if h.done:
        return
    _finish(h)
    dealer_turn = bj_total(h.player) <= 21 and not is_blackjack(h.player)
    if dealer_turn:
        dealer_play(h.dealer, h.shoe)
    payout, verdict = bj_payout(h.player, h.dealer, h.bet)
    bal = await settle(h.ctx, h.bet, payout, "blackjack")            # 정산 먼저 (연출이 실패해도 돈은 끝)
    footer = (note + "\n" if note else "") + f"━━━━━━━━\n{verdict}\n" + result_line(h.bet, payout, bal) + await dealer_tail(h.ctx, h.bet, payout, bal)
    final = _bj_view(h, True, footer, result=_bj_banner(h, payout))
    if not (animate and dealer_turn):
        await _edit(h, final, None, q, final=True)
        return
    steps = [2] if h.photo else bj_reveal_steps(len(h.dealer))       # 사진 판: 숨긴 카드 뒤집기 1장 → 결과 (교체 2회)
    frames = [_bj_view(h, True, "🎩 딜러 카드 공개 중…" if k == 2 else "🎩 딜러가 한 장 더…", upto=k) for k in steps]

    async def reveal() -> None:
        for i, text in enumerate(frames):
            if i:
                await basic.sleep(basic.anim_gap())
            await _edit(h, text, None, q)
        await basic.sleep(basic.anim_gap())
        if not await _edit(h, final, None, q, final=True):
            try:                                                      # 결과만은 꼭: 새 메시지로
                await h.ctx.bot.send_message(h.ctx.chat_id, final[0], parse_mode="HTML")
            except (TelegramError, RuntimeError):
                pass
    h.anim = reveal


async def _bj_act(h: Hand, act: str, q) -> str | None:
    if act == "d":
        if len(h.player) != 2:
            return "더블은 처음 두 장일 때만 돼요."
        extra = h.bet
        if not await debit(h.ctx.svc.db, h.ctx.chat_id, h.ctx.user.id, extra, "bet:blackjack"):
            return f"잔액이 모자라서 더블을 못 해요. (더블엔 {fmt(extra)} 더 필요)"
        h.bet += extra
        h.player.append(h.shoe.draw())
        await _bj_finish(h, q, animate=True)
        return None
    if act == "s":
        await _bj_finish(h, q, animate=True)
        return None
    if act == "h":
        h.player.append(h.shoe.draw())
        if bj_total(h.player) >= 21:          # 버스트 또는 21 → 자동으로 끝
            await _bj_finish(h, q, animate=True)
            return None
        h.touch()
        await _edit(h, _bj_view(h, False, "히트·스탠드 중에 골라주세요."), _bj_kb(h), q)
        return None
    return "지난 버튼이에요."


async def g_blackjack(ctx: Ctx) -> None:
    await sweep()
    if not ctx.args:
        bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
        await ctx.reply("🃏 <b>블랙잭</b>: <code>!블랙잭 1000</code>\n"
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
        handoff(ctx, bet)
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
        await _send(ctx, h, _bj_view(h, True, f"━━━━━━━━\n{verdict}\n" + result_line(bet, payout, bal)
                                     + await dealer_tail(ctx, bet, payout, bal), result=_bj_banner(h, payout)))
        return
    h.touch()
    HANDS[_key("bj", ctx.chat_id, ctx.user.id)] = h
    handoff(ctx, bet)                              # 이제 판이 정산·시간 초과·종료 환불을 맡음
    await _send(ctx, h, _bj_view(h, False, "히트·스탠드·더블 중에 골라주세요. (2분 안에)"), _bj_kb(h))


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
    """확률 p 에 맞는 배수 (97% / p, 소수 둘째 자리에서 버림). 나올 카드가 없거나 1.01 미만(맞혀도 손해)이면 0."""
    m = int(HL_EDGE / p * 100) / 100 if p > 0 else 0.0
    return m if m >= 1.01 else 0.0


def _hl_mults(h: Hand) -> tuple[float, float]:
    ph, pl = hl_odds(h.card, h.shoe.remaining())
    return hl_mult(ph), hl_mult(pl)


def _hl_text(h: Hand, footer: str = "", compact: bool = False) -> str:
    """compact: 사진 캡션 (지난 카드 줄은 그림에)."""
    trail = "" if compact else cards_str(h.history[-6:])
    steps = "🟩" * h.step + "⬜" * (HL_MAX_STEPS - h.step)
    return (f"🔼🔽 <b>하이로우</b> · {_name(h.ctx.user)}님 · 베팅 {fmt(h.bet)}\n"
            + (f"지난 카드: {trail}\n" if trail else "")
            + f"지금 카드: <b>{face(h.card)}</b>\n"
            f"{steps} {h.step}/{HL_MAX_STEPS}단계\n"
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


def _hl_view(h: Hand, footer: str = "", result: tuple[str, int] | None = None) -> View:
    """result 가 없으면 진행 중 (그림에 HI/LO 배수), 있으면 끝 (결과 띠)."""
    card, hist, step = h.card, list(h.history), h.step
    mh, ml = (0.0, 0.0) if result else _hl_mults(h)
    banner, win = result or ("", None)
    return (_hl_text(h, footer), _hl_text(h, footer, compact=True),
            lambda: cardimg.hilo_png(card, hist, step, HL_MAX_STEPS, mh, ml, banner, win))


async def _hl_cashout(h: Hand, q=None, note: str = "") -> None:
    if h.done:
        return
    _finish(h)
    bal = await settle(h.ctx, h.bet, h.prize, "hilo")
    footer = (note + "\n" if note else "") + f"━━━━━━━━\n💰 {fmt(h.prize)} 받고 그만!\n" + result_line(h.bet, h.prize, bal) + await dealer_tail(h.ctx, h.bet, h.prize, bal)
    gain = h.prize - h.bet
    result = (f"CASH OUT +{gain:,}", 1) if gain > 0 else ("CASH OUT", 0)
    await _edit(h, _hl_view(h, footer, result), None, q, final=True)


async def _hl_act(h: Hand, act: str, q) -> str | None:
    if act == "c":
        await _hl_cashout(h, q)
        return None
    if act not in ("h", "l"):
        return "지난 버튼이에요."
    mh, ml = _hl_mults(h)
    mult = mh if act == "h" else ml
    if not mult:
        return "그쪽은 고를 수 없어요 (나올 카드가 없거나 맞혀도 손해)."
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
        await _edit(h, _hl_view(h, f"━━━━━━━━\n{face(prev)} → <b>{face(nxt)}</b> ({pick} 선택) · {why} 😭\n"
                                   + result_line(h.bet, 0, bal) + await dealer_tail(h.ctx, h.bet, 0, bal),
                                ("SAME - MISS" if nxt.rank == prev.rank else "MISS", -1)), None, q, final=True)
        return None
    h.prize = int(h.prize * mult)
    h.step += 1
    msg = f"{face(prev)} → <b>{face(nxt)}</b> {pick} 적중! ×{mult:g}"
    if h.step >= HL_MAX_STEPS:
        await _hl_cashout(h, q, note=msg + f"\n🏁 {HL_MAX_STEPS}단계 완주!")
        return None
    h.touch()
    await _edit(h, _hl_view(h, msg + "\n계속 갈까요, 그만할까요? (같은 숫자는 꽝)"), _hl_kb(h), q)
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
        handoff(ctx, bet)
        await ctx.reply("진행 중인 하이로우가 있어서 베팅을 돌려드렸어요.")
        return
    shoe = make_shoe(1)
    h = Hand("hl", Ctx(ctx.svc, ctx.bot, None, ctx.chat_id, ctx.user, ctx.role, []), bet, shoe,
             stake=bet, prize=bet)
    h.card = shoe.draw()
    h.touch()
    HANDS[_key("hl", ctx.chat_id, ctx.user.id)] = h
    handoff(ctx, bet)                              # 이제 판이 정산·시간 초과·종료 환불을 맡음
    await _send(ctx, h, _hl_view(h, "다음 카드는 더 높을까요, 낮을까요? (같은 숫자는 꽝 · 2분 안에)"), _hl_kb(h))


async def cb_hilo(svc, bot, q, parts: list[str]) -> None:
    await _callback("hl", svc, bot, q, parts, _hl_act)


register(("바카라", "baccarat", "바카"), g_baccarat, usage="금액 플|뱅|타이|플페어|뱅페어",
         help="🃏 플 ×2 · 뱅 ×1.95 · 타이 ×10 · 페어 ×13", group="카드")
register(("블랙잭", "blackjack", "bj"), g_blackjack, usage="금액", help="🃏 버튼으로 히트·스탠드·더블 · 블랙잭 ×2.5",
         group="카드")
register(("하이로우", "hilo", "하이로"), g_hilo, usage="금액", help="🔼🔽 높을까 낮을까, 맞힐수록 상금 ↑", group="카드")
register_callback("bj", cb_blackjack)
register_callback("hl", cb_hilo)


async def refund_open_hands(svc) -> int:
    """봇이 꺼질 때: 만료된 판은 평소처럼 정리(sweep), 남은 하이로우는 현재 상금 지급,
    블랙잭은 걸린 금액(더블 포함) 환불. 처리한 판 수 (h.done 이라 두 번 불러도 한 번만)."""
    n = await sweep()
    for h in list(HANDS.values()):
        async with h.lock:
            if h.done:
                continue
            if h.game == "hl":
                await _hl_cashout(h, note="🔌 봇 점검으로 자동으로 그만했어요.")
            else:
                _finish(h)
                await credit(svc.db, h.ctx.chat_id, h.ctx.user.id, h.bet, f"refund:{h.game}")
            n += 1
    return n


SHUTDOWN_HOOKS.append(refund_open_hands)
