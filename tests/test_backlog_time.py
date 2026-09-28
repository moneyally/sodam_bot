"""재시작 뒤 밀린 메시지의 기록 시각: python tests/run_all.py backlog_time

봇이 꺼져 있던 동안 온 메시지는 켜질 때 한꺼번에 받는다. 대화 기록(AI 맥락·하루 요약·리포트)엔
받은 시각이 아니라 보낸 시각(msg.date)으로 남아야 한다.
"""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fake_llm import Room
from fakes import fake_user, runner

from sodam import handlers

test, run_all = runner()


@test
async def backlog_message_logged_at_sent_time():
    r = await Room().open(admins={1}, settings={"injection_guard": False})
    sent = datetime.now(timezone.utc) - timedelta(hours=3)
    m = r.msg(fake_user(20, "멤버"), "세 시간 전에 보낸 말")
    m.date = sent
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)
    await r.say(fake_user(21, "지금"), "방금 보낸 말")
    rows = {x["text"]: x["ts"] for x in await r.db._all("SELECT text, ts FROM messages WHERE is_bot=0")}
    assert rows["세 시간 전에 보낸 말"] == int(sent.timestamp()), rows
    assert abs(rows["방금 보낸 말"] - time.time()) < 5, "date 없으면 지금 시각"


if __name__ == "__main__":
    run_all()
