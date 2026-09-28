"""Whole-body motion presets.

Every preset returns f(t) -> (angle_deg, sx, sy, dx, dy[, shear_x]) in MASTER pixels, and
several presets can be stacked: angles, offsets and shear add, scales multiply.
각 프리셋 docstring 첫 줄 = 카탈로그에 보이는 한 줄 설명 (sticker_catalog 가 코드에서 바로 읽음). All
periodic terms use whole multiples of the loop, so frame 88 flows into frame 0.
Impulse presets (recoil, jab, slam) finish their decay before the loop ends for
the same reason.

Motion vocabulary, roughly from calm to violent:
  idle < float < breathe < punch < shake < wobble < hop < pop < jab < recoil < slam
"""
from __future__ import annotations

import math

from .const import D


def _imp(t, t0, tau):
    u = t - t0
    return math.exp(-u / tau) if u >= 0 else 0.0


def _smooth(u):
    return u * u * (3 - 2 * u)


def _ease_out_back(u, s=1.70158):
    u -= 1
    return 1 + u * u * ((s + 1) * u + s)


def idle(amp_rot=2.0, amp_bob=10, breathe=0.012, phase=0.0, cycles=1, **_):
    """숨쉬듯 흔들리는 대기 (잔잔·기본)."""
    def f(t):
        w = 2 * math.pi * cycles * t / D + phase
        s = 1 + breathe * math.sin(2 * w)
        return (amp_rot * math.sin(w), s, s, 0, -amp_bob * (0.5 - 0.5 * math.cos(2 * w)))
    return f


def float_(amp_rot=2.2, amp_bob=16, sway=6, breathe=0.012, **_):
    """무중력 둥둥 (떠오르며 기울고 좌우로)."""
    def f(t):
        w = 2 * math.pi * t / D
        s = 1 + breathe * math.sin(2 * w)
        return (amp_rot * math.sin(w), s, s, sway * math.sin(w + 0.8), -amp_bob * (0.5 - 0.5 * math.cos(w)))
    return f


def punch(hits=3, amount=0.06, **_):
    """줌 펀치 (hits 번 확 커졌다 줄어듦, 강조)."""
    def f(t):
        u = (t * hits / D) % 1.0
        s = 1 + amount * (1 - u) ** 4
        return (0, s, s, 0, 0)
    return f


def shake(amp=6, freq=6, **_):
    """덜덜 떨림 (좌우 진동, 신남·긴장)."""
    def f(t):
        w = 2 * math.pi * freq * t / D
        return (1.2 * math.sin(w), 1, 1, amp * math.sin(w), 3 * math.sin(2 * w))
    return f


def wobble(amp=9, cycles=2, **_):
    """젤리처럼 발을 축으로 크게 기울어짐 (귀엽게)."""
    def f(t):
        w = 2 * math.pi * cycles * t / D
        ang = amp * math.sin(w) * (0.75 + 0.25 * math.cos(2 * w))
        return (ang, 1 + 0.02 * math.sin(2 * w), 1 - 0.02 * math.sin(2 * w), 0, 0)
    return f


def hop(jumps=2, height=110, squash=0.16, **_):
    """점프 (움츠림→뛰어오름→착지 찌그러짐, 신남)."""
    def f(t):
        u = (t * jumps / D) % 1.0
        if u < 0.15:
            k = _smooth(u / 0.15); sy = 1 - squash * k; return (0, 1 / sy, sy, 0, 0)
        if u < 0.60:
            k = (u - 0.15) / 0.45; h = math.sin(math.pi * k)
            sy = 1 + 0.10 * h; return (2.5 * math.sin(math.pi * k), 1 / sy, sy, 0, -height * h)
        if u < 0.78:
            k = math.sin(math.pi * (u - 0.60) / 0.18); sy = 1 - squash * 1.1 * k; return (0, 1 / sy, sy, 0, 0)
        return (0, 1, 1, 0, 0)
    return f


def pop(hold=0.55, **_):
    """짠 등장 (작게서 튀어나와 오버슛, 끝에 빨려 들어감)."""
    def f(t):
        u = t / D
        if u < 0.30:
            s = 0.15 + 0.85 * _ease_out_back(u / 0.30, 2.2)
        elif u < 0.30 + hold:
            s = 1 + 0.015 * math.sin(2 * math.pi * (u - 0.30) / hold * 2)
        else:
            k = (u - 0.30 - hold) / (1 - 0.30 - hold); s = 1 - 0.85 * (k * k)
        s = max(0.08, s)
        return (0, s, s, 0, 0)
    return f


