"""🧱 움프·스티커 애니메이션 프레임워크 (stickerforge/prims · sanitize · 카탈로그 · 어댑터): python tests/run_all.py animation

오너 지시 2026-09-30 '고정 목록 말고 프레임워크로' — 움프가 매번 '줌+네온+별빛'(17건 중 8건)만 나오던 것.
진짜 엔진·진짜 ffmpeg, AI 호출 없음 (가짜 LLM).
"""
import io
import json
import math
import resource
import subprocess
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from fake_llm import Room, reply, tool_call
from fakes import runner
from test_sanction_multi import A, BOSS
from test_sticker import mascot
from test_sticker_upgrade import pattern

from sodam import avatar, stickerforge as SF, stickerlearn as L
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.stickerforge import engine, examples, keying, prims
from sodam.stickerforge.const import D, MASTER, S
from sodam.tools import ToolCtx
from sodam.vision import Attached

test, run_all = runner()
TL = {"nf": 89, "fps": 30, "rep": 1, "seconds": D, "loop": True}


def ctx(seed=3, box=None):
    return {"spec": {}, "seed": seed, "hits": [], "font": SF.FONT, "prev": [], "timeline": TL, "subject_box": box}


def base_frame(color=(90, 120, 200)) -> Image.Image:
    im = Image.new("RGBA", (S, S), color + (255,))
    d = ImageDraw.Draw(im)
    for x in range(0, S, 32):
        d.line((x, 0, x, S), fill=(200, 200, 60, 255), width=4)
    return im


def cutout_frame() -> Image.Image:
    import tempfile
    p = tempfile.mktemp(suffix=".png")
    open(p, "wb").write(mascot())
    src, _ = keying.key_image(p, "auto")
    master, pivot = engine.prepare_cutout(src, 0.10)
    return engine.unpremultiply(master.transform((S, S), Image.AFFINE, engine.affine(0, 1, 1, 0, 0, pivot), resample=Image.BICUBIC))


def arr(im):
    return np.asarray(im).astype(np.int16)


def cpu() -> float:
    a, b = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


# ── 1. 값 검사: 경로·거대한 수·모르는 이름 ─────────────────────────────
@test
def sanitize_clamps_everything_and_rejects_paths_and_unknowns():
    spec, err = SF.sanitize({"loop": False, "cover": True, "motion": [
        {"type": "keyframes", "pivot": [2, -1], "keys": [{"t": 0, "rotate": 99999, "scale": 1e9, "x": float("nan"), "ease": "rm -rf"}] * 40}],
        "layers": [{"type": "particles", "shape": "smoke", "count": 99999, "size": [0, 9999], "speed": 1e12, "life": -3,
                    "colors": ["#ff8800", [300, -5, 20.7], "red", "/etc/passwd"], "image": "/etc/passwd", "blend": "multiply"},
                   {"type": "particles", "shape": "🔥", "count": 200},
                   {"type": "grade", "brightness": [-9, 9], "hue_shift": "180; ls", "tint": "#00ff00", "tint_amount": 5},
                   {"type": "transition", "kind": "burn", "embers": 5000, "start": -1, "end": 7, "to": [0, 0, 0]}]})
    assert err is None, err
    assert spec["loop"] is False and spec["cover"] is True
    kf = spec["motion"][0]
    assert len(kf["keys"]) == SF.MAX_KEYS and kf["pivot"] == (1, 0)
    k0 = kf["keys"][0]
    assert k0["rotate"] == 1440 and k0["scale"] == 4 and "x" not in k0 and "ease" not in k0       # NaN·모르는 이징은 버림
    p1, p2, g, tr = spec["layers"]
    assert p1["size"] == (2, 160) and p1["speed"] == 900 and p1["life"] == 0.1 and "image" not in p1 and "blend" not in p1
    assert p1["colors"] == [(255, 136, 0), (255, 0, 20)]                                           # 글·경로 색은 버림
    assert p2["shape"] == "flame"                                                                  # 이모지 → 비슷한 모양
    assert g["brightness"] == [-0.4, 0.4] and "hue_shift" not in g and g["tint"] == (0, 255, 0) and g["tint_amount"] == 0.8
    assert tr["start"] == 0 and tr["end"] == 1 and tr["to"] == (0, 0, 0)
    total = sum(l.get("count", 30) for l in spec["layers"] if l["type"] == "particles") + tr["embers"]
    assert total <= SF.PARTICLE_BUDGET, total                                                      # CPU 상한 (합계)
    for bad, word in (({"layers": [{"type": "particles", "shape": "/etc/passwd"}]}, "shape"),
                      ({"layers": [{"type": "rain", "image": "/etc/passwd"}]}, "layers type"),
                      ({"layers": [{"type": "exec", "code": "import os"}]}, "layers type"),
                      ({"layers": [{"type": "particles", "shape": "char", "char": "😀"}]}, "글꼴"),
                      ({"layers": [{"type": "particles", "shape": "char"}]}, "char"),
                      ({"layers": ["grade"] * 7}, "6개"),
                      ({"fx": ["sparkle"] * 3, "layers": ["grade"] * 4}, "6개"),
                      ({"layers": "grade"}, "목록"),
                      ({"motion": [{"type": "keyframes"}]}, "keys")):
        spec, err = SF.sanitize(bad)
        assert spec is None and word in err, (bad, err)
    spec, err = SF.sanitize({"layers": [{"type": "particles", "shape": "char", "char": "../../etc/passwd"}]})
    assert err is None and spec["layers"][0]["char"] == ".."                                      # 글자는 2자, PIL 로만 그림
    spec, err = SF.sanitize({"layers": [{"type": "glitch", "amp": 999, "start": 0.2, "end": 0.4}]})
    assert err is None and spec["layers"][0] == {"type": "glitch", "amp": 9, "start": 0.2, "end": 0.4}   # 옛 효과도 레이어로


