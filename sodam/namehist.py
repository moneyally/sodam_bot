"""이름·아이디 변경 기록.

봇이 본 사람(봇이 있는 방의 멤버·1:1 사용자)의 이름·@아이디가 바뀔 때마다 기록한다.
- 그룹에서 멤버 이름이 바뀌면 방에 알림 (설정 `name_change_notice`, 기본 켜짐, `.이름알림`) → 사칭·먹튀 계정 식별
- 명령 (대상 = 답장 > @아이디(예전 것도)·ID·이름 > 전달된 메시지 > 나):
  `.기록 /history` 최근 · `.전체기록 /allhistory` · `.이름조회 /check_name` · `.아이디조회 /check_username` · `.내기록 /myhistory`
  결과 아래 [최근][전체][이름만][아이디만] 버튼(nh:<모드>:<ID>) — 누를 때마다 권한 재확인
- 권한: 누구나, 소담이 본 모든 사람의 기록을 조회 가능 (사용자 결정 — 익명 방이라 사칭 확인 우선)
- 1:1: 메시지를 전달하면 보낸 사람 기록 · 메뉴 🕵️ 이름 기록
봇이 들어오기 전의 변경이나 봇이 없는 방에서의 변경은 알 수 없다.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

from .db import now, register_schema
from .settings import register_setting
from .util import display_name, esc, fmt_time, to_int

if TYPE_CHECKING:
    from .db import DB

log = logging.getLogger(__name__)

register_schema("""
CREATE TABLE IF NOT EXISTS name_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    first_name TEXT,
    last_name  TEXT,
    username   TEXT,
    ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_name_history ON name_history(user_id, id);
