"""간단한 게임: 텔레그램 주사위 애니메이션(🎲🎰🏀⚽🎯🎳)·룰렛·사다리.

주사위 계열은 텔레그램 서버가 결과를 정한다(send_dice) → 봇도 조작 불가, 모두가 같은 애니메이션을 본다.
애니메이션이 끝날 즈음(약 3.5초) 결과를 알린다. 룰렛·사다리는 secrets 난수.
배당은 기대 환급률 약 94~97% (오래 하면 조금씩 잃는 구조 → 채굴·출석이 계속 의미 있음).
"""
from __future__ import annotations

import asyncio

from ..util import esc
from . import Ctx, register
from .core import balance, fmt, result_line, rng, settle, split_bet, take_bet

DICE_WAIT = 3.5   # 애니메이션 끝날 때까지 (테스트에선 0)

SLOT_SYMBOLS = ["BAR", "🍇", "🍋", "7️⃣"]


def slot_reels(value: int) -> list[str]:
    """텔레그램 🎰 값(1~64) → 릴 3개. 64 = 7️⃣7️⃣7️⃣, 1 = BAR×3, 22 = 🍇×3, 43 = 🍋×3."""
    v = value - 1
    return [SLOT_SYMBOLS[(v >> s) & 3] for s in (0, 2, 4)]


async def _roll(ctx: Ctx, emoji: str) -> int:
    sent = await ctx.bot.send_dice(ctx.chat_id, emoji=emoji, reply_to_message_id=ctx.msg.message_id)
    await asyncio.sleep(DICE_WAIT)
    return sent.dice.value


async def _bet_or_help(ctx: Ctx, game: str, usage: str):
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    amount, rest = split_bet(ctx.args, bal)
    if amount is None and not ctx.args:
        await ctx.reply(usage + f"\n내 잔액: <b>{fmt(bal)}</b>")
        return None, rest
    return amount, rest


# ── 🎲 홀짝 ───────────────────────────────────────────────
async def g_oddeven(ctx: Ctx) -> None:
    usage = "🎲 <b>홀짝</b>: <code>!홀짝 1000 홀</code> (홀·짝 맞히면 ×1.95)"
    amount, rest = await _bet_or_help(ctx, "oddeven", usage)
    pick = next((a for a in rest if a in ("홀", "짝")), None)
    if amount is None and not ctx.args:
        return
    if pick is None:
        await ctx.reply(usage)
        return
    bet = await take_bet(ctx, amount, "oddeven")
    if bet is None:
        return
    v = await _roll(ctx, "🎲")
    got = "홀" if v % 2 else "짝"
    payout = int(bet * 1.95) if got == pick else 0
    bal = await settle(ctx, bet, payout, "oddeven")
    await ctx.reply(f"🎲 {v} → <b>{got}</b> ({esc(pick)} 선택)\n" + result_line(bet, payout, bal))


# ── 🎲 주사위 숫자 ────────────────────────────────────────
async def g_dice(ctx: Ctx) -> None:
    usage = "🎲 <b>주사위</b>: <code>!주사위 1000 6</code> (숫자 맞히면 ×5.7) · <code>!주사위 1000 높음</code>(4~6, ×1.95) · <code>낮음</code>(1~3)"
    amount, rest = await _bet_or_help(ctx, "dice", usage)
    if amount is None and not ctx.args:
        return
    pick = next((a for a in rest if a in ("1", "2", "3", "4", "5", "6", "높음", "낮음", "하이", "로우")), None)
    if pick is None:
        await ctx.reply(usage)
        return
    bet = await take_bet(ctx, amount, "dice")
    if bet is None:
        return
    v = await _roll(ctx, "🎲")
    if pick.isdecimal():
        payout = int(bet * 5.7) if v == int(pick) else 0
    else:
        high = pick in ("높음", "하이")
        payout = int(bet * 1.95) if (v >= 4) == high else 0
    bal = await settle(ctx, bet, payout, "dice")
    await ctx.reply(f"🎲 <b>{v}</b> ({esc(pick)} 선택)\n" + result_line(bet, payout, bal))


