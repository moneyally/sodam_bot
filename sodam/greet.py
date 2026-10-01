"""입장 인사. 몇 초 모았다가 한 번에 인사한다.

새 멤버 이름은 AI에게 넘기지 않는다 (닉네임에 지시문을 넣는 공격 방지).
AI는 {names} 자리표시자가 들어간 인사말만 만들고, 이름 멘션은 코드가 끼운다.

인사 편집기(panels/greet.py)로 사진·영상·GIF 와 URL 버튼을 붙일 수 있다.
설정값은 `.set`·AI 도구로도 바뀔 수 있으니 보낼 때마다 다시 검사한다 (https:// · tg:// 만, 최대 6개).
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from openai import OpenAIError
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from .llm import BudgetExceeded
from .prompt import system_prompt
from .security import filter_output
from .settings import max_text, register_setting, register_validator
from .util import esc, mention, send_retry

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

WAIT_SECONDS = 5
AUTO_GREET_WINDOW = 600   # 자동 입장 인사 뒤 이 시간 안의 AI 인사 요청은 중복으로 봄
FALLBACKS = [
    "{names} 대표님, 소통방에 오신 걸 환영합니다! 편하게 인사 나눠주세요 🙌",
    "반갑습니다 {names} 대표님! 궁금한 건 언제든 저를 불러주세요.",
    "{names} 대표님 어서 오세요! 좋은 인연 많이 만드시길 바랍니다.",
]

MEDIA_TYPES = {"photo": "사진", "video": "영상", "animation": "GIF"}
MAX_TEMPLATE = 800        # 인사말 글자 수 (이름 15명까지 붙어도 사진 설명 1024자 안쪽이 되게)
MAX_BUTTONS = 6
MAX_BUTTON_TEXT = 30
MAX_URL = 512
CAPTION_LIMIT = 1024      # 텔레그램 사진·영상 설명 글자 한도

_URL_BAD = re.compile(r"[\s<>\"'`\\\x00-\x1f\x7f]")
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
# tg:// 는 채널·그룹·사용자 이동만 (proxy·socks 처럼 설정을 바꾸는 링크 금지)
_TG = re.compile(r"^tg://(?:resolve|join|openmessage|privatepost|user)(?:\?[A-Za-z0-9_.=&%+-]{0,400})?$")
_LINE = re.compile(r"^(.*\S)\s+-\s+(\S+)$")


def _render_buttons(value: Any) -> str:
    """`.설정 전체` 표시용 (값이 [글자, 주소] 목록이라 기본 표시로는 안 됨)."""
    good = clean_buttons(value)
    return ", ".join(f"{t} ({u})" for t, u in good) if good else "(없음)"


register_setting("greet_media_type", "", "인사 미디어 종류")
register_setting("greet_media_id", "", "인사 미디어")
register_setting("greet_buttons", [], "인사 URL 버튼", render_fn=_render_buttons)
# 끄면 이름을 멘션(태그) 대신 글자로만, 인사말에 {names} 가 없으면 이름 없이 인사말만 (오너 요청 2026-10-01 베베방)
register_setting("greet_mention", True, "입장 인사 이름 태그")
register_validator("greet_template", max_text(MAX_TEMPLATE))   # .설정변경·AI 도 편집기와 같은 한도


# ── 검사 ──────────────────────────────────────────────────
def normalize_url(url: str) -> str | None:
    """버튼에 써도 되는 주소면 정리해서 돌려준다. https:// · tg:// 만 (http·javascript 등은 거절)."""
    if not isinstance(url, str) or len(url) > MAX_URL or _URL_BAD.search(url):
        return None
    low = url[:8].lower()
    if low.startswith("tg://"):
        url = "tg://" + url[5:]
        return url if _TG.fullmatch(url) else None
    if not low.startswith("https://"):
        return None
    url = "https://" + url[8:]
    try:
        parts = urlsplit(url)
        parts.port  # 이상한 포트면 ValueError
    except ValueError:
        return None
    # user@host 형태(https://google.com@나쁜곳.com)는 주소를 속이는 데 쓰여서 거절
    if "@" in parts.netloc or not _HOST.fullmatch(parts.hostname or ""):
        return None
    return url


