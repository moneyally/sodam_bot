"""입장 인사. 몇 초 모았다가 한 번에 인사한다.

새 멤버 이름은 AI에게 넘기지 않는다 (닉네임에 지시문을 넣는 공격 방지).
AI는 {names} 자리표시자가 들어간 인사말만 만들고, 이름 멘션은 코드가 끼운다.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

from openai import OpenAIError
from telegram import Bot
from telegram.error import TelegramError

from .llm import BudgetExceeded
from .prompt import system_prompt
from .security import filter_output
from .util import esc, mention

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

WAIT_SECONDS = 5
FALLBACKS = [
    "{names} 대표님, 소통방에 오신 걸 환영합니다! 편하게 인사 나눠주세요 🙌",
    "반갑습니다 {names} 대표님! 궁금한 건 언제든 저를 불러주세요.",
    "{names} 대표님 어서 오세요! 좋은 인연 많이 만드시길 바랍니다.",
]


class Greeter:
    def __init__(self, svc: Services):
        self.svc = svc
        self._pending: dict[int, list[tuple[int, str]]] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    def queue(self, bot: Bot, chat_id: int, user_id: int, name: str) -> None:
        self._pending.setdefault(chat_id, []).append((user_id, name))
        if chat_id not in self._tasks or self._tasks[chat_id].done():
            self._tasks[chat_id] = asyncio.create_task(self._flush_later(bot, chat_id))

    async def _flush_later(self, bot: Bot, chat_id: int) -> None:
        await asyncio.sleep(WAIT_SECONDS)
        people = self._pending.pop(chat_id, [])
        if not people:
            return
        names = ", ".join(mention(uid, name) for uid, name in people[:15])
        if len(people) > 15:
            names += f" 외 {len(people) - 15}분"
        template = await self._template(chat_id, len(people))
        text = esc(template).replace(esc("{names}"), names)
        try:
            sent = await bot.send_message(chat_id, text, parse_mode="HTML")
            await self.svc.db.log_message(chat_id, bot.id, sent.message_id, template, is_bot=True)
        except TelegramError as e:
            log.warning("greet send failed: %s", e)

    async def _template(self, chat_id: int, count: int) -> str:
        s = await self.svc.db.get_settings(chat_id)
        if s["greet_template"]:
            tpl = s["greet_template"]
            return tpl if "{names}" in tpl else "{names} " + tpl
        try:
            msg = await self.svc.llm.chat(
                [{"role": "system", "content": system_prompt(self.svc.cfg.bot_name, s["style"])},
                 {"role": "user", "content": (
                     f"방금 대표님 {count}명이 소통방에 입장했다. 환영 인사를 1~2문장으로 써라. "
                     "이름 자리에는 {names} 라는 글자를 정확히 한 번 그대로 넣어라. 링크·이모지 과다 금지.")}],
                model=self.svc.cfg.guard_model, max_tokens=800, purpose="greet")
            text = (msg.content or "").strip()
            if "{names}" in text and len(text) < 300:
                # 인사말에도 링크·지갑주소·외부 멘션이 섞여 나가지 않게
                return filter_output(text, max_chars=300, allowed_usernames=set())
        except (OpenAIError, BudgetExceeded) as e:
            log.info("AI greeting unavailable: %s", e)
        return random.choice(FALLBACKS)