# ── 2. keyframes · 이징 ──────────────────────────────────────────
@test
def easing_and_keyframes_interpolate_and_rotate_about_pivot():
    for name, e in prims.EASES.items():
        assert abs(e(0.0)) < 1e-9 and abs(e(1.0) - 1) < 1e-9, name
    assert max(prims.EASES["back"](x / 100) for x in range(101)) > 1.05                          # 살짝 넘어감
    assert prims.EASES["in"](0.5) < 0.5 < prims.EASES["out"](0.5)
    keys = [{"t": 0, "scale": 1, "ease": "linear"}, {"t": 0.5, "scale": 2, "rotate": 90}]
    f = prims.keyframes(keys, pivot=None, loop=True)
    (ang, sx, sy, dx, dy, _), op = f(0.25)
    assert abs(sx - 1.5) < 1e-6 and abs(ang - 45) < 1e-6 and op == 1                              # 선형 중간값
    assert f(0.0) == f(0.9999999) or abs(f(0.0)[0][1] - f(0.9999999)[0][1]) < 1e-3              # 반복: 끝 = 처음
    once = prims.keyframes(keys, loop=False)
    assert abs(once(1.0)[0][1] - 2) < 1e-9                                                         # 한 번: 마지막 값 유지
    # pivot 을 축으로 돌면 pivot 점은 제자리 (엔진 forward: X' = A(X - c) + c + d)
    c = (MASTER / 2, MASTER * 0.9)
    piv = (0.2, 0.3)
    g = prims.keyframes([{"t": 0, "rotate": 0}, {"t": 0.5, "rotate": 70, "scale": 1.4}], pivot=piv, engine_pivot=c)
    (ang, sx, sy, dx, dy, _), _ = g(0.5)
    a = math.radians(ang)
    px, py = piv[0] * MASTER, piv[1] * MASTER
    vx, vy = px - c[0], py - c[1]
    x2 = math.cos(a) * sx * vx - math.sin(a) * sy * vy + c[0] + dx
    y2 = math.sin(a) * sx * vx + math.cos(a) * sy * vy + c[1] + dy
    assert abs(x2 - px) < 1e-6 and abs(y2 - py) < 1e-6, (x2, y2, px, py)
    op = prims.keyframes([{"t": 0, "opacity": 1}, {"t": 1, "opacity": 0, "ease": "linear"}], loop=False)(0.5)[1]
    assert abs(op - 0.5) < 0.01


