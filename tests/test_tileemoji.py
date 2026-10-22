"""🧩 소담 분할 이모지 자동 제작 (2026-10-22 '챗지피티처럼·자율모드', sodam/panels/tileemoji.py).
build_spec(글자→타일 spec) + make_tile_emoji 도구(오너 팩 생성→addemoji 링크).

뮤테이션: build_spec 이 글자당 타일 안 만들면 count FAIL / name 검사 빼면 invalid_name FAIL /
create_pack 이 sticker_type='custom_emoji' 안 주면 pack_is_custom_emoji FAIL.
"""
from types import SimpleNamespace

from fakes import fake_user, runner

from sodam.panels import tileemoji as TE
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
OWNER = 777


class PackBot:
    def __init__(self):
        self.sets, self.added, self.msgs, self.photos = [], [], [], []

    async def get_me(self):
        return SimpleNamespace(username="sodam_ai_bot")

    async def create_new_sticker_set(self, **kw):
        self.sets.append(kw)

    async def add_sticker_to_set(self, **kw):
        self.added.append(kw)

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append((chat_id, caption))

    async def send_message(self, chat_id, text, **kw):
        self.msgs.append((chat_id, text))


def _ctx(bot):
    return ToolCtx(SimpleNamespace(), bot, OWNER, fake_user(OWNER, "오너"), Role.OWNER, {})


@test
async def build_spec_one_tile_per_char():
    s = TE.build_spec("로얄클럽", "gold", "gold")
    assert s["cols"] == 4 and s["rows"] == 1 and len(s["layers"]) == 4
    assert all(l["style"] == "gold" for l in s["layers"])
    from sodam.stickerforge import banner as B
    r = B.build_tiles(s)
    assert r["count"] == 4 and r["qa_error"] == 0.0


@test
async def hangul_bold_latin_royal():
    assert TE._font_for("로", "gold") == "bold"
    assert TE._font_for("R", "gold") == "royal"      # 영문 금색 = 로열 세리프
    assert TE._font_for("R", "neon") == "heavy"


@test
async def wraps_to_two_rows_over_ten():
    s = TE.build_spec("ABCDEFGHIJKL", "chrome", "none")   # 12자 → 2줄
    assert s["cols"] == 10 and s["rows"] == 2 and len(s["layers"]) == 12
    assert s["frames"] is None                            # frame none


@test
async def invalid_name_rejected_no_pack():
    bot = PackBot()
    out = await TE.t_make_tile_emoji(_ctx(bot), {"text": "로얄", "name": "123bad"})
    assert "name" in out and not bot.sets                  # 팩 안 만들어짐


@test
async def empty_text_rejected():
    bot = PackBot()
    out = await TE.t_make_tile_emoji(_ctx(bot), {"text": "   ", "name": "royalclub"})
    assert "글자" in out and not bot.sets


@test
async def creates_custom_emoji_pack_and_links():
    bot = PackBot()
    ctx = _ctx(bot)
    out = await TE.t_make_tile_emoji(ctx, {"text": "로얄클럽", "name": "royalclub", "style": "gold"})
    assert len(bot.sets) == 1
    s = bot.sets[0]
    assert s["sticker_type"] == "custom_emoji" and s["user_id"] == OWNER and len(s["stickers"]) == 4
    assert "t.me/addemoji/royalclub_by_sodam_ai_bot" in out
    assert ctx.quiet is True and bot.msgs                  # 오너에게 링크 보냄


@test
async def unknown_style_and_frame_fall_back():
    bot = PackBot()
    out = await TE.t_make_tile_emoji(_ctx(bot), {"text": "AB", "name": "test", "style": "없음", "frame": "없음"})
    assert len(bot.sets) == 1 and "addemoji" in out        # gold 로 떨어져 그대로 만들어짐


if __name__ == "__main__":
    run_all()
