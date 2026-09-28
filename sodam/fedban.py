"""여러 방 공동 차단 명단 (사기·스팸 계정).

- 올리기: 방 관리자(텔레그램 '사용자 차단' 권한)가 `.공동차단 @아이디|답장 사유` → 명단 + 그 방에서 밴.
  이용 기간(구독·체험) 중인 방만, 한 방 하루 DAILY_LIMIT 명, 관리자·봇·운영자는 못 올림.
- 다른 방(설정 fedban_mode): off 끔 / alert 텔레그램 관리자 1:1 알림(기본, 모든 방) / ban 자동 밴(이용 기간 중인 방만,
  아니면 알림으로). 입장할 때(hooks.add_member_join_hook)와 처음 말할 때(add_group_message_hook) 확인.
- 빼기: 올린 방이 해제하면 그 방 표시만 빠짐 (남은 방이 없으면 명단에서 사라짐) · 오너는 통째로 삭제.
- 개인정보: 명단엔 user_id·당시 이름·사유·올린 방 id·시각만. 누가 올렸는지는 그 방 관리 기록(mod_log)에만.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from . import free, hooks, incidents
from .db import register_schema
from .permissions import Role
from .settings import register_setting
from .util import esc, mention, post_temp

log = logging.getLogger(__name__)

DAILY_LIMIT = 20
MODES = {"off": "끔", "alert": "관리자에게 알림", "ban": "자동 밴"}
register_setting("fedban_mode", "alert", "공동 차단 명단",
                 choices={"off": "off", "끔": "off", "alert": "alert", "알림": "alert", "ban": "ban", "자동밴": "ban",
                          "밴": "ban"},
                 render_fn=lambda v: MODES.get(v, str(v)))

register_schema("""
CREATE TABLE IF NOT EXISTS fedban_entries (
    user_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fedban_reports (
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    reason  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);
CREATE INDEX IF NOT EXISTS fedban_reports_chat ON fedban_reports(chat_id, ts);
CREATE TABLE IF NOT EXISTS fedban_seen (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    n_rooms INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"fedban_reports": "composite", "fedban_seen": "composite"})

USAGE = ("🛡️ <b>공동 차단 명단</b>\n"
         "사기·스팸 계정을 올리면 소담을 쓰는 다른 방에 들어올 때 관리자에게 알려드려요 (방 설정에 따라 자동 밴).\n"
         "• 올리기: <code>.공동차단 @아이디 사유</code> 또는 메시지에 답장하며 <code>.공동차단 사유</code>\n"
         "  → 이 방에서도 바로 내보내요. 사유는 꼭 적어주세요.\n"
         f"• 빼기: <code>.공동차단 해제 숫자ID</code> (이 방이 올린 표시만 빠져요)\n"
         f"• 한 방에서 하루 {DAILY_LIMIT}명까지, 이용 기간 중인 방만 올릴 수 있어요.")
NOT_PAID = "공동 차단 명단에 올리기는 이용 기간(구독·체험) 중인 방에서만 할 수 있어요."


# ── 조회 ──────────────────────────────────────────────────
LISTED = ("fedban_entries",)   # db.cached 키: 명단 user_id 전부 (메시지마다 확인 → 캐시, 올리기·빼기가 지움)


async def _listed(db) -> frozenset[int]:
    async def load():
        return frozenset(r["user_id"] for r in await db._all("SELECT user_id FROM fedban_entries"))
    return await db.cached(LISTED, load)


async def lookup(db, user_id: int) -> dict | None:
    """명단에 있으면 {name, rooms, reasons(최근 3개)}."""
    if user_id not in await _listed(db):
        return None
    entry = await db._one("SELECT name FROM fedban_entries WHERE user_id=?", (user_id,))
    if not entry:
        return None
    reps = await db._all("SELECT chat_id, reason FROM fedban_reports WHERE user_id=? ORDER BY ts DESC", (user_id,))
    if not reps:
        return None
    return {"name": entry["name"], "rooms": len(reps), "reasons": [r["reason"] for r in reps[:3]],
            "chats": [r["chat_id"] for r in reps]}


