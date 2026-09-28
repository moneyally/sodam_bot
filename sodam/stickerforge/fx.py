"""Full-frame effects applied to the 512 RGBA frame after the body motion.

Each effect is fx(frame, t, ctx) -> frame. `ctx` carries the spec, a seeded RNG
and the hit times of impulse motions so effects can sync to them (a muzzle flash
on each recoil, a shockwave on each jab). Effects that draw outside the
silhouette (rings, bolts, sparkles) extend the alpha, effects that only recolour
(sweep, aura, scan) stay inside it.

Ordering matters: put backdrop effects first (rays, outline), colour effects in
the middle (sweep, scan, aura), overlays next (sparkle, hearts, meteors,
shockwave, bolts, flash), and distortions last (glitch, slice_glitch) so they
tear everything that was drawn before them.
"""
from __future__ import annotations

import math
import random

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .const import S, D

# ---------------------------------------------------------------- sprites

def _star(size: int, ss: int = 6, color=(255, 252, 235)) -> Image.Image:
    s = size * ss
    im = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(im)
    c, arm, waist = s / 2, s * 0.5, s * 0.075
    d.polygon([(c, c - arm), (c + waist, c - waist), (c + arm, c), (c + waist, c + waist),
               (c, c + arm), (c - waist, c + waist), (c - arm, c), (c - waist, c - waist)], fill=255)
    short, thin = s * 0.26, s * 0.035
    d.polygon([(c - short, c - short), (c + thin, c - thin), (c + short, c + short), (c - thin, c + thin)], fill=220)
    d.polygon([(c + short, c - short), (c + thin, c + thin), (c - short, c + short), (c - thin, c - thin)], fill=220)
    im = im.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), color + (0,))
    glow = im.filter(ImageFilter.GaussianBlur(size * 0.09))
    out.putalpha(ImageChops.lighter(im, glow.point(lambda v: int(v * 0.8))))
    return out


def _heart(size: int, ss: int = 6, color=(255, 92, 120)) -> Image.Image:
    s = size * ss
    im = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(im)
    d.ellipse([s * 0.06, s * 0.10, s * 0.52, s * 0.58], fill=255)
    d.ellipse([s * 0.48, s * 0.10, s * 0.94, s * 0.58], fill=255)
    d.polygon([(s * 0.06, s * 0.38), (s * 0.94, s * 0.38), (s * 0.5, s * 0.95)], fill=255)
    out = Image.new("RGBA", (size, size), color + (0,))
    out.putalpha(im.resize((size, size), Image.LANCZOS))
    return out


_CACHE = {}


def sprite(kind: str, size: int, color=None) -> Image.Image:
    key = (kind, size, color)
    if key not in _CACHE:
        if kind == "star":
            _CACHE[key] = _star(size, color=color or (255, 252, 235))
        elif kind == "heart":
            _CACHE[key] = _heart(size, color=color or (255, 92, 120))
        else:
            im = Image.open(kind).convert("RGBA")
            _CACHE[key] = im.resize((size, int(size * im.height / im.width)), Image.LANCZOS)
    return _CACHE[key]


def _fade_alpha(spr: Image.Image, a: float) -> Image.Image:
    if a >= 1:
        return spr
    out = spr.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * a)))
    return out


# ---------------------------------------------------------------- particles

_TWINKLE = [(56, 26, 44, 0.10, 0.5), (40, 418, 74, 0.55, -0.5), (28, 110, 196, 0.95, 0.5),
            (40, 322, 26, 1.35, -0.5), (56, 436, 244, 0.30, 0.5), (40, 44, 306, 1.70, -0.5),
            (28, 238, 110, 2.05, 0.5), (28, 186, 286, 0.75, -0.5), (56, 300, 320, 2.30, 0.5),
            (40, 150, 60, 1.95, -0.5)]
_RISING = [(56, 40, 0.00, 1, 0.25), (40, 150, 0.40, 1, -0.25), (28, 250, 0.70, 2, 0.25),
           (40, 330, 0.20, 1, -0.25), (56, 430, 0.85, 2, 0.25), (28, 205, 0.50, 1, -0.25)]
