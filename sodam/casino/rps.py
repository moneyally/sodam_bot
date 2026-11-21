"""✊✋✌ 가위바위보: 이기면 ×1.95 · 비기면 돌려받음 · 지면 꽝. 방 난이도(tilt)가 지는 확률을 정한다.

전목사님이 보낸 독립 가위바위보 봇(자체 지갑·무료충전·보상)을 소담 casino 틀에 맞춰 한 게임으로 녹였다.
지갑·무료충전(!파산·!출석)·이벤트(!이벤트)·잔액 유지는 소담이 이미 다 갖고 있어서, 여기선 '한 판'만 담당한다.

- 봇 손은 tilt 로 뽑는다(룰렛·사다리와 같은 방식): 공정 방이면 승·무·패 1/3씩, 난이도 80 방이면 약 80% 패.
  이길 확률을 '올리는' 일은 절대 없다(공정보다 유리하게는 안 됨).
- 배당은 홀짝(×1.95)과 같은 틀 — 무승부는 환불이라 공정 방 환급률 ≈ 98%(채굴·출석이 계속 의미 있음).
- 금액만 치면 가위·바위·보 버튼판(룰렛 베팅판과 같은 구조, 판 주인만 누름).
"""
from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..util import esc, user_name
from . import Ctx, anim, register, register_callback
from .basic import show_anim
from .board import record
from .core import balance, fmt, guarded, rng, settle_text, split_bet, take_bet, tilt

WIN_MULT = 1.95                                  # 이기면 원금 포함 ×1.95 (홀짝과 같은 틀)
SHAPES = ("rock", "scissors", "paper")           # tilt 가 이 중에서 봇 손을 뽑음
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}   # 키가 값을 이김
KO = {"rock": "바위 ✊", "scissors": "가위 ✌️", "paper": "보 ✋"}

# 손 낱말 → 속값 (별칭 넉넉히: 묵찌빠·주먹·보자기·영문)
PICKS = {
    "바위": "rock", "바구": "rock", "주먹": "rock", "묵": "rock", "락": "rock", "rock": "rock",
    "가위": "scissors", "가새": "scissors", "찌": "scissors", "scissors": "scissors",
    "보": "paper", "보자기": "paper", "빠": "paper", "paper": "paper",
}
CHIPS = (1000, 5000, 10000, 50000)


def result(mine: str, bot: str) -> str:
    """mine(나) vs bot(봇) → 'win'|'draw'|'lose'."""
    if mine == bot:
        return "draw"
    return "win" if BEATS[mine] == bot else "lose"


def payout_of(bet: int, mine: str, bot: str) -> int:
    """원금 포함 받을 돈: 승 ×1.95 · 무 환불(원금) · 패 0."""
    r = result(mine, bot)
    if r == "win":
        return int(bet * WIN_MULT)
    if r == "draw":
        return bet
    return 0


def _chip(n: int) -> str:
    return f"{n // 10000}만" if n >= 10000 and n % 10000 == 0 else f"{n // 1000}천" if n % 1000 == 0 else f"{n:,}"


def board_text(user, amount: int) -> str:
    return (f"✊✋✌ <b>가위바위보</b> · {esc(user_name(user))}님\n"
            f"금액 칩을 고르고 <b>가위·바위·보</b>를 누르면 바로 승부! (지금 <b>{fmt(amount)}</b>)\n"
            f"이기면 ×{WIN_MULT:g} · 비기면 돌려받음 · 글로도 가능: <code>!가위바위보 1000 바위</code>")


def board_kb(uid: int, amount: int) -> InlineKeyboardMarkup:
    chips = [InlineKeyboardButton(("✅" if c == amount else "") + _chip(c), callback_data=f"cs:rp:{uid}:{c}:chip")
             for c in CHIPS]
    if amount not in CHIPS:
        chips.append(InlineKeyboardButton("✅" + _chip(amount), callback_data=f"cs:rp:{uid}:{amount}:chip"))
    hands = [InlineKeyboardButton(KO[s], callback_data=f"cs:rp:{uid}:{amount}:{s}") for s in SHAPES]
    return InlineKeyboardMarkup([chips, hands])


