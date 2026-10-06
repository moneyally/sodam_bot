"""📦 소담이 스티커 팩에 바로 넣기 (sodam/stickerpack.py) · 정지 스티커 · '글자만 바꿔서'(redraw + 남은 글자 검사).
python tests/run_all.py stickerpack"""
import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, reply, tool_call  # noqa: E402
from fakes import FakeQuery, fake_user, runner  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from sodam import handlers, hooks, stickerforge as SF, stickerpack as P  # noqa: E402
from sodam.panels import sticker as S  # noqa: E402

test, run_all = runner()
BOSS, LUFFY = fake_user(10, "방장", "boss"), fake_user(20, "루피", "luffy")


class PackBot:
    """FakeBot 에 스티커 팩 API 를 붙임: 팩 상태를 기억하고 텔레그램처럼 거절."""
    def __init__(self, bot, no_dm=()):
        self.bot, self.sets, self.no_dm = bot, {}, set(no_dm)
        bot.create_new_sticker_set, bot.add_sticker_to_set = self.create, self.add

    async def create(self, user_id, name, title, stickers, **kw):
        if user_id in self.no_dm:
            raise BadRequest("Peer_id_invalid")
        if name in self.sets:
            raise BadRequest("Sticker set name is already occupied")
        assert name.endswith("_by_" + self.bot.username) and "__" not in name and name[0].isalpha()
        self.sets[name] = list(stickers)
        self.bot.calls.append(("create_set", user_id, name, title))

    async def add(self, user_id, name, sticker, **kw):
        if name not in self.sets:
            raise BadRequest("Stickerset_invalid")
        if len(self.sets[name]) >= P.PER_SET:
            raise BadRequest("Stickers_too_much")
        self.sets[name].append(sticker)
        self.bot.calls.append(("add_set", user_id, name))


async def room():
    r = Room()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, LUFFY):
        await r.join(u)
    return r


async def press(r, user, data):
    q = FakeQuery(Room.CHAT, user, data)
    await handlers.on_callback(SimpleNamespace(callback_query=q, effective_user=user), r.ctx)
    return q


@test
async def button_creates_then_adds_to_pressers_own_pack():
    r = await room()
    pb = PackBot(r.bot)
    iid = await P.new_item(r.db, fmt="video", emoji="👋", chat_id=Room.CHAT, user_id=BOSS.id, file_id="F1")
    q = await press(r, LUFFY, f"spk:{iid}")                      # 방장이 만든 스티커를 루피가 누름 → 루피 팩
    name = P.pack_name(LUFFY.id, 1, r.bot.username)
    assert name in pb.sets and "넣었어요" in q.answers[-1][0], q.answers
    dm = [c for c in r.bot.named("send_message") if c[1] == LUFFY.id]
    assert dm and P.add_url(name) in str(dm[-1][3]["reply_markup"].to_dict())
    await press(r, LUFFY, f"spk:{iid}")
    assert len(pb.sets[name]) == 2 and r.bot.named("add_set")
    st = pb.sets[name][0]
    assert st.format == "video" and st.emoji_list == ("👋",) and st.sticker == "F1"


@test
async def full_pack_rolls_to_next_volume_and_deleted_pack_recreated():
    r = await room()
    pb = PackBot(r.bot)
    iid = await P.new_item(r.db, fmt="static", emoji="", chat_id=Room.CHAT, user_id=BOSS.id, file_id="F2")
    row = await P.item(r.db, iid)
    name1 = P.pack_name(BOSS.id, 1, r.bot.username)
    pb.sets[name1] = [None] * P.PER_SET                           # DB 는 모르는 꽉 찬 팩 (이름 이미 있음 → 추가 → 꽉 참 → 2권)
    name, n = await P.add(r.bot, r.db, BOSS.id, "방장", row)
    assert name == P.pack_name(BOSS.id, 2, r.bot.username) and n == 1, name
    del pb.sets[name]                                             # 사람이 팩을 지움 → 같은 권으로 새로
    name, _ = await P.add(r.bot, r.db, BOSS.id, "방장", row)
    assert name in pb.sets and len(pb.sets[name]) == 1


