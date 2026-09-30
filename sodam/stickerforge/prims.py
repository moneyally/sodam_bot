"""🧱 움프·스티커 공통 부품(프리미티브) — "정해진 몇 개 중 고르기" 말고 **부품 + 값** (CLAUDE.md 교훈, 오너 지시 2026-09-30).

사용자가 말로 부탁한 연출("쌍절곤 휘둘러", "상품권이 타서 없어지게", "담배 연기")을 AI 가 아래 일반 부품의 값으로 옮긴다.
새 효과가 필요하면 이름을 하나 더 박기 전에 여기 값으로 되는지부터 본다.

- 시간: u = 타임라인 위치 0~1 (스티커 2.97초 반복 / 움프 3초×2 반복 / 움프 한 번 6초). 레이어마다 start·end(0~1) 창.
- keyframes: [{t, scale, sx, sy, rotate, x, y, opacity, ease}] + pivot → 아무 움직임 (쌍절곤 = 손잡이 축 빠른 회전).
  이징은 easings.net 공식(Robert Penner 계열: back·bounce·elastic).
- particles: 일반 입자 방출기 (Reeves 1983 '파티클 시스템': 태어남 → 움직임(속도·중력·바람·흔들림) → 모양·크기·투명도 변화 → 죽음).
  모양은 코드로 그림(원·별·하트·불꽃·연기·색종이·잎·꽃잎·눈·방울·동전·지폐·번개·글자…). 반복 모드에선 태어난 시각이
  타임라인에 대해 주기적이라 마지막 장 다음 = 첫 장 (끊김 없음).
- grade: 밝기·대비·채도·색상 돌리기·틴트·비네트·그레인·블룸, 숫자 대신 [a, b] 를 주면 그 사이를 오감(진동).
- flash / lightning: 화면 번쩍 · 번개 줄기.
- transition: 피사체(앞에 그린 것 전체)에 dissolve·burn(노이즈 임계값 + 타는 가장자리 + 불씨)·fade·pixelate·shatter.
  노이즈 = numpy 값 노이즈 여러 옥타브를 순위로 평탄화 → 진행률 p 가 곧 사라진 면적 비율.

전부 PIL/numpy 로만 그림 (서버 파일 경로·임의 코드·수식 없음). 값 범위·개수 상한은 __init__.sanitize 의 LAYER_PARAMS.
CPU: 2vCPU VPS 에서 한 편 ~20초 CPU 안 — 입자 합계·레이어 수·스프라이트 캐시 크기로 묶는다 (tests/test_animation.py 가 잼).
"""
from __future__ import annotations

import math
import random
from functools import lru_cache

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .const import MASTER, S

# ───────────────────────────────────────────── 시간·이징
def _out_bounce(x: float) -> float:
    n1, d1 = 7.5625, 2.75
    if x < 1 / d1:
        return n1 * x * x
    if x < 2 / d1:
        x -= 1.5 / d1
        return n1 * x * x + 0.75
    if x < 2.5 / d1:
        x -= 2.25 / d1
        return n1 * x * x + 0.9375
    x -= 2.625 / d1
    return n1 * x * x + 0.984375


EASES = {   # easings.net (x = 구간 진행 0~1 → 0~1, back·elastic 은 잠깐 넘어감)
    "linear": lambda x: x,
    "in": lambda x: x ** 3,
    "out": lambda x: 1 - (1 - x) ** 3,
    "in_out": lambda x: 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2,
    "back": lambda x: 1 + 2.70158 * (x - 1) ** 3 + 1.70158 * (x - 1) ** 2,
    "bounce": _out_bounce,
    "elastic": lambda x: x if x in (0.0, 1.0) else 2 ** (-10 * x) * math.sin((x * 10 - 0.75) * (2 * math.pi / 3)) + 1,
    "hold": lambda x: 0.0 if x < 1 else 1.0,
}


def window(u: float, start: float = 0.0, end: float = 1.0) -> float | None:
    """레이어 창 [start, end] 안이면 창 안 진행률 0~1, 밖이면 None."""
    if end <= start or u < start or u > end:
        return None
    return (u - start) / (end - start)


def osc(v, u: float, cycles: int = 1) -> float:
    """숫자면 그대로, [a, b] 면 a↔b 를 타임라인에 cycles 번 오감 (정수 번 → 반복 이음새 없음)."""
    if isinstance(v, (list, tuple)):
        a, b = float(v[0]), float(v[1])
        return a + (b - a) * (0.5 - 0.5 * math.cos(2 * math.pi * cycles * u))
    return float(v)


# ───────────────────────────────────────────── keyframes (아무 움직임)
KEY_PROPS = {"scale": 1.0, "sx": 1.0, "sy": 1.0, "rotate": 0.0, "x": 0.0, "y": 0.0, "opacity": 1.0}


