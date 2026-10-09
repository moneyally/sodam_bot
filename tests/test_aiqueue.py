"""AI 요청 영구 대기열 (aiqueue): 재시작·연결 끊김에도 답이 사라지지 않게.
python tests/run_all.py aiqueue"""
import asyncio
import sys
import time
from pathlib import Path

import httpx
from telegram.error import NetworkError, TimedOut

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers  # noqa: E402
from fakes import FakeMsg, fake_user, runner  # noqa: E402

from sodam import aiqueue, util  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(1, "방장", "boss"), fake_user(20, "박준호", "junho")


class HangLLM(ScriptedLLM):
    def __init__(self):
        super().__init__([])
        self.entered = asyncio.Event()

    async def chat(self, messages, **kw):
        self.entered.set()
        await asyncio.Event().wait()


async def _room(llm):
    r = Room()
    r.llm = llm
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


def _rows(r):
    return r.db._all("SELECT * FROM ai_queue")


def _answers(r, text):
    return [c for c in r.bot.named("send_message") if c[2] == text]


@test
async def request_cut_by_restart_is_answered_after_restart_once():
    old = fast_timers()
    try:
        r = await _room(HangLLM())
        t = asyncio.create_task(r.say(JUNHO, "소담아 오늘 공지 뭐였지", settle=False))
        await asyncio.wait_for(r.llm.entered.wait(), 60)
        t.cancel()                                                         # 배포·종료로 실행이 끊김
        await asyncio.gather(t, return_exceptions=True)
        rows = await _rows(r)
        assert len(rows) == 1 and "오늘 공지 뭐였지" in rows[0]["request"] and rows[0]["answer"] is None, rows
        aiqueue.RUNNING.clear()                                            # 새 프로세스
        r.llm = r.svc.llm = ScriptedLLM([reply("오늘 공지는 저녁 8시 이벤트예요.")])
        assert await aiqueue.sweep(r.ctx) == 1
        await r.settle()
        sent = _answers(r, "오늘 공지는 저녁 8시 이벤트예요.")
        assert len(sent) == 1 and sent[0][3]["reply_parameters"].message_id == rows[0]["msg_id"], sent
        assert not await _rows(r)
        assert await aiqueue.sweep(r.ctx) == 0 and len(_answers(r, "오늘 공지는 저녁 8시 이벤트예요.")) == 1
    finally:
        restore_timers(old)


@test
async def running_request_is_not_replayed_and_normal_answer_leaves_no_row():
    old = fast_timers()
    try:
        r = await _room(HangLLM())
        t = asyncio.create_task(r.say(JUNHO, "소담아 안녕", settle=False))
        await asyncio.wait_for(r.llm.entered.wait(), 60)
        assert await aiqueue.sweep(r.ctx) == 0                              # 이 프로세스가 처리 중 → 건드리지 않음
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        aiqueue.RUNNING.clear()
        await r.db._write("DELETE FROM ai_queue")
        r.llm = r.svc.llm = ScriptedLLM([reply("안녕하세요!")])
        m = await r.say(JUNHO, "소담아 안녕")
        assert m.replies == ["안녕하세요!"] and not await _rows(r) and not aiqueue.RUNNING
    finally:
        restore_timers(old)


def _net_error(cause):
    e = NetworkError("down")
    e.__cause__ = cause
    return e


@test
async def answer_made_but_connection_down_is_kept_and_resent_without_new_ai_call():
    old, orig = fast_timers(), FakeMsg.reply_text
    util.RETRY_DELAY = 0

    async def down(self, text, **kw):
        raise _net_error(httpx.ConnectError("no route"))
    FakeMsg.reply_text = down
    try:
        r = await _room(ScriptedLLM([reply("규칙은 광고 금지예요.")]))
        await r.say(JUNHO, "소담아 규칙 알려줘")
        rows = await _rows(r)
        assert len(rows) == 1 and rows[0]["answer"] == "규칙은 광고 금지예요." and not aiqueue.RUNNING, rows
        assert await aiqueue.sweep(r.ctx) == 1                             # 대본이 비어 있음 = AI 다시 안 부름
        assert len(_answers(r, "규칙은 광고 금지예요.")) == 1 and not await _rows(r)
        got = await r.db._one("SELECT text FROM messages WHERE reply_to_msg_id=? AND is_bot=1", (rows[0]["msg_id"],))
        assert got and got["text"] == "규칙은 광고 금지예요.", ("다시 보낸 답은 실제 글로 기록 (예전 '(다시 보낸 답)')", got)
        turn = await r.db._one("SELECT answer FROM ai_turns WHERE user_id=?", (JUNHO.id,))
        assert turn and turn["answer"] == "규칙은 광고 금지예요.", "이어 말하기·답장 판단에 쓰이게 대화 기록에도"
    finally:
        FakeMsg.reply_text = orig
        restore_timers(old)


