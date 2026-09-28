"""Caption layer: token-streaming text rendered with Pillow.

Letters arrive one at a time (the "typing" look people ask for), each with a
heavy black outline, a 3D extrusion and a gradient fill. Rendering happens at
3x and is downsampled, so edges are smooth at sticker size.

Why not ffmpeg drawtext: it paints a flat colour, cannot do per-letter timing
curves, and does not touch the alpha plane — text over a transparent region
would be invisible. Here the caption is an RGBA layer composited in Python, so
it works over photos and over transparent cut-outs alike.

Glyph safety: outlines and fills are drawn as two passes (all outlines, then
all fills). If each glyph were flattened first, the next glyph's outline would
paint over the previous glyph's strokes — a Korean 하 silently becomes 히.
"""
from __future__ import annotations

import colorsys
import math

import numpy as np
import os
import random
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .const import S, FPS, NF, D

MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 18, 8, 26

# 자막 애니메이션 (이름: 한 줄 설명). sticker_catalog·sanitize 가 여기서 읽는다. 등장(글자별) / 등장(줄) / 루프.
ANIMS = {
    "bounce": "글자가 아래서 튀어 오르며 등장 (오버슛)",
    "pop": "글자가 0→1.25→1 로 팡 커지며 등장 (스프링)",
    "drop": "글자가 위에서 떨어져 착지 때 납작 찌그러짐",
    "flip": "글자가 세로로 뒤집히며 등장 (flipInX)",
    "spin": "글자가 한 바퀴 돌며 커져서 등장",
    "rise": "글자가 아래서 조용히 떠오르며 등장",
    "slide": "글자가 오른쪽에서 미끄러져 들어옴",
    "stamp": "줄 전체가 3배 크기에서 쾅 찍힘 (도장)",
    "punch": "타이핑이 끝나면 줄이 1.1배 한 번 펀치",
    "shake": "타이핑이 끝나면 줄이 잠깐 덜덜",
    "zoom": "타이핑이 끝나면 줄이 1.3→1 로 줌 (집중선 느낌)",
    "shine": "흰 광채가 글자 위를 한 번 지나감",
    "glow": "글자 뒤 네온 블룸이 숨쉼",
    "neon": "네온 간판처럼 서너 번 깜빡이다 켜짐",
    "wave": "글자들이 차례로 위아래 물결",
    "wobble": "글자들이 좌우로 살랑 기울며 흔들림 (루프)",
    "jitter": "글자가 계속 잘게 떨림 (긴장·추위)",
    "rainbow": "글자 색이 무지개로 돌아감 (루프)",
    "karaoke": "글자 색이 왼쪽부터 차례로 밝게 물듦",
    "pulse": "줄 전체가 심장 박동처럼 두 번 쿵쿵",
}
ENTRANCES = {"bounce", "pop", "drop", "flip", "spin", "rise", "slide"}    # 글자별 등장 — 하나만 쓴다

PALETTES = {
    "gold":   dict(top=(255, 250, 205), mid=(255, 205, 60), bottom=(238, 146, 8), extrude=(92, 46, 0)),
    "silver": dict(top=(255, 255, 255), mid=(214, 220, 232), bottom=(140, 150, 170), extrude=(40, 44, 56)),
    "ice":    dict(top=(240, 252, 255), mid=(150, 220, 255), bottom=(60, 150, 235), extrude=(10, 40, 90)),
    "fire":   dict(top=(255, 245, 160), mid=(255, 120, 30), bottom=(210, 30, 20), extrude=(70, 0, 0)),
    "pink":   dict(top=(255, 235, 245), mid=(255, 150, 200), bottom=(230, 60, 140), extrude=(90, 10, 50)),
    "neon":   dict(top=(230, 255, 250), mid=(90, 255, 220), bottom=(20, 200, 190), extrude=(0, 50, 60)),
    "white":  dict(top=(255, 255, 255), mid=(245, 245, 245), bottom=(215, 215, 215), extrude=(50, 50, 50)),
}


