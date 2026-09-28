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

from .const import S, D, FPS, NF

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
SPRITE_MAX = 64                                                 # 크기·색은 AI 가 고름 → 캐시는 몇 개만


def sprite(kind: str, size: int, color=None) -> Image.Image:
    key = (kind, size, color)
    if key not in _CACHE:
        if len(_CACHE) >= SPRITE_MAX:
            _CACHE.clear()
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
    """반짝이 별 (subset 로 개수 줄임, 0~9)."""
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
    """하트가 떠오름."""
    for sz, x, phase, speed, spin in _RISING:
        u = (t * speed / D + phase) % 1.0
        y = S - u * (S + sz)
        spr = sprite("heart", sz, tuple(color) if color else None).rotate(spin * 30 * math.sin(2 * math.pi * t / D), resample=Image.BICUBIC)
        frame.alpha_composite(spr, (x, int(y)))
    return frame


def rain(frame, t, ctx, image=None, rising=False, **_):
    """(막음) 파일 이미지가 떨어짐."""
    if not image:
        raise ValueError("fx 'rain'/'rise' needs image=<png with alpha>")
    for sz, x, phase, speed, spin in _FALLING:
        u = (t * speed / D + phase) % 1.0
        y = S - u * (S + sz) if rising else -sz + u * (S + sz)
        spr = sprite(image, sz).rotate(spin * 360 * t / D, resample=Image.BICUBIC, expand=False)
        frame.alpha_composite(spr, (x, int(y)))
    return frame


def rise(frame, t, ctx, image=None, **k):
    """(막음) 파일 이미지가 떠오름."""
    return rain(frame, t, ctx, image=image, rising=True)


def meteors(frame, t, ctx, paths=None, **_):
    """별똥별 3개가 위쪽을 가로지름."""
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
    """Zzz 가 origin 에서 떠오름 (잘자)."""
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
POLAR_MAX = 8                                                   # 중심은 AI 가 고름 → 격자(2MB) 캐시는 몇 개만


def _polar(center):
    """rays 용 (각도, 페이드) — 중심마다 한 번만 계산."""
    if center not in _POLAR:
        if len(_POLAR) >= POLAR_MAX:
            _POLAR.clear()
        cx, cy = S * center[0], S * center[1]
        r = np.hypot(_XX - cx, _YY - cy)
        _POLAR[center] = (np.arctan2(_YY - cy, _XX - cx), np.clip(1 - r / (S * 0.62), 0, 1) * np.clip(r / 40, 0, 1))
    return _POLAR[center]

