"""🚀 채팅 집계 부스트 — 명령(.채팅부스트)·AI 도구. 핵심 구간 로직은 sodam/rankboost.py.

NASA(뉴월드) 2026-10-31: 평소 5글자 집계인데 잠깐 2글자로 낮춰 짧은 글도 집계하고(이벤트·부스트),
다시 5글자로 되돌려도 **그 부스트 기간에 친 글은 순위에 영구 보존**하고 싶다. (설정 chat_min_chars 는
'지금 값으로 전체 소급'이라 되돌리면 빠진다.) 부스트는 '구간'으로 저장돼 그 당시 기준으로 고정된다.
"""
from __future__ import annotations

from .. import rankboost, tools
from ..permissions import Role
from ..util import esc, to_int


async def c_boost(ctx) -> None:
    """.채팅부스트 [글자수] 켜기 / .채팅부스트 끄기 / .채팅부스트 (상태). 관리자·그룹방."""
    if ctx.chat_id > 0:
        await ctx.reply("그룹방에서 써 주세요.")
        return
    arg = (ctx.argstr or "").strip()
    db = ctx.svc.db
    if arg in ("끄기", "꺼", "off", "종료", "해제", "중지", "stop"):
        _, msg = await rankboost.stop(db, ctx.chat_id)
        await db.log_mod(ctx.chat_id, ctx.user.id, None, "rank_boost", "끄기")
        await ctx.reply(msg)
        return
    if not arg or arg in ("상태", "status", "?"):
        await ctx.reply(await rankboost.status(db, ctx.chat_id))
        return
    mc = to_int(arg.replace("글자", "").replace("자", "").strip())
    if mc is None:
        await ctx.reply("예: <code>.채팅부스트 2</code> (2글자 이상 집계 시작) · "
                        "<code>.채팅부스트 끄기</code> · <code>.채팅부스트</code>(상태)")
        return
    ok, msg = await rankboost.start(db, ctx.chat_id, mc, ctx.user.id)
    if ok:
        await db.log_mod(ctx.chat_id, ctx.user.id, None, "rank_boost", f"{mc}글자")
    await ctx.reply(msg)


def register_commands() -> None:
    """handlers 가 import 뒤 부름 (commands→panels 순환 피함)."""
    from .. import commands
    if any(c.fn is c_boost for c in commands.COMMANDS):
        return
    cmd = commands.Cmd(("채팅부스트", "집계부스트", "부스트집계"), c_boost, Role.ADMIN,
                       usage="[글자수] | 끄기 | (없으면 상태)",
                       help="잠깐 최소 글자수를 낮춰 집계(이벤트) — 끈 뒤에도 그 동안 친 글은 순위에 남음", group="집계")
    commands.COMMANDS.append(cmd)
    for name in cmd.names:
        commands._INDEX.setdefault(name.lower(), cmd)


async def t_chat_boost(ctx: tools.ToolCtx, a: dict) -> str:
    """AI 도구: 채팅 집계 부스트 켜기/끄기 (관리자·그 방)."""
    db = ctx.svc.db
    if bool(a.get("on", True)):
        mc = max(0, int(a.get("min_chars", 0) or 0))
        ok, msg = await rankboost.start(db, ctx.chat_id, mc, ctx.caller.id)
        if ok:
            await db.log_mod(ctx.chat_id, ctx.caller.id, None, "rank_boost", f"{mc}글자")
        return esc(msg) + " 짧게 그대로 안내."
    _, msg = await rankboost.stop(db, ctx.chat_id)
    await db.log_mod(ctx.chat_id, ctx.caller.id, None, "rank_boost", "끄기")
    return esc(msg) + " 짧게 그대로 안내."


tools.register_tool(tools.Tool(
    "chat_boost",
    "채팅 집계 '부스트' 켜기(on=true)/끄기(on=false). 부스트 = 잠깐 최소 글자수를 낮춰(min_chars, 0=모든 글) "
    "짧은 글도 순위에 집계하는 이벤트. 끈 뒤에도 켜 둔 동안 친 글은 순위에 영구 보존됨(그 당시 기준 고정). "
    "'채팅 부스트 켜줘', '2글자 이상 집계되게 부스트', '이벤트로 짧은 글도 순위에' → on=true·min_chars. "
    "'부스트 꺼줘', '집계 원래대로' → on=false. 방 기본 최소 글자수(chat_min_chars)를 바꾸는 change_setting 과 다름 "
    "(그건 전체 소급이라 되돌리면 빠짐). 글·기록·AI 기억은 안 건드림.",
    {"on": {"type": "boolean"}, "min_chars": {"type": "integer"}},
    [], t_chat_boost, Role.ADMIN, where="room"), read_only=False)
