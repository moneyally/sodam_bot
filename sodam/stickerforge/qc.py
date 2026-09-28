"""검수 지표 — AI 없이 코드로 세는 것들 (Result.warnings / metrics).

소담이는 결과를 눈으로 못 보니 프레임을 숫자로 잰다. 각 지표는 (값, 한계) 로 두고 한계를 넘으면
한국어 경고 한 줄을 만든다. 경고 문장은 소담이가 그대로 읽고 "무엇을 고칠지" 정하는 입력이라
원인 + 고칠 방향을 같이 적는다.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .const import S

# 한계 (지표 이름: 값). 바꾸면 tests/test_sticker_upgrade.py 도 같이.
LIMITS = {
    "caption_overlap": 0.12,   # 자막 상자 안에 든 피사체(또는 관심 영역) 비율
    "edge_clip": 0.02,         # 가장자리 2px 띠에서 불투명 픽셀 비율 (cutout)
    "holes": 200,              # 폐쇄 투명 픽셀 수
    "motion_min": 0.3,         # 프레임 간 평균 차이(0~255) 가 이보다 작으면 밋밋 (잔잔한 idle ≈ 0.5)
    "motion_max": 48.0,        # 한 프레임 차이가 이보다 크면 요란
    "whiteout_frames": 1,      # 불투명 픽셀의 90% 이상이 245↑ 인 프레임 수
}
THUMB = 96                     # 프레임 차이는 축소본으로 잰다 (빠르고 노이즈에 둔감)


def _small(frame: Image.Image) -> np.ndarray:
    return np.asarray(frame.convert("RGBA").resize((THUMB, THUMB), Image.BILINEAR), dtype=np.float32)


def motion_stats(frames: list[Image.Image]) -> tuple[float, float]:
    """(평균, 최대) 프레임 간 차이. RGB 는 알파로 가중해 투명 영역의 쓰레기 값은 안 센다."""
    prev = None
    diffs = []
    for f in frames:
        a = _small(f)
        cur = np.concatenate([a[..., :3] * (a[..., 3:4] / 255.0), a[..., 3:4]], axis=2)
        if prev is not None:
            diffs.append(float(np.abs(cur - prev).mean()))
        prev = cur
    return (float(np.mean(diffs)), float(np.max(diffs))) if diffs else (0.0, 0.0)


def whiteout_frames(frames: list[Image.Image], step: int = 2) -> int:
    n = 0
    for f in frames[::step]:
        a = _small(f)
        opaque = a[..., 3] > 128
        if opaque.sum() < 20:
            continue
        bright = (a[..., :3].min(axis=2) >= 245) & opaque
        if bright.sum() >= 0.9 * opaque.sum():
            n += 1
    return n * step if n else 0


def edge_clip(frames: list[Image.Image], band: int = 2, step: int = 4) -> float:
    """가장자리 띠의 불투명 비율 (최대값). cutout 에서 0 이어야 정상 — 피사체가 화면 밖으로 나갔다는 뜻."""
    worst = 0.0
    for f in frames[::step]:
        a = np.asarray(f.getchannel("A"))
        rim = np.concatenate([a[:band].ravel(), a[-band:].ravel(), a[:, :band].ravel(), a[:, -band:].ravel()])
        worst = max(worst, float((rim > 64).mean()))
    return worst


def caption_overlap(subject_boxes: list, cap_box: tuple | None, focus_mask: np.ndarray | None = None) -> float:
    """자막 상자와 피사체가 얼마나 겹치나.
    cutout: 프레임별 피사체 알파 bbox 와 자막 bbox 의 교집합 / 자막 넓이 (최대값).
    photo: 관심 영역(focus_mask, 0~1) 중 자막 상자 안에 든 비율."""
    if not cap_box:
        return 0.0
    x0, y0, x1, y1 = cap_box
    if focus_mask is not None:
        total = float(focus_mask.sum()) or 1.0
        return float(focus_mask[y0:y1, x0:x1].sum()) / total
    worst = 0.0
    area = max(1, (x1 - x0) * (y1 - y0))
    for b in subject_boxes:
        if not b:
            continue
        ix = max(0, min(x1, b[2]) - max(x0, b[0]))
        iy = max(0, min(y1, b[3]) - max(y0, b[1]))
        worst = max(worst, ix * iy / area)
    return worst


def inspect(frames: list[Image.Image], *, mode: str, keying: str, subject_boxes: list, cap_box: tuple | None,
            focus_mask: np.ndarray | None, holes: int | None, has_caption: bool) -> tuple[list[str], dict]:
    """→ (경고 목록, 지표). 경고는 소담이가 읽고 고칠 방향을 정하는 문장."""
    m_mean, m_max = motion_stats(frames)
    metrics = {
        "caption_overlap": round(caption_overlap(subject_boxes, cap_box, focus_mask if mode == "photo" else None), 3),
        "edge_clip": round(edge_clip(frames), 3) if mode == "cutout" else 0.0,
        "holes": int(holes or 0),
        "motion_mean": round(m_mean, 2), "motion_max": round(m_max, 2),
        "whiteout_frames": whiteout_frames(frames),
    }
    w = []
    if has_caption and metrics["caption_overlap"] > LIMITS["caption_overlap"]:
        w.append(f"자막이 피사체를 {metrics['caption_overlap']:.0%} 덮음 → position=top 으로 옮기거나 framing=blur/top, 또는 글자 수를 줄일 것")
    if metrics["edge_clip"] > LIMITS["edge_clip"]:
        w.append(f"피사체가 가장자리에서 잘림(테두리 불투명 {metrics['edge_clip']:.0%}) → margin 을 키우거나(hop·slam 은 0.14↑) 움직임 폭을 줄일 것")
    if holes is not None and holes > LIMITS["holes"] and keying != "glow" and not has_caption:
        w.append(f"배경 빼기 구멍 {holes}px (옷·머리의 어두운 부분이 빠짐) → keying tol 을 낮추거나 mode=photo")
    if m_mean < LIMITS["motion_min"]:
        w.append(f"너무 밋밋함(프레임 차이 평균 {m_mean:.1f}) → 움직임을 넣거나(idle→punch/float) 효과 하나 추가")
    if m_max > LIMITS["motion_max"]:
        w.append(f"너무 요란함(한 프레임 차이 최대 {m_max:.0f}) → 효과를 하나 빼거나 amp/strength 를 줄일 것")
    if metrics["whiteout_frames"] >= LIMITS["whiteout_frames"]:
        w.append(f"{metrics['whiteout_frames']}프레임이 하얗게 날아감 → flashbang amount·aura flare·glow strength 를 줄일 것")
    return w, metrics


def focus_map(im: Image.Image, size: int = 64) -> np.ndarray:
    """관심 영역(0~1): 국소 대비(에지) 에너지. 얼굴·글자·피사체가 높고 하늘·벽·옷은 낮다.
    가볍게: 축소 → 그레이 → 라플라시안 절대값 → 블러."""
    from PIL import ImageFilter
    g = im.convert("L").resize((size, size), Image.BILINEAR)
    e = np.asarray(g.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    e = np.asarray(Image.fromarray(e.astype(np.uint8)).filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
    return e / (e.max() or 1.0)


def focus_mask_512(im: Image.Image) -> np.ndarray:
    """photo 모드 자막 겹침용: 512×512 관심 영역 (프레임 좌표)."""
    m = focus_map(im, 64)
    return np.asarray(Image.fromarray((m * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR), dtype=np.float32) / 255.0
