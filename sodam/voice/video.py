"""통화 영상 칸에 소담 사진 한 장 (가볍게: 변환 1번, 1초에 1장 같은 그림 재전송 → 메모리 안 늘어남).

ntgcalls 외부 영상 = I420(yuv420p) 날 프레임 (pytgcalls ffmpeg.py 도 rawvideo yuv420p). 크기 W×H×1.5 바이트.
사진 = 도우미 계정 프로필 사진 (없으면 단색 카드). 가로 640×360 에 비율 유지해 가운데, 나머지는 어두운 배경.
"""
from __future__ import annotations

import io

import numpy as np

W, H, FPS = 640, 360, 1
BG = (27, 31, 42)


def to_i420(image: bytes | None) -> bytes:
    from PIL import Image
    canvas = Image.new("RGB", (W, H), BG)
    if image:
        try:
            im = Image.open(io.BytesIO(image)).convert("RGB")
            im.thumbnail((W, H))
            canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
        except Exception:
            pass
    rgb = np.asarray(canvas, dtype=np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.257 * r + 0.504 * g + 0.098 * b + 16
    u = -0.148 * r - 0.291 * g + 0.439 * b + 128
    v = 0.439 * r - 0.368 * g - 0.071 * b + 128
    sub = lambda p: p.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))   # noqa: E731 — 2×2 평균 (4:2:0)
    planes = (y, sub(u), sub(v))
    return b"".join(np.clip(p, 0, 255).astype(np.uint8).tobytes() for p in planes)