_FALLING = [(64, 26, 0.00, 1, 0.4), (48, 140, 0.35, 1, -0.4), (36, 246, 0.62, 2, 0.5),
            (64, 330, 0.15, 1, -0.5), (48, 432, 0.78, 2, 0.4), (36, 196, 0.45, 1, -0.4),
            (48, 60, 0.90, 2, 0.5), (64, 398, 0.52, 1, -0.4)]


def sparkle(frame, t, ctx, subset=None, color=None, **_):
    """Star glints that twinkle at staggered times. `subset` = list of indices 0-9
    to thin them out (fewer particles = more bits for the character)."""
    for i, (sz, x, y, t0, spin) in enumerate(_TWINKLE):
        if subset is not None and i not in subset:
            continue
        u = t - t0
        if u < 0 or u > 0.52:
            continue
        a = min(1.0, u / 0.16) if u < 0.28 else max(0.0, (0.52 - u) / 0.24)
        spr = sprite("star", sz, tuple(color) if color else None).rotate(spin * 360 * t / D, resample=Image.BICUBIC)
        frame.alpha_composite(_fade_alpha(spr, a), (x, y))
    return frame


def hearts(frame, t, ctx, color=None, **_):
    for sz, x, phase, speed, spin in _RISING:
        u = (t * speed / D + phase) % 1.0
        y = S - u * (S + sz)
        spr = sprite("heart", sz, tuple(color) if color else None).rotate(spin * 30 * math.sin(2 * math.pi * t / D), resample=Image.BICUBIC)
        frame.alpha_composite(spr, (x, int(y)))
    return frame


def rain(frame, t, ctx, image=None, rising=False, **_):
    """Your own PNG (coins, tokens, logos) falling — or rising with rising=True."""
    if not image:
        raise ValueError("fx 'rain'/'rise' needs image=<png with alpha>")
    for sz, x, phase, speed, spin in _FALLING:
        u = (t * speed / D + phase) % 1.0
        y = S - u * (S + sz) if rising else -sz + u * (S + sz)
        spr = sprite(image, sz).rotate(spin * 360 * t / D, resample=Image.BICUBIC, expand=False)
        frame.alpha_composite(spr, (x, int(y)))
    return frame


def rise(frame, t, ctx, image=None, **k):
    return rain(frame, t, ctx, image=image, rising=True)


def meteors(frame, t, ctx, paths=None, **_):
    """Shooting stars. Default three streaks across the upper half."""
    paths = paths or [(0.30, (60, 20), (300, 150)), (1.35, (250, 10), (470, 140)), (2.05, (150, 30), (330, 160))]
    for t0, (x0, y0), (x1, y1) in paths:
        u = t - t0
        if not (0 <= u < 0.55):
            continue
        k = u / 0.55; k2 = k * k * (3 - 2 * k)
        hx, hy = x0 + (x1 - x0) * k2, y0 + (y1 - y0) * k2
        L = 0.28; tx, ty = x0 + (x1 - x0) * max(0, k2 - L), y0 + (y1 - y0) * max(0, k2 - L)
        a = int(255 * math.sin(math.pi * k))
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
        d.line([(tx, ty), (hx, hy)], fill=(255, 240, 180, a), width=5)
        layer = layer.filter(ImageFilter.GaussianBlur(2.2))
        d = ImageDraw.Draw(layer); d.line([(tx, ty), (hx, hy)], fill=(255, 255, 255, a), width=2)
        d.ellipse([hx - 3, hy - 3, hx + 3, hy + 3], fill=(255, 255, 255, a))
        frame.alpha_composite(layer)
    return frame


_ZFONT = None


def zzz(frame, t, ctx, origin=(330, 120), color=(255, 225, 120), **_):
    """Three Z's drifting up from `origin`. Sleep stickers."""
    global _ZFONT
    for k in range(3):
        u = ((t / D) + k * 0.33) % 1.0
        if u > 0.85:
            continue
        a = min(1.0, u / 0.1) * (max(0.0, (0.85 - u) / 0.3) if u > 0.55 else 1.0)
        size = 26 + int(30 * u)
        f = ImageFont.truetype(ctx["font"], size)
        x = origin[0] + int(70 * u) + int(10 * math.sin(2 * math.pi * u * 2))
        y = origin[1] - int(120 * u)
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((x, y), "Z", font=f, fill=tuple(color) + (int(255 * a),),
                                   stroke_width=3, stroke_fill=(70, 40, 0, int(255 * a)))
        frame.alpha_composite(layer)
    return frame