async def recent(db, limit: int = 15) -> list:
    return await db._all(
        "SELECT e.user_id, e.name, COUNT(r.chat_id) AS rooms, MAX(r.ts) AS last_ts, "
        "(SELECT reason FROM fedban_reports x WHERE x.user_id=e.user_id ORDER BY x.ts DESC LIMIT 1) AS reason "
        "FROM fedban_entries e JOIN fedban_reports r ON r.user_id=e.user_id "
        "GROUP BY e.user_id ORDER BY last_ts DESC LIMIT ?", (limit,))


async def total(db) -> int:
    row = await db._one("SELECT COUNT(*) AS n FROM fedban_entries")
    return row["n"] if row else 0


async def room_reported(db, chat_id: int, user_id: int) -> bool:
    return bool(await db._one("SELECT 1 FROM fedban_reports WHERE user_id=? AND chat_id=?", (user_id, chat_id)))


def _day(svc) -> str:
    return datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")


# ── 올리기 · 빼기 ─────────────────────────────────────────
async def add(svc, bot, chat_id: int, user_id: int, name: str, reason: str, actor_id: int) -> str:
    """명단에 올리고 이 방에서 밴. 권한·대상 확인(관리자·봇·운영자 제외)은 부르는 쪽(commands.c_fedban)에서.
    돌려주는 값 = 방에 보낼 안내문."""
    db = svc.db
    if not await svc.paid_features(chat_id):
        return NOT_PAID
    day = _day(svc)
    if await db.counter(day, chat_id, "fedban_add") >= DAILY_LIMIT:
        return f"공동 차단 명단엔 한 방에서 하루 {DAILY_LIMIT}명까지 올릴 수 있어요. 내일 다시 해주세요."
    reason, now = reason.strip()[:200], int(time.time())
    await db.bump(day, chat_id, "fedban_add")
    await db._write("INSERT INTO fedban_entries(user_id, name, ts) VALUES(?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (user_id, name[:64], now))
    db.uncache(LISTED)
    await db._write("INSERT INTO fedban_reports(user_id, chat_id, reason, ts) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(user_id, chat_id) DO UPDATE SET reason=excluded.reason, ts=excluded.ts",
                    (user_id, chat_id, reason, now))
    await db.log_mod(chat_id, actor_id, user_id, "fedban", reason)
    info = await lookup(db, user_id)
    try:
        await svc.mod.ban(bot, chat_id, user_id, actor_id, f"공동 차단: {reason}")
        banned = "이 방에서 내보냈어요"
    except TelegramError as e:
        log.warning("fedban ban failed in %s: %s", chat_id, e)
        banned = "이 방에서 내보내기는 실패했어요 (봇에게 '사용자 차단' 권한이 있는지 확인해주세요)"
    return (f"🛡️ {mention(user_id, name)}님을 <b>공동 차단 명단</b>에 올리고 {banned}.\n"
            f"사유: {esc(reason)}\n"
            f"지금 {info['rooms']}개 방이 올린 계정이에요. 소담을 쓰는 다른 방에 들어오면 그 방 관리자에게 알려드려요.")


async def remove(svc, chat_id: int, user_id: int, actor_id: int, *, owner: bool) -> str:
    """오너: 명단에서 완전히 삭제. 방 관리자: 그 방이 올린 표시만 빼기 (남은 방이 없으면 명단에서 사라짐)."""
    db = svc.db
    info = await lookup(db, user_id)
    if not info:
        return "공동 차단 명단에 없는 계정이에요."
    if owner:
        await db._write("DELETE FROM fedban_reports WHERE user_id=?", (user_id,))
        await db._write("DELETE FROM fedban_entries WHERE user_id=?", (user_id,))
        db.uncache(LISTED)
        await db._write("DELETE FROM fedban_seen WHERE user_id=?", (user_id,))
        if chat_id:
            await db.log_mod(chat_id, actor_id, user_id, "fedban_remove", "전체 삭제")
        return f"✅ {esc(info['name'])}(<code>{user_id}</code>)님을 공동 차단 명단에서 완전히 뺐어요."
    if chat_id not in info["chats"]:
        return "이 방이 올린 기록이 없어요. 다른 방이 올린 표시는 그 방이나 봇 운영자만 뺄 수 있어요."
    await db._write("DELETE FROM fedban_reports WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    left = info["rooms"] - 1
    if not left:
        await db._write("DELETE FROM fedban_entries WHERE user_id=?", (user_id,))
        db.uncache(LISTED)
    await db.log_mod(chat_id, actor_id, user_id, "fedban_remove", "이 방 표시")
    tail = f"다른 방 {left}곳이 올린 표시는 남아 있어요." if left else "올린 방이 없어서 명단에서 사라졌어요."
    return (f"✅ {esc(info['name'])}(<code>{user_id}</code>)님 공동 차단 표시를 이 방에서 뺐어요. {tail}\n"
            "이 방 밴은 그대로예요. 다시 들어오게 하려면 <code>.밴해제 ID</code>")


# ── 입장·첫 발언 검사 ─────────────────────────────────────


def alert_text(title: str, name: str, user_id: int, info: dict, spoke: bool) -> str:
    head = "공동 차단 명단 계정이 말을 했어요" if spoke else "공동 차단 명단 계정 입장"
    reasons = " / ".join(esc(r[:60]) for r in info["reasons"])
    return (f"🛡️ <b>{head}</b>\n"
            f"방: {esc(title)}\n"
            f"이름: {esc(name)} (ID <code>{user_id}</code>)\n"
            f"사유: {reasons}\n"
            f"올린 방: {info['rooms']}곳 (많을수록 믿을 만해요)\n"
            "내보낼까요?")


def alert_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 밴", callback_data=f"m:fbx:{chat_id}:{user_id}:b"),
                                  InlineKeyboardButton("무시", callback_data=f"m:fbx:{chat_id}:{user_id}:i")]])


