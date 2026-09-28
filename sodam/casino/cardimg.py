"""카드 그림 (Pillow): 흰 둥근 카드 · 초록 펠트 테이블 → PNG(블랙잭·하이로우) / GIF(바카라, 한 장씩 놓임).

- 무늬 ♠♥♦♣ 는 서버 폰트에 글리프가 없을 수 있어 도형(다각형·원)으로 직접 그린다. ♥♦ 빨강, ♠♣ 검정.
- 숫자·A·J·Q·K·영문은 DejaVu Bold (없으면 기본 폰트). 이미지 안엔 한글을 넣지 않는다 (한글은 캡션).
- 카드는 2배 크기로 그린 뒤 줄여서(안티에일리어싱) 캐시 → 테이블 한 장 약 0.05~0.2초, PNG 수십 KB.
- 카드 객체는 `.rank`(1~13) · `.suit`(0♠ 1♥ 2♦ 3♣) 만 쓴다 (cards.Card). None = 뒷면.
- 자리 계산(`slot`)을 테스트도 같이 써서 "그림 속 카드 수·색 = 결과 카드" 를 픽셀로 확인한다.
"""
from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw

from .anim import FRAME_MS, HOLD, _font

CW, CH = 84, 118                    # 카드 크기 (1배)
W = 520                             # 테이블 폭
ROW_H = 142                         # 줄 높이 (카드 + 여백)
TOP = 14
CARD_X0, CARD_X1 = 130, 436         # 카드가 놓이는 가로 범위
GAP = 94                            # 카드 사이 (넘치면 겹쳐 놓음)
BADGE_X = 474                       # 점수 동그라미 가운데
BANNER_H = 44
FELT, FELT_DARK, RIM = (22, 104, 62), (16, 80, 48), (92, 58, 28)
RED, BLACK = (208, 32, 48), (28, 28, 34)
BACK = (38, 62, 150)
GOLD, WHITE = (250, 200, 40), (255, 255, 255)
BLUE_T, RED_T = (110, 180, 255), (255, 120, 120)
RANK_TXT = {1: "A", 11: "J", 12: "Q", 13: "K"}


# ── 무늬 (도형) ───────────────────────────────────────────
def suit_color(suit: int):
    return RED if suit in (1, 2) else BLACK


def draw_suit(d: ImageDraw.ImageDraw, suit: int, cx: float, cy: float, s: float, fill=None) -> None:
    """(cx, cy) 가운데, 반지름 s 쯤 되는 무늬."""
    fill = fill or suit_color(suit)

    def circle(x, y, r):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)
    if suit == 2:                                              # ♦
        d.polygon([(cx, cy - s), (cx + 0.74 * s, cy), (cx, cy + s), (cx - 0.74 * s, cy)], fill=fill)
    elif suit == 1:                                            # ♥
        r = 0.52 * s
        circle(cx - 0.49 * s, cy - 0.38 * s, r)
        circle(cx + 0.49 * s, cy - 0.38 * s, r)
        d.polygon([(cx - 0.99 * s, cy - 0.24 * s), (cx + 0.99 * s, cy - 0.24 * s), (cx, cy + s)], fill=fill)
    elif suit == 0:                                            # ♠
        r = 0.48 * s
        circle(cx - 0.47 * s, cy + 0.2 * s, r)
        circle(cx + 0.47 * s, cy + 0.2 * s, r)
        d.polygon([(cx - 0.94 * s, cy + 0.08 * s), (cx + 0.94 * s, cy + 0.08 * s), (cx, cy - s)], fill=fill)
        d.polygon([(cx, cy + 0.2 * s), (cx - 0.38 * s, cy + s), (cx + 0.38 * s, cy + s)], fill=fill)
    else:                                                      # ♣
        r = 0.42 * s
        circle(cx, cy - 0.5 * s, r)
        circle(cx - 0.5 * s, cy + 0.12 * s, r)
        circle(cx + 0.5 * s, cy + 0.12 * s, r)
        circle(cx, cy - 0.05 * s, 0.3 * s)
        d.polygon([(cx, cy), (cx - 0.36 * s, cy + s), (cx + 0.36 * s, cy + s)], fill=fill)


# ── 카드 한 장 (2배로 그려 줄임, 캐시) ────────────────────
def _rounded_mask(w: int, h: int, r: int) -> Image.Image:
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], r, fill=255)
    return m


