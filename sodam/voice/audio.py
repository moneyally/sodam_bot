"""PCM16 모노 변환 (numpy). 텔레그램 통화 = 48 kHz, OpenAI Realtime = 24 kHz 전용 (SDK AudioPCM: rate 24000)."""
from __future__ import annotations

import numpy as np

TG_RATE, AI_RATE = 48_000, 24_000
FRAME_MS = 10
FRAME_BYTES = TG_RATE * 2 * FRAME_MS // 1000     # 48 kHz 모노 10 ms = 480 샘플 = 960 바이트 (py-tgcalls 예제와 같은 10 ms 조각)


def _pcm(b: bytes) -> np.ndarray:
    return np.frombuffer(b[: len(b) // 2 * 2], dtype="<i2").astype(np.int32)


def mix(chunks: list[bytes]) -> bytes:
    """여러 사람 소리를 하나로 (평균, 잘림 방지)."""
    arrs = [_pcm(c) for c in chunks if c]
    if not arrs:
        return b""
    n = max(len(a) for a in arrs)
    out = np.zeros(n, dtype=np.int32)
    for a in arrs:
        out[: len(a)] += a
    return np.clip(out // len(arrs), -32768, 32767).astype("<i2").tobytes()


def down(pcm48: bytes) -> bytes:
    """48 k → 24 k: 두 샘플 평균 (간단한 저역 통과 겸)."""
    a = _pcm(pcm48)
    a = a[: len(a) // 2 * 2].reshape(-1, 2).mean(axis=1)
    return a.astype("<i2").tobytes()


def up(pcm24: bytes) -> bytes:
    """24 k → 48 k: 선형 보간 (샘플 사이에 평균 하나씩)."""
    a = _pcm(pcm24)
    if not len(a):
        return b""
    nxt = np.append(a[1:], a[-1])
    out = np.empty(len(a) * 2, dtype=np.int32)
    out[0::2], out[1::2] = a, (a + nxt) // 2
    return out.astype("<i2").tobytes()


def level(pcm: bytes) -> float:
    """소리 크기 (RMS, 0~32768) — 누가 말하는지·침묵 판단용."""
    a = _pcm(pcm)
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if len(a) else 0.0
