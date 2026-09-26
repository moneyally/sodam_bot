"""구독 화면·결제 흐름 (텔레그램 쪽).

방에는 결제 관련 내용(금액·주소·'결제' 단어)을 보이지 않는다.
방에는 '⚙️ 봇 설정 (관리자)' 버튼만 두고, 누르면 봇 1:1 채팅이 열리며 관리자 확인 후에만 결제 화면을 보여준다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .billing import fmt_usdt
from .util import esc, to_int

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)
STATE_LABEL = {"trial": "🎁 무료 체험 중", "paid": "✅ 구독 중", "expired": "⛔ 만료", "free": "✅ 무료 이용"}


def setup_link(bot_username: str, chat_id: int) -> str:
    return f"https://t.me/{bot_username}?start=sub_{chat_id}"


def setup_button(bot_username: str, chat_id: int) -> InlineKeyboardMarkup:
    """방에 두는 버튼. 결제 단어 없이 '설정'으로만."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ 봇 설정 (관리자)", url=setup_link(bot_username, chat_id))]])


def _date(ts: int | None, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M") if ts else "-"


async def chat_title(svc: Services, chat_id: int) -> str:
    row = await svc.db._one("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
    return (row["title"] if row and row["title"] else str(chat_id))


async def panel(svc: Services, chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    st = await svc.billing.status(chat_id)
    title = esc(await chat_title(svc, chat_id))
    lines = [f"⚙️ <b>{title}</b>", f"상태: {STATE_LABEL[st.state]}" + (f" (~{_date(st.until, svc.cfg.tz)})" if st.until else "")]
    if st.state == "free":
        return "\n".join(lines), None
    lines += [
        f"요금: 월 <b>{fmt_usdt(svc.billing.price_units)} USDT</b> (TRC20) · {svc.cfg.sub_days}일",
        "구독하면 AI 대화·게임·예약공지·자료 학습·리포트를 제한 없이 써요. 방 관리(캡차·도배 방지)는 항상 무료예요.",
        "남은 기간이 있으면 그 뒤로 이어서 연장돼요.",
    ]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"💳 {svc.cfg.sub_days}일 구독 결제", callback_data=f"pay:new:{chat_id}")]])
    return "\n".join(lines), kb


def invoice_text(svc: Services, inv, title: str) -> str:
    return (
        f"💳 <b>소담 구독 결제</b> — {esc(title)}\n\n"
        f"금액: <code>{fmt_usdt(inv['amount_units'])}</code> USDT  ← <b>끝자리까지 정확히</b>\n"
        "네트워크: <b>TRON (TRC20)</b> 만\n"
        f"받는 주소: <code>{svc.cfg.pay_address}</code>\n"
        f"유효 시간: {_date(inv['expires'], svc.cfg.tz)} 까지\n\n"
        "⚠️ 다른 네트워크(ERC20·BEP20)나 다른 금액으로 보내면 자동 확인이 안 돼요.\n"
        "⚠️ 거래소에서 보낼 땐 출금 수수료를 뺀 금액이 아니라 <b>위 금액이 그대로 도착</b>해야 해요.\n"
        "보낸 뒤 [✅ 입금했어요] 를 누르세요. 자동으로도 1분마다 확인해요 (트론 확정까지 1~3분)."
    )


def invoice_buttons(inv_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 입금했어요", callback_data=f"pay:check:{inv_id}"),
        InlineKeyboardButton("취소", callback_data=f"pay:cancel:{inv_id}"),
    ]])


async def send_panel_dm(svc: Services, bot: Bot, chat_id: int, user_id: int) -> bool:
    """관리자에게 1:1 로 설정 화면 전송. 봇과 대화를 시작한 적 없으면 False."""
    text, kb = await panel(svc, chat_id)
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
        return True
    except TelegramError:
        return False