def sweep(frame, t, ctx, strength=0.5, width=0.09, color=(255, 250, 225), **_):
    """대각선 광채가 한 번 지나감."""
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
    """가로 광선이 위→아래로 훑음 (전송)."""
    y0 = -height + ((t / D) % 1.0) * (S + height)
    yy = np.arange(S)[:, None]
    band = np.exp(-((yy - (y0 + height / 2)) / (height / 3.2)) ** 2)
    a = np.asarray(frame).astype(np.float32)
    a[..., :3] = np.clip(a[..., :3] + (band * alpha * 255)[..., None] * (a[..., 3:4] / 255.0), 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def rays(frame, t, ctx, spokes=12, strength=0.22, color=(255, 244, 200), center=(0.5, 0.42), **_):
    """뒤에서 도는 빛살 (center 0~1 비율)."""
    base_ang, fade = _polar(tuple(center))
    ang = base_ang + 2 * math.pi * (t / D) / spokes
    wedge = (np.sin(ang * spokes) > 0.55).astype(np.float32)
    layer = Image.new("RGBA", frame.size, tuple(color) + (0,))
    layer.putalpha(Image.fromarray((wedge * fade * strength * 255).astype(np.uint8), "L"))
    layer.alpha_composite(frame)
    return layer


def outline(frame, t, ctx, color=(255, 255, 255), width=5, pulse=0, **_):
    """실루엣 바깥 흰 테두리 (width px, pulse 로 숨쉼)."""
    a = frame.getchannel("A")
    w = width + (int(round(pulse * math.sin(2 * math.pi * 2 * t / D))) if pulse else 0)
    ring = Image.new("RGBA", frame.size, tuple(color) + (0,))
    ring.putalpha(_dilate(a, w))
    ring.alpha_composite(frame)
    return ring


def glow(frame, t, ctx, color=(255, 255, 255), radius=10, strength=0.6, pulse=True, **_):
    """뒤에서 은은한 빛 (color, radius, strength)."""
    k = strength * (0.6 + 0.4 * math.sin(2 * math.pi * 2 * t / D)) if pulse else strength
    a = frame.getchannel("A").filter(ImageFilter.GaussianBlur(radius))
    layer = Image.new("RGBA", frame.size, tuple(color) + (0,))
    layer.putalpha(a.point(lambda v: int(v * k)))
    layer.alpha_composite(frame)
    return layer


def aura(frame, t, ctx, flicker=0.06, flare=0.32, tau=0.12, **_):
    """발광부(반투명)만 깜빡이고 타격 때 타오름 (glow 키잉 그림용, flare ≤0.35)."""
    g = 1 + flicker * math.sin(2 * math.pi * 7 * t / D) + flicker * 0.7 * math.sin(2 * math.pi * 11 * t / D + 1.3)
    g += sum(flare * _imp(t, h, tau) for h in ctx["hits"])
    if abs(g - 1) < 0.02:
        return frame
    a = np.asarray(frame).astype(np.float32); al = a[..., 3:4] / 255.0
    w = np.clip((1 - al) * 1.6, 0, 1) * (al > 0.02)
    a[..., :3] = np.clip(a[..., :3] * (1 + (g - 1) * w), 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def flashbang(frame, t, ctx, amount=55, tau=0.10, **_):
    """타격 때 전체가 번쩍 (amount ≤60)."""
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
    """타격 때 at 지점에 섬광 별 (총구)."""
    for h in ctx["hits"]:
        u = t - h
        if 0 <= u < 0.10:
            k = 1 - u / 0.10; sz = int(size * (0.7 + 1.6 * k))
            spr = sprite("star", 56).resize((sz, sz), Image.LANCZOS).rotate(37 * u * 40, resample=Image.BICUBIC)
            frame.alpha_composite(_fade_alpha(spr, min(1, k * 1.4)), (at[0] - sz // 2, at[1] - sz // 2))
    return frame


def shockwave(frame, t, ctx, center=(256, 236), color=(215, 205, 255), r0=55, r1=315, life=0.38, **_):
    """타격 때 center 에서 퍼지는 충격파 링."""
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
    """타격 때 center 주변 번개 볼트 (seed 고정)."""
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
    """R/B 채널 어긋남 글리치 (bursts 번, 또는 on_hits) — 컬러 그림용."""
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
    """가로 띠가 찢어지는 글리치 (흑백·고대비 그림용)."""
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
    """잔상 (이전 두 프레임이 흐릿하게 따라옴)."""
    prev = ctx.get("prev") or []
    if not prev:
        return frame
    out = frame.copy()
    for i, p in enumerate(reversed(prev[-2:])):
        w = (0.55, 0.28)[i]
        out.alpha_composite(_fade_alpha(p, w))
    out.alpha_composite(frame)
    return out


# ---------------------------------------------------------------- 추가 (인기 팩 연출·밈·만화 효과; 전부 루프 안전)

def _hits(ctx, default=(0.30,)):
    """타격 시각. 충격 모션이 없으면 기본 시각(루프 안에서 끝나게)."""
    return ctx["hits"] or list(default)


def _rng(ctx, name, extra=0):
    return random.Random(f"{ctx['seed']}:{name}:{extra}")


def _tick(t, every=2):
    """t → 재시드 번호. 루프 길이로 접어서(프레임 89 = 프레임 0) 이음새가 없다."""
    return (int(round(t * FPS)) % NF) // every


def _dilate(alpha, width):
    """알파 팽창 (scipy maximum_filter — PIL MaxFilter 보다 10배 빠름)."""
    from scipy import ndimage as ndi
    return Image.fromarray(ndi.maximum_filter(np.asarray(alpha), size=2 * int(width) + 1), "L")


def _ring(frame, width):
    """실루엣 바깥 띠 (width px) 알파."""
    a = frame.getchannel("A")
    return ImageChops.subtract(_dilate(a, width), a)


def _draw_text(layer, text, xy, size, fill, ctx, angle=0.0, stroke=6, stroke_fill=(0, 0, 0, 255)):
    """짧은 글자를 별도 레이어에 그려 회전해 붙임 (PIL 만 — 글은 ffmpeg 인자에 안 감)."""
    font = ImageFont.truetype(ctx["font"], int(size))
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    box = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
    w, h = box[2] - box[0] + 8, box[3] - box[1] + 8
    piece = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(piece).text((4 - box[0], 4 - box[1]), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
    if angle:
        piece = piece.rotate(angle, resample=Image.BICUBIC, expand=True)
    layer.alpha_composite(piece, (int(xy[0] - piece.width / 2), int(xy[1] - piece.height / 2)))


def speedlines(frame, t, ctx, count=70, color=(255, 255, 255), strength=0.8, center=(256, 230), **_):
    """스피드라인 (center 에서 바깥으로 뻗는 선, 2프레임마다 재배치 — 만화 속도감)."""
    rng = _rng(ctx, "speed", _tick(t))          # 2프레임마다 재배치, 루프로 접혀 이음새 없음
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    cx, cy = center
    for _ in range(int(count)):
        a = rng.uniform(0, 2 * math.pi); r0 = rng.uniform(150, 230); r1 = r0 + rng.uniform(60, 260)
        d.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)), (cx + r1 * math.cos(a), cy + r1 * math.sin(a))],
               fill=tuple(color) + (int(255 * strength * rng.uniform(0.4, 1)),), width=rng.choice([1, 2, 3]))
    layer.alpha_composite(frame)
    return layer


def focus_lines(frame, t, ctx, count=90, color=(0, 0, 0), center=(256, 230), strength=0.9, **_):
    """집중선 (테두리에서 center 쪽으로 모이는 가는 삼각형, 만화 강조)."""
    rng = _rng(ctx, "focus", _tick(t))
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    cx, cy = center
    for _ in range(int(count)):
        a = rng.uniform(0, 2 * math.pi); inner = rng.uniform(120, 220); w = rng.uniform(0.004, 0.02)
        far = 420
        p0 = (cx + far * math.cos(a - w), cy + far * math.sin(a - w)); p1 = (cx + far * math.cos(a + w), cy + far * math.sin(a + w))
        p2 = (cx + inner * math.cos(a), cy + inner * math.sin(a))
        d.polygon([p0, p1, p2], fill=tuple(color) + (int(255 * strength),))
    layer.alpha_composite(frame)
    return layer


def deep_fry(frame, t, ctx, amount=1.0, **_):
    """딥프라이드 밈 (채도·대비 과다 + 노이즈 + 붉은 기, 루프 내내 일정)."""
    from PIL import ImageEnhance
    a = frame.getchannel("A")
    rgb = frame.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(1 + 1.6 * amount)
    rgb = ImageEnhance.Contrast(rgb).enhance(1 + 0.9 * amount)
    rgb = rgb.filter(ImageFilter.UnsharpMask(2, int(150 * amount), 0))
    arr = np.asarray(rgb).astype(np.int16)
    nz = np.random.RandomState(ctx["seed"] * 7 + _tick(t)).randint(-int(14 * amount), int(14 * amount) + 1, arr.shape[:2])
    arr = np.clip(arr + nz[..., None] + np.array([12, 0, -10]) * amount, 0, 255).astype(np.uint8)
    out = Image.fromarray(arr, "RGB").convert("RGBA"); out.putalpha(a)
    return out


def sfx_text(frame, t, ctx, text="쾅!", at=(380, 110), size=86, color=(255, 230, 40), angle=-12, **_):
    """만화 효과음 글자 (text ≤8자, at 에 도장처럼 팍 찍혔다 사라짐, 타격 동기)."""
    text = str(text)[:8] or "!"
    for h in _hits(ctx, (0.25,)):
        u = t - h
        if not (0 <= u < 1.0):
            continue
        k = min(1.0, u / 0.12); sc = 2.2 - 1.2 * (1 - (1 - k) ** 3)
        alpha = 1.0 if u < 0.7 else max(0.0, 1 - (u - 0.7) / 0.3)
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        _draw_text(layer, text, at, size * sc, tuple(color) + (255,), ctx, angle=angle + 6 * (1 - k))
        frame.alpha_composite(_fade_alpha(layer, alpha))
    return frame


def bubble(frame, t, ctx, text="ㅋㅋ", at=(400, 90), size=44, color=(255, 255, 255), **_):
    """말풍선 (text ≤8자, at 위치에 통통 튀며 나타남, 루프 후반에 사라짐)."""
    text = str(text)[:8] or "!"
    u = t / D
    if u < 0.10 or u > 0.92:
        return frame
    k = min(1.0, (u - 0.10) / 0.12); sc = 0.2 + 0.8 * (1 + 1.7 * (k - 1) ** 3 + 1.7 * (k - 1) ** 2 * (k - 1) + 1)  if False else 0.2 + 0.8 * _ease_back(k)
    alpha = 1.0 if u < 0.82 else max(0.0, 1 - (u - 0.82) / 0.10)
    font = ImageFont.truetype(ctx["font"], int(size))
    d0 = ImageDraw.Draw(Image.new("L", (1, 1))); box = d0.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    w, h = int((tw + 44) * sc), int((th + 30) * sc)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    x0, y0 = at[0] - w // 2, at[1] - h // 2
    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=int(h * 0.45), fill=tuple(color) + (255,), outline=(30, 30, 30, 255), width=3)
    d.polygon([(at[0] - int(10 * sc), y0 + h - 2), (at[0] + int(14 * sc), y0 + h - 2), (at[0] - int(26 * sc), y0 + h + int(22 * sc))],
              fill=tuple(color) + (255,), outline=(30, 30, 30, 255))
    if sc > 0.5:
        f2 = ImageFont.truetype(ctx["font"], max(8, int(size * sc)))
        d.text((at[0] - tw * sc / 2 - box[0] * sc, at[1] - th * sc / 2 - box[1] * sc), text, font=f2, fill=(20, 20, 20, 255))
    frame.alpha_composite(_fade_alpha(layer, alpha))
    return frame


