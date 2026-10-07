"""빠른 답 (조사 2026-09-28): 시간 상한 · '입력 중' 유지. python tests/run_all.py test_fast_agent"""
import asyncio
from types import SimpleNamespace

from fake_llm import Room, reply, tool_call
from fakes import runner
from test_sanction_multi import A, BOSS

from sodam import agent, handlers
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()


@test
async def deadline_stops_searching_and_answers_with_what_it_has():
    r = await Room().open(admins={BOSS.id})
    await r.join(A)
    old = dict(agent.DEADLINE)
    agent.DEADLINE.update(group=0, dm=0)
    try:
        r.llm.script = [tool_call("chat_stats", {}), reply("지금까지 본 걸로 답해요")]
        ctx = ToolCtx(r.svc, r.bot, r.CHAT, A, Role.MEMBER, await r.db.get_settings(r.CHAT))
        text = await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="통계", extras={})
    finally:
        agent.DEADLINE.update(old)
    calls = r.llm.of("chat")
    assert calls[-1]["tool_choice"] == "none", "시간이 지나면 도구 없이 마무리"
    assert sum(1 for m in calls[-1]["messages"] if m["role"] == "tool") == 1 and "답해요" in text


@test
async def media_run_gets_longer_deadline_in_group():
    """그림·스티커를 만든 실행은 25초가 아니라 MEDIA_DEADLINE 까지 (2026-10-07 실측: 원본 고치기 16초 + 렌더 → 경고 고칠 기회 없이 버림)."""
    r = await Room().open(admins={BOSS.id})
    await r.join(A)
    old, real = dict(agent.DEADLINE), agent.execute

    async def fake(name, args, ctx):
        return "만들었지만 검수 경고 (아직 안 보냄): 자막이 피사체를 17% 덮음" if name == "make_sticker" else "통계"
    agent.DEADLINE.update(group=0, dm=0)
    agent.execute = fake
    try:
        r.llm.script = [tool_call("make_sticker", {"spec": {}}), tool_call("make_sticker", {"spec": {}}), reply("보냈어요")]
        ctx = ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT))
        await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="스티커", extras={})
        calls = r.llm.of("chat")
        assert len(calls) == 3 and calls[1]["tool_choice"] != "none", "만들기 실행은 0초 상한에도 한 번 더 고칠 기회"
    finally:
        agent.DEADLINE.update(old)
        agent.execute = real


@test
async def typing_is_kept_alive_while_thinking():
    """'입력 중' 은 한 번에 5초만 보임 → 답이 나올 때까지 되풀이, 끝나면 멈춤."""
    sent = []

    class Bot:
        async def send_chat_action(self, chat_id, action):
            sent.append(chat_id)
    task = asyncio.create_task(handlers._keep_typing(Bot(), -100, every=0.01))
    await asyncio.sleep(0.08)
    task.cancel()
    n = len(sent)
    await asyncio.sleep(0.05)
    assert n >= 3 and len(sent) == n, (n, len(sent))
    assert "_keep_typing(bot, chat_id)" in open(handlers.__file__).read(), "AI 답 만드는 동안 켜 둠"
