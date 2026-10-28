"""🧩 커스텀 이모지 만들기 (make_emoji, sodam/panels/tileemoji.py · emojipack.py · stickerforge/emoji.py).
채팅 글자 옆에 붙여 쓰는 100×100 프리미엄 커스텀 이모지 — 멤버도·방/1:1·글자 또는 사진. (2026-10-28 오너 요청)
python tests/run_all.py make_emoji

뮤테이션:
 1) emojipack.add_emojis 의 'count>=PER_SET → 다음 권' 제거 → full_pack_rolls_to_next_vol FAIL
 2) tileemoji 하루 한도 검사(counter>=cap) 제거 → daily_cap_blocks FAIL
 3) emoji.image_to_emoji 의 keying(배경 빼기)을 원본 그대로로 → photo_emoji_is_transparent FAIL
 4) emojipack 이 Forbidden/peer_invalid 에 NeedsDM 안 던짐 → needs_dm FAIL
 5) tileemoji 가 db.bump(emo:) 안 함 → two_calls_count_toward_cap FAIL
"""
import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, fake_user, make_db, make_svc, runner  # noqa: E402
from telegram.error import BadRequest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from sodam import emojipack as EP  # noqa: E402
from sodam.panels import tileemoji as TE  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.stickerforge import emoji as EM  # noqa: E402
from sodam.tools import ToolCtx  # noqa: E402
from sodam.vision import Attached  # noqa: E402

test, run_all = runner()
ROOM = -100555
MEM = fake_user(20, "멤버", "mem")
import datetime as _dt  # noqa: E402


def make_webps(n: int = 1) -> list:
    out = []
    for _ in range(n):
        im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse([10, 10, 90, 90], fill=(200, 30, 30, 255))
        b = io.BytesIO()
        im.save(b, "WEBP", lossless=True)
        out.append(b.getvalue())
    return out


def photo_png() -> bytes:
    im = Image.new("RGB", (300, 300), (255, 255, 255))      # 흰 배경 + 빨간 원
    ImageDraw.Draw(im).ellipse([60, 60, 240, 240], fill=(200, 30, 30))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class EmojiBot:
    """FakeBot 에 커스텀 이모지 팩 API 를 붙임 (텔레그램처럼 상태 기억·거절)."""
    def __init__(self, bot, no_dm=()):
        self.bot, self.sets, self.no_dm = bot, {}, set(no_dm)
        bot.create_new_sticker_set = self.create
        bot.add_sticker_to_set = self.add
        bot.get_sticker_set = self.get

    async def create(self, user_id, name, title, stickers, sticker_type=None, **kw):
        if user_id in self.no_dm:
            raise BadRequest("Peer_id_invalid")
        assert sticker_type == "custom_emoji", sticker_type
        assert name.endswith("_by_" + self.bot.username) and name[0].isalpha() and "__" not in name, name
        if name in self.sets:
            raise BadRequest("Sticker set name is already occupied")
        self.sets[name] = list(stickers)
        self.bot.calls.append(("create_emoji_set", user_id, name, len(stickers)))

    async def add(self, user_id, name, sticker, **kw):
        if user_id in self.no_dm:
            raise BadRequest("Peer_id_invalid")
        if name not in self.sets:
            raise BadRequest("Stickerset_invalid")
        if len(self.sets[name]) >= EP.PER_SET:
            raise BadRequest("Stickers_too_much")
        self.sets[name].append(sticker)

    async def get(self, name):
        n = len(self.sets.get(name, []))
        return SimpleNamespace(stickers=[SimpleNamespace(custom_emoji_id=f"{name}:{i}") for i in range(n)])


async def world(no_dm=()):
    db = await make_db()
    svc = await make_svc(db, admins=())
    await db.ensure_chat(ROOM, "이모지방")
    await db.upsert_user(MEM)
    bot = FakeBot()
    EmojiBot(bot, no_dm=no_dm)
    return db, svc, bot


def ctx(svc, bot, chat=ROOM, uid=MEM.id, role=Role.MEMBER, settings=None):
    return ToolCtx(svc, bot, chat, fake_user(uid, "멤버", "mem"), role, settings or {"emoji_daily": 5})


# ── emojipack (팩 기계장치) ──────────────────────────────────────
@test
async def add_emojis_creates_pack():
    db, svc, bot = await world()
    name, count, ids = await EP.add_emojis(bot, db, MEM.id, "제휴업체", make_webps(3))
    assert name == f"sodamemoji{MEM.id}_by_sodambot", name
    assert count == 3 and len(ids) == 3, (count, ids)
    # 다시 넣으면 같은 팩에 쌓임
    name2, count2, _ = await EP.add_emojis(bot, db, MEM.id, "x", make_webps(2))
    assert name2 == name and count2 == 5, (name2, count2)


