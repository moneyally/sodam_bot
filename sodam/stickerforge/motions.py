"""Whole-body motion presets.

Every preset returns f(t) -> (angle_deg, sx, sy, dx, dy) in MASTER pixels, and
several presets can be stacked: angles and offsets add, scales multiply. All
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
    """Breathing sway. The default for anything calm."""
    def f(t):
        w = 2 * math.pi * cycles * t / D + phase
        s = 1 + breathe * math.sin(2 * w)
        return (amp_rot * math.sin(w), s, s, 0, -amp_bob * (0.5 - 0.5 * math.cos(2 * w)))
    return f


def float_(amp_rot=2.2, amp_bob=16, sway=6, breathe=0.012, **_):
    """Zero-gravity drift: rises, tilts and sways once per loop."""
    def f(t):
        w = 2 * math.pi * t / D
        s = 1 + breathe * math.sin(2 * w)
        return (amp_rot * math.sin(w), s, s, sway * math.sin(w + 0.8), -amp_bob * (0.5 - 0.5 * math.cos(w)))
    return f


def punch(hits=3, amount=0.06, **_):
    """Zoom hits that decay fast. `hits` per loop."""
    def f(t):
        u = (t * hits / D) % 1.0
        s = 1 + amount * (1 - u) ** 4
        return (0, s, s, 0, 0)
    return f


def shake(amp=6, freq=6, **_):
    def f(t):
        w = 2 * math.pi * freq * t / D
        return (1.2 * math.sin(w), 1, 1, amp * math.sin(w), 3 * math.sin(2 * w))
    return f


def wobble(amp=9, cycles=2, **_):
    """Jelly tilt around the feet with overshoot."""
    def f(t):
        w = 2 * math.pi * cycles * t / D
        ang = amp * math.sin(w) * (0.75 + 0.25 * math.cos(2 * w))
        return (ang, 1 + 0.02 * math.sin(2 * w), 1 - 0.02 * math.sin(2 * w), 0, 0)
    return f


def hop(jumps=2, height=110, squash=0.16, **_):
    """Anticipation crouch -> jump (stretch) -> landing (squash) -> rest."""
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
    """Pops in with overshoot, breathes, then gets sucked back out — seamless loop."""
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
    """Falls from above, squashes hard on impact, rebounds, settles.
    Frame 0 is mid-air, so the loop reads as a fresh drop each time."""
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
    """Gun-style kickback at each time: shoved sideways, tilted, then a fast
    decaying shake. `direction` -1 pushes left (muzzle on the right)."""
    def f(t):
        k = sum(_imp(t, s, 0.09) for s in times)
        sh = sum(_imp(t, s, 0.16) * math.sin(2 * math.pi * shake_hz * (t - s)) for s in times)
        s = 1 + 0.012 * k
        return (tilt * k + 0.8 * sh, s, s, direction * kick * k + 7 * sh, 3 * sh)
    return f


def jab(times=(0.22, 1.12, 2.02), lunge=0.11, **_):
    """Thumbs-up / punch recoil: crouch (anticipation) -> lunge forward (scale up)
    -> spring back with a 9 Hz rebound -> rest. Different feel from `recoil`,
    which is pushed *back*; this one strikes *toward* the viewer."""
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
    """Photo-mode breathing zoom (cosine, so start == end)."""
    def f(t):
        s = 1 + amount * (1 - math.cos(2 * math.pi * t / D)) / 2
        return (0, s, s, 0, 0)
    return f


def pan(amount=40, **_):
    """Photo-mode slow horizontal drift (needs the photo over-scanned)."""
    def f(t):
        return (0, 1, 1, amount * math.sin(2 * math.pi * t / D), 0)
    return f


def still(**_):
    return lambda t: (0, 1, 1, 0, 0)


PRESETS = {
    "idle": idle, "float": float_, "breathe": lambda **k: idle(amp_rot=0.7, amp_bob=4, breathe=0.010, **k),
    "punch": punch, "shake": shake, "wobble": wobble, "hop": hop, "pop": pop, "slam": slam,
    "recoil": recoil, "jab": jab, "zoom": zoom, "pan": pan, "still": still,
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
        ang = dx = dy = 0.0; sx = sy = 1.0
        for fn in fns:
            a, x, y, ddx, ddy = fn(t)
            ang += a; sx *= x; sy *= y; dx += ddx; dy += ddy
        return (ang, sx, sy, dx, dy)
    return f