def slam(drops=1, height=420, **_):
    """위에서 쿵 착지 (강한 찌그러짐·리바운드, 임팩트). 루프마다 다시 떨어짐."""
    def f(t):
        u = (t * drops / D) % 1.0
        if u < 0.25:
            k = (u / 0.25) ** 2; return (0, 1, 1, 0, -height * (1 - k))
        if u < 0.36:
            k = math.sin(math.pi * (u - 0.25) / 0.11); sy = 1 - 0.28 * k; return (0, 1 / sy, sy, 0, 0)
        if u < 0.60:
            k = math.sin(math.pi * (u - 0.36) / 0.24); return (0, 1 - 0.05 * k, 1 + 0.05 * k, 0, -60 * k)
        if u < 0.72:
            k = math.sin(math.pi * (u - 0.60) / 0.12); sy = 1 - 0.08 * k; return (0, 1 / sy, sy, 0, 0)
        return (0, 1, 1, 0, 0)
    return f


def recoil(times=(0.10, 0.62, 1.14, 1.90, 2.42), kick=26, direction=-1, tilt=-1.6, shake_hz=22, **_):
    """총 반동 (times 마다 옆으로 밀리고 기울며 떨림, direction -1=왼쪽)."""
    def f(t):
        k = sum(_imp(t, s, 0.09) for s in times)
        sh = sum(_imp(t, s, 0.16) * math.sin(2 * math.pi * shake_hz * (t - s)) for s in times)
        s = 1 + 0.012 * k
        return (tilt * k + 0.8 * sh, s, s, direction * kick * k + 7 * sh, 3 * sh)
    return f


def jab(times=(0.22, 1.12, 2.02), lunge=0.11, **_):
    """잽 (움츠림→앞으로 찌름(확대)→되튕김, 엄지척·펀치)."""
    def f(t):
        ang = 0.0; s = 1.0; dx = 0.0; dy = 0.0
        for h in times:
            u = t - h
            if -0.14 <= u < 0:
                k = _smooth((u + 0.14) / 0.14); s *= 1 - 0.045 * k; dy += 10 * k; ang += 1.8 * k
            elif u >= 0:
                lg = math.exp(-u / 0.11) * math.sin(min(u / 0.06, 1) * math.pi / 2)
                rb = _imp(u, 0.06, 0.18) * math.sin(2 * math.pi * 9 * (u - 0.06)) if u > 0.06 else 0
                sh = _imp(u, 0, 0.22) * math.sin(2 * math.pi * 19 * u)
                s *= 1 + lunge * lg - 0.04 * rb
                dy += -14 * lg + 5 * rb + 4 * sh
                dx += 6 * sh
                ang += -2.2 * lg + 1.2 * rb + 0.8 * sh
        return (ang, s, s, dx, dy)
    return f


def zoom(amount=0.06, **_):
    """사진 숨쉬기 줌 (천천히 커졌다 돌아옴)."""
    def f(t):
        s = 1 + amount * (1 - math.cos(2 * math.pi * t / D)) / 2
        return (0, s, s, 0, 0)
    return f


def pan(amount=40, **_):
    """사진 좌우 드리프트 (느리게 옆으로)."""
    def f(t):
        return (0, 1, 1, amount * math.sin(2 * math.pi * t / D), 0)
    return f


def still(**_):
    """움직임 없음 (로고·아이콘)."""
    return lambda t: (0, 1, 1, 0, 0)


# ── 추가 (animate.css·Animista·만화 연출 중 아핀(회전·크기·이동·기울임)으로 되는 것) ──

def bounce_in(bounces=3, height=140, squash=0.16, **_):
    """통통 튀기 (bounce: 점점 작아지는 바운스, 착지마다 찌그러짐, 시작·끝은 정지)."""
    def f(t):
        u = t / D
        if u >= 0.72:
            return (0, 1, 1, 0, 0)
        k = u / 0.72
        seg = min(int(k * bounces), bounces - 1)
        p = k * bounces - seg
        h = height * (0.55 ** seg) * math.sin(math.pi * p)
        land = (p > 0.88 or (p < 0.12 and seg > 0))                      # 착지 직전·직후만 찌그러짐 (첫 프레임은 정지)
        sy = 1 - squash * (0.55 ** seg) * abs(math.cos(math.pi * p)) if land else 1.0
        return (0, 1 / sy, sy, 0, -h)
    return f


def rubber_band(amount=0.25, cycles=1, **_):
    """고무줄 (rubberBand: 가로로 늘었다 세로로 늘었다 감쇠)."""
    def f(t):
        u = (t * cycles / D) % 1.0
        if u > 0.7:
            return (0, 1, 1, 0, 0)
        k = u / 0.7
        env = amount * (1 - k) ** 1.2
        sx = 1 + env * math.sin(2 * math.pi * 2.5 * k)
        return (0, sx, 1 / sx, 0, 0)
    return f