# ── 3. 부품마다 올바른 프레임 + 반복 이음새 ─────────────────────────
VARIANTS = {
    "particles": [dict(shape=s, count=12, spawn=sp, size=[10, 30], speed=120) for s, sp in
                  zip(prims.SHAPES, ["top", "bottom", "left", "right", "edges", "center", "point", "area", "around", "subject"] * 3)
                  if s != "char"] + [dict(shape="char", char="돈", count=8, blend="add", blur=2)],
    "grade": [dict(brightness=[-0.1, 0.1], contrast=1.3, saturation=0, hue_spin=1, tint=(255, 0, 0), tint_amount=0.3,
                   vignette=0.6, grain=0.1, bloom=0.8), dict(hue_shift=[-40, 40], cycles=2)],
    "flash": [dict(count=3, strength=0.7)],
    "lightning": [dict(count=2, branches=3)],
    "transition": [dict(kind=k) for k in prims.TRANSITIONS],
}


@test
def every_primitive_renders_valid_rgba_and_full_window_ones_loop():
    base = base_frame()
    for name, variants in VARIANTS.items():
        assert name in prims.LAYERS
        for p in variants:
            for u in (0.0, 0.3, 0.55, 0.99):
                out = prims.LAYERS[name](base.copy(), u, ctx(box=(100, 100, 400, 420)), **p)
                assert out.size == (S, S) and out.mode == "RGBA", (name, p)
            if name in ("particles", "grade"):                                       # 전체 창이면 끝 다음 장 = 첫 장
                a0 = arr(prims.LAYERS[name](base.copy(), 0.0, ctx(), **p))
                a1 = arr(prims.LAYERS[name](base.copy(), 1.0, ctx(), **p))
                assert np.abs(a0 - a1).mean() < 0.6, (name, p)
    moving = prims.particles(base.copy(), 0.3, ctx(), shape="heart", count=30)
    assert np.abs(arr(moving) - arr(base)).mean() > 0.5                                      # 실제로 그려짐
    same = prims.grade(base.copy(), 0.3, ctx())
    assert np.abs(arr(same) - arr(base)).max() <= 1                                          # 기본값 = 그대로
    red = prims.grade(base.copy(), 0.3, ctx(), tint=(255, 0, 0), tint_amount=0.8)
    assert arr(red)[..., 0].mean() > arr(base)[..., 0].mean() + 60


# ── 4. 전환: 끝나면 투명(또는 지정 색) · 나타나기 · 타는 가장자리 ───────────
@test
def transitions_end_as_specified_and_burn_has_hot_edge():
    base = base_frame()
    for kind in prims.TRANSITIONS:
        gone = prims.transition(base.copy(), 0.95, ctx(), kind=kind, start=0.1, end=0.9)
        assert np.asarray(gone.getchannel("A")).max() == 0, kind                            # 다 사라짐 (스티커 = 투명)
        start = prims.transition(base.copy(), 0.05, ctx(), kind=kind, start=0.1, end=0.9)
        assert np.abs(arr(start) - arr(base)).max() == 0, kind                               # 시작 전 = 그대로
        back = prims.transition(base.copy(), 0.95, ctx(), kind=kind, direction="in", start=0.1, end=0.9)
        assert np.abs(arr(back) - arr(base)).max() == 0, kind                                # 나타나기 끝 = 원래
        early = prims.transition(base.copy(), 0.05, ctx(), kind=kind, direction="in", start=0.1, end=0.9)
        assert np.asarray(early.getchannel("A")).max() == 0, kind
    black = prims.transition(base.copy(), 0.95, ctx(), kind="burn", to=(0, 0, 0))
    a = arr(black)
    assert a[..., 3].min() == 255 and a[..., :3].max() == 0                                  # 움프: 검정이 남음
    mid = prims.transition(base.copy(), 0.5, ctx(), kind="burn", start=0.1, end=0.9, edge=0.05, embers=40)
    m = arr(mid)
    alpha = m[..., 3]
    assert 0.2 < (alpha == 0).mean() < 0.8, (alpha == 0).mean()                            # 절반쯤 탐 (순위 노이즈 = 면적 비율)
    hot = (m[..., 0] > 220) & (m[..., 1] > 110) & (m[..., 2] < 200) & (alpha > 0)
    assert hot.sum() > 500, hot.sum()                                                        # 주황 불 가장자리
    diss = prims.transition(base.copy(), 0.5, ctx(), kind="dissolve", start=0.0, end=1.0)
    assert abs((np.asarray(diss.getchannel("A")) == 0).mean() - 0.5) < 0.08               # 진행률 = 사라진 면적
    n1, n2 = prims.noise(1, 24), prims.noise(2, 24)
    assert n1.shape == (S, S) and abs(n1.mean() - 0.5) < 0.01 and np.abs(n1 - n2).mean() > 0.1   # seed 마다 다른 무늬


