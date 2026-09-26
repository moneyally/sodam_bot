"""🖼 그림장: 방의 최근 게임 결과를 이미지 한 장으로. `!그림장 [바카라|홀짝|룰렛|사다리|그래프|경마]`

- 각 게임이 결과를 record() 로 남긴다 (방·게임마다 최근 KEEP 개만).
- 이미지는 Pillow 로 그린다. 서버마다 한글 폰트가 달라 깨질 수 있어서 **이미지 안엔 기호·숫자·영문만**, 한글 설명은 캡션.
- 바카라는 흔히 쓰는 출목표: 위 = 본매(한 판씩 칸), 아래 = 연속 줄(같은 쪽이 이어지면 아래로, 바뀌면 옆 줄). 타이는 초록 빗금.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import time

from telegram import ReplyParameters
from telegram.error import TelegramError

from ..db import register_schema
from . import Ctx, register
from .anim import REDS

log = logging.getLogger(__name__)

register_schema("""
CREATE TABLE IF NOT EXISTS casino_results (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    game    TEXT NOT NULL,
    value   TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_results ON casino_results(chat_id, game, id);
""")

KEEP = 120          # 방·게임마다 남기는 판 수
SHOW = 60           # 그림장에 그리는 판 수 (그래프는 40)
COOLDOWN = 10       # 같은 방에서 그림장 이미지 간격(초) — 이미지 도배 방지
CELL, ROWS, PAD = 34, 6, 16
BG, GRID, INK = (250, 250, 247), (222, 222, 216), (40, 40, 40)
RED, BLUE, GREEN, BLACK = (214, 48, 49), (38, 98, 214), (22, 150, 80), (45, 45, 45)
HORSE = [(214, 48, 49), (38, 98, 214), (22, 150, 80), (230, 150, 20), (130, 60, 190)]
GAMES = {"바카라": "baccarat", "홀짝": "oddeven", "룰렛": "roulette", "사다리": "ladder", "그래프": "crash", "경마": "horse"}
_last: dict[int, float] = {}


async def record(db, chat_id: int, game: str, value: str) -> None:
    """결과 한 판 기록 + 오래된 것 정리. 실패해도 게임엔 영향 없게 호출하는 쪽에서 await 만."""
    try:
        await db.conn.execute("INSERT INTO casino_results(chat_id, game, value, ts) VALUES(?,?,?,?)",
                              (chat_id, game, value, int(time.time())))
        await db.conn.execute(
            "DELETE FROM casino_results WHERE chat_id=? AND game=? AND id <= (SELECT id FROM casino_results "
            "WHERE chat_id=? AND game=? ORDER BY id DESC LIMIT 1 OFFSET ?)", (chat_id, game, chat_id, game, KEEP))
        await db.conn.commit()
    except Exception:
        log.exception("result record failed %s %s", chat_id, game)


async def recent(db, chat_id: int, game: str, n: int = SHOW) -> list[str]:
    rows = await db._all("SELECT value FROM casino_results WHERE chat_id=? AND game=? ORDER BY id DESC LIMIT ?",
                         (chat_id, game, n))
    return [r["value"] for r in reversed(rows)]


# ── 그리기 ────────────────────────────────────────────────
def _font(size: int):
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _canvas(cols: int, rows_px: int, title: str):
    from PIL import Image, ImageDraw
    w = max(PAD * 2 + cols * CELL, 360)
    img = Image.new("RGB", (w, rows_px + 44 + PAD), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD, 12), title, fill=INK, font=_font(18))
    return img, d


def _grid(d, top: int, cols: int, rows: int = ROWS) -> None:
    for c in range(cols + 1):
        d.line([(PAD + c * CELL, top), (PAD + c * CELL, top + rows * CELL)], fill=GRID)
    for r in range(rows + 1):
        d.line([(PAD, top + r * CELL), (PAD + cols * CELL, top + r * CELL)], fill=GRID)


def _disc(d, top: int, col: int, row: int, color, label: str = "", ring: bool = False) -> None:
    x, y = PAD + col * CELL, top + row * CELL
    box = [x + 4, y + 4, x + CELL - 4, y + CELL - 4]
    if ring:
        d.ellipse(box, outline=color, width=4)
    else:
        d.ellipse(box, fill=color)
    if label:
        f = _font(13 if len(label) > 2 else 15)
        d.text((x + CELL / 2, y + CELL / 2), label, fill=color if ring else "white", font=f, anchor="mm")


def _bead(values: list[str], style, title: str):
    """본매: 위→아래, 왼→오 로 한 판씩. style(v) → (색, 글자)."""
    cols = max(1, math.ceil(len(values) / ROWS))
    img, d = _canvas(cols, ROWS * CELL, title)
    top = 44
    _grid(d, top, cols)
    for i, v in enumerate(values):
        color, label = style(v)
        _disc(d, top, i // ROWS, i % ROWS, color, label)
    return img


def big_road(values: list[str]) -> list[tuple[int, int, str, int]]:
    """바카라 연속 줄: [(열, 행, 'P'|'B', 그 칸에 겹친 타이 수)].
    같은 쪽이 이어지면 아래로, 바닥(6칸)이나 막히면 오른쪽으로 꼬리. 바뀌면 그 줄이 시작한 열의 다음 열 맨 위.
    맨 앞 타이는 첫 칸에 붙인다."""
    cells: list[list] = []
    taken: set[tuple[int, int]] = set()
    col = row = start = -1
    last, lead_ties = None, 0
    for v in values:
        w = v[0]
        if w == "T":
            if cells:
                cells[-1][3] += 1
            else:
                lead_ties += 1
            continue
        if w == last and row + 1 < ROWS and (col, row + 1) not in taken:
            row += 1
        elif w == last:
            col += 1                                   # 꼬리
        else:
            start += 1
            col, row = start, 0
            while (col, row) in taken:                 # 앞 줄 꼬리와 겹치면 옆으로
                col += 1
            start = col
        taken.add((col, row))
        cells.append([col, row, w, lead_ties])
        lead_ties, last = 0, w
    return [tuple(c) for c in cells]


def render_baccarat(values: list[str]) -> bytes:
    from PIL import Image, ImageDraw
    road = big_road(values)
    bcols = max(1, math.ceil(len(values) / ROWS))
    rcols = max((c for c, *_ in road), default=0) + 1
    cols = max(bcols, rcols)
    top1, top2 = 44, 44 + ROWS * CELL + 30
    img = Image.new("RGB", (max(PAD * 2 + cols * CELL, 360), top2 + ROWS * CELL + PAD), BG)
    d = ImageDraw.Draw(img)
    p, b, t = (sum(v[0] == k for v in values) for k in "PBT")
    d.text((PAD, 12), f"BACCARAT  last {len(values)}   P {p}  B {b}  T {t}", fill=INK, font=_font(18))
    _grid(d, top1, bcols)
    color = {"P": BLUE, "B": RED, "T": GREEN}
    for i, v in enumerate(values):
        _disc(d, top1, i // ROWS, i % ROWS, color[v[0]], v[0])
        x, y = PAD + (i // ROWS) * CELL, top1 + (i % ROWS) * CELL
        if "p" in v:                                   # 플레이어 페어: 왼쪽 위 파란 점
            d.ellipse([x + 3, y + 3, x + 11, y + 11], fill=BLUE, outline="white")
        if "b" in v:                                   # 뱅커 페어: 오른쪽 아래 빨간 점
            d.ellipse([x + CELL - 11, y + CELL - 11, x + CELL - 3, y + CELL - 3], fill=RED, outline="white")
    d.text((PAD, top2 - 24), "BIG ROAD", fill=INK, font=_font(14))
    _grid(d, top2, cols)
    for col, row, w, ties in road:
        _disc(d, top2, col, row, color[w], ring=True)
        if ties:                                       # 타이: 초록 빗금 (여러 번이면 숫자)
            x, y = PAD + col * CELL, top2 + row * CELL
            d.line([(x + 8, y + CELL - 8), (x + CELL - 8, y + 8)], fill=GREEN, width=4)
            if ties > 1:
                d.text((x + CELL / 2, y + CELL / 2), str(ties), fill=GREEN, font=_font(12), anchor="mm")
    return _png(img)


def render_crash(values: list[str]) -> bytes:
    """그래프: 최근 판 막대 (로그 높이, 2배 이상 초록·미만 빨강)."""
    from PIL import Image, ImageDraw
    vals = [int(v) for v in values]
    left = PAD + 36                                    # 왼쪽 눈금 글자 자리
    w, h, bw = max(left + PAD + len(vals) * 16, 360), 260, 12
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    big = sum(v >= 200 for v in vals)
    d.text((PAD, 12), f"CRASH  last {len(vals)}   >=2x {big}  <2x {len(vals) - big}", fill=INK, font=_font(18))
    base, span = h - 30, h - 80
    top = math.log(max(max(vals), 1000) / 100)
    for k, m in ((200, "2x"), (1000, "10x")):
        y = base - math.log(k / 100) / top * span
        d.line([(left, y), (w - PAD, y)], fill=GRID)
        d.text((left - 6, y), m, fill=(130, 130, 130), font=_font(12), anchor="rm")
    for i, v in enumerate(vals):
        x = left + i * 16
        y = base - max(3, math.log(max(v, 100) / 100) / top * span)
        d.rectangle([x, y, x + bw, base], fill=GREEN if v >= 200 else RED)
    last = vals[-1]
    d.text((PAD, base + 6), f"last {last // 100}.{last % 100:02d}x", fill=INK, font=_font(13))
    return _png(img)


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render(game: str, values: list[str]) -> bytes:
    if game == "baccarat":
        return render_baccarat(values)
    if game == "crash":
        return render_crash(values)
    if game == "oddeven":
        return _png(_bead(values, lambda v: (RED if int(v) % 2 else BLUE, v),
                          f"ODD/EVEN  last {len(values)}   odd {sum(int(v) % 2 for v in values)}"))
    if game == "roulette":
        return _png(_bead(values, lambda v: (GREEN if v == "0" else RED if int(v) in REDS else BLACK, v),
                          f"ROULETTE  last {len(values)}"))
    if game == "ladder":                               # 좌3짝 → L3 (홀 빨강 · 짝 파랑)
        return _png(_bead(values, lambda v: (RED if v.endswith("홀") else BLUE, ("L" if v[0] == "좌" else "R") + v[1]),
                          f"LADDER  last {len(values)}"))
    return _png(_bead(values, lambda v: (HORSE[int(v) - 1], v), f"HORSE  last {len(values)}"))


# ── 명령 ─────────────────────────────────────────────────
def caption(game_ko: str, game: str, values: list[str]) -> str:
    n = len(values)
    if game == "baccarat":
        c = {k: sum(v[0] == k for v in values) for k in "PBT"}
        stats = f"🔵플 {c['P']} · 🔴뱅 {c['B']} · 🟢타이 {c['T']} (● 페어)"
    elif game == "oddeven":
        odd = sum(int(v) % 2 for v in values)
        stats = f"🔴홀 {odd} · 🔵짝 {n - odd}"
    elif game == "roulette":
        red = sum(v != "0" and int(v) in REDS for v in values)
        zero = values.count("0")
        stats = f"🔴빨강 {red} · ⚫검정 {n - red - zero} · 🟢0 {zero}"
    elif game == "ladder":
        odd = sum(v.endswith("홀") for v in values)
        stats = f"L=좌 R=우, 숫자=줄 수 · 🔴홀 {odd} · 🔵짝 {n - odd}"
    elif game == "crash":
        vals = [int(v) for v in values]
        stats = f"2배 이상 {sum(v >= 200 for v in vals)}판 · 최고 {max(vals) // 100}.{max(vals) % 100:02d}x"
    else:
        stats = " · ".join(f"{h}번 {values.count(str(h))}" for h in range(1, 6))
    return f"🖼 <b>{game_ko} 그림장</b> (최근 {n}판)\n{stats}"


async def g_board(ctx: Ctx) -> None:
    game_ko = ctx.args[0] if ctx.args else "바카라"
    game = GAMES.get(game_ko)
    if not game:
        await ctx.reply("그림장: <code>!그림장 바카라</code> · " + " · ".join(f"<code>{g}</code>" for g in GAMES if g != "바카라"))
        return
    values = await recent(ctx.svc.db, ctx.chat_id, game, 40 if game == "crash" else SHOW)
    if not values:
        await ctx.reply(f"아직 이 방 {game_ko} 기록이 없어요. 몇 판 하고 다시 불러주세요!")
        return
    if time.monotonic() - _last.get(ctx.chat_id, -1e9) < COOLDOWN:
        await ctx.reply("그림장은 10초에 한 번만 보여드려요 🙏")
        return
    _last[ctx.chat_id] = time.monotonic()
    try:
        png = await asyncio.to_thread(render, game, values)   # 그리는 동안 다른 메시지 처리가 멈추지 않게
        await ctx.bot.send_photo(ctx.chat_id, photo=png, caption=caption(game_ko, game, values), parse_mode="HTML",
                                 reply_parameters=ReplyParameters(ctx.msg.message_id, allow_sending_without_reply=True))
    except TelegramError as e:
        log.warning("board send failed: %s", e)


register(("그림장", "출목", "road", "board"), g_board, usage="[바카라|홀짝|룰렛|사다리|그래프|경마]",
         help="🖼 최근 결과 그림 (기본 바카라)", group="시작", needs_account=False)
