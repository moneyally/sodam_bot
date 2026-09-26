"""게임 전체 점검(카드 그림·메시지 정리): python tests/run_all.py games_all

본코드 경로 그대로 (casino.dispatch → 게임 → 가짜 봇):
- 바카라 GIF 마지막 화면의 카드 수·무늬 색·승자 강조 = 실제 결과 카드
- 블랙잭·하이로우: 시작 = 사진+버튼, 버튼마다 그림 교체(edit_message_media), 정산 금액은 글자 판과 같음
- 그림 실패(그리기·보내기·교체) → 글자 화면으로 자동 대체
- 그래프·경마 🎫 참가 답장은 마감 무렵 삭제 예약 · 꺼진 방 안내는 방당 10분에 1번
"""
import asyncio
import io
import sys

import test_casino_cards as K
import test_casino_multi as TM
from fakes import FakeMsg, FakeQuery, runner
from PIL import Image, ImageSequence
from telegram.error import BadRequest, TelegramError

from sodam import casino
from sodam.casino import cardimg, core
from sodam.casino import cards as C

test, run_all = runner()
CHAT = K.CHAT


# ── 픽셀 도우미 ───────────────────────────────────────────
def is_red(p):
    return p[0] > 150 and p[1] < 100 and p[2] < 110


def is_black(p):
    return max(p[:3]) < 80


def is_back(p):                       # 뒷면 남색·격자
    return p[2] > 120 and p[0] < 110


def is_felt(p):
    return p[1] > 70 and p[0] < 60 and p[2] < 90


def center(row, i, n, c=None):
    """카드 무늬 자리: 숫자 카드는 가운데, J·Q·K 는 큰 글자 아래 무늬."""
    x, y = cardimg.slot(row, i, n)
    return x + cardimg.CW // 2, y + cardimg.CH // 2 + (21 if c is not None and c.rank >= 11 else 0)