# ── 5. 입자 방향·중력 ───────────────────────────────────────────
def centroid(im):
    a = np.asarray(im.getchannel("A")).astype(float)
    ys, xs = np.nonzero(a)
    return xs.mean(), ys.mean()


@test
def particle_emitter_goes_where_the_angle_says():
    empty = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    common = dict(shape="circle", count=20, spawn="point", at=(0.5, 0.5), spread=0.0, speed=200, angle_spread=0,
                  life=2.5, burst=True, start=0.0, end=1.0, fade="none", size=10)
    for angle, axis, sign in ((-90, 1, -1), (90, 1, 1), (0, 0, 1), (180, 0, -1)):
        c1 = centroid(prims.particles(empty.copy(), 0.1, ctx(), angle=angle, **common))
        c2 = centroid(prims.particles(empty.copy(), 0.3, ctx(), angle=angle, **common))
        assert (c2[axis] - c1[axis]) * sign > 30, (angle, c1, c2)
    up = dict(common, angle=-90, gravity=600)
    y1 = centroid(prims.particles(empty.copy(), 0.1, ctx(), **up))[1]
    y2 = centroid(prims.particles(empty.copy(), 0.45, ctx(), **up))[1]
    assert y2 > y1, (y1, y2)                                                                  # 위로 쏴도 중력이면 떨어짐
    wind = centroid(prims.particles(empty.copy(), 0.4, ctx(), **dict(common, angle=-90, wind=400)))[0]
    assert wind > S / 2 + 20                                                                   # 바람 = 옆으로
    smoke = prims.particles(empty.copy(), 0.5, ctx(), shape="smoke", count=10, spawn="point", at=(0.5, 0.8), angle=-90,
                            speed=40, grow=3, blur=3)
    al = np.asarray(smoke.getchannel("A"))
    assert al.max() > 20 and (al > 0).mean() > 0.01 and al.max() < 255                      # 부드럽게 흐린 연기


# ── 6. 옛 인자 어댑터 ───────────────────────────────────────────
@test
def old_tool_args_map_onto_the_one_engine():
    for m in avatar.MOTIONS:
        for c in avatar.COLORS:
            for p in avatar.PARTICLES:
                for sp in avatar.SPEEDS:
                    spec, err = SF.sanitize(avatar.to_forge_spec(avatar.Spec(m, sp, c, p)))
                    assert err is None and spec["mode"] == "photo" and spec["layers"] is not None, (m, c, p, err)
    s = avatar.Spec("still", "slow", "none", "smoke")                                          # 실제 실패 사례 → 이제 됨
    assert s.check() is None
    spec, _ = SF.sanitize(avatar.to_forge_spec(s))
    assert spec["layers"][0]["shape"] == "smoke"
    err = avatar.Spec("teleport").check()
    assert "spec" in err and "sticker_catalog" in err                                          # 모르는 옛 이름 → 부품으로 안내
    assert avatar.Spec("breathe", "fast", "shine", "hearts").label() == "🌬️ 숨쉬기 · ✨ 반짝임 · 💕 하트 (빠르게)"
    # 옛 zoom·breathe·pan·sway 는 키마다 값을 다 적어야 오르내림 (예전: 1.0→1.2 한 번 커지고 멈춤 → 이음새에서 튐)
    for m, prop in (("zoom", 1), ("breathe", 1), ("pan", 3), ("sway", 0)):
        for sp, (_, k) in avatar.SPEEDS.items():
            spec, _ = SF.sanitize(avatar.to_forge_spec(avatar.Spec(m, sp)))
            kf = spec["motion"][0]
            f = prims.keyframes(kf["keys"], kf.get("pivot"), True)
            vals = [f(i / 240)[0][prop] for i in range(241)]
            assert abs(vals[0] - vals[-1]) < 1e-6, (m, sp, vals[0], vals[-1])                 # 끝 = 처음 (반복 이음새 없음)
            lo, hi = min(vals), max(vals)
            assert hi - lo > 0.01, (m, sp)
            peaks = sum(1 for a, b, c in zip(vals, vals[1:], vals[2:]) if b > a and b >= c and b > lo + 0.9 * (hi - lo))
            assert peaks == k, (m, sp, peaks)                                                    # 빠르기 = 오르내림 횟수