def _fill_keys(keys: list, loop: bool) -> list:
    ks = sorted(keys, key=lambda k: k["t"])
    out, prev = [], dict(KEY_PROPS)
    for k in ks:
        cur = {p: float(k.get(p, prev[p])) for p in KEY_PROPS}
        cur["t"], cur["ease"] = float(k["t"]), k.get("ease") if k.get("ease") in EASES else "in_out"
        out.append(cur)
        prev = cur
    if not out:
        out = [{**KEY_PROPS, "t": 0.0, "ease": "linear"}]
    if out[0]["t"] > 0:
        out.insert(0, {**out[0], "t": 0.0})
    if out[-1]["t"] < 1:
        # 반복이면 첫 키로 돌아와 끝 다음 장 = 첫 장, 한 번이면 마지막 값 유지
        out.append({**(out[0] if loop else out[-1]), "t": 1.0})
    return out


def key_value(keys: list, u: float) -> dict:
    u = min(max(u, 0.0), 1.0)
    for a, b in zip(keys, keys[1:]):
        if a["t"] <= u <= b["t"]:
            span = b["t"] - a["t"]
            e = EASES[a["ease"]](0.0 if span <= 0 else (u - a["t"]) / span)
            return {p: a[p] + (b[p] - a[p]) * e for p in KEY_PROPS}
    return {p: keys[-1][p] for p in KEY_PROPS}


def keyframes(keys: list, pivot=None, loop: bool = True, engine_pivot=(MASTER / 2, MASTER / 2)):
    """→ f(u) = ((angle, sx, sy, dx, dy, shear), opacity). x·y 는 화면 비율(1 = 한 변), rotate 양수 = 시계 방향.
    pivot [0~1, 0~1] 을 축으로 돌고 커짐 (엔진은 자기 축으로 변환하므로 A(c - p) + p - c 만큼 옮겨 맞춤)."""
    ks = _fill_keys(keys, loop)
    cx, cy = engine_pivot
    px, py = (pivot[0] * MASTER, pivot[1] * MASTER) if pivot else (cx, cy)

    def f(u):
        v = key_value(ks, u)
        a = math.radians(v["rotate"])
        sx, sy = v["scale"] * v["sx"], v["scale"] * v["sy"]
        vx, vy = cx - px, cy - py
        ax = math.cos(a) * sx * vx - math.sin(a) * sy * vy
        ay = math.sin(a) * sx * vx + math.cos(a) * sy * vy
        return ((v["rotate"], sx, sy, ax + px - cx + v["x"] * MASTER, ay + py - cy + v["y"] * MASTER, 0.0),
                min(max(v["opacity"], 0.0), 1.0))
    return f


# ───────────────────────────────────────────── 색
def rgb(c, default=(255, 255, 255)) -> tuple:
    if isinstance(c, (list, tuple)) and len(c) == 3:
        return tuple(int(min(max(x, 0), 255)) for x in c)
    return tuple(default)


# ───────────────────────────────────────────── 입자 모양 (코드로 그림, 파일 없음)
SHAPES = {   # 이름: (한 줄 설명, 기본 색)
    "circle": ("동그라미", (255, 255, 255)), "ring": ("고리", (255, 255, 255)), "square": ("네모", (255, 255, 255)),
    "triangle": ("세모", (255, 255, 255)), "diamond": ("마름모", (180, 230, 255)), "star": ("별(다섯 꼭지)", (255, 225, 90)),
    "sparkle": ("반짝(네 꼭지 빛)", (255, 252, 235)), "heart": ("하트", (255, 92, 120)),
    "spark": ("불티·빛 알갱이 (add 권장)", (255, 170, 60)), "flame": ("불꽃", (255, 120, 20)),
    "smoke": ("연기 뭉치 (부드러운 흐림)", (190, 190, 195)), "confetti": ("색종이 조각", (255, 90, 110)),
    "leaf": ("나뭇잎", (90, 170, 70)), "petal": ("꽃잎", (255, 170, 200)), "snow": ("눈송이(부드러운 점)", (255, 255, 255)),
    "snowflake": ("눈 결정(육각)", (220, 240, 255)), "bubble": ("비눗방울", (200, 240, 255)), "coin": ("동전", (255, 200, 60)),
    "bill": ("지폐", (80, 170, 90)), "bolt": ("번개 모양", (255, 235, 80)), "drop": ("물방울", (120, 190, 255)),
    "char": ("글자 1~2자 (char 인자, 한글·영문·숫자)", (255, 255, 255)),
}
ROUND = {"circle", "ring", "spark", "smoke", "snow", "bubble"}          # 돌려도 같음 → 회전 안 함 (캐시 절약)
UPRIGHT = {"flame", "drop", "bolt", "heart", "char", "bill"}            # 똑바로 서 있어야 자연스러움 → spin 을 줄 때만 돎
EMOJI = {"❤": "heart", "♥": "heart", "💕": "heart", "💖": "heart", "⭐": "star", "★": "star", "☆": "star", "🌟": "star",
         "✨": "sparkle", "🔥": "flame", "💨": "smoke", "🎉": "confetti", "🎊": "confetti", "🍃": "leaf", "🍂": "leaf",
         "🍁": "leaf", "🌸": "petal", "❄": "snowflake", "☃": "snow", "🫧": "bubble", "🪙": "coin", "💰": "coin",
         "💵": "bill", "💸": "bill", "⚡": "bolt", "💧": "drop", "💦": "drop"}
