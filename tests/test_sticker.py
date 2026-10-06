"""🧩 스티커 공방 (sodam/stickerforge · panels/sticker.py): 진짜 렌더링 + 텔레그램 검사표. python tests/run_all.py sticker"""
import io
from types import SimpleNamespace

from PIL import Image, ImageDraw

from fake_llm import Room, reply, tool_call
from fakes import runner
from test_sanction_multi import A, BOSS

from sodam import stickerforge as SF
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.tools import ToolCtx
from sodam.vision import Attached

test, run_all = runner()


def mascot(bg="white") -> bytes:
    im = Image.new("RGB", (420, 420), bg)
    d = ImageDraw.Draw(im)
    d.ellipse((110, 60, 310, 250), fill=(255, 214, 150), outline=(40, 30, 30), width=6)
    d.ellipse((165, 130, 190, 160), fill=(20, 20, 20)); d.ellipse((230, 130, 255, 160), fill=(20, 20, 20))
    d.rounded_rectangle((140, 245, 280, 380), 30, fill=(30, 32, 40), outline=(20, 20, 20), width=6)   # 어두운 옷
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@test
def sanitize_keeps_only_known_names_numbers_and_safe_ranges():
    spec, err = SF.sanitize({"mode": "cutout", "keying": "glow", "font": "/etc/passwd",
                             "motion": [{"type": "jab", "times": [0.2, 2.9, 5], "evil": "x"}, "idle", "shake"],
                             "fx": [{"type": "glitch", "amp": 50}, {"type": "flashbang", "amount": 200},
                                    {"type": "glow", "color": [300.5, 12.2, -4]}, {"type": "sparkle", "subset": [1, 99, 3]},
                                    {"type": "hearts"}],
                             "caption": {"text": "출근완료", "palette": "rainbowx", "anims": ["bounce", "explode"], "font": "/x"}})
    assert err is None and "font" not in spec and spec["keying"]["mode"] == "glow"
    assert [m["type"] for m in spec["motion"]] == ["jab", "idle"]                            # 2개까지
    assert spec["motion"][0]["times"] == [0.2, 2.3, 2.3] and "evil" not in spec["motion"][0]  # 충격은 끝나기 전에
    fx = {f["type"]: f for f in spec["fx"]}
    assert len(spec["fx"]) == 4 and fx["glitch"]["amp"] == 9 and fx["flashbang"]["amount"] == 60   # 하얗게 날아가지 않게
    assert fx["glow"]["color"] == (255, 12, 0) and fx["sparkle"]["subset"] == [1, 3]
    assert spec["caption"] == {"text": "출근완료", "palette": "gold", "anims": ["bounce"], "position": "bottom", "typing": True}
    for bad in ({"fx": [{"type": "rain", "image": "/etc/passwd"}]}, {"fx": ["rise"]}, {"motion": ["teleport"]},
                {"mode": "video"}, {"keying": "magic"}, {"caption": "열세글자가넘는아주긴자막입니다"}):
        spec, err = SF.sanitize(bad)
        assert spec is None and err, bad                                                     # 파일 경로 받는 효과·모르는 이름 거절


@test
async def real_render_passes_every_telegram_check_and_keeps_dark_clothes():
    spec, _ = SF.sanitize({"motion": ["idle"], "fx": ["sparkle"], "caption": {"text": "안녕", "palette": "pink"}})
    res = await SF.forge(mascot(), spec, icon=True)
    rows = {n: (v, ok) for n, v, ok in res.rows}
    assert res.ok and res.keying == "white", res.summary()
    for name in ("dimensions", "codec", "duration", "size", "alpha_mode", "alpha_range", "audio", "glyphs"):
        assert rows[name][1], (name, rows[name])
    assert res.webm[:4] == b"\x1aE\xdf\xa3" and 0 < len(res.icon) <= 32 * 1024              # WebM · 팩 아이콘 32KB↓
    prev = Image.open(io.BytesIO(res.preview))
    assert prev.size == (800, 400)
    photo, _ = SF.sanitize({"mode": "photo", "radius": 256, "motion": [{"type": "punch", "hits": 2}], "fx": ["slice_glitch"]})
    res = await SF.forge(mascot("navy"), photo)
    assert res.ok and res.keying == "photo", res.summary()


async def ask(r, caller, script, image=None):
    r.llm.script = [*script, reply("짠!")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, caller, Role.MEMBER, await r.db.get_settings(r.CHAT), image=image)
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="스티커", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


@test
async def tool_sends_sticker_and_file_retries_lighter_and_limits():
    from sodam.panels import sticker as P
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    calls = []

    async def fake_forge(image, spec, icon=False):
        calls.append([f["type"] for f in spec["fx"]])
        ok = len(calls) != 1                                                    # 첫 번째는 크기 초과로 실패
        return SF.Result(ok, b"\x1aE\xdf\xa3webm" if ok else b"", b"", b"",
                         [("size", "300KB / 256KB", ok)], "white")
    orig, SF.forge = SF.forge, fake_forge
    try:
        img = Attached(mascot(), "image/png", BOSS.id)
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"fx": ["sparkle", "glitch"], "caption": "출근완료"}})], image=img)
        assert "보냈음" in res[0] and calls == [["sparkle", "glitch"], ["sparkle"]], (res, calls)   # 효과 하나 덜고 다시
        kb = r.bot.named("send_sticker")[-1][4]["reply_markup"]                 # 파일·@Stickers 안내 대신 팩 넣기 버튼
        assert kb.inline_keyboard[0][0].callback_data.startswith("spk:") and not r.bot.named("send_document")
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"fx": [{"type": "rain", "image": "/etc/passwd"}]}})], image=img)
        assert "spec 오류" in res[0], res
        for _ in range(P.FREE_DAILY - 1):
            await ask(r, A, [tool_call("make_sticker", {"spec": {}})], image=img)
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {}})], image=img)
        assert "하루" in res[0] and len(r.bot.named("send_sticker")) == P.FREE_DAILY, res
    finally:
        SF.forge = orig


_ = SimpleNamespace