def _ease_back(u, s=1.70158):
    u -= 1
    return 1 + u * u * ((s + 1) * u + s)


def sparkle_border(frame, t, ctx, width=6, color=(255, 250, 200), count=14, **_):
    """반짝이 테두리 (실루엣 가장자리를 따라 별이 돌아가며 반짝임)."""
    ring = _ring(frame, width)
    ys, xs = np.nonzero(np.asarray(ring) > 100)
    if len(xs) == 0:
        return frame
    rng = _rng(ctx, "border")
    idx = rng.sample(range(len(xs)), min(int(count) * 3, len(xs)))
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for j, i in enumerate(idx):
        ph = (t / D + j / len(idx)) % 1.0
        a = max(0.0, math.sin(math.pi * ((ph * 3) % 1.0)))    # 각각 루프에 3번 반짝
        if a < 0.05:
            continue
        sz = 18 + 10 * a
        spr = sprite("star", 28, tuple(color)).resize((int(sz), int(sz)), Image.LANCZOS)
        layer.alpha_composite(_fade_alpha(spr, a), (int(xs[i] - sz / 2), int(ys[i] - sz / 2)))
    frame.alpha_composite(layer)
    return frame


def _fall(frame, t, ctx, name, draw_one, count=28, speed=1, sway=12, up=False):
    """떨어지거나(up=False) 떠오르는 입자 공통 (루프 한 번에 정확히 speed 칸 → 이음새 없음)."""
    rng = _rng(ctx, name)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    for i in range(int(count)):
        x0 = rng.uniform(0, S); ph = rng.uniform(0, 1); sz = rng.uniform(0.6, 1.4); spin = rng.choice([-1, 1])   # 정수 바퀴 = 이음새 없음
        u = (t * speed / D + ph) % 1.0
        y = -60 + u * (S + 120) if not up else S + 60 - u * (S + 120)
        x = x0 + sway * math.sin(2 * math.pi * (t / D + ph) * 2)
        draw_one(d, x, y, sz, spin * 360 * t / D + ph * 360, rng)
    frame.alpha_composite(layer)
    return frame


