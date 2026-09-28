"""Read the encoded file back and prove it meets Telegram's rules.

ffprobe reports VP9-with-alpha as plain yuv420p because the default decoder
ignores Matroska block additions, so the alpha check decodes a frame with
libvpx-vp9 and looks at the pixels.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

import numpy as np
from PIL import Image

from .const import ffmpeg

LIMITS = {"sticker": (512, 256 * 1024), "icon": (100, 32 * 1024), "emoji": (100, 64 * 1024)}


def _probe(path) -> dict:
    """ffprobe 없이 `ffmpeg -i` 출력으로 (imageio-ffmpeg 에는 ffprobe 가 없음)."""
    err = subprocess.run([ffmpeg(), "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    v = re.search(r"Stream #0:\d+.*?: Video: (\w+).*?, (\d+)x(\d+).*?, ([\d.]+) fps", err)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    return {"codec": v.group(1) if v else "?", "w": int(v.group(2)) if v else 0, "h": int(v.group(3)) if v else 0,
            "fps": float(v.group(4)) if v else 0.0,
            "dur": int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 99.0,
            "alpha": bool(re.search(r"(?i)alpha_mode\s*:\s*1", err)), "audio": "Audio:" in err}


def _decode_frame(path, n=0):
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "f.png")
        subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-c:v", "libvpx-vp9", "-i", path,
                        "-vf", f"select='eq(n\\,{n})'", "-frames:v", "1", "-pix_fmt", "rgba", png], check=True)
        return Image.open(png).convert("RGBA")


def enclosed_holes(path, frame=45) -> int:
    """Transparent pixels fully surrounded by opaque ones. Catches a dark jacket
    keyed away by mistake. Glow-keyed art legitimately has many (gaps in the
    haze), so the check is advisory for kind='glow'."""
    from scipy import ndimage as ndi
    a = np.asarray(_decode_frame(path, frame).getchannel("A"))
    tr = a < 128
    lab, _ = ndi.label(tr)
    border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    return int((tr & ~np.isin(lab, border)).sum())


def spec(path: str, kind: str = "sticker", holes: bool = True) -> dict:
    side, limit = LIMITS[kind]
    p = _probe(path)
    w, h, codec, fps, dur = p["w"], p["h"], p["codec"], p["fps"], p["dur"]
    size = os.path.getsize(path)
    tag, audio = (["1"] if p["alpha"] else []), p["audio"]
    lo, hi = _decode_frame(path, 0).getchannel("A").getextrema()
    rows = [
        ("dimensions", f"{w}x{h}", int(w) == side and int(h) == side),
        ("codec", codec, codec == "vp9"),
        ("fps", f"{fps:g}", fps <= 30),
        ("duration", f"{dur:.3f}s", dur <= 3.0),
        ("size", f"{size / 1024:.1f}KB / {limit // 1024}KB", size <= limit),
        ("alpha_mode", tag[0] if tag else "missing", bool(tag) and tag[0] == "1"),
        ("alpha_range", f"{lo}-{hi}", lo == 0 and hi == 255),
        ("audio", "none" if not audio else "present", not audio),
    ]
    if holes and kind == "sticker":
        n = enclosed_holes(path)
        rows.append(("holes", f"{n} px enclosed", n <= 200))
    return {"rows": rows, "ok": all(r[2] for r in rows), "size": size}


def report(res: dict, extra: list | None = None) -> str:
    lines = [f"  {'PASS' if ok else 'FAIL'}  {name:<12} {val}" for name, val, ok in res["rows"]]
    for name, val, ok in extra or []:
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {name:<12} {val}")
    ok = res["ok"] and all(e[2] for e in extra or [])
    lines.append("  -> " + ("READY" if ok else "NOT READY"))
    return "\n".join(lines)