FONT_PATH = __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "data_files", "fonts", "BlackHanSans.ttf")   # OFL


def glyph_ok(ch: str) -> bool:
    """글꼴에 있는 글자인가 (없는 글자 = 빈 상자 → 쓰지 않음). 이모지는 대개 없음 → EMOJI 로 모양 대신."""
    try:
        f = ImageFont.truetype(FONT_PATH, 40)
        return all(f.getmask(c).getbbox() is not None for c in ch if not c.isspace())
    except Exception:   # 글꼴 문제 → 글자 안 씀
        return False


def _draw_shape(shape: str, c: int, col: tuple, char: str) -> Image.Image:
    """c×c RGBA (곧은 알파). c = 4배 크기로 그린 뒤 줄여서 가장자리가 매끈."""
    im = Image.new("RGBA", (c, c), col + (0,))
    m = Image.new("L", (c, c), 0)
    d = ImageDraw.Draw(m)
    q = c / 100.0
    P = lambda pts: [(x * q, y * q) for x, y in pts]   # noqa: E731
    if shape == "circle":
        d.ellipse(P([(8, 8), (92, 92)]), fill=255)
    elif shape == "ring":
        d.ellipse(P([(10, 10), (90, 90)]), outline=255, width=int(12 * q))
    elif shape == "square":
        d.rectangle(P([(15, 15), (85, 85)]), fill=255)
    elif shape == "triangle":
        d.polygon(P([(50, 8), (92, 88), (8, 88)]), fill=255)
    elif shape == "diamond":
        d.polygon(P([(50, 4), (80, 50), (50, 96), (20, 50)]), fill=255)
    elif shape == "star":
        pts = [(50 + (46 if i % 2 == 0 else 19) * math.cos(-math.pi / 2 + i * math.pi / 5),
                52 + (46 if i % 2 == 0 else 19) * math.sin(-math.pi / 2 + i * math.pi / 5)) for i in range(10)]
        d.polygon(P(pts), fill=255)
    elif shape == "sparkle":
        d.polygon(P([(50, 2), (57, 43), (98, 50), (57, 57), (50, 98), (43, 57), (2, 50), (43, 43)]), fill=255)
        m = Image.eval(m, lambda v: v).filter(ImageFilter.GaussianBlur(q * 1.2))
        glow = m.filter(ImageFilter.GaussianBlur(q * 8)).point(lambda v: int(v * 0.8))
        m = ImageChops.lighter(m, glow)
    elif shape == "heart":
        d.ellipse(P([(6, 10), (52, 58)]), fill=255)
        d.ellipse(P([(48, 10), (94, 58)]), fill=255)
        d.polygon(P([(7, 40), (93, 40), (50, 95)]), fill=255)
    elif shape in ("spark", "smoke", "snow"):
        yy, xx = np.mgrid[0:c, 0:c].astype(np.float32)
        r = np.hypot(xx - c / 2, yy - c / 2) / (c / 2)
        if shape == "spark":
            a = np.clip(1 - r, 0, 1) ** 2.2
            core = np.clip(1 - r * 3, 0, 1)[..., None]
            rgbv = np.array(col, np.float32) * (1 - core) + 255 * core
            arr = np.dstack([rgbv, a[..., None] * 255]).astype(np.uint8)
            return Image.fromarray(arr, "RGBA")
        if shape == "smoke":
            rnd = np.random.RandomState(7)
            tex = np.asarray(Image.fromarray((rnd.rand(6, 6) * 255).astype(np.uint8)).resize((c, c), Image.BICUBIC),
                             np.float32) / 255
            a = np.exp(-(r ** 2) * 3.2) * (0.7 + 0.3 * tex) * (r < 1)
        else:
            a = np.clip((1 - r) * 2.2, 0, 1)
        m = Image.fromarray((a * 255).astype(np.uint8), "L")
    elif shape == "flame":
        d.ellipse(P([(22, 42), (78, 96)]), fill=255)
        d.polygon(P([(24, 64), (50, 2), (76, 64)]), fill=255)
        m = m.filter(ImageFilter.GaussianBlur(q * 3))
        yy, xx = np.mgrid[0:c, 0:c].astype(np.float32) / c
        hot = np.clip(1 - np.hypot(xx - 0.5, (yy - 0.72) * 0.8) * 3.2, 0, 1)[..., None]   # 아래 가운데가 노랗게
        rgbv = np.array(col, np.float32) * (1 - hot) + np.array((255, 245, 170), np.float32) * hot
        return Image.fromarray(np.dstack([rgbv, np.asarray(m, np.float32)[..., None]]).astype(np.uint8), "RGBA")
    elif shape == "confetti":
        d.rectangle(P([(10, 32), (90, 68)]), fill=255)
    elif shape == "leaf":
        d.chord(P([(4, 20), (96, 130)]), 200, 340, fill=255)
        d.chord(P([(4, -30), (96, 80)]), 20, 160, fill=255)
        im2 = Image.new("RGBA", (c, c), col + (0,))
        im2.putalpha(m)
        ImageDraw.Draw(im2).line(P([(10, 50), (90, 50)]), fill=tuple(int(x * 0.6) for x in col) + (255,), width=max(1, int(3 * q)))
        return im2
    elif shape == "petal":
        d.ellipse(P([(4, 26), (96, 74)]), fill=255)
        d.polygon(P([(84, 50), (100, 38), (100, 62)]), fill=0)
    elif shape == "snowflake":
        for i in range(3):
            a = i * math.pi / 3
            d.line(P([(50 - 44 * math.cos(a), 50 - 44 * math.sin(a)), (50 + 44 * math.cos(a), 50 + 44 * math.sin(a))]),
                   fill=255, width=int(8 * q))
        d.ellipse(P([(40, 40), (60, 60)]), fill=255)
    elif shape == "bubble":
        d.ellipse(P([(8, 8), (92, 92)]), outline=230, width=int(6 * q))
        d.ellipse(P([(26, 22), (44, 38)]), fill=255)
        d.ellipse(P([(12, 12), (88, 88)]), fill=40)
        d.ellipse(P([(8, 8), (92, 92)]), outline=230, width=int(6 * q))
        d.ellipse(P([(26, 22), (44, 38)]), fill=255)
    elif shape == "coin":
        im2 = Image.new("RGBA", (c, c), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(im2)
        dark = tuple(int(x * 0.62) for x in col)
        d2.ellipse(P([(6, 6), (94, 94)]), fill=dark + (255,))
        d2.ellipse(P([(12, 12), (88, 88)]), fill=col + (255,))
        d2.ellipse(P([(26, 26), (74, 74)]), outline=dark + (255,), width=int(5 * q))
        d2.ellipse(P([(22, 16), (40, 30)]), fill=(255, 255, 230, 200))
        return im2
    elif shape == "bill":
        im2 = Image.new("RGBA", (c, c), (0, 0, 0, 0))
        d2 = ImageDraw.Draw(im2)
        dark = tuple(int(x * 0.55) for x in col)
        d2.rectangle(P([(4, 26), (96, 74)]), fill=col + (255,), outline=dark + (255,), width=int(4 * q))
        d2.ellipse(P([(38, 36), (62, 64)]), outline=dark + (255,), width=int(4 * q))
        return im2
    elif shape == "bolt":
        d.polygon(P([(58, 2), (18, 56), (46, 56), (34, 98), (84, 38), (54, 38), (70, 2)]), fill=255)
    elif shape == "drop":
        d.ellipse(P([(22, 40), (78, 96)]), fill=255)
        d.polygon(P([(24, 62), (50, 2), (76, 62)]), fill=255)
    elif shape == "char":
        text = char or "?"
        size = int(c * (0.8 if len(text) == 1 else 0.5))
        f = ImageFont.truetype(FONT_PATH, max(8, size))
        box = d.textbbox((0, 0), text, font=f, stroke_width=int(c * 0.04))
        w, h = box[2] - box[0], box[3] - box[1]
        im2 = Image.new("RGBA", (c, c), (0, 0, 0, 0))
        ImageDraw.Draw(im2).text(((c - w) / 2 - box[0], (c - h) / 2 - box[1]), text, font=f, fill=col + (255,),
                                 stroke_width=int(c * 0.04), stroke_fill=(20, 20, 20, 255))
        return im2
    im.putalpha(m)
    return im


def _q(v: float) -> int:
    """크기 양자화 (캐시 적중↑): 작으면 1px, 크면 약 8% 단위."""
    v = max(2, int(round(v)))
    step = max(1, v // 12)
    return v - v % step


@lru_cache(maxsize=320)   # uint8 배열 — 160px 스프라이트도 100KB, 상한 ~32MB
def sprite(shape: str, size: int, col: tuple, rot: int = 0, blur: int = 0, char: str = "") -> np.ndarray:
    """→ (h, w, 4) uint8 곱해진(premultiplied) RGBA. rot 는 15° 단위, blur 는 px."""
    ss = 4 if size <= 64 else 2
    im = _draw_shape(shape, size * ss, col, char).resize((size, size), Image.LANCZOS)
    if rot and shape not in ROUND:
        im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
    if blur:
        pad = blur * 2
        big = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad), (0, 0, 0, 0))
        big.paste(im, (pad, pad))
        im = big.filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(im, np.float32)
    a[..., :3] *= a[..., 3:4] / 255.0
    return a.astype(np.uint8)