# ── 🎰 슬롯 ───────────────────────────────────────────────
SLOT_PAY = {"7️⃣": 30, "🍋": 10, "🍇": 10, "BAR": 10}   # 같은 그림 3개 (기대 환급률 약 94%)


async def g_slot(ctx: Ctx) -> None:
    usage = "🎰 <b>슬롯</b>: <code>!슬롯 1000</code> · 7️⃣7️⃣7️⃣ ×30 · 같은 그림 3개 ×10"
    amount, _ = await _bet_or_help(ctx, "slot", usage)
    if amount is None and not ctx.args:
        return
    bet = await take_bet(ctx, amount, "slot")
    if bet is None:
        return
    v = await _roll(ctx, "🎰")
    reels = slot_reels(v)
    mult = SLOT_PAY[reels[0]] if reels[0] == reels[1] == reels[2] else 0
    payout = bet * mult
    bal = await settle(ctx, bet, payout, "slot")
    head = "🎊 <b>잭팟!!! 777</b>\n" if mult == 30 else ""
    await ctx.reply(f"{head}🎰 {' | '.join(reels)}\n" + result_line(bet, payout, bal))


# ── 🏀⚽🎯🎳 스포츠 한 방 ─────────────────────────────────
SPORTS = {
    # 명령: (이모지, 성공 값, 배당, 설명)
    "농구": ("🏀", {4, 5}, 2.4, "골인하면 ×2.4"),
    "축구": ("⚽", {3, 4, 5}, 1.6, "골이면 ×1.6"),
    "다트": ("🎯", {6}, 5.7, "정중앙이면 ×5.7"),
    "볼링": ("🎳", {6}, 5.7, "스트라이크면 ×5.7"),
}


def _sport(name: str):
    emoji, wins, mult, desc = SPORTS[name]

    async def play(ctx: Ctx) -> None:
        usage = f"{emoji} <b>{name}</b>: <code>!{name} 1000</code> ({desc})"
        amount, _ = await _bet_or_help(ctx, name, usage)
        if amount is None and not ctx.args:
            return
        bet = await take_bet(ctx, amount, name)
        if bet is None:
            return
        v = await _roll(ctx, emoji)
        payout = int(bet * mult) if v in wins else 0
        bal = await settle(ctx, bet, payout, name)
        await ctx.reply(f"{emoji} {'성공!' if payout else '아깝다…'}\n" + result_line(bet, payout, bal))
    return play


# ── 🎡 룰렛 (0~36, 유럽식) ────────────────────────────────
REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_PICKS = {"빨강": "red", "레드": "red", "검정": "black", "블랙": "black", "홀": "odd", "짝": "even",
                  "하이": "high", "높음": "high", "로우": "low", "낮음": "low"}


def roulette_win(n: int, pick: str) -> int:
    """배당 (원금 포함). 0 은 숫자 0 에 건 사람만."""
    if pick.isdecimal():
        return 36 if int(pick) == n else 0
    if n == 0:
        return 0
    kind = ROULETTE_PICKS[pick]
    ok = {"red": n in REDS, "black": n not in REDS, "odd": n % 2 == 1, "even": n % 2 == 0,
          "high": n >= 19, "low": n <= 18}[kind]
    return 2 if ok else 0