# ── 7. 카탈로그·도구 설명 ────────────────────────────────────────
@test
def tool_descriptions_are_short_and_catalog_fits_and_lists_every_primitive():
    from sodam import tools
    from sodam.panels import sticker as P
    mpv, stk = tools._BY_NAME["make_profile_video"], tools._BY_NAME["make_sticker"]
    for t in (mpv, stk):
        assert len(t.description) < 900 and "sticker_catalog" in t.description and "seed" in t.description, len(t.description)
        assert "wanted" in t.description
    assert not {"motion", "color", "particles", "speed"} & set(mpv.params)                     # 옛 고정 목록은 설명에서 뺌
    for q in ("상품권이 다 타서 없어지게", "수건으로 쌍절곤 휘둘러", "담배 피는 곰", "더 화려하고 멋지게", "", "x" * 300):
        txt = P.catalog_text(q)
        assert len(txt) <= 3950, (q, len(txt))                                                 # 도구 결과 4000자에서 안 잘림
        for name in [*prims.LAYERS, "keyframes", *prims.SHAPES, *prims.TRANSITIONS]:
            assert name in txt, name
    assert "쌍절곤" in P.catalog_text("수건으로 쌍절곤 휘둘러").split("예시 (")[1].splitlines()[1]                   # 닮은 예시가 먼저
    assert "타서" in P.catalog_text("상품권 타서 없어지게").split("예시 (")[1].splitlines()[1]
    for sec in ("examples", "effects", "recipes"):
        assert len(P.catalog_text("화려하게", section=sec)) <= 3950, sec
    assert all(e["request"] in P.catalog_text("", section="examples") for e in examples.EXAMPLES[:6])
    reqs = {e["request"] for e in examples.EXAMPLES}
    for must in ("수건으로 쌍절곤 휘둘러", "상품권이 다 타서 없어지게", "더 화려하고 멋지게", "잔잔하게", "글리치 느낌", "네온"):
        assert must in reqs, must


@test
def every_worked_example_sanitizes_and_draws():
    img = Image.open(io.BytesIO(pattern())).convert("RGBA")
    sigs = set()
    for e in examples.EXAMPLES:
        spec, err = SF.sanitize({"mode": "photo", "radius": 0, **e["spec"]})
        assert err is None, (e["request"], err)
        sigs.add(L.signature(spec))
        tl = {**engine.timeline(spec, "ump"), "nf": 12}
        built = engine.build(img, spec, None, SF.FONT, tl=tl)
        assert len(built["frames"]) == 12 and all(f.size == (S, S) for f in built["frames"]), e["request"]
    assert len(sigs) == len(examples.EXAMPLES)                                                # 예시끼리도 같은 조합 없음


# ── 8. 렌더 시간 상한 (2vCPU 서버: 무거운 조합 한 편 ~20초 CPU) ────────────
HEAVY = {"mode": "photo", "radius": 0, "loop": False,
         "motion": [{"type": "keyframes", "keys": [{"t": 0, "scale": 1.0}, {"t": 0.5, "scale": 1.1, "rotate": 3}, {"t": 1, "scale": 1.0}]}],
         "layers": [{"type": "particles", "shape": "spark", "count": 200, "spawn": "bottom", "angle": -90, "speed": 160, "blend": "add",
                     "size": [6, 14]},
                    {"type": "transition", "kind": "burn", "start": 0.2, "end": 0.85, "embers": 60},
                    {"type": "grade", "bloom": 0.6, "saturation": 1.3, "contrast": 1.1}]}


