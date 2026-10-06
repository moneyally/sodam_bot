"""❓ 되묻기 버튼 ask_choice (sodam/panels/askchoice.py): python tests/run_all.py askchoice

진짜 handlers(on_group_message·on_private·on_callback) → ai_reply → run_agent → 도구 → 버튼 → 누름 → 다시 실행.
"""
import asyncio
import time
from types import SimpleNamespace

from fake_llm import Room, reply, tool_call
from fakes import FakeMsg, FakeQuery, fake_user, runner

from sodam import agentlog, handlers, tools
from sodam.agent import CHIME_TOOLS, run_agent
from sodam.panels import askchoice
from sodam.permissions import Role
from sodam.tools import ToolCtx, available, execute

test, run_all = runner()
BOSS, A, B = fake_user(1, "방장", "boss"), fake_user(20, "철수", "cs1"), fake_user(21, "철수", "cs2")
Q = {"question": "어느 철수님을 1시간 뮤트할까요?", "options": ["철수 (@cs1)", "철수 (@cs2)"]}


async def room():
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False, "ai_sanction_card": "all"})
    for u in (BOSS, A, B):
        await r.join(u)
    return r


def cards(r, chat=Room.CHAT):
    return [c for c in r.bot.named("send_message") if c[1] == chat and c[3].get("reply_markup") is not None
            and "❓" in c[2]]


def buttons(card):
    return [b for row in card[3]["reply_markup"].inline_keyboard for b in row]


async def press(r, user, data, chat=Room.CHAT):
    q = FakeQuery(chat, user, data)
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    await asyncio.gather(*list(askchoice.TASKS))   # 누른 뒤 뒤에서 도는 에이전트
    return q


async def asked(r, script_after=("알겠어요.",)):
    """방장이 '철수 뮤트해' → AI 가 ask_choice. 질문 카드를 돌려줌."""
    r.llm.script = [tool_call("ask_choice", Q), *script_after]
    m = await r.say(BOSS, "소담아 철수 1시간 뮤트해")
    [card] = cards(r)
    return m, card


@test
async def posts_buttons_as_reply_and_stays_quiet():
    r = await room()
    m, card = await asked(r)
    btns = buttons(card)
    assert [b.text for b in btns] == ["철수 (@cs1)", "철수 (@cs2)", "✏️ 직접 입력"], btns
    assert all(len(b.callback_data.encode()) <= 64 and b.callback_data.startswith("m:k:") for b in btns)
    assert card[3]["reply_parameters"].message_id == m.message_id, "요청한 메시지에 답장"
    assert "어느 철수님을" in card[2] and "방장님만" in card[2]
    assert not m.replies, "ctx.quiet: AI 글 답은 안 보냄"
    # 버튼을 올렸으면 그 라운드로 끝 (agent.TERMINAL — 버려질 AI 답을 쓰려고 또 부르지 않음)
    assert len(r.llm.of("chat")) == 1
    row = await r.db._one("SELECT steps, events FROM agent_runs ORDER BY id DESC LIMIT 1")
    assert askchoice.SENT[:20] in row["steps"] and '"e": "terminal"' in row["events"], row
    assert await r.db.is_ai_message(Room.CHAT, r.bot._next_id), "질문 = AI 답으로 기록 → 답장으로 직접 쓰면 호출"


@test
async def labels_are_escaped_and_validated():
    r = await room()
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(Room.CHAT))
    bad = [{"question": "q", "options": ["하나"]}, {"question": "q", "options": list("abcde")},
           {"question": "q", "options": ["a" * 21, "b"]}, {"question": "x" * 121, "options": ["a", "b"]},
           {"question": "q", "options": ["같음", "같음"]}, {"question": "q", "options": "a,b"}]
    for a in bad:
        res = await askchoice.t_ask_choice(ctx, a)
        assert res != askchoice.SENT and not cards(r), (a, res)
    res = await askchoice.t_ask_choice(ctx, {"question": "<b>누구</b> & 뭐?", "options": ["<i>x</i>", "y&z"]})
    assert res == askchoice.SENT
    [card] = cards(r)
    assert "&lt;b&gt;누구&lt;/b&gt; &amp; 뭐?" in card[2], card[2]
    assert [b.text for b in buttons(card)][:2] == ["<i>x</i>", "y&z"], "버튼 글자는 HTML 이 아님 (그대로)"


@test
async def only_requester_can_press():
    r = await room()
    _, card = await asked(r)
    pick = buttons(card)[0].callback_data
    q = await press(r, A, pick)
    assert q.answers == [("요청한 사람만 누를 수 있어요.", True)] and not q.edits
    assert (await r.db._one("SELECT status FROM ask_choices"))["status"] is None, "남이 눌러도 그대로"
    q = await press(r, B, buttons(card)[2].callback_data)                # ✏️ 도 요청자만
    assert q.answers[0][0] == "요청한 사람만 누를 수 있어요." and not q.edits


