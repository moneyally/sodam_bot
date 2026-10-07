"""🎴 바카라 회차판: 방 사람들이 한 판에 같이 건다 (`!회차 1000 플`). 혼자 하는 `!바카라` 는 그대로 (casino/cards.py).

- 첫 베팅이 판을 연다 → 덮인 카드 사진(N회차) + 30초 동안 베팅 → 마감 → **사진 한 장을 고쳐 가며** 공개
  (플레이어 2장 → 뱅커 2장 → 3번째 카드 → 결과 배너) → 당첨·꽝 목록 한 번. 한 판 = 메시지 2개 + 참가 확인(출발 때 지움).
  (오너 결정 2026-10-06: 다른 봇은 단계마다 새 사진 7~8개 — 그룹 분당 20개 제한·도배라 한 장 수정으로.)
- 규칙·배당은 혼자 판과 같음 (cards.deal_baccarat · bac_payout). 판마다 새 8덱 슈, 결과는 열 때 봉인(해시)·끝나고 공개.
- 정산 먼저 → 공개는 보여주기만 (연출 중 꺼져도 결과대로). 돈 흐름은 multi.Round (take_bet·settle·환불) 그대로.
- 카드 그림 = cardart (실사 카드 CC0). 그림 파일이 없거나 사진 보내기가 안 되면 글자 공개로.
- 회차 번호는 방마다 chat_state 'bac_round_no'. 결과는 🖼 그림장(board.record 'baccarat')에도 쌓임.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InputMediaPhoto
from telegram.error import BadRequest, RetryAfter, TelegramError

from . import Ctx, cardart, register
from .basic import photo_file
from .board import record
from .cards import BAC_LABEL, BAC_PICKS, BacRound, cards_str, deal_baccarat, make_shoe
from .cards import bac_payout as payout_of
from .core import balance, fmt, split_bet
from .dealer import line as dealer_line
from .multi import RETRY_MAX, Player, Round, _join, _secs

log = logging.getLogger(__name__)

WINDOW = 30
STEP = 2.6                    # 공개 단계 사이 (그룹 수정 제한 분당 ~20 → 한 판 수정 4번)
PICKS = ("player", "banker", "tie", "ppair", "bpair")
ROUND_KEY = "bac_round_no"
USAGE = ("🎴 <b>바카라 회차판</b>: <code>!회차 1000 플</code> — 방 사람들이 같이 걸고 30초 뒤 다 같이 공개\n"
         "플 ×2 · 뱅 ×1.95 · 타이 ×10 (타이면 플·뱅 원금) · 플페어·뱅페어 ×13\n"
         "혼자 바로 하려면 <code>!바카라 1000 플</code>")


class BacTableRound(Round):
    game = "bactable"
    title = "🎴 바카라 회차"
    next_hint = "다음 회차에 걸어요!"
    window = WINDOW

    def __init__(self, ctx: Ctx):
        super().__init__(ctx)
        self.rd: BacRound = deal_baccarat(make_shoe(8))
        self.no = 0
        self.seal(f"P {cards_str(self.rd.player)} / B {cards_str(self.rd.banker)}")

    def pick_of(self, p: Player) -> str:
        return PICKS[p.pick]

    def open_text(self, p: Player, pick_txt: str) -> str:
        return (f"🎴 <b>바카라 {self.no}회차</b> 베팅 받는 중 ({self.window}초)\n"
                f"첫 베팅: {p.name} {fmt(p.bet)}{pick_txt}\n{self.seal_line()}\n\n"
                "베팅: <code>!회차 금액 플|뱅|타이|플페어|뱅페어</code>")

    async def open_send(self, ctx: Ctx, p: Player, pick_txt: str) -> None:
        self.no = int(await self.svc.db.get_state(self.chat_id, ROUND_KEY, 0) or 0) + 1
        await self.svc.db.set_state(self.chat_id, ROUND_KEY, self.no)
        text = self.open_text(p, pick_txt)
        if cardart.available():
            try:
                img = await asyncio.to_thread(cardart.open_image, self.no)
                self.live = await self.bot.send_photo(self.chat_id, photo=img, caption=text, parse_mode="HTML")
                self.last_edit = self.clock()
                return
            except Exception as e:   # noqa: BLE001 — 그림이 안 되면 글자로
                log.warning("bactable open photo failed, text: %r", e)
        self.live = None
        await ctx.reply(text)

    async def photo(self, img: bytes, caption: str) -> bool:
        """live 사진을 다른 그림으로 (429 면 한 번 기다렸다). 실패면 False."""
        for attempt in (0, 1):
            try:
                await self.bot.edit_message_media(chat_id=self.chat_id, message_id=self.live.message_id,
                                                  media=InputMediaPhoto(photo_file(img), caption=caption, parse_mode="HTML"))
                self.last_edit = self.clock()
                return True
            except RetryAfter as e:
                if attempt:
                    return False
                await self.sleep(min(_secs(e), RETRY_MAX))
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return True
                log.warning("bactable edit failed: %s", e)
                return False
            except TelegramError as e:
                log.warning("bactable edit failed: %s", e)
                return False
        return False

    async def play(self) -> None:
        rd = self.rd
        for p in self.players.values():          # 정산 먼저 → 공개는 보여주기만
            await self.pay(p, payout_of(rd, self.pick_of(p), p.bet))
        await record(self.svc.db, self.chat_id, "baccarat",
                     rd.winner[0].upper() + ("p" if rd.pair("player") else "") + ("b" if rd.pair("banker") else ""))
        shown = False
        if self.live is not None and cardart.available():
            try:
                steps = await asyncio.to_thread(cardart.stages, rd.player, rd.banker, rd.winner, self.no,
                                                rd.pair("player"), rd.pair("banker"))
            except Exception as e:   # noqa: BLE001
                log.warning("bactable render failed: %r", e)
                steps = []
            head = f"🎴 <b>바카라 {self.no}회차</b> · 베팅 마감 ({len(self.players)}명)"
            words = {"player": "🔵 플레이어 카드 공개!", "banker": "🔴 뱅커 카드 공개!",
                     "third": "➕ 추가 카드 공개!", "result": self.result_line()}
            for i, (name, img) in enumerate(steps):
                if i:
                    await self.sleep(STEP)
                shown = await self.photo(img, f"{head}\n{words[name]}")
                if not shown:
                    break
        self.phase = "done"
        await self.send(self.board(cards=not shown))

    def result_line(self) -> str:
        rd = self.rd
        head = {"player": "🔵 <b>플레이어 승</b>", "banker": "🔴 <b>뱅커 승</b>", "tie": "🟢 <b>타이</b>"}[rd.winner]
        tags = [t for t, ok in (("내추럴", rd.natural), ("플레이어 페어", rd.pair("player")),
                                ("뱅커 페어", rd.pair("banker"))) if ok]
        return f"{head} (플 {rd.p} : {rd.b} 뱅)" + (f" · {' · '.join(tags)}" if tags else "")

    def board(self, cards: bool = False) -> str:
        rd = self.rd
        lines = [f"🎴 <b>바카라 {self.no}회차 결과</b> — {self.result_line()}"]
        if cards:   # 사진 공개가 안 됐으면 카드도 글자로
            lines.append(f"🔵 {cards_str(rd.player)}  ·  🔴 {cards_str(rd.banker)}")
        won = sorted((p for p in self.players.values() if p.payout > p.bet), key=lambda p: -p.payout)
        back = [p for p in self.players.values() if p.payout == p.bet]
        lost = [p for p in self.players.values() if not p.payout]
        if won:
            lines.append(f"\n🎉 <b>적중 {len(won)}명</b>")
            lines += [f"· {p.name} {BAC_LABEL[self.pick_of(p)]} {fmt(p.bet)} → <b>+{fmt(p.payout - p.bet)}</b>" for p in won]
        if back:
            lines.append(f"\n↩️ <b>원금 반환 {len(back)}명</b> (타이)")
            lines += [f"· {p.name} {BAC_LABEL[self.pick_of(p)]} {fmt(p.bet)}" for p in back]
        if lost:
            lines.append(f"\n💸 <b>미적중 {len(lost)}명</b>")
            lines += [f"· {p.name} {BAC_LABEL[self.pick_of(p)]} -{fmt(p.bet)}" for p in lost]
        total_in = sum(p.bet for p in self.players.values())
        total_out = sum(p.payout for p in self.players.values())
        lines.append("\n" + self.reveal_line())
        lines.append(dealer_line(getattr(self, "style", "polite"), total_in, total_out, 10**9))
        lines.append("다음 회차: <code>!회차 금액 플|뱅|타이</code> · 흐름: <code>!그림장 바카라</code>")
        return "\n".join(lines)

    def status(self) -> str:
        if self.phase == "betting":
            pot = sum(p.bet for p in self.players.values())
            return f"🎴 바카라 {self.no}회차: 베팅 받는 중 ({self.left()}초 남음) · {len(self.players)}명 · 판돈 {fmt(pot)}"
        return f"🎴 바카라 {self.no}회차: 카드 공개 중 · {len(self.players)}명 참가"


async def g_bactable(ctx: Ctx) -> None:
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    amount, rest = split_bet(ctx.args, bal)
    pick = next((BAC_PICKS[a.lower()] for a in rest if a.lower() in BAC_PICKS), None)
    if pick is None:
        await ctx.reply(USAGE + f"\n내 잔액: <b>{fmt(bal)}</b>")
        return
    await _join(ctx, BacTableRound, amount, PICKS.index(pick), f" → {BAC_LABEL[pick]}")


register(("회차", "회차바카라", "바카라회차", "바회"), g_bactable, usage="금액 플|뱅|타이|플페어|뱅페어",
         help="🎴 다 같이 거는 바카라 (30초 뒤 공개)", group="같이 하는 게임")
