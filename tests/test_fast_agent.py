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
async def keep_typing_self_ends_and_does_not_loop_forever():
    """'입력 중' 태스크는 TYPING_MAX_SEC 뒤 스스로 끝나야 한다 — cancel 이 어쩌다 안 되거나
    run_agent 가 매달려도 '입력 중' 이 몇십 분 유령으로 남지 않게 (2026-11-14 태산방 30분 먹통)."""
    old = handlers.TYPING_MAX_SEC
    handlers.TYPING_MAX_SEC = 0.15

    class _Bot:
        async def send_chat_action(self, *a, **k):
            pass

    try:
        # 상한이 있으면 0.15초에 스스로 끝남. 없으면(while True) 무한 반복 → wait_for 가 TimeoutError
        await asyncio.wait_for(handlers._keep_typing(_Bot(), -100, every=0.05), timeout=3.0)
    except asyncio.TimeoutError:
        raise AssertionError("입력중 태스크가 상한 없이 무한 반복 — 유령 입력중 영구화")
    finally:
        handlers.TYPING_MAX_SEC = old


@test
async def a_hung_model_call_is_cut_at_cap_not_hanging_13min():
    # 2026-10-26 실측: 스트리밍 추론 한 호출이 783초 매달림 (DEADLINE 은 라운드 사이에서만 재서 못 끊음)
    r = await Room().open(admins={BOSS.id})
    await r.join(A)
    old_cap = agent.CALL_CAP
    agent.CALL_CAP = 0.1

    async def slow(*a, **k):
        r.llm.calls.append({"kind": "chat", "tool_choice": k.get("tool_choice", "auto")})
        await asyncio.sleep(5)        # 매달림 흉내 (CALL_CAP 0.1초보다 훨씬 김)
        return reply("늦은 답")
    r.llm.chat = slow
    try:
        ctx = ToolCtx(r.svc, r.bot, r.CHAT, A, Role.MEMBER, await r.db.get_settings(r.CHAT))
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        # 상한이 안 걸리면 5초 sleep 에 막혀 이 2초 타임아웃에 걸림 → 시험 실패(= 버그 잡음)
        text = await asyncio.wait_for(
            run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="뭐해", extras={}),
            timeout=2)
        dt = loop.time() - t0
    finally:
        agent.CALL_CAP = old_cap
    assert dt < 1.5, f"호출 상한에 안 걸리고 매달림: {dt:.2f}s"
    assert "밀려서" in text or text == "", text     # 끊긴 뒤 짧게 돌려보냄


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


@test
async def concurrency_cap_limits_parallel_run_agents():
    """🧭 동시 AI 답 생성 상한(MAX_CONCURRENT) — 바쁜 방·여러 방이 한꺼번에 불러도 N개까지만 돌게
    (OpenAI 왕복·메모리·비용 완충, 2026-11-16). 폭주해도 상한 넘게 동시에 뜨지 않는다."""
    peak = cur = 0

    async def fake(*a, **k):
        nonlocal peak, cur
        cur += 1
        peak = max(peak, cur)
        await asyncio.sleep(0.03)
        cur -= 1
        return "ok"

    orig_run, orig_max = agent._run_agent, agent.MAX_CONCURRENT
    agent._run_agent = fake
    agent.MAX_CONCURRENT = 2
    agent._SEM_BY_LOOP.clear()   # 새 상한으로 세마포어를 다시 만들게
    try:
        res = await asyncio.gather(*[
            run_agent(None, style_key="", notes={}, history=[], reply_to=None, request="x")
            for _ in range(6)])
        assert res == ["ok"] * 6
        assert peak == 2, f"동시 실행이 상한 2 에 묶여야 하는데 {peak} 개가 같이 돌았다"
    finally:
        agent._run_agent, agent.MAX_CONCURRENT = orig_run, orig_max
        agent._SEM_BY_LOOP.clear()


@test
async def concurrency_cap_off_means_unlimited():
    """상한 0 = 끔 → 예전처럼 전부 동시에 (회귀: 기능을 꺼도 run_agent 가 그대로 돈다)."""
    cur = peak = 0

    async def fake(*a, **k):
        nonlocal peak, cur
        cur += 1
        peak = max(peak, cur)
        await asyncio.sleep(0.02)
        cur -= 1
        return "ok"

    orig_run, orig_max = agent._run_agent, agent.MAX_CONCURRENT
    agent._run_agent = fake
    agent.MAX_CONCURRENT = 0
    agent._SEM_BY_LOOP.clear()
    try:
        await asyncio.gather(*[
            run_agent(None, style_key="", notes={}, history=[], reply_to=None, request="x")
            for _ in range(5)])
        assert peak == 5, f"상한 0 이면 5 개 다 동시여야 하는데 {peak}"
    finally:
        agent._run_agent, agent.MAX_CONCURRENT = orig_run, orig_max
        agent._SEM_BY_LOOP.clear()


@test
async def hung_run_agent_releases_slot_within_hard_cap():
    """세마포어 슬롯을 쥔 _run_agent 가 극단적으로 매달려도 RUN_HARD_CAP 안에 반드시 슬롯을 돌려줘야 한다.
    2026-11-27 베베방 8분 먹통 근본원인: social(끼어들기·이어말하기)·askchoice 가 타임아웃 없이 run_agent 를 불러
    내부 CALL_CAP·DEADLINE 이 못 잡는 hang 이 슬롯을 영구 점유 → 24칸이 다 말라 전체 응답이 먹통. 안 풀리면 뒤 요청이 영원히 막힌다."""
    async def hung(*a, **k):
        await asyncio.Event().wait()   # 영원히 안 끝남

    orig_run, orig_max, orig_cap = agent._run_agent, agent.MAX_CONCURRENT, agent.RUN_HARD_CAP
    agent._run_agent = hung
    agent.MAX_CONCURRENT = 1           # 슬롯 1칸뿐 — 안 풀리면 다음 호출이 영원히 막힘
    agent.RUN_HARD_CAP = 0.1
    agent._SEM_BY_LOOP.clear()
    try:
        try:
            await run_agent(None, style_key="", notes={}, history=[], reply_to=None, request="x")
            assert False, "매달린 호출이 상한에 끊겨야 함"
        except asyncio.TimeoutError:
            pass
        async def ok(*a, **k):
            return "ok"
        agent._run_agent = ok
        got = await asyncio.wait_for(                      # 슬롯이 안 풀렸으면 여기서 막혀 TimeoutError 로 실패
            run_agent(None, style_key="", notes={}, history=[], reply_to=None, request="y"), 2)
        assert got == "ok", got
    finally:
        agent._run_agent, agent.MAX_CONCURRENT, agent.RUN_HARD_CAP = orig_run, orig_max, orig_cap
        agent._SEM_BY_LOOP.clear()
