"""생각하는 에이전트 (AGENT_THINK): 요청 고르기 · Responses 변환 · 추론 이어가기 · 실패하면 예전 방식. python tests/run_all.py agent_think"""
import asyncio
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import BadRequestError
from openai.types.responses import ResponseFunctionToolCall, ResponseReasoningItem

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import cfg, fake_user, make_db, runner  # noqa: E402

from sodam import costs  # noqa: E402
from sodam.agent import wants_thinking  # noqa: E402
from sodam.llm import LLM, to_input  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(1, "방장", "boss"), fake_user(20, "박준호", "junho")


@test
def routing_picks_multistep_and_why_only():
    multi = ["방금 먹튀 얘기한 사람 찾아서 경고 줘", "매일 밤 11시에 요약 올리고, 입금 얘기하면 알려줘",
             "말투 친근하게 바꾸고 김민지 대표님한테 인사드려", "박준호 요즘 왜 계속 문제야?", "걔 좀 내보내",
             "이수진이랑 박준호 둘 다 10분 뮤트해줘", "도배 기준 엄격으로 하면 누가 걸릴지 보고 괜찮으면 바꿔줘"]
    simple = ["안녕~ 오늘 날씨 좋다", "점심 뭐 먹을까 추천해줘", "박준호 도배해서 10분 뮤트해줘", "끝말잇기 하자",
              "나도 여행 하고 싶어", "오늘 비트코인 시세 검색해서 알려줘", "이 방 말투 친근하게 바꿔줘", "밥 먹고 왔어"]
    for t in multi:
        assert wants_thinking("auto", t, "call"), t
    for t in simple:
        assert not wants_thinking("auto", t, "call"), t
    assert not wants_thinking("off", multi[0], "call")
    assert not wants_thinking("always", "안녕", "chime")          # 끼어들기는 절대 안 씀 (비용)
    assert wants_thinking("always", "안녕", "follow")


@test
def chat_messages_convert_to_responses_input():
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA", "detail": "high"}}
    items = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
             {"type": "function_call", "call_id": "c2", "name": "read_chat", "arguments": "{}"}]
    out = to_input([
        {"role": "system", "content": "규칙"},
        {"role": "user", "content": [{"type": "text", "text": "이 사진"}, img]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_chat", "arguments": '{"keyword": "먹튀"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "결과"},
        {"role": "assistant", "content": "", "tool_calls": [], "items": items},
        {"role": "tool", "tool_call_id": "c2", "content": "대화"},
    ])
    assert out[0] == {"role": "system", "content": "규칙"}
    assert out[1]["content"] == [{"type": "input_text", "text": "이 사진"},
                                 {"type": "input_image", "image_url": "data:image/png;base64,AA", "detail": "high"}]
    assert out[2] == {"type": "function_call", "call_id": "c1", "name": "search_chat", "arguments": '{"keyword": "먹튀"}'}
    assert out[3] == {"type": "function_call_output", "call_id": "c1", "output": "결과"}
    assert out[4:6] == items                                     # 추론 항목은 뒤따르는 function_call 과 그대로
    assert out[6] == {"type": "function_call_output", "call_id": "c2", "output": "대화"}


def _response(output, text=""):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=300, total_tokens=1300,
                            input_tokens_details=SimpleNamespace(cached_tokens=600))
    return SimpleNamespace(output=output, output_text=text, usage=usage)