@dataclass
class Style:
    size_max: int = 76
    stroke: int = 9
    depth: int = 6
    tracking: int = 3
    step: float = 0.135          # seconds between letters
    delay: float = 0.12
    supersample: int = 3
    palette: str = "gold"
    top: tuple = None
    mid: tuple = None
    bottom: tuple = None
    extrude: tuple = None
    anims: tuple = ("bounce", "punch", "shine")   # ANIMS 의 키들
    position: str = "bottom"     # bottom | top
    typing: bool = True          # False = whole line appears at once

    def colors(self):
        base = dict(PALETTES.get(self.palette, PALETTES["gold"]))
        for k in ("top", "mid", "bottom", "extrude"):
            v = getattr(self, k)
            if v:
                base[k] = tuple(v)
        return base


def _ease_back(u, s=1.70158):
    u -= 1
    return 1 + u * u * ((s + 1) * u + s)


def _tick(t, every=2):
    return (int(round(t * FPS)) % NF) // every


def _hue_palette(h):
    """무지개 자막용 팔레트 (색상 h 0~1)."""
    def c(v, s):
        return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h % 1.0, s, v))
    return {"top": c(1.0, 0.35), "mid": c(1.0, 0.85), "bottom": c(0.75, 1.0), "extrude": c(0.3, 1.0)}


def _amplitudes(anims):
    scale = 1.10 if "punch" in anims else 1.0
    scale = max(scale, 1.3 if "zoom" in anims else 1.0, 1.12 if "pulse" in anims else 1.0)
    return {"pop": 12 if "bounce" in anims else (40 if "drop" in anims else 8), "overshoot": 7 if "bounce" in anims else 0,
            "wave": 7 if "wave" in anims else 0, "shake": 4 if ("shake" in anims or "jitter" in anims) else 0,
            "glow": 16 if ("glow" in anims or "neon" in anims) else 0, "scale": scale}


def _fit(text, font_path, style, amp, max_h_frac=0.55):
    letters = len(text.replace(" ", ""))
    track = style.tracking * max(0, letters - 1)
    usable_w = S - 2 * MARGIN_X - 2 * amp["shake"] - 2 * amp["glow"] - track
    usable_h = (S - MARGIN_TOP - MARGIN_BOTTOM - amp["overshoot"] - amp["wave"] - 2 * amp["shake"] - 2 * amp["glow"]) * max_h_frac
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    size = style.size_max
    while size > 24:
        font = ImageFont.truetype(font_path, size)
        box = probe.textbbox((0, 0), text, font=font, stroke_width=style.stroke)
        w = box[2] - box[0] + style.depth; h = box[3] - box[1] + style.depth
        if w * amp["scale"] <= usable_w and h * amp["scale"] <= usable_h:
            break
        size -= 2
    return size


def _gradient(size, top_y, bottom_y, col):
    """세로 3색 그라데이션 (numpy — 파이썬 픽셀 루프는 1536² 에서 수 초)."""
    h = size[1]
    tt = np.clip((np.arange(h, dtype=np.float32) - top_y) / max(1, bottom_y - top_y), 0, 1)
    top, mid, bot = (np.array(col[k], dtype=np.float32) for k in ("top", "mid", "bottom"))
    k1 = np.clip(tt / 0.55, 0, 1)[:, None]; k2 = np.clip((tt - 0.55) / 0.45, 0, 1)[:, None]
    rows = np.where(tt[:, None] < 0.55, top + (mid - top) * k1, mid + (bot - mid) * k2)
    rgba = np.concatenate([np.repeat(rows[:, None, :], size[0], axis=1), np.full((h, size[0], 1), 255, np.float32)], axis=2)
    return Image.fromarray(rgba.astype(np.uint8), "RGBA")


def _sweep_band(width):
    strip = Image.new("L", (width, 1)); px = strip.load()
    for x in range(width):
        px[x, 0] = int(255 * math.exp(-((x - width / 2) / 46.0) ** 2))
    band = strip.resize((width, S))
    return band.transform((width, S), Image.AFFINE, (1, 0.55, 0, 0, 1, 0), Image.BILINEAR)