@test
async def heavy_once_spec_renders_to_profile_spec_within_cpu_bound():
    spec, err = SF.sanitize(HEAVY)
    assert err is None
    c0, w0 = cpu(), time.time()
    res = await SF.forge_video(pattern(), spec)
    used = cpu() - c0
    print(f"    무거운 움프(입자 200 + 불씨 60 + burn + bloom, 6초 {engine.ONCE_FPS}fps): CPU {used:.1f}초 · 벽시계 {time.time() - w0:.1f}초 · "
          f"{len(res.mp4) // 1024}KB")
    assert res.ok, res.summary()
    assert used < 45, used                    # 이 상자 실측 ~18초. 몇 배로 느려진 회귀만 잡음 (서버 부하·Nice 여유)
    info = subprocess.run([avatar.ffmpeg_path(), "-hide_banner", "-f", "mp4", "-i", "pipe:0"], input=res.mp4,
                          capture_output=True).stderr.decode(errors="replace")
    assert "h264" in info and "640x640" in info and "yuv420p" in info and "Audio" not in info, info
    assert "Duration: 00:00:06.0" in info or "Duration: 00:00:05.9" in info, info           # 한 번 6초
    assert len(res.mp4) <= SF.VIDEO_MAX_BYTES and res.mp4.find(b"moov") < res.mp4.find(b"mdat")
    assert engine.timeline(spec, "ump")["nf"] == int(engine.ONCE_SECONDS * engine.ONCE_FPS)
    assert engine.timeline(spec, "sticker")["nf"] == 89                                       # 스티커는 언제나 2.97초 반복


# ── 9. qc: 너무 어두움 · 스티커 규격 그대로 ─────────────────────────────
@test
def qc_warns_when_mostly_dark_and_stickers_with_new_layers_still_pass_telegram_rules():
    spec, _ = SF.sanitize({"mode": "photo", "radius": 8, "layers": [{"type": "transition", "kind": "fade", "start": 0.0, "end": 0.2}]})
    img = Image.open(io.BytesIO(pattern())).convert("RGBA")
    built = engine.build(img, spec, None, SF.FONT)
    warns, metrics = SF._inspect(built, "photo", None, spec)
    assert metrics["dark_ratio"] > 0.5 and any("까맣" in w for w in warns), (metrics, warns)
    spec, err = SF.sanitize({"motion": [{"type": "keyframes", "pivot": [0.5, 0.95], "keys": [{"t": 0, "rotate": -20}, {"t": 0.5, "rotate": 20}]}],
                             "layers": [{"type": "particles", "shape": "heart", "spawn": "around", "count": 20, "speed": 40},
                                        {"type": "transition", "kind": "dissolve", "start": 0.7, "end": 0.95}],
                             "margin": 0.14})
    assert err is None
    res = SF.render(mascot(), spec)
    rows = {n: ok for n, v, ok in res.rows}
    assert res.ok and all(rows.values()) and len(res.webm) <= 256 * 1024, res.summary()


