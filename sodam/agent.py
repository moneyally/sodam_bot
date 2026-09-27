"""에이전트 루프: AI가 도구를 부르고, 코드가 실행하고, 결과를 다시 넣는다."""
import logging

from . import memory
from .prompt import build_messages
from .security import nonce, wrap
from .tools import ToolCtx, available, execute

log = logging.getLogger(__name__)

MAX_STEPS = 4          # 도구 호출 라운드 최대 횟수
MAX_TOKENS = 1500      # 추론 모델은 생각 토큰도 여기 포함됨
# 먼저 끼어들 때는 방 자료만 볼 수 있게 (웹검색·제재·게임 같은 도구는 숨김 → 비용·오작동 방지)
CHIME_TOOLS = frozenset({"search_knowledge", "room_rules"})


async def run_agent(ctx: ToolCtx, *, style_key: str, notes: dict, history: list,
                    reply_to: str | None, request: str, mode: str = "call",
                    extras: dict | None = None, hints: list[str] | None = None,
                    images: list[dict] | None = None) -> str:
    """mode: call(이름 불러서) / follow(이어 말하기) / chime·morning(먼저 끼어들기).
    extras: memory.context_for 결과. None 이면 여기서 읽는다 (실패해도 기억 없이 진행)."""
    svc = ctx.svc
    role_label = {0: "member", 1: "admin", 2: "owner"}[int(ctx.role)]
    if extras is None:
        try:
            extras = await memory.context_for(svc, ctx.chat_id, ctx.caller.id, ctx.settings, history)
        except Exception:
            log.exception("memory context failed")
            extras = {}
    messages = build_messages(
        bot_name=svc.cfg.bot_name, bot_id=ctx.bot.id, style_key=style_key, tz=svc.cfg.tz,
        caller=ctx.caller, role_label=role_label, notes=notes, history=history,
        reply_to=reply_to, request=request, mode=mode, hints=hints, images=images, **extras)
    tools = available(ctx.role, ctx.settings, ctx.chat_id > 0)
    if mode in ("chime", "morning"):
        tools = [t for t in tools if t.name in CHIME_TOOLS]
    schemas = [t.schema() for t in tools]
    allowed = {t.name for t in tools}
    purpose = f"agent:{role_label}" if mode not in ("chime", "morning") else "agent:chime"

    for _ in range(MAX_STEPS):
        msg = await svc.llm.chat(messages, tools=schemas or None, max_tokens=MAX_TOKENS, purpose=purpose,
                                 chat_id=ctx.chat_id)
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
            if c.function.name not in allowed:  # 이번 호출에 보여주지 않은 도구
                result = "이 도구는 지금 사용할 수 없음."
            else:
                result = await execute(c.function.name, c.function.arguments, ctx)
            log.info("도구 %s chat=%s user=%s 인자=%s → %s", c.function.name, ctx.chat_id, ctx.caller.id,
                     (c.function.arguments or "")[:200], result[:200].replace("\n", " "))
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": wrap("tool_result", result[:4000], nonce())})

    # 도구 라운드를 다 쓰면 도구 없이 마무리 답변만 받는다
    msg = await svc.llm.chat(messages, tools=schemas or None, tool_choice="none", max_tokens=MAX_TOKENS,
                             purpose=purpose, chat_id=ctx.chat_id)
    return msg.content or ""
