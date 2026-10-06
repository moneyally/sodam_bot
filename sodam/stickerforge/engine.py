"""Frame renderer + encoder.

Quality rules baked in here:
  * the master is kept premultiplied so transparent black never bleeds into edges
  * exactly one resample per frame: rotate/scale/translate and 1024->512 in one affine
  * unpremultiply right before effects and saving
  * VP9 two-pass with alpha_mode=1, no alt-ref frames, and a bitrate ladder that
    walks down until the file fits (the alpha plane is not counted by -b:v, so
    transparent stickers need less than you'd guess)

소담 보강: photo 모드는 피사체(얼굴) 를 고려해 자르고(focus_window / framing), 프레임은 메모리에 두고
검수 지표(qc)·미리보기에 바로 쓰며 PNG 는 ffmpeg 입력용으로만 빠르게(compress_level 1) 쓴다.
움프(프로필 영상)는 같은 프레임을 640 H.264 로 두 바퀴 잇는다 (encode_profile).
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .const import S, FPS, NF, D, MASTER, ffmpeg
from . import motions as M
from . import fx as FX
from . import caption as CAP
from . import qc

LADDERS = {
    "cutout": (("460k", "28"), ("400k", "31"), ("340k", "34"), ("280k", "37"), ("220k", "40"), ("170k", "44")),
    "photo":  (("660k", "20"), ("560k", "24"), ("460k", "28"), ("380k", "32"), ("300k", "36"), ("240k", "40")),
    "icon":   (("80k", "14"), ("64k", "18"), ("50k", "22"), ("40k", "26"), ("32k", "30")),
    "emoji":  (("140k", "12"), ("110k", "16"), ("90k", "20"), ("70k", "24"), ("55k", "28")),
}
FRAMINGS = ("auto", "center", "top", "blur")
PROFILE_SIDE, PROFILE_LOOPS, PROFILE_MAX = 640, 2, 2 * 1024 * 1024     # 움프 규격 (avatar.py 와 같음)


def premultiply(im):
    a = np.asarray(im.convert("RGBA"), dtype=np.float32) / 255.0
    a[..., :3] *= a[..., 3:4]
    return Image.fromarray((a * 255 + 0.5).astype(np.uint8), "RGBA")


def unpremultiply(im):
    """반투명 픽셀(경계) 만 나눈다 — 대부분 픽셀은 알파 0 또는 255 라 그대로 (float 변환보다 3배 빠름)."""
    a = np.array(im, dtype=np.uint8)
    al = a[..., 3]
    edge = (al > 0) & (al < 255)
    if edge.any():
        rgb = a[edge][:, :3].astype(np.float32) * (255.0 / al[edge][:, None]) + 0.5
        a[edge, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    a[al == 0, :3] = 0
    return Image.fromarray(a, "RGBA")


def prepare_cutout(im: Image.Image, margin: float, bottom_px: int = S):
    """Crop to the alpha bbox and seat the subject on a MASTER canvas with
    `margin` of free space around it (room for motion). `bottom_px` (in 512
    units) reserves the strip below for a caption."""
    im = im.crop(im.getchannel("A").getbbox())
    limit = bottom_px * MASTER / S
    avail_w = MASTER * (1 - 2 * margin)
    avail_h = limit - MASTER * margin * 2
    sc = min(avail_w / im.width, avail_h / im.height)
    im = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))), Image.LANCZOS)
    c = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    x = (MASTER - im.width) // 2
    y = int(limit - MASTER * margin) - im.height
    c.alpha_composite(im, (x, y))
    pivot = (MASTER / 2, limit - MASTER * margin)          # feet
    return premultiply(c), pivot


def focus_window(im: Image.Image, top_bias: float = 0.35) -> tuple:
    """정사각형 자르기 창 (x0, y0, x1, y1). 관심 영역(에지 에너지) 이 가장 많이 들어오는 위치를 고르되,
    세로로 긴 사진은 위쪽(얼굴) 을 조금 더 쳐준다. 짧은 변이 창의 한 변."""
    w, h = im.size
    side = min(w, h)
    if w == h:
        return (0, 0, w, h)
    m = qc.focus_map(im, 64)                                   # 64×64, 0~1
    if h > w:                                                  # 세로: y 만 고른다
        prof = m.sum(axis=1)                                   # 행별 에너지
        span = max(1, round(64 * side / h))
        best, best_s = 0, -1.0
        for y in range(0, 64 - span + 1):
            s = prof[y:y + span].sum() * (1 + top_bias * (1 - y / max(1, 64 - span)))
            if s > best_s:
                best, best_s = y, s
        y0 = min(h - side, round(best / 64 * h))
        return (0, y0, side, y0 + side)
    prof = m.sum(axis=0)
    span = max(1, round(64 * side / w))
    best, best_s = 0, -1.0
    for x in range(0, 64 - span + 1):
        s = prof[x:x + span].sum()
        if s > best_s:
            best, best_s = x, s
    x0 = min(w - side, round(best / 64 * w))
    return (x0, 0, x0 + side, side)


def prepare_photo(im: Image.Image, overscan: float = 1.06, framing: str = "auto"):
    """정사각형으로 맞춰 MASTER 캔버스를 꽉 채운다 (pan·shake 가 가장자리를 드러내지 않게 살짝 크게).
    framing: auto(관심 영역 우선) · center(가운데) · top(위쪽) · blur(흐린 배경 위에 전체를 담음).
    → (master, pivot, info{window, framing})"""
    im = im.convert("RGBA")
    w, h = im.size
    side = min(w, h)
    if framing == "blur" and w != h:
        big = max(w, h)
        bg = im.resize((big, big), Image.BILINEAR).filter(ImageFilter.GaussianBlur(big / 24))
        bg = Image.blend(bg, Image.new("RGBA", bg.size, (0, 0, 0, 255)), 0.25)
        bg.alpha_composite(im, ((big - w) // 2, (big - h) // 2))
        im, window = bg, (0, 0, big, big)
    else:
        if framing == "center" or w == h:
            window = ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
        elif framing == "top":
            window = ((w - side) // 2, 0, (w - side) // 2 + side, side)
        else:
            window = focus_window(im)
        im = im.crop(window)
    im.putalpha(255)
    sc = MASTER * overscan / im.width
    im = im.resize((int(im.width * sc), int(im.height * sc)), Image.LANCZOS)
    c = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 255))
    c.alpha_composite(im, ((MASTER - im.width) // 2, (MASTER - im.height) // 2))
    return premultiply(c), (MASTER / 2, MASTER / 2), {"window": window, "framing": framing if framing in FRAMINGS else "auto"}


def rounded_mask(radius: int, ss: int = 4) -> Image.Image:
    m = Image.new("L", (S * ss, S * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S * ss - 1, S * ss - 1], radius=radius * ss, fill=255)
    return m.resize((S, S), Image.LANCZOS)


def affine(angle_deg, sx, sy, dx, dy, pivot, shear=0.0):
    """MASTER 좌표의 순방향 변환(기울임 → 크기·회전 → 이동 → 1024→512) 을 PIL 이 원하는 역행렬 계수로. shear = x 기울임(라디안)."""
    a = math.radians(angle_deg)
    cx, cy = pivot
    R = np.array([[math.cos(a) * sx, -math.sin(a) * sy, 0], [math.sin(a) * sx, math.cos(a) * sy, 0], [0, 0, 1]])
    SH = np.array([[1, math.tan(shear), 0], [0, 1, 0], [0, 0, 1]])
    T1 = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]])
    T2 = np.array([[1, 0, cx + dx], [0, 1, cy + dy], [0, 0, 1]])
    out = np.array([[S / MASTER, 0, 0], [0, S / MASTER, 0], [0, 0, 1]])
    inv = np.linalg.inv(out @ T2 @ R @ SH @ T1)
    return tuple(inv[:2].reshape(-1))


def cover_scale(ang, sx, sy, dx, dy, pivot, shear=0.0, lo=1.0, hi=3.0) -> float:
    """photo 모드 cover: 출력 네 모서리를 거꾸로 되짚어 MASTER 사진 안에 들어오는 가장 작은 추가 배율 (이분 탐색 12번).
    회전·이동 양으로 대충 키우면 축(pivot)이 멀 때 1.9배까지 과하게 커졌음 (쌍절곤 예시 미리보기에서 확인)."""
    def inside(k):
        c = affine(ang, sx * k, sy * k, dx, dy, pivot, shear)
        for x, y in ((0, 0), (S, 0), (0, S), (S, S)):
            mx, my = c[0] * x + c[1] * y + c[2], c[3] * x + c[4] * y + c[5]
            if not (-0.5 <= mx <= MASTER + 0.5 and -0.5 <= my <= MASTER + 0.5):
                return False
        return True
    if inside(lo):
        return lo
    if not inside(hi):
        return hi
    for _ in range(12):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if inside(mid) else (mid, hi)
    return hi


def hit_times(motion_specs) -> list:
    """Impulse motions expose their hit times so FX can sync to them."""
    hits = []
    for sp in motion_specs or []:
        if isinstance(sp, str):
            sp = {"type": sp}
        if sp.get("type") in ("recoil", "jab"):
            hits += list(sp.get("times") or M.PRESETS[sp["type"]].__defaults__[0])
        elif sp.get("type") == "slam":
            hits += [D * (i + 0.25) / sp.get("drops", 1) for i in range(sp.get("drops", 1))]
    return sorted(hits)


_BLACK = Image.new("RGBA", (S, S), (0, 0, 0, 255))
RAW = "frames.rgba"      # ffmpeg 입력: 89장 RGBA 원시 바이트 한 파일 (PNG 인코드·디코드 생략 → 그리기 1.5초·인코딩 1초 절약)


def timeline(spec: dict, product: str = "sticker") -> dict:
    """스티커 = 2.97초 반복(89장@30). 움프 loop=True = 같은 89장을 두 바퀴(5.93초), loop=False = 6초 한 번 흐름
    (ONCE_FPS 로 그려 CPU 를 아낌 — 2vCPU 서버에서 180장@30 은 무거운 조합이 20초를 넘김, tests/test_animation.py 실측)."""
    if product == "ump" and spec.get("loop") is False:
        nf = int(ONCE_SECONDS * ONCE_FPS)
        return {"nf": nf, "fps": ONCE_FPS, "rep": max(1, round(ONCE_SECONDS / D)), "seconds": ONCE_SECONDS, "loop": False}
    return {"nf": NF, "fps": FPS, "rep": 1, "seconds": D, "loop": True}


ONCE_SECONDS, ONCE_FPS = 6.0, 20


def _layer_fn(item: dict, idx: int):
    """레이어 하나 → fn(frame, u, t_old, ctx). 새 부품(prims)은 u, 옛 효과(fx)는 옛 시간 t — 둘 다 start·end 창."""
    from . import prims
    name = item["type"]
    params = {k: v for k, v in item.items() if k != "type"}
    if name in prims.LAYERS:
        fn = prims.LAYERS[name]

        def run(frame, u, t, ctx):
            return fn(frame, u, ctx, _layer=idx, **params)
        return run
    fn = FX.PRESETS[name]
    start, end = params.pop("start", 0.0), params.pop("end", 1.0)

    def run_fx(frame, u, t, ctx):
        if prims.window(u, start, end) is None:
            return frame
        return fn(frame, t, ctx, **params)
    return run_fx


def build(src: Image.Image, spec: dict, outdir: str | None, font: str, tl: dict | None = None, flatten: bool = False,
          last_only: bool = False) -> dict:
    """타임라인 nf 장을 그린다. outdir 가 있으면 outdir/frames.rgba (ffmpeg 입력) 로도 쓴다.
    그리는 순서: 움직임(motion·keyframes, 투명도) → fx(옛 효과) → layers(순서대로, 각자 start·end) → 자막.
    flatten = 움프용: 알파를 검정 위에 미리 곱해서 씀 (반투명 가장자리·fade 가 mp4 에서 제 밝기).
    → {"frames": [RGBA...], "mode", "caption": cap_info, "cap_box", "subject_boxes", "focus", "hits", "framing", "timeline"}"""
    tl = tl or timeline(spec)
    nf = tl["nf"]
    mode = spec.get("mode", "cutout")
    apply_fx = FX.build(spec.get("fx"))
    layers = [_layer_fn(it, i) for i, it in enumerate(spec.get("layers") or [])]
    ctx = {"spec": spec, "seed": int(spec.get("seed", 1)), "hits": hit_times(spec.get("motion")),
           "font": font, "prev": [], "timeline": tl}

    cap_frames, cap_info = None, None
    cap = spec.get("caption")
    if cap and cap.get("text"):
        style = CAP.Style(**{k: v for k, v in cap.items() if k not in ("text", "font")})
        if isinstance(style.anims, list):
            style.anims = tuple(style.anims)
        cap_frames, cap_info = CAP.render(cap["text"], cap.get("font") or font, style)

    focus, framing = None, None
    if mode == "photo":
        master, pivot, framing = prepare_photo(src, spec.get("overscan", 1.06), spec.get("framing", "auto"))
        mask = rounded_mask(int(spec.get("radius", 56))) if spec.get("radius", 56) > 0 else None
        focus = qc.focus_mask_512(src.crop(framing["window"]) if framing["framing"] != "blur" else src)
    else:
        bottom = S
        if cap_info and cap.get("position", "bottom") == "bottom":
            bottom = cap_info["band_top"] + 6            # subject sits above the caption band
        master, pivot = prepare_cutout(src, float(spec.get("margin", 0.08)), bottom)
        mask = None
    motion = M.build_u(spec.get("motion"), tl["rep"], pivot, tl["loop"])
    keyed = any((m.get("type") if isinstance(m, dict) else m) == "keyframes" for m in spec.get("motion") or [])
    cover = bool(spec.get("cover"))

    raw = None
    if outdir:
        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir)
        raw = open(os.path.join(outdir, RAW), "wb")
    frames, boxes = [], []
    for n in (range(nf - 1, nf) if last_only else range(nf)):      # 정지 스티커 = 마지막 장면 한 장만
        u = n / nf
        t = ((u * tl["rep"]) % 1.0) * D                     # 옛 부품(이름 있는 움직임·fx·자막)의 시간
        (ang, sx, sy, dx, dy, shear), op = motion(u)
        if mode == "photo" and cover:                        # 돌리고 옮겨도 가장자리가 안 보일 만큼만 확대
            k = cover_scale(ang, sx, sy, dx, dy, pivot, shear)
            sx, sy = sx * k, sy * k
        elif mode == "photo" and not keyed:                  # never reveal the canvas edge
            sx, sy = max(abs(sx), 1.0) * (1 if sx >= 0 else -1), max(abs(sy), 1.0) * (1 if sy >= 0 else -1)
        frame = master.transform((S, S), Image.AFFINE, affine(ang, sx, sy, dx, dy, pivot, shear), resample=Image.BICUBIC)
        frame = unpremultiply(frame)
        if mask is not None:
            frame.putalpha(ImageChops.multiply(frame.getchannel("A"), mask) if keyed else mask)
        if op < 1:
            frame.putalpha(frame.getchannel("A").point(lambda v, o=op: int(v * o)))
        box = frame.getchannel("A").getbbox() if mode == "cutout" else None
        boxes.append(box)   # 피사체 위치 (효과·자막 전)
        ctx["subject_box"] = box
        frame = apply_fx(frame, t, ctx)
        for run in layers:
            frame = run(frame, u, t, ctx)
        if cap_frames:
            frame.alpha_composite(cap_frames[min(NF - 1, int(round(t * FPS)))])
        ctx["prev"] = (ctx["prev"] + [frame])[-2:]
        frames.append(frame)
        if raw:
            if flatten:                                      # 검정 위에 평탄화 (PIL C 함수)
                raw.write(Image.alpha_composite(_BLACK, frame).tobytes())
            else:
                raw.write(frame.tobytes())
    if raw:
        raw.close()
    cap_box = None
    if cap_info:
        cap_box = (max(0, cap_info["left"] - 4), max(0, cap_info["band_top"]), min(S, cap_info["left"] + cap_info["width"] + 4),
                   cap_info["band_bottom"])
    return {"frames": frames, "mode": mode, "caption": cap_info, "cap_box": cap_box, "subject_boxes": boxes,
            "focus": focus, "hits": ctx["hits"], "framing": framing, "timeline": tl}


def _input(frames_dir, fps=FPS, loops=1) -> list:
    """raw RGBA 시퀀스 입력 인자 (PNG 디렉터리도 받음 — tools/디버그용)."""
    raw = os.path.join(frames_dir, RAW)
    loop = ["-stream_loop", str(loops - 1)] if loops > 1 else []
    if os.path.exists(raw):
        return [*loop, "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{S}x{S}", "-framerate", str(fps), "-i", raw]
    return [*loop, "-framerate", str(fps), "-i", os.path.join(frames_dir, "f%04d.png")]


def _encode(frames_dir, out, bitrate, crf, fps=FPS, extra_vf=None):
    args = _input(frames_dir, fps)
    if extra_vf:
        args += ["-vf", extra_vf]
    args += ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-metadata:s:v:0", "alpha_mode=1",
             "-b:v", bitrate, "-crf", crf, "-maxrate", "950k", "-bufsize", "1300k",
             "-g", str(NF + 1), "-row-mt", "1", "-tile-columns", "1", "-threads", "4", "-deadline", "good",
             "-auto-alt-ref", "0", "-an", "-t", str(D), "-f", "webm"]
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "pass")
        # 1차 패스는 통계만 모으므로 빠르게(cpu-used 4), 2차만 cpu-used 1 (best/0 대비 PSNR 같고 10배 빠름: 44→4초)
        for p, dst, speed in (("1", os.devnull, "4"), ("2", out, "1")):
            subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args, "-cpu-used", speed,
                            "-pass", p, "-passlogfile", log, dst], check=True, timeout=120)
    return os.path.getsize(out)


def encode_fit(frames_dir, out, ladder="cutout", limit=None, start=None):
    """비트레이트 사다리를 내려가며 용량 안에 넣는다. 넘치면 넘친 비율만큼 건너뛰어(대개 두 번째에 맞음) 인코딩 횟수를 아낀다.
    → (size, bitrate, crf)"""
    limits = {"cutout": 250 * 1024, "photo": 250 * 1024, "icon": 31 * 1024, "emoji": 62 * 1024}
    limit = limit or limits[ladder]
    steps = list(LADDERS[ladder])
    if start and any(s[0] == start for s in steps):
        steps = steps[[i for i, s in enumerate(steps) if s[0] == start][0]:]
    small = ladder in ("icon", "emoji")
    vf = "scale=100:100:flags=lanczos,unsharp=5:5:0.9:5:5:0.0" if small else None
    fps = 24 if ladder == "icon" else FPS
    size = br = crf = None
    i = 0
    while i < len(steps):
        br, crf = steps[i]
        size = _encode(frames_dir, out, br, crf, fps=fps, extra_vf=vf)
        if size <= limit:
            break
        want = int(br[:-1]) * limit / size * 0.92                    # 이 비율로 줄여야 들어감
        nxt = [j for j in range(i + 1, len(steps)) if int(steps[j][0][:-1]) <= want]
        i = nxt[0] if nxt else i + 1
    return size, br, crf


def encode_profile(frames_dir, out, side=PROFILE_SIDE, loops=PROFILE_LOOPS, limit=PROFILE_MAX, fps=FPS, seconds=D):
    """움프: 같은 프레임을 `loops` 바퀴 이어 640×640 H.264 MP4 (알파는 검정 위에 평탄화, 소리 없음, faststart).
    512→640 은 lanczos + 약한 unsharp. 2MB 넘으면 crf 를 올려 한 번 더. → (size, crf)"""
    vf = f"scale={side}:{side}:flags=lanczos,unsharp=5:5:0.6:5:5:0.0,format=yuv420p"
    size, crf = None, 22
    for _ in range(4):
        subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *_input(frames_dir, fps, loops),
                        "-vf", vf, "-t", f"{seconds * loops:.4f}", "-c:v", "libx264", "-profile:v", "main", "-preset", "veryfast",
                        "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", out],
                       check=True, timeout=120)
        size = os.path.getsize(out)
        if size <= limit or crf >= 36:
            break
        # x264 는 crf +6 에 용량 약 절반 → 넘친 비율만큼 한 번에 건너뜀 (인코딩 횟수 = CPU 절약)
        crf = min(36, crf + max(2, math.ceil(6 * math.log2(size / limit * 1.06))))
    return size, crf


def contact_sheet(frames, out, picks=(0, 12, 24, 36, 48, 60, 72, 88), tile=200, bg=(118, 128, 142)):
    """Preview strip on a neutral background so transparency is visible. `frames` = 디렉터리 또는 프레임 목록."""
    sheet = Image.new("RGBA", (tile * 4, tile * ((len(picks) + 3) // 4)), bg + (255,))
    for i, n in enumerate(picks):
        if isinstance(frames, list):
            f = frames[n]
        else:                                                   # 디렉터리: raw 파일에서 n번째 장
            with open(os.path.join(frames, RAW), "rb") as fh:
                fh.seek(n * S * S * 4)
                f = Image.frombytes("RGBA", (S, S), fh.read(S * S * 4))
        sheet.alpha_composite(f.convert("RGBA").resize((tile, tile), Image.LANCZOS), ((i % 4) * tile, (i // 4) * tile))
    sheet.convert("RGB").save(out)
    return out
