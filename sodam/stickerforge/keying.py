"""Background removal that keeps the character intact.

Three situations show up in practice and each needs a different rule:

  flat   — white, black or a solid colour behind a subject that has its own
           outline. Only the background region *connected to the image border*
           becomes transparent, so dark clothes, black eyes and enclosed gaps
           inside the character survive. Soft 1 px feather on the cut.
  glow   — a dark illustration on black with luminous particles/aura. The body
           is dark too, so "dark = background" would hollow out the face. Instead:
           background = dark region touching the border, body = large dark
           regions that are *not* the background, everything else (light haze,
           chains, sparks) gets a luminance-based soft alpha. Result works on
           both light and dark chat themes.
  none   — the PNG already carries alpha; use it as is.

`auto` looks at the border pixels and picks flat-white / flat-black / flat-colour
/ none. Ask for `glow` explicitly when the artwork is a dark subject with light
effects on black (auto cannot tell that from a plain black background).
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from scipy import ndimage as ndi


def _border_stats(rgb: np.ndarray):
    b = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]).astype(np.float32)
    return b.mean(axis=0), b.std(axis=0).max()


def _is_checker(rgb: np.ndarray) -> bool:
    """투명 '격자무늬(체커보드)'가 픽셀로 박힌 배경인지 — 투명 PNG 를 사진으로 보내거나
    투명 미리보기를 캡처하면 흰·회색 두 칸이 그림에 구워진다. 흰 배경(white)으로 오인해 흰 칸만
    지우면 회색 칸이 '타일'로 남으므로(2026-11-03 태산방 자이), 테두리가 저채도 밝은 두 톤
    (흰~255 + 회색~200)으로 갈리면 체커보드로 본다. 균일한 흰/회색 배경은 한 톤뿐이라 안 걸린다."""
    b = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]).astype(np.float32)
    chroma = b.max(1) - b.min(1)
    bright = b.mean(1)
    hi = (bright >= 235).mean()                    # 흰 칸
    mid = ((bright >= 185) & (bright < 235)).mean()  # 회색 칸
    return (chroma < 22).mean() >= 0.85 and hi >= 0.15 and mid >= 0.15 and (hi + mid) >= 0.8


def detect_mode(im: Image.Image) -> str:
    if im.mode == "RGBA":
        a = np.asarray(im.getchannel("A"))
        if (a < 16).mean() > 0.05:                 # real transparency already present
            return "none"
    rgb = np.asarray(im.convert("RGB"))
    if _is_checker(rgb):                            # 격자무늬 배경 → 흰·회색 둘 다 지움
        return "checker"
    b = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    # 인물이 가장자리까지 꽉 찬 그림 (옷·불꽃이 테두리에 닿음): 테두리 대부분이 흰색/검정이면 그 색 배경
    # (2026-10-07 얼라이드 '반갑습니다': 퍼짐이 커서 none → 흰 배경 그대로 나감)
    if (b.min(1) >= 225).mean() >= 0.5:
        return "white"
    if (b.max(1) <= 40).mean() >= 0.5:
        return "black"
    mean, spread = _border_stats(rgb)
    if spread > 40:                                # busy border: no uniform background
        return "none"
    if mean.min() >= 225:
        return "white"
    if mean.max() <= 40:
        return "black"
    return "color"


def _flood_background(cand: np.ndarray) -> np.ndarray:
    lab, _ = ndi.label(cand)
    border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    border = border[border > 0]
    return np.isin(lab, border)


def key_flat(im: Image.Image, mode: str, tol: int = 20, feather: float = 0.7) -> Image.Image:
    """`mode`: white | black | color. `tol` widens the candidate range.
    White-background art usually has a faint grey rim just outside its sticker
    outline (~225); the default threshold stays above it so the flood fill stops
    there instead of eating the white outline."""
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    mx, mn = rgb.max(2), rgb.min(2)
    if mode == "white":
        cand = mn >= 255 - tol
    elif mode == "black":
        cand = mx < 10 + tol
    else:
        mean, _ = _border_stats(rgb)
        dist = np.abs(rgb - mean).max(2)
        cand = dist < tol + 12
    bg = _flood_background(cand)
    alpha = np.clip(1 - ndi.gaussian_filter(bg.astype(np.float32), feather), 0, 1)
    out = np.dstack([rgb, alpha[..., None] * 255]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def key_checker(im: Image.Image, thr: int = 180) -> Image.Image:
    """체커보드(격자무늬) 배경 제거: 저채도 + 밝은(흰~255·회색~200) 칸을 **둘 다** 배경 후보로
    잡아 테두리에서 flood-fill. 흰 칸만 지우는 key_flat('white') 과 달리 회색 타일이 안 남는다.
    인물 속 밝은 부분(흰 이름표·얼굴 하이라이트)은 테두리와 안 이어져 보존된다."""
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    chroma = rgb.max(2) - rgb.min(2)
    bright = rgb.mean(2)
    cand = (chroma < 24) & (bright >= thr)
    bg = _flood_background(cand)
    alpha = np.clip(1 - ndi.gaussian_filter(bg.astype(np.float32), 0.7), 0, 1)
    out = np.dstack([rgb, alpha[..., None] * 255]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def key_glow(im: Image.Image, body_min_frac: float = 0.004, body_dark: int = 80,
             bg_dark: int = 34, soft_lo: int = 14, soft_hi: int = 110) -> Image.Image:
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    H, W, _ = rgb.shape
    mx = rgb.max(2)
    bg = _flood_background(mx < bg_dark)
    enclosed = (mx < body_dark) & ~ndi.binary_dilation(bg, iterations=2)
    lab, n = ndi.label(enclosed)
    sizes = ndi.sum(enclosed, lab, range(1, n + 1))
    big = [i + 1 for i, s in enumerate(sizes) if s > body_min_frac * H * W]
    core = np.isin(lab, big)
    core = ndi.binary_dilation(ndi.binary_closing(core, iterations=3), iterations=2)
    soft = np.clip((mx - soft_lo) / float(soft_hi - soft_lo), 0, 1) ** 0.85
    alpha = np.where(core, 1.0, soft)
    alpha = np.where(bg & (mx < bg_dark), 0, alpha)
    alpha = np.maximum(alpha, ndi.gaussian_filter(core.astype(np.float32), 1.0) * 0.999)
    out = np.dstack([rgb, alpha[..., None] * 255]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def fill_enclosed_holes(im: Image.Image, max_px: int = 4000) -> Image.Image:
    """Transparent pockets fully surrounded by opaque pixels are almost always a
    keying mistake (a dark jacket that matched the background). Fill the small
    ones back in; leave big ones, which may be real (an arm akimbo)."""
    a = np.asarray(im.getchannel("A"))
    tr = a < 128
    lab, n = ndi.label(tr)
    border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    sizes = ndi.sum(tr, lab, range(1, n + 1))
    fix = np.zeros_like(tr)
    for i, s in enumerate(sizes, start=1):
        if i not in border and s <= max_px:
            fix |= lab == i
    if not fix.any():
        return im
    arr = np.asarray(im).copy()
    arr[..., 3] = np.where(fix, 255, arr[..., 3])
    return Image.fromarray(arr, "RGBA")


def key_image(path: str, mode: str = "auto", tol: int = 20, fill_holes: bool = True) -> tuple:
    """Returns (RGBA image, mode actually used)."""
    im = Image.open(path)
    if mode == "auto":
        mode = detect_mode(im)
    if mode == "none":
        out = im.convert("RGBA")
    elif mode == "glow":
        out = key_glow(im)
    elif mode == "checker":
        out = key_checker(im)
    else:
        out = key_flat(im, mode, tol=tol)
    if fill_holes and mode in ("white", "black", "color", "checker"):
        out = fill_enclosed_holes(out)
    return out, mode
