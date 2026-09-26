"""입장 캡차.

입장 → 채팅 제한 → "강아지 버튼을 누르세요" 같은 그림 버튼 6개 중 정답 누르면 제한 해제 + 환영 인사.
3번 틀리거나 시간이 지나면 설정(captcha_action)대로 처리. 관리자는 승인/거절 버튼으로 대신 처리 가능.
대기 목록은 DB에 있어서 봇을 재시작해도 시간 초과 처리가 이어진다.
"""
from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

from telegram import Bot, CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.error import TelegramError

from .util import display_name, mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

CHOICES = [("🍎", "사과"), ("🚗", "자동차"), ("🐶", "강아지"), ("⚽", "축구공"), ("🌙", "달"), ("☕", "커피"),
           ("🎁", "선물"), ("🔑", "열쇠"), ("🌸", "꽃"), ("🐟", "물고기"), ("✈️", "비행기"), ("📚", "책"),
           ("⏰", "시계"), ("🍕", "피자"), ("🎈", "풍선"), ("🐱", "고양이")]
BUTTONS = 6
MAX_ATTEMPTS = 3


class Captcha:
    def __init__(self, svc: Services):
        self.svc = svc
        self._warned: set[int] = set()

    async def start(self, bot: Bot, chat_id: int, user: User) -> bool:
        """캡차를 걸면 True. 봇 권한이 없어서 못 걸면 False (그냥 입장 처리)."""
        s = await self.svc.db.get_settings(chat_id)
        minutes = s["captcha_minutes"]
        try:
            await bot.restrict_chat_member(chat_id, user.id, ChatPermissions.no_permissions())
        except TelegramError as e:
            log.warning("captcha restrict failed (봇 관리자 권한 확인): %s", e)
            if chat_id not in self._warned:  # 방마다 한 번만 알림
                self._warned.add(chat_id)
                await self.svc.mod.report(
                    bot, f"⚠️ 방 {chat_id}: 캡차를 걸지 못했어요 — 봇에게 관리자 권한 중 <b>'사용자 차단(Ban users)'</b>을 "
                         f"켜주세요. 그 전까지는 캡차 없이 입장 인사만 해요.\n({e})")
            return False
        picks = random.sample(CHOICES, BUTTONS)
        answer = random.randrange(BUTTONS)
        rows = [[InlineKeyboardButton(emoji, callback_data=f"cap:{user.id}:{i}")
                 for i, (emoji, _) in enumerate(picks[r:r + 3], start=r)] for r in range(0, BUTTONS, 3)]
        rows.append([InlineKeyboardButton("✅ 관리자 승인", callback_data=f"cap:{user.id}:ok"),
                     InlineKeyboardButton("🚫 내보내기", callback_data=f"cap:{user.id}:no")])
        text = (f"{mention(user.id, user_name(user))} 대표님 환영합니다! 🤖 스팸 방지 확인이에요.\n"
                f"<b>{minutes}분 안에</b> 아래에서 <b>{picks[answer][1]}</b> 버튼을 눌러주세요. "
                "누르기 전까지는 채팅이 제한돼요.")
        try:
            sent = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        except TelegramError as e:
            log.warning("captcha send failed: %s", e)
            await self._lift(bot, chat_id, user.id)
            return False
        await self.svc.db.add_captcha(chat_id, user.id, sent.message_id, answer, int(time.time()) + minutes * 60)
        await self.svc.db.log_mod(chat_id, None, user.id, "captcha", f"{minutes}분")
        return True

    async def pending(self, chat_id: int, user_id: int) -> bool:
        return await self.svc.db.get_captcha(chat_id, user_id) is not None

    async def on_callback(self, bot: Bot, query: CallbackQuery, parts: list[str]) -> None:
        chat_id = query.message.chat_id if query.message else None
        try:
            target_id, choice = int(parts[0]), parts[1]
        except (IndexError, ValueError):
            await query.answer()
            return
        row = await self.svc.db.get_captcha(chat_id, target_id) if chat_id else None
        if not row:
            await query.answer("이미 끝난 확인이에요.")
            return
        presser = query.from_user

        if choice in ("ok", "no"):
            if not await self.svc.perms.is_admin(bot, chat_id, presser.id):
                await query.answer("관리자만 누를 수 있어요.", show_alert=True)
                return
            await query.answer("처리했어요.")
            if choice == "ok":
                await self.approve(bot, chat_id, target_id, await self._name(chat_id, target_id), presser.id)
            else:
                await self._fail(bot, chat_id, target_id, "관리자가 거절", presser.id, force_kick=True)
            return

        if presser.id != target_id:
            await query.answer("본인만 누를 수 있어요.", show_alert=True)
            return
        if choice.isdecimal() and int(choice) == row["answer"]:
            await query.answer("확인됐어요! 환영합니다 🙌")
            await self.approve(bot, chat_id, target_id, user_name(presser), None)
            return
        attempts = await self.svc.db.captcha_attempt(chat_id, target_id)
        if attempts >= MAX_ATTEMPTS:
            await query.answer("확인에 실패했어요.", show_alert=True)
            await self._fail(bot, chat_id, target_id, f"캡차 {MAX_ATTEMPTS}회 실패", None)
        else:
            await query.answer(f"틀렸어요! 남은 기회 {MAX_ATTEMPTS - attempts}번", show_alert=True)

    async def expire(self, bot: Bot) -> None:
        for row in await self.svc.db.expired_captchas(int(time.time())):
            await self._fail(bot, row["chat_id"], row["user_id"], "캡차 시간 초과", None)

    async def cancel(self, bot: Bot, chat_id: int, user_id: int) -> None:
        """캡차 도중 나간 경우: 기록과 안내 메시지만 정리."""
        row = await self.svc.db.get_captcha(chat_id, user_id)
        if row:
            await self.svc.db.delete_captcha(chat_id, user_id)
            await self._delete(bot, chat_id, row["message_id"])

    # ── 내부 ──────────────────────────────────────────────
    async def _name(self, chat_id: int, user_id: int) -> str:
        m = await self.svc.db.get_member(chat_id, user_id)
        return display_name(m["first_name"], m["last_name"], m["username"]) if m else str(user_id)

    async def _delete(self, bot: Bot, chat_id: int, message_id: int | None) -> None:
        if message_id:
            try:
                await bot.delete_message(chat_id, message_id)
            except TelegramError:
                pass

    async def _lift(self, bot: Bot, chat_id: int, user_id: int) -> None:
        # 모든 권한 True = 개인 제한 해제 (이후 방 기본 권한을 따름)
        await bot.restrict_chat_member(chat_id, user_id, ChatPermissions.all_permissions())

    async def approve(self, bot: Bot, chat_id: int, user_id: int, name: str, actor_id: int | None) -> None:
        row = await self.svc.db.get_captcha(chat_id, user_id)
        if row is None:
            return  # 이미 처리됨 (두 번 누름·관리자 승인과 동시) → 인사가 두 번 나가지 않게
        await self.svc.db.delete_captcha(chat_id, user_id)
        try:
            await self._lift(bot, chat_id, user_id)
        except TelegramError as e:
            log.warning("captcha lift failed: %s", e)
        await self._delete(bot, chat_id, row["message_id"])
        await self.svc.db.log_mod(chat_id, actor_id, user_id, "captcha_pass")
        m = await self.svc.db.get_member(chat_id, user_id)
        await self.svc.db.log_join(chat_id, user_id, name, m["username"] if m else None)
        if (await self.svc.db.get_settings(chat_id))["greet_enabled"]:
            self.svc.greeter.queue(bot, chat_id, user_id, name)

    async def _fail(self, bot: Bot, chat_id: int, user_id: int, reason: str, actor_id: int | None,
                    force_kick: bool = False) -> None:
        row = await self.svc.db.get_captcha(chat_id, user_id)
        await self.svc.db.delete_captcha(chat_id, user_id)
        await self._delete(bot, chat_id, row["message_id"] if row else None)
        self.svc.joins.pop((chat_id, user_id), None)  # 강퇴 후 바로 재입장해도 캡차를 다시 받게
        action = "kick" if force_kick else (await self.svc.db.get_settings(chat_id))["captcha_action"]
        mod = self.svc.mod
        try:
            if action == "ban":
                await mod.ban(bot, chat_id, user_id, actor_id, reason)
            elif action == "mute":
                # 이미 제한된 상태 유지. 관리자가 .뮤트해제 로 풀 수 있다
                await self.svc.db.log_mod(chat_id, actor_id, user_id, "mute", f"무기한 / {reason}")
            else:
                await mod.kick(bot, chat_id, user_id, actor_id, reason)
        except TelegramError as e:
            log.info("captcha fail action (%s) failed: %s", action, e)  # 이미 나간 경우 등
        await mod.report(bot, f"[캡차] chat {chat_id} / user {user_id}: {reason} → {action}")