# ---------------------------------------------------------------- light

_YY, _XX = np.mgrid[0:S, 0:S].astype(np.float32)
_DIAG = (_XX + _YY) / (2 * S)                                   # sweep 대각 좌표 (0~1)
_POLAR = {}


def _polar(center):
    """rays 용 (각도, 페이드) — 중심마다 한 번만 계산."""
    if center not in _POLAR:
        cx, cy = S * center[0], S * center[1]
        r = np.hypot(_XX - cx, _YY - cy)
        _POLAR[center] = (np.arctan2(_YY - cy, _XX - cx), np.clip(1 - r / (S * 0.62), 0, 1) * np.clip(r / 40, 0, 1))
    return _POLAR[center]

def sweep(frame, t, ctx, strength=0.5, width=0.09, color=(255, 250, 225), **_):
    """Diagonal highlight crossing the silhouette once per loop (ends off-canvas)."""
    c = -0.35 + 1.7 * (t / D)
    band = np.exp(-((_DIAG - c) / width) ** 2)          # 격자는 모듈 로드 때 한 번 (프레임마다 mgrid 는 느림)
    hit = band > 0.004                                   # 띠 밖 픽셀은 손대지 않음 (프레임당 ~15% 만 계산)
    if not hit.any():
        return frame
    a = np.array(frame, dtype=np.uint8)
    px = a[hit].astype(np.float32); al = px[:, 3:4] / 255.0
    col = np.array(color, dtype=np.float32)
    px[:, :3] += (col - px[:, :3]) * (band[hit][:, None] * strength) * al
    a[hit] = np.clip(px + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def scan(frame, t, ctx, height=70, alpha=0.45, **_):
    """Horizontal light bar sweeping top to bottom. 'sending / uploading' feel."""
    y0 = -height + ((t / D) % 1.0) * (S + height)
    yy = np.arange(S)[:, None]
    band = np.exp(-((yy - (y0 + height / 2)) / (height / 3.2)) ** 2)
    a = np.asarray(frame).astype(np.float32)
    a[..., :3] = np.clip(a[..., :3] + (band * alpha * 255)[..., None] * (a[..., 3:4] / 255.0), 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def rays(frame, t, ctx, spokes=12, strength=0.22, color=(255, 244, 200), center=(0.5, 0.42), **_):
    """Rotating sunburst behind the silhouette (translucent outside it)."""
    base_ang, fade = _polar(tuple(center))
    ang = base_ang + 2 * math.pi * (t / D) / spokes
    wedge = (np.sin(ang * spokes) > 0.55).astype(np.float32)
    layer = Image.new("RGBA", frame.size, tuple(color) + (0,))
    layer.putalpha(Image.fromarray((wedge * fade * strength * 255).astype(np.uint8), "L"))
    layer.alpha_composite(frame)
    return layer


def outline(frame, t, ctx, color=(255, 255, 255), width=5, pulse=0, **_):
    """Even sticker-style border around the silhouette. `pulse` px breathes it."""
    a = frame.getchannel("A")
    w = width + (int(round(pulse * math.sin(2 * math.pi * 2 * t / D))) if pulse else 0)
    ring = Image.new("RGBA", frame.size, tuple(color) + (0,))
    ring.putalpha(a.filter(ImageFilter.MaxFilter(2 * w + 1)))
    ring.alpha_composite(frame)
    return ring


def glow(frame, t, ctx, color=(255, 255, 255), radius=10, strength=0.6, pulse=True, **_):
    """Soft bloom behind the silhouette, optionally pulsing twice per loop."""
    k = strength * (0.6 + 0.4 * math.sin(2 * math.pi * 2 * t / D)) if pulse else strength
    a = frame.getchannel("A").filter(ImageFilter.GaussianBlur(radius))
    layer = Image.new("RGBA", frame.size, tuple(color) + (0,))
    layer.putalpha(a.point(lambda v: int(v * k)))
    layer.alpha_composite(frame)
    return layer


def aura(frame, t, ctx, flicker=0.06, flare=0.32, tau=0.12, **_):
    """Brightness flicker restricted to semi-transparent (glow) pixels, with a
    flare on each hit. Made for `glow`-keyed dark art. Keep flare <= ~0.35 or
    the whole picture whites out."""
    g = 1 + flicker * math.sin(2 * math.pi * 7 * t / D) + flicker * 0.7 * math.sin(2 * math.pi * 11 * t / D + 1.3)
    g += sum(flare * _imp(t, h, tau) for h in ctx["hits"])
    if abs(g - 1) < 0.02:
        return frame
    a = np.asarray(frame).astype(np.float32); al = a[..., 3:4] / 255.0
    w = np.clip((1 - al) * 1.6, 0, 1) * (al > 0.02)
    a[..., :3] = np.clip(a[..., :3] * (1 + (g - 1) * w), 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def flashbang(frame, t, ctx, amount=55, tau=0.10, **_):
    """Whole-silhouette brightness pop on each hit (gunshot, impact)."""
    k = sum(max(0.0, 1 - (t - h) / tau) for h in ctx["hits"] if 0 <= t - h < tau)
    if k <= 0:
        return frame
    a = np.asarray(frame).astype(np.float32)
    a[..., :3] = np.clip(a[..., :3] + amount * k * (a[..., 3:4] / 255), 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


# ---------------------------------------------------------------- impacts

def _imp(t, t0, tau):
    u = t - t0
    return math.exp(-u / tau) if u >= 0 else 0.0


def flash(frame, t, ctx, at=(374, 213), size=56, **_):
    """Muzzle-flash star at a point on each hit, 3 frames, spinning and shrinking."""
    for h in ctx["hits"]:
        u = t - h
        if 0 <= u < 0.10:
            k = 1 - u / 0.10; sz = int(size * (0.7 + 1.6 * k))
            spr = sprite("star", 56).resize((sz, sz), Image.LANCZOS).rotate(37 * u * 40, resample=Image.BICUBIC)
            frame.alpha_composite(_fade_alpha(spr, min(1, k * 1.4)), (at[0] - sz // 2, at[1] - sz // 2))
    return frame


def shockwave(frame, t, ctx, center=(256, 236), color=(215, 205, 255), r0=55, r1=315, life=0.38, **_):
    """Expanding ring from `center` on each hit."""
    for h in ctx["hits"]:
        u = t - h
        if not (0 <= u < life):
            continue
        k = u / life; r = r0 + (r1 - r0) * (1 - (1 - k) ** 2); w = max(2, int(16 * (1 - k))); a = int(210 * (1 - k) ** 1.3)
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse([center[0] - r, center[1] - r * 0.92, center[0] + r, center[1] + r * 0.92],
                                      outline=tuple(color) + (a,), width=w)
        frame.alpha_composite(layer.filter(ImageFilter.GaussianBlur(1.2)))
    return frame


def bolts(frame, t, ctx, center=(256, 236), color=(150, 120, 255), count=6, life=0.17, **_):
    """Lightning forks around `center` on each hit. Shapes are seeded per hit so
    every loop repeats identically."""
    for hi, h in enumerate(ctx["hits"]):
        u = t - h
        if not (0 <= u < life):
            continue
        rng = random.Random(ctx["seed"] * 100 + hi)
        shapes = []
        for _ in range(count):
            a0 = rng.uniform(0, 2 * math.pi); r = rng.uniform(85, 120)
            x, y = center[0] + math.cos(a0) * r, center[1] + math.sin(a0) * r * 0.9
            L = rng.uniform(90, 150); pts = [(x, y)]
            for _ in range(9):
                x += math.cos(a0) * L / 9 + rng.uniform(-16, 16); y += math.sin(a0) * L / 9 + rng.uniform(-16, 16); pts.append((x, y))
            shapes.append(pts)
        k = 1 - u / life
        glow_l = Image.new("RGBA", (S, S), (0, 0, 0, 0)); core_l = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        dg, dc = ImageDraw.Draw(glow_l), ImageDraw.Draw(core_l)
        for j, pts in enumerate(shapes):
            if (int(u * 30) + j) % 3 == 0 and u > 0.05:
                continue
            dg.line(pts, fill=tuple(color) + (int(230 * k),), width=7, joint="curve")
            dc.line(pts, fill=(255, 255, 255, int(255 * k)), width=2, joint="curve")
        frame.alpha_composite(glow_l.filter(ImageFilter.GaussianBlur(3))); frame.alpha_composite(core_l)
    return frame


# ---------------------------------------------------------------- distortion

def _rgb_split(a: np.ndarray, dx: int) -> np.ndarray:
    out = a.copy()
    out[..., 0] = np.roll(a[..., 0], dx, axis=1); out[..., 2] = np.roll(a[..., 2], -dx, axis=1)
    out[..., 3] = np.maximum.reduce([a[..., 3], np.roll(a[..., 3], dx, axis=1), np.roll(a[..., 3], -dx, axis=1)])
    return out


def glitch(frame, t, ctx, amp=7, bursts=3, k=11, on_hits=False, **_):
    """R/B channel split in bursts (gate open `bursts` times per loop), or for
    0.12 s after each hit when on_hits=True. Full-frame colour fringing looks
    great on colourful art and terrible on monochrome high-contrast art — use
    `slice_glitch` there."""
    if on_hits:
        if not any(0 <= t - h < 0.12 for h in ctx["hits"]):
            return frame
        dx = int(round(amp * math.sin(2 * math.pi * 13 * t / D))) or amp
    else:
        if math.sin(2 * math.pi * bursts * t / D) <= 0.55:
            return frame
        dx = int(round(amp * math.sin(2 * math.pi * k * t / D)))
        if dx == 0:
            return frame
    return Image.fromarray(_rgb_split(np.asarray(frame), dx), "RGBA")


def slice_glitch(frame, t, ctx, bands=6, shift=26, split=3, life=0.13, bursts=None, **_):
    """Horizontal strips tear sideways with a tiny channel split inside each strip.
    Fires after each hit, or `bursts` times per loop when no hits exist."""
    hits = ctx["hits"] or [D * i / bursts for i in range(bursts)] if (ctx["hits"] or bursts) else []
    for hi, h in enumerate(hits):
        u = t - h
        if not (0 <= u < life):
            continue
        rng = random.Random(ctx["seed"] * 500 + hi * 10 + int(u * 30))
        a = np.asarray(frame).copy(); out = a.copy()
        for _ in range(bands):
            y = rng.randrange(0, S - 30); hgt = rng.randrange(5, 26); sh = rng.randrange(-shift, shift + 1)
            band = np.roll(a[y:y + hgt], sh, axis=1)
            out[y:y + hgt] = _rgb_split(band, rng.choice([-split, split]))
        frame = Image.fromarray(out, "RGBA")
    return frame


def ghost(frame, t, ctx, **_):
    """Motion trail: blends the previous two frames in. Needs ctx['prev']."""
    prev = ctx.get("prev") or []
    if not prev:
        return frame
    out = frame.copy()
    for i, p in enumerate(reversed(prev[-2:])):
        w = (0.55, 0.28)[i]
        out.alpha_composite(_fade_alpha(p, w))
    out.alpha_composite(frame)
    return out


PRESETS = {
    "sparkle": sparkle, "hearts": hearts, "rain": rain, "rise": rise, "meteors": meteors, "zzz": zzz,
    "sweep": sweep, "scan": scan, "rays": rays, "outline": outline, "glow": glow, "aura": aura,
    "flashbang": flashbang, "flash": flash, "shockwave": shockwave, "bolts": bolts,
    "glitch": glitch, "slice_glitch": slice_glitch, "ghost": ghost,
}


def build(specs):
    fns = []
    for sp in specs or []:
        if isinstance(sp, str):
            sp = {"type": sp}
        name = sp.get("type")
        if name not in PRESETS:
            raise ValueError(f"unknown fx '{name}'. available: {', '.join(sorted(PRESETS))}")
        params = {k: v for k, v in sp.items() if k != "type"}
        fns.append((PRESETS[name], params))

    def apply(frame, t, ctx):
        for fn, params in fns:
            frame = fn(frame, t, ctx, **params)
        return frame
    return apply
