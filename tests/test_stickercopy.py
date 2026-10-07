"""🧩 스티커 따라 만들기 copy_sticker (sodam/panels/stickercopy.py) — 견본 읽기 → 원래 글자 지우기 → 원래 상자·색으로 새 글자 →
검수(원본 비교) → 뒤에서 보내기. 실측 2026-10-07 대한동구 루피 '출근완료' 3번 실패. python tests/run_all.py stickercopy"""
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, reply, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from sodam import stickerforge as SF  # noqa: E402
from sodam.panels import sticker as S, stickercopy as C  # noqa: E402

test, run_all = runner()
BOSS = fake_user(10, "루피", "luffy")
LUFFY_STICKER = {"texts": [{"text": "루피 등장", "box": [0.1, 0.78, 0.9, 0.95], "fill": [[255, 255, 255], [150, 170, 210]],
                            "stroke": [20, 30, 60], "stroke_px": 7, "shadow": True, "bold": True}], "background": "solid"}


def png(text=False):
    im = Image.new("RGB", (400, 400), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((100, 60, 300, 300), fill=(240, 120, 40))
    if text:
        d.rectangle((60, 320, 340, 380), fill=(20, 30, 60))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@test
def analysis_is_clipped_to_safe_values():
    a = C.clean_analysis({"texts": [{"text": "  루피   등장 ", "box": [-1, 0.8, 2, 0.95], "fill": [[300, -5, 10], "x"], "stroke": [1, 2],
                                     "stroke_px": 99}, {"text": "작음", "box": [0.1, 0.1, 0.11, 0.11]}, "garbage"],
                          "background": "neon"})
    t = a["texts"]
    assert len(t) == 1 and t[0]["text"] == "루피 등장" and t[0]["box"] == [0, 0.8, 1, 0.95] and t[0]["fill"] == [[255, 0, 10]]
    assert t[0]["stroke"] is None and t[0]["stroke_px"] == 20 and a["background"] == "solid"
    assert C.clean_analysis("not json") == {"texts": [], "background": "solid"}


@test
def new_text_goes_into_old_box_with_old_style():
    spec, err = SF.sanitize(C.build_spec(C.clean_analysis(LUFFY_STICKER), "출근완료", True, 7))
    assert not err, err
    lay = spec["layers"][0]
    assert lay["type"] == "text" and lay["text"] == "출근완료" and lay["at"] == (0.5, 0.865), lay
    assert lay["colors"] == [(255, 255, 255), (150, 170, 210)] and lay["stroke_color"] == (20, 30, 60) and lay["depth"] == 5
    assert spec["mode"] == "cutout" and spec["motion"][0]["type"] == "breathe"
    spec, _ = SF.sanitize(C.build_spec(C.clean_analysis({}), "안녕", False, 7))
    assert spec["layers"][0]["at"] == (0.5, 0.86) and spec["motion"][0]["type"] == "idle"   # 글자 없던 견본 → 아래쪽


async def _room():
    r = Room()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    await r.join(BOSS)
    return r


async def _ask(r, args, image=True, kind="sticker", frames=()):
    from sodam.agent import run_agent
    from sodam.permissions import Role
    from sodam.tools import ToolCtx
    from sodam.vision import Attached
    r.llm.script = [tool_call("copy_sticker", args), reply("답장해서 다시 부탁해 주세요")]
    att = Attached(png(True), "image/png", BOSS.id, kind=kind, frames=list(frames)) if image else None
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT), image=att)
    out = await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="출근완료 해서", extras={})
    for t in list(C.RUNNING.values()):
        await t
    row = await r.db._one("SELECT steps FROM agent_runs ORDER BY id DESC LIMIT 1")
    import json
    return out, [s["result"] for s in json.loads(row["steps"])]


def _patch(redraws, reads):
    saved = (C.analyze, S.redraw_source, S.read_text, S.COPY_CHECK)

    async def analyze(ctx, data):
        return C.clean_analysis(LUFFY_STICKER)

    async def redraw(ctx, src, prompt, day, **kw):
        redraws.append(prompt)
        return png(False), ""

    async def read(ctx, data):
        return reads.pop(0) if reads else []
    C.analyze, S.redraw_source, S.read_text, S.COPY_CHECK = analyze, redraw, read, True
    return saved


def _restore(saved):
    C.analyze, S.redraw_source, S.read_text, S.COPY_CHECK = saved


