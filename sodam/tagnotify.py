"""태그·답장 알림 (그룹헬프식). 그룹에서 @태그되거나 내 메시지에 답장이 오면 1:1 로 알려준다.

흐름: 관리 검사를 통과한 그룹 메시지 → (handlers.GROUP_MESSAGE_HOOKS) on_group_message →
  대상 모으기(@username 은 DB 상 그 방 멤버만 / text_mention / 답장 원글 작성자) →
  제외(보낸 사람·봇·알림 끔·1:1 안 시작·1:1 차단) → 한도 → 지금도 방 멤버인지(get_chat_member, 10분 캐시) → 1:1 발송.
한도: 메시지당 5명 · 수신자별 같은 방 60초 1회 · 수신자별 시간당 20회 · 보낸 사람 분당 10회.
1:1 시작 여부 = 봇에게 1:1 로 말한 적 있음(on_private 가 members(chat_id=user_id) 를 남김) 또는 🔔 태그 알림 메뉴를 연 적 있음.
보내다 Forbidden(봇 차단)이면 blocked_at 을 남기고, 그 뒤로 다시 1:1 을 쓰기 전까지는 안 보낸다.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import Forbidden, TelegramError

from . import db as dbmod
from .hooks import add_group_message_hook
from .settings import register_setting
from .util import esc, mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

MAX_RECIPIENTS = 5          # 메시지 하나당
SAME_CHAT_GAP = 60          # 수신자별 같은 방 알림 간격(초)
PER_HOUR = 20               # 수신자별 시간당
SENDER_PER_MIN = 10         # 보낸 사람 기준 분당
MEMBER_TTL = 600            # get_chat_member 결과 캐시(초)
PREVIEW_CHARS = 200
MAX_CACHE = 5000

MENTION, REPLY = "mention", "reply"
TITLES = {MENTION: "🔔 <b>태그되었어요</b>", REPLY: "↩️ <b>답장이 왔어요</b>"}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # 내용 속 링크 미리보기가 알림을 덮지 않게
IN_CHAT = {"member", "administrator", "creator"}
MEDIA = [("photo", "📷 사진"), ("video", "🎬 영상"), ("animation", "🎞 GIF"), ("sticker", "스티커"),
         ("voice", "🎤 음성 메시지"), ("video_note", "📹 영상 메시지"), ("audio", "🎵 오디오"), ("document", "📎 파일")]

register_setting("tag_notify", True, "태그·답장 알림")
dbmod.register_schema("""
CREATE TABLE IF NOT EXISTS tag_dm (
    user_id    INTEGER PRIMARY KEY,
    started_at INTEGER,
    blocked_at INTEGER
);
CREATE TABLE IF NOT EXISTS tag_optout (
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);
""", migrate={"tag_optout": "composite"})


# ── 저장 ──────────────────────────────────────────────────
async def mark_started(db, user_id: int) -> None:
    """1:1 에서 메뉴를 열었다 = 1:1 가능. 예전의 차단 표시도 지운다."""
    await db._write("INSERT INTO tag_dm(user_id, started_at) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET started_at=excluded.started_at, blocked_at=NULL",
                    (user_id, dbmod.now()))


async def mark_blocked(db, user_id: int) -> None:
    await db._write("INSERT INTO tag_dm(user_id, blocked_at) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET blocked_at=excluded.blocked_at", (user_id, dbmod.now()))


async def dm_ok(db, user_id: int) -> bool:
    row = await db._one("SELECT started_at, blocked_at FROM tag_dm WHERE user_id=?", (user_id,))
    dm = await db._one("SELECT last_seen FROM members WHERE chat_id=? AND user_id=?", (user_id, user_id))
    started = max((row["started_at"] or 0) if row else 0, (dm["last_seen"] or 0) if dm else 0)
    blocked = row["blocked_at"] if row else None
    return started > 0 and not (blocked and blocked >= started)


async def opted_out(db, user_id: int, chat_id: int) -> bool:
    return await db._one("SELECT 1 FROM tag_optout WHERE user_id=? AND chat_id=?", (user_id, chat_id)) is not None


async def set_opt_out(db, user_id: int, chat_id: int, off: bool) -> None:
    """목표값 방식: 몇 번 눌러도 결과가 같다."""
    if off:
        await db._write("INSERT OR IGNORE INTO tag_optout(user_id, chat_id) VALUES(?, ?)", (user_id, chat_id))
    else:
        await db._write("DELETE FROM tag_optout WHERE user_id=? AND chat_id=?", (user_id, chat_id))


async def user_groups(db, user_id: int, limit: int = 20) -> list:
    """DB 에 멤버로 남아있는 그룹 (최근 말한 순)."""
    return await db._all(
        "SELECT m.chat_id, c.title, EXISTS(SELECT 1 FROM tag_optout o WHERE o.user_id=m.user_id "
        "AND o.chat_id=m.chat_id) AS off FROM members m JOIN chats c ON c.chat_id=m.chat_id "
        "WHERE m.user_id=? AND m.chat_id<0 ORDER BY m.last_seen DESC LIMIT ?", (user_id, limit))


async def _member_by_username(db, chat_id: int, username: str):
    return await db._one(
        "SELECT u.user_id, u.is_bot FROM users u JOIN members m ON m.user_id=u.user_id AND m.chat_id=? "
        "WHERE u.username=? COLLATE NOCASE ORDER BY u.updated_at DESC LIMIT 1", (chat_id, username))


# ── 한도 · 캐시 (메모리) ──────────────────────────────────
@dataclass
class State:
    clock: object = time.monotonic
    sent: dict[int, deque] = field(default_factory=dict)       # 수신자 → (시각, 방)
    senders: dict[int, deque] = field(default_factory=dict)    # 보낸 사람 → 시각
    members: dict[tuple[int, int], tuple[bool, float]] = field(default_factory=dict)

    def _recent(self, q: deque, window: float, now: float) -> deque:
        while q and now - (q[0][0] if isinstance(q[0], tuple) else q[0]) > window:
            q.popleft()
        return q

    def recipient_ok(self, uid: int, cid: int) -> bool:
        now = self.clock()
        q = self._recent(self.sent.setdefault(uid, deque()), 3600, now)
        return len(q) < PER_HOUR and not any(c == cid and now - t < SAME_CHAT_GAP for t, c in q)

    def sender_ok(self, sid: int) -> bool:
        return len(self._recent(self.senders.setdefault(sid, deque()), 60, self.clock())) < SENDER_PER_MIN

    def record(self, uid: int, cid: int, sid: int) -> None:
        now = self.clock()
        self.sent[uid].append((now, cid))
        self.senders[sid].append(now)


def state(svc: Services) -> State:
    st = getattr(svc, "_tagnotify", None)
    if st is None:
        st = State()
        svc._tagnotify = st  # Services 는 슬롯 없는 dataclass 라 속성을 붙일 수 있다
    return st


async def still_member(st: State, bot, chat_id: int, user_id: int) -> bool:
    now = st.clock()
    hit = st.members.get((chat_id, user_id))
    if hit and now - hit[1] < MEMBER_TTL:
        return hit[0]
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        ok = str(m.status) in IN_CHAT or (str(m.status) == "restricted" and bool(getattr(m, "is_member", False)))
    except TelegramError:
        ok = False  # 없는 사용자 등
    if len(st.members) >= MAX_CACHE:
        st.members.clear()
    st.members[(chat_id, user_id)] = (ok, now)
    return ok


# ── 메시지 해석 ───────────────────────────────────────────
def _entity_text(text: str, e) -> str:
    """텔레그램 entity 위치는 UTF-16 단위 (이모지가 앞에 있으면 파이썬 글자 위치와 어긋남)."""
    raw = text.encode("utf-16-le")
    return raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", "ignore")


def message_link(chat_id: int, username: str | None, msg_id: int) -> str | None:
    """공개 그룹 t.me/<username>/<id>, 비공개 슈퍼그룹 t.me/c/<-100 뗀 ID>/<id>. 일반 그룹은 링크 없음."""
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{msg_id}" if s.startswith("-100") else None


def _is_topic_root(msg) -> bool:
    """포럼 토픽 안의 글은 토픽 시작 메시지에 '답장'한 것으로 온다 → 답장 알림 대상 아님."""
    r = msg.reply_to_message
    if getattr(r, "forum_topic_created", None):
        return True
    return bool(getattr(msg, "is_topic_message", False)) and r.message_id == getattr(msg, "message_thread_id", None)


async def targets(svc: Services, msg) -> list[tuple[int, str]]:
    """(user_id, 종류) 목록. 태그가 답장보다 먼저, 같은 사람은 한 번만."""
    text = msg.text or msg.caption or ""
    ents = (msg.entities or ()) if msg.text else (msg.caption_entities or ())
    out: dict[int, str] = {}
    for e in ents:
        if str(e.type) == "text_mention" and e.user and not e.user.is_bot:
            out.setdefault(e.user.id, MENTION)
        elif str(e.type) == "mention":
            name = _entity_text(text, e).lstrip("@")
            row = await _member_by_username(svc.db, msg.chat_id, name) if name else None
            if row and not row["is_bot"]:
                out.setdefault(row["user_id"], MENTION)
    r = msg.reply_to_message
    if r and r.from_user and not r.from_user.is_bot and not _is_topic_root(msg):
        out.setdefault(r.from_user.id, REPLY)
    return list(out.items())


def _preview(msg) -> str:
    text = msg.text or msg.caption or ""
    if not text:
        return next((label for attr, label in MEDIA if getattr(msg, attr, None)), "(내용 없음)")
    return esc(text[:PREVIEW_CHARS]) + ("…" if len(text) > PREVIEW_CHARS else "")


def _sender_label(msg) -> str:
    if getattr(msg, "sender_chat", None):
        return "익명 관리자"
    return mention(msg.from_user.id, user_name(msg.from_user))


def build(kind: str, msg, title: str) -> tuple[str, InlineKeyboardMarkup]:
    chat = getattr(msg, "chat", None)
    text = (f"{TITLES[kind]}\n\n"
            f"보낸 사람: {_sender_label(msg)}\n"
            f"그룹: <b>{esc(title)}</b>\n"
            f"<blockquote>{_preview(msg)}</blockquote>")
    rows = []
    link = message_link(msg.chat_id, getattr(chat, "username", None), msg.message_id)
    if link:
        rows.append([InlineKeyboardButton("➡️ 메시지로 이동", url=link)])
    rows.append([InlineKeyboardButton("🔕 이 그룹 알림 끄기", callback_data=f"m:tn:0:{msg.chat_id}:dm")])
    return text, InlineKeyboardMarkup(rows)


# ── 훅 ────────────────────────────────────────────────────
async def on_group_message(svc: Services, bot, msg, role) -> None:
    chat_id, sender = msg.chat_id, msg.from_user
    anonymous = bool(getattr(msg, "sender_chat", None))
    if chat_id >= 0 or not sender or (sender.is_bot and not anonymous):
        return
    if not (await svc.db.get_settings(chat_id)).get("tag_notify", True):
        return
    text = msg.text or msg.caption or ""
    from . import commands  # 늦게 import (commands → menu → panels → 여기 순환 방지)
    if text and commands.parse(text, bot.username):
        return  # .경고 @누구 같은 명령어는 알림 안 보냄
    found = [(uid, kind) for uid, kind in await targets(svc, msg) if uid not in (sender.id, bot.id)]
    if not found:
        return
    st, sid = state(svc), (msg.sender_chat.id if anonymous else sender.id)
    title = None
    for uid, kind in found[:MAX_RECIPIENTS]:
        if await opted_out(svc.db, uid, chat_id) or not await dm_ok(svc.db, uid):
            continue
        if not st.recipient_ok(uid, chat_id) or not await still_member(st, bot, chat_id, uid):
            continue
        # 한도 확인과 기록 사이에 await 가 없어야 동시에 온 메시지가 한도를 넘지 못한다
        if not st.sender_ok(sid):
            return
        if not st.recipient_ok(uid, chat_id):
            continue
        st.record(uid, chat_id, sid)
        if title is None:
            title = getattr(getattr(msg, "chat", None), "title", None) or await _title(svc, chat_id)
        body, kb = build(kind, msg, title)
        try:
            await bot.send_message(uid, body, parse_mode="HTML", reply_markup=kb, link_preview_options=NO_PREVIEW)
        except Forbidden:
            await mark_blocked(svc.db, uid)  # 봇을 차단했거나 1:1 을 시작 안 함 → 다시 1:1 올 때까지 안 보냄
        except TelegramError as e:
            log.info("tag notify to %s failed: %s", uid, e)


async def _title(svc: Services, chat_id: int) -> str:
    row = await svc.db._one("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
    return row["title"] if row and row["title"] else "그룹"


add_group_message_hook(on_group_message)
