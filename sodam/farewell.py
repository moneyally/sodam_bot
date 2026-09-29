"""👋 퇴장 인사 (방마다 기본 꺼짐). 스스로 나간 사람에게만, AI 호출 없음(비용 0).

- 나감은 두 곳에서 온다: 멤버 상태 변경(chat_member, 서비스 메시지를 숨긴 방에서도 옴)과 'OO님이 나갔습니다' 서비스 메시지.
  (방, 사람)을 CLAIM_SECONDS 동안 먼저 본 쪽이 차지 → 나감 1번 = 인사 최대 1번.
- 스스로 나감 = 나간 사람 == 한 사람(from_user). 관리자·소담 자동 관리(스팸·CAS·대량 입장·캡차 시간 초과·공동 차단…)의
  강퇴·밴은 한 사람이 다르거나 상태가 kicked 라 인사 없음. 봇·캡차 대기 중인 사람·대량 입장 방어 중에도 없음.
- 방마다 WINDOW 초에 1번. 그 사이 나간 사람은 다음 인사에 합친다 ('A 외 2명', 🆔 줄엔 전부).
- 문구: 관리자가 쓴 것(panels/farewell.py) 또는 방 말투별 기본 문구. {name} 은 멘션 아닌 이름(이스케이프),
  {username} @아이디/'아이디 없음', {id} 숫자 ID, {count} 지금 멤버 수(못 세면 그 줄을 뺌).
  {id} 가 없으면 맨 아래 🆔 줄을 붙인다 (사칭·먹튀 추적용 — 운영자 요청).
- 새로 기록하는 건 없다 (나감은 이미 members/member_left 에 남음).
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

from . import persist, raid
from .greet import _render_buttons, button_rows as _greet_rows
from .settings import register_setting
from .util import esc, send_retry, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

WINDOW = 20             # 방마다 이 시간(초)에 인사 1번
CLAIM_SECONDS = 60      # 같은 나감이 두 경로로 와도 한 번만
MAX_PEOPLE = 30         # 한 번에 합치는 사람 (넘으면 수만 셈)
MAX_IDS = 10            # 🆔 줄에 보이는 사람
MAX_TEMPLATE = 800
ID_LINE = "🆔 {id} · {username}"
DELETE_AFTER = [(0, "안 지움"), (60, "1분"), (600, "10분"), (3600, "1시간")]   # 봇은 48시간 지난 글을 못 지움

# 말투별 기본 문구 (자유분방·여친·남친 = 반말, 나머지 존댓말 — 입장 인사 FALLBACKS 와 같은 목소리)
DEFAULTS = {
    "polite": "{name} 대표님이 방을 나가셨습니다. 그동안 함께해 주셔서 감사했습니다.",
    "friendly": "{name} 대표님이 떠나셨어요. 그동안 고마웠어요, 또 만나요! 👋",
    "brief": "{name}님이 나갔습니다.",
    "secretary": "대표님, {name}님이 방을 나가셨습니다.",
    "tsundere": "{name}님 나가셨네요. 딱히 아쉬운 건 아니지만… 잘 가요.",
    "free": "{name} 나갔네. 잘 가~ 👋",
    "girlfriend": "{name} 나갔네… 다음에 또 보자 ♡",
    "boyfriend": "{name} 나갔네. 잘 지내, 또 보자.",
    "savage": "{name} 나갔네 ㅋㅋ 잘 가라~ 가끔 생각나면 또 와 👋",
}

register_setting("farewell_mode", "off", "퇴장 인사",
                 choices={"on": "on", "켜기": "on", "켬": "on", "off": "off", "끄기": "off", "끔": "off"},
                 choice_labels={"on": "켜짐", "off": "꺼짐"})
register_setting("farewell_template", "", "퇴장 문구")
register_setting("farewell_buttons", [], "퇴장 URL 버튼", render_fn=_render_buttons)
register_setting("farewell_delete_after", 0, "퇴장 인사 삭제(초)", range_=(0, 3600))


def template_of(s: dict) -> str:
    """보낼 문구 (자리표시자 그대로). 직접 쓴 게 없으면 방 말투 기본 문구."""
    return s.get("farewell_template") or DEFAULTS.get(s.get("style"), DEFAULTS["polite"])


def button_rows(s: dict):
    return _greet_rows({"greet_buttons": s.get("farewell_buttons")})


def render(template: str, people: list[tuple[int, str, str | None]], count: int | None = None, extra: int = 0) -> str:
    """people = [(ID, 이름, @아이디)]. 문구는 이스케이프하고 자리표시자에만 코드가 만든 값을 넣는다."""
    tpl = template if "{name}" in template else "{name} " + template
    if "{id}" not in tpl:
        tpl += "\n" + ID_LINE
    if count is None:
        tpl = "\n".join(ln for ln in tpl.split("\n") if "{count}" not in ln)
    more = len(people) - 1 + extra
    shown = people[:MAX_IDS]
    tail = f" 외 {len(people) + extra - len(shown)}명" if len(people) + extra > len(shown) else ""
    values = {
        "{name}": esc(people[0][1] or "알 수 없음") + (f" 외 {more}명" if more else ""),
        "{username}": ", ".join(esc("@" + u) if u else "아이디 없음" for _, _, u in shown) + tail,
        "{id}": ", ".join(f"<code>{uid}</code>" for uid, _, _ in shown) + tail,
        "{count}": str(count),
    }
    out = esc(tpl)
    for key, value in values.items():
        out = out.replace(esc(key), value)
    return out


class _Room:
    __slots__ = ("last", "people", "extra", "task")

    def __init__(self):
        self.last = 0.0
        self.people: list[tuple[int, str, str | None]] = []
        self.extra = 0
        self.task: asyncio.Task | None = None


_rooms: dict[tuple, _Room] = {}      # (DB 경로, 방) → 상태 (WINDOW 초 합치기. 정상 종료 땐 flush_all 이 보냄)


async def _claim(svc: Services, chat_id: int, user_id: int) -> bool:
    """DB 차지 (persist.claims): 재시작 직후 같은 나감이 다시 와도(못 끝낸 업데이트 재전송) 인사 1번."""
    return await persist.claim(svc.db, f"farewell:{chat_id}:{user_id}", CLAIM_SECONDS)


async def on_leave(context, chat_id: int, user, by=None, *, kicked: bool = False) -> None:
    """handlers 가 나감을 볼 때마다 (서비스 메시지·멤버 상태 변경). 실패해도 나감 처리는 계속."""
    try:
        svc = context.bot_data["svc"]
        if not user or user.is_bot or not await _claim(svc, chat_id, user.id):
            return
        if kicked or not by or by.id != user.id:
            return   # 관리자·자동 관리가 내보냄
        s = await svc.db.get_settings(chat_id)
        if s["farewell_mode"] != "on" or await raid.active(svc, chat_id) \
                or (svc.captcha and await svc.captcha.pending(chat_id, user.id)):
            return
        room = _rooms.setdefault((svc.db.path, chat_id), _Room())
        if len(room.people) < MAX_PEOPLE:
            room.people.append((user.id, user_name(user), user.username))
        else:
            room.extra += 1
        if room.task and not room.task.done():
            return   # 곧 나갈 인사에 합침
        wait = room.last + WINDOW - time.time()
        if wait <= 0:
            await flush(context, chat_id)
        else:
            room.task = asyncio.create_task(_flush_later(context, chat_id, wait))
    except Exception:
        log.exception("farewell failed in %s", chat_id)


async def _flush_later(context, chat_id: int, wait: float) -> None:
    await asyncio.sleep(wait)
    try:
        await flush(context, chat_id)
    except Exception:
        log.exception("farewell flush failed in %s", chat_id)


async def flush(context, chat_id: int) -> None:
    svc, bot = context.bot_data["svc"], context.bot
    room = _rooms.get((svc.db.path, chat_id))
    if not room or not room.people:
        return
    people, extra = room.people, room.extra
    room.people, room.extra, room.last = [], 0, time.time()   # await 전에 차지 (동시에 온 나감은 다음 창으로)
    s = await svc.db.get_settings(chat_id)
    if s["farewell_mode"] != "on" or await raid.active(svc, chat_id):
        return
    tpl = template_of(s)
    count = None
    if "{count}" in tpl:
        try:
            count = await bot.get_chat_member_count(chat_id)
        except TelegramError:
            pass
    rows = button_rows(s)
    try:
        sent = await send_retry(lambda: bot.send_message(chat_id, render(tpl, people, count, extra), parse_mode="HTML",
                                                         reply_markup=InlineKeyboardMarkup(rows) if rows else None))
    except TelegramError as e:
        log.warning("farewell send failed in %s: %s", chat_id, e)
        return
    secs = s["farewell_delete_after"]
    if secs and getattr(context, "job_queue", None):
        from .handlers import delete_after   # send_temp 와 같은 지연 삭제 (DB 에도 → 재시작해도 지움, 순환 import 라 여기서)
        await delete_after(context, chat_id, sent.message_id, secs)
    elif secs:                                # 종료 중 보낸 인사 (persist.flush_on_stop): DB 에만 → 다음 실행의 sweep 이 지움
        await persist.remember_delete(svc.db, bot, chat_id, sent.message_id, secs)


async def flush_all(svc: Services, bot, bot_data: dict) -> None:
    """정상 종료(배포) 때: 합치려고 기다리던 퇴장 인사를 지금 보냄 (안 그러면 재시작으로 사라짐)."""
    ctx = SimpleNamespace(bot_data=bot_data, bot=bot, job_queue=None)
    for (path, chat_id), room in list(_rooms.items()):
        if path != svc.db.path or not room.people:
            continue
        if room.task and not room.task.done():
            room.task.cancel()
        try:
            await flush(ctx, chat_id)
        except Exception:
            log.exception("farewell flush on stop failed in %s", chat_id)
