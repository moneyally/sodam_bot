"""🧠 소담이 왜 틀렸나 (sodam/whyfail.py · mistakes.py · diag why): python tests/run_all.py whyfail

실제 사례 2026-10-03 일루왕: 멤버 '소담아 멈춰' → 도구 없이 '멈췄습니다'. 이런 실수를 기록만 보고 코드가 '어디서·왜' 찾아야 함.
"""
import asyncio
import time

from fake_llm import Room, reply, tool_call
from fakes import fake_user
from test_diag import Server, call
from test_sanction_multi import A, BOSS, room

from sodam import diag, mistakes, tools, whyfail
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.tools import ToolCtx
from fakes import runner

test, run_all = runner()


async def ask(r, caller, script, request, role=Role.ADMIN):
    r.llm.script = list(script)
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, caller, role, await r.db.get_settings(Room.CHAT))
    return await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request=request, extras={})


async def last(r):
    return await r.db._one("SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1")


@test
async def false_claim_without_tool_is_found_with_stage_and_check_event():
    r = await room()
    await ask(r, A, [reply("멈췄습니다, 대표님."), reply("멈췄습니다!")], "소담아 멈춰", Role.MEMBER)
    row = await last(r)
    assert row["answer"] == "멈췄습니다!" and '"check"' in row["events"] and '"route"' in row["events"], dict(row)
    found = whyfail.analyze(row)
    assert [f.code for f in found] == ["claim"] and found[0].stage == "답변" and "도구를 안 씀" in found[0].why
    lines = whyfail.timeline(row)
    assert any("보내기 전 검사" in x for x in lines) and lines[-1].endswith("멈췄습니다!"), lines


@test
async def refused_write_then_claim_names_the_blocked_tool():
    r = await room()
    await r.db.log_message(Room.CHAT, A.id, 900, "소담아 금지어에 공지 추가해")
    await ask(r, BOSS, [tool_call("read_chat", {}), tool_call("edit_list", {"list": "banned_words", "op": "add", "items": ["공지"]}),
                        reply("금지어에 공지 추가했어요.")], "대화 보고 정리해")
    row = await last(r)
    st = whyfail.steps(row)
    assert st[0]["gate"] == "ok" and "w" not in st[0] and st[1]["gate"] == "security" and st[1]["w"] == 1, st
    found = whyfail.analyze(row)
    assert found and found[0].code == "claim" and "edit_list" in found[0].why, found
    # 실제로 된 쓰기(확인 카드 포함)가 있으면 '했다'는 실수 아님
    await ask(r, BOSS, [tool_call("edit_list", {"list": "banned_words", "op": "add", "items": ["먹튀"]}),
                        reply("금지어에 먹튀 추가했어요.")], "금지어 먹튀 추가")
    assert not whyfail.analyze(await last(r))


@test
async def redo_and_complaint_and_tool_error_signals():
    base = {"id": 1, "chat_id": -1, "user_id": 5, "ts": 1000, "trigger": "방 전체 태그해줘 새로온사람말고", "status": "answered",
            "steps": '[{"tool": "greet_members", "args": "", "result": "도구 실행 중 오류가 났음", "gate": "error", "w": 1}]',
            "events": "[]", "answer": "태그했어요", "mode": "call", "purpose": "agent:admin", "ms": 1, "usd_micro": 0}
    later = [{"id": 2, "ts": 1200, "trigger": "아니 방 전체 태그해줘 새로온사람 말고"}]
    found = {f.code: f for f in whyfail.analyze(base, later, ["소담아 안되잖아"])}
    assert set(found) == {"claim", "error", "redo", "complaint"}, found
    assert "#2" in found["redo"].why and found["redo"].stage == "결과"
    assert not whyfail.analyze(base | {"steps": "[]", "answer": "좋아요"}, [{"id": 3, "ts": 1000 + 601, "trigger": base["trigger"]}],
                               ["ㅋㅋ 좋다"]), "10분 넘음·불만 아님"
    assert whyfail.gate_of(tools.ROOM_READ_REFUSED) == "security" and whyfail.gate_of("12명 찾음") == "ok"


@test
async def owner_daily_report_and_diag_why():
    r = await room()
    await ask(r, A, [reply("뮤트했어요."), reply("뮤트했어요.")], "소담아 쟤 뮤트해", Role.MEMBER)
    await ask(r, A, [reply("안녕하세요!")], "소담아 안녕", Role.MEMBER)
    bad = await last(r)
    bad = await r.db._one("SELECT * FROM agent_runs WHERE trigger LIKE '%뮤트%'")
    now = int(time.time())
    text = await mistakes.report(r.db, now - 3600, now + 60, "오늘")
    assert text and "실수 1건" in text and "AI 답 2번" in text and "꼬인 곳: 답변" in text and f"#{bad['id']}" in text, text
    assert await mistakes.report(r.db, now + 100, now + 200, "내일") is None, "실수 없으면 안 보냄"
    # 하루 한 번 오너 1:1 (9시 지나서)
    sent = []
    r.svc.perms.owners = lambda: asyncio.sleep(0, {7})
    mistakes.HOUR = 0

    class B:
        async def send_message(self, uid, text, **kw):
            sent.append((uid, text))
    await mistakes.tick(r.svc, B())
    await mistakes.tick(r.svc, B())
    assert len(sent) <= 1, "날짜 claim 으로 하루 한 번"
    # Claude 창구: why
    s = Server(r.db.path)
    try:
        tok = diag.ensure_token(diag.token_path(r.db.path))
        code, out = await call(s, "/v1/why?hours=2", tok)
        assert code == 200 and out["runs_checked"] == 2 and out["by_code"] == {"claim": 1}, out
        assert out["runs"][0]["run"] == bad["id"] and "답변" in out["runs"][0]["findings"][0]
        code, one = await call(s, f"/v1/why?run={bad['id']}", tok)
        assert code == 200 and one["findings"][0]["code"] == "claim" and "받은 말" in one["timeline"][1], one
    finally:
        s.close()


if __name__ == "__main__":
    asyncio.run(run_all())
