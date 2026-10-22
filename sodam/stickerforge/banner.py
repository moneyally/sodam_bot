"""긴 배너 → 100×100 타일 커스텀 이모지 여러 개 (처음부터 디자인해서 쪼개 만들기).

핵심 원칙 (경계 이음새 방지): **전체 캔버스에 한 번 그려 → 한 번만 축소 → 정확한 격자로 crop.**
타일을 따로 그리거나 따로 축소하면 선·글자가 타일 경계에서 어긋난다. (설계 근거: ChatGPT E·재조립 오차 QA)

spec = {
  "cols": 1~10, "rows": 1~3,          # 타일 격자 (가로·세로 칸 수)
  "bg": [r,g,b] | [r,g,b,a] | {"gradient":[[r,g,b],...], "dir":"v"|"h"} | "transparent",
  "layers": [                          # 그리는 순서대로 겹침
    {"type":"text", "text":"사이트제휴", "font":"bold", "at":[0.5,0.5], "height":0.7,
     "color":[..]|"colors":[[..],[..]], "stroke":8, "stroke_color":[..], "align":"center"},
    {"type":"shape", "kind":"rect"|"round"|"ellipse", "at":[x,y], "wh":[w,h], "fill":[..], "stroke":..},
    {"type":"image", "at":[x,y], "scale":0.9},   # 미리 넣어 준 PIL 이미지 (로고·캐릭터), spec["images"][i]
  ],
  "images": [PIL.Image, ...],          # image 레이어가 쓰는 원본들 (코드가 넣어 줌, AI 가 경로 못 넣게)
}
좌표·크기는 0~1 (배너 전체 기준). height = 글자 높이를 배너 높이의 비율로.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from . import stylefx
from .layout import _gradient, font_path, missing_glyphs

# stylefx(ImageMagick) 로 넘기는 스타일 인자 (나머지 at·height 등은 banner 가 처리)
_STYLE_PARAMS = ("color", "top", "bot", "glow", "glow_color", "bevel", "stroke", "stroke_color")

TILE = 100
SS = 3                      # 수퍼샘플: 고해상도로 그린 뒤 한 번만 축소 (작은 글씨 계단 현상 줄임)
MAX_COLS, MAX_ROWS = 10, 3  # 텔레그램 한 줄에 무리 없이 붙는 범위


def _col(c, default=(0, 0, 0, 0)):
    """[r,g,b] 또는 [r,g,b,a] → RGBA 튜플."""
    if c is None:
        return default
    c = list(c)
    if len(c) == 3:
        c = c + [255]
    return tuple(int(max(0, min(255, v))) for v in c[:4])


def _background(w: int, h: int, bg) -> Image.Image:
    if bg == "transparent" or bg is None:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if isinstance(bg, dict) and bg.get("gradient"):
        cols = [list(c)[:3] for c in bg["gradient"]][:6] or [[0, 0, 0]]
        if len(cols) == 1:
            cols = cols * 2
        if bg.get("dir") == "h":                      # 가로 그라데이션 = 세로로 만들어 90도 회전
            return _gradient(h, w, cols).rotate(-90, expand=True).resize((w, h))
        return _gradient(w, h, cols)
    return Image.new("RGBA", (w, h), _col(bg, (0, 0, 0, 255)))


def _fit_font(text: str, font: str, target_h: int, max_w: int, stroke: int) -> ImageFont.FreeTypeFont:
    """글자 높이 ≈ target_h 이되 폭이 max_w 를 넘으면 줄여서 맞춤."""
    path = font_path(font)
    size = max(8, int(target_h))
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    while size > 8:
        ft = ImageFont.truetype(path, size)
        b = probe.textbbox((0, 0), text, font=ft, stroke_width=stroke)
        if (b[2] - b[0]) <= max_w and (b[3] - b[1]) <= target_h * 1.25:
            return ft
        size = int(size * 0.92)
    return ImageFont.truetype(path, 8)


def _metal_fill(mask: Image.Image, colors, *, band: float = 0.0, bevel: float = 0.0, inner: float = 0.0,
                band_color=(255, 255, 236)) -> Image.Image:
    """글자 마스크 → 금속 채움: 세로 그라데이션 + 반사 밴드 + 베벨(위·왼 밝게/아래·오른 어둡게) + 안쪽 그림자.
    (설계: ChatGPT 역설계 — stroke·depth·glow 만으론 안 되는 금속 표면·입체. roll 기반 엣지라 scipy 불필요.)"""
    import numpy as np
    a = np.asarray(mask, dtype=np.float32) / 255.0                     # HxW 알파 0~1
    h, w = a.shape
    ys = np.linspace(0.0, 1.0, h)
    stops = [list(c)[:3] for c in (colors or [[255, 255, 255]])]
    if len(stops) == 1:
        stops = stops * 2
    seg = np.linspace(0.0, 1.0, len(stops))
    grad = np.stack([np.interp(ys, seg, [s[i] for s in stops]) for i in range(3)], axis=1)   # Hx3
    rgb = np.repeat(grad[:, None, :], w, axis=1)                        # HxWx3
    if band > 0:                                                        # 반사 밴드: 상단 1/3쯤 밝은 가로 띠
        b = (np.exp(-((ys - 0.36) ** 2) / (2 * 0.05 ** 2)) * band)[:, None, None]
        rgb = rgb * (1 - b) + np.asarray(band_color, float) * b
    if bevel > 0 or inner > 0:
        up, left = np.roll(a, -2, 0), np.roll(a, -2, 1)
        dn, rt = np.roll(a, 2, 0), np.roll(a, 2, 1)
        if bevel > 0:                                                  # 베벨: 위·왼 안쪽모서리 밝게, 아래·오른 어둡게
            light = np.clip((a - dn) + (a - rt), 0, 1)[:, :, None]
            dark = np.clip((a - up) + (a - left), 0, 1)[:, :, None]
            rgb = rgb + light * 130 * bevel - dark * 130 * bevel
        if inner > 0:                                                  # 안쪽 그림자: 위쪽 안쪽 가장자리 어둡게
            er = np.minimum.reduce([np.roll(a, k, 0) for k in range(0, 4)])
            shadow = np.clip(a - np.roll(er, 3, 0), 0, 1)[:, :, None]
            rgb = rgb - shadow * 150 * inner
    rgb = np.clip(rgb, 0, 255)
    out = np.dstack([rgb, a * 255.0]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _draw_text(canvas: Image.Image, ly: dict, W: int, H: int) -> None:
    """프리미엄 글자: 금속 그라데이션 채움 + 이중 테두리(stroke·stroke2) + 입체(depth) + 바깥 빛번짐(glow).
    로얄클럽류 팩처럼 보이게. 전부 spec 값이라 요청마다 달라짐(고정 아님)."""
    text = str(ly.get("text", ""))[:40]
    if not text:
        return
    font = ly.get("font", "bold")
    if missing_glyphs(text, font):                    # 글꼴에 없는 글자 → 다 있는 글꼴로
        font = "gothic"
    at = ly.get("at", [0.5, 0.5])
    target_h = int(float(ly.get("height", 0.7)) * H)
    style = ly.get("style")
    if style:                                         # ImageMagick 스타일(금·크롬·네온·불·유리…) — 없으면 아래 PIL 금속
        g = stylefx.render(text, style, font=font, size=max(24, target_h),
                           **{k: v for k, v in ly.items() if k in _STYLE_PARAMS})
        if g is not None:
            if g.height > target_h * 1.4:             # 글로우로 커졌으면 칸에 맞게 줄임
                k = target_h * 1.4 / g.height
                g = g.resize((max(1, int(g.width * k)), max(1, int(g.height * k))), Image.LANCZOS)
            cx, cy = int(at[0] * W), int(at[1] * H)
            canvas.alpha_composite(g, (cx - g.width // 2, cy - g.height // 2))
            return
    stroke = int(ly.get("stroke", 8)) * SS
    stroke2 = int(ly.get("stroke2", 0)) * SS          # 바깥 두 번째 테두리 (금속 느낌)
    depth = int(ly.get("depth", 0)) * SS              # 아래·오른쪽으로 겹쳐 찍어 입체
    glow = int(ly.get("glow", 0)) * SS                # 바깥 빛번짐 반경
    pad = ly.get("pad", 0.04)
    max_w = int((1 - 2 * pad) * W)
    outer = stroke + stroke2
    ft = _fit_font(text, font, target_h, max_w - 2 * (outer + depth + glow), outer)
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))
    b = probe.textbbox((0, 0), text, font=ft, stroke_width=outer)
    tw, th = b[2] - b[0], b[3] - b[1]
    m = outer + depth + glow * 2 + 2                  # 여백 (테두리·입체·빛번짐 잘리지 않게)
    glyph = Image.new("RGBA", (tw + 2 * m, th + 2 * m), (0, 0, 0, 0))
    mask = Image.new("L", glyph.size, 0)
    gd, md = ImageDraw.Draw(glyph), ImageDraw.Draw(mask)
    ox, oy = -b[0] + m, -b[1] + m
    sc = _col(ly.get("stroke_color", [30, 16, 0]))
    s2c = _col(ly.get("stroke2_color", list(sc[:3])))
    for k in range(depth, 0, -1):                     # 입체: 아래·오른쪽으로 겹쳐
        gd.text((ox + k * 0.5, oy + k), text, font=ft, fill=_col(ly.get("depth_color", list(sc[:3]))),
                stroke_width=outer, stroke_fill=s2c)
    if stroke2:
        gd.text((ox, oy), text, font=ft, fill=s2c, stroke_width=outer, stroke_fill=s2c)
    if stroke:
        gd.text((ox, oy), text, font=ft, fill=sc, stroke_width=stroke, stroke_fill=sc)
    md.text((ox, oy), text, font=ft, fill=255)
    colors = ly.get("colors") or [ly.get("color", [255, 255, 255])]
    metal = _metal_fill(mask, colors, band=float(ly.get("band", 0.0)), bevel=float(ly.get("bevel", 0.0)),
                        inner=float(ly.get("inner", 0.0)), band_color=_col(ly.get("band_color", [255, 255, 236]))[:3])
    glyph.alpha_composite(metal)                       # 금속 채움을 테두리 위에
    if glow:                                          # 빛번짐: 글자 모양을 흐려 뒤에 깔기
        from PIL import ImageFilter
        a = mask.filter(ImageFilter.GaussianBlur(glow)).point(lambda v: min(255, int(v * 1.8)))
        halo = Image.new("RGBA", glyph.size, _col(ly.get("glow_color", [255, 180, 60]))[:3] + (0,))
        halo.putalpha(a)
        glyph = Image.alpha_composite(halo, glyph)
    cx, cy = int(at[0] * W), int(at[1] * H)
    canvas.alpha_composite(glyph, (cx - glyph.width // 2, cy - glyph.height // 2))


def _draw_frames(canvas: Image.Image, spec: dict, cols: int, rows: int, W: int, H: int) -> None:
    """타일별 장식 프레임(배지): 각 100×100 칸에 둥근 바탕 + 테두리. 로얄클럽류 '칸마다 틀' 느낌."""
    fr = spec.get("frames")
    if not fr:
        return
    cw, ch = W // cols, H // rows
    gap = int(fr.get("gap", 0.06) * cw)
    rad = int(fr.get("radius", 0.22) * cw)
    fill = _col(fr.get("fill", [24, 26, 40]))
    bw = int(fr.get("border", 0.03) * cw)
    bc = _col(fr.get("border_color", [255, 200, 90]))
    inner = fr.get("inset")                            # 안쪽 가는 선(이중 테두리)
    d = ImageDraw.Draw(canvas)
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * cw + gap, r * ch + gap
            x1, y1 = (c + 1) * cw - gap, (r + 1) * ch - gap
            d.rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=fill,
                                outline=bc if bw else None, width=max(1, bw))
            if inner and bw:
                off = bw * 2
                d.rounded_rectangle([x0 + off, y0 + off, x1 - off, y1 - off], radius=max(1, rad - off),
                                    outline=_col(inner), width=max(1, bw // 2))


def _draw_shape(canvas: Image.Image, ly: dict, W: int, H: int) -> None:
    at = ly.get("at", [0.5, 0.5])
    wh = ly.get("wh", [0.4, 0.6])
    w, h = max(2, int(wh[0] * W)), max(2, int(wh[1] * H))
    x0, y0 = int(at[0] * W - w / 2), int(at[1] * H - h / 2)
    fill = _col(ly.get("fill", [255, 255, 255]))
    stroke = int(ly.get("stroke", 0)) * SS
    sc = _col(ly.get("stroke_color", [0, 0, 0]))
    d = ImageDraw.Draw(canvas)
    kind = ly.get("kind", "rect")
    box = [x0, y0, x0 + w, y0 + h]
    if kind == "ellipse":
        d.ellipse(box, fill=fill, outline=sc if stroke else None, width=stroke)
    elif kind == "round":
        d.rounded_rectangle(box, radius=int(min(w, h) * 0.25), fill=fill, outline=sc if stroke else None, width=stroke)
    else:
        d.rectangle(box, fill=fill, outline=sc if stroke else None, width=stroke)


def _draw_image(canvas: Image.Image, ly: dict, W: int, H: int, images: list) -> None:
    i = int(ly.get("i", 0))
    if not images or i >= len(images):
        return
    im = images[i].convert("RGBA")
    scale = float(ly.get("scale", 1.0))
    box_h = int(scale * H)
    if im.height != box_h:
        k = box_h / max(1, im.height)
        im = im.resize((max(1, int(im.width * k)), max(1, box_h)), Image.LANCZOS)
    at = ly.get("at", [0.5, 0.5])
    cx, cy = int(at[0] * W), int(at[1] * H)
    canvas.alpha_composite(im, (cx - im.width // 2, cy - im.height // 2))


def render_banner(spec: dict) -> tuple[Image.Image, int, int]:
    """spec → (최종 배너 RGBA, cols, rows). 고해상도로 그린 뒤 cols·100 × rows·100 으로 **한 번만** 축소."""
    cols = max(1, min(MAX_COLS, int(spec.get("cols", 1))))
    rows = max(1, min(MAX_ROWS, int(spec.get("rows", 1))))
    W, H = cols * TILE * SS, rows * TILE * SS
    canvas = _background(W, H, spec.get("bg"))
    _draw_frames(canvas, spec, cols, rows, W, H)       # 타일별 장식 프레임(배지) — 있으면
    images = spec.get("images") or []
    for ly in spec.get("layers", []):
        t = ly.get("type")
        if t == "text":
            _draw_text(canvas, ly, W, H)
        elif t == "shape":
            _draw_shape(canvas, ly, W, H)
        elif t == "image":
            _draw_image(canvas, ly, W, H, images)
    final = canvas.resize((cols * TILE, rows * TILE), Image.LANCZOS)    # ★ 한 번만 축소 (이음새 방지)
    return final, cols, rows


def make_tiles(banner: Image.Image, cols: int, rows: int, tile: int = TILE) -> list[Image.Image]:
    """최종 배너를 정확한 격자로 crop. 타일을 따로 리사이즈하지 않는다."""
    if banner.width != cols * tile or banner.height != rows * tile:
        raise ValueError(f"배너 크기 {banner.size} 가 격자 {cols}×{rows}×{tile} 와 안 맞음")
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append(banner.crop((c * tile, r * tile, (c + 1) * tile, (r + 1) * tile)))
    return out


def reassembly_error(final: Image.Image, tiles: list[Image.Image], cols: int, rows: int, tile: int = TILE) -> float:
    """타일을 원래 순서로 다시 붙여 원본과의 평균 픽셀차 (0 에 가까워야 이음새 없음)."""
    rebuilt = Image.new("RGBA", final.size, (0, 0, 0, 0))
    for i, t in enumerate(tiles):
        rebuilt.paste(t, ((i % cols) * tile, (i // cols) * tile))
    import numpy as np
    a = np.asarray(final.convert("RGBA"), dtype=np.int16)
    b = np.asarray(rebuilt.convert("RGBA"), dtype=np.int16)
    return float(np.abs(a - b).mean())


def build_tiles(spec: dict) -> dict:
    """배너 spec → 타일 이미지들 + 미리보기 + 재조립 오차. 커스텀 이모지 팩(tools/emoji_pack.py)에 넣을 재료."""
    final, cols, rows = render_banner(spec)
    tiles = make_tiles(final, cols, rows)
    err = reassembly_error(final, tiles, cols, rows)
    return {"tiles": tiles, "preview": final, "cols": cols, "rows": rows,
            "count": len(tiles), "qa_error": err}
