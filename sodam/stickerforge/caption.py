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

import math
import os
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .const import S, FPS, NF, D

MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 18, 8, 26

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
    anims: tuple = ("bounce", "punch", "shine")
    position: str = "bottom"     # bottom | top
    typing: bool = True          # False = whole line appears at once

    def colors(self):
        base = dict(PALETTES.get(self.palette, PALETTES["gold"]))
        for k in ("top", "mid", "bottom", "extrude"):
            v = getattr(self, k)
            if v:
                base[k] = tuple(v)
        return base


def _amplitudes(anims):
    return {"pop": 12 if "bounce" in anims else 8, "overshoot": 7 if "bounce" in anims else 0,
            "wave": 7 if "wave" in anims else 0, "shake": 4 if "shake" in anims else 0,
            "glow": 16 if "glow" in anims else 0, "scale": 1.10 if "punch" in anims else 1.0}


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
    grad = Image.new("RGBA", size, (0, 0, 0, 0)); px = grad.load()
    for y in range(size[1]):
        t = min(1.0, max(0.0, (y - top_y) / max(1, bottom_y - top_y)))
        if t < 0.55:
            c = tuple(int(a + (b - a) * (t / 0.55)) for a, b in zip(col["top"], col["mid"]))
        else:
            c = tuple(int(a + (b - a) * ((t - 0.55) / 0.45)) for a, b in zip(col["mid"], col["bottom"]))
        row = c + (255,)
        for x in range(size[0]):
            px[x, y] = row
    return grad


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
    times = [style.delay + (style.step * j if style.typing else 0) for j in range(len(idx))]
    typed = (times[-1] + 0.10) if times else 0.0
    band_w = S * 3; band = _sweep_band(band_w); sweep_from, sweep_to = typed + 0.12, D - 0.05

    frames = []
    for n in range(NF):
        t = n / FPS
        frame = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        visible = []
        for j, ((back, front), start) in enumerate(zip(sprites, times)):
            if t < start:
                continue
            k = min(1.0, (t - start) / 0.11)
            dy = amp["pop"] * (1 - k) - amp["overshoot"] * math.sin(math.pi * min(1, k)) * (1 - k)
            if amp["wave"] and t > typed + 0.15:
                ramp = min(1.0, (t - typed - 0.15) / 0.3)
                dy += amp["wave"] * ramp * math.sin(2 * math.pi * (2 * (t - typed) - j * 0.14))
            visible.append((back, front, int(round(dy)), k))
        for layer in (0, 1):
            for back, front, dy, k in visible:
                plate = (back, front)[layer]
                if dy:
                    plate = ImageChops.offset(plate, 0, dy)
                if k < 1:
                    plate = plate.copy(); plate.putalpha(plate.getchannel("A").point(lambda v: int(v * k)))
                frame.alpha_composite(plate)
        if "glow" in anims:
            pulse = 0.35 + 0.35 * math.sin(2 * math.pi * t / D * 2)
            g = frame.filter(ImageFilter.GaussianBlur(9)); g.putalpha(g.getchannel("A").point(lambda v: int(v * pulse)))
            g.alpha_composite(frame); frame = g
        if "shine" in anims and sweep_from <= t <= sweep_to:
            u = (t - sweep_from) / (sweep_to - sweep_from)
            shift = int(-band_w / 2 + u * (S + band_w / 2))
            crop = band.crop((band_w // 2 - shift, 0, band_w // 2 - shift + S, S))
            mask = ImageChops.multiply(crop, frame.getchannel("A")).point(lambda v: int(v * 0.85))
            hl = Image.new("RGBA", (S, S), (255, 255, 255, 0)); hl.putalpha(mask); frame.alpha_composite(hl)
        if amp["scale"] > 1 and typed <= t <= typed + 0.42:
            u = (t - typed) / 0.42; sc = 1 + (amp["scale"] - 1) * math.sin(math.pi * u)
            grown = frame.resize((int(S * sc),) * 2, Image.LANCZOS)
            frame = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            frame.paste(grown, (int(round(S / 2 - S / 2 * sc)), int(round(centre_y - centre_y * sc))))
        if amp["shake"] and typed <= t <= typed + 0.5:
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
