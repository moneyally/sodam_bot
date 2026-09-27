"""에이전트 루프: AI가 도구를 부르고, 코드가 실행하고, 결과를 다시 넣는다.
실행마다 AI 작업 기록(agentlog) 한 줄: 요청·도구 호출·결과·토큰·요금."""
import logging
import re

from openai import BadRequestError

from . import agentlog, memory
from .llm import BudgetExceeded
from .prompt import build_messages
from .security import nonce, wrap
from .tools import ToolCtx, available, execute

log = logging.getLogger(__name__)

MAX_STEPS = 4          # 도구 호출 라운드 최대 횟수
MAX_TOKENS = 1500      # 추론 모델은 생각 토큰도 여기 포함됨
THINK_MAX_TOKENS = 4000  # 생각하는 실행(llm.think): 추론 토큰 포함 상한 (gpt-5.4 출력 $15/1M → 최대 $0.06)
# 먼저 끼어들 때는 방 자료만 볼 수 있게 (웹검색·제재·게임 같은 도구는 숨김 → 비용·오작동 방지)
CHIME_TOOLS = frozenset({"search_knowledge", "room_rules"})

# 생각이 필요한 요청 (코드 판단, 비용 0): 이유·분석을 묻거나, 한 요청에 일이 둘 이상 이어진 것
# ('찾아서 경고', '요약 올리고 알림도', '확인하고 괜찮으면'). 잡담·도구 하나로 끝나는 요청은 지금처럼 추론 없이.
_WHY = re.compile(r"왜|원인|이유|분석|비교|판단|검토|영향|괜찮을까|어떻게\s?(해야|하면|할까)")
_CHAIN = re.compile(r"(찾아|확인해|알아봐|살펴|읽어|보)(서|고)[\s,]|(하|올리|바꾸|켜|끄|걸|주|먹이|보내|정리하)고[\s,](?!\s*싶)|"
                    r"그리고|다음에|한\s?(다음|뒤|후)|둘\s?다|각각|하면\s|(?<![가-힣])(걔|쟤|그\s?사람|저\s?사람)(?![가-힣])")
_ACTS = re.compile(r"(해|줘|드려|올려|알려|걸어|바꿔|켜|꺼|찾아|정리해|보여)(줘|주세요|줄래|요)?(?=[\s,.!?]|$)")


def wants_thinking(mode_setting: str, request: str, mode: str) -> bool:
    """이 요청을 생각하는 에이전트(Responses API 추론+도구)로 돌릴지. mode_setting = cfg.agent_think."""
    if mode_setting == "off" or mode not in ("call", "follow"):
        return False
    if mode_setting == "always":
        return True
    text = " ".join(request.split())
    return bool(_WHY.search(text) or _CHAIN.search(text) or len(_ACTS.findall(text)) >= 2)


async def run_agent(ctx: ToolCtx, *, style_key: str, notes: dict, history: list,
                    reply_to: str | None, request: str, mode: str = "call",
                    extras: dict | None = None, hints: list[str] | None = None,
                    images: list[dict] | None = None) -> str:
    """mode: call(이름 불러서) / follow(이어 말하기) / chime·morning(먼저 끼어들기).
    extras: memory.context_for 결과. None 이면 여기서 읽는다 (실패해도 기억 없이 진행)."""
    run, token = agentlog.start(ctx.chat_id, getattr(ctx.caller, "id", None), mode, request)
    status = "error"
    try:
        answer = await _run(ctx, run, style_key=style_key, notes=notes, history=history, reply_to=reply_to,
                            request=request, mode=mode, extras=extras, hints=hints, images=images)
        status = "answered" if answer.strip() else ("tool_only" if run.steps else "empty")
        return answer
    except BudgetExceeded:
        status = "budget"
        raise
    finally:
        await agentlog.finish(ctx.svc.db, run, token, status)   # 기록 실패는 안에서 삼킴


async def _run(ctx: ToolCtx, run: agentlog.Run, *, style_key: str, notes: dict, history: list,
               reply_to: str | None, request: str, mode: str, extras: dict | None,
               hints: list[str] | None, images: list[dict] | None) -> str:
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
        reply_to=reply_to, request=request, mode=mode, hints=hints, images=images, in_dm=ctx.chat_id > 0, **extras)
    tools = available(ctx.role, ctx.settings, ctx.chat_id > 0)
    if mode in ("chime", "morning"):
        tools = [t for t in tools if t.name in CHIME_TOOLS]
    schemas = [t.schema() for t in tools]
    allowed = {t.name for t in tools}
    purpose = f"agent:{role_label}" if mode not in ("chime", "morning") else "agent:chime"
    think = wants_thinking(getattr(svc.cfg, "agent_think", "off"), request, mode)
    run.purpose = purpose + (":think" if think else "")

    async def call(tool_choice: str = "auto"):
        nonlocal think
        if think:
            try:
                return await svc.llm.think(messages, tools=schemas or None, tool_choice=tool_choice,
                                           effort=svc.cfg.agent_think_effort, max_tokens=THINK_MAX_TOKENS,
                                           purpose=run.purpose, chat_id=ctx.chat_id)
            except BadRequestError as e:   # 모델·계정이 Responses 추론을 못 받으면 이번 실행은 예전 방식으로
                if any(m["role"] == "assistant" for m in messages):
                    raise
                log.warning("생각하는 에이전트 실패 → 기본 방식: %s", e)
                think, run.purpose = False, purpose
        return await svc.llm.chat(messages, tools=schemas or None, tool_choice=tool_choice, max_tokens=MAX_TOKENS,
                                  purpose=purpose, chat_id=ctx.chat_id)

    for _ in range(MAX_STEPS):
        msg = await call()
        calls = [c for c in (msg.tool_calls or []) if c.type == "function"]
        if not calls:
            return msg.content or ""
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                           for c in calls],
            **({"items": msg.items} if think else {}),   # 추론 항목을 다음 라운드로 (llm.to_input)
        })
        for c in calls:
            if c.function.name not in allowed:  # 이번 호출에 보여주지 않은 도구
                result = "이 도구는 지금 사용할 수 없음."
            else:
                result = await execute(c.function.name, c.function.arguments, ctx)
            log.info("도구 %s chat=%s user=%s 인자=%s → %s", c.function.name, ctx.chat_id, ctx.caller.id,
                     (c.function.arguments or "")[:200], result[:200].replace("\n", " "))
            try:
                run.step(c.function.name, c.function.arguments, result)
            except Exception:   # 기록용 요약이 답을 막으면 안 됨
                log.exception("agent log step failed")
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": wrap("tool_result", result[:4000], nonce())})

    # 도구 라운드를 다 쓰면 도구 없이 마무리 답변만 받는다
    return (await call("none")).content or ""