@test
async def full_pack_rolls_to_next_vol():
    """텔레그램 1권 팩이 꽉 차면(too_much) 다음 권으로 (DB 가 아직 자리 있다고 해도 — in-loop 처리)."""
    db, svc, bot = await world()
    name1 = EP.pack_name(MEM.id, 1, "sodambot")
    await EP.add_emojis(bot, db, MEM.id, "x", make_webps(1))   # 1권 생성
    ebw = bot.add_sticker_to_set.__self__                      # EmojiBot 래퍼
    ebw.sets[name1] = [object()] * EP.PER_SET                  # 텔레그램 쪽은 꽉 참
    await db._write("UPDATE emoji_packs SET count=10 WHERE user_id=? AND vol=1", (MEM.id,))  # DB 는 자리 있다고 (stale)
    name, count, _ = await EP.add_emojis(bot, db, MEM.id, "x", make_webps(1))
    assert "v2" in name and count == 1, (name, count)         # 꽉 찼으니 2권으로


@test
async def needs_dm():
    db, svc, bot = await world(no_dm={MEM.id})
    try:
        await EP.add_emojis(bot, db, MEM.id, "x", make_webps(1))
        assert False, "NeedsDM 를 던져야 함"
    except EP.NeedsDM:
        pass


# ── 사진 → 이모지 (배경 빼기) ──────────────────────────────────
@test
async def photo_emoji_is_transparent():
    """흰 배경 빨간 원 → 배경이 빠져 투명 비율이 높아야 함 (여백만이 아니라 원 밖 흰 배경까지)."""
    import numpy as np
    img = EM.image_to_emoji(photo_png())
    assert img.size == (100, 100), img.size
    a = np.asarray(img.getchannel("A"))
    assert (a < 16).mean() > 0.4, f"배경이 안 빠짐(투명 비율 낮음): {(a<16).mean():.2f}"   # keying 없으면 ~0.22 (여백뿐)
    assert a[50, 50] > 200, f"가운데 피사체는 남아야 함: {a[50,50]}"
    w = EM.to_webp(img)
    assert Image.open(io.BytesIO(w)).size == (100, 100)


# ── 도구 make_emoji ────────────────────────────────────────────
@test
async def tool_text_mode_makes_pack():
    db, svc, bot = await world()
    c = ctx(svc, bot)
    out = await TE.t_make_emoji(c, {"text": "A", "style": "gold"})
    assert "addemoji" in out and c.quiet, out
    assert bot.named("create_emoji_set"), "팩이 만들어져야 함"
    # 링크를 방에 보냄
    assert any("addemoji" in str(cc) for cc in bot.calls), bot.calls
    # 하루 한도 카운터가 올라감
    day = _dt.datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await db.counter(day, 0, f"emo:{MEM.id}") == 1


@test
async def tool_photo_mode():
    db, svc, bot = await world()
    c = ctx(svc, bot)
    c.image = Attached(photo_png(), "image/png", MEM.id)     # 붙은·답장한 사진 경로
    out = await TE.t_make_emoji(c, {"source": "photo"})
    assert "addemoji" in out and c.quiet, out
    assert bot.named("create_emoji_set"), out


@test
async def daily_cap_blocks():
    db, svc, bot = await world()
    day = _dt.datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    for _ in range(5):
        await db.bump(day, 0, f"emo:{MEM.id}")               # 이미 5개 썼음
    out = await TE.t_make_emoji(ctx(svc, bot, settings={"emoji_daily": 5}), {"text": "A"})
    assert "하루" in out and "5개" in out, out
    assert not bot.named("create_emoji_set"), "한도 넘으면 안 만들어야 함"


@test
async def owner_bypasses_cap():
    db, svc, bot = await world()
    day = _dt.datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    for _ in range(5):
        await db.bump(day, 0, f"emo:{MEM.id}")
    out = await TE.t_make_emoji(ctx(svc, bot, role=Role.OWNER, settings={"emoji_daily": 5}), {"text": "A"})
    assert "addemoji" in out, out                            # 오너는 한도 면제


@test
async def two_calls_count_toward_cap():
    db, svc, bot = await world()
    await TE.t_make_emoji(ctx(svc, bot, settings={"emoji_daily": 2}), {"text": "A"})
    await TE.t_make_emoji(ctx(svc, bot, settings={"emoji_daily": 2}), {"text": "B"})
    out = await TE.t_make_emoji(ctx(svc, bot, settings={"emoji_daily": 2}), {"text": "C"})
    assert "하루" in out, out                                # 세 번째는 막힘 (bump 가 쌓여야)


@test
async def tool_needs_dm_shows_open_button():
    db, svc, bot = await world(no_dm={MEM.id})
    c = ctx(svc, bot)
    out = await TE.t_make_emoji(c, {"text": "A"})
    assert "1:1" in out and c.quiet, out
    assert any("1:1" in str(cc) for cc in bot.named("send_message")), bot.calls


@test
async def emoji_off_blocks_members():
    db, svc, bot = await world()
    out = await TE.t_make_emoji(ctx(svc, bot, settings={"emoji_daily": 0}), {"text": "A"})
    assert "꺼져" in out, out
    # 오너는 0 이어도 됨
    out2 = await TE.t_make_emoji(ctx(svc, bot, role=Role.OWNER, settings={"emoji_daily": 0}), {"text": "A"})
    assert "addemoji" in out2, out2


if __name__ == "__main__":
    run_all()
