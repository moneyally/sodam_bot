"""ImageMagick 기반 글자 스타일 레시피 — 하드코딩 템플릿 하나가 아니라 스타일 이름 + 색/세기 값으로 무한 조합.

오너 결정 2026-10-22: 손님이 어떤 스타일을 원할지 모르니 금테 하나로 박지 말고 오픈소스(ImageMagick)로 아무 스타일이나.
각 스타일 = convert 명령 조립 → 투명 PNG 글자 한 장. IM 없으면 None 반환 → banner 가 PIL(_metal_fill)로 폴백.
새 스타일 = STYLES 에 함수 한 줄 추가 (코드 진실, 문서 따로 안 적음). 참고: legacy.imagemagick.org/Usage/fonts, Fred's IM scripts.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from .layout import font_path

_BIN = shutil.which("magick") or shutil.which("convert")
HAVE_IM = bool(_BIN)
# magick 은 'magick convert ...' 또는 'magick ...' 둘 다 되지만, 서브명령 없이 쓰려면 'magick'. convert(IM6)는 단독.
_PREFIX = [_BIN] if _BIN and _BIN.endswith("convert") else ([_BIN] if _BIN else [])


def _hex(c) -> str:
    c = list(c)[:3]
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c)


def _run(args: list[str]) -> bool:
    try:
        r = subprocess.run(_PREFIX + args, capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _mask(text: str, font: str, size: int, d: Path) -> Path:
    """글자 모양(흰 글자/검은 바탕). 캔버스는 넉넉히."""
    W = H = int(size * 2)
    mp = d / "mask.png"
    _run(["-size", f"{W}x{H}", "xc:black", "-font", font_path(font), "-pointsize", str(size),
          "-gravity", "center", "-fill", "white", "-annotate", "0", text, str(mp)])
    return mp


def _apply_opacity(src: Path, mask: Path, out: Path) -> None:
    """src 색을 글자 모양으로 오려냄(CopyOpacity)."""
    _run([str(src), str(mask), "-alpha", "off", "-compose", "CopyOpacity", "-composite", str(out)])


# ── 스타일들: (text, font, size, tmpdir, params) → 최종 PNG 경로 ─────────────────
def _gradient_fill(text, font, size, d, top, bot, *, bevel=0.0, stroke=0, stroke_color=(29, 23, 8)):
    """세로 그라데이션 + (선택)shade 베벨 + (선택)어두운 테두리. 금·크롬·불·유리 공통 바탕."""
    W = H = int(size * 2)
    mask = _mask(text, font, size, d)
    grad, core, out = d / "grad.png", d / "core.png", d / "out.png"
    _run(["-size", f"{W}x{H}", f"gradient:{_hex(top)}-{_hex(bot)}", str(grad)])
    _apply_opacity(grad, mask, core)
    if bevel > 0:                                             # shade 로 금속 입체
        shade = d / "shade.png"
        _run([str(mask), "-shade", f"120x{int(20 + bevel * 25)}", "-auto-level", str(shade)])
        shaded = d / "shaded.png"
        _apply_opacity(shade, mask, shaded)
        _run([str(core), str(shaded), "-compose", "Overlay", "-composite",
              str(mask), "-alpha", "off", "-compose", "CopyOpacity", "-composite", str(out)])
        core = out
    if stroke > 0:                                           # 어두운 테두리(글자 뒤 확장)
        edge, final = d / "edge.png", d / "final.png"
        _run([str(mask), "-bordercolor", "black", "-border", str(stroke),
              "-morphology", "Dilate", f"Disk:{stroke}", str(edge)])
        sc = d / "sc.png"
        _run(["-size", f"{W + 2 * stroke}x{H + 2 * stroke}", f"xc:{_hex(stroke_color)}", str(sc)])
        _apply_opacity(sc, edge, sc)
        _run([str(sc), "-gravity", "center", str(core), "-composite", str(final)])
        return final
    return core


def s_gold(text, font, size, d, **p):
    """금속 금색 (그라데이션 + 베벨 + 어두운 테두리)."""
    return _gradient_fill(text, font, size, d, p.get("top", [246, 241, 94]), p.get("bot", [130, 113, 42]),
                          bevel=p.get("bevel", 0.8), stroke=int(p.get("stroke", 5)), stroke_color=p.get("stroke_color", [29, 23, 8]))


def s_chrome(text, font, size, d, **p):
    """크롬·은색 (밝은 회청 그라데이션 + 강한 베벨)."""
    return _gradient_fill(text, font, size, d, p.get("top", [245, 250, 255]), p.get("bot", [120, 140, 170]),
                          bevel=p.get("bevel", 1.0), stroke=int(p.get("stroke", 5)), stroke_color=p.get("stroke_color", [20, 28, 44]))


def s_fire(text, font, size, d, **p):
    """불꽃 (노랑→빨강 그라데이션 + 바깥 주황 글로우)."""
    core = _gradient_fill(text, font, size, d, p.get("top", [255, 240, 120]), p.get("bot", [200, 40, 10]),
                          bevel=p.get("bevel", 0.3), stroke=int(p.get("stroke", 3)), stroke_color=p.get("stroke_color", [60, 10, 0]))
    return _glow(core, d, p.get("glow_color", [255, 120, 30]), p.get("glow", 10))


def s_neon(text, font, size, d, **p):
    """네온 (색 글로우 + 그 색 글자 + 흰 심지 — 글자가 또렷하게 보이게)."""
    col = p.get("color", [90, 220, 255])
    W = H = int(size * 2)
    mask = _mask(text, font, size, d)
    sc, core = d / "nc.png", d / "neoncore.png"
    _run(["-size", f"{W}x{H}", f"xc:{_hex(col)}", str(sc)])
    _apply_opacity(sc, mask, core)
    inner = d / "inner.png"                                   # 흰 심지: 글자 속을 깎아 흰색
    _run([str(mask), "-morphology", "Erode", "Disk:4", str(inner)])
    white = d / "white.png"
    _run(["-size", f"{W}x{H}", "xc:white", str(white)])
    _apply_opacity(white, inner, inner)
    lit = d / "lit.png"
    _run([str(core), str(inner), "-gravity", "center", "-compose", "over", "-composite", str(lit)])
    return _glow(lit, d, col, p.get("glow", 9))


def s_glass(text, font, size, d, **p):
    """유리 (반투명 밝은 그라데이션 + 윗부분 하이라이트)."""
    core = _gradient_fill(text, font, size, d, p.get("top", [235, 245, 255]), p.get("bot", [150, 180, 210]),
                          bevel=p.get("bevel", 0.6), stroke=int(p.get("stroke", 4)), stroke_color=p.get("stroke_color", [40, 60, 90]))
    out = d / "glass.png"
    _run([str(core), "-channel", "A", "-evaluate", "multiply", "0.82", "+channel", str(out)])
    return out


def _glow(core: Path, d: Path, color, radius: int, passes: int = 1) -> Path:
    """core 글자 뒤에 color 로 바깥 빛번짐. radius px 가우시안 여러 겹."""
    if radius <= 0:
        return core
    halo = d / "halo.png"
    _run([str(core), "-channel", "A", "-blur", f"0x{radius}", "-level", "0,60%", "+channel",
          "-fill", _hex(color), "-colorize", "100", str(halo)])
    out = d / "glowed.png"
    base = halo
    for _ in range(passes - 1):                               # 여러 겹이면 글로우 더 진하게
        _run([str(base), str(halo), "-compose", "Screen", "-composite", str(out)])
        base = out
    _run([str(base), str(core), "-gravity", "center", "-composite", str(out)])
    return out


STYLES = {"gold": s_gold, "metal": s_gold, "chrome": s_chrome, "silver": s_chrome,
          "fire": s_fire, "neon": s_neon, "glass": s_glass}


def render(text: str, style: str, *, font: str = "bold", size: int = 200, **params) -> Image.Image | None:
    """스타일 글자 한 장(투명 RGBA, 꼭 맞게 자름). IM 없거나 모르는 스타일이면 None → 호출부가 PIL 폴백."""
    if not HAVE_IM or style not in STYLES or not text:
        return None
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        try:
            out = STYLES[style](text, font, int(size), d, **params)
            if not out or not Path(out).exists():
                return None
            im = Image.open(out).convert("RGBA")
        except Exception:
            return None
    bb = im.getbbox()
    return im.crop(bb) if bb else im