# ── 10. 도구: 끝까지 (가짜 봇) · 같은 조합 반복 막기 · seed 새로 ─────────────────
async def ask(r, caller, script, image=None):
    r.llm.script = [*script, reply("짠!")]
    c = ToolCtx(r.svc, r.bot, r.CHAT, caller, Role.MEMBER, await r.db.get_settings(r.CHAT), image=image)
    await run_agent(c, style_key="polite", notes={}, history=[], reply_to=None, request="움프", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


async def room():
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    for u in (BOSS, A):
        await r.join(u)
    r.bot.get_user_profile_photos = lambda *a, **k: _none()
    return r


async def _none():
    return SimpleNamespace(photos=[])


@test
async def make_profile_video_end_to_end_burn_away_is_a_real_mp4():
    r = await room()
    img = Attached(pattern(), "image/png", A.id)
    burn = next(e for e in examples.EXAMPLES if "타서" in e["request"])
    res = await ask(r, A, [tool_call("make_profile_video", {"spec": burn["spec"], "request": burn["request"], "accept_warnings": True})],
                    image=img)
    assert "보냈음" in res[0], res
    [doc] = r.bot.named("send_document")
    assert "transition burn" in doc[3] and "particles smoke" in doc[3], doc[3]
    data = doc[2].input_file_content if hasattr(doc[2], "input_file_content") else None
    row = await r.db._one("SELECT spec, request, product FROM sticker_log ORDER BY id DESC LIMIT 1")
    spec = json.loads(row["spec"])
    assert row["product"] == "ump" and spec["loop"] is False and spec["layers"][1]["kind"] == "burn"
    _ = data


@test
async def same_combo_for_a_different_request_is_sent_back_and_seed_is_fresh():
    r = await room()
    img = Attached(pattern(), "image/png", A.id)
    seen = []

    async def fake_video(image, spec):
        seen.append(spec)
        return SF.Result(True, mp4=b"\x00\x00\x00\x18ftypmp4", rows=[("size", "1KB", True)], keying="photo")
    from sodam.panels import avatar as PA
    orig, SF.forge_video, limit, PA.FREE_DAILY = SF.forge_video, fake_video, PA.FREE_DAILY, 50   # 하루 한도는 다른 테스트가 봄
    try:
        neon = {"motion": [{"type": "zoom"}], "layers": [{"type": "grade", "bloom": 0.5}, {"type": "particles", "shape": "sparkle"}]}
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": neon, "request": "네온 느낌"})], image=img)
        assert "보냈음" in res[0], res
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": neon, "request": "쌍절곤 휘둘러"})], image=img)
        assert "똑같은 조합" in res[0] and "쌍절곤" in res[0] and len(seen) == 1, res           # 다른 말인데 같은 조합 → 다시 짜라
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": {**neon, "layers": [{"type": "particles", "shape": "spark"}]},
                                                                "request": "쌍절곤 휘둘러"})], image=img)
        assert "보냈음" in res[0] and len(seen) == 2, res                                       # 모양을 바꾸면 됨
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": {**neon, "layers": [{"type": "particles", "shape": "spark"}]},
                                                                "request": "한 번 더", "accept_warnings": True})], image=img)
        assert "보냈음" in res[0] and len(seen) == 3, res                                       # 정말 같게 원하면 됨
        warm = {"motion": [{"type": "keyframes", "keys": [{"t": 0, "scale": 1}, {"t": 0.5, "scale": 1.08}]}],
                "layers": [{"type": "grade", "tint": [255, 170, 90], "tint_amount": 0.15}]}
        mono = {"motion": [{"type": "keyframes", "keys": [{"t": 0, "scale": 1}, {"t": 0.5, "scale": 1.2}]}],
                "layers": [{"type": "grade", "saturation": 0}]}
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": warm, "request": "따뜻하게 숨쉬기"})], image=img)
        assert "보냈음" in res[0], res
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": mono, "request": "흑백으로 줌"})], image=img)
        assert "보냈음" in res[0] and len(seen) == 5, res                                       # 부품 이름만 같고 값이 다르면 다른 연출
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": mono, "request": "다른 느낌으로"})], image=img)
        assert "똑같은 조합" in res[0] and len(seen) == 5, res                                  # 값까지 같으면 (seed 만 달라도) 막음
        assert len({s["seed"] for s in seen}) >= 2                                               # seed 안 주면 매번 새로
        res = await ask(r, A, [tool_call("make_profile_video", {"spec": neon, "seed": 42, "request": "눈 오게"})], image=img)
        assert "보냈음" in res[0] and seen[-1]["seed"] == 42, res                              # seed 를 주면 그대로
        res = await ask(r, A, [tool_call("make_profile_video", {"particles": "smoke", "request": "연기"})], image=img)
        assert "보냈음" in res[0] and seen[-1]["layers"][-1]["shape"] == "smoke", res          # 옛 인자도 그대로 (smoke 도)
        res = await ask(r, A, [tool_call("make_profile_video", {"motion": "x; rm -rf /"})], image=img)
        assert "spec" in res[0] and "sticker_catalog" in res[0], res
    finally:
        SF.forge_video, PA.FREE_DAILY = orig, limit
