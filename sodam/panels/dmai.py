"""🗣 1:1 AI 허용 명단 화면 + AI 도구 (오너 전용). 동작·명령은 sodam/dmai.py.

메인 🗣(m:dai) = 명단 보기 · 🗑 빼기 (추가는 말로 'OO 1:1 AI 허용' 또는 명령 .AI허용).
AI 도구 dm_ai_allow(name, on) = 오너 1:1 에서 '소담아 OO 1:1 AI 허용해줘'.
"""
from __future__ import annotations

import re

from .. import dmai, menu, namehist, tools
from ..menu import OWNER, B, PanelCtx, Route, Screen
from ..permissions import Role
from ..tools import Tool, ToolCtx
from ..util import to_int


# ── 오너 메인 화면 ─────────────────────────────────────────
async def s_dai(c: PanelCtx) -> Screen:
    db = c.svc.db
    rows = await dmai.listed(db)
    n = await dmai.per_day(db)
    lines = ["🗣 <b>1:1 AI 허용 명단</b>",
             "여기 있는 사람만 소담이랑 <b>1:1 AI 대화</b>를 할 수 있어요 (오너 권한은 없이 대화만).",
             f"하루 한도: <b>{n}번</b>", ""]
    kb: list = []
    if rows:
        lines.append("뺄 사람을 누르세요:")
        for r in rows[:30]:
            kb.append([B(f"🗑 {await dmai.name_of(db, r['user_id'])}", f"m:daix:{r['user_id']}")])
    else:
        lines.append("아직 없음.")
    lines += ["", "➕ <b>추가</b>: 소담에게 <code>OO 1:1 AI 허용해줘</code> 라고 하거나,",
              "방에서 그 사람 글에 답장하고 <code>.AI허용</code> · <code>.AI허용 @아이디</code>"]
    kb.append([B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines), menu._kb(kb))


async def s_daix(c: PanelCtx) -> Screen:
    uid = to_int(c.arg(0))
    if uid:
        await dmai.remove(c.svc.db, uid)
    return await s_dai(c)


menu.register_main(93, "dai", "🗣 1:1 AI 허용", OWNER)
menu.register_route("dai", Route(s_dai, OWNER, scoped=False))
menu.register_route("daix", Route(s_daix, OWNER, scoped=False))


# ── AI 도구 (오너 1:1 에서 말로) ────────────────────────────
async def _resolve_person(ctx: ToolCtx, name: str):
    svc = ctx.svc
    raw = (name or "").strip()
    n = to_int(raw)
    if n and n > 0:
        return n, await dmai.name_of(svc.db, n)
    handle = raw.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,32}", handle):
        g = await svc.db._one("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (handle,))
        if g:
            uid = int(g["user_id"])
            return uid, await dmai.name_of(svc.db, uid)
        uid, _ = await namehist.resolve_remote(svc, "@" + handle, ctx.caller.id)
        if uid:
            return uid, "@" + handle
    row, _ = await tools._resolve(ctx, raw)
    if row:
        uid = int(row["user_id"])
        return uid, await dmai.name_of(svc.db, uid)
    return None, ""


async def t_dm_ai_allow(ctx: ToolCtx, a: dict) -> str:
    if not (ctx.chat_id > 0 and ctx.chat_id == ctx.caller.id and ctx.caller.id in await ctx.svc.perms.owners()):
        return "이건 오너 1:1 에서만 돼요."
    name = str(a.get("name", "")).strip()
    on = a.get("on", True)
    if isinstance(on, str):
        on = on.strip().lower() not in ("false", "off", "0", "no", "해제", "빼기", "제거", "안")
    uid, disp = await _resolve_person(ctx, name)
    if not uid:
        return f"'{name}' 을(를) 못 찾았어요. @아이디나 숫자 ID로 알려주세요 (명령 .AI허용 @아이디 도 돼요)."
    if on:
        await dmai.add(ctx.svc.db, uid, ctx.caller.id)
        return (f"{disp} 님을 1:1 AI 허용 명단에 넣었어요 (하루 {await dmai.per_day(ctx.svc.db)}번). "
                "오너 권한은 없이 대화만 돼요.")
    await dmai.remove(ctx.svc.db, uid)
    return f"{disp} 님을 1:1 AI 허용 명단에서 뺐어요."


tools.register_tool(Tool(
    "dm_ai_allow",
    "[오너] 어떤 사람을 1:1 AI 대화 허용 명단에 넣거나 뺀다 — 'OO 1:1 AI 허용해줘'. 지금 1:1 AI 는 오너 전용이라, "
    "여기 넣은 사람만 소담이랑 1:1 대화가 된다(오너 권한은 안 줌 — 대화만). name=@아이디·ID·이름, on=넣기/빼기.",
    {"name": {"type": "string", "description": "@아이디 · 숫자 ID · 이름"},
     "on": {"type": "boolean", "description": "true=허용 넣기(기본) / false=빼기"}},
    ["name"], t_dm_ai_allow, Role.OWNER, where="owner_dm"), read_only=False)
