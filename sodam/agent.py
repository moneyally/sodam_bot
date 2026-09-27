"""에이전트 루프: AI가 도구를 부르고, 코드가 실행하고, 결과를 다시 넣는다.
실행마다 AI 작업 기록(agentlog) 한 줄: 요청·도구 호출·결과·토큰·요금."""
import logging
import re

from openai import BadRequestError

from . import agentlog, costs, memory
from .llm import BudgetExceeded
from .permissions import Role
from .prompt import build_messages
from .security import nonce, wrap
from .tools import READ_ONLY, ToolCtx, available, execute
from .util import clip_mid

log = logging.getLogger(__name__)

# Codex CLI 처럼 모델이 도구를 그만 부를 때까지 돌되, 라운드·요금 상한은 둔다 (넘으면 도구 없이 마무리 답)
MAX_STEPS = 8          # 도구 호출 라운드 최대 횟수
RUN_USD_CAP = 0.05     # 한 실행(도구 안 AI 포함, agentlog.Run.usd_micro)이 이만큼 쓰면 더는 도구 라운드 안 함
TOOL_RESULT_CHARS = 4000  # 도구 결과를 모델에 넣는 최대 길이 (넘으면 앞+뒤만, util.clip_mid — 끝의 합계 줄이 살게)
MAX_TOKENS = 1500     # 추론 모델은 생각 토큰도 여기 포함됨
THINK_MAX_TOKENS = 4000  # 생각하는 실행(llm.think): 추론 토큰 포함 상한 (gpt-5.4 출력 $15/1M → 최대 $0.06)
# 먼저 끼어들 때는 방 자료만 볼 수 있게 (웹검색·제재·게임 같은 도구는 숨김 → 비용·오작동 방지)
CHIME_TOOLS = frozenset({"search_knowledge", "room_rules"})

# 생각이 필요한 요청 (코드 판단, 비용 0): 관리자·오너 요청 전부 + 멤버는 이유·분석을 묻거나 한 요청에 일이 둘 이상
# 이어진 것('찾아서 경고', '요약 올리고 알림도', '확인하고 괜찮으면'). 멤버 잡담·도구 하나로 끝나는 요청은 추론 없이(싸게).
_WHY = re.compile(r"왜|원인|이유|분석|비교|판단|검토|영향|괜찮을까|어떻게\s?(해야|하면|할까)")
_CHAIN = re.compile(r"(찾아|확인해|알아봐|살펴|읽어|보)(서|고)[\s,]|(하|올리|바꾸|켜|끄|걸|주|먹이|보내|정리하)고[\s,](?!\s*싶)|"
                    r"그리고|다음에|한\s?(다음|뒤|후)|둘\s?다|각각|하면\s|(?<![가-힣])(걔|쟤|그\s?사람|저\s?사람)(?![가-힣])")
# 도구를 하나도 안 불렀는데 '했어요' 라고 하는 답 (Claude Code 의 stop hook 처럼 보내기 전에 코드가 한 번 검사)
_CLAIM = re.compile(r"(뮤트|밴|경고|차단|내보냈|예약|등록|저장|삭제|지웠|켰|껐|바꿨|보냈|걸어|걸었|알림)[^\n.?!]{0,6}"
                    r"(했어|했습니다|완료|처리했|해\s?드렸|뒀어|놨어|뒀습니다|됐어요|되었습니다)")
VERIFY_NOTE = ("검사: 이번 답에서 도구를 하나도 부르지 않았는데 무언가를 '했다'고 말했습니다. 실제로 해야 하는 일이면 지금 도구를 부르세요. "
               "기록에 있는 과거 사실을 전한 것이면 그대로 답하되, 도구로 한 일이 아니면 '했다'고 하지 마세요.")