CREATE INDEX IF NOT EXISTS idx_name_history_username ON name_history(username COLLATE NOCASE);
""")
register_setting("name_change_notice", True, "이름 변경 알림")

SHOW = 10          # 최근 기록 (/history): 이름·아이디 각각
SHOW_ALL = 25      # 전체 기록 (/allhistory): 이름·아이디 각각 — 메시지 4096자 안에 들어가게
SHOW_ONE = 50      # 이름만 / 아이디만
VALUE_CHARS = 40
MODES = ("recent", "all", "names", "usernames")
MODE_LABEL = {"recent": "최근 기록", "all": "전체 기록", "names": "이름 기록", "usernames": "아이디 기록"}


def _name(first, last) -> str:
    return " ".join(x for x in (first, last) if x).strip()


async def record(db: DB, user) -> tuple[dict, dict] | None:
    """지금 이름을 기록. 바뀌었으면 (예전, 지금) 을 돌려준다. 처음 보는 사람은 기록만 하고 None."""
    if getattr(user, "is_bot", False):
        return None
    cur = {"name": _name(user.first_name, user.last_name), "username": user.username or ""}
    row = await db._one("SELECT first_name, last_name, username FROM name_history WHERE user_id=? "
                        "ORDER BY id DESC LIMIT 1", (user.id,))
    if row:
        last = {"name": _name(row["first_name"], row["last_name"]), "username": row["username"] or ""}
        if last == cur:
            return None
    await db._write("INSERT INTO name_history(user_id, first_name, last_name, username, ts) VALUES(?,?,?,?,?)",
                    (user.id, user.first_name, user.last_name, user.username, now()))
    return (last, cur) if row else None


def change_notice(user_id: int, old: dict, new: dict) -> str:
    lines = [f"🔄 <b>이름 변경 감지</b> · ID <code>{user_id}</code>"]
    if old["name"] != new["name"]:
        lines.append(f"이름: {esc(old['name'] or '(없음)')} → <b>{esc(new['name'] or '(없음)')}</b>")
    if old["username"] != new["username"]:
        o = f"@{old['username']}" if old["username"] else "(없음)"
        n = f"@{new['username']}" if new["username"] else "(없음)"
        lines.append(f"아이디: {esc(o)} → <b>{esc(n)}</b>")
    return "\n".join(lines)


async def history(db: DB, user_id: int, limit: int = 500) -> list:
    """스냅숏(이름·아이디 한 쌍) 기록, 최신이 먼저."""
    return await db._all("SELECT first_name, last_name, username, ts FROM name_history WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit))


def _changes(rows: list, field: str) -> list[tuple[str, int]]:
    """스냅숏에서 한 항목(이름 또는 아이디)이 바뀐 시점만 뽑는다. [(값, 처음 본 시각)], 최신이 먼저."""
    out: list[tuple[str, int]] = []
    for r in reversed(rows):  # 오래된 것부터 훑으며 값이 바뀔 때만
        v = _name(r["first_name"], r["last_name"]) if field == "name" else (r["username"] or "")
        if not out or out[-1][0] != v:
            out.append((v, r["ts"]))
    return list(reversed(out))


def _fmt(v: str, field: str) -> str:
    v = v if len(v) <= VALUE_CHARS else v[:VALUE_CHARS] + "…"
    if field == "username":
        return f"@{esc(v)}" if v else "(아이디 없음)"
    return esc(v) if v else "(이름 없음)"


async def history_text(db: DB, user_id: int, tz, *, mode: str = "recent", title: str | None = None) -> str:
    rows = await history(db, user_id)
    if not rows:
        return (f"🕵️ ID <code>{user_id}</code> 의 이름 기록이 없어요.\n"
                "(소담이 있는 방에서 본 적이 있어야 기록돼요)")
    cur = rows[0]
    head = title or display_name(cur["first_name"], cur["last_name"], cur["username"])
    names, users = _changes(rows, "name"), _changes(rows, "username")
    lines = [f"🕵️ <b>{esc(head)}</b> · ID <code>{user_id}</code> · {MODE_LABEL.get(mode, '')}",
             f"이름 변경 {len(names) - 1}회 · 아이디 변경 {len(users) - 1}회"]
    limit = {"recent": SHOW, "all": SHOW_ALL}.get(mode, SHOW_ONE)
    for field, items, label in (("name", names, "👤 이름"), ("username", users, "🔗 아이디")):
        if (mode == "names" and field != "name") or (mode == "usernames" and field != "username"):
            continue
        lines += ["", f"<b>{label}</b>"]
        for i, (v, ts) in enumerate(items[:limit]):
            lines.append(f"<code>{fmt_time(ts, tz, '%y.%m.%d')}</code> {_fmt(v, field)}" + (" ← 지금" if i == 0 else ""))
        if len(items) > limit:
            lines.append(f"… 이전 {len(items) - limit}개 더 (전체 기록 버튼)" if mode == "recent" else f"… 이전 {len(items) - limit}개 생략")
    lines.append("\n소담이 본 뒤부터의 기록이에요. 날짜는 처음 본 날.")
    return "\n".join(lines)


def add_button(bot_username: str | None) -> list[InlineKeyboardButton]:
    """'우리 방에도 추가' — 소담이 들어간 방이 늘수록 기록이 쌓여 조회가 정확해진다."""
    if not bot_username:
        return []
    return [InlineKeyboardButton("➕ 우리 방에도 이름 추적 달기 (무료)",
                                 url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages+restrict_members")]


def buttons(user_id: int, mode: str, bot_username: str | None = None) -> InlineKeyboardMarkup:
    """조회 결과 아래 [최근][전체][이름만][아이디만] — 누를 때마다 권한 다시 확인 (nh:<모드>:<ID>)."""
    labels = {"recent": "🕘 최근", "all": "📜 전체", "names": "👤 이름만", "usernames": "🔗 아이디만"}
    rows = [[InlineKeyboardButton(("● " if m == mode else "") + labels[m], callback_data=f"nh:{m}:{user_id}")
             for m in MODES]]
    extra = add_button(bot_username)
    return InlineKeyboardMarkup(rows + ([extra] if extra else []))


async def find_by_old_username(db: DB, chat_id: int | None, username: str) -> int | None:
    """예전 @아이디로 사람 찾기 (아이디를 바꾼 사람 추적). chat_id 가 있으면 그 방 멤버만."""
    username = username.lstrip("@")
    if chat_id is None:
        row = await db._one("SELECT user_id FROM name_history WHERE username=? COLLATE NOCASE "
                            "ORDER BY id DESC LIMIT 1", (username,))
    else:
        row = await db._one("SELECT h.user_id FROM name_history h JOIN members m ON m.user_id=h.user_id AND m.chat_id=? "
                            "WHERE h.username=? COLLATE NOCASE ORDER BY h.id DESC LIMIT 1", (chat_id, username))
    return row["user_id"] if row else None


# ── 조회 권한 · 대상 찾기 ─────────────────────────────────
async def can_view(db: DB, viewer: int, target: int, chat_id: int, is_owner: bool) -> bool:
    """누구나 전부 조회 가능 (사용자 결정 2026-09-27: 익명 닉네임 방이라 사칭·먹튀 확인이 우선).
    범위를 다시 좁히려면 여기만 바꾸면 된다 (명령·버튼·1:1 메뉴가 전부 이 함수를 거침)."""
    return True


async def resolve(db: DB, raw: str, chat_id: int | None = None) -> int | None:
    """숫자 ID · @아이디(지금 또는 예전) → user_id. chat_id 가 있으면 @아이디는 그 방 멤버 중에서."""
    raw = raw.strip()
    uid = to_int(raw)
    if uid is not None:
        return uid if uid > 0 else None
    name = raw.lstrip("@")
    if not name or not _USERNAME.fullmatch(name):
        return None
    row = await db._one("SELECT user_id FROM users WHERE username=? COLLATE NOCASE AND is_bot=0", (name,))
    if row:  # 지금 이 아이디를 쓰는 사람
        return row["user_id"]
    # 예전에 이 아이디를 쓴 사람 (이 방 멤버를 먼저, 없으면 전체에서)
    if chat_id is not None:
        found = await find_by_old_username(db, chat_id, name)
        if found is not None:
            return found
    return await find_by_old_username(db, None, name)


_USERNAME = re.compile(r"[A-Za-z0-9_]{3,32}")


def forwarded_user(msg) -> tuple[int | None, str | None]:
    """전달된 메시지의 원래 보낸 사람. (ID, 숨김이면 안내문)"""
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        return None, None
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return (None, "봇 계정은 기록하지 않아요.") if user.is_bot else (user.id, None)
    if getattr(origin, "sender_user_name", None):
        return None, "그 사람은 '전달 시 계정 숨김' 설정이라 누군지 알 수 없어요. 숫자 ID나 @아이디로 조회해주세요."
    return None, "사람이 보낸 메시지를 전달해주세요 (채널·그룹 명의 글은 조회할 수 없어요)."


async def on_callback(svc, bot, q, parts: list[str]) -> None:
    """조회 결과 아래 버튼 (nh:<모드>:<ID>). 그룹·1:1 어디서 눌러도 권한을 다시 확인한다."""
    from telegram.error import BadRequest
    mode, raw = (parts + ["", ""])[:2]
    uid = to_int(raw)
    if mode not in MODES or uid is None or uid <= 0 or not q.message:
        await q.answer()
        return
    viewer = q.from_user.id
    if not svc.menu_limiter.allow(("nh", viewer), 30):
        await q.answer("너무 빨리 누르고 있어요. 잠시 후 다시 눌러주세요.")
        return
    owner = viewer in await svc.perms.owners()
    if not await can_view(svc.db, viewer, uid, q.message.chat_id, owner):
        await q.answer("🔒 볼 수 없는 기록이에요.", show_alert=True)
        return
    await q.answer()
    title = "내 이름 기록" if uid == viewer and q.message.chat_id > 0 else None
    try:
        await q.edit_message_text(await history_text(svc.db, uid, svc.cfg.tz, mode=mode, title=title),
                                  parse_mode="HTML", reply_markup=buttons(uid, mode, getattr(bot, "username", None)))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise



# ── 기록 범위: 모든 업데이트에서 보이는 사람 ──────────────
register_schema("""
CREATE TABLE IF NOT EXISTS name_scan (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"name_scan": "drop"})

