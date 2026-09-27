"""📒 AI 작업 기록(에이전트 실행 기록)·화면 권한: python tests/run_all.py agentlog"""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

from fake_llm import reply, tool_call
from fakes import FakeBot, fake_user, make_db, make_svc, runner
from test_budget import CHAT, OTHER, panel_world, press, usage

from sodam import agentlog, costs
from sodam.agent import run_agent
from sodam.llm import LLM, BudgetExceeded
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()


def scripted_client(script, calls):
    """chat 은 script 순서대로 (usage 붙여서), responses(웹 검색)는 '요약'."""
    async def create(**kw):
        calls.append(kw)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(usage=usage(2000, 1500, 2100), choices=[SimpleNamespace(message=item)])

    async def resp(**kw):
        calls.append(kw)
        return SimpleNamespace(usage=usage(300, 0, 400), output_text="오늘 비트코인 <b>1억</b> & 상승")
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                           responses=SimpleNamespace(create=resp))


async def world(script):
    db = await make_db()
    svc = await make_svc(db)
    svc.llm = LLM(svc.cfg, db)
    calls = []
    svc.llm.client = scripted_client(script, calls)
    await db.ensure_chat(CHAT, "방")
    ctx = ToolCtx(svc, FakeBot(), CHAT, fake_user(20, "준호"), Role.MEMBER, await db.get_settings(CHAT))
    return db, svc, ctx, calls


async def ask(ctx, request="소담아 오늘 비트코인 얼마야?"):
    return await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request=request, extras={})


async def rows(db):
    return await db._all("SELECT * FROM agent_runs ORDER BY id")


@test
async def run_records_tool_steps_usage_and_cost():
    db, svc, ctx, calls = await world([tool_call("web_search", {"query": "비트코인 시세"}), reply("1억이에요.")])
    assert await ask(ctx) == "1억이에요."
    [r] = await rows(db)
    assert (r["chat_id"], r["user_id"], r["status"], r["mode"]) == (CHAT, 20, "answered", "call")
    assert r["purpose"] == "agent:member" and r["trigger"] == "소담아 오늘 비트코인 얼마야?"
    [s] = agentlog.steps_of(r)
    assert s["tool"] == "web_search" and s["args"] == "query=비트코인 시세" and s["result"].startswith("오늘 비트코인"), s
    # 토큰 = chat 2번 + 도구 안의 웹 검색 1번 (ContextVar 로 같은 실행에 잡힘)
    assert (r["tok_in"], r["tok_cached"], r["tok_out"]) == (2 * 2000 + 300, 2 * 1500, 2 * 100 + 100), dict(r)
    want = 2 * costs.usd_micro("gpt-5.4", 2000, 1500, 100) + costs.usd_micro("gpt-5.4-mini", 300, 0, 100) + 10_000
    assert r["usd_micro"] == want and await svc.llm.usd_today(CHAT) == want and await svc.llm.usd_today() == want
    assert r["models"] == "gpt-5.4×2,gpt-5.4-mini", r["models"]


@test
async def usage_outside_run_and_other_runs_not_mixed():
    db = await make_db()
    svc = await make_svc(db)
    llm = LLM(svc.cfg, db)
    await llm._record(usage(), 5, "misc", "gpt-5.4")          # 실행 밖 → 기록 없음, 에러 없음
    assert agentlog.current.get() is None
    gate = asyncio.Event()

    async def one(cid, n):
        run, tok = agentlog.start(cid, 1, "call", "x")
        await gate.wait()
        for _ in range(n):
            await llm._record(usage(), cid, "agent:member", "gpt-5.4")
        await agentlog.finish(db, run, tok, "answered")
        return run

    t1, t2 = asyncio.create_task(one(CHAT, 1)), asyncio.create_task(one(OTHER, 3))
    await asyncio.sleep(0)
    gate.set()
    a, b = await asyncio.gather(t1, t2)
    assert (a.tok_in, b.tok_in) == (1000, 3000)
    got = {r["chat_id"]: r["tok_in"] for r in await rows(db)}
    assert got == {CHAT: 1000, OTHER: 3000}
    a.add("gpt-5.4", 5, 0, 0, 5)                              # 끝난 실행엔 더 안 붙음
    assert a.tok_in == 1000


@test
async def nested_run_adds_to_parent():
    db = await make_db()
    outer, t1 = agentlog.start(CHAT, 1, "call", "바깥")
    inner, t2 = agentlog.start(CHAT, 1, "call", "안쪽")
    agentlog.add_usage("gpt-5.4", 100, 50, 10, 7)
    await agentlog.finish(db, inner, t2, "answered")
    assert agentlog.current.get() is outer
    agentlog.add_usage("gpt-5.4", 1, 0, 0, 1)
    await agentlog.finish(db, outer, t1, "answered")
    assert (outer.tok_in, outer.usd_micro, outer.models) == (101, 8, {"gpt-5.4": 2})
    assert agentlog.current.get() is None


@test
async def budget_and_error_statuses_are_recorded_and_raised():
    db, svc, ctx, calls = await world([])
    svc.llm.usd_budget = 0.001
    await db.bump(svc.llm._today(), 0, costs.USD, 1000)
    try:
        await ask(ctx)
        raise AssertionError("한도인데 통과")
    except BudgetExceeded:
        pass
    svc.llm.usd_budget = 8.0
    svc.llm.client = scripted_client([RuntimeError("연결 끊김")], calls)
    try:
        await ask(ctx)
        raise AssertionError("오류가 삼켜짐")
    except RuntimeError:
        pass
    assert [r["status"] for r in await rows(db)] == ["budget", "error"]
    svc.llm.client = scripted_client([tool_call("room_rules", {}), reply("")], calls)
    assert await ask(ctx) == ""
    assert (await rows(db))[-1]["status"] == "tool_only"


