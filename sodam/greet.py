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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest, TelegramError

from . import mediastore, persist
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
BOT_WAIT = 10             # greet_reply_bot: 인사 차례에 믿는 봇 글이 아직 없으면 더 기다리는 최대 초
BOT_LEAD = 5              # 첫 입장보다 이만큼 먼저 올라온 봇 글까지 (입장 알림 순서가 조금 엇갈려도)

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
# 켜면 ✅ 믿는 봇(🤝 다른 봇 연동)이 방금 올린 글에 답장으로 인사 — 봇끼리는 답장이어야 그 봇에게 감 (Bot-to-Bot).
# 실제 사례 2026-10-03 베베방: 소담 인사 '안내' 를 문지기 봇이 키워드로 받아 안내 글을 올려야 하는데, 그냥 글이라 문지기에게 안 감.
register_setting("greet_reply_bot", False, "다른 봇 환영 글에 답장")
# 입장 인사 = 정해 둔 글을 그대로 복사 ([보낸 대화 ID, 글 ID]). 움직이는 이모지·영상·서식이 그대로 감 (copyMessage, 실측 2026-10-03).
# 베베방: 문지기 봇이 봇 글을 무시해서 '안내' 로 못 부름 → 문지기 이벤트 안내 글을 소담이 복사.
register_setting("greet_copy", [], "입장 인사 글 복사", render_fn=lambda v: "켜짐" if copy_of({"greet_copy": v}) else "(없음)")
# 그 글의 내용 사본 {html, kind, file_id} — 원래 글이 지워지거나 봇이 바뀌어(1:1 글은 봇마다 따로) 복사가 안 되면 이걸로 (2026-10-04 봇 교체 사례)
register_setting("greet_copy_snap", {}, "입장 인사 글 복사 (사본)",
                 render_fn=lambda v: "저장됨" if copy_snap({"greet_copy_snap": v}) else "(없음)")
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


def copy_of(s: dict) -> tuple[int, int] | None:
    v = s.get("greet_copy")
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in v) \
            and v[1] > 0:
        return v[0], v[1]
    return None


def button_rows(s: dict) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(t, url=u)] for t, u in clean_buttons(s.get("greet_buttons"))]


def with_names(template: str, tag: bool = True) -> str:
    """{names} 가 없는 인사말: 태그 켜짐 → 앞에 이름을 붙임, 꺼짐 → 인사말 그대로 (이름 없이)."""
    return template if "{names}" in template or not tag else "{names} " + template


def fill(template: str, names_html: str, extra: dict[str, str] | None = None) -> str:
    """인사말(관리자·AI 가 쓴 글)은 이스케이프하고, 자리표시자에만 코드가 만든 값을 넣는다.
    {names}·{name} 이름(멘션) · {id} 고유번호 · {username} @아이디 · {time} 들어온 시각 (extra 로 받은 값, 2026-10-07 백악관 요청)."""
    out = esc(template).replace(esc("{names}"), names_html).replace(esc("{name}"), names_html)
    for key, value in (extra or {}).items():
        out = out.replace(esc(key), value)
    return out


def fill_values(people: list[tuple[int, str, str | None]], when: str) -> dict[str, str]:
    """people = [(ID, 이름, @아이디)] → {id}·{username}·{time} 값 (HTML)."""
    shown = people[:15]
    return {"{id}": ", ".join(f"<code>{uid}</code>" for uid, _, _ in shown),
            "{username}": ", ".join(esc("@" + u) if u else "아이디 없음" for _, _, u in shown),
            "{time}": esc(when)}


def _plain_len(text_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text_html)))


async def _media_lost_notice(bot: Bot, db, chat_id: int, what: str) -> None:
    """원본이 없어 인사 미디어·복사 글을 못 올림 → 방을 등록한 관리자 1:1 에 하루 한 번."""
    if db is None:
        return
    try:
        sub = await db._one("SELECT added_by FROM subscriptions WHERE chat_id=?", (chat_id,))
    except Exception:
        sub = None
    await mediastore.notify_lost(
        bot, db, f"g{chat_id}:{what}", sub["added_by"] if sub else None,
        f"👋 입장 인사의 <b>{esc(what)}</b>이 사라져서 기본 인사로 대신했어요.\n"
        "소담 1:1 → 방 설정 → ✏️ 인사 편집기에서 다시 넣어 주세요.")


def copy_snap(s: dict) -> dict | None:
    """greet_copy 사본 {html, kind, file_id} — 형식이 맞을 때만."""
    v = s.get("greet_copy_snap")
    if not isinstance(v, dict):
        return None
    html_text = v.get("html") if isinstance(v.get("html"), str) else ""
    kind, fid = v.get("kind"), v.get("file_id")
    media = (kind, fid) if kind in mediastore.KINDS and isinstance(fid, str) and fid else None
    if not html_text and not media:
        return None
    return {"html": html_text, "kind": media[0] if media else None, "file_id": media[1] if media else None}