SCAN_PER_TICK = 20          # 조용한 멤버 확인: 한 번에 몇 명 (getChatMember)
SCAN_EVERY = 12 * 3600      # 한 사람을 이 간격마다 다시 확인
SCAN_ACTIVE_DAYS = 90       # 이 기간 안에 본 멤버만


def _msg_users(m, out: list) -> None:
    if m is None:
        return
    for u in (getattr(m, "from_user", None), getattr(m, "left_chat_member", None)):
        if u is not None:
            out.append(u)
    out.extend(getattr(m, "new_chat_members", None) or ())
    for origin in (getattr(m, "forward_origin", None), getattr(getattr(m, "external_reply", None), "origin", None)):
        if origin is not None and getattr(origin, "sender_user", None) is not None:
            out.append(origin.sender_user)
    for ent in [*(getattr(m, "entities", None) or ()), *(getattr(m, "caption_entities", None) or ())]:
        if getattr(ent, "type", None) == "text_mention" and getattr(ent, "user", None) is not None:
            out.append(ent.user)


def users_in_update(update) -> list:
    """업데이트 하나에 보이는 모든 사람 (봇 제외, 중복 제거). 말한 사람·답장 원글·전달 원작성자·이름 멘션·
    입장/퇴장·상태 변경·반응·가입 요청·버튼 누른 사람·수정한 사람·고정한 사람."""
    out: list = []
    for m in (getattr(update, "message", None), getattr(update, "edited_message", None)):
        _msg_users(m, out)
        if m is not None:
            _msg_users(getattr(m, "reply_to_message", None), out)
            _msg_users(getattr(m, "pinned_message", None), out)
    for cmu in (getattr(update, "chat_member", None), getattr(update, "my_chat_member", None)):
        if cmu is not None:
            out += [cmu.from_user, cmu.old_chat_member.user, cmu.new_chat_member.user]
    for obj, attr in ((getattr(update, "message_reaction", None), "user"),
                      (getattr(update, "chat_join_request", None), "from_user"),
                      (getattr(update, "callback_query", None), "from_user"),
                      (getattr(update, "poll_answer", None), "user")):
        if obj is not None and getattr(obj, attr, None) is not None:
            out.append(getattr(obj, attr))
    seen, uniq = set(), []
    for u in out:
        if u is not None and not getattr(u, "is_bot", False) and u.id not in seen:
            seen.add(u.id)
            uniq.append(u)
    return uniq


