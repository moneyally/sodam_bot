"""🧩 스티커 공방 (telegram-sticker-forge 엔진, 스킬 `.claude/skills/telegram-sticker-forge/`).

사진 한 장 + 말로 한 요청 → 텔레그램 영상 스티커(512×512 VP9 WebM, 투명, 2.97초, 256KB↓) + 선택 100×100 팩 아이콘.
같은 엔진으로 움프(움직이는 프로필 640 H.264 6초) 도 만든다 (forge_video).
엔진(const·keying·motions·fx·caption·engine·verify)은 받은 그대로 두고, 소담 쪽에서 바꾼 것만:
- ffmpeg = imageio-ffmpeg 정적 바이너리(libvpx-vp9 포함), ffprobe 대신 `ffmpeg -i` 로 검사 (const.ffmpeg · verify._probe).
- **AI 가 준 spec 은 sanitize 로 다시 만듦**: 움직임·효과 이름은 목록만, 값은 그 함수가 받는 인자 이름 + 숫자/참거짓/숫자 목록만,
  범위는 SKILL.md 의 '화면이 하얗게 날아가지 않는' 한도로 자름. 파일 경로를 받는 rain/rise(image=)·font 는 막음 (서버 파일 읽기 X).
  `recipe` 이름 + seed 를 주면 recipes.vary 로 검증된 조합을 변주한 뒤 같은 검사를 거침.
- CPU 가 많이 드는 일(89장 그리기 + VP9 2-pass)이라 한 번에 하나(_LOCK), to_thread, 검사 표가 통과해야 ok.
- 소담이는 결과를 못 보므로 qc 가 코드로 잰 경고(warnings)·지표(metrics) 를 Result 에 담는다 → 한 번 고쳐 다시 만들 수 있음.
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
VIDEO_MAX_BYTES = 2 * 1024 * 1024      # 움프 2MB
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
    warnings: list = field(default_factory=list)   # qc 가 코드로 잰 경고 (한국어 한 줄씩, 고칠 방향 포함)
    metrics: dict = field(default_factory=dict)    # qc 지표 숫자
    mp4: bytes = b""                                # forge_video 결과 (움프)
    spec: dict = field(default_factory=dict)        # 실제로 쓴 spec

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
    """AI 가 준 spec → 엔진에 넣어도 되는 spec (또는 이유).
    raw["recipe"] 가 있으면 검증된 조합을 seed 로 변주한 것을 바탕으로 깔고, raw 의 나머지 키(caption·mode·fx…) 로 덮는다."""
    from . import caption as CAP, fx as FX, motions as M, recipes
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("recipe"):
        base = recipes.BY_NAME.get(str(raw["recipe"]))
        if not base:
            return None, f"recipe 는 {[r['name'] for r in recipes.RECIPES]} 중"
        varied = recipes.vary(base, int(raw.get("seed", 0)))
        merged = {**varied, **{k: v for k, v in raw.items() if k not in ("recipe",)}}
        if "caption" in raw and isinstance(raw["caption"], (str, dict)) and "palette" not in (raw["caption"] if isinstance(raw["caption"], dict) else {}):
            cap = raw["caption"] if isinstance(raw["caption"], dict) else {"text": raw["caption"]}
            merged["caption"] = {**cap, "palette": varied["palette"]}
        raw = merged
    mode = raw.get("mode", "cutout")
    if mode not in ("cutout", "photo"):
        return None, "mode 는 cutout(배경 빼기) / photo(사진 그대로)"
    key = (raw.get("keying") or {}) if isinstance(raw.get("keying"), dict) else {"mode": raw.get("keying") or "auto"}
    kmode = key.get("mode", "auto")
    if kmode not in ("auto", "white", "black", "color", "glow", "none"):
        return None, "keying 은 auto·white·black·color·glow·none"
    spec = {"mode": mode, "keying": {"mode": kmode, "tol": int(min(max(key.get("tol", 20), 5), 60))},
            "margin": float(min(max(raw.get("margin", 0.08), 0.04), 0.18)),
            "radius": int(min(max(raw.get("radius", 56), 0), 256)), "seed": int(raw.get("seed", 1)) % 1000,
            "framing": raw.get("framing") if raw.get("framing") in ("auto", "center", "top", "blur") else "auto"}
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


def _load(image: bytes, spec: dict, tmp: str):
    """원본 bytes → (RGBA, 키잉 방식)."""
    from PIL import Image
    from . import keying
    src_path = os.path.join(tmp, "src.img")
    with open(src_path, "wb") as f:
        f.write(image)
    if spec["mode"] == "cutout":
        return keying.key_image(src_path, spec["keying"]["mode"], tol=spec["keying"]["tol"])
    return Image.open(src_path).convert("RGBA"), "photo"


def _inspect(built: dict, used: str, holes: int | None, spec: dict) -> tuple[list, dict]:
    from . import qc
    return qc.inspect(built["frames"], mode=built["mode"], keying=used, subject_boxes=built["subject_boxes"],
                      cap_box=built["cap_box"], focus_mask=built["focus"], holes=holes, has_caption=bool(spec.get("caption")))


def render(image: bytes, spec: dict, icon: bool = False, keep: str | None = None) -> Result:
    """sanitize 된 spec 으로 스티커를 만들고 검사·검수까지 (동기 — forge 가 to_thread 로 감쌈)."""
    from . import caption as CAP, engine, verify
    tmp = tempfile.mkdtemp(prefix="sodam_stk_")
    try:
        src, used = _load(image, spec, tmp)
        frames_dir = os.path.join(tmp, "frames")
        built = engine.build(src, spec, frames_dir, FONT)
        out = os.path.join(tmp, "out.webm")
        engine.encode_fit(frames_dir, out, "photo" if spec["mode"] == "photo" else "cutout", limit=250 * 1024)
        cap = spec.get("caption")
        holes_ok = used != "glow" and not cap                        # 글자 속 구멍·후광 틈은 정상
        res = verify.spec(out, "sticker", holes=holes_ok)
        rows = list(res["rows"])
        holes = next((int(v.split()[0]) for n, v, _ in rows if n == "holes"), None)
        if cap:
            style = CAP.Style(**{k: v for k, v in cap.items() if k != "text"})
            style.anims = tuple(style.anims)
            g = CAP.glyph_check(cap["text"], FONT, style)
            rows.append(("glyphs", f"worst {g['worst']}%", g["ok"]))
        warnings, metrics = _inspect(built, used, holes, spec)
        prev = os.path.join(tmp, "preview.png")
        engine.contact_sheet(built["frames"], prev)
        icon_bytes = b""
        if icon:
            ip = os.path.join(tmp, "icon.webm")
            engine.encode_fit(frames_dir, ip, "icon")
            if verify.spec(ip, "icon")["ok"]:
                icon_bytes = open(ip, "rb").read()
        if keep:                                                     # 디버그: PNG 로 풀어 둠
            os.makedirs(keep, exist_ok=True)
            for i, f in enumerate(built["frames"]):
                f.save(os.path.join(keep, f"f{i:04d}.png"))
        return Result(all(r[2] for r in rows), open(out, "rb").read(), icon_bytes, open(prev, "rb").read(), rows, used,
                      warnings=warnings, metrics=metrics, spec=spec)
    except Exception as e:   # 이상한 사진·인코딩 실패
        return Result(False, error=f"{type(e).__name__}: {str(e)[:160]}", spec=spec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_video(image: bytes, spec: dict) -> Result:
    """움프: 같은 엔진으로 그린 뒤 640×640 H.264 6초 (3초 루프 ×2). 알파는 검정 위에 평탄화.
    photo 모드가 자연스럽고(radius 0 권장), cutout 도 됨(배경 검정)."""
    from . import engine
    spec = {**spec, "radius": spec.get("radius", 0) if spec["mode"] == "photo" else 0}
    tmp = tempfile.mkdtemp(prefix="sodam_ump_")
    try:
        src, used = _load(image, spec, tmp)
        frames_dir = os.path.join(tmp, "frames")
        built = engine.build(src, spec, frames_dir, FONT)
        out = os.path.join(tmp, "out.mp4")
        size, crf = engine.encode_profile(frames_dir, out)
        rows = [("size", f"{size / 1024:.0f}KB / {VIDEO_MAX_BYTES // 1024}KB", size <= VIDEO_MAX_BYTES),
                ("crf", str(crf), True)]
        warnings, metrics = _inspect(built, used, None, spec)
        prev = os.path.join(tmp, "preview.png")
        engine.contact_sheet(built["frames"], prev)
        return Result(size <= VIDEO_MAX_BYTES, mp4=open(out, "rb").read(), preview=open(prev, "rb").read(), rows=rows,
                      keying=used, warnings=warnings, metrics=metrics, spec=spec)
    except Exception as e:
        return Result(False, error=f"{type(e).__name__}: {str(e)[:160]}", spec=spec)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def forge(image: bytes, spec: dict, icon: bool = False) -> Result:
    """sanitize 된 spec 으로 만들고 검사까지 (한 번에 하나)."""
    async with _LOCK:
        return await asyncio.to_thread(render, image, spec, icon)


async def forge_video(image: bytes, spec: dict) -> Result:
    async with _LOCK:
        return await asyncio.to_thread(render_video, image, spec)


def looks_illustrated(image: bytes) -> bool:
    """이미 그림(일러스트·캐릭터·로고)인가? — 그림체 변환(art) 을 또 하면 돈·시간 낭비.
    가벼운 판별: 축소본에서 (1) 이웃과 거의 같은 색인 '평평한' 면이 많고 (2) 굵은 윤곽선이 있고 (3) 색 종류가 적으면 그림.
    실사는 노이즈·연속 톤이라 평평한 픽셀이 적고 색이 많다. 확신이 없으면 False (사진으로 취급)."""
    import io
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(image)).convert("RGB").resize((160, 160), Image.BILINEAR)
    a = np.asarray(im, dtype=np.int16)
    dx = np.abs(a[:, 1:] - a[:, :-1]).max(axis=2)
    dy = np.abs(a[1:] - a[:-1]).max(axis=2)
    flat = ((dx[:-1] <= 2) & (dy[:, :-1] <= 2)).mean()             # 이웃과 거의 같은 색 (면)
    edges = ((dx[:-1] > 40) | (dy[:, :-1] > 40)).mean()            # 굵은 선·윤곽
    colors = len(set(map(tuple, (a[::2, ::2] // 16).reshape(-1, 3).tolist())))   # 16단계 양자화 색 수
    return bool(flat > 0.45 and edges > 0.01 and colors < 260)