async def g_rps(ctx: Ctx) -> None:
    usage = ("✊✋✌ <b>가위바위보</b>: <code>!가위바위보 1000 바위</code> (이기면 ×1.95 · 비기면 환불)\n"
             "금액만 치면 버튼으로 낼 수 있어요: <code>!가위바위보 1000</code>")
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    amount, rest = split_bet(ctx.args, bal)
    pick = next((PICKS[a] for a in rest if a in PICKS), None)
    if amount is None and not ctx.args:
        await ctx.reply(usage + f"\n내 잔액: <b>{fmt(bal)}</b>")
        return
    if pick is None:                                 # 손을 안 적으면 버튼 베팅판
        await ctx.reply(board_text(ctx.user, amount or CHIPS[0]),
                        reply_markup=board_kb(ctx.user.id, amount or CHIPS[0]))
        return
    await play_rps(ctx, amount, pick)


async def play_rps(ctx: Ctx, amount: int | None, pick: str) -> None:
    bet = await take_bet(ctx, amount, "rps")
    if bet is None:
        return
    # 봇 손을 tilt 로: lost = 유저가 지는 손(= 봇이 유저 pick 을 이김). 무승부(환불)는 지는 게 아님.
    bot = await tilt(ctx.svc.db, lambda: SHAPES[rng(3)], lambda b: payout_of(1, pick, b) == 0, ctx.chat_id)
    await record(ctx.svc.db, ctx.chat_id, "rps", f"{pick[:1]}{bot[:1]}")   # 🖼 그림장 (원본 손)
    payout = payout_of(bet, pick, bot)
    res = result(pick, bot)
    _, tail = await settle_text(ctx, "rps", bet, payout)         # 정산 먼저 → 연출은 보여주기만
    head = f"✊✋✌ <b>가위바위보</b> · {esc(user_name(ctx.user))}님 {fmt(bet)}"
    verdict = {"win": "🎉 이겼다! ✅", "draw": "🤝 비겼어요 (환불)", "lose": "😢 졌어요 ❌"}[res]
    final = f"{head}\n나 <b>{KO[pick]}</b>  vs  봇 <b>{KO[bot]}</b>\n{verdict}\n{tail}"
    await show_anim(ctx, lambda: anim.rps(pick, bot), anim.seconds(anim.rps_frame_count()),
                    f"{head}\n✊ 가위… 바위… 보! ✌️", final,
                    [f"{head}\n✊ 안 내면 진다… 가위바위…", f"나 <b>{KO[pick]}</b> vs 봇 <b>{KO[bot]}</b> · {verdict}"])


async def cb_rps(svc, bot, q, parts: list[str]) -> None:
    """버튼 베팅판: 칩 고르기(chip) 또는 손 내기(rock/scissors/paper). 판 주인만."""
    uid, amount, pick = (parts + ["", "", ""])[:3]
    if not (uid.lstrip("-").isdecimal() and amount.isdecimal()) or q.from_user.id != int(uid):
        await q.answer("본인 베팅판만 누를 수 있어요. !가위바위보 로 내 판을 여세요 ✊", show_alert=True)
        return
    amount = int(amount)
    if pick == "chip":                               # 칩만 바꿈: 판 다시 그림
        await q.answer(f"{fmt(amount)} 선택")
        from .basic import edit_live
        await edit_live(bot, q.message.chat_id, q.message.message_id, board_text(q.from_user, amount),
                        kb=board_kb(int(uid), amount))
        return
    if pick not in SHAPES:
        await q.answer("지난 버튼이에요.")
        return
    from . import _INDEX
    from .core import gate
    ctx = Ctx(svc, bot, q.message, q.message.chat_id, q.from_user, None, [])
    blocked = await gate(ctx, _INDEX["가위바위보"])   # !명령과 같은 입장 검사 (가입·꺼진 방·이용 기간)
    if blocked:
        await q.answer(re.sub(r"<[^>]+>", "", blocked)[:190], show_alert=True)
        return
    await q.answer(f"{KO[pick]} · {fmt(amount)}!")
    await guarded(ctx, lambda c: play_rps(c, amount, pick))


register(("가위바위보", "가바보", "묵찌빠", "rps"), g_rps, usage="금액 가위|바위|보",
         help="✊✋✌ 이기면 ×1.95 · 비기면 환불 · 금액만 치면 버튼", group="한 방 게임")
register_callback("rp", cb_rps)