def jello(amount=12.5, cycles=1, **_):
    """젤리 (jello: 좌우 기울임(skew)이 감쇠 진동)."""
    def f(t):
        u = (t * cycles / D) % 1.0
        if u > 0.75:
            return (0, 1, 1, 0, 0, 0)
        k = u / 0.75
        sk = math.radians(amount) * (1 - k) ** 1.5 * math.sin(2 * math.pi * 3 * k)
        return (0, 1, 1, 0, 0, sk)
    return f


def heart_beat(amount=0.14, beats=2, **_):
    """심장 박동 (heartBeat: 두 번 연속 쿵쿵 커짐)."""
    def f(t):
        u = (t * beats / D) % 1.0
        s = 1.0
        for start in (0.0, 0.22):
            if start <= u < start + 0.18:
                s += amount * math.sin(math.pi * (u - start) / 0.18)
        return (0, s, s, 0, 0)
    return f


def tada(amount=0.10, wiggle=3.0, cycles=1, **_):
    """따단 (tada: 살짝 줄었다 커지며 좌우로 3° 씩 흔들림)."""
    def f(t):
        u = (t * cycles / D) % 1.0
        if u < 0.1:
            k = u / 0.1; s = 1 - amount * 0.9 * math.sin(math.pi * k); return (-wiggle * math.sin(math.pi * k), s, s, 0, 0)
        if u < 0.7:
            k = (u - 0.1) / 0.6; s = 1 + amount * math.sin(math.pi * k)
            return (wiggle * math.sin(2 * math.pi * 3 * k) * math.sin(math.pi * k), s, s, 0, 0)
        return (0, 1, 1, 0, 0)
    return f


def swing(amp=15, cycles=1, **_):
    """그네 (swing: 머리 위를 축으로 좌우 진자, 감쇠)."""
    def f(t):
        w = 2 * math.pi * cycles * t / D
        ang = amp * math.sin(w) * (1 - 0.5 * (t / D)) if cycles == 1 else amp * math.sin(w)
        # 발 축 회전을 머리 축처럼 보이게: 회전한 만큼 반대로 이동 (머리 높이 ≈ 800 마스터 px)
        return (ang, 1, 1, -800 * math.sin(math.radians(ang)), 800 * (1 - math.cos(math.radians(ang))))
    return f


def flip(turns=1, axis="y", **_):
    """뒤집기 (flip: 세로축(y)·가로축(x)으로 한 바퀴, 음수 크기로 뒷면)."""
    def f(t):
        c = math.cos(2 * math.pi * turns * t / D)
        c = c if abs(c) > 0.02 else 0.02 * (1 if c >= 0 else -1)
        return (0, c, 1, 0, 0) if axis == "y" else (0, 1, c, 0, 0)
    return f


def roll_in(turns=1, **_):
    """굴러 들어옴 (rollIn: 왼쪽 밖에서 굴러 와 멈췄다 오른쪽 밖으로 굴러 나감 — 양끝이 화면 밖)."""
    def f(t):
        u = t / D
        if u < 0.38:
            k = _smooth(u / 0.38); return (-360 * turns * (1 - k), 1, 1, -1200 * (1 - k), 0)
        if u < 0.72:
            return (0, 1, 1, 0, 0)
        k = ((u - 0.72) / 0.28) ** 2; return (360 * turns * k, 1, 1, 1200 * k, 0)
    return f


def light_speed(amount=30, **_):
    """빛의 속도 (lightSpeedIn/Out: 기울어진 채 옆에서 확 들어와 멈추고 다시 나감)."""
    def f(t):
        u = t / D
        if u < 0.25:
            k = 1 - (1 - u / 0.25) ** 3; return (0, 1, 1, 1100 * (1 - k), 0, math.radians(-amount) * (1 - k))
        if u < 0.72:
            return (0, 1, 1, 0, 0, 0)
        k = ((u - 0.72) / 0.28) ** 3; return (0, 1, 1, -1100 * k, 0, math.radians(amount) * k)
    return f


def back_in(depth=0.4, **_):
    """뒤에서 앞으로 (backIn: 작게 시작해 오버슛으로 커지고, 끝에 다시 뒤로)."""
    def f(t):
        u = t / D
        if u < 0.3:
            s = (1 - depth) + depth * _ease_out_back(u / 0.3, 1.9)
        elif u < 0.8:
            s = 1.0
        else:
            k = (u - 0.8) / 0.2; s = 1 - depth * k * k
        return (0, s, s, 0, -60 * (1 - s))
    return f


def spin(turns=1, **_):
    """빙글 회전 (한 루프에 turns 바퀴, 중심 축)."""
    def f(t):
        return (360 * turns * t / D, 1, 1, 0, 0)
    return f


