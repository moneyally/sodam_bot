"""🎞️ 움프(움직이는 프로필) 만들기: 사진 한 장 → 텔레그램 프로필 영상 규격 MP4.

텔레그램 프로필 영상 규격 (조금이라도 어긋나면 오류 없이 버려짐): 정사각형 ≤800(여기선 640) · ≤10초 · ≤2MB · H.264 ·
yuv420p · 소리 없음 · faststart. ffmpeg 는 imageio-ffmpeg 정적 바이너리(시스템 ffmpeg 없어도 됨) 또는 PATH 의 ffmpeg.
- 움직임은 코드가 정한 틀(STYLES)만 — AI·사용자 글이 ffmpeg 인자에 들어가지 않음. 모든 효과는 FRAMES 주기의 sin 이라 끝과
  처음이 이어져 끊김 없이 반복.
- AI 그림체(AI_STYLES)는 gpt-image 고치기(llm.image) 로 먼저 바꾼 뒤 같은 틀로 움직임 (panels/avatar.py).
- CPU 를 쓰므로 한 번에 하나(_LOCK), 60초 넘으면 끊음. 결과가 2MB 넘으면 화질을 낮춰 한 번 더.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

SIZE = 640
FPS = 30
SECONDS = 6
FRAMES = FPS * SECONDS
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 60
_LOCK = asyncio.Semaphore(1)

_P = f"2*PI*on/{FRAMES}"      # zoompan 프레임 번호 → 한 바퀴
_T = f"2*PI*n/{FRAMES}"       # 일반 필터 프레임 번호
_ZOOM = (f"zoompan=z='1.10+0.08*sin({_P})':x='iw/2-(iw/zoom/2)+20*sin({_P})':y='ih/2-(ih/zoom/2)+14*cos({_P})'"
         f":d=1:s={SIZE}x{SIZE}:fps={FPS}")
STYLES: dict[str, tuple[str, str]] = {
    "breathe": ("🌬️ 숨쉬기", f"{_ZOOM},hue=s='1+0.10*sin({_T})'"),
    "shine": ("✨ 반짝임", f"{_ZOOM},eq=brightness='0.07*sin(2*{_T})':saturation='1.15':eval=frame,vignette=PI/5"),
    "rainbow": ("🌈 무지개", f"{_ZOOM},hue=h='360*n/{FRAMES}':s='1.3'"),
    "sway": ("🎐 흔들흔들", f"scale={SIZE + 160}:{SIZE + 160},rotate='0.035*sin({_T})':c=black,"
                           f"crop={SIZE}:{SIZE},fps={FPS}"),
}
AI_STYLES: dict[str, tuple[str, str]] = {   # 그림체 바꾸기 (gpt-image 고치기) — 얼굴·구도는 그대로
    "anime": ("🎨 애니", "Redraw this exact photo as a clean Japanese anime illustration. Keep the same person, face "
                        "shape, pose, outfit and composition. Vivid colors, soft cel shading, square avatar framing."),
    "3d": ("🧸 3D", "Redraw this exact photo as a cute 3D Pixar-style character render. Keep the same person, pose, "
                   "outfit and composition. Soft studio lighting, square avatar framing."),
    "neon": ("💜 네온", "Restyle this exact photo as a cyberpunk neon portrait: glowing pink and cyan rim light, dark "
                      "city bokeh background. Keep the same person, face, pose and composition. Square avatar framing."),
    "water": ("🖌️ 수채화", "Repaint this exact photo as a soft watercolor painting. Keep the same person, pose and "
                         "composition. Pastel colors, paper texture, square avatar framing."),
}


def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:   # 패키지 없음 → 시스템 ffmpeg
        return shutil.which("ffmpeg")


def available() -> bool:
    return ffmpeg_path() is not None


def _args(ff: str, src: str, out: str, style: str, crf: int) -> list[str]:
    pre = f"scale={SIZE * 2}:{SIZE * 2}:force_original_aspect_ratio=increase,crop={SIZE * 2}:{SIZE * 2},"
    return [ff, "-y", "-loglevel", "error", "-loop", "1", "-i", src, "-t", str(SECONDS),
            "-filter_complex", f"[0:v]{pre}{STYLES[style][1]},format=yuv420p",
            "-an", "-c:v", "libx264", "-profile:v", "main", "-preset", "veryfast", "-crf", str(crf),
            "-movflags", "+faststart", out]


async def _run(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffmpeg 시간 초과")
    if proc.returncode:
        raise RuntimeError(f"ffmpeg 실패: {err.decode(errors='replace')[-200:]}")


async def make(image: bytes, style: str) -> bytes:
    """사진 bytes → 규격 MP4 bytes. style 은 STYLES 키만."""
    if style not in STYLES:
        raise ValueError(style)
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg 없음")
    async with _LOCK:
        with tempfile.TemporaryDirectory(prefix="sodam_ava_") as d:
            src, out = os.path.join(d, "src.img"), os.path.join(d, "out.mp4")
            with open(src, "wb") as f:
                f.write(image)
            for crf in (26, 32):   # 2MB 넘으면 화질을 낮춰 한 번 더
                await _run(_args(ff, src, out, style, crf))
                with open(out, "rb") as f:
                    data = f.read()
                if len(data) <= MAX_BYTES:
                    return data
    raise RuntimeError("2MB 안으로 못 줄임")