def confetti(frame, t, ctx, count=32, **_):
    """색종이 비 (알록달록 조각이 흔들리며 떨어짐)."""
    cols = [(255, 80, 90), (80, 200, 255), (255, 220, 60), (120, 255, 140), (220, 120, 255)]

    def one(d, x, y, sz, ang, rng):
        c = cols[int(sz * 10) % len(cols)]; w, h = 14 * sz, 7 * sz * abs(math.cos(math.radians(ang)))
        d.rectangle([x - w / 2, y - h / 2, x + w / 2, y + h / 2], fill=c + (230,))
    return _fall(frame, t, ctx, "confetti", one, count=count, speed=1)


def snow(frame, t, ctx, count=40, **_):
    """눈 (흰 점이 살랑살랑 내려옴)."""
    def one(d, x, y, sz, ang, rng):
        r = 3 + 4 * sz; d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 210))
    return _fall(frame, t, ctx, "snow", one, count=count, speed=1, sway=16)


def petals(frame, t, ctx, count=22, color=(255, 170, 200), **_):
    """꽃잎 (분홍 꽃잎이 빙글 돌며 떨어짐)."""
    def one(d, x, y, sz, ang, rng):
        w, h = 12 * sz, 6 * sz * (0.3 + 0.7 * abs(math.cos(math.radians(ang))))
        d.ellipse([x - w, y - h, x + w, y + h], fill=tuple(color) + (220,))
    return _fall(frame, t, ctx, "petals", one, count=count, speed=1, sway=20)