def shiver(amp=3, freq=24, **_):
    """오들오들 (아주 빠르고 작은 떨림 + 살짝 기울임)."""
    def f(t):
        w = 2 * math.pi * freq * t / D
        return (0.6 * math.sin(w * 1.5), 1, 1, amp * math.sin(w), amp * 0.5 * math.cos(w * 0.5 * 2))
    return f


def nod(amp=5, cycles=2, **_):
    """끄덕끄덕 (앞으로 숙였다 들기: 세로 눌림 + 기울임)."""
    def f(t):
        w = 2 * math.pi * cycles * t / D
        k = 0.5 - 0.5 * math.cos(w)
        return (amp * 0.4 * math.sin(w), 1 + 0.02 * k, 1 - 0.05 * k, 0, 18 * k)
    return f


def kenburns(amount=0.10, drift=50, **_):
    """켄 번즈 (사진: 천천히 확대하며 대각선으로 흐르고 돌아옴)."""
    def f(t):
        k = (1 - math.cos(2 * math.pi * t / D)) / 2
        s = 1 + amount * k
        return (0, s, s, drift * math.sin(2 * math.pi * t / D), -drift * 0.6 * k)
    return f


def jack_in_box(**_):
    """잭인더박스 (jackInTheBox: 작게 30° 기울어 튀어나와 -10°→3°→정지, 끝에 다시 상자로)."""
    def f(t):
        u = t / D
        if u < 0.35:
            k = u / 0.35; s = 0.1 + 0.9 * _ease_out_back(k, 1.4)
            ang = 30 * (1 - k) if k < 0.5 else (-10 * (1 - (k - 0.5) / 0.5) if k < 0.75 else 3 * (1 - (k - 0.75) / 0.25))
            return (ang, s, s, 0, 0)
        if u < 0.82:
            return (0, 1, 1, 0, 0)
        k = (u - 0.82) / 0.18; s = 1 - 0.9 * k * k; return (30 * k, s, s, 0, 0)
    return f


def head_shake(amp=14, **_):
    """도리도리 (headShake: 좌우로 점점 작게 흔들며 살짝 돌아봄)."""
    def f(t):
        u = t / D
        if u > 0.6:
            return (0, 1, 1, 0, 0)
        k = u / 0.6
        env = (1 - k) ** 1.3
        dx = amp * env * math.sin(2 * math.pi * 3.5 * k)
        return (0, 1 - 0.04 * env * abs(math.sin(2 * math.pi * 3.5 * k)), 1, dx * 8, 0)
    return f


def sway(amp=25, cycles=1, **_):
    """비틀비틀 (wobble-hor-bottom: 발 축으로 옆으로 밀리며 기울기, 감쇠)."""
    def f(t):
        u = (t * cycles / D) % 1.0
        if u > 0.8:
            return (0, 1, 1, 0, 0)
        k = u / 0.8; env = (1 - k) ** 1.1
        return (-6 * env * math.sin(2 * math.pi * 2.5 * k), 1, 1, -amp * 4 * env * math.sin(2 * math.pi * 2.5 * k), 0)
    return f


PRESETS = {
    "idle": idle, "float": float_, "breathe": lambda **k: idle(amp_rot=0.7, amp_bob=4, breathe=0.010, **k),
    "punch": punch, "shake": shake, "wobble": wobble, "hop": hop, "pop": pop, "slam": slam,
    "recoil": recoil, "jab": jab, "zoom": zoom, "pan": pan, "still": still,
    "bounce_in": bounce_in, "rubber_band": rubber_band, "jello": jello, "heart_beat": heart_beat, "tada": tada,
    "swing": swing, "flip": flip, "roll_in": roll_in, "light_speed": light_speed, "back_in": back_in, "spin": spin,
    "shiver": shiver, "nod": nod, "kenburns": kenburns, "jack_in_box": jack_in_box, "head_shake": head_shake, "sway": sway,
}


def build(specs) -> callable:
    """specs: list of {"type": name, ...params} (or a bare name string). Stacked."""
    fns = []
    for sp in specs or [{"type": "idle"}]:
        if isinstance(sp, str):
            sp = {"type": sp}
        name = sp.get("type")
        if name not in PRESETS:
            raise ValueError(f"unknown motion '{name}'. available: {', '.join(sorted(PRESETS))}")
        fns.append(PRESETS[name](**{k: v for k, v in sp.items() if k != "type"}))

    def f(t):
        ang = dx = dy = sh = 0.0; sx = sy = 1.0
        for fn in fns:
            r = fn(t)
            a, x, y, ddx, ddy = r[:5]
            ang += a; sx *= x; sy *= y; dx += ddx; dy += ddy; sh += r[5] if len(r) > 5 else 0.0
        return (ang, sx, sy, dx, dy, sh)
    return f
