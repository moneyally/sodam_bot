"""🎞️ 움프(움직이는 프로필) — 옛 부품 이름 → 프레임워크 spec 어댑터 (엔진은 하나: sodam/stickerforge).

텔레그램 프로필 영상 규격: 정사각형 ≤800(여기선 640) · ≤10초 · ≤2MB · H.264 · yuv420p · 소리 없음 · faststart.
2026-09-30 (오너 지시 '고정 목록 말고 프레임워크로'): 예전엔 여기서 ffmpeg 필터로 motion 6 × color 8 × particles 5 를 직접 만들었고
AI 가 그 목록만 골라 최근 3일 17건 중 8건이 '줌+네온+별빛' 이었다. 이제 그리기는 전부 stickerforge(움직임·keyframes·입자 방출기·
색 보정·전환·효과)가 하고, 이 파일은 **옛 도구 인자(motion·speed·color·particles)를 그 부품의 값으로 옮기는 얇은 어댑터**만 남긴다.
새 효과를 여기 목록에 더하지 말 것 — prims 의 값으로 표현한다.
- ffmpeg 는 imageio-ffmpeg 정적 바이너리(시스템 ffmpeg 없어도 됨) 또는 PATH 의 ffmpeg (stickerforge.const 도 이걸 씀).
- AI 그림체(AI_STYLES)는 gpt-image 고치기(llm.image) — 효과가 아니라 원본을 바꾸는 단계라 그대로 둠 (panels/avatar.py).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass

SIZE = 640
FPS = 30
MAX_BYTES = 2 * 1024 * 1024


def _keys(k: int, a: dict, b: dict) -> list:
    """a → b → a 를 k 번 (옛 sin 한 바퀴와 같은 모양). 키마다 **움직이는 값을 전부 적는다** — 빠진 값은 prims._fill_keys 가
    앞 키 값으로 채워서, 예전엔 zoom 이 1.0→1.2 로 한 번 커지고 그대로 멈춰 반복 이음새에서 튀었음 (코드 리뷰 2026-09-30)."""
    keys = []
    for i in range(k):
        keys.append({"t": i / k, "ease": "in_out", **a})
        keys.append({"t": (i + 0.5) / k, "ease": "in_out", **b})
    keys.append({"t": 1.0, **a})
    return keys


# 옛 이름: (라벨, k → 프레임워크 motion 목록)
MOTIONS = {
    "breathe": ("🌬️ 숨쉬기", lambda k: [{"type": "keyframes", "keys": _keys(k, {"scale": 1.0, "x": 0.0}, {"scale": 1.08, "x": 0.02})}]),
    "zoom": ("🔍 다가왔다 멀어지기", lambda k: [{"type": "keyframes", "keys": _keys(k, {"scale": 1.0}, {"scale": 1.2})}]),
    "pan": ("↔️ 좌우 이동", lambda k: [{"type": "keyframes", "keys": _keys(k, {"scale": 1.2, "x": -0.07}, {"scale": 1.2, "x": 0.07})}]),
    "shake": ("📳 두근두근 떨림", lambda k: [{"type": "shake", "amp": 6, "freq": 6 * k}]),
    "sway": ("🎐 흔들흔들", lambda k: [{"type": "keyframes", "keys": _keys(k, {"scale": 1.12, "rotate": -2.0}, {"scale": 1.12, "rotate": 2.0})}]),
    "still": ("🖼️ 움직임 없음", lambda k: [{"type": "still"}]),
}
SPEEDS = {"slow": ("느리게", 1), "normal": ("보통", 2), "fast": ("빠르게", 3)}
# 옛 색 이름 → grade 레이어 값
COLORS = {
    "none": ("", lambda k: None),
    "shine": ("✨ 반짝임", lambda k: {"type": "grade", "brightness": [-0.02, 0.07], "cycles": 2 * k, "saturation": 1.15, "vignette": 0.3}),
    "rainbow": ("🌈 무지개", lambda k: {"type": "grade", "hue_spin": k, "saturation": 1.3}),
    "neon": ("💜 네온", lambda k: {"type": "grade", "contrast": 1.25, "saturation": 1.8, "hue_shift": [-25, 25], "cycles": k, "bloom": 0.3}),
    "warm": ("🌅 따뜻하게", lambda k: {"type": "grade", "tint": [255, 170, 90], "tint_amount": 0.15}),
    "cool": ("🧊 차갑게", lambda k: {"type": "grade", "tint": [90, 160, 255], "tint_amount": 0.15}),
    "mono": ("⚫ 흑백", lambda k: {"type": "grade", "saturation": 0}),
    "vintage": ("📜 빈티지", lambda k: {"type": "grade", "saturation": 0.25, "tint": [190, 140, 90], "tint_amount": 0.3, "vignette": 0.45}),
}
# 옛 날리는 것 → particles 레이어 값 (방향·모양만 옛 느낌대로)
PARTICLES = {
    "none": ("", None),
    "hearts": ("💕 하트", {"type": "particles", "shape": "heart", "spawn": "bottom", "angle": -90, "speed": 90, "count": 16, "size": [18, 34]}),
    "snow": ("❄️ 눈", {"type": "particles", "shape": "snow", "spawn": "top", "angle": 90, "speed": 70, "count": 40, "size": [5, 12],
                      "turbulence": 12}),
    "sparkles": ("✨ 별빛", {"type": "particles", "shape": "sparkle", "spawn": "area", "speed": 10, "count": 22, "size": [14, 30],
                           "life": 1.0, "blend": "add"}),
    "bubbles": ("🫧 방울", {"type": "particles", "shape": "bubble", "spawn": "bottom", "angle": -90, "speed": 80, "count": 16,
                          "size": [14, 30], "turbulence": 8}),
    "petals": ("🌸 꽃잎", {"type": "particles", "shape": "petal", "spawn": "top", "angle": 90, "speed": 70, "count": 18,
                         "size": [14, 24], "spin": 180, "turbulence": 14}),
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
    """옛 도구 인자 묶음 (호환용). 알 수 없는 날리는 것 이름이 입자 모양(smoke·flame…)이면 그 모양으로 옮김."""
    motion: str = "breathe"
    speed: str = "slow"
    color: str = "none"
    particles: str = "none"

    def check(self) -> str | None:
        from .stickerforge import prims
        for val, table, name in ((self.motion, MOTIONS, "motion"), (self.speed, SPEEDS, "speed"), (self.color, COLORS, "color"),
                                 (self.particles, PARTICLES, "particles")):
            if val not in table and not (name == "particles" and val in prims.SHAPES):
                return (f"{name} 는 {list(table)} 중 하나 — 목록에 없는 연출은 spec(부품 조합)으로: "
                        "sticker_catalog(요청, for_video=true) 를 보고 spec.layers 에 particles·grade·transition 을 넣을 것")
        return None

    def label(self) -> str:
        pl = PARTICLES[self.particles][0] if self.particles in PARTICLES else f"입자 {self.particles}"
        parts = [MOTIONS[self.motion][0], COLORS[self.color][0], pl]
        return " · ".join(p for p in parts if p) + f" ({SPEEDS[self.speed][0]})"


PRESETS = {   # 예전 이름 (style) → 부품
    "breathe": Spec("breathe"), "shine": Spec("breathe", color="shine"),
    "rainbow": Spec("breathe", color="rainbow"), "sway": Spec("sway"),
}


def to_forge_spec(spec: Spec, seed: int = 1) -> dict:
    """옛 인자 → stickerforge spec (sanitize 전 raw). 사진 꽉 채움·모서리 0·3초 반복 두 바퀴."""
    k = SPEEDS[spec.speed][1]
    layers = []
    grade = COLORS[spec.color][1](k)
    if grade:
        layers.append(grade)
    if spec.particles in PARTICLES:
        if PARTICLES[spec.particles][1]:
            layers.append(dict(PARTICLES[spec.particles][1]))
    else:   # 모양 이름 (예: 옛 인자에 smoke) → 일반 방출기
        layers.append({"type": "particles", "shape": spec.particles, "spawn": "bottom", "angle": -90, "speed": 60, "count": 24,
                       "size": [20, 44], "grow": 2.0, "blur": 3 if spec.particles == "smoke" else 0})
    return {"mode": "photo", "radius": 0, "seed": seed, "motion": MOTIONS[spec.motion][1](k), "layers": layers}


# ── ffmpeg ─────────────────────────────────────────────────
def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:   # 패키지 없음 → 시스템 ffmpeg
        return shutil.which("ffmpeg")


def available() -> bool:
    return ffmpeg_path() is not None


async def make(image: bytes, spec: Spec | str = "breathe", seed: int = 1) -> bytes:
    """사진 bytes → 규격 MP4 bytes (옛 인자 경로도 같은 엔진 stickerforge.forge_video)."""
    from . import stickerforge as SF
    spec = PRESETS[spec] if isinstance(spec, str) else spec
    err = spec.check()
    if err:
        raise ValueError(err)
    fs, err = SF.sanitize(to_forge_spec(spec, seed))
    if err:
        raise ValueError(err)
    res = await SF.forge_video(image, fs)
    if not res.ok:
        raise RuntimeError(res.summary())
    return res.mp4
