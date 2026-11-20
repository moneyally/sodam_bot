"""💤 자리비움 AI 도구 (set_away): "소담아 나 잘게"·"자리비움 해줘"·"다시 왔어(복귀)". 동작은 sodam/away.py.

본인 자리비움만 켜고 끈다 (남을 자리비움 시킬 수 없음). 켜면 누가 부를 때 소담이 대신 안내하고, 본인이 다시 말하면 자동 복귀.
"""
from __future__ import annotations

import time

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


async def t_check_away(ctx: tools.ToolCtx, a: dict) -> str:
    """누가 'X 언제 와?/자?/어디감?/자리비움이야?/안 보이네' 물을 때 X 의 자리비움 상태·멘트를 돌려준다.
    X 가 자리비움을 안 걸어놨으면 '모름' — 소담은 언제 오는지 알 길이 없으니 지어내지 말 것 (2026-11-20 태산 루디
    '몽키 언제 와?'에 소담이 못 답함 → 자리비움 노트를 AI 가 못 봐서였음. set_away 는 본인만 거는 것이라 조회 길이 없었음)."""
    name = str(a.get("name", "")).strip()
    if not name:
        return "누구인지 이름·@아이디·ID 를 알려줄 것."
    row, err = await tools._resolve(ctx, name)
    if err:
        return err
    who = tools._row_name(row)
    info = await away.get_away(ctx.svc.db, row["user_id"])
    if info is None:
        return (f"{who} 는 지금 자리비움을 안 걸어놨음 → 언제 오는지·어디 있는지 소담은 모름. "
                "지어내지 말고 '모른다'고 솔직히 답할 것 (그 사람이 <code>.자리비움</code> 으로 멘트를 남겨야 알 수 있다고 덧붙여도 됨).")
    mins = max(0, (int(time.time()) - info["since"]) // 60)
    ago = f"{mins // 60}시간 {mins % 60}분 전부터" if mins >= 60 else f"{mins}분 전부터"
    note = (info["message"] or away.DEFAULT_MSG).strip()
    return (f"{who} 는 지금 💤 자리비움 중 ({ago}). 본인이 남긴 멘트: \"{note}\". "
            "이 멘트 그대로 전해 줄 것 (없는 도착 시간 등을 보태 지어내지 말 것).")


tools.register_tool(tools.Tool(
    "check_away",
    "누가 **특정 사람에 대해** '언제 와?/언제 옴?/자?/어디 갔어?/안 보이네/자리비움이야?' 물을 때 그 사람의 자리비움 상태·멘트를 확인한다. "
    "그 사람이 자리비움(.자리비움·'나 잘게')을 걸어놨으면 남긴 멘트를 돌려주고, 안 걸었으면 '모름'을 돌려준다 "
    "— 언제 오는지는 본인이 자리비움 멘트로 남겨야만 알 수 있고, 없으면 지어내면 안 된다. (set_away 는 본인을 거는 것, 이건 남을 조회)",
    {"name": {"type": "string", "description": "자리비움인지 확인할 사람 (이름·@아이디·ID)"}},
    ["name"], t_check_away, Role.MEMBER, where="room"), read_only=True)
