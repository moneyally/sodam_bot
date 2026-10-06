"""조회 도구 동시 실행 (Codex parallel.rs: 읽기는 같이, 쓰기는 혼자·순서대로) · GPT-6 도구 추론 낮음.
python tests/run_all.py agent_parallel"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import agent  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(10, "방장", "boss"), fake_user(20, "박준호", "junho")


def calls(*names):
    return SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id=f"c{i}", type="function", function=SimpleNamespace(name=n, arguments=json.dumps({})))
        for i, n in enumerate(names)])


async def room(script):
    r = Room()
    r.llm = ScriptedLLM(script)
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


class Probe:
    """agent.execute 대신: 실행 순서·동시 실행 수를 잼."""
    def __init__(self):
        self.live = self.peak = 0
        self.order: list[str] = []

    async def __call__(self, name, args, ctx):
        self.order.append(name)
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.05)
        self.live -= 1
        return f"결과:{name}"


def tool_results(llm):
    last = llm.of("chat")[-1]["messages"]
    return [m["content"] for m in last if m.get("role") == "tool"]


@test
async def leading_reads_run_together_and_results_keep_order():
    old, real = fast_timers(), agent.execute
    probe = Probe()
    agent.execute = probe
    try:
        r = await room([calls("chat_stats", "room_rules", "search_chat"), reply("다 봤어요")])
        await r.say(BOSS, "소담아 오늘 통계랑 규칙이랑 검색 다 봐줘")
        assert probe.peak == 3, probe.peak
        res = tool_results(r.llm)
        assert len(res) == 3 and all(f"결과:{n}" in x for n, x in zip(("chat_stats", "room_rules", "search_chat"), res)), res
        row = await r.db._one("SELECT events FROM agent_runs ORDER BY id DESC LIMIT 1")
        assert '"e": "parallel"' in row["events"]
    finally:
        agent.execute = real
        restore_timers(old)


@test
async def writes_stay_alone_and_reads_after_write_wait():
    old, real = fast_timers(), agent.execute
    probe = Probe()
    agent.execute = probe
    try:
        r = await room([calls("mute_member", "chat_stats", "room_rules"), reply("처리했어요")])
        await r.say(BOSS, "소담아 박준호 뮤트하고 통계랑 규칙 봐줘")
        assert probe.peak == 1 and probe.order == ["mute_member", "chat_stats", "room_rules"], (probe.peak, probe.order)
        probe2 = Probe()
        agent.execute = probe2
        r = await room([calls("chat_stats", "mute_member", "room_rules"), reply("처리했어요")])
        await r.say(BOSS, "소담아 통계 보고 박준호 뮤트하고 규칙 봐줘")
        assert probe2.peak == 1 and probe2.order == ["chat_stats", "mute_member", "room_rules"]   # 앞 조회 1개뿐 → 동시 X
    finally:
        agent.execute = real
        restore_timers(old)


@test
async def agent_asks_for_parallel_and_low_effort_except_banter():
    old = fast_timers()
    try:
        r = await room([reply("네 대표님")])
        await r.say(BOSS, "소담아 오늘 통계 좀")
        kw = r.llm.of("chat")[0]
        assert kw["parallel"] is True and kw["effort"] is None and kw["max_tokens"] == agent.THINK_MAX_TOKENS
        # 말싸움 길(mirror 방에서 소담에게 욕)은 순발력 — 생각 안 함
        import dataclasses
        r = Room()
        r.llm = ScriptedLLM([reply("ㅋㅋ 그게 다냐")])
        await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, "ai_comeback": "mirror"})
        r.svc.cfg = dataclasses.replace(r.svc.cfg, light_model="gpt-5.4-mini")
        await r.join(JUNHO)
        await r.say(JUNHO, "소담아 병신아 뭐하냐")
        kw = r.llm.of("chat")[0]
        assert kw["purpose"].endswith(":banter") and kw["effort"] == "none", (kw["purpose"], kw["effort"])
    finally:
        restore_timers(old)


if __name__ == "__main__":
    run_all()
