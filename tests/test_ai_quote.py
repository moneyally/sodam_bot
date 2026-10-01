"""방 설정 ai_quote (답장 인용): 끄면 그룹 AI 답을 인용 없이 보냄. 기록(누구에게 한 답)은 그대로.
python tests/run_all.py ai_quote"""
import asyncio
import sys
from pathlib import Path

import httpx
from telegram.error import NetworkError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers  # noqa: E402
from fakes import FakeMsg, fake_user, runner  # noqa: E402

from sodam import aiqueue, util  # noqa: E402
from sodam import settings as S  # noqa: E402
from sodam.panels import ai as ai_panel  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(1, "방장", "boss"), fake_user(20, "박준호", "junho")


async def _room(llm, **settings):
    r = Room()
    r.llm = llm
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, **settings})
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


async def _bot_row(r, text):
    return await r.db._one("SELECT reply_to_msg_id, reply_to_user FROM messages WHERE is_bot=1 AND text=?", (text,))


@test
async def default_on_quotes_the_request():
    assert S.DEFAULTS["ai_quote"] is True and "ai_quote" in ai_panel.SOCIAL_TOGGLES
    old = fast_timers()
    try:
        r = await _room(ScriptedLLM([reply("안녕하세요!")]))
        m = await r.say(JUNHO, "소담아 안녕")
        kw = m.reply_kws[-1]
        assert m.replies == ["안녕하세요!"] and kw["reply_parameters"].message_id == m.message_id, kw
        assert kw.get("do_quote") is None, kw
    finally:
        restore_timers(old)


@test
async def off_sends_without_quote_but_keeps_who_it_answered():
    old = fast_timers()
    try:
        r = await _room(ScriptedLLM([reply("안내")]), ai_quote=False)
        m = await r.say(JUNHO, "소담아 답글 말고 안내만 쳐")
        kw = m.reply_kws[-1]
        # reply_parameters 만 비우면 PTB 가 그룹에서 알아서 인용함 → do_quote=False 꼭 필요
        assert m.replies == ["안내"] and kw["reply_parameters"] is None and kw["do_quote"] is False, kw
        row = await _bot_row(r, "안내")
        assert row and row["reply_to_msg_id"] == m.message_id and row["reply_to_user"] == JUNHO.id, row
    finally:
        restore_timers(old)


@test
async def queued_answer_resent_without_quote_when_off():
    old, orig = fast_timers(), FakeMsg.reply_text
    util.RETRY_DELAY = 0

    async def down(self, text, **kw):
        e = NetworkError("down")
        e.__cause__ = httpx.ConnectError("no route")
        raise e
    FakeMsg.reply_text = down
    try:
        r = await _room(ScriptedLLM([reply("규칙은 광고 금지예요.")]), ai_quote=False)
        await r.say(JUNHO, "소담아 규칙 알려줘")
        rows = await r.db._all("SELECT * FROM ai_queue")
        assert len(rows) == 1, rows
        assert await aiqueue.sweep(r.ctx) == 1
        sent = [c for c in r.bot.named("send_message") if c[2] == "규칙은 광고 금지예요."]
        assert len(sent) == 1 and sent[0][3]["reply_parameters"] is None, sent
        got = await r.db._one("SELECT 1 FROM messages WHERE reply_to_msg_id=? AND is_bot=1", (rows[0]["msg_id"],))
        assert got, "인용을 꺼도 '이미 답함' 기록은 남아야 다시 안 보냄"
        assert await aiqueue.sweep(r.ctx) == 0
    finally:
        FakeMsg.reply_text = orig
        restore_timers(old)


if __name__ == "__main__":
    asyncio.run(run_all())
