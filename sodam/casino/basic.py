"""간단한 게임: 텔레그램 주사위 애니메이션(🎲🎰🏀⚽🎯🎳)·룰렛·사다리.

주사위 계열은 텔레그램 서버가 결과를 정한다(send_dice) → 봇도 조작 불가, 모두가 같은 애니메이션을 본다.
애니메이션이 끝날 즈음(약 3.5초) 결과를 알린다. 룰렛·사다리는 secrets 난수.
배당은 기대 환급률 약 94~97% (오래 하면 조금씩 잃는 구조 → 채굴·출석이 계속 의미 있음).

연출(룰렛·사다리, cards.py 의 바카라·블랙잭도 같이 씀): 결과를 정하고 **정산을 먼저 끝낸 뒤**
메시지 하나를 보내 anim_gap() 간격으로 2~3번 고친다. 중간 화면 수정이 실패해도 건너뛰고,
마지막 화면은 제한(429)이면 기다렸다 한 번 더, 그래도 안 되면 새 메시지로 결과를 보낸다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest, RetryAfter, TelegramError

from ..util import esc, user_name
from . import Ctx, anim, register, register_callback
from .anim import REDS, WHEEL
from .board import record
from .core import balance, credit, finish, fmt, rng, settle_text, split_bet, take_bet

log = logging.getLogger(__name__)

DICE_WAIT = 3.5   # 애니메이션 끝날 때까지 (테스트에선 0)
sleep = asyncio.sleep   # 테스트에서 바꿔 끼움 (기다리지 않게)
RETRY_MAX = 30          # 마지막 화면이 429 면 최대 이만큼 기다렸다 다시


def anim_gap() -> float:
    """연출 화면 사이 간격 (≈1.2초): 한 판 연출이 주사위 애니메이션 길이(DICE_WAIT)쯤에 끝나게.
    그룹 한도(보내기+수정 분당 약 20개) 안에서 한 판 = 보내기 1 + 수정 2~3."""
    return DICE_WAIT / 3


def _secs(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if isinstance(ra, timedelta) else float(ra)


async def edit_live(bot, chat_id, message_id, text: str, *, final: bool = False, kb=None, q=None,
                    caption: bool = False) -> bool:
    """연출 화면 수정 (q 가 있으면 버튼 콜백 메시지, caption 이면 애니메이션의 캡션). 고쳤으면(또는 이미 같으면) True.
    중간 화면은 실패하면 그냥 건너뛰고, final 은 429 면 기다렸다 한 번 더."""
    for attempt in (0, 1):
        try:
            if caption:
                await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, parse_mode="HTML")
            elif q is not None:
                await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            elif message_id is not None:
                await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                            parse_mode="HTML", reply_markup=kb)
            else:
                return False
            return True
        except RetryAfter as e:
            if not final or attempt:
                return False
            await sleep(min(_secs(e), RETRY_MAX))
        except BadRequest as e:
            return "not modified" in str(e).lower()
        except Exception as e:   # 네트워크·봇 종료 뒤(RuntimeError) 등: 연출은 보여주기만이라 게임은 계속
            log.debug("live edit failed: %r", e)
            return False
    return False


async def show_anim(ctx: Ctx, make_gif, moving_s: float, spin: str, final: str, fallback: list[str]) -> None:
    """결과 애니메이션(GIF) 한 번 + 끝나면 캡션을 결과로. 그리기·보내기가 안 되면 글자 연출(show)로."""
    try:
        gif = await asyncio.to_thread(make_gif)          # 약 1초: 그리는 동안 다른 메시지 처리가 멈추지 않게
        sent = await ctx.bot.send_animation(ctx.chat_id, animation=gif, caption=spin, parse_mode="HTML",
                                            reply_parameters=ReplyParameters(ctx.msg.message_id,
                                                                             allow_sending_without_reply=True))
    except Exception as e:                              # Pillow 없음·그리기 실패·텔레그램 오류
        log.warning("animation failed, text fallback: %r", e)
        await show(ctx, fallback, final)
        return
    await sleep(moving_s)
    if not await edit_live(ctx.bot, ctx.chat_id, sent.message_id, final, final=True, caption=True):
        try:                                            # 결과만은 꼭: 새 메시지로
            await ctx.reply(final)
        except TelegramError as e:
            log.warning("anim final failed: %s", e)


async def show(ctx: Ctx, frames: list[str], final: str) -> None:
    """frames[0] 을 답장으로 보내고 anim_gap() 마다 frames[1:] → final 로 고친다. 정산은 부르기 전에 끝낼 것."""
    try:
        sent = await ctx.reply(frames[0] if frames else final)
    except TelegramError as e:
        log.warning("anim send failed: %s", e)
        sent = None
        frames = []
    if not frames:
        if sent is None:
            try:
                await ctx.reply(final)
            except TelegramError:
                pass
        return
    mid = getattr(sent, "message_id", None)
    for text in frames[1:]:
        await sleep(anim_gap())
        await edit_live(ctx.bot, ctx.chat_id, mid, text)
    await sleep(anim_gap())
    if not await edit_live(ctx.bot, ctx.chat_id, mid, final, final=True):
        try:                                  # 마지막 화면만은 꼭: 새 메시지로
            await ctx.reply(final)
        except TelegramError as e:
            log.warning("anim final failed: %s", e)

SLOT_SYMBOLS = ["BAR", "🍇", "🍋", "7️⃣"]


def slot_reels(value: int) -> list[str]:
    """텔레그램 🎰 값(1~64) → 릴 3개. 64 = 7️⃣7️⃣7️⃣, 1 = BAR×3, 22 = 🍇×3, 43 = 🍋×3."""
    v = value - 1
    return [SLOT_SYMBOLS[(v >> s) & 3] for s in (0, 2, 4)]


async def _roll(ctx: Ctx, emoji: str, game: str, bet: int) -> int | None:
    """주사위를 굴려 값. 텔레그램 오류면 베팅을 돌려주고 None (원본 메시지가 지워져도 답장 없이 보냄)."""
    try:
        sent = await ctx.bot.send_dice(ctx.chat_id, emoji=emoji, reply_parameters=ReplyParameters(
            ctx.msg.message_id, allow_sending_without_reply=True))
    except TelegramError as e:
        log.warning("send_dice failed (%s): %s", game, e)
        bal = await credit(ctx.svc.db, ctx.chat_id, ctx.user.id, bet, f"refund:{game}")
        await ctx.bot.send_message(ctx.chat_id, f"{emoji} 주사위를 못 굴려서 베팅 {fmt(bet)}을 돌려드렸어요. "
                                                f"잔액 {fmt(bal)}", parse_mode="HTML")
        return None
    await sleep(DICE_WAIT)
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
    v = await _roll(ctx, "🎲", "oddeven", bet)
    if v is None:
        return
    got = "홀" if v % 2 else "짝"
    await record(ctx.svc.db, ctx.chat_id, "oddeven", str(v))        # 🖼 그림장
    payout = int(bet * 1.95) if got == pick else 0
    await finish(ctx, "oddeven", bet, payout, f"🎲 {v} → <b>{got}</b> ({esc(pick)} 선택)")


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
    v = await _roll(ctx, "🎲", "dice", bet)
    if v is None:
        return
    if pick.isdecimal():
        payout = int(bet * 5.7) if v == int(pick) else 0
    else:
        high = pick in ("높음", "하이")
        payout = int(bet * 1.95) if (v >= 4) == high else 0
    await finish(ctx, "dice", bet, payout, f"🎲 <b>{v}</b> ({esc(pick)} 선택)")


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
    v = await _roll(ctx, "🎰", "slot", bet)
    if v is None:
        return
    reels = slot_reels(v)
    mult = SLOT_PAY[reels[0]] if reels[0] == reels[1] == reels[2] else 0
    payout = bet * mult
    head = "🎊 <b>잭팟!!! 777</b>\n" if mult == 30 else ""
    await finish(ctx, "slot", bet, payout, f"{head}🎰 {' | '.join(reels)}")


# ── 🏀⚽🎯🎳 스포츠 한 방 ─────────────────────────────────
SPORTS = {
    # 명령: (이모지, 성공 값, 배당, 설명)
    "농구": ("🏀", {4, 5}, 2.4, "골인하면 ×2.4"),
    "축구": ("⚽", {4, 5}, 2.4, "골이면 ×2.4"),   # 1~3 은 노골 (PTB 문서: 4·5 골)
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
        v = await _roll(ctx, emoji, name, bet)
        if v is None:
            return
        payout = int(bet * mult) if v in wins else 0
        await finish(ctx, name, bet, payout, f"{emoji} {'성공!' if payout else '아깝다…'}")
    return play


# ── 🎡 룰렛 (0~36, 유럽식) ────────────────────────────────
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
    amount, rest = split_bet(ctx.args, await balance(ctx.svc.db, ctx.chat_id, ctx.user.id))
    pick = next((a for a in rest if a in ROULETTE_PICKS or (a.isdecimal() and 0 <= int(a) <= 36)), None)
    if pick is None:                                   # 칸을 안 적으면 버튼 베팅판
        await ctx.reply(board_text(ctx.user, amount or CHIPS[0]), reply_markup=board_kb(ctx.user.id, amount or CHIPS[0]))
        return
    await spin_roulette(ctx, amount, pick)


async def spin_roulette(ctx: Ctx, amount: int | None, pick: str) -> None:
    bet = await take_bet(ctx, amount, "roulette")
    if bet is None:
        return
    n = rng(37)
    await record(ctx.svc.db, ctx.chat_id, "roulette", str(n))
    mult = roulette_win(n, pick)
    payout = bet * mult
    _, tail = await settle_text(ctx, "roulette", bet, payout)       # 정산 먼저 → 연출은 보여주기만
    head = f"🎡 <b>룰렛</b> · {esc(user_name(ctx.user))}님 {fmt(bet)} ({esc(pick)} 선택)"
    frames, final = roulette_frames(n, head, mult)
    await show_anim(ctx, lambda: anim.roulette(n), anim.seconds(anim.ROULETTE_FRAMES),
                    f"{head}\n🌀 휠이 돌아요…", final + "\n" + tail, frames)


# ── 🎡 룰렛 베팅판 (버튼): 금액 칩을 고르고 칸을 누르면 바로 돈다. 판 주인만 누를 수 있음 ──
CHIPS = (1000, 5000, 10000, 50000)


def _chip(n: int) -> str:
    return f"{n // 10000}만" if n >= 10000 and n % 10000 == 0 else f"{n // 1000}천" if n % 1000 == 0 else f"{n:,}"


def board_text(user, amount: int) -> str:
    return (f"🎡 <b>룰렛 베팅판</b> · {esc(user_name(user))}님\n"
            f"금액 칩을 고르고 칸을 누르면 바로 돌아요 (지금 <b>{fmt(amount)}</b>)\n"
            "색·홀짝·하이로우 ×2 · 숫자 ×36 · 글로도 가능: <code>!룰렛 1000 빨강</code>")


def board_kb(uid: int, amount: int) -> InlineKeyboardMarkup:
    def b(label: str, pick: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"cs:rb:{uid}:{amount}:{pick}")
    chips = [InlineKeyboardButton(("✅" if c == amount else "") + _chip(c), callback_data=f"cs:rb:{uid}:{c}:c")
             for c in CHIPS]
    if amount not in CHIPS:
        chips.append(InlineKeyboardButton("✅" + _chip(amount), callback_data=f"cs:rb:{uid}:{amount}:c"))
    outside = [b("🔴빨강", "빨강"), b("⚫검정", "검정"), b("홀", "홀"), b("짝", "짝"), b("1~18", "로우"), b("19~36", "하이")]
    nums = [[b(pocket(0), "0")]] + [[b(pocket(n), str(n)) for n in range(r, r + 6)] for r in range(1, 37, 6)]
    return InlineKeyboardMarkup([chips, outside, *nums])


async def cb_board(svc, bot, q, parts: list[str]) -> None:
    uid, amount, pick = (parts + ["", "", ""])[:3]
    if not (uid.lstrip("-").isdecimal() and amount.isdecimal()) or q.from_user.id != int(uid):
        await q.answer("본인 베팅판만 누를 수 있어요. !룰렛 으로 내 판을 여세요 🎡", show_alert=True)
        return
    amount = int(amount)
    if pick == "c":                                    # 칩 고르기: 판만 다시 그림
        await q.answer(f"{fmt(amount)} 선택")
        await edit_live(bot, q.message.chat_id, q.message.message_id, board_text(q.from_user, amount),
                        kb=board_kb(int(uid), amount))
        return
    if not (pick in ROULETTE_PICKS or (pick.isdecimal() and 0 <= int(pick) <= 36)):
        await q.answer("지난 버튼이에요.")
        return
    from . import _INDEX
    from .core import gate
    ctx = Ctx(svc, bot, q.message, q.message.chat_id, q.from_user, None, [])
    blocked = await gate(ctx, _INDEX["룰렛"])        # !명령과 같은 입장 검사 (가입·꺼진 방·이용 기간)
    if blocked:
        await q.answer(re.sub(r"<[^>]+>", "", blocked)[:190], show_alert=True)
        return
    await q.answer(f"🎡 {pick} · {fmt(amount)}!")
    await spin_roulette(ctx, amount, pick)


# 유럽식 휠의 실제 칸 순서 (0 에서 시계 방향)
SPIN = ["🌀 휠이 돌아요… 💨💨💨", "🌀 느려져요… 💨💨", "🌀 거의 멈춰요… 💨"]


def pocket(n: int) -> str:
    return ("🟢" if n == 0 else "🔴" if n in REDS else "⚫") + str(n)


def wheel_strip(idx: int, half: int = 2) -> str:
    """휠에서 idx 칸을 가운데(【】)에 둔 5칸 띠."""
    cells = [pocket(WHEEL[(idx + d) % len(WHEEL)]) for d in range(-half, half + 1)]
    cells[half] = f"<b>【{cells[half]}】</b>"
    return " ".join(cells)


def roulette_frames(n: int, head: str, mult: int) -> tuple[list[str], str]:
    """(돌아가는 화면 3개, 멈춘 화면). 띠가 점점 적게 흘러가다 결과 칸에서 멈춘다 (흘러가는 폭은 꾸밈용 난수)."""
    end = WHEEL.index(n)
    back = [19 + secrets.randbelow(9), 6 + secrets.randbelow(4), 1 + secrets.randbelow(2)]
    frames = [f"{head}\n{wheel_strip(end - b)}\n{SPIN[i]}" for i, b in enumerate(back)]
    props = "초록" if n == 0 else " · ".join(("빨강" if n in REDS else "검정", "홀" if n % 2 else "짝",
                                            "하이" if n >= 19 else "로우"))
    hit = f"✅ 적중! ×{mult}" if mult else "❌ 빗나감"
    return frames, f"{head}\n{wheel_strip(end)}\n🎯 멈춤! <b>{pocket(n)}</b> ({props}) · {hit}"


# ── 🪜 사다리 ─────────────────────────────────────────────
# 출발(좌/우) × 줄 수(3/4) 로 도착(홀/짝)이 정해짐: 좌3→짝, 좌4→홀, 우3→홀, 우4→짝 (4가지 25%씩)
# 그림: 왼쪽 기둥 아래 = 홀, 오른쪽 = 짝. 가로줄을 만날 때마다 반대 기둥으로 건너감 (지나간 길은 굵은 선)
LADDER = [("좌", 3, "짝"), ("좌", 4, "홀"), ("우", 3, "홀"), ("우", 4, "짝")]
RUNG_W = 7
CURTAIN = "│" + "░" * RUNG_W + "│"


def ladder_art(start: str, lines: int, shown: int | None = None) -> str:
    """사다리 그림 (<pre> 안에 넣음). shown = 드러난 가로줄 수 (None=전부). 가려진 부분은 줄 수가 안 새게 같은 높이 ░."""
    shown = lines if shown is None else shown
    side = 0 if start == "좌" else 1

    def post(s: int) -> str:
        return ("┃" if s == 0 else "│") + " " * RUNG_W + ("┃" if s == 1 else "│")
    rows = ["좌" + " " * (RUNG_W - 1) + "우", post(side)]
    for _ in range(shown):
        rows.append(("┗" + "━" * RUNG_W + "┓") if side == 0 else ("┏" + "━" * RUNG_W + "┛"))
        side = 1 - side
        rows.append(post(side))
    if shown < lines:
        rows += [CURTAIN] * (7 if shown == 0 else 3)
    rows.append("홀" + " " * (RUNG_W - 1) + "짝")
    return "\n".join(rows)


def ladder_frames(result: tuple[str, int, str], head: str, won: bool) -> tuple[list[str], str]:
    """(출발만 보이는 화면, 절반 내려간 화면), 도착 화면."""
    start, lines, end = result
    f0 = f"{head}\n출발 <b>{start}</b> 🔵\n<pre>{ladder_art(start, lines, 0)}</pre>\n도착 ❔ 사다리 타는 중…"
    f1 = f"{head}\n출발 <b>{start}</b> 🔵\n<pre>{ladder_art(start, lines, 2)}</pre>\n도착 ❔ 내려가는 중… ⬇️"
    hit = "✅ 적중!" if won else "❌ 빗나감"
    final = (f"{head}\n출발 <b>{start}</b> 🔵\n<pre>{ladder_art(start, lines)}</pre>\n"
             f"{lines}줄 → 도착 <b>{end}</b> 🏁 (<b>{start}{lines}{end}</b>) · {hit}")
    return [f0, f1], final


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
    await record(ctx.svc.db, ctx.chat_id, "ladder", "".join(map(str, result)))
    payout = int(bet * ladder_payout(result, pick))
    _, tail = await settle_text(ctx, "ladder", bet, payout)         # 정산 먼저 → 연출은 보여주기만
    head = f"🪜 <b>사다리</b> · {esc(user_name(ctx.user))}님 {fmt(bet)} ({esc(pick)} 선택)"
    frames, final = ladder_frames(result, head, payout > 0)
    await show_anim(ctx, lambda: anim.ladder(result[0], result[1]), anim.seconds(anim.LADDER_STEPS + 1),
                    f"{head}\n출발 <b>{result[0]}</b> 🔵 사다리 타는 중… (아래 O=홀 · E=짝)", final + "\n" + tail, frames)


register(("홀짝", "oddeven"), g_oddeven, usage="금액 홀|짝", help="🎲 ×1.95", group="주사위·슬롯")
register(("주사위", "dice"), g_dice, usage="금액 숫자|높음|낮음", help="🎲 숫자 ×5.7 · 높낮이 ×1.95", group="주사위·슬롯")
register(("슬롯", "slot", "슬롯머신"), g_slot, usage="금액", help="🎰 777 ×30 · 3개 ×10", group="주사위·슬롯")
for _name, (_e, _w, _m, _d) in SPORTS.items():
    register((_name,), _sport(_name), usage="금액", help=f"{_e} {_d}", group="한 방 게임")
register_callback("rb", cb_board)
register(("룰렛", "roulette"), g_roulette, usage="금액 빨강|검정|홀|짝|숫자", help="🎡 ×2 · 숫자 ×36 · 금액만 치면 버튼 베팅판", group="카지노")
register(("사다리", "ladder"), g_ladder, usage="금액 좌|우|3줄|홀|좌3짝…", help="🪜 ×1.95 · 조합 ×3.8", group="카지노")
