"""장시간 게임 알림 (방마다 켜기, 기본 꺼짐).

멤버가 게임 명령(기본: / 나 ! 로 시작하는 명령·🎲 주사위, 또는 관리자가 정한 명령 목록)을 보낼 때마다 그 사람의 '연속 게임'을
늘린다. gt_gap 분 넘게 안 하면 새로 시작. 10분마다(job) 연속 시간이 gt_hours 를 넘은 사람을 한 번 알린다:
방에 짧게 + 알릴 관리자 1:1 에 [🔇 뮤트][👌 괜찮음] (gt_action=auto 면 바로 뮤트하고 [🔊 풀기]).
멤버가 보낸 명령 시각으로 센다. 🤝 다른 봇 연동(sodam/botlink.py)을 켜고 게임봇을 ✅ 믿는 봇으로 두면
그 봇이 멤버 글에 단 답장(버튼으로만 하는 게임 결과 등)도 그 멤버의 게임 활동으로 센다.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from . import free, hooks
from .db import register_schema
from .settings import register_setting
from .util import esc, mention

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

ACTIONS = {"notify": "알림만", "button": "알림+뮤트 버튼", "auto": "자동 뮤트"}
register_setting("gt_enabled", False, "장시간 게임 알림")
register_setting("gt_hours", 12, "게임 알림 기준(시간)", range_=(1, 48))
register_setting("gt_gap", 30, "게임 쉬면 초기화(분)", range_=(5, 180))
register_setting("gt_action", "button", "게임 알림 조치",
                 choices={**{k: k for k in ACTIONS}, **{v: k for k, v in ACTIONS.items()}, "알림": "notify",
                          "버튼": "button", "자동": "auto"}, choice_labels=ACTIONS)
register_setting("gt_mute_hours", 3, "게임 뮤트 시간(시간)", range_=(1, 48))
register_setting("gt_notify", "setter", "게임 알림 받을 사람",
                 choices={"setter": "setter", "지정": "setter", "admins": "admins", "관리자": "admins"},
                 choice_labels={"setter": "지정한 관리자", "admins": "모든 관리자"})
register_setting("gt_cmds", "", "게임 명령 목록(비우면 / ! 명령·🎲 전부)")
register_setting("gt_setter", 0, "게임 알림 받을 관리자 ID")
KEYS = ("gt_enabled", "gt_hours", "gt_gap", "gt_action", "gt_mute_hours", "gt_notify", "gt_cmds")

register_schema("""
CREATE TABLE IF NOT EXISTS game_sessions (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    start   INTEGER NOT NULL,
    last    INTEGER NOT NULL,
    alerted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"game_sessions": "composite"})


def is_game(msg, cmds: str) -> bool:
    if getattr(msg, "dice", None):
        return True
    words = (msg.text or "").split()
    if not words:
        return False
    first = words[0].split("@")[0].lower()
    if cmds.strip():
        return first in {c.lower() for c in cmds.split()}
    return len(first) > 1 and first[0] in "/!"


async def record(db, chat_id: int, user_id: int, gap_min: int, ts: int | None = None) -> None:
    """한 문장: gap 넘게 쉬었으면 새 세션(start·alerted 초기화), 아니면 last 만 (SET 우변은 모두 옛 값 기준)."""
    ts = ts or int(time.time())
    await db._write(
        "INSERT INTO game_sessions(chat_id, user_id, start, last) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
        "start=CASE WHEN excluded.last-last > ? THEN excluded.start ELSE start END, "
        "alerted=CASE WHEN excluded.last-last > ? THEN 0 ELSE alerted END, last=excluded.last",
        (chat_id, user_id, ts, ts, gap_min * 60, gap_min * 60))


async def on_message(svc: Services, bot: Bot, msg, role) -> None:
    s = await svc.db.get_settings(msg.chat_id)
    if s["gt_enabled"] and is_game(msg, s["gt_cmds"]):
        await record(svc.db, msg.chat_id, msg.from_user.id, s["gt_gap"])


hooks.add_group_message_hook(on_message)