def clean_button_text(text: str) -> str | None:
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text or len(text) > MAX_BUTTON_TEXT or any(ord(ch) < 32 for ch in text):
        return None
    return text


def clean_buttons(value: Any) -> list[tuple[str, str]]:
    """저장된 값에서 쓸 수 있는 버튼만 (최대 6개). 형식이 틀린 항목은 조용히 뺀다."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            text, url = clean_button_text(item[0]), normalize_url(item[1])
            if text and url:
                out.append((text, url))
    return out[:MAX_BUTTONS]


def parse_buttons(raw: str) -> tuple[list[list[str]], str | None]:
    """관리자가 보낸 `버튼 글자 - https://주소` 줄들 → ([[글자, 주소]], 오류문 HTML). 하나라도 틀리면 전부 거절."""
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return [], "한 줄에 하나씩 <code>버튼 글자 - https://주소</code> 로 보내주세요."
    if len(lines) > MAX_BUTTONS:
        return [], f"URL 버튼은 최대 {MAX_BUTTONS}개예요. (보낸 줄: {len(lines)}개)"
    out = []
    for i, line in enumerate(lines, 1):
        m = _LINE.match(line)
        where = f"{i}번째 줄 <code>{esc(line[:40])}</code>"
        if not m:
            return [], f"{where}: 형식이 틀렸어요. <code>버튼 글자 - https://주소</code>"
        text, url = clean_button_text(m.group(1)), normalize_url(m.group(2))
        if not text:
            return [], f"{where}: 버튼 글자는 1~{MAX_BUTTON_TEXT}자로 써주세요."
        if not url:
            return [], f"{where}: 주소는 <code>https://</code> 또는 <code>tg://</code> 로 시작하는 올바른 주소만 돼요."
        out.append([text, url])
    return out, None


def media_of(s: dict) -> tuple[str, str] | None:
    kind, file_id = s.get("greet_media_type"), s.get("greet_media_id")
    if kind in MEDIA_TYPES and isinstance(file_id, str) and file_id:
        return kind, file_id
    return None


def button_rows(s: dict) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(t, url=u)] for t, u in clean_buttons(s.get("greet_buttons"))]


def with_names(template: str, tag: bool = True) -> str:
    """{names} 가 없는 인사말: 태그 켜짐 → 앞에 이름을 붙임, 꺼짐 → 인사말 그대로 (이름 없이)."""
    return template if "{names}" in template or not tag else "{names} " + template


def fill(template: str, names_html: str) -> str:
    """인사말(관리자·AI 가 쓴 글)은 이스케이프하고, {names} 자리에만 코드가 만든 멘션을 넣는다."""
    return esc(template).replace(esc("{names}"), names_html)


def _plain_len(text_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text_html)))


# ── 보내기 (실제 인사와 편집기 미리보기가 같이 씀) ─────────
async def send_greeting(bot: Bot, chat_id: int, s: dict, text_html: str,
                        extra_rows: list[list[InlineKeyboardButton]] | None = None) -> list:
    """설정된 미디어·URL 버튼을 붙여 인사를 보낸다. 보낸 메시지 목록을 돌려준다.
    미디어가 안 보내지면(지워진 파일 등) 글만 보낸다."""
    rows = button_rows(s) + (extra_rows or [])
    kb = InlineKeyboardMarkup(rows) if rows else None
    media = media_of(s)
    sent = []
    if media:
        kind, file_id = media
        send = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation}[kind]
        fits = _plain_len(text_html) <= CAPTION_LIMIT
        try:
            if fits:
                return [await send_retry(lambda: send(chat_id, file_id, caption=text_html, parse_mode="HTML",
                                                      reply_markup=kb))]
            sent.append(await send_retry(lambda: send(chat_id, file_id)))  # 설명이 너무 길면 미디어 따로, 글+버튼 따로
        except BadRequest as e:
            log.warning("greet media failed, sending text only: %s", e)
    sent.append(await send_retry(lambda: bot.send_message(chat_id, text_html, parse_mode="HTML", reply_markup=kb)))
    return sent


class Greeter:
    def __init__(self, svc: Services):
        self.svc = svc
        self._pending: dict[int, list[tuple[int, str]]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._greeted: dict[tuple[int, int], float] = {}   # 자동 인사를 했거나 곧 할 사람 → 시각 (AI 인사 중복 방지)

    def auto_greeted(self, chat_id: int, user_id: int, within: int = AUTO_GREET_WINDOW) -> bool:
        return time.time() - self._greeted.get((chat_id, user_id), 0) < within

    def queue(self, bot: Bot, chat_id: int, user_id: int, name: str) -> None:
        now = time.time()
        if len(self._greeted) > 5000:
            self._greeted = {k: t for k, t in self._greeted.items() if now - t < AUTO_GREET_WINDOW}
        self._greeted[(chat_id, user_id)] = now
        self._pending.setdefault(chat_id, []).append((user_id, name))
        if chat_id not in self._tasks or self._tasks[chat_id].done():
            self._tasks[chat_id] = asyncio.create_task(self._flush_later(bot, chat_id))

    async def _flush_later(self, bot: Bot, chat_id: int) -> None:
        await asyncio.sleep(WAIT_SECONDS)
        await self.flush(bot, chat_id)

    async def flush(self, bot: Bot, chat_id: int) -> None:
        people = self._pending.pop(chat_id, [])
        if not people:
            return
        s = await self.svc.db.get_settings(chat_id)
        tag = bool(s.get("greet_mention", True))
        names = ", ".join(mention(uid, name) if tag else esc(name) for uid, name in people[:15])
        if len(people) > 15:
            names += f" 외 {len(people) - 15}분"
        template = with_names(await self._template(chat_id, len(people)), tag)
        try:
            sent = await send_greeting(bot, chat_id, s, fill(template, names))
            # 대화 기록(AI 맥락)엔 {names} 자리표시자 대신 실제 이름으로
            plain = ", ".join(name for _, name in people[:15])
            await self.svc.db.log_message(chat_id, bot.id, sent[-1].message_id, template.replace("{names}", plain),
                                          is_bot=True)
        except TelegramError as e:
            log.warning("greet send failed: %s", e)
            for uid, _ in people:           # 못 보냈으면 AI 인사까지 막지 않게
                self._greeted.pop((chat_id, uid), None)

    async def _template(self, chat_id: int, count: int) -> str:
        s = await self.svc.db.get_settings(chat_id)
        if s["greet_template"]:
            return s["greet_template"]               # {names} 없을 때 이름 붙이기는 with_names (태그 설정)
        if self.svc.llm is None:
            return random.choice(FALLBACKS)
        try:
            msg = await self.svc.llm.chat(
                [{"role": "system", "content": system_prompt(self.svc.cfg.bot_name, s["style"])},
                 {"role": "user", "content": (
                     f"방금 대표님 {count}명이 소통방에 입장했다. 환영 인사를 1~2문장으로 써라. "
                     "이름 자리에는 {names} 라는 글자를 정확히 한 번 그대로 넣어라. 링크·이모지 과다 금지.")}],
                model=self.svc.cfg.guard_model, max_tokens=800, purpose="greet", chat_id=chat_id)  # 방 토큰에 포함
            text = (msg.content or "").strip()
            if "{names}" in text and len(text) < 300:
                # 인사말에도 링크·지갑주소·외부 멘션이 섞여 나가지 않게
                return filter_output(text, max_chars=300, allowed_usernames=set())
        except (OpenAIError, BudgetExceeded) as e:
            log.info("AI greeting unavailable: %s", e)
        return random.choice(FALLBACKS)
