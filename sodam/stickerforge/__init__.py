"""🧩 스티커 공방 (telegram-sticker-forge 엔진, 스킬 `.claude/skills/telegram-sticker-forge/`).

사진 한 장 + 말로 한 요청 → 텔레그램 영상 스티커(512×512 VP9 WebM, 투명, 2.97초, 256KB↓) + 선택 100×100 팩 아이콘.
같은 엔진으로 움프(움직이는 프로필 640 H.264 6초) 도 만든다 (forge_video).
엔진(const·keying·motions·fx·caption·engine·verify)은 받은 그대로 두고, 소담 쪽에서 바꾼 것만:
- ffmpeg = imageio-ffmpeg 정적 바이너리(libvpx-vp9 포함), ffprobe 대신 `ffmpeg -i` 로 검사 (const.ffmpeg · verify._probe).
- **AI 가 준 spec 은 sanitize 로 다시 만듦**: 움직임·효과 이름은 목록만, 값은 그 함수가 받는 인자 이름 + 숫자/참거짓/숫자 목록만,
  범위는 SKILL.md 의 '화면이 하얗게 날아가지 않는' 한도로 자름. 파일 경로를 받는 rain/rise(image=)·font 는 막음 (서버 파일 읽기 X).
  `recipe` 이름 + seed 를 주면 recipes.vary 로 검증된 조합을 변주한 뒤 같은 검사를 거침.
- CPU 가 많이 드는 일(89장 그리기 + VP9 2-pass)이라 한 번에 하나(_LOCK), to_thread, 검사 표가 통과해야 ok.
- 소담이는 결과를 못 보므로 qc 가 코드로 잰 경고(warnings)·지표(metrics) 를 Result 에 담는다 → 한 번 고쳐 다시 만들 수 있음.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "..", "data_files", "fonts", "BlackHanSans.ttf")   # OFL, 굵은 한글 제목체
MAX_MOTIONS, MAX_FX, MAX_CAPTION = 2, 4, 12
CAPTION_NUM = {"stroke": (0, 16), "depth": (0, 14), "size_max": (36, 120)}
STATIC_MAX_BYTES = 512 * 1024          # 정지 스티커 WEBP (Bot API: 한 변 512, 512KB↓)
BLOCKED_FX = {"rain", "rise"}          # image= 로 파일 경로를 받음
VIDEO_MAX_BYTES = 2 * 1024 * 1024      # 움프 2MB
CLAMP = {                              # (최소, 최대) — 넘으면 잘라서 씀
    "amp_rot": (0, 8), "amp_bob": (0, 40), "breathe": (0, 0.05), "cycles": (1, 4), "hits": (1, 5), "amount": (0, 80),
    "amp": (0, 12), "freq": (1, 12), "jumps": (1, 3), "height": (0, 420), "squash": (0, 0.3), "drops": (1, 2),
    "kick": (0, 40), "lunge": (0, 0.2), "strength": (0, 0.8), "width": (0, 12), "radius": (0, 30), "flare": (0, 0.35),
    "flicker": (0, 0.15), "bursts": (0, 5), "k": (1, 20), "bands": (1, 12), "shift": (0, 40), "split": (0, 6),
    "spokes": (4, 24), "count": (1, 120), "size": (10, 140), "r0": (0, 300), "r1": (50, 400), "life": (0.05, 0.6),
    "sway": (0, 20), "hold": (0.2, 0.7), "direction": (-1, 1), "tilt": (-4, 4), "shake_hz": (5, 30),
    "height_px": (20, 140), "alpha": (0, 0.8), "tau": (0.03, 0.4), "pulse": (0, 1), "phase": (0, 6.3),
    # 추가 부품
    "bounces": (1, 4), "beats": (1, 3), "wiggle": (0, 6), "turns": (1, 2), "depth": (0, 0.7), "drift": (0, 80), "waves": (1, 4),
    "block": (4, 24), "gap": (2, 6), "dark": (0, 0.6), "rate": (1, 8), "spread": (10, 120), "flashes": (1, 4), "gain": (1, 1.8),
    "dx": (-30, 30), "dy": (-30, 30), "blur": (2, 16), "opacity": (0, 0.8), "length": (60, 600), "angle": (-180, 180),
}
TEXT_KEYS = {"text": 8, "axis": 1}            # PIL 로만 그리는 짧은 글 (ffmpeg 인자·경로 아님). axis: x|y
POINT_KEYS = {"points": 4}                    # [[x,y],...] 512 좌표
SPECIAL = {"impact": {"frames": (1, 4)}, "flashbang": {"amount": (0, 60)}, "glitch": {"amp": (0, 9)}, "punch": {"amount": (0, 0.12)},
           "zoom": {"amount": (0, 0.12)}, "pan": {"amount": (0, 60)}, "scan": {"height": (20, 140)},
           "kenburns": {"amount": (0, 0.2)}, "rubber_band": {"amount": (0, 0.4)}, "jello": {"amount": (0, 25)},
           "heart_beat": {"amount": (0, 0.3)}, "tada": {"amount": (0, 0.2)}, "light_speed": {"amount": (0, 40)},
           "deep_fry": {"amount": (0, 1.5)}, "swing": {"amp": (0, 30)}, "sway": {"amp": (0, 40)}, "head_shake": {"amp": (0, 24)},
           "bounce_in": {"height": (40, 260)}, "hop": {"height": (40, 200)}, "slam": {"height": (200, 520)},
           "wave": {"amp": (0, 16)}, "laser": {"length": (100, 600)}, "bolts": {"count": (1, 10)}}


@dataclass
class Result:
    ok: bool
    webm: bytes = b""
    icon: bytes = b""
    preview: bytes = b""
    rows: list = field(default_factory=list)
    keying: str = ""
    error: str = ""
    warnings: list = field(default_factory=list)   # qc 가 코드로 잰 경고 (한국어 한 줄씩, 고칠 방향 포함)
    metrics: dict = field(default_factory=dict)    # qc 지표 숫자
    mp4: bytes = b""                                # forge_video 결과 (움프)
    spec: dict = field(default_factory=dict)        # 실제로 쓴 spec
    still: bytes = b""                              # forge_static 결과 (정지 스티커 WEBP)

    def summary(self) -> str:
        bad = [f"{n} {v}" for n, v, ok in self.rows if not ok]
        return "통과" if self.ok else ("검사 실패: " + ", ".join(bad) if bad else self.error or "실패")


def _int(v, default: int) -> int:
    return int(v) if _num(v) else default


def _num(v):
    import math
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clean_params(kind: str, fn, raw: dict) -> dict:
    allowed = {p for p in inspect.signature(fn).parameters if p not in ("frame", "t", "ctx", "_", "image")}
    out = {}
    for k, v in (raw or {}).items():
        if k not in allowed:
            continue
        lo_hi = SPECIAL.get(kind, {}).get(k) or CLAMP.get(k)
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str) and k in TEXT_KEYS:                # 짧은 글자 (PIL 에서만 그림)
            v = " ".join(v.split())[:TEXT_KEYS[k]]
            if k == "axis":
                v = v if v in ("x", "y") else "y"
            if v:
                out[k] = v
        elif k in POINT_KEYS and isinstance(v, (list, tuple)) and 1 <= len(v) <= POINT_KEYS[k] \
                and all(isinstance(p, (list, tuple)) and len(p) == 2 and all(_num(x) for x in p) for p in v):
            out[k] = [(int(min(max(p[0], 0), 512)), int(min(max(p[1], 0), 512))) for p in v]
        elif _num(v):
            out[k] = min(max(v, lo_hi[0]), lo_hi[1]) if lo_hi else v
            if isinstance(v, int) and k in ("hits", "cycles", "jumps", "drops", "bursts", "count", "bands", "spokes", "bounces",
                                            "beats", "turns", "flashes", "block", "gap"):
                out[k] = int(out[k])
        elif isinstance(v, (list, tuple)) and len(v) <= 12 and all(_num(x) for x in v):
            if k == "subset":
                out[k] = [int(x) for x in v if 0 <= x <= 9]
            elif k == "color" and len(v) == 3:                      # PIL 은 정수 색만
                out[k] = tuple(int(min(max(x, 0), 255)) for x in v)
            elif k in ("center", "at", "origin") and len(v) == 2:  # 512 캔버스 좌표 (center 는 0~1 비율도 받음)
                out[k] = tuple(x if isinstance(x, float) and x <= 1 else int(min(max(x, 0), 512)) for x in v)
            elif k == "times":
                out[k] = [float(min(max(x, 0), 2.3)) for x in v][:6]   # 충격은 끝나기 0.6초 전까지 (반복 이음새)
        elif isinstance(v, (list, tuple)) and len(v) <= 6 and all(isinstance(x, (list, tuple)) for x in v):
            continue   # meteors paths 같은 중첩 목록은 기본값으로
    return out


def sanitize(raw: dict) -> tuple[dict | None, str | None]:
    """AI 가 준 spec → 엔진에 넣어도 되는 spec (또는 이유).
    raw["recipe"] 가 있으면 검증된 조합을 seed 로 변주한 것을 바탕으로 깔고, raw 의 나머지 키(caption·mode·fx…) 로 덮는다."""
    from . import caption as CAP, fx as FX, motions as M, recipes
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("recipe"):
        base = recipes.BY_NAME.get(str(raw["recipe"]))
        if not base:
            return None, f"recipe 는 {[r['name'] for r in recipes.RECIPES]} 중"
        varied = recipes.vary(base, _int(raw.get("seed"), 0))
        merged = {**varied, **{k: v for k, v in raw.items() if k not in ("recipe",)}}
        if "caption" in raw and isinstance(raw["caption"], (str, dict)) and "palette" not in (raw["caption"] if isinstance(raw["caption"], dict) else {}):
            cap = raw["caption"] if isinstance(raw["caption"], dict) else {"text": raw["caption"]}
            merged["caption"] = {**cap, "palette": varied["palette"]}
        raw = merged
    mode = raw.get("mode", "cutout")
    if mode not in ("cutout", "photo"):
        return None, "mode 는 cutout(배경 빼기) / photo(사진 그대로)"
    key = (raw.get("keying") or {}) if isinstance(raw.get("keying"), dict) else {"mode": raw.get("keying") or "auto"}
    kmode = key.get("mode", "auto")
    if kmode not in ("auto", "white", "black", "color", "glow", "none"):
        return None, "keying 은 auto·white·black·color·glow·none"
    spec = {"mode": mode, "keying": {"mode": kmode, "tol": int(min(max(key.get("tol", 20), 5), 60))},
            "margin": float(min(max(raw.get("margin", 0.08), 0.04), 0.18)),
            "radius": int(min(max(raw.get("radius", 56), 8), 256)),   # 스티커는 투명 픽셀이 있어야 함(alpha_range) → 모서리 최소 8
            "seed": _int(raw.get("seed"), 1) % 1000,
            "framing": raw.get("framing") if raw.get("framing") in ("auto", "center", "top", "blur") else "auto"}
    spec["motion"], spec["fx"], spec["layers"] = [], [], []
    spec["loop"] = raw.get("loop") is not False                    # 움프: false = 6초 한 번 흐름 (타서 없어지기 등)
    if raw.get("cover") is True:
        spec["cover"] = True                                       # 사진이 돌거나 움직여도 가장자리(검정)가 안 보이게 확대
    motions = raw.get("motion") or [{"type": "idle"}]
    for item in (motions if isinstance(motions, list) else [motions])[:MAX_MOTIONS]:
        item = {"type": item} if isinstance(item, str) else item
        name = (item or {}).get("type") if isinstance(item, dict) else None
        if name == "keyframes":
            kf, err = _clean_keyframes(item)
            if err:
                return None, err
            spec["motion"].append(kf)
            continue
        if name not in M.PRESETS:
            return None, f"motion 은 keyframes 또는 {sorted(M.PRESETS)} 중"
        fn = {"breathe": M.idle, "float": M.float_}.get(name, M.PRESETS[name])
        spec["motion"].append({"type": name, **_clean_params(name, fn, item)})
    for item in (raw.get("fx") or [])[:MAX_FX]:
        item = {"type": item} if isinstance(item, str) else item
        name = (item or {}).get("type")
        if name not in FX.PRESETS or name in BLOCKED_FX:
            return None, f"fx 는 {sorted(set(FX.PRESETS) - BLOCKED_FX)} 중"
        spec["fx"].append({"type": name, **_clean_params(name, FX.PRESETS[name], item)})
    layers = raw.get("layers") or []
    if not isinstance(layers, list):
        return None, "layers 는 [{type, ...}, ...] 목록"
    light = [x for x in layers if isinstance(x, dict) and x.get("type") in LIGHT_LAYERS]
    if len(spec["fx"]) + len(layers) - len(light) > MAX_LAYERS:
        return None, f"효과(fx)+레이어(layers, 글자·도형 빼고)는 합쳐서 {MAX_LAYERS}개까지 (렌더 시간 상한)"
    if len(light) > MAX_LIGHT:
        return None, f"글자(text)·도형(shape) 레이어는 {MAX_LIGHT}개까지"
    for item in layers:
        lay, err = _clean_layer(item)
        if err:
            return None, err
        spec["layers"].append(lay)
    _fit_budget(spec["layers"])
    cap = raw.get("caption")
    if isinstance(cap, str):
        cap = {"text": cap}
    if isinstance(cap, dict) and str(cap.get("text") or "").strip():
        text = " ".join(str(cap["text"]).split())
        if len(text) > MAX_CAPTION:
            return None, f"글자는 {MAX_CAPTION}자까지 (2~7자가 가장 예쁨)"
        anims, seen_entr = [], False
        for a in (cap.get("anims") or ["bounce", "punch", "shine"]):
            if a in CAP.ANIMS and a not in anims and not (a in CAP.ENTRANCES and seen_entr):
                anims.append(a); seen_entr |= a in CAP.ENTRANCES
        anims = anims[:4] or ["bounce", "punch", "shine"]
        spec["caption"] = {"text": text, "palette": cap.get("palette") if cap.get("palette") in CAP.PALETTES else "gold",
                           "anims": anims, "position": "top" if cap.get("position") == "top" else "bottom",
                           "typing": bool(cap.get("typing", True))}
        for k in ("top", "mid", "bottom", "extrude"):            # 참고 스티커 글씨 색을 값으로 (팔레트 이름 대신)
            if (c := _color(cap.get(k))) is not None:
                spec["caption"][k] = c
        for k, (lo, hi) in CAPTION_NUM.items():                 # 테두리 두께·입체 깊이·글자 최대 크기
            if _num(cap.get(k)):
                spec["caption"][k] = int(_clip(cap[k], lo, hi))
    return spec, None


# ── 프레임워크 부품(prims) 값 검사 — 이름·숫자·범위만 통과 (경로·코드·수식 없음) ─────────────
MAX_LAYERS = 6            # fx + layers 합계 (CPU 상한) — 글자·도형은 따로
LIGHT_LAYERS, MAX_LIGHT = {"text", "shape"}, 8   # 한 번 그려 캐시하는 가벼운 레이어
MAX_KEYS = 16
PARTICLE_BUDGET = 300     # 모든 입자 레이어 개수 합 + 불씨 (2vCPU 서버 렌더 시간 상한, tests/test_animation.py 실측)


def _p():
    from . import prims
    return prims


def layer_params() -> dict:
    """레이어 종류 → {인자: (형식, …)}. 형식: num lo hi · int lo hi · osc lo hi(숫자 또는 [a,b] 진동) · range lo hi(숫자 또는 [최소,최대]) ·
    enum 선택지 · color · colors n · xy(0~1 좌표) · times n(0~1 시각 목록) · text n · lines n(줄바꿈 유지, 4줄) · pair lo hi([가로,세로]) ·
    bool. 모든 레이어에 start·end(0~1)."""
    P = _p()
    from . import layout as L
    return {
        "particles": {"shape": ("enum", tuple(P.SHAPES)), "char": ("text", 2), "colors": ("colors", 6), "color": ("color",),
                      "count": ("int", 1, 200), "size": ("range", 2, 160), "spawn": ("enum", P.SPAWNS), "at": ("xy",),
                      "spread": ("num", 0, 0.5), "angle": ("num", -360, 360), "angle_spread": ("num", 0, 180),
                      "speed": ("num", 0, 900), "gravity": ("num", -900, 900), "wind": ("num", -600, 600),
                      "spin": ("num", -1080, 1080), "life": ("num", 0.1, 6), "fade": ("enum", P.FADES),
                      "grow": ("num", 0.1, 5), "blend": ("enum", P.BLENDS), "blur": ("int", 0, 10),
                      "opacity": ("num", 0.05, 1), "turbulence": ("num", 0, 60), "burst": ("bool",)},
        "grade": {"brightness": ("osc", -0.4, 0.4), "contrast": ("osc", 0.5, 2), "saturation": ("osc", 0, 2.5),
                  "hue_shift": ("osc", -180, 180), "hue_spin": ("int", -3, 3), "tint": ("color",), "tint_amount": ("osc", 0, 0.8),
                  "vignette": ("osc", 0, 1), "grain": ("num", 0, 0.25), "bloom": ("osc", 0, 1.2), "cycles": ("int", 1, 6)},
        "text": {"text": ("lines", 60), "font": ("enum", tuple(L.FONTS)), "at": ("xy",), "size": ("int", 14, 200),
                 "width": ("num", 0.1, 1), "align": ("enum", L.ALIGNS), "color": ("color",), "colors": ("colors", 4),
                 "stroke": ("int", 0, 20), "stroke_color": ("color",), "depth": ("int", 0, 16), "depth_color": ("color",),
                 "stroke2": ("int", 0, 16), "stroke2_color": ("color",), "glow": ("int", 0, 24), "glow_color": ("color",), "slant": ("num", -0.5, 0.5),
                 "rotate": ("num", -45, 45), "opacity": ("num", 0.05, 1), "enter": ("enum", L.ENTERS),
                 "enter_dur": ("num", 0, 1), "idle": ("enum", L.IDLES), "amp": ("num", 0, 4)},
        "shape": {"kind": ("enum", L.SHAPE_KINDS), "at": ("xy",), "wh": ("pair", 0.05, 1), "fill": ("color",),
                  "stroke": ("int", 0, 16), "stroke_color": ("color",), "radius": ("num", 0, 0.5), "tail": ("xy",),
                  "points": ("int", 3, 12), "rotate": ("num", -45, 45), "opacity": ("num", 0.05, 1),
                  "enter": ("enum", L.ENTERS), "enter_dur": ("num", 0, 1), "idle": ("enum", L.IDLES), "amp": ("num", 0, 4)},
        "flash": {"times": ("times", 6), "count": ("int", 1, 6), "color": ("color",), "strength": ("num", 0, 0.75),
                  "decay": ("num", 0.02, 0.3)},
        "lightning": {"times": ("times", 4), "count": ("int", 1, 4), "color": ("color",), "origin": ("xy",), "target": ("xy",),
                      "branches": ("int", 0, 4), "width": ("int", 1, 8), "strength": ("num", 0, 0.6), "decay": ("num", 0.03, 0.25)},
        "transition": {"kind": ("enum", P.TRANSITIONS), "direction": ("enum", ("out", "in")), "scale": ("int", 8, 160),
                       "edge": ("num", 0, 0.2), "edge_color": ("color",), "embers": ("int", 0, 80), "tiles": ("int", 3, 10),
                       "to": ("color",), "origin": ("enum", P.ORIGINS)},
    }


KEY_RANGES = {"t": (0, 1), "scale": (0.05, 4), "sx": (0.05, 4), "sy": (0.05, 4), "rotate": (-1440, 1440),
              "x": (-1.5, 1.5), "y": (-1.5, 1.5), "opacity": (0, 1)}


def _color(v):
    """[r,g,b] 또는 '#rrggbb' → 정수 튜플 (아니면 None)."""
    import re
    if isinstance(v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", v.strip()):
        h = v.strip().lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(_num(x) for x in v):
        return tuple(int(min(max(x, 0), 255)) for x in v)
    return None


def _clip(v, lo, hi):
    return min(max(float(v), lo), hi)


def _clean_value(kind: tuple, v):
    """형식 하나 검사 → 값 (못 쓰면 None — 그 인자만 기본값으로)."""
    k = kind[0]
    if k == "num" and _num(v):
        return _clip(v, kind[1], kind[2])
    if k == "int" and _num(v):
        return int(_clip(v, kind[1], kind[2]))
    if k == "osc":
        if _num(v):
            return _clip(v, kind[1], kind[2])
        if isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v):
            return [_clip(x, kind[1], kind[2]) for x in v]
    if k == "range":
        if _num(v):
            return (_clip(v, kind[1], kind[2]),) * 2
        if isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v):
            a, b = sorted(_clip(x, kind[1], kind[2]) for x in v)
            return (a, b)
    if k == "enum" and isinstance(v, str) and v in kind[1]:
        return v
    if k == "color":
        return _color(v)
    if k == "colors" and isinstance(v, (list, tuple)):
        cols = [c for c in (_color(x) for x in v[:kind[1]]) if c]
        return cols or None
    if k == "xy" and isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v):
        return tuple(_clip(x, 0, 1) for x in v)
    if k == "times" and isinstance(v, (list, tuple)) and all(_num(x) for x in v):
        return [_clip(x, 0, 1) for x in v[:kind[1]]] or None
    if k == "text" and isinstance(v, str):
        return " ".join(v.split())[:kind[1]] or None
    if k == "lines" and isinstance(v, str):
        lines = [" ".join(ln.split()) for ln in v.replace("\\n", "\n").split("\n")]
        out = "\n".join(ln for ln in lines if ln)[:kind[1]]
        return "\n".join(out.split("\n")[:4]) or None
    if k == "pair" and isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v):
        return tuple(_clip(x, kind[1], kind[2]) for x in v)
    if k == "bool" and isinstance(v, bool):
        return v
    return None


def _clean_keyframes(item: dict) -> tuple[dict | None, str | None]:
    keys = item.get("keys")
    if not isinstance(keys, list) or not keys:
        return None, "keyframes 는 keys=[{t, scale, rotate, x, y, opacity, ease}, ...] (1~16개, t 는 0~1)"
    out = []
    P = _p()
    for i, k in enumerate(keys[:MAX_KEYS]):
        if not isinstance(k, dict):
            continue
        c = {p: _clip(k[p], *KEY_RANGES[p]) for p in KEY_RANGES if p in k and _num(k[p])}
        c.setdefault("t", i / max(1, len(keys) - 1))
        if k.get("ease") in P.EASES:
            c["ease"] = k["ease"]
        out.append(c)
    if not out:
        return None, "keyframes keys 가 비었음"
    kf = {"type": "keyframes", "keys": out}
    piv = _clean_value(("xy",), item.get("pivot"))
    if piv:
        kf["pivot"] = piv
    return kf, None


def _clean_layer(item) -> tuple[dict | None, str | None]:
    from . import fx as FX
    item = {"type": item} if isinstance(item, str) else item
    if not isinstance(item, dict):
        return None, "layers 항목은 {type, ...}"
    name = item.get("type")
    schema = layer_params()
    win = {k: _clip(item[k], 0, 1) for k in ("start", "end") if _num(item.get(k))}
    if name in schema:
        P = _p()
        if name == "particles":
            shape = item.get("shape", "circle")
            if isinstance(shape, str) and shape not in P.SHAPES:        # 이모지·글자로 준 모양
                if P.EMOJI.get(shape.strip().rstrip("️")):
                    item = {**item, "shape": P.EMOJI[shape.strip().rstrip("️")]}
                elif 1 <= len(shape.strip()) <= 2 and P.glyph_ok(shape.strip()):
                    item = {**item, "shape": "char", "char": shape.strip()}
                else:
                    return None, f"particles shape 는 {list(P.SHAPES)} 중 (글자는 shape=char, char='별')"
            if item.get("shape") == "char":
                ch = str(item.get("char") or "").strip()[:2]
                if ch and not P.glyph_ok(ch):
                    mapped = P.EMOJI.get(ch.rstrip("️"))
                    if not mapped:
                        return None, f"글꼴에 없는 글자 '{ch}' — 한글·영문·숫자만, 이모지는 비슷한 shape 로"
                    item = {**item, "shape": mapped}
                elif not ch:
                    return None, "shape=char 이면 char 에 글자 1~2자"
        if name == "text":
            from . import layout as L
            words = _clean_value(("lines", 60), item.get("text"))
            if not words:
                return None, "text 레이어는 text 에 글자 (줄바꿈 '\\n', 4줄·60자까지)"
            want = item.get("font") if item.get("font") in L.FONTS else "bold"
            bad = L.missing_glyphs(words, want)
            if bad:   # 둥근·귀여운 글꼴은 흔한 글자 2,350자만 → 그 글자가 다 있는 다른 글꼴로 (깔끔한 고딕이 가장 넓음)
                other = next((f for f in ("gothic", "bold", "pen", "round", "cute") if not L.missing_glyphs(words, f)), None)
                if not other:
                    return None, f"글꼴에 없는 글자 '{bad[:6]}' — 그 글자를 빼기 (이모지는 shape·particles 로)"
                item = {**item, "font": other}
        lay = {"type": name}
        for k, kind in schema[name].items():
            if k in item:
                v = _clean_value(kind, item[k])
                if v is not None:
                    lay[k] = v
        lay.update(win)
        return lay, None
    if name in FX.PRESETS and name not in BLOCKED_FX:                     # 기존 효과 이름도 레이어로 (순서·시간 창 조절)
        return {"type": name, **_clean_params(name, FX.PRESETS[name], item), **win}, None
    return None, (f"layers type 은 {list(schema)} 또는 효과 이름({', '.join(sorted(set(FX.PRESETS) - BLOCKED_FX))}) 중")


def _fit_budget(layers: list) -> None:
    """입자 합계가 PARTICLE_BUDGET 를 넘으면 비율대로 줄임 (한 편 렌더 CPU 상한)."""
    total = sum(l.get("count", 30) for l in layers if l["type"] == "particles") + \
        sum(l.get("embers", 30) for l in layers if l["type"] == "transition" and l.get("kind") == "burn")
    if total > PARTICLE_BUDGET:
        k = PARTICLE_BUDGET / total
        for l in layers:
            if l["type"] == "particles":
                l["count"] = max(1, int(l.get("count", 30) * k))
            elif l["type"] == "transition" and l.get("kind") == "burn":
                l["embers"] = int(l.get("embers", 30) * k)


def catalog() -> dict:
    """부품 목록을 코드에서 바로 (이름 → 한 줄 설명 + 조절 인자·기본값). 문서에 따로 적으면 엔진과 어긋나므로 여기서만 읽는다."""
    from . import caption as CAP, fx as FX, motions as M

    def entry(fn):
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        params = {}
        for name, p in inspect.signature(fn).parameters.items():
            if name in ("frame", "t", "ctx", "_", "image", "k") or p.kind == p.VAR_KEYWORD or p.default is p.empty:
                continue
            params[name] = p.default
        return {"doc": doc, "params": params}
    motions = {n: entry({"breathe": M.idle, "float": M.float_}.get(n, f)) for n, f in M.PRESETS.items()}
    motions["breathe"]["doc"] = "아주 잔잔한 숨쉬기 (잠·밤)."
    fxs = {n: entry(f) for n, f in FX.PRESETS.items() if n not in BLOCKED_FX}
    return {"motions": motions, "fx": fxs, "caption_anims": dict(CAP.ANIMS), "palettes": list(CAP.PALETTES)}


_LOCK = asyncio.Semaphore(1)


def _load(image: bytes, spec: dict, tmp: str):
    """원본 bytes → (RGBA, 키잉 방식)."""
    from PIL import Image
    from . import keying
    src_path = os.path.join(tmp, "src.img")
    with open(src_path, "wb") as f:
        f.write(image)
    if spec["mode"] == "cutout":
        return keying.key_image(src_path, spec["keying"]["mode"], tol=spec["keying"]["tol"])
    return Image.open(src_path).convert("RGBA"), "photo"


def _inspect(built: dict, used: str, holes: int | None, spec: dict) -> tuple[list, dict]:
    from . import qc
    return qc.inspect(built["frames"], mode=built["mode"], keying=used, subject_boxes=built["subject_boxes"],
                      cap_box=built["cap_box"], focus_mask=built["focus"], holes=holes, has_caption=bool(spec.get("caption")))


def render(image: bytes, spec: dict, icon: bool = False, keep: str | None = None) -> Result:
    """sanitize 된 spec 으로 스티커를 만들고 검사·검수까지 (동기 — forge 가 to_thread 로 감쌈)."""
    from . import caption as CAP, engine, verify
    tmp = tempfile.mkdtemp(prefix="sodam_stk_")
    try:
        src, used = _load(image, spec, tmp)
        frames_dir = os.path.join(tmp, "frames")
        built = engine.build(src, spec, frames_dir, FONT)
        out = os.path.join(tmp, "out.webm")
        engine.encode_fit(frames_dir, out, "photo" if spec["mode"] == "photo" else "cutout", limit=250 * 1024)
        cap = spec.get("caption")
        holes_ok = used != "glow" and not cap                        # 글자 속 구멍·후광 틈은 정상
        res = verify.spec(out, "sticker", holes=holes_ok)
        rows = list(res["rows"])
        holes = next((int(v.split()[0]) for n, v, _ in rows if n == "holes"), None)
        if cap:
            style = CAP.Style(**{k: v for k, v in cap.items() if k != "text"})
            style.anims = tuple(style.anims)
            g = CAP.glyph_check(cap["text"], FONT, style)
            rows.append(("glyphs", f"worst {g['worst']}%", g["ok"]))
        warnings, metrics = _inspect(built, used, holes, spec)
        prev = os.path.join(tmp, "preview.png")
        engine.contact_sheet(built["frames"], prev)
        icon_bytes = b""
        if icon:
            ip = os.path.join(tmp, "icon.webm")
            engine.encode_fit(frames_dir, ip, "icon")
            if verify.spec(ip, "icon")["ok"]:
                icon_bytes = open(ip, "rb").read()
        if keep:                                                     # 디버그: PNG 로 풀어 둠
            os.makedirs(keep, exist_ok=True)
            for i, f in enumerate(built["frames"]):
                f.save(os.path.join(keep, f"f{i:04d}.png"))
        return Result(all(r[2] for r in rows), open(out, "rb").read(), icon_bytes, open(prev, "rb").read(), rows, used,
                      warnings=warnings, metrics=metrics, spec=spec)
    except Exception as e:   # 이상한 사진·인코딩 실패
        return Result(False, error=f"{type(e).__name__}: {str(e)[:160]}", spec=spec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_video(image: bytes, spec: dict) -> Result:
    """움프: 같은 엔진으로 그린 뒤 640×640 H.264 약 6초 — loop(기본) = 3초 루프 ×2, loop=false = 6초 한 번 흐름. 알파는 검정 위에 평탄화.
    photo 모드가 자연스럽고(radius 0 권장), cutout 도 됨(배경 검정)."""
    from . import engine
    spec = {**spec, "radius": 0}                                     # 프사는 원형으로 잘리므로 모서리 없음
    tmp = tempfile.mkdtemp(prefix="sodam_ump_")
    try:
        src, used = _load(image, spec, tmp)
        frames_dir = os.path.join(tmp, "frames")
        tl = engine.timeline(spec, "ump")
        built = engine.build(src, spec, frames_dir, FONT, tl=tl, flatten=True)
        out = os.path.join(tmp, "out.mp4")
        size, crf = engine.encode_profile(frames_dir, out, loops=2 if tl["loop"] else 1, fps=tl["fps"], seconds=tl["seconds"])
        rows = [("size", f"{size / 1024:.0f}KB / {VIDEO_MAX_BYTES // 1024}KB", size <= VIDEO_MAX_BYTES),
                ("crf", str(crf), True)]
        warnings, metrics = _inspect(built, used, None, spec)
        prev = os.path.join(tmp, "preview.png")
        engine.contact_sheet(built["frames"], prev)
        return Result(size <= VIDEO_MAX_BYTES, mp4=open(out, "rb").read(), preview=open(prev, "rb").read(), rows=rows,
                      keying=used, warnings=warnings, metrics=metrics, spec=spec)
    except Exception as e:
        return Result(False, error=f"{type(e).__name__}: {str(e)[:160]}", spec=spec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_static(image: bytes, spec: dict) -> Result:
    """정지 스티커: 같은 엔진으로 그린 뒤 글자가 다 나온 마지막 장면 한 장 → 512 WEBP (투명)."""
    import io
    from . import caption as CAP, engine
    if spec.get("caption"):
        spec["caption"] = {**spec["caption"], "typing": False}
    tmp = tempfile.mkdtemp(prefix="sodam_stk_")
    try:
        src, used = _load(image, spec, tmp)
        built = engine.build(src, spec, None, FONT, last_only=True)   # 움직임은 마지막 순간 그대로 (keyframes 한 점 = 배치)
        frame = built["frames"][-1]
        buf = io.BytesIO()
        frame.save(buf, "WEBP", quality=92, method=6)
        data = buf.getvalue()
        if len(data) > STATIC_MAX_BYTES:
            buf = io.BytesIO()
            frame.save(buf, "WEBP", quality=70, method=6)
            data = buf.getvalue()
        alpha = frame.getchannel("A").getextrema()
        rows = [("size", f"{len(data) // 1024}KB / {STATIC_MAX_BYTES // 1024}KB", len(data) <= STATIC_MAX_BYTES),
                ("dims", f"{frame.width}x{frame.height}", max(frame.size) == 512),
                ("alpha_range", f"{alpha[0]}..{alpha[1]}", alpha[0] < 255)]
        cap = spec.get("caption")
        if cap:
            style = CAP.Style(**{k: v for k, v in cap.items() if k != "text"})
            style.anims = tuple(style.anims)
            g = CAP.glyph_check(cap["text"], FONT, style)
            rows.append(("glyphs", f"worst {g['worst']}%", g["ok"]))
        warnings, metrics = qc_one(built, used, spec)
        prev = io.BytesIO()
        frame.save(prev, "PNG")
        return Result(all(r[2] for r in rows), preview=prev.getvalue(), rows=rows, keying=used, warnings=warnings,
                      metrics=metrics, spec=spec, still=data)
    except Exception as e:
        return Result(False, error=f"{type(e).__name__}: {str(e)[:160]}", spec=spec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def qc_one(built: dict, used: str, spec: dict) -> tuple[list, dict]:
    """정지 스티커 검수: 움직임 경고(밋밋·요란)는 뺌."""
    warnings, metrics = _inspect(built, used, None, spec)
    return [w for w in warnings if not w.startswith(("너무 밋밋", "너무 요란"))], metrics


async def forge_static(image: bytes, spec: dict) -> Result:
    async with _LOCK:
        return await asyncio.to_thread(render_static, image, spec)


async def forge(image: bytes, spec: dict, icon: bool = False) -> Result:
    """sanitize 된 spec 으로 만들고 검사까지 (한 번에 하나)."""
    async with _LOCK:
        return await asyncio.to_thread(render, image, spec, icon)


async def forge_video(image: bytes, spec: dict) -> Result:
    async with _LOCK:
        return await asyncio.to_thread(render_video, image, spec)


def looks_illustrated(image: bytes) -> bool:
    """이미 그림(일러스트·캐릭터·로고)인가? — 그림체 변환(art) 을 또 하면 돈·시간 낭비.
    가벼운 판별: 축소본에서 (1) 이웃과 거의 같은 색인 '평평한' 면이 많고 (2) 굵은 윤곽선이 있고 (3) 색 종류가 적으면 그림.
    실사는 노이즈·연속 톤이라 평평한 픽셀이 적고 색이 많다. 확신이 없으면 False (사진으로 취급)."""
    import io
    import numpy as np
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(image)).convert("RGB").resize((160, 160), Image.BILINEAR)
    except Exception:   # 깨진 사진 → 사진으로 취급 (뒤 단계가 형식 오류를 안내)
        return False
    a = np.asarray(im, dtype=np.int16)
    dx = np.abs(a[:, 1:] - a[:, :-1]).max(axis=2)
    dy = np.abs(a[1:] - a[:-1]).max(axis=2)
    flat = ((dx[:-1] <= 2) & (dy[:, :-1] <= 2)).mean()             # 이웃과 거의 같은 색 (면)
    edges = ((dx[:-1] > 40) | (dy[:, :-1] > 40)).mean()            # 굵은 선·윤곽
    colors = len(set(map(tuple, (a[::2, ::2] // 16).reshape(-1, 3).tolist())))   # 16단계 양자화 색 수
    return bool(flat > 0.45 and edges > 0.01 and colors < 260)
