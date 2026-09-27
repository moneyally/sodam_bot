"""대량 입장 공격 방어.

N초에 M명 이상 들어오면(기본 60초 10명) 방어 모드 K분(기본 30분):
- 그동안 새 입장자는 raid_action 대로: captcha = 캡차 설정과 상관없이 캡차 (handlers.handle_new_member 가 active() 를 봄),
  kick = 안내 없이 바로 내보냄 (재입장은 가능 → 방어가 끝난 뒤 들어오면 평소대로). 끝날 때 내보낸 수를 관리자 1:1 로
- 방에 사라지는 안내 1번 · 텔레그램 관리자들에게 1:1 알림 · 오너 보고 1번
- 시간이 지나면 handlers.job_tick 이 tick() 으로 해제 (안내 메시지도 지움). 재시작해도 DB(chat_state)에 남아 이어짐
관리자는 🚨 대량 입장 방어 화면(panels/locks.py)에서 직접 켜고 끌 수 있다.
입장 수 세기는 hooks.add_member_join_hook 으로 등록 (관리자·권한 없는 방은 handlers 가 훅 전에 걸러냄).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from telegram.error import TelegramError

from . import hooks, joinreq
from .settings import register_setting
from .subscription import chat_title
from .util import esc, human_minutes

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

register_setting("raid_guard", True, "대량 입장 자동 방어")
register_setting("raid_count", 10, "대량 입장 기준(명)", range_=(3, 500))
register_setting("raid_seconds", 60, "대량 입장 기준(초)", range_=(10, 600))
register_setting("raid_minutes", 30, "방어 모드 시간(분)", range_=(5, 1440))
register_setting("raid_action", "captcha", "방어 모드 중 새 입장자",
                 choices={"captcha": "captcha", "캡차": "captcha", "kick": "kick", "내보내기": "kick"},
                 choice_labels={"captcha": "캡차 받게", "kick": "바로 내보내기"})

STATE = "raid"   # chat_state: {"until": 끝나는 시각, "notice": 방 안내 메시지 ID, "since": 시작 시각}
KICK_REASON = "대량 입장 방어"
_joins: dict[int, deque] = {}
_starting: set[int] = set()


def room_text(minutes: int, action: str = "captcha") -> str:
    return (f"🚨 짧은 시간에 입장이 몰려서 <b>{human_minutes(minutes)} 동안 방어 모드</b>예요.\n"
            + ("지금은 새로 입장할 수 없어요. 잠시 뒤에 다시 들어와 주세요." if action == "kick"
               else "지금 들어오는 분은 스팸 확인 버튼(캡차)을 눌러야 채팅할 수 있어요."))


async def until(svc: Services, chat_id: int) -> int:
    """방어 모드가 끝나는 시각. 방어 중이 아니면 0."""
    st = await svc.db.get_state(chat_id, STATE)
    return st["until"] if st and st.get("until", 0) > time.time() else 0


async def active(svc: Services, chat_id: int) -> bool:
    return bool(await until(svc, chat_id))


async def on_member_join(svc: Services, bot, chat_id: int, user) -> bool:
    """입장마다 수를 센다. 방어 중이고 raid_action=kick 이면 바로 내보내고 True (막음).
    captcha 면 막지 않는다(False) — 캡차는 handlers 가 active() 를 보고 건다."""
    s = await svc.db.get_settings(chat_id)
    if not s["raid_guard"]:
        return False
    now = time.time()
    q = _joins.setdefault(chat_id, deque(maxlen=1000))
    q.append(now)
    while q and now - q[0] > s["raid_seconds"]:
        q.popleft()
    # 입장은 동시에 처리됨 → await 전에 표시해 둬서, 켜는 도중 몰려온 입장이 방어 모드를 또 켜지 않게
    if len(q) >= s["raid_count"] and chat_id not in _starting:
        n = len(q)
        q.clear()
        _starting.add(chat_id)
        try:
            if not await active(svc, chat_id):
                await start(svc, bot, chat_id, s["raid_minutes"], joined=n, seconds=s["raid_seconds"])
        finally:
            _starting.discard(chat_id)
    if s["raid_action"] != "kick" or not await active(svc, chat_id) or await joinreq.passed(svc, chat_id, user.id):
        return False   # 가입 신청 1:1 확인을 통과한 사람은 내보내지 않음
    try:
        await svc.mod.kick(bot, chat_id, user.id, None, KICK_REASON)
    except TelegramError as e:
        log.warning("raid kick failed in %s: %s", chat_id, e)
        return False
    return True   # 내보낸 수는 끝날 때 관리 기록으로 셈 (동시 입장에도 정확, stop)


hooks.add_member_join_hook(on_member_join)


async def _delete_notice(bot, chat_id: int, st: dict | None) -> None:
    if st and st.get("notice"):
        try:
            await bot.delete_message(chat_id, st["notice"])
        except TelegramError:
            pass


async def start(svc: Services, bot, chat_id: int, minutes: int, *, joined: int = 0, seconds: int = 0,
                actor_id: int | None = None) -> int:
    """방어 모드 켜기. actor_id 가 없으면 자동(입장 몰림) → 관리자 1:1 알림 + 오너 보고."""
    end = int(time.time()) + minutes * 60
    await _delete_notice(bot, chat_id, await svc.db.get_state(chat_id, STATE))
    action = (await svc.db.get_settings(chat_id))["raid_action"]
    notice = None
    try:
        notice = (await bot.send_message(chat_id, room_text(minutes, action), parse_mode="HTML")).message_id
    except TelegramError as e:
        log.warning("raid notice failed in %s: %s", chat_id, e)
    await svc.db.set_state(chat_id, STATE, {"until": end, "notice": notice, "since": int(time.time()) - 1})
    why = f"{seconds}초에 {joined}명 입장" if actor_id is None else "관리자가 켬"
    await svc.db.log_mod(chat_id, actor_id, None, "raid", f"{human_minutes(minutes)} / {why}")
    if actor_id is None:
        title = esc(await chat_title(svc, chat_id))
        dm = (f"🚨 <b>{title}</b> 방에 {seconds}초 사이 {joined}명이 들어와서 방어 모드를 {human_minutes(minutes)} 켰어요.\n"
              + ("그동안 새로 들어오는 사람은 안내 없이 바로 내보내요 (다시 들어올 수는 있어요)." if action == "kick"
                 else "그동안 새로 들어오는 사람은 모두 캡차를 받아요.")
              + " 끄기: /start → ⚙️ 내 그룹 관리 → 🚨 대량 입장 방어")
        await _dm_admins(svc, bot, chat_id, dm)
        await svc.mod.report(bot, f"[대량 입장] {title} ({chat_id}) {seconds}초에 {joined}명 → "
                                  f"방어 모드 {human_minutes(minutes)}")
    return end


async def stop(svc: Services, bot, chat_id: int, actor_id: int | None = None) -> None:
    st = await svc.db.get_state(chat_id, STATE)
    if st is None:
        return
    await _delete_notice(bot, chat_id, st)
    await svc.db.set_state(chat_id, STATE, None)
    _joins.pop(chat_id, None)
    row = await svc.db._one("SELECT COUNT(*) AS n FROM mod_log WHERE chat_id=? AND action='kick' AND detail=? AND ts>=?",
                            (chat_id, KICK_REASON, st.get("since", st.get("until", 0))))
    kicked = row["n"]
    await svc.db.log_mod(chat_id, actor_id, None, "raid_off",
                         ("관리자가 끔" if actor_id else "시간 끝남") + (f" / {kicked}명 내보냄" if kicked else ""))
    if kicked:
        title = esc(await chat_title(svc, chat_id))
        await _dm_admins(svc, bot, chat_id, f"✅ <b>{title}</b> 방어 모드가 끝났어요. 그동안 새로 들어온 {kicked}명을 내보냈어요 "
                                            "(다시 들어올 수 있어요). 목록: /start → ⚙️ 내 그룹 관리 → 📋 관리 기록")


async def _dm_admins(svc: Services, bot, chat_id: int, text: str) -> None:
    for a in await svc.perms.admin_users(bot, chat_id):
        if getattr(a, "is_bot", False):
            continue
        try:
            await bot.send_message(a.id, text, parse_mode="HTML")
        except TelegramError:  # 봇과 1:1 을 시작 안 한 관리자
            pass


async def tick(svc: Services, bot) -> None:
    """30초마다: 시간이 끝난 방어 모드 해제."""
    now = time.time()
    for row in await svc.db._all("SELECT chat_id FROM chat_state WHERE key=?", (STATE,)):
        st = await svc.db.get_state(row["chat_id"], STATE)
        if st and st.get("until", 0) <= now:
            await stop(svc, bot, row["chat_id"])
