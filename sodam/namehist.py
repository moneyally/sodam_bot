"""이름·아이디 변경 기록 (SangMata 방식).

봇이 본 사람(봇이 있는 방의 멤버·1:1 사용자)의 이름·@아이디가 바뀔 때마다 기록한다.
- 그룹에서 멤버 이름이 바뀌면 방에 잠깐 알림 (설정 `name_change_notice`, 기본 켜짐) → 사칭·먹튀 계정 식별
- `.이름기록 [@user|ID|답장]` : 그 사람의 변경 기록 (그룹: 이 방 멤버만 / 1:1: 나와 같은 방에 있는 사람만, 오너는 전부)
- 1:1 메뉴 🕵️ 이름 기록 : 내 기록 · 다른 사람 조회
봇이 들어오기 전의 변경이나 봇이 없는 방에서의 변경은 알 수 없다.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .db import now, register_schema
from .settings import register_setting
from .util import display_name, esc, fmt_time

if TYPE_CHECKING:
    from .db import DB

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

SHOW = 20


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


async def history(db: DB, user_id: int) -> list:
    return await db._all("SELECT first_name, last_name, username, ts FROM name_history WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (user_id, SHOW + 1))


async def history_text(db: DB, user_id: int, tz, *, title: str | None = None) -> str:
    rows = await history(db, user_id)
    if not rows:
        return f"🕵️ ID <code>{user_id}</code> 의 이름 기록이 없어요.\n(소담이 있는 방에서 본 적이 있어야 기록돼요)"
    cur = rows[0]
    head = title or display_name(cur["first_name"], cur["last_name"], cur["username"])
    changes = len(rows) - 1
    lines = [f"🕵️ <b>{esc(head)}</b> · ID <code>{user_id}</code>",
             f"이름·아이디 변경 {changes}회" + (" 이상" if len(rows) > SHOW else "") if changes else "변경 기록 없음 (처음 본 그대로)",
             ""]
    for i, r in enumerate(rows[:SHOW]):
        name = esc(_name(r["first_name"], r["last_name"]) or "(이름 없음)")
        uname = f" · @{esc(r['username'])}" if r["username"] else ""
        mark = " ← 지금" if i == 0 else ""
        lines.append(f"<code>{fmt_time(r['ts'], tz, '%Y-%m-%d')}</code> {name}{uname}{mark}")
    lines.append("\n봇이 본 뒤부터의 기록이에요.")
    return "\n".join(lines)


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


async def shares_group(db: DB, a: int, b: int) -> bool:
    """두 사람이 봇이 있는 같은 그룹에 있는지 (1:1 에서 남의 기록을 볼 수 있는 조건)."""
    row = await db._one("SELECT 1 FROM members x JOIN members y ON x.chat_id=y.chat_id "
                        "WHERE x.user_id=? AND y.user_id=? AND x.chat_id < 0 LIMIT 1", (a, b))
    return row is not None