@test
async def press_reruns_agent_with_label_and_context():
    r = await room()
    m, card = await asked(r)
    qmsg = r.bot._next_id
    seen = {}

    def rerun(messages):
        seen["user"] = messages[-1]["content"]
        seen["all"] = "\n".join(str(x.get("content")) for x in messages)
        return reply("철수(@cs1)님으로 할게요.")
    r.llm.script = [rerun]
    q = await press(r, BOSS, buttons(card)[0].callback_data)
    assert len(q.answers) == 1 and "골랐어요" in q.answers[0][0], q.answers
    assert "✅ <b>철수 (@cs1)</b>" in q.edits[-1] and q.kb is None, "질문을 고른 것으로 고치고 버튼 없앰"
    assert "(선택: 철수 (@cs1)) 어느 철수님을 1시간 뮤트할까요?" in seen["user"], seen["user"]
    assert "철수 1시간 뮤트해" in seen["all"], "지난 대화(원래 요청)가 이어짐"
    [ans] = [c for c in r.bot.named("send_message") if "으로 할게요" in c[2]]
    assert ans[3]["reply_parameters"].message_id == qmsg, "질문에 답장"
    turn = await r.db._one("SELECT * FROM ai_turns ORDER BY id DESC LIMIT 1")
    assert turn["request"].startswith("(선택: 철수 (@cs1))") and "으로 할게요" in turn["answer"], dict(turn)
    run = await r.db._one("SELECT * FROM agent_runs ORDER BY rowid DESC LIMIT 1")
    assert run is not None and "(선택:" in run["trigger"], "AI 작업 기록에도 한 줄"


@test
async def double_press_runs_once():
    r = await room()
    _, card = await asked(r)
    a, b = buttons(card)[0].callback_data, buttons(card)[1].callback_data
    r.llm.script = [reply("첫 번째")]
    await press(r, BOSS, a)
    q2 = await press(r, BOSS, b)                                         # 다른 보기 → 이미 고름
    q3 = await press(r, BOSS, a)                                         # 같은 보기 → 토큰 이미 씀
    assert q2.answers == [("이미 골랐어요.", False)] and not q2.edits, q2.answers
    assert len(q3.answers) == 1 and not q3.edits
    assert len([c for c in r.bot.named("send_message") if c[2] == "첫 번째"]) == 1
    assert not r.llm.script, "에이전트는 한 번만"
    # 동시에 두 보기를 눌러도 한 번 (DB 차지)
    r2 = await room()
    _, card2 = await asked(r2)
    r2.llm.script = [reply("한 번"), reply("두 번")]
    qa, qb = FakeQuery(Room.CHAT, BOSS, buttons(card2)[0].callback_data), FakeQuery(Room.CHAT, BOSS, buttons(card2)[1].callback_data)
    await asyncio.gather(handlers.on_callback(SimpleNamespace(callback_query=qa), r2.ctx),
                         handlers.on_callback(SimpleNamespace(callback_query=qb), r2.ctx))
    await asyncio.gather(*list(askchoice.TASKS))
    assert len(r2.llm.script) == 1 and len(qa.answers) == len(qb.answers) == 1


@test
async def expired_question_closes_gracefully():
    r = await room()
    _, card = await asked(r)
    await r.db._write("UPDATE ask_choices SET expires=?", (time.time() - 1,))
    before = len(r.llm.of("chat"))
    q = await press(r, BOSS, buttons(card)[0].callback_data)
    assert len(q.answers) == 1 and "시간이 지났어요" in q.answers[0][0], q.answers
    assert "10분이 지나" in q.edits[-1] and q.kb is None, "버튼 정리"
    assert (await r.db._one("SELECT status FROM ask_choices"))["status"] == "expired"
    q = await press(r, BOSS, buttons(card)[1].callback_data)
    assert len(q.answers) == 1 and len(r.llm.of("chat")) == before, "다시 실행 없음"


@test
async def free_input_button_and_typed_reply_closes():
    r = await room()
    _, card = await asked(r)
    qmsg = r.bot._next_id
    q = await press(r, BOSS, buttons(card)[2].callback_data)
    assert "답장</b>으로 직접" in q.edits[-1] and [b.text for row in q.kb.inline_keyboard for b in row] == \
        ["철수 (@cs1)", "철수 (@cs2)"], "✏️ 는 안내로 바뀌고 보기 버튼은 그대로"
    # 질문에 답장으로 직접 씀 → 보통 AI 호출 + 버튼 닫힘
    r.llm.script = [reply("두 번째 철수님이군요.")]
    bot_msg = SimpleNamespace(message_id=qmsg, from_user=r.bot_user(), text="❓ 어느 철수님을", caption=None)
    await r.say(BOSS, "아이디 cs2", reply_to=bot_msg)
    assert (await r.db._one("SELECT status FROM ask_choices"))["status"] == "typed"
    assert ("edit_markup", Room.CHAT, qmsg, None) in r.bot.calls
    assert "어느 철수님을" in r.llm.of("chat")[-1]["messages"][-1]["content"], "답장한 질문이 맥락으로"
    q = await press(r, BOSS, buttons(card)[0].callback_data)
    assert q.answers == [("이미 골랐어요.", False)] and not r.llm.script


