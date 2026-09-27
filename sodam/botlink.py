"""🤝 다른 봇 연동 (방마다 선택, 기본 꺼짐) — 텔레그램 Bot-to-Bot Communication Mode (Bot API 10.0, 2026-05-08).

텔레그램 규칙 (core.telegram.org/api/bots/bot-to-bot):
- 그룹에서 봇은 다른 봇의 글을 '/명령@받는봇' 이거나 자기 글에 단 답장일 때 받는다 — 두 봇 중 **하나라도** BotFather 에서
  Bot-to-Bot 모드를 켰으면. 모드를 켠 봇이 그 방 관리자(또는 Privacy Mode 꺼짐)면 다른 봇 글을 **전부** 받는다.
- 봇은 다른 봇의 인라인 버튼을 못 누른다 (messages.getBotCallbackAnswer 는 'Only users can use this method').
- 무한 주고받기 방지는 봇 책임 (중복 제거·방/봇별 속도 제한·주고받기 깊이·시간 제한).

그래서 여기서:
- 👀 observe: 다른 봇 글을 botlink_msgs 에 기록(7일) → AI 읽기 도구 other_bot_results (봇 글 = 데이터, ctx.tainted).
  🎮 믿는 봇이 사람 글에 단 답장(게임 결과)은 장시간 게임 알림(gametime)의 게임 활동으로도 셈.
- 🤖 interact: 관리자 AI 도구 bot_command 로 믿는 봇에게 '/명령@그봇 인자' 한 줄 (처음 쓰는 봇·명령은 방에 확인 카드).
  intent(play·skip…)+query 면 🎓 봇 명령 프로필(sodam/botskills.py)에서 그 봇의 명령을 골라 씀.
- 소담은 봇 글에 절대 자동으로 반응하지 않는다: handlers.on_group_message 가 봇 글은 맨 앞에서 hooks.BOT_MESSAGE_HOOKS
  (이 파일)만 부르고 끝냄 → 관리·명령·게임·AI·끼어들기·태그 알림·알림 규칙 전부 안 탐. 보내는 건 사람(관리자) 요청으로만,
  봇 글을 읽은 답변(tainted)에선 못 보내고, 답변 한 번에 1번, 방 분당/하루 한도, 방·봇 쌍 분당 주고받기 한도, 연속 깊이 한도.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import Bot, LinkPreviewOptions
from telegram.error import TelegramError

from . import gametime, hooks
from .db import register_schema
from .security import scan
from .settings import register_setting

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

MODES = {"off": "❌ 끔", "observe": "👀 기록만", "interact": "🤖 명령까지"}
ACTIVE = ("observe", "interact")
STATUS = {"seen": "👀 기록만", "trusted": "✅ 믿는 봇", "ignored": "🙈 무시"}

IN_PER_MIN = 30        # 방마다 분당 기록할 다른 봇 글 (넘으면 버림 — 봇끼리 폭주해도 DB·CPU 안전)
OUT_PER_MIN = 3        # 방마다 분당 보내는 명령
OUT_PER_DAY = 30       # 방마다 하루 보내는 명령 (DB 로 셈 → 재시작해도)
PAIR_PER_MIN = 8       # 방·봇 쌍 분당 주고받기(받은 글 + 보낸 명령) — 넘으면 보내지 않음
MAX_DEPTH = 3          # '보냄 → 그 봇이 답함 → 또 보냄' 연속 깊이
DEPTH_WINDOW = 60      # 이 시간 안에 답이 오고 또 보내면 같은 사슬
WAIT_SECONDS = 6.0     # 명령 뒤 그 봇의 답(내 글에 단 답장)을 기다리는 시간
MAX_TEXT = 1000
MAX_BOTS = 50          # 방마다 기억하는 봇 수
KEEP_DAYS = 7

register_setting("botlink_mode", "off", "다른 봇 연동",
                 choices={"off": "off", "끔": "off", "observe": "observe", "기록": "observe", "기록만": "observe",
                          "interact": "interact", "명령": "interact"},
                 choice_labels={"off": "끔", "observe": "기록만", "interact": "명령까지"})

register_schema("""
CREATE TABLE IF NOT EXISTS botlink_bots (
    chat_id    INTEGER NOT NULL,
    bot_id     INTEGER NOT NULL,
    username   TEXT,
    name       TEXT,
    status     TEXT NOT NULL DEFAULT 'seen',
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    msgs       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, bot_id)
);
CREATE TABLE IF NOT EXISTS botlink_msgs (
    id        INTEGER PRIMARY KEY,
    chat_id   INTEGER NOT NULL,
    bot_id    INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    text      TEXT NOT NULL,
    to_user   INTEGER,
    to_us     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_botlink_msgs ON botlink_msgs(chat_id, bot_id, ts);
CREATE TABLE IF NOT EXISTS botlink_cmds (
    chat_id  INTEGER NOT NULL,
    bot_id   INTEGER NOT NULL,
    command  TEXT NOT NULL,
    added_by INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (chat_id, bot_id, command)
);
CREATE TABLE IF NOT EXISTS botlink_sent (
    id      INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    bot_id  INTEGER NOT NULL,
    msg_id  INTEGER,
    text    TEXT NOT NULL,
    by_user INTEGER NOT NULL,
    ts      INTEGER NOT NULL
);
-- 🎓 봇 명령 프로필 (sodam/botskills.py): 명령·인자 모양·어디서 알았나(preset|manual|seen|helper)·하는 일(intent)
CREATE TABLE IF NOT EXISTS botlink_skills (
    chat_id   INTEGER NOT NULL,
    bot_id    INTEGER NOT NULL,
    command   TEXT NOT NULL,
    args_hint TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL,
    intent    TEXT NOT NULL DEFAULT 'other',
    updated   INTEGER NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, bot_id, command)
);
""", migrate={"botlink_bots": "composite", "botlink_msgs": "drop", "botlink_cmds": "composite", "botlink_sent": "plain",
              "botlink_skills": "composite"})

# 다른 봇 글을 기록한 뒤 불림 (sodam/botskills.py 명령 배우기): async fn(svc, bot, msg, status, now). 방에 보내지 말 것.
SEEN_HOOKS: list = []


@dataclass
class _Pair:
    events: deque = field(default_factory=deque)   # 받은 글·보낸 명령 시각 (분당 주고받기)
    last_out: float = 0.0
    last_reply: float = 0.0                         # 그 봇이 내 글에 답한 시각
    depth: int = 0


@dataclass
class _State:
    inbound: dict = field(default_factory=dict)    # chat → deque[시각]
    outbound: dict = field(default_factory=dict)   # chat → deque[시각]
    pairs: dict = field(default_factory=dict)      # (chat, bot) → _Pair
    waiters: dict = field(default_factory=dict)    # (chat, 내 글 ID) → Future[str]
    replies: dict = field(default_factory=dict)    # (chat, 내 글 ID) → 그 봇의 답 (기다리기 전에 온 빠른 답, 최근 200개)
    dropped: int = 0
    pruned: float = 0.0


def state(svc: Services) -> _State:
    st = getattr(svc, "_botlink", None)
    if st is None:
        st = svc._botlink = _State()
    return st


def _window(q: deque, now: float, span: float = 60.0) -> deque:
    while q and now - q[0] >= span:
        q.popleft()
    return q


def pair(st: _State, chat_id: int, bot_id: int) -> _Pair:
    return st.pairs.setdefault((chat_id, bot_id), _Pair())


def describe(msg) -> str:
    """봇 글 → 기록할 글자: 글·캡션 + 🎲 값 + 버튼 글자 (카지노 봇은 결과를 버튼에 쓰기도 함)."""
    parts = [msg.text or msg.caption or ""]
    dice = getattr(msg, "dice", None)
    if dice:
        parts.append(f"{dice.emoji} = {dice.value}")
    kb = getattr(msg, "reply_markup", None)
    labels = [b.text for row in (getattr(kb, "inline_keyboard", None) or ()) for b in row][:8]
    if labels:
        parts.append("[버튼: " + " | ".join(labels) + "]")
    if not any(parts) and (getattr(msg, "photo", None) or getattr(msg, "animation", None)):
        parts.append("[사진/움짤]")
    return "\n".join(p for p in parts if p)[:MAX_TEXT]


async def active(svc: Services, chat_id: int, want: tuple = ACTIVE) -> dict | None:
    """이 방에서 켜져 있고 이용 기간이면 설정 dict, 아니면 None."""
    if chat_id >= 0:
        return None
    s = await svc.db.get_settings(chat_id)
    if s["botlink_mode"] not in want or not await svc.paid_features(chat_id):
        return None
    return s


# ── 받기 (hooks.BOT_MESSAGE_HOOKS: handlers 가 다른 봇 글에서만 부름, 절대 답하지 않음) ──
async def on_bot_message(svc: Services, bot: Bot, msg) -> None:
    user, cid = msg.from_user, msg.chat_id
    if user is None or not user.is_bot or user.id == bot.id:   # 내 글은 기록도 안 함
        return
    s = await active(svc, cid)
    if s is None:
        return
    st, now = state(svc), time.time()
    q = _window(st.inbound.setdefault(cid, deque()), now)
    if len(q) >= IN_PER_MIN:                                  # 폭주: 이 분은 버림 (DB 도 안 씀)
        st.dropped += 1
        return
    q.append(now)
    r = msg.reply_to_message
    to_us = bool(r and r.from_user and r.from_user.id == bot.id)
    to_user = r.from_user.id if r and r.from_user and not r.from_user.is_bot else None
    if to_user is None:   # 버튼 게임 결과는 답장 대신 이름 링크(text_mention)로 사람을 부르기도 함
        to_user = next((e.user.id for e in (msg.entities or msg.caption_entities or ())
                        if e.type == "text_mention" and getattr(e, "user", None) and not e.user.is_bot), None)
    text, ts = describe(msg), int(now)
    name = " ".join(x for x in (user.first_name, user.last_name) if x)[:64]
    prune = now - st.pruned > 3600
    if prune:
        st.pruned = now

    def work(c):
        known = c.execute("SELECT 1 FROM botlink_bots WHERE chat_id=? AND bot_id=?", (cid, user.id)).fetchone()
        if not known and c.execute("SELECT COUNT(*) FROM botlink_bots WHERE chat_id=?", (cid,)).fetchone()[0] >= MAX_BOTS:
            return "full"
        c.execute("INSERT INTO botlink_bots(chat_id, bot_id, username, name, first_seen, last_seen) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(chat_id, bot_id) DO UPDATE SET username=excluded.username, name=excluded.name, "
                  "last_seen=excluded.last_seen", (cid, user.id, user.username, name, ts, ts))
        status = c.execute("SELECT status FROM botlink_bots WHERE chat_id=? AND bot_id=?", (cid, user.id)).fetchone()[0]
        if status != "ignored" and text:
            if c.execute("INSERT OR IGNORE INTO botlink_msgs(chat_id, bot_id, msg_id, ts, text, to_user, to_us) "
                         "VALUES(?,?,?,?,?,?,?)", (cid, user.id, msg.message_id, ts, text, to_user, int(to_us))).rowcount:
                c.execute("UPDATE botlink_bots SET msgs=msgs+1 WHERE chat_id=? AND bot_id=?", (cid, user.id))
        if prune:
            c.execute("DELETE FROM botlink_msgs WHERE ts < ?", (ts - KEEP_DAYS * 86400,))
            c.execute("DELETE FROM botlink_sent WHERE ts < ?", (ts - KEEP_DAYS * 86400,))
        return status

    status = await svc.db.atomic(work)
    if status in ("full", "ignored"):
        return
    p = pair(st, cid, user.id)
    _window(p.events, now).append(now)
    if to_us:
        p.last_reply = now
        fut = st.waiters.pop((cid, r.message_id), None)
        if fut is not None and not fut.done():
            fut.set_result(text)
        else:
            st.replies[(cid, r.message_id)] = text
            while len(st.replies) > 200:
                st.replies.pop(next(iter(st.replies)))
    if to_user and status == "trusted" and s["gt_enabled"]:     # 🎮 믿는 게임봇의 결과 = 그 사람의 게임 활동
        await gametime.record(svc.db, cid, to_user, s["gt_gap"], ts)
    for fn in SEEN_HOOKS:
        try:
            await fn(svc, bot, msg, status, now)
        except Exception:
            log.exception("botlink seen hook %s failed", getattr(fn, "__name__", fn))


async def on_bot_edit(svc: Services, bot: Bot, msg) -> None:
    """봇이 결과 글을 고치면(카지노 판 진행 등) 기록한 글자만 바꿈. 새 글로 세지 않음."""
    user = msg.from_user
    if user is None or not user.is_bot or user.id == bot.id or await active(svc, msg.chat_id) is None:
        return
    st, now = state(svc), time.time()
    q = _window(st.inbound.setdefault(msg.chat_id, deque()), now)
    if len(q) >= IN_PER_MIN:
        st.dropped += 1
        return
    q.append(now)
    await svc.db._write("UPDATE botlink_msgs SET text=?, ts=? WHERE chat_id=? AND msg_id=? AND bot_id=?",
                        (describe(msg), int(now), msg.chat_id, msg.message_id, user.id))


hooks.add_bot_message_hook(on_bot_message)
hooks.add_bot_edit_hook(on_bot_edit)


# ── 조회 ──────────────────────────────────────────────────
async def bots(db, chat_id: int) -> list:
    return await db._all("SELECT * FROM botlink_bots WHERE chat_id=? ORDER BY status='ignored', last_seen DESC", (chat_id,))


async def get_bot(db, chat_id: int, bot_id: int):
    return await db._one("SELECT * FROM botlink_bots WHERE chat_id=? AND bot_id=?", (chat_id, bot_id))


async def find_bot(db, chat_id: int, query: str):
    """@아이디·이름(일부)·ID → 봇 행. 여러 개 맞으면 None 과 후보 목록."""
    q = query.strip().lstrip("@").lower()
    rows = [r for r in await bots(db, chat_id) if r["status"] != "ignored"]
    if not q:
        return None, rows
    exact = [r for r in rows if (r["username"] or "").lower() == q or str(r["bot_id"]) == q]
    if exact:
        return exact[0], exact
    part = [r for r in rows if q in (r["username"] or "").lower() or q in (r["name"] or "").lower()]
    return (part[0] if len(part) == 1 else None), part


async def recent(db, chat_id: int, bot_id: int | None = None, limit: int = 10, since: int | None = None) -> list:
    since = since if since is not None else int(time.time()) - 86400
    where, args = "m.chat_id=? AND m.ts>=? AND COALESCE(b.status,'seen')!='ignored'", [chat_id, since]
    if bot_id is not None:
        where += " AND m.bot_id=?"
        args.append(bot_id)
    return await db._all(
        "SELECT m.*, b.username, b.name, u.first_name AS to_name FROM botlink_msgs m "
        "LEFT JOIN botlink_bots b ON b.chat_id=m.chat_id AND b.bot_id=m.bot_id "
        f"LEFT JOIN users u ON u.user_id=m.to_user WHERE {where} ORDER BY m.ts DESC, m.id DESC LIMIT ?", (*args, limit))


async def set_status(db, chat_id: int, bot_id: int, status: str) -> None:
    def work(c):
        c.execute("UPDATE botlink_bots SET status=? WHERE chat_id=? AND bot_id=?", (status, chat_id, bot_id))
        if status == "ignored":   # 무시 = 기록도 지움, 허용 명령·배운 명령도 지움
            c.execute("DELETE FROM botlink_msgs WHERE chat_id=? AND bot_id=?", (chat_id, bot_id))
            c.execute("DELETE FROM botlink_cmds WHERE chat_id=? AND bot_id=?", (chat_id, bot_id))
            c.execute("DELETE FROM botlink_skills WHERE chat_id=? AND bot_id=?", (chat_id, bot_id))
        elif status == "seen":    # 믿음 해제 = 허용 명령도 지움
            c.execute("DELETE FROM botlink_cmds WHERE chat_id=? AND bot_id=?", (chat_id, bot_id))
    await db.atomic(work)


async def commands(db, chat_id: int, bot_id: int) -> list[str]:
    return [r["command"] for r in await db._all(
        "SELECT command FROM botlink_cmds WHERE chat_id=? AND bot_id=? ORDER BY command", (chat_id, bot_id))]


async def approve(db, chat_id: int, bot_id: int, head: str, by: int) -> None:
    await db._write("INSERT OR IGNORE INTO botlink_cmds(chat_id, bot_id, command, added_by, ts) VALUES(?,?,?,?,?)",
                    (chat_id, bot_id, head, by, int(time.time())))


async def approved(db, chat_id: int, bot_id: int, head: str) -> bool:
    return bool(await db._one("SELECT 1 FROM botlink_cmds WHERE chat_id=? AND bot_id=? AND command=?",
                              (chat_id, bot_id, head)))


# ── 보내기 ────────────────────────────────────────────────
MAX_ARG = 64           # 보통 인자
MAX_ARG_WIDE = 100     # 재생·검색(play/search): '가수 - 곡 (feat. 누구) 라이브' 같은 곡명이 64자를 넘고, 유튜브 주소만 43자라
                       # 곡명+주소가 들어가게. 여전히 한 줄·링크는 유튜브 두 모양만.
CMD_RE = re.compile(r"/([A-Za-z0-9_]{1,32})(?:@\w{1,64})?(?: (.{1,%d}))?" % MAX_ARG_WIDE)
BAD_ARG = re.compile(r"[@/<>\\`]|https?:|t\.me", re.I)
# 재생·검색에서만 허용하는 링크: https://youtu.be/<11자> · https://www.youtube.com/watch?v=<11자> (다른 쿼리·도메인 X)
YT_LINK = re.compile(r"(?<!\S)https://(?:youtu\.be/|www\.youtube\.com/watch\?v=)[A-Za-z0-9_-]{11}(?!\S)")


def build(raw: str, username: str, wide: bool = False) -> tuple[str, str] | None:
    """AI·관리자가 준 명령 → ('/머리', '/머리@그봇 인자'). 한 줄 · '/' 명령 하나 · 인자 64자 · 멘션·링크·다른 명령 없음.
    wide(재생·검색) = 인자 100자 + 유튜브 링크(YT_LINK)만 허용."""
    raw = " ".join(str(raw).split())
    m = CMD_RE.fullmatch(raw)
    if not m or not username:
        return None
    args = m.group(2) or ""
    if len(args) > (MAX_ARG_WIDE if wide else MAX_ARG):
        return None
    rest = YT_LINK.sub(" ", args) if wide else args
    if BAD_ARG.search(rest) or scan(rest).blocked:
        return None
    return "/" + m.group(1).lower(), f"/{m.group(1)}@{username}" + (f" {args}" if args else "")


async def refuse_reason(svc: Services, chat_id: int, row) -> str | None:
    """보내면 안 되는 이유 (None = 보내도 됨). 한도는 여기서 '확인'만, 차지는 reserve."""
    if await active(svc, chat_id, ("interact",)) is None:
        return "이 방은 다른 봇에게 명령 보내기가 꺼져 있음 (관리자 1:1 메뉴 → 🤝 다른 봇 연동 → 🤖 명령까지)."
    if row is None or row["status"] != "trusted":
        return "믿는 봇으로 표시된 봇에게만 보낼 수 있음 (관리자 1:1 메뉴 → 🤝 다른 봇 연동 → 봇 선택 → ✅ 믿는 봇)."
    if not row["username"]:
        return "이 봇은 @아이디가 없어서 명령을 보낼 수 없음."
    day = await svc.db._one("SELECT COUNT(*) AS n FROM botlink_sent WHERE chat_id=? AND ts>=?",
                            (chat_id, int(time.time()) - 86400))
    if day["n"] >= OUT_PER_DAY:
        return f"이 방은 오늘 다른 봇 명령 한도({OUT_PER_DAY}번)를 다 썼음."
    return None


def reserve(svc: Services, chat_id: int, bot_id: int) -> str | None:
    """분당·쌍·깊이 한도를 확인하고 바로 차지 (await 없음 → 동시에 두 요청이 와도 한도를 못 넘음)."""
    st, now = state(svc), time.time()
    out = _window(st.outbound.setdefault(chat_id, deque()), now)
    if len(out) >= OUT_PER_MIN:
        return f"다른 봇 명령은 방마다 1분에 {OUT_PER_MIN}번까지. 잠시 뒤에."
    p = pair(st, chat_id, bot_id)
    if len(_window(p.events, now)) >= PAIR_PER_MIN:
        return "그 봇과 1분 안에 너무 많이 주고받았음 (봇끼리 반복 방지). 잠시 뒤에."
    chained = p.last_out and p.last_reply >= p.last_out and now - p.last_reply < DEPTH_WINDOW
    depth = p.depth + 1 if chained else 1
    if depth > MAX_DEPTH:
        return f"그 봇과 연달아 {MAX_DEPTH}번 주고받았음 (봇끼리 반복 방지). {DEPTH_WINDOW}초 뒤에."
    p.depth, p.last_out = depth, now
    out.append(now)
    p.events.append(now)
    return None


async def send(svc: Services, bot: Bot, chat_id: int, row, text: str, by_uid: int) -> int | None:
    """한 줄 명령을 방에 보냄 (답장 아님 · 미리보기 끔). 보낸 글 ID. 실패면 None."""
    try:
        sent = await bot.send_message(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramError as e:
        log.warning("botlink send failed %s: %s", chat_id, e)
        return None
    mid = getattr(sent, "message_id", None)
    await svc.db._write("INSERT INTO botlink_sent(chat_id, bot_id, msg_id, text, by_user, ts) VALUES(?,?,?,?,?,?)",
                        (chat_id, row["bot_id"], mid, text, by_uid, int(time.time())))
    await svc.db.log_mod(chat_id, by_uid, row["bot_id"], "botlink_send", text[:100])
    return mid


async def wait_reply(svc: Services, chat_id: int, msg_id: int | None, timeout: float | None = None) -> str | None:
    """보낸 명령에 그 봇이 단 답장 글자 (없으면 None). on_bot_message 가 채움."""
    if msg_id is None:
        return None
    st = state(svc)
    if (chat_id, msg_id) in st.replies:     # 보내는 사이 이미 답이 옴
        return st.replies.pop((chat_id, msg_id))
    fut = asyncio.get_running_loop().create_future()
    st.waiters[(chat_id, msg_id)] = fut
    try:
        return await asyncio.wait_for(fut, WAIT_SECONDS if timeout is None else timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        st.waiters.pop((chat_id, msg_id), None)


async def stats(db, chat_id: int) -> dict:
    since = int(time.time()) - KEEP_DAYS * 86400
    m = await db._one("SELECT COUNT(*) AS n FROM botlink_msgs WHERE chat_id=? AND ts>=?", (chat_id, since))
    s = await db._one("SELECT COUNT(*) AS n FROM botlink_sent WHERE chat_id=? AND ts>=?", (chat_id, since))
    return {"msgs": m["n"], "sent": s["n"]}