async def g_roulette(ctx: Ctx) -> None:
    usage = ("🎡 <b>룰렛</b>: <code>!룰렛 1000 빨강</code>\n"
             "빨강·검정·홀·짝·하이(19~36)·로우(1~18) ×2 · 숫자(0~36) ×36")
    amount, rest = await _bet_or_help(ctx, "roulette", usage)
    if amount is None and not ctx.args:
        return
    pick = next((a for a in rest if a in ROULETTE_PICKS or (a.isdecimal() and 0 <= int(a) <= 36)), None)
    if pick is None:
        await ctx.reply(usage)
        return
    bet = await take_bet(ctx, amount, "roulette")
    if bet is None:
        return
    n = rng(37)
    color = "🟢" if n == 0 else ("🔴" if n in REDS else "⚫")
    payout = bet * roulette_win(n, pick)
    bal = await settle(ctx, bet, payout, "roulette")
    await ctx.reply(f"🎡 빙글빙글… {color} <b>{n}</b> ({esc(pick)} 선택)\n" + result_line(bet, payout, bal))


# ── 🪜 사다리 ─────────────────────────────────────────────
# 출발(좌/우) × 줄 수(3/4) 로 도착(홀/짝)이 정해짐: 좌3→짝, 좌4→홀, 우3→홀, 우4→짝 (4가지 25%씩)
LADDER = [("좌", 3, "짝"), ("좌", 4, "홀"), ("우", 3, "홀"), ("우", 4, "짝")]
LADDER_ART = {3: "├─┤\n├─┤\n├─┤", 4: "├─┤\n├─┤\n├─┤\n├─┤"}


def ladder_payout(result: tuple[str, int, str], pick: str) -> float:
    start, lines, end = result
    single = {"좌": start == "좌", "우": start == "우", "3줄": lines == 3, "4줄": lines == 4,
              "홀": end == "홀", "짝": end == "짝"}
    if pick in single:
        return 1.95 if single[pick] else 0
    combo = f"{start}{lines}{end}"
    return 3.8 if pick == combo else 0


LADDER_PICKS = {"좌", "우", "3줄", "4줄", "홀", "짝", "좌3짝", "좌4홀", "우3홀", "우4짝"}


async def g_ladder(ctx: Ctx) -> None:
    usage = ("🪜 <b>사다리</b>: <code>!사다리 1000 좌</code>\n"
             "좌·우 / 3줄·4줄 / 홀·짝 ×1.95 · 조합(좌3짝·좌4홀·우3홀·우4짝) ×3.8")
    amount, rest = await _bet_or_help(ctx, "ladder", usage)
    if amount is None and not ctx.args:
        return
    pick = next((a for a in rest if a in LADDER_PICKS), None)
    if pick is None:
        await ctx.reply(usage)
        return
    bet = await take_bet(ctx, amount, "ladder")
    if bet is None:
        return
    result = LADDER[rng(4)]
    payout = int(bet * ladder_payout(result, pick))
    bal = await settle(ctx, bet, payout, "ladder")
    start, lines, end = result
    await ctx.reply(f"🪜 출발 <b>{start}</b>\n<code>{LADDER_ART[lines]}</code>\n{lines}줄 → 도착 <b>{end}</b>  "
                    f"(<b>{start}{lines}{end}</b>, {esc(pick)} 선택)\n" + result_line(bet, payout, bal))


register(("홀짝", "oddeven"), g_oddeven, usage="금액 홀|짝", help="🎲 ×1.95", group="주사위·슬롯")
register(("주사위", "dice"), g_dice, usage="금액 숫자|높음|낮음", help="🎲 숫자 ×5.7 · 높낮이 ×1.95", group="주사위·슬롯")
register(("슬롯", "slot", "슬롯머신"), g_slot, usage="금액", help="🎰 777 ×30 · 3개 ×10", group="주사위·슬롯")
for _name, (_e, _w, _m, _d) in SPORTS.items():
    register((_name,), _sport(_name), usage="금액", help=f"{_e} {_d}", group="한 방 게임")
register(("룰렛", "roulette"), g_roulette, usage="금액 빨강|검정|홀|짝|숫자", help="🎡 ×2 · 숫자 ×36", group="카지노")
register(("사다리", "ladder"), g_ladder, usage="금액 좌|우|3줄|홀|좌3짝…", help="🪜 ×1.95 · 조합 ×3.8", group="카지노")
