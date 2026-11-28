"""🗣 1:1 AI 허용 명단 (오너 전용). 2026-11-28 오너 요청.

지금 1:1 AI 대화는 오너 전용(`cfg.dm_ai_owner_only`, 외부인이 봇을 공짜 AI 처럼 악용 → 매출 0·비용만,
2026-11-26)이라 딴 사람이 1:1 에서 소담을 부르면 '운영자 전용' 안내 뒤 막힌다. 오너가 **콕 집어 추가한
사람만** 1:1 에서 소담이랑 AI 대화할 수 있게 한다.

추가된 사람은 **'그냥 멤버' 그대로** — 오너 권한 0(설정·제재·매출·서버 아무것도 못 봄), 역할도 안 올라감.
오직 `handlers._within_ai_quota` 의 1:1 AI 게이트만 통과시킨다. 악용 방지로 하루 한도 `per_day`(기본 200,
chat_state(0) dm_ai_allow_per_day 로 조절) 를 적용(전체 $ 예산도 그대로라 이중 방어).

관리는 오너만: 명령 `.AI허용`/`.AI허용해제`/`.AI허용목록` · AI 도구 `dm_ai_allow`(panels/dmai.py) ·
오너 메인 🗣 화면(panels/dmai.py). 명단은 봇 DB 단위(화이트라벨 클론은 별도 DB라 자동 분리).
"""
from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from .db import register_schema
from .permissions import Role
from .util import display_name, esc

if TYPE_CHECKING:  # pragma: no cover
    pass

log = logging.getLogger(__name__)

PER_DAY_DEFAULT = max(1, int(os.getenv("DM_AI_ALLOW_PER_DAY", "200") or "200"))

register_schema("""
CREATE TABLE IF NOT EXISTS dm_ai_allow (
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER NOT NULL,
    ts       INTEGER NOT NULL
);
""")

# 메시지마다 보는 핫패스라 방별이 아닌 전역 집합을 30초 캐시 (바꾸는 코드가 즉시 비움, 딴 프로세스는 30초 안)
_ALLOW: set[int] | None = None
_AT = 0.0
TTL = 30.0


async def _load(db) -> set[int]:
    global _ALLOW, _AT
    if _ALLOW is None or time.monotonic() - _AT > TTL:
        rows = await db._all("SELECT user_id FROM dm_ai_allow")
        _ALLOW = {int(r["user_id"]) for r in rows}
        _AT = time.monotonic()
    return _ALLOW


async def is_allowed(db, user_id: int) -> bool:
    return user_id in await _load(db)


async def per_day(db) -> int:
    v = await db.get_state(0, "dm_ai_allow_per_day")
    return int(v) if v and str(v).isdecimal() and int(v) > 0 else PER_DAY_DEFAULT


async def add(db, user_id: int, by: int) -> None:
    await db._write("INSERT OR REPLACE INTO dm_ai_allow(user_id, added_by, ts) VALUES(?, ?, ?)",
                    (user_id, by, int(time.time())))
    global _ALLOW
    _ALLOW = None   # 즉시 반영


async def remove(db, user_id: int) -> None:
    await db._write("DELETE FROM dm_ai_allow WHERE user_id=?", (user_id,))
    global _ALLOW
    _ALLOW = None


async def listed(db) -> list:
    return await db._all("SELECT user_id, added_by, ts FROM dm_ai_allow ORDER BY ts DESC")


async def name_of(db, uid: int) -> str:
    u = await db._one("SELECT first_name, last_name, username FROM users WHERE user_id=?", (uid,))
    return display_name(u["first_name"], u["last_name"], u["username"]) if u else f"ID {uid}"


# ── 오너 명령 (.AI허용 / .AI허용해제 / .AI허용목록) ──────────────
async def c_allow(ctx) -> None:
    if ctx.role < Role.OWNER:
        return
    from .commands import _target
    t = await _target(ctx, sanction=False, outside=True)
    if not t:
        await ctx.reply("누구를 허용할지 알려줘: <code>.AI허용 @아이디</code> · <code>.AI허용 123456789</code> · 그 사람 글에 답장")
        return
    uid, name, _ = t
    await add(ctx.svc.db, uid, ctx.user.id)
    await ctx.reply(f"✅ <b>{esc(name)}</b> 님 1:1 AI 대화 허용했어 (하루 {await per_day(ctx.svc.db)}번까지). "
                    "오너 권한은 없이 수다만 돼.")


async def c_unallow(ctx) -> None:
    if ctx.role < Role.OWNER:
        return
    from .commands import _target
    t = await _target(ctx, sanction=False, outside=True)
    if not t:
        await ctx.reply("누구를 뺄지 알려줘: <code>.AI허용해제 @아이디</code> · 답장 (<code>.AI허용목록</code> 에서 확인)")
        return
    uid, name, _ = t
    await remove(ctx.svc.db, uid)
    await ctx.reply(f"🚫 <b>{esc(name)}</b> 님 1:1 AI 허용 뺐어.")


async def c_allow_list(ctx) -> None:
    if ctx.role < Role.OWNER:
        return
    rows = await listed(ctx.svc.db)
    if not rows:
        await ctx.reply("1:1 AI 허용 명단이 비어 있어. <code>.AI허용 @아이디</code> 로 추가해.")
        return
    lines = [f"🗣 <b>1:1 AI 허용 명단</b> ({len(rows)}명 · 하루 {await per_day(ctx.svc.db)}번)"]
    for r in rows[:50]:
        lines.append(f"• {esc(await name_of(ctx.svc.db, r['user_id']))} (<code>{r['user_id']}</code>)")
    await ctx.reply("\n".join(lines))


def register_commands() -> None:
    """handlers 가 import 뒤 부름 (commands→menu→panels 순환 피함)."""
    from . import commands
    if any(c.fn is c_allow for c in commands.COMMANDS):
        return
    cmds = [
        commands.Cmd(("AI허용", "1대1허용"), c_allow, Role.OWNER, usage="@아이디|ID|답장",
                     help="그 사람만 1:1 AI 대화 허용 (오너)", group="오너", dm_ok=True),
        commands.Cmd(("AI허용해제", "1대1허용해제"), c_unallow, Role.OWNER, usage="@아이디|ID|답장",
                     help="1:1 AI 허용 빼기 (오너)", group="오너", dm_ok=True),
        commands.Cmd(("AI허용목록",), c_allow_list, Role.OWNER,
                     help="1:1 AI 허용 명단 (오너)", group="오너", dm_ok=True),
    ]
    for cmd in cmds:
        commands.COMMANDS.append(cmd)
        for nm in cmd.names:
            commands._INDEX.setdefault(nm.lower(), cmd)
