"""에이전트 루프: AI가 도구를 부르고, 코드가 실행하고, 결과를 다시 넣는다."""
from .prompt import build_messages
from .security import nonce, wrap
from .tools import ToolCtx, available, execute

MAX_STEPS = 4          # 도구 호출 라운드 최대 횟수
MAX_TOKENS = 1500      # 추론 모델은 생각 토큰도 여기 포함됨


async def run_agent(ctx: ToolCtx, *, style_key: str, notes: dict, history: list,
                    reply_to: str | None, request: str) -> str:
    svc = ctx.svc
    role_label = {0: "member", 1: "admin", 2: "owner"}[int(ctx.role)]
    messages = build_messages(
        bot_name=svc.cfg.bot_name, bot_id=ctx.bot.id, style_key=style_key, tz=svc.cfg.tz,
        caller=ctx.caller, role_label=role_label, notes=notes, history=history,
        reply_to=reply_to, request=request)
    schemas = [t.schema() for t in available(ctx.role, ctx.settings)]

    for _ in range(MAX_STEPS):
        msg = await svc.llm.chat(messages, tools=schemas, max_tokens=MAX_TOKENS, purpose=f"agent:{role_label}")
        calls = [c for c in (msg.tool_calls or []) if c.type == "function"]
        if not calls:
            return msg.content or ""
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                           for c in calls],
        })
        for c in calls:
            result = await execute(c.function.name, c.function.arguments, ctx)
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": wrap("tool_result", result[:4000], nonce())})

    # 도구 라운드를 다 쓰면 도구 없이 마무리 답변만 받는다
    msg = await svc.llm.chat(messages, tools=schemas, tool_choice="none", max_tokens=MAX_TOKENS, purpose=f"agent:{role_label}")
    return msg.content or ""