def snapshot_of(msg) -> dict:
    """전달·답장으로 고른 글 → 사본 (움직이는 이모지·서식은 텔레그램 HTML 로)."""
    from .announce import extract_media
    from .util import rich_html
    kind, fid = extract_media(msg)
    plain = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    try:
        body = rich_html(msg) or esc(plain)
    except (AttributeError, TypeError):   # 엔티티 정보가 없는 메시지
        body = esc(plain)
    return {"html": body, "kind": kind, "file_id": fid}


async def send_snap(bot: Bot, db, chat_id: int, snap: dict):
    """사본으로 같은 글 올리기 (미디어는 mediastore — 봇이 바뀌어도 보관 원본으로)."""
    if snap["kind"]:
        caption = snap["html"] if _plain_len(snap["html"]) <= CAPTION_LIMIT else None
        sent = await mediastore.send(bot, db, snap["kind"], chat_id, snap["file_id"],
                                     caption=caption or None, parse_mode="HTML" if caption else None)
        if snap["html"] and caption is None:
            sent = await bot.send_message(chat_id, snap["html"], parse_mode="HTML")
        return sent
    return await bot.send_message(chat_id, snap["html"], parse_mode="HTML")


# ── 보내기 (실제 인사와 편집기 미리보기가 같이 씀) ─────────
async def send_greeting(bot: Bot, chat_id: int, s: dict, text_html: str,
                        extra_rows: list[list[InlineKeyboardButton]] | None = None, reply_to: int | None = None,
                        db=None) -> list:
    """설정된 미디어·URL 버튼을 붙여 인사를 보낸다. 보낸 메시지 목록을 돌려준다.
    미디어가 안 보내지면(지워진 파일 등) 글만 보낸다. reply_to = 그 메시지에 답장으로 (지워졌으면 그냥).
    미디어는 mediastore 로 — 봇이 바뀌어 file_id 가 안 먹으면 보관 원본으로 다시 올림."""
    rp = ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
    rows = button_rows(s) + (extra_rows or [])
    kb = InlineKeyboardMarkup(rows) if rows else None
    media = media_of(s)
    db = db if db is not None else persist.db_of(bot)
    sent = []
    if media:
        kind, file_id = media
        fits = _plain_len(text_html) <= CAPTION_LIMIT
        try:
            if fits:
                return [await send_retry(lambda: mediastore.send(bot, db, kind, chat_id, file_id, caption=text_html,
                                                                 parse_mode="HTML", reply_markup=kb, reply_parameters=rp))]
            # 설명이 너무 길면 미디어 따로, 글+버튼 따로
            sent.append(await send_retry(lambda: mediastore.send(bot, db, kind, chat_id, file_id, reply_parameters=rp)))
        except BadRequest as e:
            log.warning("greet media failed, sending text only: %s", e)
            if isinstance(e, mediastore.MediaLost):
                await _media_lost_notice(bot, db, chat_id, f"인사 {MEDIA_TYPES.get(kind, '미디어')}")
    sent.append(await send_retry(lambda: bot.send_message(chat_id, text_html, parse_mode="HTML", reply_markup=kb,
                                                          reply_parameters=rp)))
    return sent