@test
async def without_dm_asks_to_open_dm_then_deep_link_adds():
    r = await room()
    pb = PackBot(r.bot, no_dm={LUFFY.id})
    iid = await P.new_item(r.db, fmt="video", emoji="😀", chat_id=Room.CHAT, user_id=BOSS.id, file_id="F3")
    q = await press(r, LUFFY, f"spk:{iid}")
    assert q.answers[-1][1] and "1:1" in q.answers[-1][0]
    kb = r.bot.named("send_message")[-1][3]["reply_markup"].to_dict()
    assert f"start=spk_{iid}" in str(kb)
    pb.no_dm.clear()                                              # 1:1 을 열었음
    out = []
    msg = SimpleNamespace(from_user=LUFFY, reply_text=lambda t, **kw: _rec(out, t))
    await hooks.DEEP_LINKS["spk"](r.svc, r.bot, msg, str(iid))
    assert "넣었어요" in out[-1] and P.pack_name(LUFFY.id, 1, r.bot.username) in pb.sets
    q = await press(r, LUFFY, "spk:999999")
    assert "오래된" in q.answers[-1][0]


async def _rec(out, t):
    out.append(t)


@test
def leftover_text_check():
    assert S.leftover(["안녕하세요!", "포인트 지급완료입니다"], "안녕하세요", "포인트 지급완료입니다") == "안녕하세요!"
    assert S.leftover(["포인트 지급완료입니다"], "안녕하세요", "포인트 지급완료입니다") == ""
    assert S.leftover(["안녕"], "안녕하세요", "x") == "안녕"                       # 일부만 남아도 남은 것
    assert S.leftover(["안녕하세요 친구"], "안녕하세요", "안녕하세요 친구") == ""     # 새 글자 안에 들어 있으면 괜찮음


def png():
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(im).ellipse((60, 40, 240, 260), fill=(240, 120, 40))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@test
def static_sticker_is_webp_512_with_value_colors():
    spec, err = SF.sanitize({"mode": "cutout", "caption": {"text": "포인트 지급완료입니다", "top": "#ffffff", "extrude": [20, 30, 90],
                                                            "stroke": 99, "depth": 8}})
    assert not err and spec["caption"]["top"] == (255, 255, 255) and spec["caption"]["stroke"] == 16   # 범위로 자름
    res = SF.render_static(png(), spec)
    assert res.ok and res.still[:4] == b"RIFF" and res.still[8:12] == b"WEBP", res.summary()
    assert not any(w.startswith("너무 밋밋") for w in res.warnings)


async def _agent(r, script, image):
    from sodam.agent import run_agent
    from sodam.permissions import Role
    from sodam.tools import ToolCtx
    from sodam.vision import Attached
    r.llm.script = [*script, reply("짠!")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT),
                  image=Attached(image, "image/png", BOSS.id))
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="스티커", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"], ctx


@test
async def remake_redraws_then_blocks_when_old_text_left():
    """실제(10-06 루피): '이런 걸로 포인트 지급완료입니다' → 원래 '안녕하세요' 위에 새 글자만 얹음. 이제 redraw + 남은 글자 검사."""
    r = await room()
    drawn, read = [], [["안녕하세요", "포인트 지급완료입니다"]]

    async def image(prompt, source=None, chat_id=None):
        drawn.append(prompt)
        return png()

    async def ocr(ctx, data):
        return read[0]
    r.svc.llm.image, orig = image, S.read_text
    S.read_text = ocr
    try:
        args = {"spec": {"mode": "cutout", "caption": {"text": "포인트 지급완료입니다"}}, "format": "static",
                "redraw": "remove the text '안녕하세요'", "old_text": "안녕하세요", "request": "이런걸로 포인트 지급완료입니다"}
        res, ctx = await _agent(r, [tool_call("make_sticker", args)], png())
        assert "남아 있어 안 보냄" in res[0] and not r.bot.named("send_sticker"), res
        assert len(drawn) == 1 and "Do not write any text" in drawn[0] and "remove the text" in drawn[0]
        read[0] = ["포인트 지급완료입니다"]
        res, _ = await _agent(r, [tool_call("make_sticker", args)], png())
        assert "보냈음" in res[0], res
        sent = r.bot.named("send_sticker")[-1]
        assert sent[2].filename == "sticker.webp"
        assert await r.db.counter(__import__("datetime").datetime.now(r.svc.cfg.tz).strftime("%Y-%m-%d"), r.CHAT, "image") == 2
    finally:
        S.read_text = orig


if __name__ == "__main__":
    run_all()
