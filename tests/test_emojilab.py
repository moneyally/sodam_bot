"""🧩 이모지 공방 (sodam/emojilab.py · tools/emoji_pack.py, 2026-10-10 오너 '제휴업체' 타일 이모지): python tests/run_all.py emojilab"""
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner
from PIL import Image

from sodam import emojilab as E

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import emoji_pack as P  # noqa: E402

test, run_all = runner()
OWNER, MEMBER = 1, 7
TILE = "🟨"   # 한 칸 = UTF-16 2 단위 (위치 계산 확인용)


def webp(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (100, 100), color).save(buf, "WEBP")
    return buf.getvalue()


class Bot(FakeBot):
    def __init__(self):
        super().__init__()
        self.files = {f"F{i}": webp((40 * i, 200, 0, 255)) for i in range(1, 7)}

    async def get_custom_emoji_stickers(self, ids):
        self.calls.append(("get_custom", list(ids)))
        return [SimpleNamespace(custom_emoji_id=i, file_id=f"F{i}", set_name="jehyu_by_x", emoji="⭐",
                                is_animated=False, is_video=False, width=100, height=100, needs_repainting=False)
                for i in ids if i != "6"]

    async def get_sticker_set(self, name):
        return SimpleNamespace(title="제휴업체", sticker_type="custom_emoji",
                               stickers=[SimpleNamespace(custom_emoji_id=str(i)) for i in range(1, 5)])


def emoji_msg(user, rows, note=""):
    """rows = [[id,…],…] → 텔레그램처럼 UTF-16 위치를 가진 custom_emoji 글."""
    text, ents = note, []
    for r, row in enumerate(rows):
        if r or text:
            text += "\n"
        for cid in row:
            ents.append(SimpleNamespace(type="custom_emoji", custom_emoji_id=cid,
                                        offset=len(text.encode("utf-16-le")) // 2, length=2))
            text += TILE
    m = SimpleNamespace(text=text, entities=ents, caption=None, caption_entities=None, from_user=user,
                        replies=[], photos=[])

    async def reply_text(t, **kw):
        m.replies.append(t)

    async def reply_photo(p, caption=None, **kw):
        m.photos.append((p, caption))
    m.reply_text, m.reply_photo = reply_text, reply_photo
    return m


async def setup():
    db = await make_db()
    svc = await make_svc(db)

    async def owners():
        return {OWNER}
    svc.perms.owners = owners
    return svc


@test
async def owner_tiles_are_saved_in_order_with_rows_and_preview():
    svc = await setup()
    bot = Bot()
    m = emoji_msg(fake_user(OWNER, "오너"), [["1", "2", "3", "4"], ["5", "6", "1"]], note="이거 분석")
    assert await E.maybe_capture(svc, bot, m, m.from_user)
    assert bot.named("get_custom")[0][1] == ["1", "2", "3", "4", "5", "6"], "같은 조각은 한 번만"
    out = sorted(E.lab_dir(svc.cfg.db_path).iterdir())[-1]
    meta = json.loads((out / "meta.json").read_text())
    assert meta["rows"] == [["1", "2", "3", "4"], ["5", "6", "1"]] and meta["note"] == "이거 분석"
    assert meta["missing"] == ["6"] and meta["sets"]["jehyu_by_x"]["count"] == 4
    assert (out / meta["tiles"]["3"]["file"]).read_bytes() == bot.files["F3"]
    assert (out / "preview.png").exists() and Image.open(out / "preview.png").size == (420, 220)
    assert m.photos and "조각 7칸" in m.photos[0][1] and "못 받은 조각 1개" in m.photos[0][1]


@test
async def members_and_normal_chat_go_on_to_the_ai():
    svc = await setup()
    bot = Bot()
    m = emoji_msg(fake_user(MEMBER, "멤버"), [["1", "2"]])
    assert not await E.maybe_capture(svc, bot, m, m.from_user)
    long_talk = emoji_msg(fake_user(OWNER, "오너"), [["1", "2"]],
                          note="소담아 오늘 백악관 방 분위기 어땠는지 길게 정리해서 알려줘 부탁해 내일 회의 때 쓸 거야")
    assert not await E.maybe_capture(svc, bot, long_talk, long_talk.from_user)
    one = emoji_msg(fake_user(OWNER, "오너"), [["1"]], note="소담아 ㅋㅋ 고마워")
    assert not await E.maybe_capture(svc, bot, one, one.from_user), "이모지 하나 + 평소 말"
    plain = SimpleNamespace(text="안녕", entities=[], caption=None, caption_entities=None)
    assert E.tiles_of(plain) == ([], "안녕")
    assert not bot.named("get_custom")


@test
def utf16_offsets_after_wide_characters_keep_order():
    m = emoji_msg(fake_user(OWNER), [["a", "b"]], note="🎉🎉 앞")
    rows, note = E.tiles_of(m)
    assert rows == [["a", "b"]] and note == "🎉🎉 앞"


@test
def pack_tool_orders_tiles_and_checks_size():
    d = Path(tempfile.mkdtemp())
    for n in ("10.png", "2.png", "1.png", "readme.txt", "preview.png"):
        Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(d / n) if n.endswith(".png") else (d / n).write_text("x")
    assert [p.name for p in P.tiles(d)] == ["1.png", "2.png", "10.png"]
    data, fmt, name = P.load(d / "2.png")
    assert fmt == "static" and name == "2.webp" and Image.open(io.BytesIO(data)).format == "WEBP"
    Image.new("RGBA", (120, 100)).save(d / "3.png")
    try:
        P.load(d / "3.png")
        raise AssertionError("크기 틀린 조각은 거절해야 함")
    except ValueError:
        pass
    (d / "4.webm").write_bytes(b"x" * (64 * 1024 + 1))
    try:
        P.load(d / "4.webm")
        raise AssertionError("64KB 넘는 영상 조각은 거절해야 함")
    except ValueError:
        pass


if __name__ == "__main__":
    run_all()
