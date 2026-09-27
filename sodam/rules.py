"""알림 규칙: '누가 입금 얘기하면 나한테 알려줘' 같은 관리자 규칙 (방마다, 🔔 메뉴·AI 도구 alert_rule).

규칙은 코드가 아니라 데이터 — 정해진 부품만 조립한다 (AI 는 말 → 부품 번역만, 저장은 만든 관리자의 확인 버튼):
  언제(trig): keyword 메시지에 낱말 · user 특정 사람이 말함 · join 누가 들어옴 · quiet 방이 N시간 조용함
  누구(who):  all / newbie (들어온 지 24시간 안 된 사람, keyword 만)
  하면(action): dm 만든 관리자 1:1 · call 방에서 만든 관리자 호출 · post 방에 정해진 글
실행은 코드만 (AI 없음). 쿨다운·하루 상한은 한 문장 UPDATE 로 '먼저 차지한 알림만' 나가게, 만든 사람이 더는
관리자가 아니면 규칙을 끈다. 뮤트·밴 같은 무거운 동작은 부품에 없다 (제재는 기존 확인 버튼으로만).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING

from telegram import Bot, LinkPreviewOptions
from telegram.error import TelegramError

from . import hooks
from .db import register_schema
from .util import esc, mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

TRIGGERS = {"keyword": "🔑 낱말이 나오면", "user": "👤 이 사람이 말하면", "join": "🚪 누가 들어오면", "quiet": "💤 방이 조용하면"}
ACTIONS = {"dm": "나한테 1:1 알림", "call": "방에서 나를 호출", "post": "방에 정해진 글"}
MAX_RULES = 20
DAILY_CAP = 30            # 규칙 하나가 하루에 울리는 최대 횟수
NEWBIE_SECONDS = 86400
KEYWORD_MIN, KEYWORD_MAX = 2, 30
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

register_schema("""
CREATE TABLE IF NOT EXISTS alert_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    trig       TEXT NOT NULL,
    arg        TEXT NOT NULL DEFAULT '',      -- keyword: 낱말 · user: 사람 ID · quiet: 시간
    who        TEXT NOT NULL DEFAULT 'all',
    action     TEXT NOT NULL DEFAULT 'dm',
    text       TEXT NOT NULL DEFAULT '',      -- post 로 올릴 글
    cooldown   INTEGER NOT NULL DEFAULT 10,   -- 분
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_fired INTEGER NOT NULL DEFAULT 0,
    day        TEXT NOT NULL DEFAULT '',
    fired      INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_rules_chat ON alert_rules(chat_id);
