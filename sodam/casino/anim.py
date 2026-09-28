"""결과 애니메이션(GIF): 결과가 **이미 정해진 뒤** 그 결과로 끝나는 짧은 영상을 서버가 그려 보낸다.

- 룰렛(공이 휠을 돌다 멈춤) · 경마(말이 달려 결승) · 사다리(길을 따라 내려감).
- 그래프는 여기 없음: 미리 그린 영상엔 터지는 지점이 들어 있어서 판 도중에 보내면 결과가 샌다.
- 한 판 = 애니메이션 1개 + 결과 캡션 수정 1번 (메시지 수정 연출보다 그룹 한도에 덜 걸림).
- 이미지 안엔 숫자·영문·기호만 (서버마다 한글 폰트가 달라서). 한글은 캡션.
- 한 장 그리는 데 약 1초 → 호출하는 쪽이 asyncio.to_thread 로.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

FRAME_MS = 70
HOLD = 45                      # 마지막 화면 유지 프레임 (텔레그램은 영상을 반복 재생 → 결과가 오래 보이게)
REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
WHEEL = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10,
         5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]
GOLD, FELT = (200, 170, 60), (18, 60, 40)
HORSE_COLORS = [(214, 48, 49), (38, 98, 214), (22, 150, 80), (230, 150, 20), (130, 60, 190)]


def _font(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def _gif(frames: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:] + [frames[-1]] * HOLD,
                   duration=FRAME_MS, loop=0, optimize=True)
    return buf.getvalue()


def seconds(n_frames: int) -> float:
    """움직이는 부분 길이 (결과 캡션을 이 뒤에 고친다)."""
    return n_frames * FRAME_MS / 1000


def _ease(t: float) -> float:
    return 1 - (1 - t) ** 3            # 처음 빠르고 점점 느려짐


# ── 🎡 룰렛 ──────────────────────────────────────────────
ROULETTE_FRAMES = 38


def _pocket_color(n: int):
    return (22, 150, 80) if n == 0 else (200, 40, 40) if n in REDS else (30, 30, 30)


def roulette(result: int, size: int = 360) -> bytes:
    c, r, rin = size / 2, size / 2 - 20, size / 2 - 70
    step, f = 360 / len(WHEEL), _font(13)
    wheel = Image.new("RGB", (size, size), FELT)
    d = ImageDraw.Draw(wheel)
    for i, n in enumerate(WHEEL):
        a0 = -90 + i * step - step / 2
        d.pieslice([c - r, c - r, c + r, c + r], a0, a0 + step, fill=_pocket_color(n), outline=GOLD)
        a = math.radians(-90 + i * step)
        d.text((c + (r - 16) * math.cos(a), c + (r - 16) * math.sin(a)), str(n), fill="white", font=f, anchor="mm")
    d.ellipse([c - rin, c - rin, c + rin, c + rin], fill=(120, 80, 40), outline=GOLD, width=3)
    total = 720 + WHEEL.index(result) * step                      # 두 바퀴 돌고 결과 칸으로
    frames = []
    for k in range(ROULETTE_FRAMES):
        p = _ease(k / (ROULETTE_FRAMES - 1))
        ang, rr = math.radians(-90 + total * p), rin + 18 + (1 - p) * 22
        im = wheel.copy()
        dd = ImageDraw.Draw(im)
        x, y = c + rr * math.cos(ang), c + rr * math.sin(ang)
        dd.ellipse([x - 8, y - 8, x + 8, y + 8], fill="white", outline=(80, 80, 80))
        if k == ROULETTE_FRAMES - 1:
            dd.ellipse([c - 50, c - 50, c + 50, c + 50], fill=_pocket_color(result), outline="white", width=3)
            dd.text((c, c), str(result), fill="white", font=_font(40), anchor="mm")
        frames.append(im)
    return _gif(frames)


# ── 🏇 경마 ──────────────────────────────────────────────
RACE_STEPS = 8                   # 위치 프레임 사이 보간


def race(positions: list[list[int]], track: int, winner: int, w: int = 480) -> bytes:
    """positions: 프레임별 말 위치(0..track) — multi.race_frames 그대로. 마지막엔 우승마만 결승선."""
    lanes, lane_h, left, right = len(positions[0]), 44, 40, w - 50
    h = 40 + lanes * lane_h + 16
    f = _font(15)
    bg = Image.new("RGB", (w, h), (70, 140, 60))
    d = ImageDraw.Draw(bg)
    d.text((12, 10), "SODAM DERBY", fill="white", font=_font(16))
    for i in range(lanes):
        y = 40 + i * lane_h
        d.rectangle([left, y + 2, right, y + lane_h - 2], fill=(150, 110, 70) if i % 2 else (160, 120, 78))
        d.text((18, y + lane_h / 2), str(i + 1), fill="white", font=f, anchor="mm")
    for k in range(0, h, 10):                                      # 결승선 체크무늬
        d.rectangle([right, k, right + 8, k + 5], fill="white" if (k // 10) % 2 else "black")
    track_px = right - left - 36                    # 우승마가 결승선 무늬에 안 겹치게
    frames = []
    path = [[0] * lanes, *positions]
    for a, b in zip(path, path[1:]):
        for s in range(RACE_STEPS):
            t = (s + 1) / RACE_STEPS
            im = bg.copy()
            dd = ImageDraw.Draw(im)
            for i in range(lanes):
                x = left + 4 + (a[i] + (b[i] - a[i]) * t) / track * track_px
                y = 40 + i * lane_h + lane_h / 2
                dd.ellipse([x, y - 13, x + 26, y + 13], fill=HORSE_COLORS[i % 5], outline="white", width=2)
                dd.text((x + 13, y), str(i + 1), fill="white", font=f, anchor="mm")
            frames.append(im)
    dd = ImageDraw.Draw(frames[-1])
    y = 40 + winner * lane_h
    dd.rectangle([left, y + 2, right, y + lane_h - 2], outline=(255, 215, 0), width=4)
    dd.text((w / 2, 20), f"WINNER  No.{winner + 1}", fill=(255, 215, 0), font=_font(18), anchor="mm")
    return _gif(frames)


def race_frame_count(positions: list[list[int]]) -> int:
    return len(positions) * RACE_STEPS


# ── 🪜 사다리 ─────────────────────────────────────────────
LADDER_STEPS = 30


def ladder(start: str, lines: int, w: int = 300, h: int = 360) -> bytes:
    """좌/우 출발 → 가로줄 lines 개를 건너며 내려감. 아래 왼쪽 = 홀(O, 빨강) · 오른쪽 = 짝(E, 파랑)."""
    lx, rx, top, bot = 70, w - 70, 60, h - 60
    ys = [top + (bot - top) * (i + 1) / (lines + 1) for i in range(lines)]
    bg = Image.new("RGB", (w, h), (245, 240, 228))
    d = ImageDraw.Draw(bg)
    f = _font(20)
    for x, t in ((lx, "L"), (rx, "R")):
        d.line([(x, top), (x, bot)], fill=(90, 70, 50), width=6)
        d.text((x, top - 26), t, fill=(60, 60, 60), font=f, anchor="mm")
    for y in ys:
        d.line([(lx, y), (rx, y)], fill=(90, 70, 50), width=6)
    d.ellipse([lx - 20, bot + 8, lx + 20, bot + 48], fill=(214, 48, 49))
    d.text((lx, bot + 28), "O", fill="white", font=f, anchor="mm")
    d.ellipse([rx - 20, bot + 8, rx + 20, bot + 48], fill=(38, 98, 214))
    d.text((rx, bot + 28), "E", fill="white", font=f, anchor="mm")
    x = lx if start == "좌" else rx                               # 지나갈 점들: 내려가다 가로줄에서 건너감
    pts = [(x, top)]
    for y in ys:
        pts += [(x, y), (rx if x == lx else lx, y)]
        x = pts[-1][0]
    pts.append((x, bot))
    seg = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    total = sum(seg)
    frames = []
    for k in range(LADDER_STEPS + 1):
        goal, done, trail = total * k / LADDER_STEPS, 0.0, [pts[0]]
        for (a, b), L in zip(zip(pts, pts[1:]), seg):
            if done + L >= goal:
                t = (goal - done) / L if L else 1
                trail.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                break
            trail.append(b)
            done += L
        im = bg.copy()
        dd = ImageDraw.Draw(im)
        dd.line(trail, fill=(250, 190, 0), width=8, joint="curve")
        px, py = trail[-1]
        dd.ellipse([px - 11, py - 11, px + 11, py + 11], fill=(250, 190, 0), outline="white", width=3)
        frames.append(im)
    return _gif(frames)
