"""🎁 방 전용 메뉴의 그림 카드 — 배경(방장 사진·AI 그림·없으면 색 그라데이션) 위에 코드가 글자·은박·도장을 얹음.

- 그림 AI 에게 글자를 쓰게 하지 않음 (한글이 깨짐) → 배경만 AI, 글자는 여기서 PIL 로 (스티커 공방과 같은 원칙).
- 배경은 data/brand/ 에 1280×720 JPEG 로 보관 (봇이 바뀌어도 안 깨지고, 미니앱도 같은 파일을 씀).
- 글꼴: 우리가 정한 글(당첨·출석…)은 Black Han Sans, 방장·멤버 이름처럼 아무 글자나 올 수 있는 건 나눔고딕 ExtraBold (한글 11,172자).
  이모지는 PIL 이 못 그려서 뺌.
테스트 tests/test_brand.py (카드 크기·글자 넘침).
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps


W, H = 1280, 720
FONT_DIR = Path(__file__).parent / "data_files" / "fonts"
DISPLAY = FONT_DIR / "BlackHanSans.ttf"
BODY = FONT_DIR / "NanumGothic-ExtraBold.ttf"
MAX_UPLOAD = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
_EMOJI = re.compile("[\U00010000-\U0010ffff☀-➿⬀-⯿️‍⃣]")
_fonts: dict = {}


DIR: Path | None = None   # 테스트가 바꿀 수 있음


def media_dir(db) -> Path:
    path = getattr(db, "path", "") or ""
    base = DIR or (Path(tempfile.gettempdir()) / f"sodam_brand_{os.getpid()}" if path in ("", ":memory:")
                   else Path(path).resolve().parent / "brand")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _font(path: Path, size: int):
    key = (str(path), size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(path), size)
    return _fonts[key]


def plain(text: str) -> str:
    return " ".join(_EMOJI.sub("", text or "").split())


# ── 배경 보관 ─────────────────────────────────────────────
def normalize(data: bytes) -> bytes:
    """아무 사진 → 1280×720 JPEG (가운데를 16:9 로 자름). 사진이 아니면 ValueError."""
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("사진이 너무 크거나 비었어요 (15MB 안)")
    try:
        im = Image.open(io.BytesIO(data))
        if im.size[0] * im.size[1] > MAX_PIXELS:   # 펼치기 전에 (작은 파일이 큰 그림으로 풀리는 것 막기)
            raise ValueError("too many pixels")
        im.draft("RGB", (W * 2, H * 2))
        im = ImageOps.exif_transpose(im)            # 휴대폰 사진 돌림 정보
        im = im.convert("RGB")
    except Exception as e:
        raise ValueError("사진이 너무 크거나 사진으로 못 읽었어요") from e
    if min(im.size) < 200:
        raise ValueError("사진이 너무 작아요 (가로·세로 200px 넘게)")
    w, h = im.size
    if w / h > W / H:
        nw = int(h * W / H)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w * H / W)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize((W, H), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def save_file(db, chat_id: int, slot: str, jpeg: bytes) -> str:
    name = f"{chat_id}_{slot}_{hashlib.sha256(jpeg).hexdigest()[:10]}.jpg"
    p = media_dir(db) / name
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(jpeg)
    os.replace(tmp, p)
    return name


def remove_file(db, name: str | None) -> None:
    if name and re.fullmatch(r"-?\d+_[a-z]+_[0-9a-f]{10}\.jpg", name):
        try:
            (media_dir(db) / name).unlink()
        except OSError:
            pass


def load_file(db, name: str | None) -> bytes | None:
    if not name or not re.fullmatch(r"-?\d+_[a-z]+_[0-9a-f]{10}\.jpg", name):
        return None
    try:
        return (media_dir(db) / name).read_bytes()
    except OSError:
        return None


# ── 그리기 ────────────────────────────────────────────────
def _diag(w: int, h: int) -> Image.Image:
    """왼쪽 위 0 → 오른쪽 아래 255 대각선 그라데이션 (돌리기·늘리기 자국 없이)."""
    v = Image.linear_gradient("L").resize((w, h)).point(lambda x: x // 2)
    hz = Image.linear_gradient("L").rotate(90).transpose(Image.FLIP_LEFT_RIGHT).resize((w, h)).point(lambda x: x // 2)
    return ImageChops.add(v, hz)


def _gradient(theme: dict) -> Image.Image:
    a, b = theme["bg1"], theme["bg2"]
    grad = _diag(W, H)
    return Image.composite(Image.new("RGB", (W, H), b), Image.new("RGB", (W, H), a), grad)


def _base(theme: dict, bg: bytes | None) -> Image.Image:
    im = None
    if bg:
        try:
            im = Image.open(io.BytesIO(bg)).convert("RGB")
            if im.size != (W, H):
                im = im.resize((W, H))
        except Exception:
            im = None
    if im is None:
        im = _gradient(theme)
        glow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(glow).ellipse((W * 0.55, -H * 0.4, W * 1.3, H * 0.7), fill=90)
        im = Image.composite(Image.new("RGB", (W, H), theme["bg2"]), im, glow.filter(ImageFilter.GaussianBlur(120)))
    # 아래쪽을 어둡게 (글자가 어떤 배경에서도 읽히게)
    shade = Image.linear_gradient("L").resize((W, H)).point(lambda v: int(v * 0.62))
    return Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), im, shade)


def _fit(text: str, path: Path, max_w: int, start: int, min_size: int = 18):
    size = start
    while size > min_size:
        f = _font(path, size)
        if f.getlength(text) <= max_w:
            return f
        size -= 4
    return _font(path, min_size)


def _clip(text: str, f, max_w: int) -> str:
    if f.getlength(text) <= max_w:
        return text
    while text and f.getlength(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def _text(im: Image.Image, xy, text: str, f, fill, anchor="mm", stroke=0, stroke_fill=(0, 0, 0), glow=None):
    if glow:
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(xy, text, font=f, fill=glow + (200,), anchor=anchor, stroke_width=stroke + 6,
                                   stroke_fill=glow + (200,))
        im.paste(layer.filter(ImageFilter.GaussianBlur(18)), (0, 0), layer.filter(ImageFilter.GaussianBlur(18)))
    ImageDraw.Draw(im).text(xy, text, font=f, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=stroke_fill)


def _brand(im, theme, name: str) -> None:
    name = plain(name)
    if name:
        f = _fit(name, BODY, W // 2, 46, 24)
        _text(im, (48, 44), _clip(name, f, W // 2), f, theme["accent"], anchor="lt", stroke=3)


def _foil(im: Image.Image, box, theme) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    foil = _diag(w, h).point(lambda v: 150 + v * 90 // 255)
    foil = Image.merge("RGB", (foil, foil, foil.point(lambda v: min(255, v + 8))))
    d = ImageDraw.Draw(foil)
    for x in range(-h, w, 18):
        d.line((x, h, x + int(h * 0.6), 0), fill=(236, 238, 242), width=4)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=28, fill=255)
    im.paste(foil, (x0, y0), mask)
    ImageDraw.Draw(im).rounded_rectangle(box, radius=28, outline=theme["accent"], width=6)


def _stamps(im, theme, streak: int, cx: int, cy: int) -> None:
    d = ImageDraw.Draw(im)
    n = 7
    filled = ((streak - 1) % n) + 1 if streak > 0 else 0
    r, gap = 34, 22
    x = cx - (n * 2 * r + (n - 1) * gap) // 2
    f = _font(DISPLAY, 30)
    for i in range(n):
        box = (x, cy - r, x + 2 * r, cy + r)
        if i < filled:
            d.ellipse(box, fill=theme["accent"])
            d.text((x + r, cy), str(i + 1), font=f, fill=theme["bg1"], anchor="mm")
        else:
            d.ellipse(box, outline=theme["accent"], width=5)
            d.text((x + r, cy), str(i + 1), font=f, fill=theme["accent"], anchor="mm")
        x += 2 * r + gap


def _sub(im, text: str, y: int, color=(225, 230, 245), size: int = 40) -> None:
    text = plain(text)
    if text:
        f = _fit(text, BODY, W - 160, size, 22)
        _text(im, (W // 2, y), _clip(text, f, W - 160), f, color, stroke=2)


def render(kind: str, room, bg: bytes | None = None, **kw) -> bytes:
    """카드 그림 JPEG. kind = lotto/win/lose/soldout/checkin/fortune/welcome. 글자·색은 전부 방 설정(room.text·room.colors)."""
    t = room.colors
    im = _base(t, bg)
    _brand(im, t, room.name)

    def big(s: str, y: int, size: int) -> None:
        s = plain(s)
        if s:
            _text(im, (W // 2, y), s, _fit(s, DISPLAY_FOR(s), W - 140, size), t["accent"], stroke=4, stroke_fill=(30, 20, 0),
                  glow=t["accent"])
    if kind == "lotto":
        _foil(im, (W // 2 - 400, 170, W // 2 + 400, 500), t)
        title = plain(room.text("lotto_title"))
        _text(im, (W // 2, 335), title, _fit(title, DISPLAY_FOR(title), 720, 92), (88, 92, 104))
        _sub(im, room.text("lotto_left", left=int(kw.get("left", 0))), 600, size=44)
        _sub(im, room.notice.split("\n")[0] if room.notice else "", 666, size=30)
    elif kind == "win":
        big(room.text("win_title"), 300, 220)
        _sub(im, room.text("win_sub", prize=kw.get("prize", "")), 500, t["fg"], 60)
        if room.contact:
            _sub(im, f"@{room.contact}", 600, size=38)
    elif kind == "lose":
        big(room.text("lose_title"), 310, 130)
        _sub(im, room.text("lose_sub"), 500, size=48)
    elif kind in ("soldout", "recent"):
        big(room.text("soldout_title") if kind == "soldout" else room.text("lose_title"), 310, 110)
    elif kind == "checkin":
        big(room.text("checkin_title", streak=int(kw.get("streak", 1))), 250, 130)
        _stamps(im, t, int(kw.get("streak", 1)), W // 2, 430)
        _sub(im, room.text("checkin_sub", month=int(kw.get("month", 1))), 560, size=44)
        if kw.get("ticket"):
            _sub(im, "+ " + room.text("lotto_left", left=1).replace("남은 ", ""), 640, t["accent"], 40)
    elif kind == "fortune":
        big(room.text("fortune_title", grade=plain(kw.get("grade", ""))), 250, 140)
        _sub(im, kw.get("line", ""), 430, t["fg"], 46)
        _sub(im, f"행운 숫자 {kw.get('number', '')}  ·  행운 색 {kw.get('color', '')}", 540, size=40)
    else:   # welcome
        big(plain(room.name) or "혜택 메뉴", 300, 130)
        _sub(im, room.text("welcome_sub"), 480, size=48)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()


def backdrop(room, bg: bytes | None = None, size: int = 512) -> bytes:
    """글자 없는 정사각 배경 (당첨 스티커 원본 — 글자는 스티커 엔진이 자막으로 얹음)."""
    im = _base(room.colors, bg)
    left = (W - H) // 2
    im = im.crop((left, 0, left + H, H)).resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue()


def DISPLAY_FOR(text: str) -> Path:
    """방장이 쓴 글에 Black Han Sans(흔한 한글 2,350자)에 없는 글자가 있으면 나눔고딕 (빈 네모 방지)."""
    f = _font(DISPLAY, 40)
    try:
        for ch in text:
            if ch.strip() and f.getmask(ch).getbbox() is None:
                return BODY
    except Exception:
        return BODY
    return DISPLAY