@test
async def copies_luffy_sticker_end_to_end_in_background():
    r = await _room()
    redraws = []
    saved = _patch(redraws, [["루피 등장"]])                         # 첫 지우기 뒤에도 남음 → 한 번 더 지움
    try:
        out, res = await _ask(r, {"text": "출근완료", "format": "static"})
        assert "만드는 중" in r.bot.named("send_message")[0][2] and "시작" in res[0]
        assert len(r.llm.of("chat")) == 1, "끝 도구 → 다음 AI 호출 없음"
        assert len(redraws) == 2 and "'루피 등장'" in redraws[0] and "Keep the character" in redraws[0]
        st = r.bot.named("send_sticker")
        assert len(st) == 1 and st[0][2].filename == "sticker.webp"
        assert st[0][4]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("spk:")
        assert r.bot.named("delete"), "만드는 중 글은 지움"
    finally:
        _restore(saved)


@test
async def soft_warnings_never_drop_the_result():
    """원본도 글자가 캐릭터를 가리는 디자인 → 자막 겹침 경고로 버리지 않음 (실측 실패 #2764·#2767)."""
    r = await _room()
    saved, real = _patch([], []), SF.forge_static

    async def warned(src, spec):
        res = await real(src, spec)
        res.warnings = ["자막이 피사체를 17% 덮음 → position=top"]
        return res
    SF.forge_static = warned
    try:
        await _ask(r, {"text": "출근완료", "format": "static"})
        assert len(r.bot.named("send_sticker")) == 1
    finally:
        SF.forge_static = real
        _restore(saved)


@test
async def needs_a_sample_and_reports_redraw_failure():
    r = await _room()
    _, res = await _ask(r, {"text": "출근완료"}, image=False)
    assert "답장" in res[0] and not r.bot.named("send_message")
    saved = _patch([], [])

    async def no(ctx, src, prompt, day, **kw):
        return None, "오늘 이 방 그림 한도를 다 써서 원본 고치기(redraw)는 안 됨."
    S.redraw_source = no
    try:
        await _ask(r, {"text": "출근완료"})
        edits = r.bot.named("edit_text")
        assert edits and "못 지웠어요" in edits[-1][2] and not r.bot.named("send_sticker"), edits
    finally:
        _restore(saved)


ALLIED = {"texts": [{"text": "안녕하세요", "box": [0.1, 0.75, 0.95, 0.95], "fill": [[250, 250, 255], [170, 170, 185], [70, 70, 90], [230, 230, 240]],
                     "stroke": [30, 20, 50], "stroke_px": 4, "outer_stroke": [140, 60, 220], "outer_px": 5,
                     "glow": [170, 80, 255], "glow_px": 12, "shadow": False, "italic": True, "backdrop": [25, 20, 35],
                     "font": "brush"}], "background": "transparent"}


