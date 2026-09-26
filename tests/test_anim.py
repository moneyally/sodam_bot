"""결과 애니메이션(GIF): 룰렛·사다리·경마 — 결과 캡션·정산·실패 시 글자 연출: python tests/run_all.py anim"""
import asyncio
import io
import sys

import test_casino_multi as TM
import test_game_visual2 as V
from fakes import FakeQuery, fake_user, runner
from PIL import Image, ImageSequence
from telegram.error import TelegramError

from sodam import casino
from sodam.casino import anim, basic, multi

test, run_all = runner()


def near(c, want, tol=40) -> bool:
    """GIF 는 색 수를 줄여 저장해서 조금 달라짐."""
    return all(abs(a - b) <= tol for a, b in zip(c, want))


def frames(gif: bytes) -> list:
    return [f.convert("RGB") for f in ImageSequence.Iterator(Image.open(io.BytesIO(gif)))]


class GifBot(V.Bot):
    """V.Bot 은 애니메이션을 막아 두었으니 여기선 다시 허용."""

    async def send_animation(self, chat_id, animation, caption=None, **kw):
        self.env_ref[0].ops.append(("anim", caption))
        self.calls.append(("send_animation", chat_id, animation, caption, kw))
        return self._msg(chat_id)

    async def edit_message_caption(self, chat_id=None, message_id=None, caption=None, **kw):
        self.env_ref[0].ops.append(("caption", caption))


async def gif_env():
    env = await V.setup()
    bot = GifBot(env.bot.env_ref)
    env.bot, env.bot.env_ref[0] = bot, env
    return env


@test
def gifs_end_on_the_result():
    g = anim.roulette(17)
    fr = frames(g)
    assert g[:6] == b"GIF89a" and len(fr) >= anim.ROULETTE_FRAMES - 2
    assert near(fr[-1].getpixel((138, 180)), (30, 30, 30))                # 17 = 검정 칸이 가운데에 크게
    assert near(frames(anim.roulette(1))[-1].getpixel((138, 180)), (200, 40, 40))
    assert near(frames(anim.roulette(0))[-1].getpixel((138, 180)), (22, 150, 80))
    import math
    for n in (17, 0, 26, 5):                                              # 공(흰 점)이 그 숫자 칸 자리에 멈춤
        ang = math.radians(-90 + anim.WHEEL.index(n) * 360 / len(anim.WHEEL))
        x, y = 180 + (180 - 70 + 18) * math.cos(ang), 180 + (180 - 70 + 18) * math.sin(ang)
        assert near(frames(anim.roulette(n))[-1].getpixel((round(x), round(y))), (255, 255, 255)), n
    for start, lines, end_x in (("좌", 3, 230), ("좌", 4, 70), ("우", 3, 70), ("우", 4, 230)):
        last = frames(anim.ladder(start, lines))[-1]
        assert near(last.getpixel((end_x, 300)), (250, 190, 0)), (start, lines)   # 도착 기둥 끝에 노란 공 (좌3→짝 오른쪽)


@test
async def roulette_and_ladder_send_one_gif_then_result_caption():
    env = await gif_env()
    basic.rng = lambda n: 16 if n == 37 else 0                            # 룰렛 16(빨강) · 사다리 좌3짝
    try:
        await V.say(env, "!룰렛 1000 빨강")
        await V.say(env, "!사다리 1000 좌")
    finally:
        V.restore()
    kinds = [o[0] for o in env.ops if o[0] in ("anim", "caption", "send", "edit")]
    assert kinds == ["anim", "caption", "anim", "caption"], env.ops
    caps = [o[1] for o in env.ops if o[0] == "caption"]
    assert "16" in caps[0] and "적중" in caps[0] and "+1,000P" in caps[0]
    assert "좌3짝" in caps[1] and "적중" in caps[1]
    # 캡션은 애니메이션이 다 돈 뒤에 (결과를 미리 보여주지 않음)
    i = env.ops.index(next(o for o in env.ops if o[0] == "caption"))
    assert ("sleep", anim.seconds(anim.ROULETTE_FRAMES)) in env.ops[:i]
    assert await V.bal(env) == V.START + 1000 + int(1000 * 0.95)


@test
async def horse_falls_back_to_text_when_animation_fails():
    env = await TM.setup()
    a = env.users[0]

    async def boom(*_a, **_kw):
        raise TelegramError("no animation")
    env.bot.send_animation = boom
    multi._rand = lambda n: 2 if n == multi.HORSES else 0
    await TM.say(env, a, "!경마 1000 3")
    await TM.finish(env, "horse")
    assert len(env.bot.edits) == multi.FRAMES and "3번 우승" in env.bot.edits[-1][1]
    assert await TM.bal(env, a) == TM.core.START_POINTS - 1000 + 4700
    TM.restore()


async def press_board(env, user, data):
    q = FakeQuery(V.CHAT, user, data)
    q.message = V.Msg(env, V.CHAT, env.user, "", message_id=777)          # 베팅판 메시지
    V.core._last_bet.clear()
    await casino.on_callback(env.svc, env.bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers                                 # 누를 때마다 응답 정확히 한 번
    return q


@test
async def roulette_board_buttons():
    env = await gif_env()
    msg = await V.say(env, "!룰렛")
    kb = msg.kbs[-1]
    rows = kb.inline_keyboard
    assert [len(r) for r in rows] == [4, 6, 1, 6, 6, 6, 6, 6, 6]                # 칩 · 바깥 칸 · 0 · 1~36
    assert rows[0][0].text.startswith("✅") and all(len(b.callback_data.encode()) <= 64 for r in rows for b in r)
    other = fake_user(9999, "남")
    q = await press_board(env, other, rows[1][0].callback_data)
    assert "본인" in q.answers[0][0] and await V.bal(env) == V.START       # 남의 판은 못 누름
    q = await press_board(env, env.user, rows[0][2].callback_data)        # 1만 칩
    edits = [o for o in env.ops if o[0] == "edit"]
    assert "10,000P" in edits[-1][1] and await V.bal(env) == V.START
    basic.rng = lambda n: 16
    try:
        await press_board(env, env.user, f"cs:rb:{env.user.id}:10000:빨강")
    finally:
        V.restore()
    assert await V.bal(env) == V.START + 10_000
    caps = [o[1] for o in env.ops if o[0] == "caption"]
    assert "16" in caps[-1] and "적중" in caps[-1]
    await env.svc.db.set_setting(V.CHAT, "casino_enabled", False)         # 꺼진 방: 버튼도 막힘
    q = await press_board(env, env.user, f"cs:rb:{env.user.id}:1000:검정")
    assert "꺼져" in q.answers[0][0] and await V.bal(env) == V.START + 10_000


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
