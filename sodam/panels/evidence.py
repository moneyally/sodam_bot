"""근거(영수증) AI 도구 — 제재·단정·멤버가 따질 때 기록에서 '원문+시각'을 뽑아 보여 준다.

오너 2026-10-21 '거짓말했으면 근거 제시해서 반박 못하게'. 사실은 코드가 기록에서만 (evidence.receipts, AI 비용 0).
"""
from .. import evidence, tools
from ..tools import Tool, ToolCtx


async def t_show_evidence(ctx: ToolCtx, a: dict) -> str:
    name = str(a.get("name", "")).strip()
    if not name:
        return "누구 근거인지 이름·@아이디·ID를 줄 것."
    row, err = await tools._resolve(ctx, name)
    if err:
        return err
    ctx.tainted = True   # 멤버가 쓴 글을 읽음 → 이 답변에선 이후 읽기 도구만 (숨은 지시 방지)
    out = await evidence.receipts(ctx.svc, ctx.chat_id, row["user_id"])
    if not out:
        return (f"{name} 의 최근 6시간 기록에 근거로 댈 글이 없어요. 없는 걸 지어내지 말고, "
                "근거 없이 단정하지 말 것.")
    return out + "\n\n→ 이 원문·시각을 그대로 근거로 들어 답할 것 (멤버가 '내가 언제?' 못 하게). 링크는 가린 채로."


tools.register_tool(Tool(
    "show_evidence",
    "멤버를 제재하거나 '쟤가 도배·광고·욕 했다'고 단정하기 전에, 또는 멤버가 '내가 언제 그랬냐'고 따질 때 — 그 사람 최근 기록에서 "
    "'원문+시각'을 뽑아 근거로 보여 준다 (도배 횟수·링크 올린 글·경고 누적). 기록에 없으면 단정하지 말 것. 링크·지갑은 가려서 나옴.",
    {"name": {"type": "string", "description": "근거를 댈 멤버 이름·@아이디·숫자ID"}},
    ["name"], t_show_evidence, where="room"), read_only=True)
