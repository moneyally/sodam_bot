"""GPT-6 (sol·luna) 전환: python tests/run_all.py gpt6

OpenAI 옮겨가기 가이드·프롬프트 캐시 문서(2026-10-05)와 codex models.json 대조:
- GPT-6 은 전부 Responses (Chat Completions 는 추론 none 일 때만 도구, 6.1-sol·astra 는 도구 없음 — 코덱스도 Responses 만)
- 캐시: prompt_cache_retention 대신 prompt_cache_options, 고정 지시 끝에만 prompt_cache_breakpoint (요청까지 1.25배로 쓰지 않게)
- 캐시 쓰기 토큰(input_tokens_details.cache_write_tokens)은 입력값 × 1.25 로 요금 계산
- 거절되면 예전 방식으로: 캐시 지점 → 자동 캐시, 모델 이름 → gpt-5.4 / gpt-5.4-mini
"""
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import BadRequestError, NotFoundError
from openai.types.responses import ResponseFunctionToolCall

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import cfg, make_db, runner  # noqa: E402

from sodam import costs  # noqa: E402
from sodam.llm import LLM, fix_effort, with_breakpoint  # noqa: E402

test, run_all = runner()
SCHEMA = {"type": "function", "function": {"name": "read_chat", "description": "d", "parameters": {"type": "object"}}}
MSGS = [{"role": "system", "content": "고정 규칙"}, {"role": "system", "content": "[말투]"},
        {"role": "user", "content": "요청"}]


def _err(cls, msg, status, code=None):
    return cls(msg, response=httpx.Response(status, request=httpx.Request("POST", "https://x")),
               body={"error": {"code": code, "message": msg}} if code else None)


def _usage(inp=2000, cached=1200, written=0, out=100):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, total_tokens=inp + out,
                           input_tokens_details=SimpleNamespace(cached_tokens=cached, cache_write_tokens=written))


class Client:
    def __init__(self, fail=()):
        self.resp, self.chat_kw, self.fail = [], [], list(fail)

        async def r_create(**kw):
            self.resp.append(kw)
            if self.fail:
                raise self.fail.pop(0)
            out = [ResponseFunctionToolCall(id="fc", type="function_call", call_id="c1", name="read_chat", arguments="{}")]
            return SimpleNamespace(output=out if kw.get("tools") else [], output_text="" if kw.get("tools") else '{"ok": 1}',
                                   usage=_usage())

        async def c_create(**kw):
            self.chat_kw.append(kw)
            msg = SimpleNamespace(content="예전 방식", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                                   usage=SimpleNamespace(prompt_tokens=100, total_tokens=120,
                                                         prompt_tokens_details=SimpleNamespace(cached_tokens=0)))
        self.responses = SimpleNamespace(create=r_create)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=c_create))


async def llm_with(model="gpt-6-sol", fail=(), retention="24h"):
    db = await make_db()
    llm = LLM(dataclasses.replace(cfg(db.path), model=model, cache_retention=retention), db)
    llm.client = Client(fail)
    return llm, db


@test
async def gpt6_agent_call_goes_to_responses_with_explicit_cache_point():
    llm, _ = await llm_with()
    msg = await llm.chat(MSGS, tools=[SCHEMA], purpose="agent:admin", chat_id=-100, cache_key="agent:abc")
    kw = llm.client.resp[0]
    assert not llm.client.chat_kw and kw["model"] == "gpt-6-sol"
    assert kw["reasoning"] == {"effort": "none"} and kw["text"] == {"verbosity": "low"} and kw["store"] is False
    assert kw["prompt_cache_options"] == {"mode": "explicit"} and "prompt_cache_retention" not in kw
    assert kw["prompt_cache_key"] == "sodam:agent:abc"
    first, second, user = kw["input"]
    assert first["content"] == [{"type": "input_text", "text": "고정 규칙",
                                 "prompt_cache_breakpoint": {"mode": "explicit"}}]          # 모든 방이 같은 첫 system 끝에만
    assert second == {"role": "system", "content": "[말투]"}                                   # 말투(방·사람마다 다름)는 지점 뒤
    assert user == {"role": "user", "content": "요청"}                                         # 요청엔 쓰기 지점 없음
    assert kw["tools"][0]["name"] == "read_chat" and msg.tool_calls[0].function.name == "read_chat"


@test
async def gpt6_json_and_effort_rules():
    llm, _ = await llm_with("gpt-6-luna")
    data = await llm.json("JSON 으로 답", "글", model="gpt-6-luna", effort="minimal")
    kw = llm.client.resp[0]
    assert data == {"ok": 1} and kw["text"] == {"verbosity": "low", "format": {"type": "json_object"}}
    assert kw["reasoning"] == {"effort": "low"}, "GPT-6 엔 minimal 이 없음 → low"
    assert fix_effort("gpt-6.1-sol", "none") == "low" and fix_effort("gpt-6-sol", "none") == "none"
    assert fix_effort("gpt-5.4", "minimal") == "minimal" and fix_effort("gpt-6-astra", None) is None
    await llm.chat([{"role": "user", "content": "x"}], model="gpt-6-luna")
    assert llm.client.resp[-1]["reasoning"] == {"effort": "none"}, ".env 값이 없으면 none (기본 medium 이면 느리고 비쌈)"