@test
async def think_calls_responses_with_reasoning_and_tools():
    db = await make_db()
    llm = LLM(cfg(db.path), db)
    sent = []
    out = [ResponseReasoningItem(id="rs_1", type="reasoning", summary=[], encrypted_content="enc"),
           ResponseFunctionToolCall(id="fc_1", type="function_call", call_id="call_1", name="read_chat", arguments='{"hours": 3}')]

    async def create(**kw):
        sent.append(kw)
        return _response(out)
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    schema = {"type": "function", "function": {"name": "read_chat", "description": "d", "parameters": {"type": "object"}}}
    msg = await llm.think([{"role": "user", "content": "요약"}], tools=[schema], effort="medium", purpose="agent:admin:think",
                          chat_id=-100)
    kw = sent[0]
    assert kw["reasoning"] == {"effort": "medium"} and kw["store"] is False
    assert kw["include"] == ["reasoning.encrypted_content"] and kw["parallel_tool_calls"] is False
    assert kw["tools"] == [{"type": "function", "name": "read_chat", "description": "d", "parameters": {"type": "object"},
                            "strict": False}]
    assert kw["prompt_cache_key"] == "sodam:agent:admin:think" and kw["model"] == "gpt-5.4"
    assert msg.tool_calls[0].id == "call_1" and msg.tool_calls[0].function.name == "read_chat"
    assert msg.items[0]["encrypted_content"] == "enc" and msg.items[1]["call_id"] == "call_1"
    # Responses usage(input_tokens) 도 요금으로: 400×2.5 + 600×0.25 + 300×15 = 5650 마이크로달러
    assert await db.counter(llm._today(), -100, costs.ROOM_USD) == 5650


class ThinkLLM(ScriptedLLM):
    def __init__(self, think_script=(), fail=None):
        super().__init__()
        self.think_script, self.fail, self.think_calls = list(think_script), fail, []

    async def think(self, messages, **kw):
        self.think_calls.append({"input": to_input(messages), **kw})
        if self.fail:
            raise self.fail
        return self.think_script.pop(0)


def _thought(msg, *items):
    msg.items = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"}, *items]
    return msg


async def _room(think="auto", llm=None):
    r = Room()
    r.llm = llm or ThinkLLM()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    r.svc.cfg = dataclasses.replace(r.svc.cfg, agent_think=think)
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


@test
async def think_run_carries_reasoning_across_tool_rounds():
    old = fast_timers()
    try:
        call = tool_call("search_chat", {"keyword": "먹튀"}, "call_1")
        llm = ThinkLLM([_thought(call, {"type": "function_call", "call_id": "call_1", "name": "search_chat",
                                        "arguments": '{"keyword": "먹튀"}'}),
                        _thought(reply("박준호 대표님이었어요."))])
        r = await _room(llm=llm)
        await r.say(BOSS, "소담아 방금 먹튀 얘기한 사람 찾아서 알려줘")
        assert not llm.of("chat", "agent:admin")                     # 예전 방식은 안 부름
        assert len(llm.think_calls) == 2 and llm.think_calls[0]["purpose"] == "agent:admin:think"
        second = llm.think_calls[1]["input"]
        assert {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"} in second
        assert second[-1]["type"] == "function_call_output" and second[-1]["call_id"] == "call_1"
        run = await r.db._one("SELECT purpose, status FROM agent_runs ORDER BY id DESC LIMIT 1")
        assert (run["purpose"], run["status"]) == ("agent:admin:think", "answered")
    finally:
        restore_timers(old)


@test
async def simple_request_and_flag_off_stay_on_chat():
    old = fast_timers()
    try:
        for think, text in (("auto", "소담아 안녕"), ("off", "소담아 방금 먹튀 얘기한 사람 찾아서 알려줘")):
            r = await _room(think)
            r.llm.script = [reply("안녕하세요!")]
            await r.say(BOSS, text)
            assert not r.llm.think_calls and len(r.llm.of("chat", "agent:admin")) == 1, think
    finally:
        restore_timers(old)


@test
async def think_rejected_falls_back_to_chat():
    old = fast_timers()
    try:
        bad = BadRequestError("no", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
        r = await _room(llm=ThinkLLM(fail=bad))
        r.llm.script = [reply("찾아볼게요.")]
        await r.say(BOSS, "소담아 방금 먹튀 얘기한 사람 찾아서 알려줘")
        assert len(r.llm.think_calls) == 1 and len(r.llm.of("chat", "agent:admin")) == 1
        run = await r.db._one("SELECT purpose FROM agent_runs ORDER BY id DESC LIMIT 1")
        assert run["purpose"] == "agent:admin"
    finally:
        restore_timers(old)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
