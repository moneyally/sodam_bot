"""🧩 스티커 공방 보강 (framing·qc warnings·recipes·render_mp4·그림 판별): python tests/run_all.py sticker_upgrade

진짜 엔진·진짜 ffmpeg. AI 호출 없음.
"""
import io
import subprocess
import time

from PIL import Image, ImageChops, ImageDraw, ImageStat

from fakes import runner
from test_sticker import mascot

from sodam import stickerforge as SF
from sodam.stickerforge import engine, qc, recipes

test, run_all = runner()


def portrait(w=600, h=1000, face_y=140) -> Image.Image:
    """세로로 긴 '인물 사진': 얼굴(밝은 원 + 눈) 이 위쪽, 아래는 밋밋한 옷. 무늬 배경 (단색이면 잘림 검사가 무의미)."""
    im = Image.new("RGB", (w, h), (70, 80, 110))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 24):                                            # 배경 무늬
        d.line((x, 0, x, h), fill=(80, 92, 125), width=3)
    d.ellipse((w / 2 - 110, face_y - 110, w / 2 + 110, face_y + 110), fill=(245, 205, 170), outline=(90, 60, 40), width=5)
    d.ellipse((w / 2 - 50, face_y - 30, w / 2 - 25, face_y), fill=(20, 20, 20))
    d.ellipse((w / 2 + 25, face_y - 30, w / 2 + 50, face_y), fill=(20, 20, 20))
    d.rounded_rectangle((w / 2 - 170, face_y + 120, w / 2 + 170, h), 40, fill=(40, 45, 60))   # 옷
    return im