async def _notify(svc, bot, chat_id: int, user_id: int, changed) -> None:
    if chat_id >= 0 or not (await svc.db.get_settings(chat_id))["name_change_notice"]:
        return
    try:
        await bot.send_message(chat_id, change_notice(user_id, *changed), parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup(
                                   [[InlineKeyboardButton("🕵️ 전체 기록", callback_data=f"nh:all:{user_id}")]]
                                   + [row for row in [add_button(getattr(bot, "username", None))] if row]))
    except TelegramError as e:
        log.info("name change notice failed: %s", e)


async def observe(svc, bot, update) -> None:
    """모든 업데이트 앞에서 (handlers 그룹 -1): 보이는 사람 이름을 전부 기록, 그룹에서 바뀐 걸 보면 알림."""
    chat = getattr(update, "effective_chat", None)
    chat_id = chat.id if chat is not None else 0
    for u in users_in_update(update):
        changed = await record(svc.db, u)
        if changed:
            await _notify(svc, bot, chat_id, u.id, changed)


async def record_admins(svc, bot, chat_id: int, admins) -> None:
    """관리자 목록을 받아올 때 그 사람들 이름도 기록."""
    for a in admins:
        changed = await record(svc.db, a.user)
        if changed:
            await _notify(svc, bot, chat_id, a.user.id, changed)


async def sweep(svc, bot) -> int:
    """조용한 멤버 확인: 말을 안 해도 이름을 바꿨는지 getChatMember 로 조금씩 확인 (한 사람 12시간마다).
    확인한 수를 돌려준다. 텔레그램이 속도 제한을 걸면 이번 회차는 멈춘다."""
    now_ts = now()
    rows = await svc.db._all(
        "SELECT m.chat_id, m.user_id FROM members m JOIN users u ON u.user_id=m.user_id AND u.is_bot=0 "
        "LEFT JOIN name_scan s ON s.chat_id=m.chat_id AND s.user_id=m.user_id "
        "WHERE m.chat_id < 0 AND COALESCE(m.last_seen, 0) > ? AND COALESCE(s.ts, 0) < ? "
        "ORDER BY COALESCE(s.ts, 0) LIMIT ?",
        (now_ts - SCAN_ACTIVE_DAYS * 86400, now_ts - SCAN_EVERY, SCAN_PER_TICK))
    done = 0
    for r in rows:
        chat_id, user_id = r["chat_id"], r["user_id"]
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except RetryAfter:
            break
        except TelegramError:
            member = None  # 봇이 나간 방·없는 사용자
        await svc.db._write("INSERT INTO name_scan(chat_id, user_id, ts) VALUES(?,?,?) "
                            "ON CONFLICT(chat_id, user_id) DO UPDATE SET ts=excluded.ts", (chat_id, user_id, now_ts))
        done += 1
        if member is None or getattr(member.user, "is_bot", False):
            continue
        changed = await record(svc.db, member.user)
        if changed and str(member.status) in ("member", "administrator", "creator", "restricted"):
            await _notify(svc, bot, chat_id, user_id, changed)
    return done
