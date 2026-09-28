"""🖼 그림장: 결과 기록·출목표 규칙·이미지·명령: python tests/run_all.py board"""
import asyncio
import io
import sys

import test_casino_cards as TC
from fakes import runner
from PIL import Image

from sodam.casino import board

test, run_all = runner()
CHAT = TC.CHAT


@test
def big_road_rules():
    road = board.big_road(["T", "B", "B", "P", "T", "P", *["B"] * 8, "P"])
    assert road[0] == (0, 0, "B", 1)                      # 맨 앞 타이는 첫 칸에
    assert road[1] == (0, 1, "B", 0)                      # 같은 쪽 → 아래로
    assert road[2] == (1, 0, "P", 1)                      # 바뀌면 옆 열 맨 위, 타이는 그 칸에 겹침
    dragon = [c for c in road if c[2] == "B"][2:]
    assert [(c, r) for c, r, *_ in dragon] == [(2, 0), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (3, 5), (4, 5)]  # 6칸 넘으면 꼬리
    assert road[-1][:3] == (3, 0, "P")                    # 다음 줄은 그 줄이 시작한 열(2)의 다음 열


@test
async def record_keeps_only_recent():
    svc, bot, _ = await TC.setup()
    for i in range(board.KEEP + 15):
        await board.record(svc.db, CHAT, "roulette", str(i % 37))
    vals = await board.recent(svc.db, CHAT, "roulette", 1000)
    assert len(vals) == board.KEEP and vals[-1] == str((board.KEEP + 14) % 37)      # 오래된 것부터 지움
    assert await board.recent(svc.db, CHAT, "baccarat") == []


@test
def every_game_renders_a_png():
    samples = {"baccarat": ["P", "Bp", "T", "Bb", "B"] * 12, "oddeven": [str(i % 6 + 1) for i in range(60)],
               "roulette": [str(i % 37) for i in range(60)], "ladder": ["좌3짝", "우4짝", "좌4홀", "우3홀"] * 15,
               "crash": [str(v) for v in (100, 150, 230, 1000, 10000) * 8], "horse": [str(i % 5 + 1) for i in range(60)]}
    for game, vals in samples.items():
        img = Image.open(io.BytesIO(board.render(game, vals)))
        assert img.format == "PNG" and img.width >= 360 and img.height >= 200, game
        assert len(img.getcolors(1 << 20)) > 5, game               # 빈 그림이 아님


@test
async def games_record_results_and_command_sends_image():
    svc, bot, (u,) = await TC.setup()
    TC.fix("♠9", "♥Q", "♦9", "♣Q")                            # 플 ♠9♦9 : 뱅 ♥Q♣Q → 플 승, 양쪽 페어
    await TC.cmd(svc, bot, u, "!바카라 1000 플")
    TC.unfix()
    await TC.cmd(svc, bot, u, "!룰렛 1000 빨강")
    assert await board.recent(svc.db, CHAT, "baccarat") == ["Ppb"]
    assert len(await board.recent(svc.db, CHAT, "roulette")) == 1
    board._last.clear()
    msg = await TC.cmd(svc, bot, u, "!그림장")
    photo = bot.named("send_photo")[-1]
    assert Image.open(io.BytesIO(photo[2])).format == "PNG" and "바카라 그림장" in photo[3] and "🔵플 1" in photo[3]
    msg = await TC.cmd(svc, bot, u, "!그림장 룰렛")                   # 10초 안에 또 → 도배 방지
    assert "10초" in msg.replies[-1] and len(bot.named("send_photo")) == 1
    board._last.clear()
    msg = await TC.cmd(svc, bot, u, "!그림장 경마")
    assert "기록이 없어요" in msg.replies[-1]
    msg = await TC.cmd(svc, bot, u, "!그림장 포커")
    assert "<code>!그림장 바카라</code>" in msg.replies[-1]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
