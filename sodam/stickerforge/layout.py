"""글자·도형 레이어 (스티커·움프 공통 부품) — '그림 1장 + 글자 1줄 고정' 을 없앰 (2026-10-07 오너 '프레임워크로').

text  = 글자 덩어리 하나: 위치(at)·최대 폭(width)·크기(size, 넘치면 자동 축소)·여러 줄('\\n', 4줄)·글꼴(font)·
        채우기 색 또는 위→아래 그라데이션(colors)·테두리(stroke)·입체 그림자(depth)·기울기(rotate)·등장(enter)·계속 움직임(idle).
shape = 도형 하나: 말풍선(bubble, 꼬리 tail 좌표)·사각·둥근 사각·타원·별·폭발(burst) — 위치·크기(wh)·색·테두리·등장·움직임 같은 값.
레이어는 순서대로 그려짐 → 말풍선 다음에 글자를 두면 말풍선 안 글자. 피사체 위치·크기는 motion keyframes 한 점(scale·x·y)으로.
값은 __init__.layer_params 형식으로만 들어옴(경로·코드 없음). 그림은 레이어마다 한 번 그려 ctx 에 캐시, 장마다 변환·붙이기만.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

from .const import S

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_files", "fonts")
FONTS = {   # 이름: (파일, 설명) — 전부 OFL (data_files/fonts/LICENSE.md)
    "bold": ("BlackHanSans.ttf", "아주 굵은 제목체 (기본)"),
    "round": ("DoHyeon-Regular.ttf", "둥근 굵은 고딕"),
    "cute": ("Jua-Regular.ttf", "귀여운 둥근 손글씨풍"),
    "pen": ("NanumPenScript-Regular.ttf", "펜 손글씨"),
    "gothic": ("NanumGothic-ExtraBold.ttf", "깔끔한 굵은 고딕"),
    "brush": ("EastSeaDokdo-Regular.ttf", "거친 붓글씨 (액션·먹물 느낌)"),
    "brush2": ("NanumBrushScript-Regular.ttf", "부드러운 붓글씨"),
}
ALIGNS = ("center", "left", "right")
ENTERS = ("none", "pop", "fade", "slide_up", "slide_left", "drop", "wipe", "zoom")
IDLES = ("none", "bob", "pulse", "shake", "swing", "blink")
SHAPE_KINDS = ("bubble", "rect", "round", "ellipse", "star", "burst")
SS = 2   # 그릴 때 2배로 그리고 줄임 (가장자리 부드럽게)


def font_path(name: str) -> str:
    return os.path.join(HERE, FONTS.get(name, FONTS["bold"])[0])


def missing_glyphs(text: str, font: str) -> str:
    """그 글꼴에 없는 글자들 (공백·줄바꿈 제외). 빈 글자는 텔레그램에서 네모로 보임."""
    ft = ImageFont.truetype(font_path(font), 40)
    bad = []
    for ch in dict.fromkeys(text):
        if ch.isspace():
            continue
        box = ft.getmask(ch).getbbox()
        if box is None or ft.getbbox(ch) == ft.getbbox("￿"):
            bad.append(ch)
    return "".join(bad)


def _ease_out_back(x: float) -> float:
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def _bounce(x: float) -> float:
    n, d = 7.5625, 2.75
    if x < 1 / d:
        return n * x * x
    if x < 2 / d:
        x -= 1.5 / d
        return n * x * x + 0.75
    if x < 2.5 / d:
        x -= 2.25 / d
        return n * x * x + 0.9375
    x -= 2.625 / d
    return n * x * x + 0.984375


def _window(u: float, start: float, end: float) -> float | None:
    if end <= start:
        end = min(1.0, start + 0.05)
    if u < start or u > end:
        return None
    return (u - start) / (end - start)


def _gradient(w: int, h: int, cols: list) -> Image.Image:
    """위→아래 여러 색 그라데이션 (RGBA)."""
    g = Image.new("RGBA", (1, max(2, h)))
    n = len(cols)
    for y in range(g.height):
        f = y / (g.height - 1) * (n - 1)
        i = min(int(f), n - 2)
        k = f - i
        a, b = cols[i], cols[i + 1]
        g.putpixel((0, y), tuple(int(a[j] + (b[j] - a[j]) * k) for j in range(3)) + (255,))
    return g.resize((max(1, w), h))


# ── 글자 ─────────────────────────────────────────────────────────────────
def render_text(text: str, font: str = "bold", size: int = 72, width: float = 0.9, align: str = "center",
                color=(255, 255, 255), colors=None, stroke: int = 8, stroke_color=(0, 0, 0), depth: int = 0,
                depth_color=(40, 40, 40), line_gap: float = 0.15, stroke2: int = 0, stroke2_color=(255, 255, 255),
                glow: int = 0, glow_color=(170, 80, 255), slant: float = 0.0) -> Image.Image:
    """글자 덩어리 한 장 (투명 배경, 꼭 맞게 자른 RGBA). 폭(width×512)·높이(0.9×512)에 맞을 때까지 크기를 줄임.
    stroke2 = 테두리 바깥 두 번째 테두리, glow = 바깥 빛번짐(흐림 반경 px) — 견본 글자(금속·네온) 따라 하기용."""
    outer = stroke + stroke2
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()][:4] or [" "]
    path = font_path(font)
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    size = int(size)
    while True:
        ft = ImageFont.truetype(path, size * SS)
        boxes = [probe.textbbox((0, 0), ln, font=ft, stroke_width=outer * SS) for ln in lines]
        lw = [b[2] - b[0] for b in boxes]
        lh = [b[3] - b[1] for b in boxes]
        gap = int(size * SS * line_gap)
        tw, th = max(lw) + depth * SS, sum(lh) + gap * (len(lines) - 1) + depth * SS
        if (tw <= width * S * SS and th <= 0.9 * S * SS) or size <= 14:
            break
        size = max(14, int(size * 0.92))
    pad = (outer + depth + 4 + glow * 2) * SS
    W, H = tw + 2 * pad, th + 2 * pad
    back = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    mask = Image.new("L", (W, H), 0)
    db, dm = ImageDraw.Draw(back), ImageDraw.Draw(mask)
    y = pad
    for ln, b, w, h in zip(lines, boxes, lw, lh):
        x = pad + {"left": 0, "right": tw - depth * SS - w}.get(align, (tw - depth * SS - w) // 2) - b[0]
        yy = y - b[1]
        for k in range(depth * SS, 0, -1):                                   # 입체: 아래·오른쪽으로 겹쳐 찍기
            db.text((x + k * 0.5, yy + k), ln, font=ft, fill=tuple(depth_color) + (255,),
                    stroke_width=outer * SS, stroke_fill=tuple(stroke2_color if stroke2 else stroke_color) + (255,))
        if stroke2:
            db.text((x, yy), ln, font=ft, fill=tuple(stroke2_color) + (255,), stroke_width=outer * SS,
                    stroke_fill=tuple(stroke2_color) + (255,))
        if stroke:
            db.text((x, yy), ln, font=ft, fill=tuple(stroke_color) + (255,), stroke_width=stroke * SS,
                    stroke_fill=tuple(stroke_color) + (255,))
        dm.text((x, yy), ln, font=ft, fill=255)
        y += h + gap
    fill = _gradient(W, H, list(colors)) if colors and len(colors) >= 2 else Image.new("RGBA", (W, H), tuple(color) + (255,))
    back.paste(fill, (0, 0), mask)
    if glow:                                                                  # 빛번짐: 글자 모양을 흐려 아래에 깔기
        from PIL import ImageFilter
        a = back.getchannel("A").filter(ImageFilter.GaussianBlur(glow * SS))
        a = a.point(lambda v: min(255, int(v * 1.8)))
        halo = Image.new("RGBA", (W, H), tuple(glow_color) + (0,))
        halo.putalpha(a)
        back = Image.alpha_composite(halo, back)
    img = back.resize((max(1, W // SS), max(1, H // SS)), Image.LANCZOS)
    if slant:                                                                 # 기울임: 위가 오른쪽(+)으로 밀리는 이탤릭
        extra = int(abs(slant) * img.height) + 2
        wide = Image.new("RGBA", (img.width + extra, img.height), (0, 0, 0, 0))
        wide.paste(img, (extra if slant > 0 else 0, 0))
        img = wide.transform(wide.size, Image.AFFINE, (1, slant, -slant * img.height if slant > 0 else 0, 0, 1, 0),
                             resample=Image.BICUBIC)
        if img.width > width * S:                                             # 기울여서 넓어진 만큼 줄임
            k = width * S / img.width
            img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
    return img.crop(img.getbbox() or (0, 0, 1, 1))


# ── 도형 ─────────────────────────────────────────────────────────────────
def render_shape(kind: str, w: int, h: int, fill=(255, 255, 255), stroke: int = 6, stroke_color=(0, 0, 0),
                 radius: float = 0.25, tail=None, center=(0, 0), points: int = 5) -> tuple[Image.Image, tuple]:
    """도형 한 장 + 이 그림 안에서 '중심' 좌표 (꼬리가 붙으면 중심이 그림 가운데가 아님)."""
    w, h = max(8, int(w)), max(8, int(h))
    pad = stroke + 4
    # 꼬리 끝 (512 좌표) → 도형 중심 기준 상대 좌표
    tip = None
    if kind == "bubble" and tail:
        tip = (tail[0] * S - center[0], tail[1] * S - center[1])
    minx, miny = -w / 2 - pad, -h / 2 - pad
    maxx, maxy = w / 2 + pad, h / 2 + pad
    if tip:
        minx, miny = min(minx, tip[0] - pad), min(miny, tip[1] - pad)
        maxx, maxy = max(maxx, tip[0] + pad), max(maxy, tip[1] + pad)
    W, H = int(maxx - minx), int(maxy - miny)
    ox, oy = -minx, -miny                                                   # 도형 중심의 그림 안 좌표
    im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = tuple(fill) + (255,)
    sc = tuple(stroke_color) + (255,)
    sw = stroke * SS
    box = [(ox - w / 2) * SS, (oy - h / 2) * SS, (ox + w / 2) * SS, (oy + h / 2) * SS]

    def poly(pts):
        pts = [(x * SS, y * SS) for x, y in pts]
        d.polygon(pts, fill=f, outline=sc if sw else None, width=sw or 1)

    if kind in ("bubble", "round", "rect"):
        r = 0 if kind == "rect" else int(min(w, h) * min(max(radius, 0), 0.5) * SS)
        if tip:   # 꼬리: 몸통 가장자리 두 점 → 끝. 테두리 먼저 그리고 몸통으로 이음매를 덮음
            ang = math.atan2(tip[1], tip[0])
            base = min(w, h) * 0.18
            bx, by = math.cos(ang) * min(w, h) * 0.3, math.sin(ang) * min(w, h) * 0.3
            px, py = -math.sin(ang) * base, math.cos(ang) * base
            tri = [(ox + bx + px, oy + by + py), (ox + bx - px, oy + by - py), (ox + tip[0], oy + tip[1])]
            poly(tri)
        d.rounded_rectangle(box, r, fill=f, outline=sc if sw else None, width=sw or 1)
        if tip:
            inner = [(ox + bx + px * 0.6, oy + by + py * 0.6), (ox + bx - px * 0.6, oy + by - py * 0.6),
                     (ox + bx + (tip[0] - bx) * 0.5, oy + by + (tip[1] - by) * 0.5)]
            d.polygon([(x * SS, y * SS) for x, y in inner], fill=f)
    elif kind == "ellipse":
        d.ellipse(box, fill=f, outline=sc if sw else None, width=sw or 1)
    elif kind in ("star", "burst"):
        n = max(3, min(int(points), 24)) if kind == "star" else max(8, min(int(points) * 2, 24))
        inner = 0.45 if kind == "star" else 0.72
        pts = []
        for i in range(n * 2):
            a = -math.pi / 2 + i * math.pi / n
            rr = 1.0 if i % 2 == 0 else inner
            pts.append((ox + math.cos(a) * w / 2 * rr, oy + math.sin(a) * h / 2 * rr))
        poly(pts)
    img = im.resize((W, H), Image.LANCZOS)
    return img, (ox, oy)


# ── 장마다: 등장·계속 움직임 → 변환 → 붙이기 ─────────────────────────────────────
def _motion(p: float, u: float, enter: str, enter_dur: float, idle: str, amp: float, seed: int):
    """→ (크기 배율, 회전, dx, dy, 투명도, 보이는 폭 비율)."""
    sc, rot, dx, dy, op, reveal = 1.0, 0.0, 0.0, 0.0, 1.0, 1.0
    e = min(1.0, p / enter_dur) if enter_dur > 0 else 1.0
    if enter == "pop":
        sc = max(0.0, _ease_out_back(e))
    elif enter == "zoom":
        sc = 2.2 - 1.2 * _ease_out(e)
        op = e
    elif enter == "fade":
        op = e
    elif enter == "slide_up":
        dy, op = (1 - _ease_out(e)) * 120, min(1.0, e * 2)
    elif enter == "slide_left":
        dx, op = (1 - _ease_out(e)) * 220, min(1.0, e * 2)
    elif enter == "drop":
        dy = -(1 - _bounce(e)) * 260
    elif enter == "wipe":
        reveal = _ease_out(e)
    w = 2 * math.pi
    if idle == "bob":
        dy += math.sin(w * 2 * u) * 8 * amp
    elif idle == "pulse":
        sc *= 1 + 0.06 * amp * math.sin(w * 3 * u)
    elif idle == "shake":
        dx += math.sin(w * 13 * u + seed) * 3 * amp
        dy += math.cos(w * 17 * u + seed) * 3 * amp
    elif idle == "swing":
        rot += math.sin(w * 2 * u) * 6 * amp
    elif idle == "blink":
        op *= 0.35 + 0.65 * (0.5 + 0.5 * math.cos(w * 4 * u))
    return sc, rot, dx, dy, op, reveal


def _place(frame: Image.Image, spr: Image.Image, anchor: tuple, at: tuple, m: tuple, base_rot: float,
           opacity: float) -> Image.Image:
    sc, rot, dx, dy, op, reveal = m
    if sc <= 0.01 or op * opacity <= 0.01:
        return frame
    img = spr
    if reveal < 1:
        keep = max(1, int(img.width * reveal))
        cut = Image.new("RGBA", img.size, (0, 0, 0, 0))
        cut.paste(img.crop((0, 0, keep, img.height)), (0, 0))
        img = cut
    ax, ay = anchor
    if abs(sc - 1) > 0.005:
        img = img.resize((max(1, int(img.width * sc)), max(1, int(img.height * sc))), Image.BICUBIC)
        ax, ay = ax * sc, ay * sc
    angle = base_rot + rot
    if abs(angle) > 0.05:
        w0, h0 = img.size
        img = img.rotate(-angle, resample=Image.BICUBIC, expand=True)
        cx, cy = ax - w0 / 2, ay - h0 / 2                                   # 중심 기준 앵커를 같이 돌림
        a = math.radians(angle)
        ax = img.width / 2 + cx * math.cos(a) - cy * math.sin(a)
        ay = img.height / 2 + cx * math.sin(a) + cy * math.cos(a)
    a_mul = op * opacity
    if a_mul < 0.999:
        img = img.copy()
        img.putalpha(img.getchannel("A").point(lambda v, k=a_mul: int(v * k)))
    x = int(round(at[0] * S + dx - ax))
    y = int(round(at[1] * S + dy - ay))
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    layer.paste(img, (x, y), img)
    return Image.alpha_composite(frame, layer)


def text(frame, u, ctx, text="", font="bold", at=(0.5, 0.85), size=72, width=0.9, align="center", color=(255, 255, 255),
         colors=None, stroke=8, stroke_color=(0, 0, 0), depth=0, depth_color=(40, 40, 40), rotate=0.0, opacity=1.0,
         enter="pop", enter_dur=0.2, idle="none", amp=1.0, start=0.0, end=1.0, _layer=0, stroke2=0,
         stroke2_color=(255, 255, 255), glow=0, glow_color=(170, 80, 255), slant=0.0, **_):
    """글자 덩어리 — 위치·크기·여러 줄·글꼴·색/그라데이션·테두리 2겹·빛번짐·입체·기울기·등장·계속 움직임"""
    p = _window(u, start, end)
    if p is None or not str(text).strip():
        return frame
    key = ("text", _layer)
    cache = ctx.setdefault("_layout", {})
    if key not in cache:
        img = render_text(str(text), font, size, width, align, color, colors, stroke, stroke_color, depth, depth_color,
                          stroke2=stroke2, stroke2_color=stroke2_color, glow=glow, glow_color=glow_color, slant=slant)
        cache[key] = (img, (img.width / 2, img.height / 2))
    spr, anchor = cache[key]
    m = _motion(p, u, enter, enter_dur, idle, amp, _layer)
    return _place(frame, spr, anchor, at, m, rotate, opacity)


def shape(frame, u, ctx, kind="bubble", at=(0.5, 0.3), wh=(0.7, 0.3), fill=(255, 255, 255), stroke=6, stroke_color=(0, 0, 0),
          radius=0.3, tail=None, points=5, rotate=0.0, opacity=1.0, enter="pop", enter_dur=0.2, idle="none", amp=1.0,
          start=0.0, end=1.0, _layer=0, **_):
    """도형 — 말풍선(꼬리 tail)·사각·둥근 사각·타원·별·폭발, 위치·크기·색·테두리·등장·움직임"""
    p = _window(u, start, end)
    if p is None:
        return frame
    key = ("shape", _layer)
    cache = ctx.setdefault("_layout", {})
    if key not in cache:
        cache[key] = render_shape(kind, wh[0] * S, wh[1] * S, fill, stroke, stroke_color, radius, tail,
                                  (at[0] * S, at[1] * S), points)
    spr, anchor = cache[key]
    m = _motion(p, u, enter, enter_dur, idle, amp, _layer + 7)
    return _place(frame, spr, anchor, at, m, rotate, opacity)


LAYERS = {"text": text, "shape": shape}
