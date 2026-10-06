"""🎴 바카라 회차판 (casino/bactable.py · cardart.py): python tests/run_all.py bactable

다 같이 걸고 30초 뒤 사진 한 장을 고쳐 가며 공개 — 한 판 메시지 2개(사진 + 결과).
진짜로 기다리지 않는다 (test_casino_multi 의 가짜 시계).
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402
from harness import html_errors  # noqa: E402
from telegram.error import BadRequest  # noqa: E402
from test_casino_multi import Bot, bal, board, finish, ledger_consistent, restore, say, setup  # noqa: E402

from sodam.casino import bactable, cardart, core  # noqa: E402
from sodam.casino.cards import BacRound, Card  # noqa: E402
from sodam.casino.multi import current  # noqa: E402
from fakes import runner  # noqa: E402

test, run_all = runner()
START = core.START_POINTS


class PhotoBot(Bot):
    def __init__(self, clock):
        super().__init__(clock)
        self.media: list[tuple[float, bytes, str]] = []
        self.photo_fails = False

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        if self.photo_fails:
            raise BadRequest("photo not allowed")
        return await super().send_photo(chat_id, photo, caption, **kw)

    async def edit_message_media(self, chat_id=None, message_id=None, media=None, **kw):
        self.media.append((self.clock.t, media.media.input_file_content if hasattr(media.media, "input_file_content")
                           else media.media, media.caption))


async def env_with_photos():
    env = await setup()
    env.bot = PhotoBot(env.clock)
    return env


# 플 9♥ 3♥ Q♥ = 2 · 뱅 10♣ 6♣ = 6 → 뱅커 승 (실제 그 봇 38회차와 같은 패)
BANKER_WIN = BacRound([Card(9, 1), Card(3, 1), Card(12, 1)], [Card(10, 3), Card(6, 3)])
TIE = BacRound([Card(4, 0), Card(4, 2)], [Card(8, 1), Card(10, 0)])      # 8 : 8, 플레이어 페어


def jpeg_ok(b) -> bool:
    data = bytes(b) if not isinstance(b, (bytes, bytearray)) else b
    return Image.open(io.BytesIO(data)).size[0] == cardart.W


@test
async def round_opens_with_photo_reveals_by_editing_and_pays_once():
    env = await env_with_photos()
    a, b, c = env.users[:3]
    first = await say(env, a, "!회차 1000 플")
    assert not first, "판을 연 사람에겐 따로 글 없이 사진 한 장"
    [(_, chat, photo, cap, _)] = env.bot.named("send_photo")
    assert chat == -1009001 and "바카라 1회차" in cap and "🔒 결과 봉인" in cap and jpeg_ok(photo)
    assert "→ 뱅커" in (await say(env, b, "!회차 2000 뱅"))[0]
    await say(env, c, "!바회 500 타이")
    assert "이미" in (await say(env, a, "!회차 1000 뱅"))[0]
    r = current(-1009001, "bactable")
    r.rd = BANKER_WIN
    r.seal("고정")
    st = (await say(env, a, "!라운드"))[0]
    assert "바카라 1회차" in st and "3명" in st
    await finish(env, "bactable")
    assert await bal(env, a) == START - 1000
    assert await bal(env, b) == START - 2000 + 3900                   # 뱅커 ×1.95
    assert await bal(env, c) == START - 500
    names = [cap for _, _, cap in env.bot.media]
    assert len(names) == 4, names                                     # 플레이어 → 뱅커 → 추가 카드 → 결과 (수정 4번)
    assert "플레이어 카드 공개" in names[0] and "뱅커 카드 공개" in names[1] and "추가 카드" in names[2]
    assert "뱅커 승" in names[3] and "2 : 6" in names[3]
    times = [t for t, _, _ in env.bot.media]
    assert all(t2 - t1 >= bactable.STEP for t1, t2 in zip(times, times[1:])), times   # 수정 사이 간격 (그룹 수정 제한)
    assert all(jpeg_ok(img) for _, img, _ in env.bot.media)
    res = board(env)[-1]
    assert "1회차 결과" in res and "적중 1명" in res and "미적중 2명" in res and not html_errors(res)
    assert "「" not in res, "사진으로 공개됐으면 결과 글엔 카드 글자를 다시 안 씀"
    rows = await env.db._all("SELECT value FROM casino_results WHERE chat_id=? AND game='baccarat' ORDER BY id", (-1009001,))
    assert [x["value"] for x in rows][-1] == "B"                      # 🖼 그림장에도
    await ledger_consistent(env)
    await say(env, a, "!회차 1000 플")                                  # 다음 판 = 2회차
    assert "바카라 2회차" in env.bot.named("send_photo")[-1][3]
    if (r2 := current(-1009001, "bactable")) is not None:
        r2.task.cancel()
    restore()


@test
async def tie_returns_player_and_banker_and_text_fallback_without_photos():
    env = await env_with_photos()
    env.bot.photo_fails = True
    a, b, c = env.users[:3]
    opened = await say(env, a, "!회차 1000 플")
    assert "바카라 1회차" in opened[0], "사진이 안 되면 여는 글을 글자로"
    await say(env, b, "!회차 1000 타이")
    await say(env, c, "!회차 1000 플페어")
    r = current(-1009001, "bactable")
    r.rd = TIE
    await finish(env, "bactable")
    assert await bal(env, a) == START                                  # 타이 = 플·뱅 원금
    assert await bal(env, b) == START - 1000 + 10000
    assert await bal(env, c) == START - 1000 + 13000
    assert not env.bot.media
    res = board(env)[-1]
    assert "타이" in res and "원금 반환 1명" in res and "「" in res, res   # 사진이 없었으니 카드를 글자로
    await ledger_consistent(env)
    restore()


@test
async def bad_args_show_usage_and_open_nothing():
    env = await env_with_photos()
    out = await say(env, env.users[0], "!회차 1000")
    assert "회차판" in out[0] and current(-1009001, "bactable") is None
    restore()


@test
def card_art_files_and_layout():
    assert cardart.available(), "카드 52장 PNG (sodam/data_files/cards)"
    assert (cardart.DIR / "LICENSE.md").read_text().count("CC0")
    assert cardart.card_x(2, 3) + cardart.CW < cardart.BADGE_X - 42, "세 번째 카드가 점수 동그라미를 안 가림"
    two = cardart.stages(TIE.player, TIE.banker, "tie", 7, True, False)
    assert [n for n, _ in two] == ["player", "banker", "result"], "3번째 카드가 없으면 추가 공개 단계 없음"
    three = cardart.stages(BANKER_WIN.player, BANKER_WIN.banker, "banker", 38, False, False)
    assert [n for n, _ in three] == ["player", "banker", "third", "result"]
    im = Image.open(io.BytesIO(three[-1][1]))
    # 결과 화면: 플레이어 줄에 앞면 카드 3장 (카드 가운데 흰색) · 뱅커 줄 2장
    y_p = cardart.TITLE_H + 16 + cardart.CH // 2
    y_b = y_p + cardart.ROW_H
    white = lambda x, y: min(im.getpixel((x, y))) > 200   # noqa: E731
    assert all(white(cardart.card_x(i, 3) + 5, y_p) for i in range(3))     # 카드 왼쪽 흰 테두리
    assert all(white(cardart.card_x(i, 2) + 5, y_b) for i in range(2))
    assert not white(cardart.card_x(1, 2) + cardart.CW + 20, y_b), "뱅커 3번째 자리는 비어 있음 (펠트)"
    assert len(three[-1][1]) < 200_000


if __name__ == "__main__":
    import asyncio
    sys.exit(1 if asyncio.run(run_all()) else 0)
