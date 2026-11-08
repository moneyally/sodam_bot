"""🔞 로컬 신체부위 탐지 (NudeNet 320n, 2026-11-06 오너 'NudeNet 넣되 조심히').

omni-moderation 은 '야함'을 통째로 점수 매겨서 **조각낸 신체사진**(크롭·타일 스티커)은 놓침.
NudeNet 은 부위별(FEMALE_GENITALIA_EXPOSED 등)로 잡아서 조각에 강함. 320n 모델은 패키지에 들어 있어
실행 중 내려받지 않음(ProtectSystem=strict 안전), 추론은 CPU 로 한 장 ~10~40ms.

조심히(VPS·노래/음성과 CPU 경쟁):
  · 라이브러리 없으면 조용히 꺼짐(봇은 그대로 동작) — requirements 에 nudenet 넣지만 import 는 지연.
  · 한 번에 하나(_SEM), 스레드풀(to_thread), 5초 타임아웃.
  · nsfwguard 가 **살색 비율이 어느 정도 높은, omni 가 안 걸린 이미지에만** 부르게 해서 호출 수를 줄임.
  · 끄기: .env NSFW_LOCAL=0.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os

log = logging.getLogger(__name__)

# 명백한 '노출' 부위만 (COVERED·BELLY·ARMPITS·FEET·FACE 는 제외 — 오탐 방지)
EXPOSED = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "FEMALE_BREAST_EXPOSED",
           "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
TIMEOUT = 5.0

_SEM = asyncio.Semaphore(1)
_det = None
_tried = False


def available() -> bool:
    return _load() is not None


def _load():
    global _det, _tried
    if _tried:
        return _det
    _tried = True
    if os.getenv("NSFW_LOCAL", "1").strip() in ("0", "off", "false", ""):
        log.info("NSFW_LOCAL 꺼짐 — 로컬 신체부위 탐지 안 씀")
        return None
    try:
        from nudenet import NudeDetector
        _det = NudeDetector()   # 320n 모델은 패키지 번들
        log.info("NudeNet 로컬 탐지 켜짐")
    except Exception as e:
        log.warning("NudeNet 없음/로드 실패 — 로컬 탐지 꺼짐(omni·살색·팩차단만): %s", e)
        _det = None
    return _det


def _run(data: bytes) -> float | None:
    det = _load()
    if det is None:
        return None
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.ascontiguousarray(np.array(im)[:, :, ::-1])   # RGB→BGR (opencv 관례)
    best = 0.0
    for d in det.detect(arr) or []:
        if d.get("class") in EXPOSED:
            best = max(best, float(d.get("score") or 0.0))
    return best


async def nude_score(data: bytes) -> float | None:
    """노출 신체부위 최고 점수 0~1 (없으면 0.0, 라이브러리 없거나 실패면 None)."""
    if _load() is None:
        return None
    try:
        async with _SEM:
            return await asyncio.wait_for(asyncio.to_thread(_run, data), TIMEOUT)
    except Exception as e:
        log.info("NudeNet 추론 실패: %s", e)
        return None
