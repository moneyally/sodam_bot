"""불안정한 네트워크 대비: python tests/run_all.py network_retry

① 확실히 안 보내진 오류(연결 실패)만 방 글 재전송 — 응답만 끊긴 경우는 이미 보내졌을 수 있어 중복 방지
② 오너 보고는 둘 다 재전송(1:1 이라 중복 무해), 실패 원인을 제대로 기록  ③ 이름 순찰은 한 차례 시간 제한
"""
import asyncio
import logging
from types import SimpleNamespace

import httpx
from telegram.error import Forbidden, NetworkError

from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam import namehist, util
from sodam.greet import Greeter

test, run_all = runner()
CHAT = -100123


def neterr(cls):
    e = NetworkError(f"httpx.{cls.__name__}")
    e.__cause__ = cls("boom")
    return e


def flaky(bot, name, errors):
    """bot.<name> 이 errors 를 차례로 던진 뒤 원래대로 동작."""
    real, left = getattr(bot, name), list(errors)

    async def f(*a, **kw):
        bot.calls.append(("try_" + name, a[0]))
        if left:
            raise left.pop(0)
        return await real(*a, **kw)
    setattr(bot, name, f)


@test
async def classify_and_retry_rules():
    util.RETRY_DELAY = 0
    assert util.surely_unsent(neterr(httpx.ConnectError)) and util.surely_unsent(neterr(httpx.ConnectTimeout))
    assert not util.surely_unsent(neterr(httpx.RemoteProtocolError)) and not util.surely_unsent(neterr(httpx.ReadTimeout))
    n = []

    async def once(err):
        n.append(1)
        if len(n) == 1:
            raise err
        return "ok"
    assert await util.send_retry(lambda: once(neterr(httpx.ConnectError))) == "ok" and len(n) == 2
    n.clear()
    try:
        await util.send_retry(lambda: once(neterr(httpx.RemoteProtocolError)))
        raise AssertionError("중복 위험이면 다시 보내면 안 됨")
    except NetworkError:
        assert len(n) == 1
    n.clear()
    assert await util.send_retry(lambda: once(neterr(httpx.RemoteProtocolError)), dup_ok=True) == "ok" and len(n) == 2


async def greet_with(errors):
    util.RETRY_DELAY = 0
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot()
    flaky(bot, "send_message", errors)
    g = Greeter(svc)
    g._pending[CHAT] = [(20, "신입")]
    await g.flush(bot, CHAT)
    return bot


@test
async def greeting_retried_only_when_surely_unsent():
    bot = await greet_with([neterr(httpx.ConnectError)])
    assert len(bot.named("try_send_message")) == 2 and len(bot.named("send_message")) == 1, "연결 실패 → 한 번 더"
    bot = await greet_with([neterr(httpx.RemoteProtocolError)])
    assert len(bot.named("try_send_message")) == 1 and not bot.named("send_message"), "응답만 끊김 → 중복 방지로 안 보냄"


@test
async def owner_report_retried_and_cause_logged():
    util.RETRY_DELAY = 0
    db = await make_db()
    svc = await make_svc(db)
    svc.perms.owner_ids = {7}
    bot = FakeBot()
    flaky(bot, "send_message", [neterr(httpx.RemoteProtocolError)])
    await svc.mod.report(bot, "보고")
    assert [c[1] for c in bot.named("send_message")] == [7], "1:1 보고는 응답만 끊겨도 다시 보냄"
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger("sodam.moderation").addHandler(handler)
    try:
        flaky(bot, "send_message", [Forbidden("blocked")])
        await svc.mod.report(bot, "보고2")
        flaky(bot, "send_message", [neterr(httpx.ConnectError), neterr(httpx.ConnectError)])
        await svc.mod.report(bot, "보고3")
    finally:
        logging.getLogger("sodam.moderation").removeHandler(handler)
    msgs = [r.getMessage() for r in records]
    assert any("1:1 채팅을 먼저" in m for m in msgs) and any("네트워크" in m and "1:1" not in m for m in msgs), msgs


@test
async def name_sweep_stops_at_time_budget():
    db = await make_db()
    svc = await make_svc(db)
    for uid in range(100, 106):
        await db.upsert_user(fake_user(uid, f"u{uid}"))
        await db.touch_member(CHAT, uid)
    bot = FakeBot()

    async def slow(chat_id, user_id):
        await asyncio.sleep(0.06)
        return SimpleNamespace(status="member", user=fake_user(user_id, f"u{user_id}"))
    bot.get_chat_member = slow
    old = namehist.SCAN_BUDGET
    namehist.SCAN_BUDGET = 0.1
    try:
        done = await namehist.sweep(svc, bot)
    finally:
        namehist.SCAN_BUDGET = old
    assert 1 <= done < 6, done


if __name__ == "__main__":
    run_all()
