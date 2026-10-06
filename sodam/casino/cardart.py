"""실사 카드 그림 (바카라 회차판): 영국식 카드 52장 PNG(CC0, sodam/data_files/cards/LICENSE.md) + Pillow 로 테이블을 그린다.

- 카드 파일은 미리 PNG 로 넣어 둠 → 서버엔 SVG 변환 도구가 필요 없다. 파일이 없으면 None (부르는 쪽이 cardimg 그림으로).
- 단계: 덮음(베팅 중) → 플레이어 2장 → 뱅커 2장 → 3번째 카드 → 결과(이긴 쪽 금테 + 배너). 한 장 약 0.1초, JPEG ~100KB.
- 이미지 안엔 한글을 넣지 않는다 (서버 폰트) — 한글은 캡션.
- 카드 객체는 `.rank`(1~13) · `.suit`(0♠ 1♥ 2♦ 3♣) 만 쓴다 (cards.Card).
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .anim import _font

DIR = Path(__file__).resolve().parent.parent / "data_files" / "cards"
CW, CH = 200, 300
W = 780
TITLE_H = 64
ROW_H = 340
X0 = 170                      # 첫 카드 왼쪽
STEP2, STEP3 = 214, 140       # 카드 사이 (2장일 때 / 3장이면 겹쳐 놓음)
BADGE_X = 712
BANNER_H = 96
FELT_TOP, FELT_BOT = (16, 82, 54), (8, 48, 32)
GOLD, WHITE, BLACK = (232, 190, 80), (255, 255, 255), (12, 12, 14)
SIDE = {"player": ("PLAYER", (110, 180, 255)), "banker": ("BANKER", (255, 120, 120))}
WIN_BANNER = {"player": ("PLAYER WINS", (22, 70, 150)), "banker": ("BANKER WINS", (140, 22, 34)), "tie": ("TIE", (20, 110, 60))}


def available() -> bool:
    return (DIR / "01s.png").exists() and len(list(DIR.glob("*.png"))) >= 52


@lru_cache(maxsize=64)
def _face(rank: int, suit: int) -> Image.Image:
    return Image.open(DIR / f"{rank:02d}{'shdc'[suit]}.png").convert("RGBA").resize((CW, CH))


@lru_cache(maxsize=1)
def _back() -> Image.Image:
    im = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, CW - 1, CH - 1], 14, fill=WHITE)
    inner = Image.new("RGBA", (CW - 20, CH - 20), (120, 18, 30, 255))
    di = ImageDraw.Draw(inner)
    for i in range(-CH, CW, 16):                                   # 빗살 무늬 (안쪽 칸에만)
        di.line([(i, 0), (i + CH, CH)], fill=(150, 34, 46), width=4)
    mask = Image.new("L", inner.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner.width - 1, inner.height - 1], 10, fill=255)
    im.paste(inner, (10, 10), mask)
    d.rounded_rectangle([10, 10, CW - 11, CH - 11], 10, outline=GOLD, width=4)
    d.ellipse([CW / 2 - 34, CH / 2 - 34, CW / 2 + 34, CH / 2 + 34], fill=(120, 18, 30), outline=GOLD, width=4)
    d.text((CW / 2, CH / 2), "S", font=_font(40), fill=GOLD, anchor="mm")
    return im


@lru_cache(maxsize=1)
def _shadow() -> Image.Image:
    sh = Image.new("RGBA", (CW + 24, CH + 24), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([12, 12, CW + 12, CH + 12], 14, fill=(0, 0, 0, 130))
    return sh.filter(ImageFilter.GaussianBlur(7))


def _point(c) -> int:
    return c.rank if c.rank < 10 else 0


def total(cards: list) -> int:
    return sum(_point(c) for c in cards) % 10


def card_x(i: int, n: int) -> int:
    """i 번째 카드의 왼쪽 x (n = 그 줄에 놓일 카드 수). 테스트도 이걸로 자리를 확인한다."""
    return X0 + i * (STEP3 if n > 2 else STEP2)


def _paste(im: Image.Image, card: Image.Image, x: int, y: int) -> None:
    sh = _shadow()
    im.paste(sh, (x - 8, y - 4), sh)
    im.paste(card, (x, y), card)


def _felt(h: int) -> Image.Image:
    im = Image.new("RGB", (W, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = abs(y - h / 2) / (h / 2)
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(FELT_TOP, FELT_BOT)))
    d.rounded_rectangle([8, 8, W - 9, h - 9], 20, outline=(150, 112, 48), width=5)
    return im


def table(player: list, banker: list, p_shown: int, b_shown: int, title: str, winner: str | None = None,
          ppair: bool = False, bpair: bool = False) -> Image.Image:
    """p_shown·b_shown = 앞면으로 보일 카드 수 (나머지 처음 두 장은 뒷면, 3번째는 받기 전엔 안 그림).
    winner 가 있으면 결과 화면 (이긴 줄 금테 + 아래 배너)."""
    h = TITLE_H + 2 * ROW_H + (BANNER_H if winner else 16)
    im = _felt(h)
    d = ImageDraw.Draw(im)
    d.text((W / 2, TITLE_H / 2 + 6), title, font=_font(30), fill=GOLD, anchor="mm")
    for row, (side, cards, shown, pair) in enumerate((("player", player, p_shown, ppair),
                                                      ("banker", banker, b_shown, bpair))):
        y = TITLE_H + row * ROW_H + 16
        label, color = SIDE[side]
        if winner in (side, "tie") and winner:
            d.rounded_rectangle([20, y - 10, W - 21, y + CH + 12], 16, outline=GOLD, width=5)
        d.text((92, y + CH / 2 - (14 if pair and shown >= 2 else 0)), label, font=_font(30), fill=color, anchor="mm")
        if pair and shown >= 2:
            d.text((92, y + CH / 2 + 24), "PAIR", font=_font(22), fill=GOLD, anchor="mm")
        n = len(cards) if shown > 2 else 2
        for i in range(n):
            face = i < shown
            _paste(im, _face(cards[i].rank, cards[i].suit) if face else _back(), card_x(i, n), y)
        pts = total(cards[:shown]) if shown else None
        d.ellipse([BADGE_X - 42, y + CH / 2 - 42, BADGE_X + 42, y + CH / 2 + 42], fill=BLACK, outline=color, width=5)
        d.text((BADGE_X, y + CH / 2), "?" if pts is None else str(pts), font=_font(46), fill=WHITE, anchor="mm")
    if winner:
        text, bg = WIN_BANNER[winner]
        y = TITLE_H + 2 * ROW_H
        d.rounded_rectangle([40, y + 4, W - 41, y + BANNER_H - 16], 18, fill=bg, outline=GOLD, width=3)
        d.text((W / 2, y + (BANNER_H - 12) / 2), f"{text}   {total(player)} : {total(banker)}", font=_font(40),
               fill=(255, 226, 140), anchor="mm")
    return im


def jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


def stages(player: list, banker: list, winner: str, round_no: int, ppair: bool, bpair: bool) -> list[tuple[str, bytes]]:
    """(단계 이름, JPEG) — 플레이어 공개 → 뱅커 공개 → (3번째 카드) → 결과. 덮인 화면은 open_image."""
    t = f"ROUND {round_no}"
    out = [("player", jpeg(table(player, banker, 2, 0, t))),
           ("banker", jpeg(table(player, banker, 2, 2, t)))]
    if len(player) > 2 or len(banker) > 2:
        out.append(("third", jpeg(table(player, banker, len(player), len(banker), t))))
    out.append(("result", jpeg(table(player, banker, len(player), len(banker), f"{t}  RESULT", winner, ppair, bpair))))
    return out


def open_image(round_no: int) -> bytes:
    """베팅 받는 동안: 네 장 다 뒷면."""
    dummy = [type("C", (), {"rank": 1, "suit": 0})()] * 2
    return jpeg(table(dummy, dummy, 0, 0, f"ROUND {round_no}  BETTING"))