@test
async def tainted_blocked():
    r = await room()
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(Room.CHAT))
    ctx.tainted = True
    res = await execute("ask_choice", tools.json.dumps(Q), ctx)
    assert "못 씀" in res and not cards(r) and not ctx.quiet, res
    assert "기록을 읽은" in await askchoice.t_ask_choice(ctx, Q), "도구 안에서도 한 번 더"
    assert "ask_choice" not in tools.READ_ONLY


@test
async def once_per_run():
    r = await room()
    # 같은 라운드에 질문 두 개 → 하나만 올라감 (두 번째는 거절), 그 뒤 AI 를 또 부르지 않음
    two = SimpleNamespace(content="", tool_calls=[tool_call("ask_choice", Q).tool_calls[0],
                                                  tool_call("ask_choice", {**Q, "question": "또?"}, "c2").tool_calls[0]])
    r.llm.script = [two, reply("끝")]
    await r.say(BOSS, "소담아 철수 뮤트")
    assert len(cards(r)) == 1 and len(r.llm.of("chat")) == 1
    row = await r.db._one("SELECT steps FROM agent_runs ORDER BY id DESC LIMIT 1")
    assert "이미 질문을 보냈음" in row["steps"], row["steps"]


@test
async def never_in_chime_or_morning():
    assert "ask_choice" not in CHIME_TOOLS
    r = await room()
    for mode in ("chime", "morning"):
        ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(Room.CHAT))
        r.llm.script = [tool_call("ask_choice", Q), reply("")]
        await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="x", mode=mode, extras={})
        assert all(t["function"]["name"] != "ask_choice" for t in r.llm.of("chat")[-2]["tools"] or [])
        run, tok = agentlog.start(Room.CHAT, BOSS.id, mode, "x")   # 목록을 우회해도 도구가 한 번 더 거절
        try:
            assert "사용할 수 없음" in await askchoice.t_ask_choice(ctx, Q)
        finally:
            agentlog.current.reset(tok)
    assert not cards(r) and not ctx.quiet


@test
async def available_room_and_dm_for_members():
    names = lambda role, dm: {t.name for t in available(role, {}, in_dm=dm)}   # noqa: E731
    assert "ask_choice" in names(Role.MEMBER, False) and "ask_choice" in names(Role.MEMBER, True)
    assert len(askchoice.TOOL.description) < 200, "캐시되는 도구 목록이라 짧게"


@test
async def dm_flow():
    r = await room()
    r.llm.script = [tool_call("ask_choice", {"question": "어느 방 기준으로 볼까요?", "options": ["소통방", "공지방"]}), "x"]
    m = FakeMsg(A.id, A, "소담아 오늘 요약해줘", message_id=55)
    await handlers.on_private(SimpleNamespace(message=m), r.ctx)
    [card] = cards(r, A.id)
    assert card[3]["reply_parameters"].message_id == 55 and not m.replies
    r.llm.script = [reply("소통방 기준 요약이에요.")]
    q = await press(r, A, buttons(card)[1].callback_data, chat=A.id)
    assert "공지방" in q.edits[-1]
    assert "(선택: 공지방)" in r.llm.of("chat")[-1]["messages"][-1]["content"]
    assert any(c[1] == A.id and "기준 요약" in c[2] for c in r.bot.named("send_message"))


@test
async def rerun_sanction_still_needs_confirm_card_and_no_second_ask():
    r = await room()
    _, card = await asked(r)
    r.llm.script = [tool_call("ask_choice", Q), tool_call("mute_member", {"names": ["@cs1"], "minutes": 60, "reason": "도배"}, "c2"),
                    reply("확인 버튼 보냈어요.")]
    await press(r, BOSS, buttons(card)[0].callback_data)
    tool_msgs = [x["content"] for x in r.llm.of("chat")[-1]["messages"] if x["role"] == "tool"]
    assert "이미 질문을 보냈음" in tool_msgs[0], "고른 뒤 또 되묻지 않음"
    assert r.svc.pending and not r.bot.named("restrict"), "제재는 확인 카드로만 (바로 실행 없음)"
    assert len(cards(r)) == 1


@test
async def member_rerun_uses_fresh_role():
    """누를 때 권한으로: 질문 뒤 관리자에서 내려오면 다시 돌린 실행엔 관리자 도구가 없음."""
    r = await room()
    _, card = await asked(r)
    r.svc.perms.admins.discard(BOSS.id)
    r.llm.script = [reply("네")]
    await press(r, BOSS, buttons(card)[0].callback_data)
    names = {t["function"]["name"] for t in r.llm.of("chat")[-1]["tools"]}
    assert "mute_member" not in names and "ask_choice" in names


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
