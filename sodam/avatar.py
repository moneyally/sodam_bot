"""🎞️ 움프(움직이는 프로필) 만들기: 사진 한 장 → 텔레그램 프로필 영상 규격 MP4.

텔레그램 프로필 영상 규격 (조금이라도 어긋나면 오류 없이 버려짐): 정사각형 ≤800(여기선 640) · ≤10초 · ≤2MB · H.264 ·
yuv420p · 소리 없음 · faststart. ffmpeg 는 imageio-ffmpeg 정적 바이너리(시스템 ffmpeg 없어도 됨) 또는 PATH 의 ffmpeg.
- 효과 = 부품 조합 Spec(움직임 MOTIONS × 빠르기 SPEEDS × 색 COLORS × 날리는 것 PARTICLES). 전부 코드의 고정 문자열이고
  AI·사용자 글은 목록 키로만 들어옴 (ffmpeg 인자에 글이 섞이지 않음). 사용자가 말한 효과는 AI 가 가장 가까운 부품으로 옮김.
- 끊김 없는 반복: 모든 움직임·색은 한 영상(FRAMES) 안에 정수 바퀴 도는 sin, 날리는 것은 같은 타일 두 장을 이어 붙여
  영상 한 번에 정확히 한 칸(또는 정수 칸) 흐르게 → 끝과 처음이 같은 그림.
- AI 그림체(AI_STYLES)는 gpt-image 고치기(llm.image) 로 먼저 바꾼 뒤 같은 효과를 입힘 (panels/avatar.py).
- CPU 를 쓰므로 한 번에 하나(_LOCK), 60초 넘으면 끊음. 결과가 2MB 넘으면 화질을 낮춰 한 번 더.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import random
import shutil
import tempfile
from dataclasses import dataclass

SIZE = 640
FPS = 30
SECONDS = 6
FRAMES = FPS * SECONDS
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 60
_LOCK = asyncio.Semaphore(1)


def _zp(z: str, x: str = "0", y: str = "0") -> str:
    """zoompan (가운데 기준 + 흔들림). on = 출력 프레임 번호."""
    return (f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)+{x}':y='ih/2-(ih/zoom/2)+{y}':d=1:s={SIZE}x{SIZE}:fps={FPS}")


def _p(k: int) -> str:
    return f"2*PI*{k}*on/{FRAMES}"


def _t(k: int) -> str:
    return f"2*PI*{k}*n/{FRAMES}"


# 움직임 (k = 영상 한 번에 도는 바퀴 수 — 정수라야 끊김 없음)
MOTIONS = {
    "breathe": ("🌬️ 숨쉬기", lambda k: _zp(f"1.10+0.08*sin({_p(k)})", f"20*sin({_p(k)})", f"14*cos({_p(k)})")),
    "zoom": ("🔍 다가왔다 멀어지기", lambda k: _zp(f"1.02+0.20*(0.5-0.5*cos({_p(k)}))")),
    "pan": ("↔️ 좌우 이동", lambda k: _zp("1.25", f"(iw-iw/zoom)/2*sin({_p(k)})")),
    "shake": ("📳 두근두근 떨림", lambda k: _zp(f"1.08+0.02*sin({_p(4 * k)})", f"6*sin({_p(6 * k)})", f"5*cos({_p(7 * k)})")),
    "sway": ("🎐 흔들흔들", lambda k: f"scale={SIZE + 160}:{SIZE + 160},rotate='0.035*sin({_t(k)})':c=black,"
                                     f"crop={SIZE}:{SIZE},fps={FPS}"),
    "still": ("🖼️ 움직임 없음", lambda k: f"scale={SIZE}:{SIZE},fps={FPS}"),
}
SPEEDS = {"slow": ("느리게", 1), "normal": ("보통", 2), "fast": ("빠르게", 3)}
COLORS = {
    "none": ("", lambda k: ""),
    "shine": ("✨ 반짝임", lambda k: f"eq=brightness='0.07*sin({_t(2 * k)})':saturation=1.15:eval=frame,vignette=PI/5"),
    "rainbow": ("🌈 무지개", lambda k: f"hue=h='360*{k}*n/{FRAMES}':s=1.3"),
    "neon": ("💜 네온", lambda k: f"eq=contrast=1.25:saturation=1.8,hue=h='25*sin({_t(k)})'"),
    "warm": ("🌅 따뜻하게", lambda k: "colorbalance=rs=.12:gs=.03:bs=-.10"),
    "cool": ("🧊 차갑게", lambda k: "colorbalance=rs=-.10:bs=.15"),
    "mono": ("⚫ 흑백", lambda k: "hue=s=0"),
    "vintage": ("📜 빈티지", lambda k: "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,vignette=PI/4"),
}
# 날리는 것: (이름, 방향 +1 = 아래로 / -1 = 위로, 영상 한 번에 흐르는 칸 수)
PARTICLES = {
    "none": ("", 0, 0),
    "hearts": ("💕 하트", -1, 1),
    "snow": ("❄️ 눈", 1, 1),
    "sparkles": ("✨ 별빛", 1, 1),
    "bubbles": ("🫧 방울", -1, 1),
    "petals": ("🌸 꽃잎", 1, 1),
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


@dataclass(frozen=True)
class Spec:
    motion: str = "breathe"
    speed: str = "slow"
    color: str = "none"
    particles: str = "none"

    def check(self) -> str | None:
        for val, table, name in ((self.motion, MOTIONS, "motion"), (self.speed, SPEEDS, "speed"),
                                 (self.color, COLORS, "color"), (self.particles, PARTICLES, "particles")):
            if val not in table:
                return f"{name} 는 {list(table)} 중 하나"
        return None

    def label(self) -> str:
        parts = [MOTIONS[self.motion][0], COLORS[self.color][0], PARTICLES[self.particles][0]]
        return " · ".join(p for p in parts if p) + f" ({SPEEDS[self.speed][0]})"


PRESETS = {   # 예전 이름 (style) → 부품
    "breathe": Spec("breathe"), "shine": Spec("breathe", color="shine"),
    "rainbow": Spec("breathe", color="rainbow"), "sway": Spec("sway"),
}


# ── 날리는 것 타일 (PIL, 같은 seed → 같은 그림) ────────────────
def _heart(d, x, y, r, fill):
    d.ellipse((x - r, y - r, x, y), fill=fill)
    d.ellipse((x, y - r, x + r, y), fill=fill)
    d.polygon([(x - r, y - r / 2), (x + r, y - r / 2), (x, y + r)], fill=fill)


def _star(d, x, y, r, fill):
    pts = []
    for i in range(8):
        a = math.pi / 4 * i
        rr = r if i % 2 == 0 else r / 3
        pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


def particle_tile(kind: str, seed: int = 7) -> bytes:
    """SIZE×(2·SIZE) 투명 PNG = 같은 타일 두 장 (세로로 흐를 때 이음새 없이)."""
    from PIL import Image, ImageDraw
    tile = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    rnd = random.Random(seed)
    for _ in range(26 if kind in ("snow", "sparkles") else 16):
        x, y, r = rnd.uniform(0, SIZE), rnd.uniform(0, SIZE), rnd.uniform(6, 16)
        for dx in (-SIZE, 0, SIZE):          # 가장자리에 걸친 것도 반대편에 그려 가로 이음새 없앰
            for dy in (-SIZE, 0, SIZE):
                cx, cy = x + dx, y + dy
                if kind == "hearts":
                    _heart(d, cx, cy, r * 1.3, (255, rnd.randint(60, 120), 150, 210))
                elif kind == "snow":
                    d.ellipse((cx - r / 2, cy - r / 2, cx + r / 2, cy + r / 2), fill=(255, 255, 255, 200))
                elif kind == "sparkles":
                    _star(d, cx, cy, r, (255, 240, 170, 230))
                elif kind == "bubbles":
                    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(200, 240, 255, 220), width=3)
                elif kind == "petals":
                    d.ellipse((cx - r, cy - r / 2, cx + r, cy + r / 2), fill=(255, 170, 200, 210))
    sheet = Image.new("RGBA", (SIZE, SIZE * 2), (0, 0, 0, 0))
    sheet.paste(tile, (0, 0))
    sheet.paste(tile, (0, SIZE))
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()


# ── ffmpeg ─────────────────────────────────────────────────
def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:   # 패키지 없음 → 시스템 ffmpeg
        return shutil.which("ffmpeg")


def available() -> bool:
    return ffmpeg_path() is not None


def graph(spec: Spec) -> str:
    """filter_complex 문자열 (코드 상수 + 목록 키만)."""
    k = SPEEDS[spec.speed][1]
    pre = f"scale={SIZE * 2}:{SIZE * 2}:force_original_aspect_ratio=increase,crop={SIZE * 2}:{SIZE * 2}"
    chain = ",".join(x for x in (pre, MOTIONS[spec.motion][1](k), COLORS[spec.color][1](k)) if x)
    _, direction, cells = PARTICLES[spec.particles]
    if not direction:
        return f"[0:v]{chain},format=yuv420p[v]"
    step = f"{SIZE}*mod({cells}*n/{FRAMES},1)"        # 영상 한 번에 정확히 cells 칸
    y = f"{SIZE}-{step}" if direction > 0 else step   # 아래로 = 창이 위로 올라감
    return (f"[0:v]{chain}[b];[1:v]fps={FPS},crop={SIZE}:{SIZE}:0:'{y}',format=rgba[p];"
            f"[b][p]overlay=0:0:shortest=0,format=yuv420p[v]")


def _args(ff: str, src: str, tile: str | None, out: str, spec: Spec, crf: int) -> list[str]:
    # 입력도 FPS 로 (기본 25fps 면 n 기준 효과(흔들·색)가 7.2초 주기가 돼 이음새가 생김 — 실제 버그)
    ins = ["-framerate", str(FPS), "-loop", "1", "-i", src] + (["-framerate", str(FPS), "-loop", "1", "-i", tile] if tile else [])
    return [ff, "-y", "-loglevel", "error", *ins, "-t", str(SECONDS), "-filter_complex", graph(spec), "-map", "[v]",
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


async def make(image: bytes, spec: Spec | str = "breathe") -> bytes:
    """사진 bytes → 규격 MP4 bytes."""
    spec = PRESETS[spec] if isinstance(spec, str) else spec
    err = spec.check()
    if err:
        raise ValueError(err)
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg 없음")
    async with _LOCK:
        with tempfile.TemporaryDirectory(prefix="sodam_ava_") as d:
            src, out = os.path.join(d, "src.img"), os.path.join(d, "out.mp4")
            with open(src, "wb") as f:
                f.write(image)
            tile = None
            if PARTICLES[spec.particles][1]:
                tile = os.path.join(d, "tile.png")
                data = await asyncio.to_thread(particle_tile, spec.particles)
                with open(tile, "wb") as f:
                    f.write(data)
            for crf in (26, 32):   # 2MB 넘으면 화질을 낮춰 한 번 더
                await _run(_args(ff, src, tile, out, spec, crf))
                with open(out, "rb") as f:
                    data = f.read()
                if len(data) <= MAX_BYTES:
                    return data
    raise RuntimeError("2MB 안으로 못 줄임")