@test
async def gpt54_path_unchanged():
    llm, _ = await llm_with("gpt-5.4")
    msg = await llm.chat(MSGS, tools=[SCHEMA], purpose="agent:admin")
    kw = llm.client.chat_kw[0]
    assert not llm.client.resp and msg.content == "예전 방식"
    assert kw["reasoning_effort"] == "none" and kw["prompt_cache_retention"] == "24h" and "prompt_cache_options" not in kw


@test
async def rejected_cache_point_falls_back_to_implicit_cache():
    llm, _ = await llm_with(fail=[_err(BadRequestError, "Unknown parameter: 'prompt_cache_options'.", 400)])
    msg = await llm.chat(MSGS, tools=[SCHEMA])
    first, again = llm.client.resp
    assert "prompt_cache_options" in first and "prompt_cache_options" not in again and llm.cache_opts_off
    assert "prompt_cache_retention" not in again, "GPT-6 엔 예전 retention 도 안 보냄"
    assert all("prompt_cache_breakpoint" not in str(i) for i in again["input"])
    assert msg.tool_calls
    await llm.chat(MSGS)
    assert "prompt_cache_options" not in llm.client.resp[-1]                     # 이 프로세스에선 계속 자동 캐시


@test
async def rejected_model_falls_back_and_stays_back():
    llm, _ = await llm_with(fail=[_err(NotFoundError, "The model `gpt-6-sol` does not exist or you do not have access to it.",
                                       404, "model_not_found")])
    msg = await llm.chat(MSGS, tools=[SCHEMA], chat_id=-100)
    assert [k["model"] for k in llm.client.resp] == ["gpt-6-sol", "gpt-5.4"] and msg.tool_calls
    assert "gpt-6-sol" in llm.models_off
    await llm.chat(MSGS, tools=[SCHEMA])                                         # 다음부턴 바로 예전 모델 (chat 경로)
    assert llm.client.chat_kw[-1]["model"] == "gpt-5.4"
    other = _err(BadRequestError, "Invalid value for 'input'.", 400)
    llm2, _ = await llm_with(fail=[other])
    try:
        await llm2.chat(MSGS)
        raise AssertionError("다른 400 은 그대로 올라와야 함")
    except BadRequestError:
        assert not llm2.models_off


@test
async def cache_writes_are_billed_at_125_percent():
    assert abs(costs.token_cost("gpt-6-sol", 2000, 1200, 100, written=800)
               - (800 * 2.0 * 1.25 + 1200 * 0.2 + 100 * 10) / 1e6) < 1e-12
    assert costs.token_cost("gpt-6-sol", 2000, 1200, 100) == (800 * 2.0 + 1200 * 0.2 + 100 * 10) / 1e6
    assert costs.token_cost("gpt-6-luna", 1_000_000, 0, 0) == 0.10
    llm, db = await llm_with()
    await llm._record(_usage(2000, 1200, 800, 100), -100, "agent", "gpt-6-sol")
    assert await db.counter(llm._today(), 0, "m:gpt-6-sol:written") == 800
    assert await db.counter(llm._today(), -100, costs.ROOM_USD) == 3240        # 2000 + 240 + 1000 마이크로달러


@test
async def web_search_falls_back_when_guard_model_rejected():
    llm, _ = await llm_with(fail=[_err(NotFoundError, "model gpt-6-luna not found", 404, "model_not_found")])
    llm.cfg = dataclasses.replace(llm.cfg, guard_model="gpt-6-luna")
    out = await llm.web_search("날씨")
    assert [k["model"] for k in llm.client.resp] == ["gpt-6-luna", "gpt-5.4-mini"] and out == ""


@test
def breakpoint_only_on_first_system():
    items = [{"role": "user", "content": "x"}]
    assert with_breakpoint(items) == items                                       # system 없으면 그대로
    parts = [{"role": "system", "content": [{"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"}]},
             {"role": "user", "content": "x"}, {"role": "system", "content": "늦은 system"}]
    out = with_breakpoint(parts)
    assert "prompt_cache_breakpoint" not in out[0]["content"][0] and out[0]["content"][1]["prompt_cache_breakpoint"]
    assert out[2] == parts[2] and "prompt_cache_breakpoint" not in parts[0]["content"][1], "원본은 안 바꿈"
    two = [{"role": "system", "content": "고정"}, {"role": "system", "content": "말투"}, {"role": "user", "content": "x"}]
    out = with_breakpoint(two)
    assert out[0]["content"][0]["prompt_cache_breakpoint"] and out[1] == two[1], "지점은 첫 system 뿐 (방·말투마다 캐시가 갈리지 않게)"


if __name__ == "__main__":
    import asyncio
    sys.exit(1 if asyncio.run(run_all()) else 0)