class Greeter:
    def __init__(self, svc: Services):
        self.svc = svc
        self._pending: dict[int, list[tuple[int, str]]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._greeted: dict[tuple[int, int], float] = {}   # 자동 인사를 했거나 곧 할 사람 → 시각 (AI 인사 중복 방지)
        self._since: dict[int, float] = {}                 # 방 → 이번 묶음 첫 입장 시각 (greet_reply_bot)
        self._flushed: dict[int, asyncio.Event] = {}       # 방 → 인사가 나가면 set (채널 구독 안내가 인사 뒤에 오게)

    def auto_greeted(self, chat_id: int, user_id: int, within: int = AUTO_GREET_WINDOW) -> bool:
        return time.time() - self._greeted.get((chat_id, user_id), 0) < within

    def queue(self, bot: Bot, chat_id: int, user_id: int, name: str) -> None:
        now = time.time()
        if len(self._greeted) > 5000:
            self._greeted = {k: t for k, t in self._greeted.items() if now - t < AUTO_GREET_WINDOW}
        self._greeted[(chat_id, user_id)] = now
        self._since.setdefault(chat_id, now)
        self._pending.setdefault(chat_id, []).append((user_id, name))
        if chat_id not in self._tasks or self._tasks[chat_id].done():
            self._tasks[chat_id] = asyncio.create_task(self._flush_later(bot, chat_id))

    def busy(self, chat_id: int) -> bool:
        """이 방에 곧 나갈 입장 인사가 있음."""
        task = self._tasks.get(chat_id)
        return bool(self._pending.get(chat_id)) or (task is not None and not task.done())

    async def wait_sent(self, chat_id: int, timeout: float = WAIT_SECONDS + BOT_WAIT + 15) -> None:
        """곧 나갈 인사가 있으면 나갈 때까지 (실패해도·timeout 이면 그냥) 기다림 — 📢 구독 안내가 인사 아래에 오게
        (2026-10-10 뉴월드: 인사는 5초 모아서 나가는데 구독 안내가 바로 떠서 순서가 거꾸로였음)."""
        if not self.busy(chat_id):
            return
        ev = self._flushed.setdefault(chat_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _flush_later(self, bot: Bot, chat_id: int) -> None:
        try:
            await asyncio.sleep(WAIT_SECONDS)
            await self.flush(bot, chat_id)
        finally:
            ev = self._flushed.pop(chat_id, None)
            if ev is not None:
                ev.set()

    async def flush(self, bot: Bot, chat_id: int) -> None:
        people = self._pending.pop(chat_id, [])
        since = self._since.pop(chat_id, time.time() - WAIT_SECONDS)
        if not people:
            return
        s = await self.svc.db.get_settings(chat_id)
        if await self._copy(bot, chat_id, s):
            self._again(bot, chat_id)
            return
        reply_to = await self._bot_welcome(chat_id, since) if s.get("greet_reply_bot") else None
        tag = bool(s.get("greet_mention", True))
        names = ", ".join(mention(uid, name) if tag else esc(name) for uid, name in people[:15])
        if len(people) > 15:
            names += f" 외 {len(people) - 15}분"
        template = with_names(await self._template(chat_id, len(people)), tag)
        extra = None
        if any(k in template for k in ("{id}", "{username}", "{time}")):
            rows = {r["user_id"]: r["username"] for r in await self.svc.db._all(
                f"SELECT user_id, username FROM users WHERE user_id IN ({','.join('?' * len(people[:15]))})",
                [uid for uid, _ in people[:15]])}
            from datetime import datetime
            extra = fill_values([(uid, name, rows.get(uid)) for uid, name in people],
                                datetime.now(self.svc.cfg.tz).strftime("%Y-%m-%d %H:%M"))
        try:
            sent = await send_greeting(bot, chat_id, s, fill(template, names, extra), reply_to=reply_to, db=self.svc.db)
            # 대화 기록(AI 맥락)엔 {names} 자리표시자 대신 실제 이름으로
            plain = ", ".join(name for _, name in people[:15])
            await self.svc.db.log_message(chat_id, bot.id, sent[-1].message_id, template.replace("{names}", plain),
                                          is_bot=True)
        except TelegramError as e:
            log.warning("greet send failed: %s", e)
            for uid, _ in people:           # 못 보냈으면 AI 인사까지 막지 않게
                self._greeted.pop((chat_id, uid), None)
        self._again(bot, chat_id)

    def _again(self, bot: Bot, chat_id: int) -> None:
        if self._pending.get(chat_id):      # 봇 글을 기다리는 사이 또 들어온 사람 (이 작업이 아직 안 끝나 queue 가 새로 안 띄움)
            self._tasks[chat_id] = asyncio.create_task(self._flush_later(bot, chat_id))

    async def _copy(self, bot: Bot, chat_id: int, s: dict) -> bool:
        """greet_copy 가 있으면 그 글을 그대로 복사해 인사 (이름 없음). 원래 글이 지워졌거나 봇이 바뀌어 복사가 안 되면
        저장해 둔 사본(greet_copy_snap)으로 같은 글, 그것도 안 되면 False → 보통 인사."""
        src = copy_of(s)
        if src is None:
            return False
        try:
            sent = await send_retry(lambda: bot.copy_message(chat_id, src[0], src[1]))
        except TelegramError as e:
            snap = copy_snap(s)
            log.warning("greet copy failed %s (%s/%s): %s — %s", chat_id, src[0], src[1], e,
                        "사본으로" if snap else "보통 인사로")
            if snap is None:
                await _media_lost_notice(bot, self.svc.db, chat_id, "복사해 둔 글")
                return False
            try:
                sent = await send_retry(lambda: send_snap(bot, self.svc.db, chat_id, snap))
            except TelegramError as e2:
                log.warning("greet copy snapshot failed %s: %s — 보통 인사로", chat_id, e2)
                await _media_lost_notice(bot, self.svc.db, chat_id, "복사해 둔 글")
                return False
        await self.svc.db.log_message(chat_id, bot.id, getattr(sent, "message_id", None),
                                      "[입장 인사: 관리자가 정한 글을 그대로 올림]", is_bot=True)
        return True

    async def _bot_welcome(self, chat_id: int, since: float) -> int | None:
        """✅ 믿는 봇이 이번 입장 즈음 올린 마지막 글 ID (🤝 연동이 켜져 있어야 기록됨). 아직 없으면 BOT_WAIT 초까지 기다림.
        botlink 를 import 하지 않고 표만 읽음 (순환 import 방지)."""
        s = await self.svc.db.get_settings(chat_id)
        if s.get("botlink_mode", "off") == "off":
            return None
        end = time.monotonic() + BOT_WAIT
        while True:
            row = await self.svc.db._one(
                "SELECT m.msg_id FROM botlink_msgs m JOIN botlink_bots b ON b.chat_id=m.chat_id AND b.bot_id=m.bot_id "
                "WHERE m.chat_id=? AND b.status='trusted' AND m.ts>=? ORDER BY m.ts DESC, m.msg_id DESC LIMIT 1",
                (chat_id, int(since) - BOT_LEAD))
            if row or time.monotonic() >= end:
                return row["msg_id"] if row else None
            await asyncio.sleep(1)

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
