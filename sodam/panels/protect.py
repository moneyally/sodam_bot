"""🛡️ 보호 멤버 화면 (그룹 허브). 동작은 sodam/protect.py · 제재 거절은 permissions.shield.

m:prt:<방>              목록 · 🗑 삭제 · ✏️ @아이디로 추가
m:prx:<방>:<사람>       한 명 보호 해제
m:in:<방>:prta         @아이디·이름 입력 → 보호 추가
AI 도구 protect_member(name, on): [관리자] 말로 보호/해제 ('철수 밴 못하게', '쓰잔 보호 풀어').
"""
from __future__ import annotations

from telegram import Message

from .. import menu, protect, tools
from ..menu import TG_ADMIN, B, HubItem, PanelCtx, Route, Screen, _back, _kb
from ..permissions import Role, may
from ..util import display_name, esc, to_int

GUIDE = ("보호 멤버는 관리자가 <b>소담으로</b> 밴·뮤트·경고·강퇴를 시도해도 소담이 거절해요 (오너는 제외).\n"
         "⚠️ <b>텔레그램 앱에서 관리자가 직접 밴하는 건 못 막아요</b> — 그건 그 사람을 관리자로 올려야 해요.\n"
         "자동 통제(도배·링크 등)만 빼려면 🕊️ 자유 멤버(.free)를 쓰세요.")


def _name(r) -> str:
    return display_name(r["first_name"], r["last_name"], r["username"]) or str(r["user_id"])


async def s_prt(c: PanelCtx) -> Screen:
    rows = await protect.members(c.svc.db, c.cid)
    lines = [f"🛡️ <b>보호 멤버</b> ({len(rows)}명)", GUIDE, ""]
    lines += [f"• {esc(_name(r))} (<code>{r['user_id']}</code>)" for r in rows] or ["아직 없어요."]
    kb = [[B(f"🗑 {esc(_name(r))[:12]}", f"m:prx:{c.cid}:{r['user_id']}")] for r in rows[:10]]
    kb.append([B("✏️ @아이디로 추가", f"m:in:{c.cid}:prta")])
    return Screen("\n".join(lines), _kb([*kb, _back(c.cid)]))


async def r_prx(c: PanelCtx) -> Screen:
    uid = to_int(c.args[0]) if c.args else None
    if not await may(c.svc.perms, c.svc.bot, c.cid, c.uid, "restrict"):
        screen = await s_prt(c)
        screen.toast = "'사용자 차단' 권한이 있는 관리자만 할 수 있어요."
        return screen
    done = uid is not None and await protect.remove(c.svc.db, c.cid, uid)
    if done:
        await c.svc.db.log_mod(c.cid, c.uid, uid, "protect", "해제")
    screen = await s_prt(c)
    screen.toast = "보호를 풀었어요." if done else "이미 없어요."
    return screen


async def in_add(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    if not await may(c.svc.perms, c.svc.bot, c.cid, c.uid, "restrict"):
        return False, "❌ '사용자 차단' 권한이 있는 관리자만 할 수 있어요."
    q = (msg.text or "").strip().lstrip("@")
    rows = await c.svc.db.find_members(c.cid, q)
    if not rows:
        return False, f"❌ '{esc(q)}' 멤버를 못 찾았어요. @아이디나 정확한 이름·숫자 ID 로."
    if len(rows) > 1:
        names = ", ".join(f"{esc(_name(r))}({r['user_id']})" for r in rows[:5])
        return False, f"❌ 여러 명이에요: {names}. 숫자 ID 로 다시."
    r = rows[0]
    await protect.add(c.svc.db, c.cid, r["user_id"], c.uid)
    await c.svc.db.log_mod(c.cid, c.uid, r["user_id"], "protect", "지정")
    return True, f"🛡️ {esc(_name(r))}님을 보호 멤버로 지정했어요."


async def t_protect_member(ctx: tools.ToolCtx, a: dict) -> str:
    if not await may(ctx.svc.perms, ctx.bot, ctx.chat_id, ctx.caller.id, "restrict"):
        return "보호 멤버 지정은 '사용자 차단' 권한이 있는 관리자만 돼요. 그렇게 짧게 안내할 것."
    row, err = await tools._resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    uid, name = row["user_id"], tools._row_name(row)
    on = bool(a.get("on", True))
    if on:
        await protect.add(ctx.svc.db, ctx.chat_id, uid, ctx.caller.id)
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, uid, "protect", "지정")
        return (f"{name}님을 보호 멤버로 지정함 — 이제 관리자가 소담으로 밴·뮤트·경고·강퇴를 시도해도 소담이 거절함 "
                "(오너는 제외, 텔레그램 앱 직접 밴은 못 막음). 짧게 '보호했어요' 정도로.")
    if not await protect.remove(ctx.svc.db, ctx.chat_id, uid):
        return f"{name}님은 보호 멤버가 아니었음. 그렇게 안내."
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, uid, "protect", "해제")
    return f"{name}님 보호 멤버를 해제함 — 다시 제재받을 수 있음. 짧게 안내."


menu.register_hub(HubItem(39, "prt", "🛡️ 보호 멤버", TG_ADMIN))
menu.register_screen("prt", s_prt, need=TG_ADMIN)
menu.register_route("prx", Route(r_prx, TG_ADMIN, fresh=True))
menu.register_input("prta", ("🛡️ 보호할 멤버의 <b>@아이디</b>나 이름·숫자 ID 를 보내 주세요.\n"
                             "그 사람은 관리자가 소담으로 제재할 수 없게 돼요."), "prt", in_add, s_prt)
tools.register_tool(tools.Tool(
    "protect_member",
    "[관리자] 멤버를 '보호 멤버'로 지정하거나(on) 해제한다(off). 보호 멤버는 관리자가 소담으로 밴·뮤트·경고·강퇴를 "
    "시켜도 소담이 거절한다 ('철수 밴 못하게 해줘'·'쓰잔 보호 풀어'). 오너는 제외되고, 텔레그램 앱에서 직접 밴하는 건 "
    "못 막는다('사용자 차단' 권한 관리자만).",
    {"name": {"type": "string", "description": "대상: @아이디·이름·숫자 ID"},
     "on": {"type": "boolean", "description": "보호 켜기=true(기본), 해제=false"}},
    ["name"], t_protect_member, Role.ADMIN, where="room"))