def png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def pattern(w=900, h=700) -> bytes:
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 40):
        for y in range(0, h, 40):
            d.rectangle((x, y, x + 39, y + 39), fill=((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256))
    return png(im)


# ── 1. photo 모드가 얼굴(위쪽)을 안 자름 ──────────────────────────
@test
def photo_framing_keeps_the_face_instead_of_center_cropping():
    im = portrait()
    box = engine.focus_window(im)                                          # (x0, y0, x1, y1) 정사각형
    assert box[2] - box[0] == box[3] - box[1] == 600 and box[1] <= 40, box  # 위쪽 우선 (얼굴 140px)
    master, _, info = engine.prepare_photo(im.convert("RGBA"), framing="auto")
    assert info["window"][1] <= 40 and info["framing"] == "auto"
    master, _, info = engine.prepare_photo(im.convert("RGBA"), framing="center")
    assert info["window"][1] == 200                                        # 가운데면 얼굴이 잘림 (옛 동작)
    master, _, info = engine.prepare_photo(im.convert("RGBA"), framing="blur")
    assert info["framing"] == "blur"                                       # 흐린 배경 위에 전체
    a = master.getchannel("A")
    assert a.getextrema() == (255, 255)                                    # blur 도 프레임이 꽉 참 (투명 X)
    wide = portrait(1000, 600, 300).transpose(Image.Transpose.ROTATE_90)   # 가로로 긴 사진도 정사각형
    box = engine.focus_window(wide)
    assert box[2] - box[0] == box[3] - box[1] == 600
    spec, err = SF.sanitize({"mode": "photo", "framing": "top"})
    assert err is None and spec["framing"] == "top"
    spec, _ = SF.sanitize({"mode": "photo", "framing": "sideways"})
    assert spec["framing"] == "auto"                                       # 모르는 값은 auto


# ── 2. 코드로 세는 검수 지표 → warnings ────────────────────────────
def frames_of(spec_raw, image_bytes):
    spec, err = SF.sanitize(spec_raw)
    assert err is None, err
    res = SF.render(image_bytes, spec)
    assert res.ok or res.rows, res.error
    return res


@test
def warnings_name_caption_overlap_clipping_flat_and_whiteout():
    # 자막이 피사체를 덮음: 마스코트를 아래까지 꽉 채우고 자막을 위(top)가 아니라 마스코트 위로 강제
    res = frames_of({"mode": "photo", "radius": 8, "framing": "center", "motion": ["still"],
                     "caption": {"text": "안녕하세요", "position": "bottom"}}, png(portrait(600, 600, 470)))
    assert any("자막" in w for w in res.warnings), res.warnings          # 얼굴이 아래쪽 → 자막과 겹침
    assert res.metrics["caption_overlap"] > 0.2
    # 잘림: 마진 0 + 큰 점프 → 가장자리에 불투명 픽셀
    res = frames_of({"motion": [{"type": "hop", "height": 420}], "margin": 0.04}, mascot())
    assert any("잘" in w for w in res.warnings), res.warnings
    assert res.metrics["edge_clip"] > 0
    # 밋밋함: 움직임 없음
    res = frames_of({"motion": ["still"], "fx": []}, mascot())
    assert any("밋밋" in w for w in res.warnings), res.warnings
    assert res.metrics["motion_mean"] < 0.5
    # 하얗게 날아감: flashbang 최대 + 큰 flare
    res = frames_of({"mode": "photo", "radius": 8, "motion": [{"type": "jab", "times": [0.3, 1.2, 2.0]}],
                     "fx": [{"type": "flashbang", "amount": 60, "tau": 0.4}]},
                    png(Image.new("RGB", (400, 400), (235, 235, 235))))
    assert any("하얗" in w for w in res.warnings), res.warnings
    # 깔끔한 것은 경고 없음
    res = frames_of({"motion": ["idle"], "fx": ["sparkle"], "caption": "안녕"}, mascot())
    assert res.ok and res.warnings == [], res.warnings
    assert 0.3 <= res.metrics["motion_mean"] and res.metrics["whiteout_frames"] == 0


# ── 3. 레시피 데이터 + 변주 ────────────────────────────────────────
@test
def recipes_cover_kinds_and_vary_within_limits():
    assert len(recipes.RECIPES) >= 20
    names = [r["name"] for r in recipes.RECIPES]
    assert len(names) == len(set(names))
    for r in recipes.RECIPES:
        spec, err = SF.sanitize(recipes.vary(r, seed=3))
        assert err is None and spec["motion"] and spec["fx"], (r["name"], err)
        assert set(r["kinds"]) <= {"cutout", "photo", "glow", "mono"} and r["moods"]
    kinds = {k for r in recipes.RECIPES for k in r["kinds"]}
    assert kinds == {"cutout", "photo", "glow", "mono"}
    a, b = recipes.vary(recipes.RECIPES[0], seed=1), recipes.vary(recipes.RECIPES[0], seed=2)
    assert a["motion"][0] != b["motion"][0] and a["motion"][0]["type"] == b["motion"][0]["type"]   # 같은 계열, 숫자만 다름
    assert recipes.vary(recipes.RECIPES[0], seed=1) == a                   # 같은 seed = 같은 결과
    hit = recipes.pick("출근 완료 강렬하게", kind="glow")
    assert hit and "glow" in hit["kinds"] and recipes.family(hit) != "calm", hit["name"]
    calm = recipes.pick("잘자요 잔잔하게", kind="cutout")
    assert calm and recipes.family(calm) == "calm", calm["name"]
    other = recipes.pick("잘자요 잔잔하게", kind="cutout", exclude_family=recipes.family(calm))
    assert other and recipes.family(other) != recipes.family(calm)         # "다르게" = 계열 바꿈
    spec, err = SF.sanitize({"recipe": hit["name"], "seed": 7, "caption": "출근완료"})
    assert err is None and spec["motion"][0]["type"] == hit["motion"][0]["type"] and spec["caption"]["text"] == "출근완료"
    spec, err = SF.sanitize({"recipe": "없는레시피"})
    assert spec is None and "recipe" in err


# ── 4. 움프 = 스티커 엔진 (640 H.264 6초, 끊김 없는 반복) ────────────
def frame(data: bytes, n: int):
    from sodam.avatar import ffmpeg_path
    p = subprocess.run([ffmpeg_path(), "-loglevel", "error", "-f", "mp4", "-i", "pipe:0", "-vf", f"select=eq(n\\,{n})",
                        "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"], input=data, capture_output=True)
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


@test
async def render_mp4_meets_profile_spec_and_loops():
    spec, err = SF.sanitize({"mode": "photo", "radius": 0, "motion": [{"type": "punch", "hits": 2, "amount": 0.08}],
                             "fx": ["sweep", "sparkle", {"type": "glitch", "amp": 6}]})
    assert err is None
    res = await SF.forge_video(pattern(), spec)
    assert res.ok, res.summary()
    data = res.mp4
    assert 0 < len(data) <= SF.VIDEO_MAX_BYTES and data[4:8] == b"ftyp" and data.find(b"moov") < data.find(b"mdat")
    info = subprocess.run([__import__("sodam.avatar", fromlist=["ffmpeg_path"]).ffmpeg_path(), "-hide_banner", "-f", "mp4",
                           "-i", "pipe:0"], input=data, capture_output=True).stderr.decode(errors="replace")
    assert "h264" in info and "yuv420p" in info and "640x640" in info and "Audio" not in info, info
    assert "Duration: 00:00:05.9" in info, info                           # 89장 × 2 = 5.93초 (≤10초)
    f0 = frame(data, 0)
    loop = max(ImageStat.Stat(ImageChops.difference(f0, frame(data, 89))).mean)   # 두 번째 바퀴 첫 장 = 첫 장
    mid = max(ImageStat.Stat(ImageChops.difference(f0, frame(data, 44))).mean)
    assert loop <= max(4.0, 0.3 * mid) and mid > 3 * max(loop, 0.1), (loop, mid)
    assert res.preview and res.warnings == [], res.warnings


# ── 5. 이미 그림인 원본 판별 (art 변환 낭비 방지) ────────────────────
@test
def looks_illustrated_flags_flat_art_not_noisy_photos():
    assert SF.looks_illustrated(mascot())                                 # 단색 면 + 굵은 선
    import random
    rnd = random.Random(1)
    noisy = Image.new("RGB", (300, 300))
    noisy.putdata([(rnd.randrange(60, 200), rnd.randrange(60, 200), rnd.randrange(60, 200)) for _ in range(300 * 300)])
    assert not SF.looks_illustrated(png(noisy))                           # 실사(노이즈·연속 톤)
    grad = Image.linear_gradient("L").resize((300, 300)).convert("RGB")
    assert not SF.looks_illustrated(png(grad))                            # 연속 톤도 그림 아님


# ── 6. 속도 (한 장 8초 목표 — 여기선 상한만 느슨하게) ────────────────
@test
def render_is_fast_enough():
    spec, _ = SF.sanitize({"motion": ["idle"], "fx": ["sparkle", "sweep", "glitch"], "caption": "출근완료"})
    t0 = time.time()
    res = SF.render(mascot(), spec)
    took = time.time() - t0
    assert res.ok, res.summary()
    assert took < 25, took                                                 # 4코어 봇 서버 기준 8초 목표, 테스트 상자는 느릴 수 있음


# ── 7. AI 도구: 카탈로그 검색 · 경고 게이트 · 움프 spec 경로 · 그림엔 art 생략 ────────
from types import SimpleNamespace  # noqa: E402

from fake_llm import Room, reply, tool_call  # noqa: E402
from test_sanction_multi import A, BOSS  # noqa: E402
from sodam.agent import run_agent  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.tools import ToolCtx  # noqa: E402
from sodam.vision import Attached  # noqa: E402


async def ask(r, caller, script, image=None):
    r.llm.script = [*script, reply("짠!")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, caller, Role.MEMBER, await r.db.get_settings(r.CHAT), image=image)
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="스티커", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


@test
async def catalog_tool_gives_three_families_and_all_parts():
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    [out] = await ask(r, A, [tool_call("sticker_catalog", {"query": "출근완료 강렬하게", "kind": "glow"})])
    names = [l.split(" · ")[0][2:] for l in out.splitlines() if l.startswith("- ")]
    fams = [recipes.family(recipes.BY_NAME[n]) for n in names]
    assert 1 < len(names) <= 3 and len(set(fams)) == len(fams) and "출근_번개잽" in names, out
    assert "slice_glitch" in out and "recipe" in out and "framing" in out


@test
async def sticker_tool_holds_on_warnings_then_sends_after_fix_or_accept():
    from sodam.panels import sticker as P
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    calls = []

    async def fake_forge(image, spec, icon=False):
        calls.append(spec.get("caption", {}).get("position"))
        warn = ["자막이 피사체를 40% 덮음 → position=top"] if calls[-1] == "bottom" else []
        return SF.Result(True, b"\x1aE\xdf\xa3webm", b"", b"", [("size", "200KB / 256KB", True)], "white",
                         warnings=warn, metrics={"caption_overlap": 0.4 if warn else 0.0})
    orig, SF.forge = SF.forge, fake_forge
    try:
        img = Attached(mascot(), "image/png", BOSS.id)
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"fx": ["sparkle"], "caption": "출근완료"}})], image=img)
        assert "경고" in res[0] and "position=top" in res[0] and not r.bot.named("send_sticker"), res   # 안 보내고 고치라고
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"fx": ["sparkle"], "caption": {"text": "출근완료", "position": "top"}}})], image=img)
        assert "보냈음" in res[0] and len(r.bot.named("send_sticker")) == 1, res                       # 고친 뒤 통과
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"caption": "출근완료"}, "accept_warnings": True})], image=img)
        assert "보냈음" in res[0] and "경고 안고" in res[0] and len(r.bot.named("send_sticker")) == 2, res   # 두 번째면 그대로
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"recipe": "출근_번개잽", "seed": 5,
                                                                    "caption": {"text": "출근완료", "position": "top"}}})], image=img)
        assert "보냈음" in res[0] and "jab" in res[0], res                                                # 레시피 이름으로도
    finally:
        SF.forge = orig


