"""Frame renderer + encoder.

Quality rules baked in here:
  * the master is kept premultiplied so transparent black never bleeds into edges
  * exactly one resample per frame: rotate/scale/translate and 1024->512 in one affine
  * unpremultiply right before effects and saving
  * VP9 two-pass with alpha_mode=1, no alt-ref frames, and a bitrate ladder that
    walks down until the file fits (the alpha plane is not counted by -b:v, so
    transparent stickers need less than you'd guess)
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .const import S, FPS, NF, D, MASTER, ffmpeg
from . import motions as M
from . import fx as FX
from . import caption as CAP

LADDERS = {
    "cutout": (("460k", "28"), ("400k", "31"), ("340k", "34"), ("280k", "37"), ("220k", "40"), ("170k", "44")),
    "photo":  (("660k", "20"), ("560k", "24"), ("460k", "28"), ("380k", "32"), ("300k", "36"), ("240k", "40")),
    "icon":   (("80k", "14"), ("64k", "18"), ("50k", "22"), ("40k", "26"), ("32k", "30")),
    "emoji":  (("140k", "12"), ("110k", "16"), ("90k", "20"), ("70k", "24"), ("55k", "28")),
}


def premultiply(im):
    a = np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0
    a[..., :3] *= a[..., 3:4]
    return Image.fromarray((a * 255 + 0.5).astype(np.uint8), "RGBA")


def unpremultiply(im):
    a = np.asarray(im, dtype=np.float32) / 255.0
    al = a[..., 3:4]
    a[..., :3] = np.where(al > 1e-4, a[..., :3] / np.maximum(al, 1e-4), 0)
    return Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


def prepare_cutout(im: Image.Image, margin: float, bottom_px: int = S):
    """Crop to the alpha bbox and seat the subject on a MASTER canvas with
    `margin` of free space around it (room for motion). `bottom_px` (in 512
    units) reserves the strip below for a caption."""
    im = im.crop(im.getchannel("A").getbbox())
    limit = bottom_px * MASTER / S
    avail_w = MASTER * (1 - 2 * margin)
    avail_h = limit - MASTER * margin * 2
    sc = min(avail_w / im.width, avail_h / im.height)
    im = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))), Image.LANCZOS)
    c = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    x = (MASTER - im.width) // 2
    y = int(limit - MASTER * margin) - im.height
    c.alpha_composite(im, (x, y))
    pivot = (MASTER / 2, limit - MASTER * margin)          # feet
    return premultiply(c), pivot


def prepare_photo(im: Image.Image, overscan: float = 1.06):
    """Scale-to-cover a MASTER canvas, slightly over-scanned so pans and shakes
    never reveal the edge."""
    im = im.convert("RGBA")
    sc = MASTER * overscan / min(im.width, im.height)
    im = im.resize((int(im.width * sc), int(im.height * sc)), Image.LANCZOS)
    c = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 255))
    c.alpha_composite(im, ((MASTER - im.width) // 2, (MASTER - im.height) // 2))
    return premultiply(c), (MASTER / 2, MASTER / 2)


def rounded_mask(radius: int, ss: int = 4) -> Image.Image:
    m = Image.new("L", (S * ss, S * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S * ss - 1, S * ss - 1], radius=radius * ss, fill=255)
    return m.resize((S, S), Image.LANCZOS)


def affine(angle_deg, sx, sy, dx, dy, pivot):
    a = math.radians(angle_deg)
    cx, cy = pivot
    R = np.array([[math.cos(a) * sx, -math.sin(a) * sy, 0], [math.sin(a) * sx, math.cos(a) * sy, 0], [0, 0, 1]])
    T1 = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]])
    T2 = np.array([[1, 0, cx + dx], [0, 1, cy + dy], [0, 0, 1]])
    out = np.array([[S / MASTER, 0, 0], [0, S / MASTER, 0], [0, 0, 1]])
    inv = np.linalg.inv(out @ T2 @ R @ T1)
    return tuple(inv[:2].reshape(-1))


def hit_times(motion_specs) -> list:
    """Impulse motions expose their hit times so FX can sync to them."""
    hits = []
    for sp in motion_specs or []:
        if isinstance(sp, str):
            sp = {"type": sp}
        if sp.get("type") in ("recoil", "jab"):
            hits += list(sp.get("times") or M.PRESETS[sp["type"]].__defaults__[0])
        elif sp.get("type") == "slam":
            hits += [D * (i + 0.25) / sp.get("drops", 1) for i in range(sp.get("drops", 1))]
    return sorted(hits)


def build(src: Image.Image, spec: dict, outdir: str, font: str) -> dict:
    """Render NF PNG frames into `outdir`. Returns metrics (caption info, mode)."""
    mode = spec.get("mode", "cutout")
    motion = M.build(spec.get("motion"))
    apply_fx = FX.build(spec.get("fx"))
    ctx = {"spec": spec, "seed": int(spec.get("seed", 1)), "hits": hit_times(spec.get("motion")),
           "font": font, "prev": []}

    cap_frames, cap_info = None, None
    cap = spec.get("caption")
    if cap and cap.get("text"):
        style = CAP.Style(**{k: v for k, v in cap.items() if k not in ("text", "font")})
        if isinstance(style.anims, list):
            style.anims = tuple(style.anims)
        cap_frames, cap_info = CAP.render(cap["text"], cap.get("font") or font, style)

    if mode == "photo":
        master, pivot = prepare_photo(src, spec.get("overscan", 1.06))
        mask = rounded_mask(int(spec.get("radius", 56))) if spec.get("radius", 56) > 0 else None
    else:
        bottom = S
        if cap_info and cap.get("position", "bottom") == "bottom":
            bottom = cap_info["band_top"] + 6            # subject sits above the caption band
        master, pivot = prepare_cutout(src, float(spec.get("margin", 0.08)), bottom)
        mask = None

    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    for n in range(NF):
        t = n / FPS
        ang, sx, sy, dx, dy = motion(t)
        if mode == "photo":                                  # never reveal the canvas edge
            sx, sy = max(sx, 1.0), max(sy, 1.0)
        frame = master.transform((S, S), Image.AFFINE, affine(ang, sx, sy, dx, dy, pivot), resample=Image.BICUBIC)
        frame = unpremultiply(frame)
        if mask is not None:
            frame.putalpha(mask)
        frame = apply_fx(frame, t, ctx)
        if cap_frames:
            frame.alpha_composite(cap_frames[n])
        ctx["prev"] = (ctx["prev"] + [frame])[-2:]
        frame.save(os.path.join(outdir, f"f{n:04d}.png"))
    return {"mode": mode, "caption": cap_info, "hits": ctx["hits"]}


def _encode(frames_dir, out, bitrate, crf, fps=FPS, extra_vf=None):
    args = ["-framerate", str(fps), "-i", os.path.join(frames_dir, "f%04d.png")]
    if extra_vf:
        args += ["-vf", extra_vf]
    args += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-metadata:s:v:0", "alpha_mode=1",
             "-b:v", bitrate, "-crf", crf, "-maxrate", "950k", "-bufsize", "1300k",
             "-g", str(NF + 1), "-row-mt", "1", "-deadline", "good", "-cpu-used", "1",   # 소담: best/0 대비 PSNR 같고(27.47 vs 27.48) 10배 빠름 (44→4초)
             "-auto-alt-ref", "0", "-an", "-t", str(D), "-f", "webm"]
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "pass")
        for p, dst in (("1", os.devnull), ("2", out)):
            subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args, "-pass", p, "-passlogfile", log, dst],
                           check=True, timeout=120)
    return os.path.getsize(out)


def encode_fit(frames_dir, out, ladder="cutout", limit=None, start=None):
    """Walk the bitrate ladder until the file fits. Returns (size, bitrate, crf)."""
    limits = {"cutout": 250 * 1024, "photo": 250 * 1024, "icon": 31 * 1024, "emoji": 62 * 1024}
    limit = limit or limits[ladder]
    steps = LADDERS[ladder]
    if start:
        steps = steps[[i for i, s in enumerate(steps) if s[0] == start][0]:] if any(s[0] == start for s in steps) else steps
    small = ladder in ("icon", "emoji")
    vf = "scale=100:100:flags=lanczos,unsharp=5:5:0.9:5:5:0.0" if small else None
    fps = 24 if ladder == "icon" else FPS
    size = br = crf = None
    for br, crf in steps:
        size = _encode(frames_dir, out, br, crf, fps=fps, extra_vf=vf)
        if size <= limit:
            break
    return size, br, crf


def contact_sheet(frames_dir, out, frames=(0, 12, 24, 36, 48, 60, 72, 88), tile=200, bg=(118, 128, 142)):
    """Preview strip on a neutral background so transparency is visible."""
    sheet = Image.new("RGBA", (tile * 4, tile * ((len(frames) + 3) // 4)), bg + (255,))
    for i, n in enumerate(frames):
        f = Image.open(os.path.join(frames_dir, f"f{n:04d}.png")).convert("RGBA").resize((tile, tile), Image.LANCZOS)
        sheet.alpha_composite(f, ((i % 4) * tile, (i // 4) * tile))
    sheet.convert("RGB").save(out)
    return out