def full_frame_white(fill_bottom=True):
    """인물이 아래·옆 가장자리까지 꽉 찬 흰 배경 그림 (그림 AI 가 글자 지운 결과 모양)."""
    im = Image.new("RGB", (512, 512), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((200, 10, 320, 130), fill=(150, 60, 220))
    d.rectangle((110, 160, 400, 511), fill=(25, 25, 30))
    d.rectangle((0, 330, 150, 511), fill=(35, 35, 35))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@test
def metallic_glow_text_style_is_copied():
    """실제 2026-10-07 얼라이드: 은색 금속 글자 + 보라 빛번짐 '안녕하세요' → 납작한 보라 글자로 나옴."""
    spec, err = SF.sanitize(C.build_spec(C.clean_analysis(ALLIED), "반갑습니다", False, 7, redrawn=True))
    assert not err, err
    lay = spec["layers"][-1]
    assert len(lay["colors"]) == 4 and lay["stroke2"] == 5 and lay["stroke2_color"] == (140, 60, 220), lay
    assert lay["glow"] == 12 and lay["glow_color"] == (170, 80, 255) and lay["font"] == "bold"
    assert spec["keying"]["mode"] == "white", "지운 그림은 흰 배경 → 흰색 빼기"
    assert lay["slant"] == 0.22 and spec["layers"][0]["type"] == "shape" and spec["layers"][0]["fill"] == (25, 20, 35)
    lay = spec["layers"][1]
    from sodam.stickerforge import layout
    plain = layout.render_text("반갑", stroke=4, stroke_color=(30, 20, 50))
    fancy = layout.render_text("반갑", stroke=4, stroke_color=(30, 20, 50), stroke2=5, stroke2_color=(140, 60, 220),
                               glow=12, glow_color=(170, 80, 255))
    assert fancy.width > plain.width + 20
    px = fancy.convert("RGBA").getpixel((3, fancy.height // 2))
    assert px[3] > 0 and px[2] > px[1], ("가장자리에 보라 빛번짐", px)
    leaned = layout.render_text("반갑", slant=0.3)
    a = leaned.getchannel("A")
    top = min(x for x in range(leaned.width) if a.getpixel((x, 2)) > 0)
    bottom = min(x for x in range(leaned.width) if a.getpixel((x, leaned.height - 3)) > 0)
    assert top > bottom, "slant + = 위가 오른쪽 (이탤릭)"


@test
async def full_frame_redraw_gets_transparent_background():
    """인물이 가장자리에 닿는 흰 배경 그림 → 예전엔 자동 판단이 'none' 이라 흰 네모 그대로 (얼라이드 '반갑습니다')."""
    from sodam.stickerforge import keying
    assert keying.detect_mode(Image.open(io.BytesIO(full_frame_white()))) == "white"
    spec, _ = SF.sanitize(C.build_spec(C.clean_analysis(ALLIED), "반갑습니다", False, 7, redrawn=True))
    res = await SF.forge_static(full_frame_white(), spec)
    out = Image.open(io.BytesIO(res.still)).convert("RGBA")
    assert res.ok and out.getpixel((60, 60))[3] == 0 and out.getpixel((470, 80))[3] == 0, res.summary()


@test
async def uses_sticker_just_made_without_reply():
    """'이걸로 반갑습니다 해줘' 를 답장 없이 → 방금 이 사람에게 만든 스티커로 (예전: '원본을 못 집어요')."""
    r = await _room()
    r.bot.files = getattr(r.bot, "files", {}) or {}
    r.bot.files["F1"] = png(True)
    from sodam import stickerpack
    await stickerpack.new_item(r.db, fmt="static", emoji="😎", chat_id=r.CHAT, user_id=BOSS.id, file_id="F1")
    saved = _patch([], [])
    try:
        _, res = await _ask(r, {"text": "반갑습니다", "format": "static"}, image=False)
        assert "시작" in res[0], res
        assert len(r.bot.named("send_sticker")) == 1
    finally:
        _restore(saved)


@test
async def swaps_words_keeping_lettering_design_first():
    """오너 2026-10-07 '유저는 100% 똑같은 걸 원함' → 그림 AI 가 글자 디자인 그대로 낱말만 바꿈. 맞게 써졌으면 코드 글자 없이 그대로."""
    r = await _room()
    redraws = []
    saved = _patch(redraws, [["반갑습니다"]])
    C.SWAP_FIRST = True
    try:
        _, res = await _ask(r, {"text": "반갑습니다", "format": "static"})
        assert len(redraws) == 1, (res, r.bot.named("edit_text"), r.bot.named("send_message"))
        assert len(redraws) == 1 and "exactly: \"반갑습니다\"" in redraws[0] and "'루피 등장'" in redraws[0], redraws
        assert len(r.bot.named("send_sticker")) == 1
        row = await r.db._one("SELECT spec FROM sticker_log ORDER BY id DESC LIMIT 1")
        assert '"layers": []' in row["spec"] and '"white"' in row["spec"], row["spec"]
        # 한글이 틀리게 써짐(반값습니다) → 지우고 코드로 쓰기
        redraws.clear()
        saved2 = _patch(redraws, [["반값습니다"], []])
        await _ask(r, {"text": "반갑습니다", "format": "static"})
        assert len(redraws) == 2 and "Remove all the written text" in redraws[1], redraws
        row = await r.db._one("SELECT spec FROM sticker_log ORDER BY id DESC LIMIT 1")
        assert '"반갑습니다"' in row["spec"], "코드가 쓴 글자 레이어"
        _restore(saved2)
    finally:
        C.SWAP_FIRST = False
        _restore(saved)
    assert C.swapped_ok(["반갑습니다"], "반갑습니다", ["안녕하세요"])
    assert not C.swapped_ok(["반갑습니다 안녕하세요"], "반갑습니다", ["안녕하세요"]) and not C.swapped_ok(None, "반갑", ["안녕"])


if __name__ == "__main__":
    run_all()
