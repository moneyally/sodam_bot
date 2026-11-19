"""💤 자리비움 AI 도구 (set_away): "소담아 나 잘게"·"자리비움 해줘"·"다시 왔어(복귀)". 동작은 sodam/away.py.

본인 자리비움만 켜고 끈다 (남을 자리비움 시킬 수 없음). 켜면 누가 부를 때 소담이 대신 안내하고, 본인이 다시 말하면 자동 복귀.
"""
from __future__ import annotations

from .. import away, tools
from ..permissions import Role
from ..security import scan
from ..util import esc, user_name


async def t_set_away(ctx: tools.ToolCtx, a: dict) -> str:
    on = a.get("on", True)
    if on in ("false", "off", "0", 0, False, "끄기", "복귀"):
        on = False
    db, uid = ctx.svc.db, ctx.caller.id
    if not on:   # 복귀
        if await away.get_away(db, uid) is None:
            return "이 사람은 자리비움 상태가 아니었음. 그냥 짧게 답할 것."
        pings = await away.clear_away(db, uid)
        if pings:
            return (f"자리비움을 풀었음. 자리비운 동안 {len(pings)}명이 찾았다고 알려 줄 것: " + " ".join(pings[:away.MAX_PINGS]))
        return "자리비움을 풀었음. '돌아온 걸 환영'한다고 짧게."
    message = str(a.get("message", "")).strip()[:away.MAX_MSG]
    if message and scan(message).blocked:   # 봇 조종 문구는 멘트로 안 씀
        message = ""
    await away.set_away(db, uid, user_name(ctx.caller), message)
    shown = f" (멘트: '{esc(message)}')" if message else ""
    return (f"이 사람을 자리비움으로 바꿨음{shown}. 누가 부르면 소담이 대신 안내하고, 본인이 방에서 다시 말하면 자동으로 풀린다고 "
            "짧게 안내할 것.")


TOOL = tools.Tool(
    "set_away",
    "본인을 자리비움(자러감/잠수)으로 바꾸거나 푼다. '나 잘게'·'자리비움 해줘'·'자리 비운다고 해줘'·'다시 왔어/복귀'. "
    "켜면 자리비운 동안 누가 @태그·답장으로 부를 때 소담이 '지금 자리비움이에요: 멘트' 라고 대신 알려 주고, "
    "본인이 방에서 다시 말하면 자동으로 풀리며 '누가 찾았는지' 알려 준다. 본인만 — 남을 자리비움 시킬 수 없다.",
    {"message": {"type": "string", "description": "자리비움 동안 보여줄 멘트 (예: '자러 가요, 아침에 답할게요'). 없으면 기본 문구"},
     "on": {"type": "boolean", "description": "자리비움 켜기=true(기본) / 풀기(복귀)=false"}},
    [], t_set_away, Role.MEMBER, where="room")
tools.register_tool(TOOL)
