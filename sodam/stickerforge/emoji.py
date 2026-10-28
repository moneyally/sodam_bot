"""사진·그림 → 100×100 투명 커스텀 이모지 한 장.

조사(2026-10-28, telegram-emoji-forge 에이전트 보고) 반영:
풀해상도에서 배경을 빼고(keying) → 알파 bounding box 로 피사체만 잘라 → 정사각 패딩 →
**마지막에 한 번만 Lanczos 로 100px 축소** (작게 자르면 계단·후광이 생김) → 작은 투명 여백.
rembg(무거운 ONNX 모델)는 아직 안 씀 — 기존 keying(흰/검/발광/투명 자동)으로 충분한 경우가 많고,
서버가 작은 VPS라 모델 내려받기가 부담. 더 깔끔한 컷아웃이 필요하면 rembg 를 뒤에 붙일 자리.
"""
from __future__ import annotations

import io

from PIL import Image

from . import keying

EMOJI = 100


def image_to_emoji(data: bytes, mode: str = "auto", margin: float = 0.06) -> Image.Image:
    """사진 bytes → 100×100 RGBA (투명 배경, 피사체 가운데). mode=auto 면 keying 이 흰/검/발광/투명 판별."""
    cut, _ = keying.key_image(io.BytesIO(data), mode=mode)
    cut = cut.convert("RGBA")
    bbox = cut.getchannel("A").getbbox()        # 피사체(불투명)만
    if bbox:
        cut = cut.crop(bbox)
    w, h = cut.size
    if not w or not h:                          # 전부 투명 등 — 원본을 정사각으로
        cut = Image.open(io.BytesIO(data)).convert("RGBA")
        w, h = cut.size
    s = max(w, h)
    square = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    square.paste(cut, ((s - w) // 2, (s - h) // 2))
    inner = max(1, min(EMOJI, int(round(EMOJI * (1 - 2 * max(0.0, min(0.3, margin)))))))
    square = square.resize((inner, inner), Image.LANCZOS)   # 풀해상도 → 한 번에 축소 (후광·계단 방지)
    out = Image.new("RGBA", (EMOJI, EMOJI), (0, 0, 0, 0))
    out.paste(square, ((EMOJI - inner) // 2, (EMOJI - inner) // 2))
    return out


def to_webp(img: Image.Image) -> bytes:
    """100×100 투명 WEBP (정적 커스텀 이모지 규격). 작은 그림이라 lossless 여도 수 KB."""
    if img.size != (EMOJI, EMOJI):
        img = img.convert("RGBA").resize((EMOJI, EMOJI), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "WEBP", lossless=True)
    return buf.getvalue()
