"""🔒 오너 잠금 AI 도구 (오너만, 그룹방). 동작·거절은 sodam/ownerlock.py · moderation.OwnerLocked.

오너가 말로 "소담아 백문주 영구 잠금뮤트" / "쟤 잠금밴" / "백문주 잠금 풀어".
오너만 쓸 수 있고(Role.OWNER), 관리자는 이걸로 못 풀며(소담이 거절), 관리자가 앱에서 풀어도 소담이 다시 건다.
명령은 commands.py `.잠금뮤트/.잠금밴/.잠금해제/.잠금목록`.
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import ownerlock, tools
from ..permissions import Role


async def t_owner_lock(ctx: tools.ToolCtx, a: dict) -> str:
    if ctx.caller.id not in await ctx.svc.perms.owners():
        return "오너만 쓸 수 있는 잠금 제재야. 그렇게 짧게 안내할 것."
    if getattr(ctx, "tainted", False):
        return "다른 사람 글을 읽은 답에서는 잠금 제재를 할 수 없음(숨은 지시 방지). 오너가 직접 명령하게 안내."
    row, err = await tools._resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    uid, name = row["user_id"], tools._row_name(row)
    kind = "ban" if str(a.get("kind", "mute")) == "ban" else "mute"
    on = bool(a.get("on", True))
    kind_ko = "밴(영구 추방)" if kind == "ban" else "영구 뮤트"

    if not on:   # 해제
        cur = await ownerlock.locked_kind(ctx.svc.db, ctx.chat_id, uid)
        if not cur:
            return f"{name}님은 오너 잠금 상태가 아니었음. 그렇게 안내."
        await ownerlock.remove(ctx.svc.db, ctx.chat_id, uid)
        try:
            if cur == "ban":
                await ctx.svc.mod.unban(ctx.bot, ctx.chat_id, uid, ctx.caller.id)
            else:
                await ctx.svc.mod.unmute(ctx.bot, ctx.chat_id, uid, ctx.caller.id)
        except TelegramError as e:
            return f"{name}님 잠금은 풀었는데 제한 해제는 실패함: {e.message}. 그대로 안내."
        await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, uid, "ownerlock", "해제")
        return f"{name}님 오너 잠금을 풀었음. 짧게 '잠금 풀었어요' 정도로."

    try:
        if kind == "ban":
            await ctx.svc.mod.ban(ctx.bot, ctx.chat_id, uid, ctx.caller.id, "오너 잠금")
        else:
            await ctx.svc.mod.mute(ctx.bot, ctx.chat_id, uid, None, ctx.caller.id, "오너 잠금")
    except TelegramError as e:
        return f"{name}님 {kind_ko} 실패: {e.message} (봇 '사용자 차단' 권한 확인). 그대로 안내."
    await ownerlock.add(ctx.svc.db, ctx.chat_id, uid, kind, ctx.caller.id)
    await ctx.svc.db.log_mod(ctx.chat_id, ctx.caller.id, uid, "ownerlock", f"{kind} 잠금")
    tail = ("방·채팅·음성방 전부 막힘." if kind == "ban"
            else "채팅이 막힘(음성방까지 막으려면 잠금밴).")
    return (f"{name}님을 오너 잠금 {kind_ko} 했음 — {tail} 오너만 풀 수 있고, 관리자가 풀어도 소담이 바로 다시 걸음. "
            "짧게 '영구 잠금했어요, 오너만 풀 수 있어요' 정도로.")


tools.register_tool(tools.Tool(
    "owner_lock",
    "[오너 전용] 멤버에게 '오너 잠금' 제재를 걸거나(on) 푼다(off). 잠금은 오너만 풀 수 있고 관리자는 소담으로도 "
    "텔레그램 앱으로도 못 푼다(앱에서 풀면 소담이 즉시 다시 건다). kind=mute 는 영구 채팅 금지, kind=ban 은 "
    "영구 추방(음성방까지 막힘). '백문주 영구 잠금뮤트'·'쟤 잠금밴 걸어'·'백문주 잠금 풀어' 같은 오너 요청에.",
    {"name": {"type": "string", "description": "대상: @아이디·이름·숫자 ID"},
     "kind": {"type": "string", "enum": ["mute", "ban"], "description": "mute=영구 뮤트(기본)·ban=영구 밴(음성방까지)"},
     "on": {"type": "boolean", "description": "잠금 걸기=true(기본), 풀기=false"}},
    ["name"], t_owner_lock, Role.OWNER, where="room"))