@test
async def follow_up_said_while_running_survives_a_restart():
    """답을 만드는 동안 이어 보낸 말은 그 실행에 들어감(메모리) → 그 실행이 재시작으로 끊기면 다시 돌 때 처음 말만 처리하던 것."""
    old = fast_timers()
    try:
        r = await _room(HangLLM())
        t = asyncio.create_task(r.say(JUNHO, "소담아 오늘 공지 뭐였지", settle=False))
        await asyncio.wait_for(r.llm.entered.wait(), 60)
        await r.say(JUNHO, "소담아 그리고 이벤트 시간도", settle=False)        # 실행 중에 이어 보냄
        rows = await _rows(r)
        assert len(rows) == 1 and "그리고 이벤트 시간도" in rows[0]["request"] and "오늘 공지" in rows[0]["request"], rows
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        aiqueue.RUNNING.clear()
        seen = []
        r.llm = r.svc.llm = ScriptedLLM([lambda m: seen.append(str(m[-1]["content"])) or "공지는 8시, 이벤트도 8시예요."])
        assert await aiqueue.sweep(r.ctx) == 1
        await r.settle()
        assert seen and "오늘 공지" in seen[0] and "이벤트 시간도" in seen[0], "다시 돌 때 이어 보낸 말까지"
        await aiqueue.append(r.db, r.bot, r.CHAT, 999999, "없는 줄")       # 없는 줄은 조용히 넘어감
    finally:
        restore_timers(old)


@test
async def maybe_delivered_timeout_is_not_resent():
    old, orig = fast_timers(), FakeMsg.reply_text
    util.RETRY_DELAY = 0

    async def timeout(self, text, **kw):
        raise TimedOut()                                                   # 응답만 끊김 = 이미 올라갔을 수 있음
    FakeMsg.reply_text = timeout
    try:
        r = await _room(ScriptedLLM([reply("네 알겠어요.")]))
        try:
            await r.say(JUNHO, "소담아 고마워")
        except TimedOut:
            pass                                                           # 예전처럼 오류 처리기로
        assert not await _rows(r) and not aiqueue.RUNNING
    finally:
        FakeMsg.reply_text = orig
        restore_timers(old)


@test
async def already_answered_stale_and_other_bot_rows():
    r = await _room(ScriptedLLM([]))
    now = time.time()
    ins = ("INSERT INTO ai_queue(bot_id, chat_id, msg_id, user_id, role, via, request, msg, ts) "
           "VALUES(?,?,?,?,?,?,?,?,?)")
    await r.db._write(ins, (r.bot.id, r.CHAT, 7, JUNHO.id, 0, "call", "a", "{}", now))
    await r.db.log_message(r.CHAT, r.bot.id, 9007, "답", is_bot=True, reply_to_msg_id=7, reply_to_user=JUNHO.id)
    await r.db._write(ins, (r.bot.id, r.CHAT, 8, JUNHO.id, 0, "call", "b", "{}", now - util.STALE_SEC - 5))
    await r.db._write(ins, (r.bot.id + 1, r.CHAT, 9, JUNHO.id, 0, "call", "c", "{}", now))
    assert await aiqueue.sweep(r.ctx) == 0                                  # 대본이 비어 있음 = 다시 실행 안 함
    assert [x["msg_id"] for x in await _rows(r)] == [9]                    # 딜러 봇 줄은 그대로


@test
async def failing_replay_gives_up_after_max_tries():
    r = await _room(ScriptedLLM([]))
    await r.db._write("INSERT INTO ai_queue(bot_id, chat_id, msg_id, user_id, role, via, request, msg, ts, answer) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)", (r.bot.id, r.CHAT, 7, JUNHO.id, 0, "call", "a", "{}", time.time(), "답"))
    orig = r.bot.send_message

    async def down(*a, **kw):
        raise _net_error(httpx.ConnectError("no route"))
    r.bot.send_message = down
    for _ in range(aiqueue.MAX_TRIES):
        await aiqueue.sweep(r.ctx)
    assert len(await _rows(r)) == 1
    await aiqueue.sweep(r.ctx)
    assert not await _rows(r)
    r.bot.send_message = orig


if __name__ == "__main__":
    run_all()