@test
async def logging_failure_never_breaks_the_answer():
    db, svc, ctx, calls = await world([tool_call("room_rules", {}), reply("규칙은 없어요.")])
    real_save, real_sum = agentlog.save, agentlog.summarize_args

    async def boom(*a, **kw):
        raise RuntimeError("디스크 가득")

    def bad(*a, **kw):
        raise ValueError("요약 실패")
    agentlog.save, agentlog.summarize_args = boom, bad
    try:
        assert await ask(ctx) == "규칙은 없어요."
    finally:
        agentlog.save, agentlog.summarize_args = real_save, real_sum
    assert agentlog.current.get() is None
    await db.conn.execute("DROP TABLE agent_runs")               # 표가 없어도 (DB 오류)
    svc.llm.client = scripted_client([reply("네")], calls)
    assert await ask(ctx) == "네"


@test
def secrets_redacted_and_text_clipped():
    s = agentlog.summarize_args(json.dumps({"query": "x", "api_key": "abc", "note": "sk-" + "a" * 30,
                                            "bot": "123456789:" + "A" * 35, "n": [1, 2]}))
    assert "abc" not in s and "api_key=***" in s and "sk-aaa" not in s and "AAAA" not in s and "n=[1, 2]" in s, s
    assert agentlog.summarize_args("not json {") == "not json {"
    run, tok = agentlog.start(CHAT, 1, "call", "가" * 500 + "\n줄바꿈")
    agentlog.current.reset(tok)
    assert len(run.trigger) == agentlog.TRIGGER_CHARS and "\n" not in run.trigger
    run.step("t", "{}", "결과 " * 200)
    assert len(run.steps[0]["result"]) == agentlog.RESULT_CHARS
    for _ in range(30):
        run.step("t", "{}", "r")
    assert len(run.steps) == agentlog.MAX_STEPS_KEPT


@test
async def old_rows_pruned_on_insert():
    db = await make_db()
    old = int(time.time()) - (agentlog.KEEP_DAYS + 1) * 86400
    await db._write("INSERT INTO agent_runs(chat_id, ts, status) VALUES(?, ?, 'answered')", (CHAT, old))
    await db._write("INSERT INTO agent_runs(chat_id, ts, status) VALUES(?, ?, 'answered')", (CHAT, int(time.time()) - 86400))
    agentlog._last_prune = float("-inf")
    run, tok = agentlog.start(CHAT, 1, "call", "새것")
    await agentlog.finish(db, run, tok, "answered")
    assert [r["ts"] > old for r in await rows(db)] == [True, True]
    await db._write("INSERT INTO agent_runs(chat_id, ts, status) VALUES(?, ?, 'answered')", (CHAT, old))
    run, tok = agentlog.start(CHAT, 1, "call", "바로 다음")        # 한 시간에 한 번만 정리 (매번 DELETE 안 함)
    await agentlog.finish(db, run, tok, "answered")
    assert len(await rows(db)) == 4


# ── 화면 권한 ─────────────────────────────────────────────
async def seeded():
    db, svc, bot = await panel_world()
    ids = {}
    for cid in (CHAT, OTHER):
        run = agentlog.Run(cid, 20, "call", f"<i>{cid}</i> 질문")
        run.step("room_rules", "{}", "규칙 & 끝")
        run.add("gpt-5.4", 1000, 0, 100, 4000)
        ids[cid] = await agentlog.save(db, run, "answered")
    return db, svc, bot, ids


@test
async def room_admin_sees_only_own_room_without_money():
    db, svc, bot, ids = await seeded()
    q = await press(svc, bot, 1, f"m:alg:{CHAT}")
    text = q.edits[0][0]
    assert f"#{ids[CHAT]}" in text and f"#{ids[OTHER]}" not in text and "$" not in text, text
    assert "&lt;i&gt;" in text and "<i>" not in text
    q = await press(svc, bot, 1, f"m:agv:{CHAT}:{ids[CHAT]}:0")
    detail = q.edits[0][0]
    assert "room_rules" in detail and "규칙 &amp; 끝" in detail and "$" not in detail, detail
    # 다른 방 기록을 자기 방 ID 로 열기 → 없음
    q = await press(svc, bot, 1, f"m:agv:{CHAT}:{ids[OTHER]}:0")
    assert q.answers[0][1] and not q.edits
    # 다른 방 화면 자체 → 라우터가 거절
    for data in (f"m:alg:{OTHER}", f"m:agv:{OTHER}:{ids[OTHER]}:0"):
        q = await press(svc, bot, 1, data)
        assert q.answers[0][1] and not q.edits, data


@test
async def member_and_admins_cannot_open_owner_log():
    db, svc, bot, ids = await seeded()
    for data in (f"m:alg:{CHAT}", f"m:agv:{CHAT}:{ids[CHAT]}:0"):
        q = await press(svc, bot, 20, data)
        assert q.answers[0][1] and not q.edits, data
    for uid in (1, 2, 20):
        for data in ("m:alr:0", f"m:alv:{ids[CHAT]}:0", "m:alp:0", f"m:alq:{CHAT}"):
            q = await press(svc, bot, uid, data)
            assert q.answers[0][1] and not q.edits, (uid, data)
    q = await press(svc, bot, 7, "m:alr:0")
    assert f"#{ids[CHAT]}" in q.edits[0][0] and f"#{ids[OTHER]}" in q.edits[0][0]
    q = await press(svc, bot, 7, f"m:alv:{ids[OTHER]}:0")
    assert "$0.0040" in q.edits[0][0] and "gpt-5.4" in q.edits[0][0], q.edits[0][0]
    q = await press(svc, bot, 7, "m:alv:999999:0")
    assert q.answers[0][1] and not q.edits


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
