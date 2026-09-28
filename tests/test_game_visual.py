"""그래프 차트·🛑 버튼·결과 봉인·100배 완주·제한(429) 재시도·하이로우 손해 버튼: python tests/run_all.py game_visual"""
import asyncio
import hashlib
import re
import sys

import test_casino_multi as TM
from fakes import FakeQuery, runner
from telegram.error import RetryAfter

from sodam import casino
from sodam.casino import cards as C
from sodam.casino import multi

test, run_all = runner()
CHAT = TM.CHAT


@test
def chart_rises_and_fits_phone():
    hist = [multi.mult_at(t * 3) for t in range(15)]
    rows = multi.chart(hist).split("\n")
    assert len(rows) == multi.CHART_H and all(len(r) <= 7 + multi.CHART_W for r in rows)
    cols = list(zip(*[r[7:] for r in rows]))
    filled = [sum(ch != " " for ch in col) for col in cols]                   # 칸 높이 (위에서부터 비어 있음)
    assert filled == sorted(filled) and filled[0] <= 1 and filled[-1] == multi.CHART_H   # 1.00x 에서 꼭대기까지
    assert len(set(filled)) >= 4                                               # 곡선 (다 꽉 찬 막대가 아님)
    for col in cols:                                                           # 막대는 아래부터 차 있음
        body = "".join(col).lstrip(" ")
        assert all(ch == "█" for ch in body[1:]), col
    assert rows[0].lstrip().startswith(multi.fx(hist[-1])) and rows[-1].lstrip().startswith("1.00x")


@test
async def stop_button_cashes_out_once_without_chat_message():
    env = await TM.setup(crash=500)
    a, b = env.users[:2]
    await TM.say(env, a, "!그래프 1000")
    await TM.say(env, b, "!그래프 1000")
    r = multi.current(CHAT, "crash")
    qs = []

    async def one(u):
        q = FakeQuery(CHAT, u, f"cs:cr:{r.rid}")
        qs.append(q)
        await casino.on_callback(env.svc, env.bot, q, ["cr", r.rid])

    async def press():
        await asyncio.gather(one(a), one(a), one(b))   # a 는 동시에 연타
    env.clock.at(15 + multi.time_to(200) + 0.1, press)
    sent_before = len(env.bot.named("send_message"))
    await TM.finish(env, "crash")
    assert [len(q.answers) for q in qs] == [1, 1, 1]
    a_answers = sorted(q.answers[0][0] for q in qs[:2])
    assert sum("내림" in x for x in a_answers) == 1 and sum("이미" in x for x in a_answers) == 1, a_answers
    assert len([x for x in await TM.ledger(env, a) if x["reason"] == "win:crash"]) == 1
    assert await TM.bal(env, a) == TM.core.START_POINTS - 1000 + 1000 * r.players[a.id].cash_at // 100
    assert r.players[a.id].cash_at >= 200 and r.players[b.id].cash_at >= 200
    # 버튼으로 내리면 방에 새 메시지가 안 생김 (차트 메시지 1개 + 결과판 1개만)
    assert len(env.bot.named("send_message")) - sent_before == 2
    q = FakeQuery(CHAT, a, f"cs:cr:{r.rid}")                     # 끝난 판 버튼
    await casino.on_callback(env.svc, env.bot, q, ["cr", r.rid])
    assert "끝난 판" in q.answers[0][0]
    await TM.ledger_consistent(env)
    TM.restore()


@test
async def live_message_has_chart_and_stop_button():
    env = await TM.setup(crash=400)
    await TM.say(env, env.users[0], "!그래프 1000")
    r = multi.current(CHAT, "crash")
    kb_seen = []
    orig = env.bot.edit_message_text

    async def edit(text, chat_id=None, message_id=None, **kw):
        kb_seen.append((text, kw.get("reply_markup")))
        return await orig(text, chat_id=chat_id, message_id=message_id, **kw)
    env.bot.edit_message_text = edit
    await TM.finish(env, "crash")
    live = [(t, kb) for t, kb in kb_seen if "상승 중" in t]
    assert live and all("<pre>" in t and "█" in t for t, _ in live[1:])
    assert all(kb and kb.inline_keyboard[0][0].callback_data == f"cs:cr:{r.rid}" for _, kb in live)
    assert "터졌어요" in kb_seen[-1][0] and kb_seen[-1][1] is None          # 끝나면 버튼 사라짐
    TM.restore()


@test
async def sealed_result_is_revealed_and_verifiable():
    for game, cmd in (("crash", "!그래프 1000"), ("horse", "!경마 1000 3")):
        env = await TM.setup(crash=250)
        opened = (await TM.say(env, env.users[0], cmd))[0]
        sealed = re.search(r"결과 봉인: <code>([0-9a-f]{16})</code>", opened).group(1)
        await TM.finish(env, game)
        reveal = next(t for t in TM.board(env) if "봉인 공개" in t)
        secret = re.search(r"봉인 공개: <code>([^<]+)</code>", reveal).group(1)
        assert hashlib.sha256(secret.encode()).hexdigest()[:16] == sealed, (game, secret)
        assert secret.startswith("2.50x|" if game == "crash" else "")
        TM.restore()


@test
async def riders_at_max_multiplier_get_paid():
    env = await TM.setup(crash=multi.CRASH_CAP)
    a = env.users[0]
    await TM.say(env, a, "!그래프 1000")
    r = await TM.finish(env, "crash")
    assert r.players[a.id].cash_at == multi.CRASH_CAP
    assert await TM.bal(env, a) == TM.core.START_POINTS - 1000 + 1000 * multi.CRASH_CAP // 100
    assert "완주" in TM.board(env)[-1]
    TM.restore()


@test
async def final_screen_and_board_survive_rate_limit():
    env = await TM.setup(crash=150)
    await TM.say(env, env.users[0], "!그래프 1000")
    orig_edit = env.bot.edit_message_text

    async def edit(text, **kw):                       # 마지막 '터졌어요' 화면이 처음엔 429
        if "터졌어요" in text and not env.bot.named("hit429"):
            env.bot.calls.append(("hit429",))
            raise RetryAfter(5)
        return await orig_edit(text, **kw)
    env.bot.edit_message_text = edit
    sends = {"n": 0}
    orig_send = env.bot.send_message

    async def send_message(chat_id, text, **kw):
        sends["n"] += 1
        if "펑" in text and sends.setdefault("hit", 0) == 0:
            sends["hit"] = 1
            raise RetryAfter(3)
        return await orig_send(chat_id, text, **kw)
    env.bot.send_message = send_message
    await TM.finish(env, "crash")
    assert any("터졌어요" in t for _, t in env.bot.edits), env.bot.edits[-3:]
    assert any("펑" in t for t in TM.board(env))
    TM.restore()


@test
def hilo_hides_losing_buttons():
    assert C.hl_mult(1.0) == 0 and C.hl_mult(0.97) == 0 and C.hl_mult(0.9) == 1.07 and C.hl_mult(0) == 0


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