@test
async def profile_video_uses_sticker_engine_spec_and_skips_art_for_drawings():
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    for u in (BOSS, A):
        await r.join(u)
    seen, arts = [], []

    async def fake_video(image, spec):
        seen.append(spec)
        return SF.Result(True, mp4=b"\x00\x00\x00\x18ftypmp4", preview=b"", rows=[("size", "1000KB / 2048KB", True)], keying="photo")

    async def image(prompt, source=None, chat_id=None):
        arts.append(prompt)
        return mascot()
    orig, SF.forge_video, r.llm.image = SF.forge_video, fake_video, image
    try:
        img = Attached(mascot(), "image/png", A.id)                                        # 그림(단색 면 + 굵은 선)
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": {"motion": [{"type": "punch", "hits": 2}], "fx": ["sweep", "glitch"]},
                                                                "art": "anime"})], image=img)
        assert "보냈음" in res[0] and "건너뜀" in res[0] and not arts, res                    # 이미 그림 → art 안 함
        assert seen and seen[0]["mode"] == "photo" and seen[0]["fx"][1]["type"] == "glitch"
        assert "스티커 엔진" in r.bot.named("send_document")[-1][3]
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": {"fx": [{"type": "rain", "image": "/etc/passwd"}]}})], image=img)
        assert "spec 오류" in res[0], res
        res = await ask(r, A, [tool_call("make_profile_video", {"motion": "sway"})], image=img)   # spec 없으면 옛 부품
        assert "보냈음" in res[0] and len(seen) == 1, res
    finally:
        SF.forge_video = orig


