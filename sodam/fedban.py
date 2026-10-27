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
from telegram.error import BadRequest, TelegramError

from . import free, hooks, incidents, mediastore, persist
from .db import register_columns, register_schema
from .permissions import Role, may
from .settings import register_setting
from .util import esc, html_plain, html_truncate, mention, post_temp

log = logging.getLogger(__name__)

DAILY_LIMIT = 20
# 수배 카드 같은 사람·같은 방 다시 띄우는 간격 (초)
CARD_GAP = 600
MEDIA_KINDS = ("photo", "video", "animation", "document")
MODES = {"off": "끔", "alert": "관리자에게 알림", "warn": "방에 경고 띄우기", "ban": "자동 밴"}
register_setting("fedban_mode", "alert", "공동 차단 명단",
                 choices={"off": "off", "끔": "off", "alert": "alert", "알림": "alert",
                          "warn": "warn", "경고": "warn", "수배": "warn",
                          "ban": "ban", "자동밴": "ban", "밴": "ban"},
                 render_fn=lambda v: MODES.get(v, str(v)))

register_schema("""
CREATE TABLE IF NOT EXISTS fedban_entries (
    user_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    no      INTEGER
);
CREATE TABLE IF NOT EXISTS fedban_reports (
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    evidence   TEXT,
    media_kind TEXT,
    media_id   TEXT,
    by_name    TEXT,
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
# 예전 DB 에도 수배번호·원글(박제 증거) 칸 추가
register_columns("fedban_entries", {"no": "INTEGER"})
register_columns("fedban_reports", {"evidence": "TEXT", "media_kind": "TEXT", "media_id": "TEXT", "by_name": "TEXT"})

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
    """명단에 있으면 {name, no(수배번호), rooms, reasons(최근 3개), chats, first_ts,
    원글: evidence/media_kind/media_id(가장 최근 신고), by_name}."""
    if user_id not in await _listed(db):
        return None
    entry = await db._one("SELECT name, no FROM fedban_entries WHERE user_id=?", (user_id,))
    if not entry:
        return None
    reps = await db._all("SELECT chat_id, reason, ts, evidence, media_kind, media_id, by_name "
                         "FROM fedban_reports WHERE user_id=? ORDER BY ts DESC", (user_id,))
    if not reps:
        return None
    ev = next((r for r in reps if r["evidence"] or r["media_id"]), reps[0])   # 원글 있는 가장 최근 신고
    return {"name": entry["name"], "no": entry["no"], "rooms": len(reps),
            "reasons": [r["reason"] for r in reps[:3]], "chats": [r["chat_id"] for r in reps],
            "first_ts": min(r["ts"] for r in reps),
            "evidence": ev["evidence"], "media_kind": ev["media_kind"], "media_id": ev["media_id"],
            "by_name": ev["by_name"] or entry["name"]}


async def by_number(db, no: int) -> int | None:
    """수배번호 → user_id."""
    row = await db._one("SELECT user_id FROM fedban_entries WHERE no=?", (no,))
    return row["user_id"] if row else None


async def recent(db, limit: int = 15) -> list:
    return await db._all(
        "SELECT e.user_id, e.name, e.no, COUNT(r.chat_id) AS rooms, MAX(r.ts) AS last_ts, "
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
# 원글(박제 증거) 임시 보관 — AI 도구(t_fedban)가 확인 카드 전에 담아두면 add 가 꺼내 씀 (명령 경로는 직접 넘김)
_PENDING_EV: dict[tuple, dict] = {}


def stash_evidence(chat_id: int, user_id: int, evidence=None, media_kind=None, media_id=None) -> None:
    if evidence or media_id:
        _PENDING_EV[(chat_id, user_id)] = {"evidence": (evidence or None),
                                           "media_kind": media_kind, "media_id": media_id}


def capture_evidence(reply_msg):
    """박제 원글(답장한 메시지) → (evidence_html, media_kind, media_id). 없으면 (None, None, None)."""
    if reply_msg is None:
        return None, None, None
    from .util import rich_html
    text = rich_html(reply_msg) or getattr(reply_msg, "text", None) or getattr(reply_msg, "caption", None)
    kind = media_id = None
    for k in MEDIA_KINDS:
        fid = mediastore.new_file_id(reply_msg, k)
        if fid:
            kind, media_id = k, fid
            break
    # HTML 을 글자수로 자르면 <tg-emoji …> 태그 중간이 잘려 나중에 발송 거부됨 → 보이는 글자로 안전하게
    return (html_truncate(text, 3000) if text else None), kind, media_id


async def add(svc, bot, chat_id: int, user_id: int, name: str, reason: str, actor_id: int,
              evidence=None, media_kind=None, media_id=None, by_name=None) -> str:
    """명단에 올리고 이 방에서 밴. 권한·대상 확인(관리자·봇·운영자 제외)은 부르는 쪽(commands.c_fedban)에서.
    evidence/media_* = 박제 원글(답장한 글). 돌려주는 값 = 방에 보낼 안내문."""
    db = svc.db
    if not await svc.paid_features(chat_id):
        return NOT_PAID
    day = _day(svc)
    if await db.counter(day, chat_id, "fedban_add") >= DAILY_LIMIT:
        return f"공동 차단 명단엔 한 방에서 하루 {DAILY_LIMIT}명까지 올릴 수 있어요. 내일 다시 해주세요."
    if evidence is None and media_id is None:           # AI 도구가 미리 담아둔 원글 꺼내기
        stashed = _PENDING_EV.pop((chat_id, user_id), None)
        if stashed:
            evidence, media_kind, media_id = stashed["evidence"], stashed["media_kind"], stashed["media_id"]
    reason, now = reason.strip()[:200], int(time.time())
    await db.bump(day, chat_id, "fedban_add")
    await db._write("INSERT INTO fedban_entries(user_id, name, ts) VALUES(?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (user_id, name[:64], now))
    # 수배번호: 새로 등재된 계정만 부여 (기존 번호는 유지)
    await db._write("UPDATE fedban_entries SET no=(SELECT COALESCE(MAX(no), 0) + 1 FROM fedban_entries) "
                    "WHERE user_id=? AND no IS NULL", (user_id,))
    db.uncache(LISTED)
    await db._write("INSERT INTO fedban_reports(user_id, chat_id, reason, ts, evidence, media_kind, media_id, by_name) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, chat_id) DO UPDATE SET reason=excluded.reason, ts=excluded.ts, "
                    "evidence=COALESCE(excluded.evidence, fedban_reports.evidence), "
                    "media_kind=COALESCE(excluded.media_kind, fedban_reports.media_kind), "
                    "media_id=COALESCE(excluded.media_id, fedban_reports.media_id), "
                    "by_name=COALESCE(excluded.by_name, fedban_reports.by_name)",
                    (user_id, chat_id, reason, now, (evidence or None), media_kind, media_id, (by_name or name)[:64]))
    if media_id and media_kind:
        mediastore.remember_soon(bot, db, media_kind, media_id)
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


# ── 공개 수배 카드 (방에 직접 올림 + 버튼으로 원글 펼침/접기) ──────────────
def _wanted_date(ts, tz) -> str:
    try:
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
    except Exception:
        return ""


def wanted_card_text(info: dict, name: str, user_id: int, *, expanded: bool, tz=None, banned: bool = False) -> str:
    """수배 카드 본문. expanded=False 요약 / True 요약+원글 전문. 필터 없이 그대로(오너 결정)."""
    no = info.get("no")
    head = f"🚨 <b>수배 #{no}</b>" if no else "🚨 <b>공동 차단 명단 계정</b>"
    reasons = "\n".join(f"• {esc(r[:120])}" for r in info["reasons"])
    lines = [head,
             f"{esc(name)} (ID <code>{user_id}</code>)",
             f"🏷 사유:\n{reasons}",
             f"📍 {info['rooms']}곳에서 신고" + (f" · 📅 {_wanted_date(info['first_ts'], tz)}" if tz else "")]
    if banned:
        lines.append("🚫 <b>이 방에서 내보냈어요.</b>")
    else:
        lines.append("⚠️ 거래·입장 시 주의하세요.")
    if expanded:
        ev = info.get("evidence")
        lines.append("─────────────")
        if ev:
            lines.append("📄 <b>박제 원글</b>:\n" + ev)   # 원글은 이미 HTML (rich_html) 또는 평문 — 그대로
        if info.get("media_id"):
            lines.append("🖼 원본 사진/영상은 아래 버튼으로 볼 수 있어요.")
        if not ev and not info.get("media_id"):
            lines.append("(저장된 원글이 없어요)")
    return "\n".join(lines)


def wanted_kb(user_id: int, *, expanded: bool, has_media: bool, banned: bool = False,
              has_evidence: bool = True) -> InlineKeyboardMarkup:
    rows = []
    top = []
    if has_evidence or has_media:
        if expanded:
            top.append(InlineKeyboardButton("🔙 접기", callback_data=f"wnt:close:{user_id}"))
        else:
            top.append(InlineKeyboardButton("📄 원글 보기", callback_data=f"wnt:open:{user_id}"))
    if has_media:
        top.append(InlineKeyboardButton("🖼 원본 보기", callback_data=f"wnt:media:{user_id}"))
    if top:
        rows.append(top)
    if not banned:
        rows.append([InlineKeyboardButton("🚫 밴", callback_data=f"wnt:ban:{user_id}"),
                     InlineKeyboardButton("🙈 무시", callback_data=f"wnt:ig:{user_id}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def post_wanted_card(svc, bot, chat_id: int, user_id: int, name: str, info: dict, *,
                           reply_to: int | None = None, banned: bool = False, force: bool = False) -> bool:
    """방에 수배 카드 1장. force=False 면 같은 사람·같은 방 CARD_GAP 초 쿨다운. 올렸으면 True."""
    if not force and not await persist.claim(svc.db, f"wanted_card:{chat_id}:{user_id}", CARD_GAP):
        return False
    text = wanted_card_text(info, name, user_id, expanded=False, tz=svc.cfg.tz, banned=banned)
    kb = wanted_kb(user_id, expanded=False, has_media=bool(info.get("media_id")), banned=banned,
                   has_evidence=bool(info.get("evidence")))
    try:
        from telegram import ReplyParameters
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb,
                               reply_parameters=ReplyParameters(reply_to, allow_sending_without_reply=True)
                               if reply_to else None)
    except TelegramError as e:
        log.warning("wanted card post failed %s: %s", chat_id, e)
        return False
    return True


async def on_wanted(svc, bot, q, parts: list) -> None:
    """공개 수배 카드 버튼 (prefix wnt). open/close 누구나 제자리 교체, media 누구나, ban/ig 는 권한 있는 사람만."""
    act = parts[0] if parts else ""
    try:
        user_id = int(parts[1])
    except (IndexError, ValueError):
        await q.answer("만료된 카드예요.")
        return
    chat_id = q.message.chat_id
    info = await lookup(svc.db, user_id)
    if not info:
        await q.answer("명단에서 빠진 계정이에요.")
        return
    name = info.get("by_name") or info["name"]
    if act in ("open", "close"):
        expanded = act == "open"
        text = wanted_card_text(info, name, user_id, expanded=expanded, tz=svc.cfg.tz)
        kb = wanted_kb(user_id, expanded=expanded, has_media=bool(info.get("media_id")),
                       has_evidence=bool(info.get("evidence")))
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except BadRequest as e:
            # 박제 원글은 '필터 없이 그대로' 라 깨진 커스텀 이모지·망가진 HTML 이 있으면 거부될 수 있다
            # → 글자로라도 펼쳐 '원글 보기' 가 씹히지 않게 (2026-10-27 버그 조사)
            if "not modified" not in str(e).lower():
                try:
                    await q.edit_message_text(html_plain(text), reply_markup=kb)
                except BadRequest:
                    pass
        await q.answer()
        return
    if act == "media":
        if not info.get("media_id"):
            await q.answer("저장된 원본이 없어요.")
            return
        mid = getattr(q.message, "message_id", None)
        try:
            from telegram import ReplyParameters
            await mediastore.send(bot, svc.db, info["media_kind"], chat_id, info["media_id"],
                                  reply_parameters=ReplyParameters(mid, allow_sending_without_reply=True)
                                  if mid else None)
        except mediastore.MediaLost:
            await q.answer("원본이 사라졌어요 (글만 남아 있어요).", show_alert=True)
            return
        except TelegramError:
            await q.answer("원본을 못 불러왔어요.")
            return
        await q.answer()
        return
    if act in ("ban", "ig"):
        if not await may(svc.perms, bot, chat_id, q.from_user.id):
            await q.answer("'사용자 차단' 권한이 있는 관리자만 할 수 있어요.", show_alert=True)
            return
        if act == "ig":
            await q.answer("무시했어요.")
            try:
                await q.edit_message_reply_markup(None)
            except (BadRequest, AttributeError):
                pass
            return
        try:
            await svc.mod.ban(bot, chat_id, user_id, q.from_user.id, "공동 차단 명단: " + info["reasons"][0][:100])
        except TelegramError as e:
            await q.answer(f"실패: {e.message[:80]}", show_alert=True)
            return
        await _mark(svc.db, chat_id, user_id, info["rooms"])
        text = wanted_card_text(info, name, user_id, expanded=False, tz=svc.cfg.tz, banned=True)
        try:
            await q.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=wanted_kb(user_id, expanded=False,
                                                             has_media=bool(info.get("media_id")), banned=True,
                                                             has_evidence=bool(info.get("evidence"))))
        except BadRequest:
            pass
        await q.answer("내보냈어요.")
        return
    await q.answer()


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
        banned = False
        try:
            await svc.mod.ban(bot, chat_id, user.id, None, "공동 차단 명단: " + info["reasons"][0][:100])
        except TelegramError as e:
            log.warning("fedban auto ban failed in %s: %s", chat_id, e)
        else:
            banned = True
        if banned:
            await _mark(svc.db, chat_id, user.id, info["rooms"])
            await post_wanted_card(svc, bot, chat_id, user.id, name, info, banned=True)   # 사유·원글 보이는 공개 카드
            return True
    if mode == "warn":   # 밴 안 하고 방에 공개 수배 카드만
        if await post_wanted_card(svc, bot, chat_id, user.id, name, info):
            await _mark(svc.db, chat_id, user.id, info["rooms"])
        return False
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
hooks.add_callback_handler("wnt", on_wanted)