# 조회 도구를 쓴 답의 숫자 검사: '12명·3건·40%' 같은 숫자가 이번 도구 결과(또는 요청)에 하나도 없으면 한 번 다시 물음
_COUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:명|개|번|건|회|%)")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
NUMBER_NOTE = ("검사: 답에 적은 숫자 {nums} 가 이번에 조회한 도구 결과에 없습니다. 도구 결과에 있는 숫자로 고치세요. "
               "도구 결과의 숫자로 직접 계산한 값이면 그대로 두고, 추측한 숫자면 빼고 답하세요.")
_ACTS = re.compile(r"(해|줘|드려|올려|알려|걸어|바꿔|켜|꺼|찾아|정리해|보여)(줘|주세요|줄래|요)?(?=[\s,.!?]|$)")


def _norm_num(n: str) -> str:
    n = n.replace(",", "")
    return n.rstrip("0").rstrip(".") if "." in n else (n.lstrip("0") or "0")


def unsupported_numbers(answer: str, sources: list[str]) -> list[str]:
    """답의 '숫자+단위(명·개·번·건·회·%)' 중 sources(도구 결과·요청) 어디에도 없는 숫자. 0 은 '없음'과 같아 뺌."""
    have = {_norm_num(n) for src in sources for n in _NUM.findall(src)}
    out = []
    for n in _COUNT.findall(answer):
        v = _norm_num(n)
        if v != "0" and v not in have and v not in out:
            out.append(v)
    return out


def wants_thinking(mode_setting: str, request: str, mode: str, role: int = Role.MEMBER) -> bool:
    """이 요청을 생각하는 에이전트(Responses API 추론+도구)로 돌릴지. mode_setting = cfg.agent_think.
    끼어들기(chime·morning)는 어떤 설정이든 안 함 (비용)."""
    if mode_setting == "off" or mode not in ("call", "follow"):
        return False
    if mode_setting == "always" or role >= Role.ADMIN:
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
    think = wants_thinking(getattr(svc.cfg, "agent_think", "off"), request, mode, ctx.role)
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

    used = checked = num_checked = read = False
    results: list[str] = []                  # 이번 실행의 도구 결과 (숫자 검사용)
    for step in range(MAX_STEPS):
        if step and run.usd_micro >= RUN_USD_CAP * costs.MICRO:   # 요금 상한: 더 찾지 않고 지금까지로 답
            log.warning("에이전트 실행 요금 상한 $%.2f 도달 (chat=%s, %d라운드) → 도구 없이 마무리", RUN_USD_CAP, ctx.chat_id, step)
            break
        msg = await call()
        calls = [c for c in (msg.tool_calls or []) if c.type == "function"]
        if not calls:
            text = msg.content or ""
            if not used:
                if checked or not allowed or mode not in ("call", "follow") or not _CLAIM.search(text):
                    return text
                checked = True                   # 한 번만 다시 물음 (추가 호출은 이 경우만)
                messages += [{"role": "assistant", "content": text}, {"role": "system", "content": VERIFY_NOTE}]
                continue
            # 조회 도구를 쓴 답: 결과에 없는 숫자를 세어 말하면 한 번만 다시 (도구 안 쓴 실행은 검사 비용 0)
            if num_checked or not read or not (bad := unsupported_numbers(text, [*results, request])):
                return text
            num_checked = True
            messages += [{"role": "assistant", "content": text},
                         {"role": "system", "content": NUMBER_NOTE.format(nums=", ".join(bad[:5]))}]
            continue
        used = True
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
                results.append(result)
                read = read or c.function.name in READ_ONLY
            log.info("도구 %s chat=%s user=%s 인자=%s → %s", c.function.name, ctx.chat_id, ctx.caller.id,
                     (c.function.arguments or "")[:200], result[:200].replace("\n", " "))
            try:
                run.step(c.function.name, c.function.arguments, result)
            except Exception:   # 기록용 요약이 답을 막으면 안 됨
                log.exception("agent log step failed")
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": wrap("tool_result", clip_mid(result, TOOL_RESULT_CHARS), nonce())})

    # 도구 라운드·요금 상한을 다 쓰면 도구 없이 마무리 답변만 받는다
    return (await call("none")).content or ""