def bubbles(frame, t, ctx, count=18, **_):
    """방울 (투명한 동그라미가 떠오름)."""
    def one(d, x, y, sz, ang, rng):
        r = 6 + 10 * sz; d.ellipse([x - r, y - r, x + r, y + r], outline=(200, 240, 255, 220), width=3)
        d.ellipse([x - r * 0.5, y - r * 0.6, x - r * 0.2, y - r * 0.3], fill=(255, 255, 255, 200))
    return _fall(frame, t, ctx, "bubbles", one, count=count, speed=1, sway=10, up=True)


def money(frame, t, ctx, count=16, **_):
    """돈비 (초록 지폐가 팔랑이며 쏟아짐)."""
    font = ImageFont.truetype(ctx["font"], 14)

    def one(d, x, y, sz, ang, rng):
        w, h = 30 * sz, 15 * sz * (0.25 + 0.75 * abs(math.cos(math.radians(ang))))
        d.rectangle([x - w, y - h, x + w, y + h], fill=(70, 160, 80, 240), outline=(20, 70, 30, 255), width=2)
        if h > 8:
            d.text((x - 6, y - 8), "$", font=font, fill=(230, 255, 210, 255))
    return _fall(frame, t, ctx, "money", one, count=count, speed=1, sway=14)


def laser(frame, t, ctx, points=((200, 200), (300, 200)), color=(255, 40, 40), angle=20, length=420, **_):
    """레이저 (points 각 지점에서 angle 방향으로 빔, 깜빡이는 굵기 — 눈에서 레이저)."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    w = 6 + 3 * math.sin(2 * math.pi * 6 * t / D)
    rad = math.radians(angle)
    for (x, y) in points:
        end = (x + length * math.cos(rad), y + length * math.sin(rad))
        d.line([(x, y), end], fill=tuple(color) + (255,), width=int(w * 3))
    glow_l = layer.filter(ImageFilter.GaussianBlur(6))
    core = Image.new("RGBA", (S, S), (0, 0, 0, 0)); dc = ImageDraw.Draw(core)
    for (x, y) in points:
        end = (x + length * math.cos(rad), y + length * math.sin(rad))
        dc.line([(x, y), end], fill=(255, 255, 255, 255), width=int(w))
        dc.ellipse([x - 8, y - 8, x + 8, y + 8], fill=(255, 255, 255, 255))
    frame.alpha_composite(glow_l); frame.alpha_composite(core)
    return frame


def vignette(frame, t, ctx, strength=0.55, pulse=0.15, **_):
    """비네트 (가장자리가 어두워지고 살짝 숨쉼, 실루엣 안쪽만)."""
    r = np.hypot(_XX - S / 2, _YY - S / 2) / (S * 0.72)
    k = strength * (1 + pulse * math.sin(2 * math.pi * t / D))
    dark = np.clip(1 - k * np.clip(r, 0, 1) ** 2, 0, 1)
    a = np.asarray(frame).astype(np.float32)
    a[..., :3] *= dark[..., None]
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def strobe(frame, t, ctx, flashes=3, gain=1.5, **_):
    """스트로브 (루프에 flashes 번, 2프레임씩 밝게 번쩍 — 알파는 그대로)."""
    n = int(round(t * FPS)) % NF; period = NF // max(1, int(flashes))
    if n % period not in (0, 1):
        return frame
    a = np.asarray(frame).astype(np.float32)
    a[..., :3] = np.clip(a[..., :3] * gain, 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def shadow(frame, t, ctx, dx=10, dy=12, blur=8, opacity=0.5, **_):
    """그림자 (실루엣 뒤에 흐린 검은 그림자 — 깊이감)."""
    a = frame.getchannel("A").filter(ImageFilter.GaussianBlur(blur))
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh.putalpha(a.point(lambda v: int(v * opacity)))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.alpha_composite(sh, (int(dx), int(dy))); out.alpha_composite(frame)
    return out


def neon_edge(frame, t, ctx, color=(255, 80, 200), width=4, pulse=0.3, **_):
    """네온 엣지 (실루엣 가장자리가 빛나는 색 테두리 + 블룸, 숨쉬듯 펄스)."""
    k = 1 - pulse * (0.5 - 0.5 * math.cos(2 * math.pi * 2 * t / D))
    ring = _ring(frame, width)
    glow_l = Image.new("RGBA", (S, S), tuple(color) + (0,)); glow_l.putalpha(ring.filter(ImageFilter.GaussianBlur(8)).point(lambda v: int(min(255, v * 1.6) * k)))
    core = Image.new("RGBA", (S, S), tuple(int(c * 0.5 + 127) for c in color) + (0,)); core.putalpha(ring.point(lambda v: int(v * k)))
    glow_l.alpha_composite(frame); glow_l.alpha_composite(core)
    return glow_l


def pixelate(frame, t, ctx, block=14, life=0.3, **_):
    """모자이크 폭발 (타격 직후 굵은 픽셀이 됐다가 되돌아옴)."""
    for h in _hits(ctx, (0.30,)):
        u = t - h
        if 0 <= u < life:
            b = max(2, int(block * (1 - u / life)))
            small = frame.resize((S // b, S // b), Image.NEAREST)
            return small.resize((S, S), Image.NEAREST)
    return frame


def wave(frame, t, ctx, amp=8, waves=2, **_):
    """물결 (가로줄이 sin 으로 좌우로 흔들리는 젤리 출렁임)."""
    a = np.asarray(frame)
    out = np.empty_like(a)
    ys = np.arange(S)
    shifts = (amp * np.sin(2 * math.pi * (waves * ys / S + t / D))).astype(int)
    for y in range(S):
        out[y] = np.roll(a[y], shifts[y], axis=0)
    return Image.fromarray(out, "RGBA")


def scanlines(frame, t, ctx, gap=3, dark=0.35, roll=True, **_):
    """CRT 스캔라인 (어두운 가로줄이 천천히 흘러내림)."""
    a = np.asarray(frame).astype(np.float32)
    off = int(t / D * gap * 10) % gap if roll else 0          # 루프에 정수 번 흐름
    rows = ((np.arange(S) + off) % gap == 0)
    a[rows, :, :3] *= (1 - dark)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def rainbow_outline(frame, t, ctx, width=6, **_):
    """무지개 테두리 (색이 한 바퀴 도는 두꺼운 테두리 — 그림 색은 안 건드림)."""
    ring = np.asarray(_ring(frame, width)).astype(np.float32) / 255.0
    hue = ((np.arctan2(_YY - S / 2, _XX - S / 2) / (2 * math.pi)) + t / D) % 1.0
    hsv = np.stack([hue * 255, np.full_like(hue, 255), np.full_like(hue, 255)], axis=2).astype(np.uint8)
    rgb = Image.fromarray(hsv, "HSV").convert("RGB")
    layer = rgb.convert("RGBA"); layer.putalpha(Image.fromarray((ring * 255).astype(np.uint8), "L"))
    layer.alpha_composite(frame)
    return layer


def flicker(frame, t, ctx, depth=0.5, rate=5, **_):
    """깜빡임 (유령처럼 투명도가 오르내림, 루프에 rate 번)."""
    k = 1 - depth * (0.5 - 0.5 * math.cos(2 * math.pi * rate * t / D))
    return _fade_alpha(frame, k)


def sweat(frame, t, ctx, at=(330, 150), size=26, **_):
    """땀방울 (at 에서 파란 물방울이 흘러내리다 사라짐, 루프에 두 번)."""
    for k0 in (0.05, 0.55):
        u = (t / D - k0) % 1.0
        if u > 0.4:
            continue
        y = at[1] + 70 * (u / 0.4) ** 1.5; a = int(255 * (1 - (u / 0.4) ** 3))
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
        r = size / 2
        d.ellipse([at[0] - r, y - r, at[0] + r, y + r], fill=(120, 190, 255, a), outline=(40, 90, 200, a), width=2)
        d.polygon([(at[0] - r * 0.8, y - r * 0.4), (at[0] + r * 0.8, y - r * 0.4), (at[0], y - r * 1.9)], fill=(120, 190, 255, a))
        frame.alpha_composite(layer)
    return frame


def anger(frame, t, ctx, at=(370, 110), size=30, color=(230, 40, 60), **_):
    """분노 마크 (💢 모양이 at 에서 펄떡임)."""
    k = 1 + 0.15 * math.sin(2 * math.pi * 3 * t / D)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    r = size * k
    for ang in (45, 135, 225, 315):
        a = math.radians(ang)
        p = [(at[0] + r * 0.35 * math.cos(a), at[1] + r * 0.35 * math.sin(a)),
             (at[0] + r * math.cos(a - 0.25), at[1] + r * math.sin(a - 0.25)),
             (at[0] + r * 1.1 * math.cos(a), at[1] + r * 1.1 * math.sin(a)),
             (at[0] + r * math.cos(a + 0.25), at[1] + r * math.sin(a + 0.25))]
        d.polygon(p, fill=tuple(color) + (255,), outline=(60, 0, 10, 255))
    frame.alpha_composite(layer)
    return frame


def mirror(frame, t, ctx, spread=60, **_):
    """분신 (좌우로 흐릿한 분신이 벌어졌다 합쳐짐)."""
    k = math.sin(math.pi * ((2 * t / D) % 1.0))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for sgn in (-1, 1):
        out.alpha_composite(_fade_alpha(frame, 0.45), (int(sgn * spread * k), 0))
    out.alpha_composite(frame)
    return out


def impact(frame, t, ctx, frames=2, **_):
    """임팩트 프레임 (타격 순간 1~2장 흑백 반전 — 만화 타격감)."""
    for h in _hits(ctx, (0.30,)):
        if 0 <= t - h < frames / FPS:
            rgb = frame.convert("L").point(lambda v: 255 if v < 128 else 0).convert("RGB")
            out = rgb.convert("RGBA"); out.putalpha(frame.getchannel("A"))
            return out
    return frame


PRESETS = {
    "sparkle": sparkle, "hearts": hearts, "rain": rain, "rise": rise, "meteors": meteors, "zzz": zzz,
    "sweep": sweep, "scan": scan, "rays": rays, "outline": outline, "glow": glow, "aura": aura,
    "flashbang": flashbang, "flash": flash, "shockwave": shockwave, "bolts": bolts,
    "glitch": glitch, "slice_glitch": slice_glitch, "ghost": ghost,
    "speedlines": speedlines, "focus_lines": focus_lines, "deep_fry": deep_fry, "sfx_text": sfx_text, "bubble": bubble,
    "sparkle_border": sparkle_border, "confetti": confetti, "snow": snow, "petals": petals, "bubbles": bubbles, "money": money,
    "laser": laser, "vignette": vignette, "strobe": strobe, "shadow": shadow, "neon_edge": neon_edge, "pixelate": pixelate,
    "wave": wave, "scanlines": scanlines, "rainbow_outline": rainbow_outline, "flicker": flicker, "sweat": sweat, "anger": anger,
    "mirror": mirror, "impact": impact,
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