def _paste(acc: np.ndarray, spr: np.ndarray, cx: float, cy: float, alpha: float, add: bool) -> None:
    """acc(float32 곱해진 RGBA) 에 스프라이트를 가운데 (cx, cy) 로 겹침. 화면 밖은 잘라냄."""
    h, w = spr.shape[:2]
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    ax0, ay0, ax1, ay1 = max(0, x0), max(0, y0), min(S, x0 + w), min(S, y0 + h)
    if ax0 >= ax1 or ay0 >= ay1:
        return
    src = spr[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0].astype(np.float32) * alpha
    dst = acc[ay0:ay1, ax0:ax1]
    if add:
        dst += src
    else:
        dst *= 1 - src[..., 3:4] / 255.0
        dst += src


def composite(frame: Image.Image, acc: np.ndarray, add: bool) -> Image.Image:
    """곱해진 입자 레이어를 프레임(곧은 알파) 위에. add = 빛처럼 더하기(불티·네온), 아니면 보통 덮기.
    합치기는 PIL C 함수(alpha_composite·add·lighter) — numpy 로 전체 화면을 나누면 프레임당 ~27ms 라 2vCPU 에서 너무 느림."""
    la = np.clip(acc[..., 3], 0, 255)
    if add:
        glow = Image.fromarray(np.clip(acc[..., :3], 0, 255).astype(np.uint8), "RGB")
        r, g, b, a = frame.split()
        rgbim = ImageChops.add(Image.merge("RGB", (r, g, b)), glow)
        return Image.merge("RGBA", (*rgbim.split(), ImageChops.lighter(a, Image.fromarray(la.astype(np.uint8), "L"))))
    straight = acc[..., :3] * (255.0 / np.maximum(la, 1.0))[..., None]
    layer = Image.fromarray(np.dstack([np.clip(straight, 0, 255), la]).astype(np.uint8), "RGBA")
    out = frame.copy()
    out.alpha_composite(layer)
    return out