""", migrate={"alert_rules": "plain"})


def norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def validate(trig: str, arg: str, action: str, text: str) -> str | None:
    """저장 전 검사. 문제면 이유."""
    if trig not in TRIGGERS or action not in ACTIONS:
        return "종류가 잘못됐어요."
    if trig == "keyword" and not KEYWORD_MIN <= len(norm(arg)) <= KEYWORD_MAX:
        return f"낱말은 {KEYWORD_MIN}~{KEYWORD_MAX}자로 해주세요."
    if trig == "user" and not arg.isdecimal():
        return "사람을 찾지 못했어요."
    if trig == "quiet" and not (arg.isdecimal() and 1 <= int(arg) <= 72):
        return "조용한 시간은 1~72시간으로 해주세요."
    if action == "post" and not text.strip():
        return "방에 올릴 글을 적어주세요."
    return None


async def describe(svc: Services, r) -> str:
    """사람 말로 한 줄 (카드·목록용)."""
    if r["trig"] == "keyword":
        when = f"'{r['arg']}' 이(가) 나오면" + (" (신규 입장자만)" if r["who"] == "newbie" else "")
    elif r["trig"] == "user":
        when = f"{await svc.db.first_name(int(r['arg'])) or r['arg']}님이 말하면"
    elif r["trig"] == "join":
        when = "누가 들어오면"
    else:
        when = f"방이 {r['arg']}시간 조용하면"
    what = ACTIONS[r["action"]] + (f" ('{r['text'][:30]}')" if r["action"] == "post" else "")
    return f"{when} → {what}"


async def add(svc: Services, chat_id: int, uid: int, spec: dict) -> int:
    rid = await svc.db._write(
        "INSERT INTO alert_rules(chat_id, created_by, trig, arg, who, action, text, cooldown, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (chat_id, uid, spec["trig"], str(spec.get("arg", ""))[:KEYWORD_MAX * 2], spec.get("who", "all"),
         spec["action"], str(spec.get("text", ""))[:300], max(1, min(int(spec.get("cooldown") or 10), 1440)),
         int(time.time())))
    await svc.db.log_mod(chat_id, uid, None, "setting", f"알림 규칙 #{rid} {spec['trig']}:{spec.get('arg', '')}")
    return rid


async def room_rules(db, chat_id: int, enabled_only: bool = False) -> list:
    return await db._all("SELECT * FROM alert_rules WHERE chat_id=?" + (" AND enabled=1" if enabled_only else "")
                         + " ORDER BY id", (chat_id,))


async def _claim(db, rid: int, now: int, day: str) -> bool:
    """쿨다운·하루 상한 안이면 이번 알림을 차지 (한 문장이라 동시에 와도 한 번만)."""
    def run(c):
        return c.execute(
            "UPDATE alert_rules SET last_fired=?, fired=CASE WHEN day=? THEN fired+1 ELSE 1 END, day=? "
            "WHERE id=? AND enabled=1 AND ?-last_fired >= cooldown*60 AND NOT (day=? AND fired>=?)",
            (now, day, day, rid, now, day, DAILY_CAP)).rowcount
    return bool(await db.atomic(run))


def _link(chat_id: int, msg_id: int | None) -> str:
    s = str(chat_id)
    return f"\n🔗 https://t.me/c/{s[4:]}/{msg_id}" if msg_id and s.startswith("-100") else ""


async def fire(svc: Services, bot: Bot, r, what: str, msg_id: int | None = None) -> bool:
    cid, creator = r["chat_id"], r["created_by"]
    if not await svc.paid_features(cid):
        return False
    if not await svc.perms.is_admin(bot, cid, creator):          # 권한은 울릴 때 다시 확인
        await svc.db._write("UPDATE alert_rules SET enabled=0 WHERE id=?", (r["id"],))
        return False
    now = int(time.time())
    if not await _claim(svc.db, r["id"], now, datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")):
        return False
    from .subscription import chat_title  # 늦게 import (순환 방지)
    name = await svc.db.first_name(creator) or "관리자"
    try:
        if r["action"] == "dm":
            await bot.send_message(creator, f"🔔 <b>{esc(await chat_title(svc, cid))}</b> · 규칙 #{r['id']}\n{what}"
                                   + _link(cid, msg_id), parse_mode="HTML", link_preview_options=NO_PREVIEW)
        elif r["action"] == "call":
            await bot.send_message(cid, f"🔔 {mention(creator, name)}님, {what}", parse_mode="HTML",
                                   link_preview_options=NO_PREVIEW)
        else:
            await bot.send_message(cid, esc(r["text"]), parse_mode="HTML", link_preview_options=NO_PREVIEW)
        return True
    except TelegramError as e:
        log.info("alert rule #%s send failed: %s", r["id"], e)   # 1:1 을 시작 안 한 관리자 등
        return False


async def on_message(svc: Services, bot: Bot, msg, role) -> None:
    rules = await room_rules(svc.db, msg.chat_id, enabled_only=True)
    if not rules:
        return
    user, text = msg.from_user, msg.text or msg.caption or ""
    body, who = norm(text), esc(user_name(user))
    preview = esc(" ".join(text.split())[:100])
    newbie = None
    for r in rules:
        if user.id == r["created_by"] or user.is_bot:
            continue
        if r["trig"] == "keyword" and norm(r["arg"]) in body:
            if r["who"] == "newbie":
                if newbie is None:
                    m = await svc.db.get_member(msg.chat_id, user.id)
                    newbie = bool(m and m["joined_at"] and time.time() - m["joined_at"] < NEWBIE_SECONDS)
                if not newbie:
                    continue
            await fire(svc, bot, r, f"'{esc(r['arg'])}' 나옴 — {who}: {preview}", msg.message_id)
        elif r["trig"] == "user" and r["arg"] == str(user.id):
            await fire(svc, bot, r, f"{who}님이 말했어요: {preview}", msg.message_id)


async def on_join(svc: Services, bot: Bot, chat_id: int, user) -> bool:
    for r in await room_rules(svc.db, chat_id, enabled_only=True):
        if r["trig"] == "join":
            await fire(svc, bot, r, f"{esc(user_name(user))}님이 들어왔어요 (ID <code>{user.id}</code>)")
    return False   # 입장은 막지 않음


async def check_quiet(svc: Services, bot: Bot, now: int | None = None) -> int:
    """10분마다: 조용함 규칙. 마지막 대화 뒤 N시간이 지났고 그 조용함에 아직 안 울렸으면 한 번."""
    now = now or int(time.time())
    n = 0
    for r in await svc.db._all("SELECT * FROM alert_rules WHERE trig='quiet' AND enabled=1"):
        row = await svc.db._one("SELECT MAX(ts) AS t FROM messages WHERE chat_id=? AND is_bot=0", (r["chat_id"],))
        last = (row and row["t"]) or 0
        if last and now - last >= int(r["arg"]) * 3600 and r["last_fired"] < last:
            n += await fire(svc, bot, r, f"방이 {r['arg']}시간째 조용해요")
    return n


hooks.add_group_message_hook(on_message)
hooks.add_member_join_hook(on_join)