async def on_callback(svc: Services, bot: Bot, q: CallbackQuery, parts: list[str]) -> None:
    # 결제 버튼은 1:1 채팅에서만 동작 (방에 떠 있을 일은 없지만 이중 안전장치)
    if not q.message or q.message.chat_id != q.from_user.id:
        await q.answer("1:1 채팅에서만 할 수 있어요.", show_alert=True)
        return
    action, arg = (parts + ["", ""])[:2]
    n = to_int(arg)
    if n is None:
        await q.answer()
        return

    if action == "new":
        chat_id = n
        try:  # 결제는 텔레그램 관리자·오너만, 캐시 말고 지금 상태로 확인
            svc.perms.forget(chat_id)
            ok = await svc.perms.is_tg_admin(bot, chat_id, q.from_user.id)
        except TelegramError:
            ok = False
        if not ok:
            await q.answer("그 방의 관리자만 결제할 수 있어요.", show_alert=True)
            return
        try:
            inv = await svc.billing.create_invoice(chat_id, q.from_user.id)
        except RuntimeError as e:  # 대기 청구서가 너무 많음 → 버튼이 빙글빙글 돌지 않게 안내
            await q.answer(str(e), show_alert=True)
            return
        await q.answer()
        await bot.send_message(q.from_user.id, invoice_text(svc, inv, await chat_title(svc, chat_id)),
                               parse_mode="HTML", reply_markup=invoice_buttons(inv["id"]))
        return

    inv = await svc.db.get_invoice(n)
    if not inv or inv["user_id"] != q.from_user.id:
        await q.answer("청구서를 찾을 수 없어요.", show_alert=True)
        return

    if inv["status"] == "paid":
        await q.answer("이미 결제가 확인됐어요 ✅", show_alert=True)
        return
    if inv["status"] in ("cancelled", "expired"):
        await q.answer("끝난 청구서예요. 새로 결제하려면 .구독 을 다시 눌러주세요.", show_alert=True)
        return

    if action == "cancel":
        await svc.db.cancel_invoice(inv["id"])
        await q.answer("취소했어요.")
        await q.edit_message_reply_markup(None)
        return

    if action == "check":
        limiter = svc.pay_check_times
        last = limiter.get(q.from_user.id, 0)
        if time.time() - last < 15:
            await q.answer("15초 뒤에 다시 눌러주세요.")
            return
        limiter[q.from_user.id] = time.time()
        await q.answer("확인 중…")
        await run_check(svc, bot)
        inv = await svc.db.get_invoice(inv["id"])
        if inv["status"] != "paid":
            await bot.send_message(q.from_user.id, "아직 입금이 확인되지 않았어요. 트론 네트워크 확정까지 1~3분 걸려요. "
                                                   "자동으로 계속 확인하고, 확인되면 바로 알려드릴게요.")
        return
    await q.answer()


async def run_check(svc: Services, bot: Bot) -> None:
    """블록체인 대조 + 결과 알림 (버튼·주기 작업 공용)."""
    try:
        paid, unmatched = await svc.billing.check_pending()
    except Exception as e:  # 네트워크·API 오류: 다음 주기에 다시
        log.warning("결제 확인 실패: %s", e)
        paid, unmatched = [], []
    alert = svc.billing.take_alert()  # TronGrid 연속 실패 시 1번, 복구 시 1번
    if alert == "down":
        await svc.mod.report(bot, f"[결제 확인 장애] TronGrid 조회가 {svc.billing.fail_streak}번 연속 실패했어요. "
                                  "입금 자동 확인이 멈춘 상태예요 (TRONGRID_API_KEY·네트워크 확인). 복구되면 다시 알려드릴게요.")
    elif alert == "up":
        await svc.mod.report(bot, "[결제 확인 복구] TronGrid 조회가 다시 정상이에요.")
    for p in paid:
        title = await chat_title(svc, p["chat_id"])
        until = _date(p["until"], svc.cfg.tz)
        for target, text in (
            (p["user_id"], f"✅ 결제 확인! <b>{esc(title)}</b> 구독이 {until} 까지 연장됐어요. 감사합니다 🙏"),
            (p["chat_id"], f"✅ 이 방의 소담 구독이 {until} 까지 활성화됐어요."),  # 방에는 금액 없이
        ):
            try:
                await bot.send_message(target, text, parse_mode="HTML")
            except TelegramError as e:
                log.info("결제 알림 실패 %s: %s", target, e)
        await svc.mod.report(bot, f"[결제] {esc(title)} ({p['chat_id']}) {fmt_usdt(p['amount_units'])} USDT "
                                  f"→ {until}\nfrom <code>{esc(p['from'])}</code>\ntx <code>{esc(p['tx_id'])}</code>")
    for u in unmatched:
        await svc.mod.report(bot, f"[확인 필요 입금] 청구서와 안 맞는 USDT 입금 {fmt_usdt(u['amount_units'])} "
                                  f"from <code>{esc(u['from'])}</code>\ntx <code>{esc(u['tx_id'])}</code>\n"
                                  "금액을 잘못 보낸 경우일 수 있어요. 확인 후 <code>.구독부여 방ID 일수</code> 로 처리하세요.")
