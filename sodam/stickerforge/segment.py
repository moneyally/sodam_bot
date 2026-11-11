"""실사 사진에서 '사람만' 오려내기 (AI 세그멘테이션).

색 키잉(keying.py)은 흰·검·단색 배경만 지운다. 카페처럼 복잡한 실사 배경은 사람과 배경 색이
섞여 있어 색으로는 못 가른다 → rembg(isnet-general-use ONNX)로 사람 윤곽을 따낸다.
**원본 픽셀은 그대로 두고 배경만 투명**하게 만든다 (gpt-image 처럼 다시 그리지 않음 → 얼굴 100% 보존).

왜 별도 프로세스인가 (옆집 Claude 조언·fetcher.py 와 같은 결):
- onnxruntime 는 기본값으로 코어 전체를 바쁜 대기(spinning)로 써서, 같은 서버의 음성채팅·노래
  (실시간 오디오)를 끊는다. → **별도 프로세스 + 코어 2,3 고정 + nice 10** 으로 오디오 코어(0,1) 보호.
- rembg/onnxruntime 는 무겁다 → **워커 프로세스 안에서만 import** (봇 본체 메모리는 가볍게).
- 한 번에 하나(max_workers=1) = 스티커 렌더의 _LOCK 과 같은 뜻.

끄기: .env STICKER_SEG=0 (그러면 ai 모드 요청도 색 키잉으로 폴백).
모델: data/models (ProtectSystem=strict 라 data/ 에만 씀), 첫 사용 때 1회 다운(약 170MB·67초) 후 상주.
"""
from __future__ import annotations

import io
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

MODEL = os.getenv("STICKER_SEG_MODEL", "isnet-general-use")
ENABLED = os.getenv("STICKER_SEG", "1").strip() not in ("0", "false", "off", "")
TIMEOUT = float(os.getenv("STICKER_SEG_TIMEOUT", "45") or 45)
_MODELS_DIR = str((Path(os.getenv("DB_PATH", "data/sodam.db")).resolve().parent / "models"))


# ── 워커 프로세스 (자식) ───────────────────────────────────────────────
def _init() -> None:
    """워커 시작 때 1번: 우선순위·코어를 오디오와 분리하고 모델을 상주시킨다."""
    try:
        os.nice(10)                                   # 음성·노래보다 낮은 우선순위
    except OSError:
        pass
    try:
        if (os.cpu_count() or 0) >= 4:                # 4코어↑면 2,3 만 (0,1 은 오디오 전용)
            os.sched_setaffinity(0, {2, 3})
    except (OSError, AttributeError):
        pass
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("U2NET_HOME", _MODELS_DIR)  # 모델 캐시 위치 (data/models)
    Path(_MODELS_DIR).mkdir(parents=True, exist_ok=True)
    from rembg import new_session                     # 워커 안에서만 import (봇 본체 가볍게)
    global _SESS
    _SESS = new_session(MODEL)                        # 상주 (첫 호출 때 모델 다운)


def _run(data: bytes) -> bytes:
    """사진 바이트 → 사람만 남긴 RGBA PNG 바이트. 배경은 투명, 원본 픽셀 그대로."""
    from rembg import remove
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    cut = remove(im, session=_SESS)                   # RGBA (soft alpha)
    cut = _cleanup(cut)
    buf = io.BytesIO()
    cut.save(buf, "PNG")
    return buf.getvalue()


def _cleanup(im):
    """마감: 작은 섬 제거 + 알파 스냅(거의 투명/불투명은 0/255 로 → 가장자리 안개·용량 정리).
    머리카락·손가락을 깎지 않도록 침식(erosion)은 쓰지 않는다 (옆집 Claude 경고)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi
    a = np.asarray(im.getchannel("A")).copy()
    solid = a >= 128
    if solid.any():
        lab, n = ndi.label(solid)                      # 불투명 덩어리들
        if n > 1:
            sizes = ndi.sum(np.ones_like(lab), lab, index=range(1, n + 1))
            big = float(sizes.max())
            small = [i + 1 for i, s in enumerate(sizes) if s < max(1.0, big * 0.005)]
            if small:                                  # 큰 덩어리의 0.5% 미만 섬 = 배경 잡음 → 투명
                a[np.isin(lab, small)] = 0
    a = np.where(a < 8, 0, np.where(a > 248, 255, a)).astype(np.uint8)   # 알파 스냅(안개·용량 정리)
    out = im.convert("RGBA")
    out.putalpha(Image.fromarray(a))
    return out


# ── 봇 쪽 (부모) ─────────────────────────────────────────────────────
_POOL: ProcessPoolExecutor | None = None


def _pool() -> ProcessPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init,
        )
    return _POOL


def available() -> bool:
    """ai 세그를 쓸 수 있는지 (.env 로 껐으면 False — 색 키잉으로 폴백)."""
    return ENABLED


def segment_sync(data: bytes) -> bytes:
    """사진 바이트 → 사람만 남긴 PNG 바이트 (동기). 스티커 렌더는 to_thread 안에서 돌아
    이 블록이 봇 이벤트 루프를 막지 않는다. 실패하면 예외 → 호출부가 색 키잉으로 폴백."""
    if not ENABLED:
        raise RuntimeError("STICKER_SEG disabled")
    return _pool().submit(_run, data).result(timeout=TIMEOUT)