def render(text: str, font_path: str, style: Style | None = None) -> tuple:
    """Returns (list of NF RGBA frames, metrics). Frames are full 512 canvases
    with the caption placed at style.position."""
    style = style or Style()
    anims = set(style.anims); amp = _amplitudes(anims); ss = style.supersample; col = style.colors()
    size = _fit(text, font_path, style, amp)
    font = ImageFont.truetype(font_path, size * ss)
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    box = probe.textbbox((0, 0), text, font=font, stroke_width=style.stroke * ss)
    width = (box[2] - box[0]) // ss + style.tracking * max(0, len(text.replace(" ", "")) - 1)
    height = (box[3] - box[1]) // ss
    left = (S - width) // 2
    if style.position == "top":
        top = MARGIN_TOP + amp["glow"] + amp["overshoot"] + 4
    else:
        top = S - MARGIN_BOTTOM - amp["glow"] - height - style.depth
    centre_y = top + height / 2
    metrics = ImageFont.truetype(font_path, size)
    advance = [metrics.getlength(text[:i]) + style.tracking * i for i in range(len(text) + 1)]
    big = (S * ss, S * ss)
    gradient = _gradient(big, top * ss, (top + height) * ss, col)

    def plates(ch, offset):
        x = (left + offset) * ss - box[0]; y = top * ss - box[1]
        back = Image.new("RGBA", big, (0, 0, 0, 0)); d = ImageDraw.Draw(back)
        for k in range(style.depth * ss, 0, -1):
            d.text((x + k * 0.5, y + k), ch, font=font, fill=col["extrude"] + (255,), stroke_width=style.stroke * ss, stroke_fill=(0, 0, 0, 255))
        d.text((x, y), ch, font=font, fill=(0, 0, 0, 255), stroke_width=style.stroke * ss, stroke_fill=(0, 0, 0, 255))
        mask = Image.new("L", big, 0); ImageDraw.Draw(mask).text((x, y), ch, font=font, fill=255)
        front = Image.new("RGBA", big, (0, 0, 0, 0)); front.paste(gradient, (0, 0), mask)
        return back.resize((S, S), Image.LANCZOS), front.resize((S, S), Image.LANCZOS)

    idx = [i for i, ch in enumerate(text) if ch != " "]
    sprites = [plates(text[i], advance[i]) for i in idx]
    typing = style.typing and "stamp" not in anims                 # 도장은 줄 전체가 한 번에
    times = [style.delay + (style.step * j if typing else 0) for j in range(len(idx))]
    typed = (times[-1] + 0.10) if times else 0.0
    band_w = S * 3; band = _sweep_band(band_w); sweep_from, sweep_to = typed + 0.12, D - 0.05

    def _piece(plate, sc=1.0, rot=0.0, flip_y=1.0):
        """플레이트(전체 캔버스)를 글자 상자만 잘라 변환하고 제자리에 되붙인다 (프레임마다 512 전체 변환은 느림)."""
        bb = plate.getbbox()
        if not bb:
            return plate
        crop = plate.crop(bb); w, h = crop.size
        if flip_y != 1.0:
            crop = crop.resize((w, max(1, int(h * abs(flip_y)))), Image.BILINEAR)
            if flip_y < 0:
                crop = crop.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if sc != 1.0:
            crop = crop.resize((max(1, int(w * sc)), max(1, int(crop.height * sc))), Image.BILINEAR)
        if rot:
            crop = crop.rotate(rot, resample=Image.BICUBIC, expand=True)
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        out.alpha_composite(crop, (int(cx - crop.width / 2), int(cy - crop.height / 2)))
        return out

    entrance = next((a for a in ("pop", "drop", "flip", "spin", "rise", "slide") if a in anims), "bounce")
    rnd = random.Random(7)
    jit = [(rnd.uniform(-1, 1), rnd.uniform(-1, 1)) for _ in sprites]
    rainbow_grads = None
    if "rainbow" in anims:
        rainbow_grads = [_gradient((S, S), top, top + height, _hue_palette(i / 6)) for i in range(6)]
    hi_grad = _gradient((S, S), top, top + height, PALETTES["white"]) if "karaoke" in anims else None
    frames = []
    for n in range(NF):
        t = n / FPS
        frame = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        visible = []
        for j, ((back, front), start) in enumerate(zip(sprites, times)):
            if t < start:
                continue
            k = min(1.0, (t - start) / 0.11)
            dy = 0.0; dx = 0.0; sc = 1.0; rot = 0.0; fy = 1.0; alpha = k
            if entrance == "bounce":
                dy = amp["pop"] * (1 - k) - amp["overshoot"] * math.sin(math.pi * min(1, k)) * (1 - k)
            elif entrance == "pop":
                k2 = min(1.0, (t - start) / 0.18); sc = max(0.05, _ease_back(k2, 2.4)); alpha = min(1.0, k2 * 3)
            elif entrance == "drop":
                k2 = min(1.0, (t - start) / 0.16); dy = -amp["pop"] * (1 - k2 * k2)
                land = (t - start) - 0.16
                if 0 <= land < 0.14:
                    q = math.sin(math.pi * land / 0.14); sc = 1.0; fy = 1 - 0.22 * q
                alpha = 1.0
            elif entrance == "flip":
                k2 = min(1.0, (t - start) / 0.2); fy = -1 + 2 * (1 - (1 - k2) ** 2); fy = fy if abs(fy) > 0.05 else 0.05; alpha = 1.0
            elif entrance == "spin":
                k2 = min(1.0, (t - start) / 0.22); e = 1 - (1 - k2) ** 3; rot = 360 * (1 - e); sc = max(0.05, e); alpha = 1.0
            elif entrance == "rise":
                k2 = min(1.0, (t - start) / 0.22); dy = 40 * (1 - (1 - (1 - k2) ** 2)); alpha = k2
            elif entrance == "slide":
                k2 = min(1.0, (t - start) / 0.16); dx = 60 * (1 - k2) ** 2; alpha = 1.0
            if amp["wave"] and t > typed + 0.15:
                ramp = min(1.0, (t - typed - 0.15) / 0.3)
                dy += amp["wave"] * ramp * math.sin(2 * math.pi * (2 * (t - typed) - j * 0.14))
            if "wobble" in anims:
                rot += 6 * math.sin(2 * math.pi * 2 * t / D + j * 0.7); dy += 3 * math.cos(2 * math.pi * 2 * t / D + j * 0.7)
            if "jitter" in anims:
                q = _tick(t); dx += 2.5 * jit[(j + q) % len(jit)][0]; dy += 2.5 * jit[(j * 3 + q) % len(jit)][1]
            visible.append((j, back, front, dx, dy, sc, rot, fy, alpha))
        for layer in (0, 1):
            for j, back, front, dx, dy, sc, rot, fy, alpha in visible:
                plate = (back, front)[layer]
                if layer == 1 and rainbow_grads is not None:
                    mask = front.getchannel("A")
                    plate = Image.new("RGBA", (S, S), (0, 0, 0, 0))
                    plate.paste(rainbow_grads[int(((t / D) + j / 6) * 6) % 6], (0, 0), mask)
                if layer == 1 and hi_grad is not None:
                    prog = (t - typed) / 0.9 if t > typed else -1           # 왼쪽부터 0.9초에 걸쳐 밝게
                    if prog >= j / max(1, len(sprites) - 1):
                        mask = front.getchannel("A")
                        plate = Image.new("RGBA", (S, S), (0, 0, 0, 0))
                        plate.paste(hi_grad, (0, 0), mask)
                if sc != 1.0 or rot or fy != 1.0:
                    plate = _piece(plate, sc, rot, fy)
                if dx or dy:
                    plate = ImageChops.offset(plate, int(round(dx)), int(round(dy)))
                if alpha < 1:
                    plate = plate.copy(); plate.putalpha(plate.getchannel("A").point(lambda v: int(v * alpha)))
                frame.alpha_composite(plate)
        if "glow" in anims or "neon" in anims:
            if "neon" in anims:
                u = t - typed
                on = 1.0 if u > 0.7 or u < 0 else (1.0 if int(u * 18) % 3 != 1 else 0.25)     # 서너 번 깜빡
                pulse = 0.7 * on
                if on < 1:
                    frame.putalpha(frame.getchannel("A").point(lambda v: int(v * 0.6)))
            else:
                pulse = 0.35 + 0.35 * math.sin(2 * math.pi * t / D * 2)
            g = frame.filter(ImageFilter.GaussianBlur(9)); g.putalpha(g.getchannel("A").point(lambda v: int(v * pulse)))
            g.alpha_composite(frame); frame = g
        if "shine" in anims and sweep_from <= t <= sweep_to:
            u = (t - sweep_from) / (sweep_to - sweep_from)
            shift = int(-band_w / 2 + u * (S + band_w / 2))
            crop = band.crop((band_w // 2 - shift, 0, band_w // 2 - shift + S, S))
            mask = ImageChops.multiply(crop, frame.getchannel("A")).point(lambda v: int(v * 0.85))
            hl = Image.new("RGBA", (S, S), (255, 255, 255, 0)); hl.putalpha(mask); frame.alpha_composite(hl)
        line_sc = 1.0; line_rot = 0.0
        if "punch" in anims and typed <= t <= typed + 0.42:
            u = (t - typed) / 0.42; line_sc = 1 + 0.10 * math.sin(math.pi * u)
        if "zoom" in anims and typed <= t <= typed + 0.35:
            u = (t - typed) / 0.35; line_sc = 1 + 0.3 * (1 - u) ** 2
        if "pulse" in anims:
            u = ((t - typed) / 0.9) % 1.0 if t > typed else -1
            for st in (0.0, 0.22):
                if st <= u < st + 0.18:
                    line_sc *= 1 + 0.12 * math.sin(math.pi * (u - st) / 0.18)
        if "stamp" in anims:
            u = t - typed
            if u < 0:
                frame = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            elif u < 0.14:
                q = 1 - (u / 0.14) ** 2; line_sc = 1 + 2.0 * q; line_rot = -10 * q
        if line_sc != 1.0 or line_rot:
            grown = frame.resize((int(S * line_sc),) * 2, Image.LANCZOS)
            if line_rot:
                grown = grown.rotate(line_rot, resample=Image.BICUBIC)
            frame = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            frame.paste(grown, (int(round(S / 2 - S / 2 * line_sc)), int(round(centre_y - centre_y * line_sc))))
        if "stamp" in anims and 0.14 <= t - typed < 0.22:
            frame = ImageChops.offset(frame, int(6 * math.sin(2 * math.pi * 12 * t)), int(4 * math.cos(2 * math.pi * 9 * t)))
        if amp["shake"] and "shake" in anims and typed <= t <= typed + 0.5:
            frame = ImageChops.offset(frame, int(round(amp["shake"] * math.sin(2 * math.pi * 12 * t))),
                                      int(round(amp["shake"] * 0.6 * math.cos(2 * math.pi * 9 * t))))
        frames.append(frame)

    info = {"size": size, "width": width, "height": height, "top": top, "left": left,
            "advance": advance, "bbox": box, "typed_at": typed, "supersample": ss,
            "band_top": top - amp["overshoot"] - amp["glow"] - 8,
            "band_bottom": min(S, top + height + style.depth + amp["glow"] + 8)}
    return frames, info


def glyph_check(text, font_path, style, threshold=0.6) -> dict:
    """Render motion-free and measure how much of each glyph's own area came out
    dark. >threshold % means a neighbour's outline ate part of the letter."""
    from dataclasses import replace
    frames, info = render(text, font_path, replace(style, anims=(), typing=False))
    luma = frames[-1].convert("L"); ss = info["supersample"]; big = (S * ss, S * ss)
    font = ImageFont.truetype(font_path, info["size"] * ss); box = info["bbox"]
    worst, damaged = 0.0, []
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        x = (info["left"] + info["advance"][i]) * ss - box[0]; y = info["top"] * ss - box[1]
        mask = Image.new("L", big, 0); ImageDraw.Draw(mask).text((x, y), ch, font=font, fill=255)
        mask = mask.resize((S, S), Image.LANCZOS).point(lambda v: 255 if v > 160 else 0)
        area = sum(mask.histogram()[255:])
        if not area:
            continue
        dark = Image.composite(luma.point(lambda v: 255 if v < 70 else 0), Image.new("L", (S, S), 0), mask)
        pct = 100.0 * sum(dark.histogram()[255:]) / area
        worst = max(worst, pct)
        if pct > threshold:
            damaged.append((ch, round(pct, 2)))
    return {"worst": round(worst, 2), "damaged": damaged, "ok": not damaged}