@lru_cache(maxsize=64)
def card_image(rank: int | None, suit: int | None) -> Image.Image:
    """RGBA 카드 (CW×CH). rank=None 이면 뒷면."""
    k = 2
    w, h = CW * k, CH * k
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if rank is None:
        d.rounded_rectangle([0, 0, w - 1, h - 1], 16, fill=WHITE, outline=(170, 170, 170), width=2)
        pat = Image.new("RGBA", (w, h), BACK)                   # 격자 무늬 (안쪽 사각형 안에만)
        pd = ImageDraw.Draw(pat)
        for x in range(-h, w, 18):
            pd.line([(x, 0), (x + h, h)], fill=(70, 96, 190), width=3)
            pd.line([(x + h, 0), (x, h)], fill=(70, 96, 190), width=3)
        inner = Image.new("L", (w, h), 0)
        ImageDraw.Draw(inner).rounded_rectangle([9, 9, w - 10, h - 10], 11, fill=255)
        im.paste(pat, (0, 0), inner)
        d.rounded_rectangle([9, 9, w - 10, h - 10], 11, outline=WHITE, width=4)
        d.ellipse([w / 2 - 26, h / 2 - 26, w / 2 + 26, h / 2 + 26], fill=BACK, outline=GOLD, width=4)
        d.text((w / 2, h / 2), "S", fill=GOLD, font=_font(34), anchor="mm")
    else:
        col = suit_color(suit)
        txt = RANK_TXT.get(rank, str(rank))
        d.rounded_rectangle([0, 0, w - 1, h - 1], 16, fill=WHITE, outline=(150, 150, 150), width=2)
        if rank >= 11:                                          # 그림 카드: 금색 안쪽 테두리 + 큰 글자
            d.rounded_rectangle([40, 44, w - 41, h - 45], 10, outline=GOLD, width=5)
            d.text((w / 2, h / 2 - 22), txt, fill=col, font=_font(64), anchor="mm")
            draw_suit(d, suit, w / 2, h / 2 + 42, 22)
        else:
            draw_suit(d, suit, w / 2, h / 2, 40 if rank == 1 else 34)
        corner = Image.new("RGBA", (54, 80), (0, 0, 0, 0))     # 모서리: 숫자 + 작은 무늬 (아래는 뒤집어서)
        cd = ImageDraw.Draw(corner)
        cd.text((27, 22), txt, fill=col, font=_font(34 if len(txt) == 1 else 28), anchor="mm")
        draw_suit(cd, suit, 27, 58, 13)
        im.alpha_composite(corner, (6, 6))
        im.alpha_composite(corner.rotate(180), (w - 60, h - 86))
    im.putalpha(Image.composite(im.getchannel("A"), Image.new("L", (w, h), 0), _rounded_mask(w, h, 16)))
    return im.resize((CW, CH), Image.LANCZOS)


def _card_img(c, hidden: bool = False) -> Image.Image:
    return card_image(None, None) if c is None or hidden else card_image(c.rank, c.suit)