def big_suit(c):
    """하이로우 큰 카드 무늬 자리 (84×118 → 118×165 확대, 왼쪽 위 60,92)."""
    k = 118 / cardimg.CW
    return 60 + 59, 92 + round((cardimg.CH // 2 + (21 if c.rank >= 11 else 0)) * k)


def check_row(im, row, cards, hide=()):
    """row 줄에 cards 가 그 순서·색으로 놓였고, 그 오른쪽은 비었는지."""
    n = max(len(cards), 2)
    for i, c in enumerate(cards):
        p = im.getpixel(center(row, i, n, c))
        if i in hide:
            x, y = cardimg.slot(row, i, n)
            assert all(is_back(im.getpixel((x + dx, y + 30))) for dx in (14, 16)), (row, i)   # 가운데는 금색 로고
        else:
            assert (is_red if c.suit in (1, 2) else is_black)(p), (row, i, c, p)
    x, y = cardimg.slot(row, len(cards) - 1, n)
    assert is_felt(im.getpixel((x + cardimg.CW + 5, y + cardimg.CH // 2))), "카드가 더 있음"


def gold_ring(im, row):
    p = im.getpixel((16, cardimg.TOP + row * cardimg.ROW_H + cardimg.ROW_H // 2))
    return p[0] > 200 and p[1] > 160 and p[2] < 120


def rgb(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def last_frame(gif: bytes) -> Image.Image:
    return [f.convert("RGB") for f in ImageSequence.Iterator(Image.open(io.BytesIO(gif)))][-1]


# ── 가짜 봇: 사진 교체까지 기록 ──────────────────────────────
class Bot(K.Bot):
    def __init__(self):
        super().__init__()
        self.fail_media = None

    async def edit_message_media(self, chat_id=None, message_id=None, media=None, reply_markup=None, **kw):
        if self.fail_media:
            raise self.fail_media
        self.calls.append(("edit_media", chat_id, message_id, media, reply_markup))

    async def edit_message_caption(self, chat_id=None, message_id=None, caption=None, **kw):
        self.calls.append(("edit_caption", chat_id, message_id, caption, kw.get("reply_markup")))


async def setup(n_users=1):
    svc, _, users = await K.setup(n_users)
    C.IMAGES = True                         # K.setup 은 글자 화면으로 돌려 둠 → 여기선 그림 경로
    return svc, Bot(), users


def media_png(call) -> Image.Image:
    return rgb(call[3].media.input_file_content)


# ── 🃏 바카라 GIF ─────────────────────────────────────────
@test
async def baccarat_gif_shows_the_real_cards_one_by_one():
    svc, bot, (u,) = await setup()
    K.fix("♠2", "♥3", "♦3", "♣9", "♠8", "♥4")               # 플 2,3 → 5 → 3번째 8 = 3 · 뱅 3,9 = 2 → 4 받음 = 6 → 뱅 승
    msg = await K.cmd(svc, bot, u, "!바카라 1000 뱅")
    (_, _, gif, spin, _kw), = bot.named("send_animation")
    assert "카드를 나눠요" in spin and not msg.replies                # 글자 연출 없음 (GIF 1 + 캡션 1)
    fr = [f.convert("RGB") for f in ImageSequence.Iterator(Image.open(io.BytesIO(gif)))]
    assert len(fr) == 1 + 2 * 6 + 1, len(fr)                          # 빈 판 → 6장 (뒷면→앞면) → 결과
    assert is_felt(fr[0].getpixel(center(0, 0, 2)))                    # 처음엔 빈 자리
    assert is_back(fr[1].getpixel((cardimg.slot(0, 0, 2)[0] + 15, cardimg.slot(0, 0, 2)[1] + 30))) and is_black(fr[2].getpixel(center(0, 0, 2)))  # 뒤집힘
    last = fr[-1]
    check_row(last, 0, K.cs("♠2", "♦3", "♠8"))
    check_row(last, 1, K.cs("♥3", "♣9", "♥4"))
    assert gold_ring(last, 1) and not gold_ring(last, 0)               # 승자(뱅커) 줄 강조
    cap = bot.named("edit_caption")[-1][3]
    assert "뱅커 승" in cap and "(플 3 : 6 뱅)" in cap and "+950P" in cap and "「" not in cap
    assert await core.balance(svc.db, CHAT, u.id) == 10_950
    await K.ledger_ok(svc, [u])
    K.unfix()


@test
async def baccarat_gif_failure_falls_back_to_text_frames():
    svc, bot, (u,) = await setup()
    orig = cardimg.baccarat_gif
    cardimg.baccarat_gif = lambda *a, **k: 1 / 0
    try:
        K.fix("♠4", "♥K", "♦4", "♣5")
        msg = await K.cmd(svc, bot, u, "!바카라 1000 플")
    finally:
        cardimg.baccarat_gif = orig
        K.unfix()
    assert not bot.named("send_animation")
    final = bot.named("edit_text")[-1][3]
    assert "「4♠️」「4♦️」" in final and "플레이어 승" in final and msg.replies[0].count(C.HIDDEN) == 4
    assert await core.balance(svc.db, CHAT, u.id) == 11_000


# ── 🃏 블랙잭 사진 ────────────────────────────────────────
BJ = ("♠10", "♥2", "♦5", "♣Q", "♠3", "♥4", "♦3")            # 나 10,5 · 딜러 2,Q → 히트 3 = 18 → 스탠드: 딜러 12+4+3 = 19


async def bj_round(images: bool):
    svc, bot, (u,) = await setup()
    C.IMAGES = images
    K.fix(*BJ)
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    kb = bot.named("send_photo")[-1][4]["reply_markup"] if images else msg.kbs[-1]
    q1 = await K.press(svc, bot, u, K.btn(kb, "히트"))
    kb = bot.named("edit_media")[-1][4] if images else q1.kb
    q2 = await K.press(svc, bot, u, K.btn(kb, "스탠드"))
    K.unfix()
    bal = await core.balance(svc.db, CHAT, u.id)
    ledger = [(r["delta"], r["reason"]) for r in await svc.db._all(
        "SELECT delta, reason FROM casino_ledger WHERE chat_id=? AND user_id=? ORDER BY id", (CHAT, u.id))]
    return svc, bot, u, msg, (q1, q2), bal, ledger


@test
async def blackjack_photo_buttons_swap_the_picture_and_money_is_unchanged():
    svc, bot, u, msg, (q1, q2), bal, ledger = await bj_round(True)
    assert not msg.replies and not q1.edits and not q2.edits          # 글자 메시지 없음
    (_, _, photo, cap, kw), = bot.named("send_photo")
    start = rgb(photo)
    check_row(start, 0, K.cs("♥2", "♣Q"), hide={1})                   # 딜러 숨긴 카드 = 뒷면 그림
    check_row(start, 1, K.cs("♠10", "♦5"))
    assert "딜러 <b>2</b> + 🎴" in cap and "나 <b>15</b>" in cap and "「" not in cap
    medias = bot.named("edit_media")
    assert len(medias) == 3, [m[0] for m in bot.calls]                # 히트 1 + 딜러 공개 1 + 결과 1
    check_row(media_png(medias[0]), 1, K.cs("♠10", "♦5", "♠3"))       # 히트: 내 카드 3장
    assert medias[0][4] is not None and "더블" not in str(medias[0][4])  # 버튼 유지 (더블은 처음 두 장만)
    check_row(media_png(medias[1]), 0, K.cs("♥2", "♣Q"))              # 공개: 숨긴 카드 뒤집힘
    assert "공개 중" in medias[1][3].caption and medias[1][4] is None
    end = media_png(medias[2])
    check_row(end, 0, K.cs("♥2", "♣Q", "♥4", "♦3"))
    assert "딜러 승" in medias[2][3].caption and "-1,000P" in medias[2][3].caption and not gold_ring(end, 1)
    assert not bot.named("edit_caption") and not bot.named("edit_text")
    _, _, _, _, _, bal_txt, ledger_txt = await bj_round(False)        # 같은 카드, 글자 판
    assert bal == bal_txt == 9_000 and ledger == ledger_txt, (ledger, ledger_txt)


@test
async def blackjack_win_photo_is_highlighted_and_paid_same():
    svc, bot, (u,) = await setup()
    K.fix("♠10", "♥10", "♦9", "♣7")                           # 나 19 · 딜러 17 → 스탠드 승
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    await K.press(svc, bot, u, K.btn(bot.named("send_photo")[-1][4]["reply_markup"], "스탠드"))
    end = media_png(bot.named("edit_media")[-1])
    assert gold_ring(end, 1) and "승리" in bot.named("edit_media")[-1][3].caption
    assert await core.balance(svc.db, CHAT, u.id) == 11_000
    K.unfix()


@test
async def blackjack_image_failures_fall_back_to_text():
    # 1) 그리기 실패 → 시작부터 글자 답장 + 글자 수정 (옛 경로 그대로)
    svc, bot, (u,) = await setup()
    orig = cardimg.blackjack_png
    cardimg.blackjack_png = lambda *a, **k: 1 / 0
    try:
        K.fix(*BJ)
        msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
        q = await K.press(svc, bot, u, K.btn(msg.kbs[-1], "히트"))
    finally:
        cardimg.blackjack_png = orig
        K.unfix()
    assert not bot.named("send_photo") and "「2♥️」" in msg.replies[-1] and "「3♠️」" in q.edits[-1]
    # 2) 사진 보내기 실패 → 글자 답장
    svc, bot, (u,) = await setup()

    async def no_photo(*a, **k):
        raise TelegramError("photo off")
    bot.send_photo = no_photo
    K.fix(*BJ)
    msg = await K.cmd(svc, bot, u, "!블랙잭 1000")
    K.unfix()
    assert "「2♥️」" in msg.replies[-1] and msg.kbs[-1] is not None
    # 3) 판 도중 그림 교체 실패 → 캡션을 카드 글자 화면으로 (버튼 유지), 정산 그대로
    svc, bot, (u,) = await setup()
    K.fix(*BJ)
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    kb = bot.named("send_photo")[-1][4]["reply_markup"]
    bot.fail_media = BadRequest("wrong file")
    q = await K.press(svc, bot, u, K.btn(kb, "히트"))
    cap = bot.named("edit_caption")[-1]
    assert "「10♠️」「5♦️」「3♠️」" in cap[3] and cap[4] is not None and not q.edits
    await K.press(svc, bot, u, K.btn(cap[4], "스탠드"))
    assert "딜러 승" in bot.named("edit_caption")[-1][3] and await core.balance(svc.db, CHAT, u.id) == 9_000
    K.unfix()
    await K.ledger_ok(svc, [u])


# ── 🔼🔽 하이로우 사진 ─────────────────────────────────────
@test
async def hilo_photo_flow():
    svc, bot, (u,) = await setup()
    K.fix("♠7", "♥K", "♦2")
    await K.cmd(svc, bot, u, "!하이로우 1000")
    (_, _, photo, cap, kw), = bot.named("send_photo")
    im = rgb(photo)
    assert is_black(im.getpixel(big_suit(K.card("♠7"))))                # 지금 카드 ♠7 (검정)
    assert is_felt(im.getpixel((96 + 21, 12 + 30)))                    # 지난 카드 없음
    await K.press(svc, bot, u, K.btn(kw["reply_markup"], "하이"))
    m = bot.named("edit_media")[-1]
    im = media_png(m)
    assert is_red(im.getpixel(big_suit(K.card("♥K")))) and not is_felt(im.getpixel((96 + 21, 12 + 30)))  # ♥K · 지난 카드 1장
    assert is_felt(im.getpixel((96 + 48 + 21, 12 + 30)))
    assert "적중" in m[3].caption and m[4] is not None
    await K.press(svc, bot, u, K.btn(m[4], "그만"))
    m = bot.named("edit_media")[-1]
    assert "받고 그만" in m[3].caption and m[4] is None
    assert await core.balance(svc.db, CHAT, u.id) == 11_060            # 남은 51장 중 높은 24장 → ×2.06 (글자 판과 같음)
    K.unfix()
    await K.ledger_ok(svc, [u])


# ── 🎫 참가 답장 삭제 예약 ─────────────────────────────────
@test
async def join_replies_are_deleted_after_betting_closes():
    env = await TM.setup()
    slept = []

    async def rec(d):
        slept.append(d)
    core.temp_sleep = rec
    try:
        a, b = env.users[:2]
        core._last_bet.clear()                                         # 첫 베팅 = 판 여는 메시지 (남김)
        await casino.dispatch(env.svc, env.bot, FakeMsg(TM.CHAT, a, "!그래프 1000"), TM.CHAT, a, None, "!그래프 1000")
        core._last_bet.clear()
        m2 = FakeMsg(TM.CHAT, b, "!그래프 1000", message_id=7)
        await casino.dispatch(env.svc, env.bot, m2, TM.CHAT, b, None, "!그래프 1000")
        core._last_bet.clear()
        m3 = FakeMsg(TM.CHAT, b, "!그래프 1000", message_id=8)
        await casino.dispatch(env.svc, env.bot, m3, TM.CHAT, b, None, "!그래프 1000")
        await asyncio.gather(*list(core._TEMP))
        assert m2.replies[0].startswith("🎫") and "이미" in m3.replies[0]
        deleted = [c[2] for c in env.bot.named("delete")]
        assert deleted == [10_007, 10_008], deleted                    # 🎫 · '이미 걸었어요' 둘 다 지움
        assert slept == [15 + 3, core.TEMP_SECS]                       # 🎫 는 베팅 마감(15초) 무렵
    finally:
        core.temp_sleep = asyncio.sleep
        await TM.multi.abandon_all()
        TM.restore()


# ── 꺼진 방 안내: 방당 10분에 1번 ────────────────────────────
@test
async def gate_notice_once_per_ten_minutes():
    svc, bot, (u,) = await setup()
    core._room_noticed.clear()
    await svc.db.set_setting(CHAT, "casino_enabled", False)
    q = FakeQuery(CHAT, u)                                              # 룰렛 버튼판 알림은 방에 안 쌓임 → 횟수에 안 셈
    q.message.message_id = 5
    await casino.on_callback(svc, bot, q, ["rb", str(u.id), "1000", "빨강"])
    assert "꺼져" in q.answers[0][0] and q.answers[0][1]
    m1 = await K.cmd(svc, bot, u, "!블랙잭 1000")
    assert "꺼져" in m1.replies[-1] and not m1.deleted
    m2 = await K.cmd(svc, bot, u, "!지갑")
    assert not m2.replies and m2.deleted                               # 10분 안: 조용히, 명령 메시지 지우기 시도
    core._room_noticed[CHAT] -= core.GATE_QUIET + 1                    # 10분 지남
    m3 = await K.cmd(svc, bot, u, "!지갑")
    assert "꺼져" in m3.replies[-1]
    other = -100999                                                     # 다른 방은 따로
    await svc.db.set_setting(other, "casino_enabled", False)
    core._last_bet.clear()
    m4 = FakeMsg(other, u, "!지갑")
    await casino.dispatch(svc, bot, m4, other, u, None, "!지갑")
    assert "꺼져" in m4.replies[-1]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
