"""🧩 스티커 공방 (telegram-sticker-forge 엔진, 스킬 `.claude/skills/telegram-sticker-forge/`).

사진 한 장 + 말로 한 요청 → 텔레그램 영상 스티커(512×512 VP9 WebM, 투명, 2.97초, 256KB↓) + 선택 100×100 팩 아이콘.
엔진(const·keying·motions·fx·caption·engine·verify)은 받은 그대로 두고, 소담 쪽에서 바꾼 것만:
- ffmpeg = imageio-ffmpeg 정적 바이너리(libvpx-vp9 포함), ffprobe 대신 `ffmpeg -i` 로 검사 (const.ffmpeg · verify._probe).
- **AI 가 준 spec 은 sanitize 로 다시 만듦**: 움직임·효과 이름은 목록만, 값은 그 함수가 받는 인자 이름 + 숫자/참거짓/숫자 목록만,
  범위는 SKILL.md 의 '화면이 하얗게 날아가지 않는' 한도로 자름. 파일 경로를 받는 rain/rise(image=)·font 는 막음 (서버 파일 읽기 X).
- CPU 가 많이 드는 일(89장 그리기 + VP9 2-pass)이라 한 번에 하나(_LOCK), to_thread, 검사 표가 통과해야 ok.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "..", "data_files", "fonts", "BlackHanSans.ttf")   # OFL, 굵은 한글 제목체
MAX_MOTIONS, MAX_FX, MAX_CAPTION = 2, 4, 12
BLOCKED_FX = {"rain", "rise"}          # image= 로 파일 경로를 받음
CLAMP = {                              # (최소, 최대) — 넘으면 잘라서 씀
    "amp_rot": (0, 8), "amp_bob": (0, 40), "breathe": (0, 0.05), "cycles": (1, 4), "hits": (1, 5), "amount": (0, 80),
    "amp": (0, 12), "freq": (1, 12), "jumps": (1, 3), "height": (0, 420), "squash": (0, 0.3), "drops": (1, 2),
    "kick": (0, 40), "lunge": (0, 0.2), "strength": (0, 0.8), "width": (0, 12), "radius": (0, 30), "flare": (0, 0.35),
    "flicker": (0, 0.15), "bursts": (0, 5), "k": (1, 20), "bands": (1, 12), "shift": (0, 40), "split": (0, 6),
    "spokes": (4, 24), "count": (1, 10), "size": (10, 120), "r0": (0, 300), "r1": (50, 400), "life": (0.05, 0.6),
    "sway": (0, 20), "hold": (0.2, 0.7), "direction": (-1, 1), "tilt": (-4, 4), "shake_hz": (5, 30),
    "height_px": (20, 140), "alpha": (0, 0.8), "tau": (0.03, 0.4), "pulse": (0, 1), "phase": (0, 6.3),
}
SPECIAL = {"flashbang": {"amount": (0, 60)}, "glitch": {"amp": (0, 9)}, "punch": {"amount": (0, 0.12)},
           "zoom": {"amount": (0, 0.12)}, "pan": {"amount": (0, 60)}, "scan": {"height": (20, 140)}}


@dataclass
class Result:
    ok: bool
    webm: bytes = b""
    icon: bytes = b""
    preview: bytes = b""
    rows: list = field(default_factory=list)
    keying: str = ""
    error: str = ""

    def summary(self) -> str:
        bad = [f"{n} {v}" for n, v, ok in self.rows if not ok]
        return "통과" if self.ok else ("검사 실패: " + ", ".join(bad) if bad else self.error or "실패")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clean_params(kind: str, fn, raw: dict) -> dict:
    allowed = {p for p in inspect.signature(fn).parameters if p not in ("frame", "t", "ctx", "_", "image")}
    out = {}
    for k, v in (raw or {}).items():
        if k not in allowed:
            continue
        lo_hi = SPECIAL.get(kind, {}).get(k) or CLAMP.get(k)
        if isinstance(v, bool):
            out[k] = v
        elif _num(v):
            out[k] = min(max(v, lo_hi[0]), lo_hi[1]) if lo_hi else v
            if isinstance(v, int) and k in ("hits", "cycles", "jumps", "drops", "bursts", "count", "bands", "spokes"):
                out[k] = int(out[k])
        elif isinstance(v, (list, tuple)) and len(v) <= 12 and all(_num(x) for x in v):
            if k == "subset":
                out[k] = [int(x) for x in v if 0 <= x <= 9]
            elif k == "color" and len(v) == 3:                      # PIL 은 정수 색만
                out[k] = tuple(int(min(max(x, 0), 255)) for x in v)
            elif k in ("center", "at", "origin") and len(v) == 2:  # 512 캔버스 좌표 (center 는 0~1 비율도 받음)
                out[k] = tuple(x if isinstance(x, float) and x <= 1 else int(min(max(x, 0), 512)) for x in v)
            elif k == "times":
                out[k] = [float(min(max(x, 0), 2.3)) for x in v][:6]   # 충격은 끝나기 0.6초 전까지 (반복 이음새)
        elif isinstance(v, (list, tuple)) and len(v) <= 6 and all(isinstance(x, (list, tuple)) for x in v):
            continue   # meteors paths 같은 중첩 목록은 기본값으로
    return out


def sanitize(raw: dict) -> tuple[dict | None, str | None]:
    """AI 가 준 spec → 엔진에 넣어도 되는 spec (또는 이유)."""
    from . import caption as CAP, fx as FX, motions as M
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode", "cutout")
    if mode not in ("cutout", "photo"):
        return None, "mode 는 cutout(배경 빼기) / photo(사진 그대로)"
    key = (raw.get("keying") or {}) if isinstance(raw.get("keying"), dict) else {"mode": raw.get("keying") or "auto"}
    kmode = key.get("mode", "auto")
    if kmode not in ("auto", "white", "black", "color", "glow", "none"):
        return None, "keying 은 auto·white·black·color·glow·none"
    spec = {"mode": mode, "keying": {"mode": kmode, "tol": int(min(max(key.get("tol", 20), 5), 60))},
            "margin": float(min(max(raw.get("margin", 0.08), 0.04), 0.18)),
            "radius": int(min(max(raw.get("radius", 56), 0), 120)), "seed": int(raw.get("seed", 1)) % 1000}
    spec["motion"], spec["fx"] = [], []
    for item in (raw.get("motion") or [{"type": "idle"}])[:MAX_MOTIONS]:
        item = {"type": item} if isinstance(item, str) else item
        name = (item or {}).get("type")
        if name not in M.PRESETS:
            return None, f"motion 은 {sorted(M.PRESETS)} 중"
        fn = {"breathe": M.idle, "float": M.float_}.get(name, M.PRESETS[name])
        spec["motion"].append({"type": name, **_clean_params(name, fn, item)})
    for item in (raw.get("fx") or [])[:MAX_FX]:
        item = {"type": item} if isinstance(item, str) else item
        name = (item or {}).get("type")
        if name not in FX.PRESETS or name in BLOCKED_FX:
            return None, f"fx 는 {sorted(set(FX.PRESETS) - BLOCKED_FX)} 중"
        spec["fx"].append({"type": name, **_clean_params(name, FX.PRESETS[name], item)})
    cap = raw.get("caption")
    if isinstance(cap, str):
        cap = {"text": cap}
    if isinstance(cap, dict) and str(cap.get("text") or "").strip():
        text = " ".join(str(cap["text"]).split())
        if len(text) > MAX_CAPTION:
            return None, f"글자는 {MAX_CAPTION}자까지 (2~7자가 가장 예쁨)"
        anims = [a for a in (cap.get("anims") or ["bounce", "punch", "shine"])
                 if a in ("bounce", "punch", "shine", "wave", "shake", "glow")][:3] or ["bounce", "punch", "shine"]
        spec["caption"] = {"text": text, "palette": cap.get("palette") if cap.get("palette") in CAP.PALETTES else "gold",
                           "anims": anims, "position": "top" if cap.get("position") == "top" else "bottom",
                           "typing": bool(cap.get("typing", True))}
    return spec, None


_LOCK = asyncio.Semaphore(1)


def _run(image: bytes, spec: dict, icon: bool) -> Result:
    from PIL import Image
    from . import caption as CAP, engine, keying, verify
    tmp = tempfile.mkdtemp(prefix="sodam_stk_")
    try:
        src_path = os.path.join(tmp, "src.img")
        with open(src_path, "wb") as f:
            f.write(image)
        if spec["mode"] == "cutout":
            src, used = keying.key_image(src_path, spec["keying"]["mode"], tol=spec["keying"]["tol"])
        else:
            src, used = Image.open(src_path).convert("RGBA"), "photo"
        frames = os.path.join(tmp, "frames")
        engine.build(src, spec, frames, FONT)
        out = os.path.join(tmp, "out.webm")
        engine.encode_fit(frames, out, "photo" if spec["mode"] == "photo" else "cutout", limit=250 * 1024)
        cap = spec.get("caption")
        res = verify.spec(out, "sticker", holes=used != "glow" and not cap)   # 글자 속 구멍·후광 틈은 정상
        rows = list(res["rows"])
        if cap:
            style = CAP.Style(**{k: v for k, v in cap.items() if k != "text"})
            style.anims = tuple(style.anims)
            g = CAP.glyph_check(cap["text"], FONT, style)
            rows.append(("glyphs", f"worst {g['worst']}%", g["ok"]))
        prev = os.path.join(tmp, "preview.png")
        engine.contact_sheet(frames, prev)
        icon_bytes = b""
        if icon:
            ip = os.path.join(tmp, "icon.webm")
            engine.encode_fit(frames, ip, "icon")
            if verify.spec(ip, "icon")["ok"]:
                icon_bytes = open(ip, "rb").read()
        return Result(all(r[2] for r in rows), open(out, "rb").read(), icon_bytes, open(prev, "rb").read(), rows, used)
    except Exception as e:   # 이상한 사진·인코딩 실패
        return Result(False, error=f"{type(e).__name__}: {str(e)[:160]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def forge(image: bytes, spec: dict, icon: bool = False) -> Result:
    """sanitize 된 spec 으로 만들고 검사까지."""
    async with _LOCK:
        return await asyncio.to_thread(_run, image, spec, icon)