# ── 테이블 ────────────────────────────────────────────────
def slot(row: int, i: int, n: int) -> tuple[int, int]:
    """row 줄에 n 장 놓을 때 i 번째 카드의 왼쪽 위. 테스트도 이걸로 픽셀을 찾는다."""
    step = GAP if n <= 1 else min(GAP, (CARD_X1 - CARD_X0 - CW) // (n - 1))
    return CARD_X0 + i * step, TOP + row * ROW_H + (ROW_H - CH) // 2 - 6


def table(rows: int, banner: bool = True) -> Image.Image:
    h = TOP * 2 + rows * ROW_H + (BANNER_H if banner else 0)
    im = Image.new("RGB", (W, h), RIM)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([6, 6, W - 7, h - 7], 26, fill=FELT, outline=(200, 170, 90), width=3)
    for r in range(1, rows):                                  # 줄 사이 점선
        y = TOP + r * ROW_H - 6
        for x in range(24, W - 24, 16):
            d.line([(x, y), (x + 8, y)], fill=FELT_DARK, width=2)
    return im


def _label(d, row: int, text: str, color, sub: str = "") -> None:
    y = TOP + row * ROW_H + ROW_H // 2 - 6
    d.text((66, y - (8 if sub else 0)), text, fill=color, font=_font(20), anchor="mm")
    if sub:
        d.text((66, y + 16), sub, fill=GOLD, font=_font(13), anchor="mm")


def _empty(d, row: int, i: int, n: int) -> None:
    x, y = slot(row, i, n)
    d.rounded_rectangle([x, y, x + CW - 1, y + CH - 1], 8, outline=FELT_DARK, width=2)


def _badge(d, row: int, value: str, color=WHITE, ring=(0, 0, 0)) -> None:
    y = TOP + row * ROW_H + ROW_H // 2 - 6
    d.ellipse([BADGE_X - 28, y - 28, BADGE_X + 28, y + 28], fill=ring, outline=color, width=3)
    d.text((BADGE_X, y), value, fill=color, font=_font(26 if len(value) <= 2 else 19), anchor="mm")


def _highlight(d, row: int, color=GOLD) -> None:
    y = TOP + row * ROW_H
    d.rounded_rectangle([14, y + 2, W - 15, y + ROW_H - 12], 16, outline=color, width=4)


def _banner(d, rows: int, text: str, color=GOLD) -> None:
    y = TOP + rows * ROW_H + BANNER_H // 2 - 6
    d.text((W / 2, y), text, fill=color, font=_font(26), anchor="mm")


def _place(im: Image.Image, row: int, cards: list, n_slots: int | None = None, hide: set[int] = frozenset()) -> None:
    n = max(len(cards), n_slots or 0)
    d = ImageDraw.Draw(im)
    for i in range(len(cards), n):
        _empty(d, row, i, n)
    for i, c in enumerate(cards):
        im.paste(_card_img(c, i in hide), slot(row, i, n), _card_img(c, i in hide))


def png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ── 🃏 바카라 (GIF: 카드가 한 장씩 놓임) ─────────────────
FLIP_BACK, FLIP_FACE = 3, 6          # 한 장: 뒷면 3프레임 → 앞면 6프레임 (≈0.63초)


def _bac_total(cards: list) -> int:
    return sum(c.rank if c.rank < 10 else 0 for c in cards) % 10


def bac_frame(player: list, banker: list, p_shown: int, b_shown: int, back: str = "",
              winner: str | None = None, ppair: bool = False, bpair: bool = False) -> Image.Image:
    """back = 'p'|'b' 면 그 줄의 마지막 보이는 카드를 뒷면으로 (뒤집기 직전). winner 가 있으면 결과 강조."""
    im = table(2)
    d = ImageDraw.Draw(im)
    for row, (name, col, cards, shown, pair) in enumerate((("PLAYER", BLUE_T, player, p_shown, ppair),
                                                            ("BANKER", RED_T, banker, b_shown, bpair))):
        _label(d, row, name, col, "PAIR" if winner and pair else "")
        vis = cards[:shown]
        hidden = {len(vis) - 1} if back == name[0].lower() and vis else set()
        _place(im, row, vis, 2, hidden)
        face_up = vis[:-1] if hidden else vis
        if face_up:
            win = winner == ("player" if row == 0 else "banker")
            _badge(d, row, str(_bac_total(face_up)), GOLD if win else WHITE, (60, 40, 10) if win else (0, 0, 0))
    if winner:
        pt, bt = _bac_total(player), _bac_total(banker)
        if winner == "tie":
            _highlight(d, 0, (90, 230, 120))
            _highlight(d, 1, (90, 230, 120))
            _banner(d, 2, f"TIE  {pt} : {bt}", (120, 240, 140))
        else:
            _highlight(d, 0 if winner == "player" else 1)
            _banner(d, 2, f"{winner.upper()} WINS  {pt} : {bt}", GOLD)
    return im


def deal_order(player: list, banker: list) -> list[tuple[int, int]]:
    """놓이는 순서의 (보이는 플 장수, 뱅 장수): P1 B1 P2 B2 (P3) (B3)."""
    steps, p, b = [], 0, 0
    for side in ["p", "b", "p", "b"] + (["p"] if len(player) > 2 else []) + (["b"] if len(banker) > 2 else []):
        if side == "p":
            p += 1
        else:
            b += 1
        steps.append((p, b, side))
    return steps


def baccarat_frames(player: list, banker: list, winner: str, ppair: bool, bpair: bool) -> list[tuple[Image.Image, int]]:
    """(화면, 몇 프레임 동안) 목록: 빈 테이블 → 한 장씩 뒷면 → 앞면 → 결과(HOLD)."""
    frames = [(bac_frame(player, banker, 0, 0), 3)]
    for p, b, side in deal_order(player, banker):
        frames += [(bac_frame(player, banker, p, b, back=side), FLIP_BACK), (bac_frame(player, banker, p, b), FLIP_FACE)]
    frames.append((bac_frame(player, banker, len(player), len(banker), winner=winner, ppair=ppair, bpair=bpair), HOLD))
    return frames


def gif_timed(frames: list[tuple[Image.Image, int]]) -> bytes:
    """anim._gif 와 같은 박자(FRAME_MS·HOLD)인데, 같은 화면을 복사해 넣지 않고 프레임 길이로 늘림 (인코딩 약 0.3초)."""
    buf = io.BytesIO()
    frames[0][0].save(buf, "GIF", save_all=True, append_images=[f for f, _ in frames[1:]],
                      duration=[n * FRAME_MS for _, n in frames], loop=0, optimize=True)
    return buf.getvalue()


def baccarat_gif(player: list, banker: list, winner: str, ppair: bool = False, bpair: bool = False) -> bytes:
    return gif_timed(baccarat_frames(player, banker, winner, ppair, bpair))


def baccarat_seconds(player: list, banker: list) -> float:
    """움직이는 부분 길이 (이 뒤에 결과 캡션)."""
    return (3 + len(deal_order(player, banker)) * (FLIP_BACK + FLIP_FACE)) * FRAME_MS / 1000


# ── 🃏 블랙잭 (PNG) ──────────────────────────────────────
def _bj_total(cards: list) -> int:
    t = sum(min(c.rank, 10) for c in cards)
    return t + 10 if any(c.rank == 1 for c in cards) and t + 10 <= 21 else t


def blackjack_png(dealer: list, player: list, hide_hole: bool, banner: str = "", win: int | None = None) -> bytes:
    """hide_hole: 딜러 두 번째 카드 뒷면. win: 1 이김(금색) · 0 비김 · -1 짐 (결과 강조)."""
    im = table(2)
    d = ImageDraw.Draw(im)
    _label(d, 0, "DEALER", (255, 220, 160))
    _label(d, 1, "YOU", BLUE_T)
    _place(im, 0, dealer, 2, {1} if hide_hole else set())
    _place(im, 1, player, 2)
    dt, pt = _bj_total(dealer[:1] if hide_hole else dealer), _bj_total(player)
    _badge(d, 0, str(dt) + ("+" if hide_hole else ""), RED_T if dt > 21 else WHITE)
    _badge(d, 1, str(pt), GOLD if pt == 21 else RED_T if pt > 21 else WHITE)
    if win is not None:
        if win > 0:
            _highlight(d, 1)
        elif win < 0:
            _highlight(d, 0, (230, 90, 90))
    if banner:
        _banner(d, 2, banner, GOLD if (win or 0) > 0 else (230, 230, 230) if win == 0 else (255, 130, 130))
    return png(im)


# ── 🔼🔽 하이로우 (PNG) ────────────────────────────────────
def hilo_png(card, history: list, step: int, max_steps: int, hi: float = 0.0, lo: float = 0.0,
             banner: str = "", win: int | None = None) -> bytes:
    """위: 지난 카드(작게, 최근 7장) · 가운데: 지금 카드(크게) · 오른쪽: HI/LO 배수 · 아래: 단계 막대."""
    h = 330
    im = Image.new("RGB", (W, h), RIM)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([6, 6, W - 7, h - 7], 26, fill=FELT, outline=(200, 170, 90), width=3)
    d.text((24, 22), "HISTORY", fill=(200, 230, 210), font=_font(13), anchor="lm")
    small = [c for c in history[-7:]]
    for i, c in enumerate(small):
        im_c = _card_img(c).resize((42, 59), Image.LANCZOS)
        im.paste(im_c, (96 + i * 48, 12), im_c)
    big = _card_img(card).resize((118, 165), Image.LANCZOS)
    bx, by = 60, 92
    ring = GOLD if win and win > 0 else (230, 90, 90) if win is not None and win < 0 else (200, 230, 210)
    d.rounded_rectangle([bx - 8, by - 8, bx + 118 + 7, by + 165 + 7], 14, outline=ring, width=4)
    im.paste(big, (bx, by), big)
    d.text((bx + 59, by + 185), "NOW", fill=(200, 230, 210), font=_font(14), anchor="mm")
    if hi or lo:
        for j, (lab, m, arrow) in enumerate((("HI", hi, 1), ("LO", lo, -1))):
            y = 120 + j * 70
            col = (230, 230, 230) if m else (110, 140, 120)
            cx = 262
            pts = [(cx, y - 16), (cx + 16, y + 10), (cx - 16, y + 10)] if arrow > 0 else \
                  [(cx, y + 16), (cx + 16, y - 10), (cx - 16, y - 10)]
            d.polygon(pts, fill=col)
            d.text((292, y), f"{lab}  x{m:g}" if m else f"{lab}  -", fill=col, font=_font(24), anchor="lm")
    for i in range(max_steps):                                 # 단계 막대
        x = 240 + i * 26
        d.rounded_rectangle([x, 262, x + 20, 282], 4, fill=(90, 210, 110) if i < step else FELT_DARK,
                            outline=(200, 230, 210), width=1)
    d.text((240, 300), f"STEP {step}/{max_steps}", fill=(200, 230, 210), font=_font(14), anchor="lm")
    if banner:
        d.text((370, 170), banner, fill=GOLD if (win or 0) > 0 else (255, 130, 130) if win is not None and win < 0
               else WHITE, font=_font(24), anchor="mm")
    return png(im)