def fmt_dur(sec: int) -> str:
    h, m = divmod(sec // 60, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


async def playing(db, chat_id: int, gap_min: int, now: int | None = None) -> list:
    """지금 게임 중(마지막 명령이 gap 안)인 사람, 오래한 순."""
    now = now or int(time.time())
    return await db._all(
        "SELECT g.*, u.first_name, u.last_name FROM game_sessions g LEFT JOIN users u ON u.user_id=g.user_id "
        "WHERE g.chat_id=? AND ?-g.last <= ? ORDER BY g.last-g.start DESC LIMIT 10", (chat_id, now, gap_min * 60))


def button_kb(chat_id: int, uid: int, muted: bool, hours: int) -> InlineKeyboardMarkup:
    b = lambda text, act: InlineKeyboardButton(text, callback_data=f"m:gtb:{chat_id}:{uid}:{act}")  # noqa: E731
    return InlineKeyboardMarkup([[b("🔊 풀기", "u")] if muted else [b(f"🔇 {hours}시간 뮤트", "m"), b("👌 괜찮음", "ok")]])


async def _alert(svc: Services, bot: Bot, row, s: dict) -> None:
    from .subscription import chat_title  # 늦게 import (순환 방지)
    cid, uid = row["chat_id"], row["user_id"]
    name = " ".join(x for x in (row["first_name"], row["last_name"]) if x) or str(uid)
    admins = [a for a in await svc.perms.admin_users(bot, cid) if not a.is_bot]
    setter = next((a for a in admins if a.id == s["gt_setter"]), None)
    targets = [setter] if s["gt_notify"] == "setter" and setter else admins
    muted = False
    if (s["gt_action"] == "auto" and await svc.perms.bot_can_moderate(bot, cid)
            and not await svc.perms.protected(bot, cid, uid) and not await free.is_free(svc.db, cid, uid)):
        try:
            await svc.mod.mute(bot, cid, uid, s["gt_mute_hours"] * 60, bot.id, f"장시간 게임 {fmt_dur(row['last'] - row['start'])}")
            muted = True
        except TelegramError as e:
            log.warning("gametime mute failed %s/%s: %s", cid, uid, e)
    what = (f"🎮 {mention(uid, name)}님이 <b>{fmt_dur(row['last'] - row['start'])}</b>째 게임 중이에요 "
            f"(기준 {s['gt_hours']}시간)." + (f" {s['gt_mute_hours']}시간 채팅 금지했어요." if muted else ""))
    call = mention(setter.id, setter.first_name) + "님 확인해 주세요." if targets == [setter] else "관리자님들 확인해 주세요."
    try:
        await bot.send_message(cid, f"{what}\n{call}", parse_mode="HTML")
    except TelegramError as e:
        log.warning("gametime room notice failed %s: %s", cid, e)
    kb = button_kb(cid, uid, muted, s["gt_mute_hours"]) if muted or s["gt_action"] == "button" else None
    title = esc(await chat_title(svc, cid))
    for a in targets:
        try:
            await bot.send_message(a.id, f"🎮 <b>{title}</b>\n{what}", parse_mode="HTML", reply_markup=kb)
        except TelegramError:
            pass  # 봇과 1:1 을 시작 안 한 관리자


async def check(svc: Services, bot: Bot, now: int | None = None) -> int:
    """기준을 넘은 세션마다 한 번 알림. 알린 수."""
    now = now or int(time.time())
    rows = await svc.db._all(
        "SELECT g.*, u.first_name, u.last_name FROM game_sessions g LEFT JOIN users u ON u.user_id=g.user_id "
        "WHERE g.alerted=0 AND g.last-g.start >= 3600")
    n = 0
    for row in rows:
        s = await svc.db.get_settings(row["chat_id"])
        if (not s["gt_enabled"] or row["last"] - row["start"] < s["gt_hours"] * 3600
                or now - row["last"] > s["gt_gap"] * 60 or not await svc.paid_features(row["chat_id"])):
            continue
        await svc.db._write("UPDATE game_sessions SET alerted=1 WHERE chat_id=? AND user_id=?",
                            (row["chat_id"], row["user_id"]))   # 먼저 표시: 알림이 실패해도 10분마다 반복하지 않게
        try:
            await _alert(svc, bot, row, s)
            n += 1
        except Exception:
            log.exception("gametime alert failed %s/%s", row["chat_id"], row["user_id"])
    await svc.db._write("DELETE FROM game_sessions WHERE last < ?", (now - 2 * 86400,))
    return n