def _spawn(spawn, rng, at, spread, box):
    """태어나는 곳 (512 좌표)."""
    s = S
    if spawn == "top":
        return rng.uniform(0, s), -8.0
    if spawn == "bottom":
        return rng.uniform(0, s), s + 8.0
    if spawn == "left":
        return -8.0, rng.uniform(0, s)
    if spawn == "right":
        return s + 8.0, rng.uniform(0, s)
    if spawn == "edges":
        return _spawn(rng.choice(["top", "bottom", "left", "right"]), rng, at, spread, box)
    if spawn == "area":
        return rng.uniform(0, s), rng.uniform(0, s)
    if spawn in ("around", "subject"):
        x0, y0, x1, y1 = box or (s * 0.15, s * 0.15, s * 0.85, s * 0.85)
        cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
        a = rng.uniform(0, 2 * math.pi)
        k = rng.uniform(0.95, 1.1) if spawn == "around" else math.sqrt(rng.uniform(0, 1)) * 0.9
        return cx + rx * k * math.cos(a), cy + ry * k * math.sin(a)
    if spawn == "center":
        at = (0.5, 0.5)
    return at[0] * s + rng.gauss(0, spread * s), at[1] * s + rng.gauss(0, spread * s)   # point / center


def particles(frame, u, ctx, shape="circle", char="", colors=None, color=None, count=30, size=(10, 24), spawn="top",
              at=(0.5, 0.5), spread=0.05, angle=90, angle_spread=20, speed=120, gravity=0, wind=0, spin=0, life=2.0,
              fade="both", grow=1.0, blend="normal", blur=0, opacity=0.9, turbulence=0, burst=False,
              start=0.0, end=1.0, _layer=0, **_):
    """입자 방출기: 모양·색·개수·크기·나오는 곳·방향(angle 0=오른쪽 90=아래 -90=위)·속도·중력·바람·회전·수명·흐려짐·커짐·섞기."""
    tl = ctx["timeline"]
    T = tl["seconds"]
    full = start <= 0 and end >= 1 and not burst
    if not full and window(u, start, 1.0) is None:
        return frame
    rng0 = random.Random(f"{ctx['seed']}:p{_layer}")
    cols = [rgb(c) for c in (colors or [])] or [rgb(color, SHAPES.get(shape, ("", (255, 255, 255)))[1])]
    lo, hi = (size if isinstance(size, (list, tuple)) else (size, size))
    life_u = min(life / T, 0.95) if full else life / T
    add = blend == "add"
    acc = np.zeros((S, S, 4), np.float32)
    box = ctx.get("subject_box")
    drawn = 0
    for i in range(int(count)):
        r = random.Random(rng0.random())
        if full:
            b = (i + r.random()) / count                                 # 고르게 퍼진 태어난 시각 (주기적)
            age_u = (u - b) % 1.0
        else:
            b = start + (r.uniform(0, 0.06) if burst else (i + r.random()) / count * max(0.0, end - start))
            age_u = u - b
        if not (0 <= age_u < life_u):
            continue
        a_s = age_u * T
        k = age_u / life_u
        x0, y0 = _spawn(spawn, r, at, spread, box)
        ang = math.radians(angle + r.uniform(-angle_spread, angle_spread))
        sp = speed * r.uniform(0.7, 1.3)
        ph = r.uniform(0, 2 * math.pi)
        x = x0 + sp * math.cos(ang) * a_s + 0.5 * wind * a_s * a_s + turbulence * math.sin(2 * math.pi * 0.8 * a_s + ph)
        y = y0 + sp * math.sin(ang) * a_s + 0.5 * gravity * a_s * a_s + turbulence * 0.5 * math.cos(2 * math.pi * 0.6 * a_s + ph)
        sz = r.uniform(lo, hi) * (1 + (grow - 1) * k)
        al = opacity
        if fade in ("in", "both"):
            al *= min(1.0, k / 0.15)
        if fade in ("out", "both"):
            al *= min(1.0, (1 - k) / 0.4)
        if al <= 0.01 or sz < 1.5:
            continue
        rot = 0
        if shape not in ROUND:
            rot0 = 0.0 if shape in UPRIGHT else r.uniform(0, 360)
            rot = int(round((rot0 + spin * a_s) / 15.0)) * 15 % 360
        spr = sprite(shape, _q(sz), cols[i % len(cols)], rot, int(blur), char if shape == "char" else "")
        _paste(acc, spr, x, y, al, add)
        drawn += 1
    if not drawn:
        return frame
    return composite(frame, acc, add)


