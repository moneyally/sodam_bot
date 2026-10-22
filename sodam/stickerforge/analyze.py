"""🔍 전달받은 커스텀 이모지 조각을 분해·분석 → 스타일 토큰 + 지문 벡터 (트랙 B '이거처럼 똑같이').

오너 2026-10-22 '분해 분석 벡터로 DB': 샘플 타일 → (색 그라데이션·프레임·글로우·스타일 추정) 토큰으로 되돌려
banner+stylefx 로 재현. 벡터는 sqlite-vec DB 에 쌓아 '이 샘플 = 85% 금색 카지노' 매칭·학습 (다음 작업).
전부 코드(AI 비용 0). 추정이라 정확하진 않음 — 토큰은 재현의 '시작값'.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

VEC_DIM = 12            # 지문 벡터 차원 (sqlite-vec 저장용)


def _arr(img: Image.Image, size: int = 48) -> tuple[np.ndarray, np.ndarray]:
    im = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32)
    return a[..., :3], a[..., 3] / 255.0


def _sat(px: np.ndarray) -> float:
    mx, mn = px.max(1), px.min(1)
    return float(((mx - mn) / (mx + 1e-6)).mean())


def tile_features(img: Image.Image) -> np.ndarray:
    """조각 한 장 → 지문 벡터(VEC_DIM). 색·채도·밝기·따뜻함·프레임·글로우·채움 — 비슷한 스타일끼리 가까움."""
    rgb, alpha = _arr(img)
    m = alpha > 0.5
    if m.sum() < 10:
        return np.zeros(VEC_DIM, dtype=np.float32)
    px = rgb[m]
    mean = px.mean(0) / 255.0                                   # 0~2: 평균 색
    sat = _sat(px)                                             # 3: 채도 (금·네온 높음, 크롬 낮음)
    bright = float((px.max(1) / 255.0).mean())                # 4: 밝기
    warm = float((px[:, 0].mean() - px[:, 2].mean()) / 255.0)  # 5: 따뜻함 R-B (금·불 +, 크롬·네온 -)
    # 세로 그라데이션: 위/아래 평균 색 차이 (금속은 위 밝고 아래 어두움)
    h = rgb.shape[0]
    top_m, bot_m = alpha[: h // 3] > 0.5, alpha[2 * h // 3:] > 0.5
    topb = float((rgb[: h // 3][top_m].max(1) / 255.0).mean()) if top_m.any() else bright
    botb = float((rgb[2 * h // 3:][bot_m].max(1) / 255.0).mean()) if bot_m.any() else bright
    grad = topb - botb                                        # 6: 위-아래 밝기차 (금속 +)
    # 프레임(배지): 가장자리 불투명 비율
    border = float((alpha[:5].mean() + alpha[-5:].mean() + alpha[:, :5].mean() + alpha[:, -5:].mean()) / 4)  # 7
    # 글로우: 반투명(0.1~0.8) 픽셀 비율 (네온·불 높음)
    glow = float(((alpha > 0.1) & (alpha < 0.85)).mean())     # 8
    cover = float(m.mean())                                   # 9: 채움 비율
    # 반투명 전체(유리): 불투명한데 알파 평균이 낮음
    glass = float(alpha[m].mean())                            # 10
    cool = float((px[:, 2].mean() - px[:, 0].mean()) / 255.0)  # 11: 차가움 B-R (네온·크롬)
    return np.array([*mean, sat, bright, warm, grad, border, glow, cover, glass, cool][:VEC_DIM], dtype=np.float32)


def guess_style(img: Image.Image) -> str:
    """조각의 스타일 추정 (stylefx.STYLES 중 하나). 투명도 → 색(따뜻/차가움) → 글로우·채도로."""
    rgb, alpha = _arr(img)
    m = alpha > 0.5
    if m.sum() < 10:
        return "gold"
    px = rgb[m]
    sat, bright = _sat(px), float((px.max(1) / 255.0).mean())
    glow = float(((alpha > 0.1) & (alpha < 0.85)).mean())
    core_a = float(alpha[m].mean())                           # 글자 속 알파 평균 (유리는 반투명, 네온 심지는 불투명)
    r, g, b = (float(px[:, i].mean()) / 255.0 for i in range(3))
    cool, warm = b - r, r - b
    if core_a < 0.86 and bright > 0.55 and glow < 0.4:        # 글자 자체가 반투명 = 유리
        return "glass"
    if cool > 0.05:                                           # 파랑·시안끼 → 네온(글로우) / 크롬(차분)
        return "neon" if glow > 0.055 and bright > 0.5 else "chrome"
    if warm > 0.1:                                            # 따뜻함 → 주황(불) / 노랑(금)
        return "fire" if g < r * 0.82 else "gold"
    if sat < 0.16:                                            # 채도 낮음 = 크롬/은
        return "chrome"
    return "gold"


def gradient_stops(img: Image.Image) -> list[list[int]]:
    """글자 색을 위→중간→아래 대표색 3개로 (재현용 colors 토큰). 불투명 픽셀만, 세로 위치별 평균."""
    rgb, alpha = _arr(img, 64)
    h = rgb.shape[0]
    stops = []
    for a, bnd in ((0, h // 3), (h // 3, 2 * h // 3), (2 * h // 3, h)):
        band, ba = rgb[a:bnd], alpha[a:bnd] > 0.5
        if ba.any():
            # 가장 밝은(글자) 픽셀 위주 — 어두운 테두리 섞이지 않게 상위 밝기
            bp = band[ba]
            keep = bp[bp.max(1) >= np.percentile(bp.max(1), 55)]
            stops.append([int(v) for v in (keep.mean(0) if len(keep) else bp.mean(0))])
    while len(stops) < 2:
        stops.append(stops[-1] if stops else [240, 210, 90])
    return stops


def analyze_tile(img: Image.Image) -> dict:
    """조각 → 재현 토큰 {style, colors(그라데이션), has_frame, glow, vector}. banner+stylefx 로 재현하는 시작값."""
    rgb, alpha = _arr(img)
    border = float((alpha[:5].mean() + alpha[-5:].mean() + alpha[:, :5].mean() + alpha[:, -5:].mean()) / 4)
    glow = float(((alpha > 0.1) & (alpha < 0.85)).mean())
    return {"style": guess_style(img), "colors": gradient_stops(img),
            "has_frame": border > 0.35, "glow": round(glow, 3),
            "vector": tile_features(img).tolist()}