async def check(svc, bot, chat_id: int, user, *, spoke: bool = False) -> bool:
    """명단에 있는 사람이면 방 설정대로 처리. 밴했으면 True."""
    s = await svc.db.get_settings(chat_id)
    mode = s.get("fedban_mode", "alert")
    if mode == "off":
        return False
    info = await lookup(svc.db, user.id)
    if not info:
        return False
    name = " ".join(x for x in (user.first_name, user.last_name) if x) or f"ID {user.id}"
    if mode == "ban" and await svc.paid_features(chat_id) and await svc.perms.bot_can_moderate(bot, chat_id):
        try:
            await svc.mod.ban(bot, chat_id, user.id, None, "공동 차단 명단: " + info["reasons"][0][:100])
        except TelegramError as e:
            log.warning("fedban auto ban failed in %s: %s", chat_id, e)
        else:
            await _mark(svc.db, chat_id, user.id, info["rooms"])
            post_temp(bot, chat_id, f"🛡️ {esc(name)}님은 공동 차단 명단(방 {info['rooms']}곳에서 신고)에 있는 계정이라 "
                                "내보냈어요.")
            return True
    seen = await svc.db._one("SELECT n_rooms FROM fedban_seen WHERE chat_id=? AND user_id=?", (chat_id, user.id))
    if seen and seen["n_rooms"] >= info["rooms"]:
        return False  # 이미 알렸음 (올린 방이 늘면 다시 알림)
    await _mark(svc.db, chat_id, user.id, info["rooms"])
    from .subscription import chat_title
    text = alert_text(await chat_title(svc, chat_id), name, user.id, info, spoke)
    # 방마다 10분 안의 명단 계정은 관리자 1:1 메시지 하나에 (사람마다 [🚫 밴][무시] 버튼 유지, sodam/incidents.py)
    admins = [a for a in await svc.perms.admin_users(bot, chat_id) if not getattr(a, "is_bot", False)]
    await incidents.open_or_bump(svc, bot, chat_id, "fedban", "room", text, alert_kb(chat_id, user.id), admins,
                                 sub=user.id, label=name)
    return False


async def _mark(db, chat_id: int, user_id: int, n: int) -> None:
    await db._write("INSERT INTO fedban_seen(chat_id, user_id, n_rooms, ts) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(chat_id, user_id) DO UPDATE SET n_rooms=excluded.n_rooms, ts=excluded.ts",
                    (chat_id, user_id, n, int(time.time())))


async def on_join(svc, bot, chat_id: int, user) -> bool:
    return await check(svc, bot, chat_id, user)


async def on_message(svc, bot, msg, role) -> None:
    if role >= Role.ADMIN or msg.chat_id > 0 or not msg.from_user or msg.from_user.is_bot:
        return
    if await free.is_free(svc.db, msg.chat_id, msg.from_user.id):   # 관리자가 믿고 풀어준 사람
        return
    await check(svc, bot, msg.chat_id, msg.from_user, spoke=True)


hooks.add_member_join_hook(on_join)
hooks.add_group_message_hook(on_message)