# ───────────────────────────────────────────── 색 보정 (grade)
_YY, _XX = np.mgrid[0:S, 0:S].astype(np.float32)
_R = np.hypot(_XX - S / 2, _YY - S / 2) / (S * 0.72)


def _hue_matrix(deg: float) -> np.ndarray:
    """YIQ 공간 회전 (색상 돌리기) — 채널별 선형 연산이라 HSV 변환보다 빠름."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    to = np.array([[0.299, 0.587, 0.114], [0.596, -0.274, -0.322], [0.211, -0.523, 0.312]], np.float32)
    rot = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)
    return np.linalg.inv(to) @ rot @ to


@lru_cache(maxsize=16)
def _vignette_mask(k100: int) -> Image.Image:
    return Image.fromarray((np.clip(1 - k100 / 100 * np.clip(_R, 0, 1.2) ** 2, 0, 1) * 255).astype(np.uint8), "L")


def grade(frame, u, ctx, brightness=0.0, contrast=1.0, saturation=1.0, hue_shift=0.0, hue_spin=0, tint=None,
          tint_amount=0.0, vignette=0.0, grain=0.0, bloom=0.0, cycles=1, start=0.0, end=1.0, **_):
    """색 보정: 밝기·대비·채도·색상·틴트·비네트·그레인·블룸. 값에 [a, b] 를 주면 그 사이를 cycles 번 오감."""
    if window(u, start, end) is None:
        return frame
    b, c, sat = osc(brightness, u, cycles), osc(contrast, u, cycles), osc(saturation, u, cycles)
    hue = osc(hue_shift, u, cycles) + 360.0 * int(hue_spin) * u
    ta, vig = osc(tint_amount, u, cycles), osc(vignette, u, cycles)
    # 밝기·대비·채도·색상·틴트는 전부 선형 → 3×4 행렬 하나로 합쳐 PIL convert(matrix) 한 번 (C, 프레임당 수 ms)
    m = np.eye(3, dtype=np.float64)
    if hue % 360:
        m = _hue_matrix(hue).astype(np.float64)
    if sat != 1:
        luma = np.array([[0.299, 0.587, 0.114]] * 3)
        m = (luma + sat * (np.eye(3) - luma)) @ m
    off = np.full(3, b * 255.0)
    m, off = c * m, c * off + 128 * (1 - c)
    if tint is not None and ta:
        m, off = (1 - ta) * m, (1 - ta) * off + ta * np.array(rgb(tint), np.float64)
    r_, g_, b_, a_ = frame.split()
    im = Image.merge("RGB", (r_, g_, b_))
    if not (np.allclose(m, np.eye(3)) and np.allclose(off, 0)):
        im = im.convert("RGB", tuple(float(x) for row in np.hstack([m, off[:, None]]) for x in row))
    if vig:
        mask = _vignette_mask(int(round(min(max(vig, 0), 1) * 100)))
        im = ImageChops.multiply(im, Image.merge("RGB", (mask, mask, mask)))
    bl = osc(bloom, u, cycles)
    if bl:
        small = im.resize((128, 128), Image.BILINEAR).point(lambda v: int(min(255, max(0, v - 170) * 1.6 * min(bl, 1.2))))
        im = ImageChops.add(im, small.filter(ImageFilter.GaussianBlur(5)).resize((S, S), Image.BILINEAR))
    if grain:
        n = int(round(u * ctx["timeline"]["nf"])) % ctx["timeline"]["nf"]      # 프레임 번호로 재시드 (반복 이음새 없음)
        x = np.asarray(im, np.int16) + np.random.RandomState(ctx["seed"] * 31 + n).normal(0, grain * 255, (S, S, 1)).astype(np.int16)
        im = Image.fromarray(np.clip(x, 0, 255).astype(np.uint8), "RGB")
    return Image.merge("RGBA", (*im.split(), a_))


# ───────────────────────────────────────────── 번쩍·번개
def _times(times, count, start, end):
    if times:
        return [float(t) for t in times]
    n = max(1, int(count))
    return [start + (end - start) * (i + 0.3) / n for i in range(n)]


def flash(frame, u, ctx, times=None, count=2, color=(255, 255, 255), strength=0.6, decay=0.06, start=0.0, end=1.0, **_):
    """화면 번쩍 (카메라 플래시·충격): times(0~1) 또는 count 번, color 로 strength 만큼 섞였다 decay 동안 사라짐."""
    k = 0.0
    for t0 in _times(times, count, start, end):
        d = u - t0
        if 0 <= d < decay:
            k = max(k, 1 - d / decay)
    if k <= 0:
        return frame
    a = np.asarray(frame, np.float32).copy()
    a[..., :3] += (np.array(rgb(color), np.float32) - a[..., :3]) * (strength * k)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")


def lightning(frame, u, ctx, times=None, count=2, color=(190, 170, 255), origin=(0.5, 0.0), target=(0.5, 0.6),
              branches=2, width=4, strength=0.35, decay=0.08, start=0.0, end=1.0, _layer=0, **_):
    """번개: origin→target 지그재그 줄기(가지 branches) + 화면 번쩍. times(0~1) 또는 count 번."""
    out = frame
    for j, t0 in enumerate(_times(times, count, start, end)):
        d = u - t0
        if not (0 <= d < decay):
            continue
        k = 1 - d / decay
        rng = random.Random(f"{ctx['seed']}:l{_layer}:{j}")
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer)
        x0, y0, x1, y1 = origin[0] * S, origin[1] * S, target[0] * S, target[1] * S
        paths = []
        pts = [(x0, y0)]
        for i in range(1, 13):
            f = i / 12
            pts.append((x0 + (x1 - x0) * f + rng.uniform(-22, 22), y0 + (y1 - y0) * f + rng.uniform(-8, 8)))
        paths.append(pts)
        for _b in range(int(branches)):
            s = pts[rng.randrange(2, 9)]
            ang = math.atan2(y1 - y0, x1 - x0) + rng.choice([-1, 1]) * rng.uniform(0.4, 0.9)
            bp = [s]
            for i in range(5):
                bp.append((bp[-1][0] + math.cos(ang) * 20 + rng.uniform(-8, 8), bp[-1][1] + math.sin(ang) * 20 + rng.uniform(-8, 8)))
            paths.append(bp)
        for p in paths:
            dr.line(p, fill=rgb(color) + (int(230 * k),), width=int(width) * 3, joint="curve")
        layer = layer.filter(ImageFilter.GaussianBlur(4))
        dr = ImageDraw.Draw(layer)
        for p in paths:
            dr.line(p, fill=(255, 255, 255, int(255 * k)), width=max(1, int(width)), joint="curve")
        out = flash(out, u, ctx, times=[t0], color=color, strength=strength, decay=decay)
        out = out.copy()
        out.alpha_composite(layer)
    return out


# ───────────────────────────────────────────── 노이즈 (dissolve·burn 용)
def _rank(total: np.ndarray) -> np.ndarray:
    """값 → 순위 0~1 (평탄화: 임계값 p 아래 픽셀 비율 = p → 진행률이 곧 사라진 면적)."""
    order = total.ravel().argsort(kind="stable")
    ranks = np.empty(S * S, np.float32)
    ranks[order] = np.linspace(0, 1, S * S, dtype=np.float32)
    return ranks.reshape(S, S)


def _fbm(seed: int, scale: int) -> np.ndarray:
    """512×512 값 노이즈 3옥타브(fbm): 작은 난수 격자를 BICUBIC 으로 키워 더함
    (pvigier/perlin-numpy 의 옥타브 합과 같은 생각 — 기울기 대신 격자 값만 써서 더 빠름). scale = 덩어리 크기(px)."""
    rng = np.random.RandomState(seed % (2 ** 31))
    total = np.zeros((S, S), np.float32)
    amp, cells = 1.0, max(2, S // max(4, int(scale)))
    for _ in range(3):
        g = rng.rand(cells + 1, cells + 1).astype(np.float32)
        up = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize((S, S), Image.BICUBIC), np.float32) / 255
        total += amp * up
        amp *= 0.5
        cells = min(S, cells * 2)
    return total / 1.75


ORIGINS = ("noise", "bottom", "top", "left", "right", "center", "edges")


@lru_cache(maxsize=6)
def noise(seed: int, scale: int, origin: str = "noise") -> np.ndarray:
    """사라지는 순서 지도 (0 = 먼저). noise = 얼룩덜룩 흩어짐, 나머지 = 그쪽부터 번짐(종이가 아래서부터 타듯) + 노이즈로 들쭉날쭉한 불 경계."""
    n = _fbm(seed, scale)
    if origin == "noise":
        return _rank(n)
    g = {"bottom": (S - _YY) / S, "top": _YY / S, "left": _XX / S, "right": (S - _XX) / S,
         "center": np.hypot(_XX - S / 2, _YY - S / 2) / (S * 0.71),
         "edges": 1 - np.minimum.reduce([_XX, S - _XX, _YY, S - _YY]) / (S / 2)}.get(origin, _YY / S)
    if origin == "edges":
        g = 1 - g
    return _rank(0.6 * g + 0.4 * n)


def transition(frame, u, ctx, kind="dissolve", direction="out", start=0.1, end=0.9, scale=48, edge=0.04,
               edge_color=(255, 150, 40), embers=30, tiles=6, to=None, origin=None, _layer=0, **_):
    """피사체 사라지기/나타나기: dissolve(노이즈로 흩어짐) · burn(타는 가장자리 + 까맣게 그을림 + 불씨) · fade · pixelate · shatter(조각나 흩어짐).
    direction out=사라짐 in=나타남, start~end 동안 진행 (끝난 뒤엔 그 상태 유지), to=[r,g,b] 면 사라진 자리를 그 색으로.
    origin = 어디부터 (noise 흩어짐 · bottom·top·left·right·center·edges 그쪽부터 번짐; burn 기본 bottom), scale = 얼룩 크기 px."""
    if u < start:
        p = 0.0
    elif u > end:
        p = 1.0
    else:
        p = (u - start) / max(1e-6, end - start)
    if direction == "in":
        p = 1 - p
    if p <= 0:
        return frame
    a = np.asarray(frame, np.float32).copy()
    if p >= 1:                                                           # 끝: 다 사라짐 (노이즈 최댓값 한 점까지)
        a[..., 3] = 0
        kind = "fade"
    al = a[..., 3]
    if kind == "fade":
        al *= 1 - p
    elif kind == "pixelate":
        b = max(1, int(2 + p * 40))
        im = Image.fromarray(a.astype(np.uint8), "RGBA")
        im = im.resize((max(1, S // b), max(1, S // b)), Image.BILINEAR).resize((S, S), Image.NEAREST)
        a = np.asarray(im, np.float32).copy()
        al = a[..., 3]
        if p > 0.6:
            al *= 1 - (p - 0.6) / 0.4
    elif kind == "shatter":
        n = int(tiles)
        tw = S // n
        out = np.zeros_like(a)
        rng = random.Random(f"{ctx['seed']}:s{_layer}")
        e = p * p
        for ty in range(n):
            for tx in range(n):
                cx, cy = (tx + 0.5) * tw - S / 2, (ty + 0.5) * tw - S / 2
                ang = math.atan2(cy, cx) + rng.uniform(-0.5, 0.5)
                v = rng.uniform(200, 420)
                ox = int(math.cos(ang) * v * e)
                oy = int(math.sin(ang) * v * e + 500 * e * e)
                piece = a[ty * tw:(ty + 1) * tw, tx * tw:(tx + 1) * tw].copy()
                piece[..., 3] *= max(0.0, 1 - p ** 1.5)
                x0, y0 = tx * tw + ox, ty * tw + oy
                sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(S, x0 + tw), min(S, y0 + tw)
                if sx0 < sx1 and sy0 < sy1:
                    src = piece[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
                    keep = src[..., 3:4] > out[sy0:sy1, sx0:sx1, 3:4]
                    out[sy0:sy1, sx0:sx1] = np.where(keep, src, out[sy0:sy1, sx0:sx1])
        a, al = out, out[..., 3]
    else:   # dissolve / burn: 노이즈 임계값
        n = noise(int(ctx["seed"]) * 7 + int(_layer), int(scale), origin or ("bottom" if kind == "burn" else "noise"))
        w = max(0.005, float(edge))
        th = p * (1 + w) - w                                            # p=0 → 다 보임, p=1 → 다 사라짐
        gone = n < th
        band = (n >= th) & (n < th + w)                                  # 타는 가장자리
        if kind == "burn":
            char = (n >= th + w) & (n < th + 3 * w)                      # 그을린 띠
            a[char, :3] *= 0.35
            ec = np.array(rgb(edge_color), np.float32)
            k = 1 - (n[band] - th) / w                                   # 경계에 가까울수록 밝게
            hot = ec + (np.array((255, 240, 180), np.float32) - ec) * k[:, None] ** 2
            a[band, :3] = hot
            a[band, 3] = np.maximum(a[band, 3], 200 * (al[band] > 0))
        elif edge_color is not None and w > 0.006:
            a[band, :3] = a[band, :3] * 0.4 + np.array(rgb(edge_color), np.float32) * 0.6
        al = a[..., 3]
        al[gone] = 0
        if kind == "burn" and embers and band.any() and 0 < p < 1:
            ys, xs = np.nonzero(band)
            fr = int(round(u * ctx["timeline"]["nf"]))
            rr = random.Random(f"{ctx['seed']}:e{_layer}:{fr}")
            acc = np.zeros((S, S, 4), np.float32)
            for _i in range(min(int(embers), len(xs))):
                j = rr.randrange(len(xs))
                sz = rr.choice((5, 7, 9))
                _paste(acc, sprite("spark", sz, rgb(edge_color)), xs[j] + rr.uniform(-4, 4), ys[j] - rr.uniform(0, 30), rr.uniform(0.5, 1), True)
            a[..., 3] = al
            base = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")
            base = composite(base, acc, True)
            a = np.asarray(base, np.float32).copy()
            al = a[..., 3]
    a[..., 3] = al
    if to is not None:
        col = np.array(rgb(to), np.float32)
        fa = a[..., 3:4] / 255.0
        a[..., :3] = a[..., :3] * fa + col * (1 - fa)
        a[..., 3] = 255
    a[a[..., 3] <= 0, :3] = 0
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")


LAYERS = {"particles": particles, "grade": grade, "flash": flash, "lightning": lightning, "transition": transition}
TRANSITIONS = ("dissolve", "burn", "fade", "pixelate", "shatter")
SPAWNS = ("top", "bottom", "left", "right", "edges", "center", "point", "area", "around", "subject")
FADES = ("none", "in", "out", "both")
BLENDS = ("normal", "add")