_ = SimpleNamespace


@test
def rays_center_cache_is_bounded():
    """rays 중심은 AI 가 고름 (0~1 실수) → 중심마다 2MB 격자를 영원히 쌓으면 메모리가 샘."""
    from PIL import Image
    from sodam.stickerforge import fx
    im = Image.new("RGBA", (fx.S, fx.S))
    for i in range(40):
        fx.rays(im, 0.1, {}, center=(0.3 + i / 1000, 0.4))
    assert fx._polar_grid.cache_info().currsize <= fx._polar_grid.cache_info().maxsize == 8


@test
def odd_seed_and_broken_image_do_not_crash():
    import sodam.stickerforge as SF
    from sodam.stickerforge import recipes
    spec, err = SF.sanitize({"recipe": recipes.RECIPES[0]["name"], "seed": "abc"})
    assert spec and not err, err
    assert SF.looks_illustrated(b"not an image") is False


@test
def sprite_cache_bounded_and_every_number_param_clamped():
    import inspect
    import sodam.stickerforge as SF
    from sodam.stickerforge import fx, motions
    for i in range(100):
        fx.sprite("star", 10 + i % 100, (i, 0, 0))
    assert fx.sprite.cache_info().currsize <= fx.sprite.cache_info().maxsize == 64
    for name, fn in [*fx.PRESETS.items(), *motions.PRESETS.items()]:      # 새 부품도 숫자 인자는 범위가 있어야 (AI 가 1e9 를 줘도)
        for p in inspect.signature(fn).parameters.values():
            if isinstance(p.default, (int, float)) and not isinstance(p.default, bool) and p.name not in ("t",):
                assert p.name in SF.CLAMP or p.name in SF.SPECIAL.get(name, {}), f"{name}.{p.name}"
