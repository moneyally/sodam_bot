"""🧩 부품 자동 검사 (모션·효과·자막 애니 전부): 끊김 없는 반복 + sanitize 통과 + 진짜 렌더 규격. python tests/run_all.py sticker_parts

새 부품을 PRESETS/ANIMS 에 넣으면 등록 없이 여기서 자동으로 검사된다.
"""
import io

import numpy as np
from PIL import Image

from fakes import runner
from test_sticker import mascot

from sodam import stickerforge as SF
from sodam.stickerforge import caption as CAP, engine, fx as FX, keying, motions as M
from sodam.stickerforge.const import D, FPS, NF

test, run_all = runner()
FONT = SF.FONT


def _frame():
    src, _ = keying.key_image(_save(mascot()), "auto")
    master, pivot = engine.prepare_cutout(src, 0.10)
    return engine.unpremultiply(master.transform((512, 512), Image.AFFINE, engine.affine(0, 1, 1, 0, 0, pivot), resample=Image.BICUBIC))


def _save(b: bytes) -> str:
    import tempfile
    p = tempfile.mktemp(suffix=".png")
    open(p, "wb").write(b)
    return p


def _ctx(hits=()):
    return {"spec": {}, "seed": 3, "hits": list(hits), "font": FONT, "prev": []}


def _off(v):
    """화면 밖 (등장/퇴장 모션의 양끝)?"""
    ang, sx, sy, dx, dy = v[:5]
    return abs(dx) > 900 or abs(dy) > 380 or abs(sx) < 0.2 or abs(sy) < 0.2


@test
def every_motion_loops_and_counts():
    assert len(M.PRESETS) >= 25, len(M.PRESETS)
    for name in M.PRESETS:
        f = M.build([name])
        a, b = f(0.0), f(D)
        dang = abs(a[0] - b[0]) % 360
        same = min(dang, 360 - dang) < 0.05 and all(abs(x - y) < 0.05 for x, y in zip(a[1:], b[1:]))   # 각도는 360° 단위
        assert same or (_off(a) and _off(b)), (name, a, b)                    # 첫 장 = 끝 다음 장, 또는 양끝 화면 밖
        for t in (0.0, 0.5, 1.0, 1.7, 2.5, D):
            v = f(t)
            assert len(v) == 6 and all(np.isfinite(v)), (name, t, v)
            assert 0.03 <= abs(v[1]) <= 2.5 and 0.03 <= abs(v[2]) <= 2.5 and abs(v[5]) < 1.2, (name, t, v)   # 크기·기울임 한도
        spec, err = SF.sanitize({"motion": [name]})
        assert err is None and spec["motion"][0]["type"] == name, (name, err)
        doc = ({"breathe": M.idle, "float": M.float_}.get(name, M.PRESETS[name]).__doc__ or "").strip()
        assert doc, name                                                     # 카탈로그 한 줄 설명


@test
def every_fx_loops_and_keeps_alpha_sane():
    assert len(FX.PRESETS) - len(SF.BLOCKED_FX) >= 35, len(FX.PRESETS)
    base = _frame()
    for name, fn in FX.PRESETS.items():
        if name in SF.BLOCKED_FX:
            continue
        assert (fn.__doc__ or "").strip(), name
        for hits in ((), (0.3, 1.4)):
            ctx = _ctx(hits)
            a = fn(base.copy(), 0.0, ctx)
            mid = fn(base.copy(), 1.1, ctx)
            z = fn(base.copy(), D, ctx)
            assert a.size == (512, 512) and a.mode == "RGBA" and mid.size == (512, 512), name
            diff = np.abs(np.asarray(a).astype(int) - np.asarray(z).astype(int)).mean()
            assert diff < 0.6, (name, hits, diff)                             # 첫 장 = 끝 다음 장
            al = np.asarray(mid.getchannel("A"))
            assert al.max() >= 100 and al.min() == 0, name                    # 알파가 살아 있음 (flicker 는 낮아질 수 있음)
        spec, err = SF.sanitize({"fx": [name]})
        assert err is None and spec["fx"][0]["type"] == name, (name, err)


@test
def every_caption_anim_renders_and_ends_readable():
    assert len(CAP.ANIMS) >= 12
    for a in CAP.ANIMS:
        frames, info = CAP.render("출근완료", FONT, CAP.Style(anims=(a,)))
        assert len(frames) == NF and frames[-1].getbbox(), a               # 마지막 장엔 글자가 다 있음
        bb = frames[-1].getbbox()
        assert bb[0] >= 2 and bb[2] <= 510 and bb[3] <= 510, (a, bb)        # 가장자리에 안 닿음
        assert sum(1 for f in frames if f.getbbox()) >= NF // 2, a          # 절반 이상 보임
    spec, _ = SF.sanitize({"caption": {"text": "안녕", "anims": ["pop", "drop", "wave", "rainbow", "glow", "neon"]}})
    assert spec["caption"]["anims"] == ["pop", "wave", "rainbow", "glow"]    # 등장은 하나, 최대 4


@test
def catalog_comes_from_code_and_text_params_are_bounded():
    cat = SF.catalog()
    assert set(cat["motions"]) == set(M.PRESETS) and set(cat["fx"]) == set(FX.PRESETS) - SF.BLOCKED_FX
    assert cat["fx"]["laser"]["params"]["angle"] == 20 and "레이저" in cat["fx"]["laser"]["doc"]
    assert cat["caption_anims"] == CAP.ANIMS
    spec, err = SF.sanitize({"fx": [{"type": "sfx_text", "text": "쾅!!!!!!!!!!!!!", "at": [999, -3]},
                                    {"type": "laser", "points": [[1, 2], [3, 4], [5, 6], [7, 8], [9, 9]]},
                                    {"type": "bubble", "text": "../../etc"}, {"type": "confetti", "count": 9999}],
                             "motion": [{"type": "flip", "axis": "z"}]})
    assert err is None
    fx = {f["type"]: f for f in spec["fx"]}
    assert fx["sfx_text"]["text"] == "쾅!!!!!!!" and fx["sfx_text"]["at"] == (512, 0)
    assert "points" not in fx["laser"] and fx["confetti"]["count"] == 120       # 점 5개는 거절(기본값), count 상한
    assert fx["bubble"]["text"] == "../../et" and spec["motion"][0]["axis"] == "y"    # 글은 8자, PIL 로만 그림; axis 는 x|y


@test
async def new_parts_render_within_telegram_limits():
    img = mascot()
    for raw in ({"motion": [{"type": "jello"}, {"type": "heart_beat"}], "fx": ["speedlines", "confetti", "neon_edge", "sfx_text"],
                 "caption": {"text": "쾅!", "anims": ["stamp", "shake"]}},
                {"mode": "photo", "motion": ["kenburns"], "fx": ["deep_fry", "focus_lines", "scanlines", "strobe"],
                 "caption": {"text": "출근완료", "anims": ["drop", "rainbow", "neon"]}},
                {"motion": [{"type": "light_speed"}], "fx": ["money", "laser", "rainbow_outline", "wave"],
                 "caption": {"text": "돈비", "anims": ["spin", "karaoke", "pulse"]}}):
        spec, err = SF.sanitize(raw)
        assert err is None, err
        res = await SF.forge(img, spec)
        assert res.ok, (raw, res.summary(), res.warnings)
        assert len(res.webm) <= 256 * 1024
