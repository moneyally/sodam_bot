"""🔍 전달받은 이모지 조각 분해·분석 (트랙 B '이거처럼 똑같이', sodam/stickerforge/analyze.py).
우리가 만든 스타일 타일을 되분석했을 때 스타일을 맞히고, 벡터가 스타일끼리 구분되는지(= 벡터 DB 매칭 근거).

뮤테이션: guess_style 규칙 하나 뒤집으면 round_trip FAIL / tile_features 가 상수면 vectors_distinguish FAIL.
"""
import numpy as np
from fakes import runner

from sodam.stickerforge import analyze as AZ
from sodam.stickerforge import banner as B
from sodam.stickerforge import stylefx as SX   # IM 유무 — 서버엔 IM 없어 폴백(스타일 구분은 IM 있을 때만 검사)

test, run_all = runner()
STYLES = ("gold", "chrome", "neon", "fire", "glass")


def _tile(style):
    r = B.build_tiles({"cols": 1, "rows": 1, "bg": "transparent",
                       "layers": [{"type": "text", "text": "로", "style": style, "at": [0.5, 0.5], "height": 0.75}]})
    return r["tiles"][0]


@test
async def round_trip_style_detection():
    """우리가 만든 스타일을 되분석하면 대부분 맞혀야 함 (최소 4/5). IM 폴백(서버)은 스타일이 다 금속 비슷 → 구분 안 됨."""
    if not SX.HAVE_IM:
        return                                       # 서버엔 IM 없어 폴백 → 크롬·네온·불·유리가 다 금속 비슷 (아래 벡터/금색은 유지)
    ok = sum(AZ.guess_style(_tile(s)) == s for s in STYLES)
    assert ok >= 4, ok


@test
async def vectors_distinguish_styles():
    """지문 벡터가 스타일끼리 떨어져 있어야 매칭/학습이 됨 (벡터 차원은 IM 무관, 거리는 IM 있을 때만)."""
    vs = {s: AZ.tile_features(_tile(s)) for s in STYLES}
    assert all(len(v) == AZ.VEC_DIM for v in vs.values())
    if not SX.HAVE_IM:
        return                                       # 폴백은 스타일이 비슷해 거리 작음 — IM 있을 때만 떨어짐 검사
    # 같은 스타일은 가깝고(자기 자신=0), 다른 스타일은 멀다
    d_gold_neon = np.linalg.norm(vs["gold"] - vs["neon"])
    d_gold_chrome = np.linalg.norm(vs["gold"] - vs["chrome"])
    assert d_gold_neon > 0.4 and d_gold_chrome > 0.2


@test
async def analyze_tile_returns_tokens():
    tok = AZ.analyze_tile(_tile("gold"))
    assert tok["style"] == "gold"
    assert len(tok["colors"]) >= 2 and all(len(c) == 3 for c in tok["colors"])
    assert isinstance(tok["has_frame"], bool) and len(tok["vector"]) == AZ.VEC_DIM
    # 금색 그라데이션: 위가 아래보다 밝아야 (금속)
    top, bot = sum(tok["colors"][0]), sum(tok["colors"][-1])
    assert top >= bot - 60


@test
async def framed_tile_detected():
    r = B.build_tiles({"cols": 1, "rows": 1, "bg": "transparent",
                       "frames": {"fill": [17, 18, 20], "border": 0.06, "border_color": [172, 143, 47], "radius": 0.1, "gap": 0.05},
                       "layers": [{"type": "text", "text": "로", "style": "gold", "at": [0.5, 0.5], "height": 0.6}]})
    assert AZ.analyze_tile(r["tiles"][0])["has_frame"] is True


@test
async def reproduce_copies_style_and_frame():
    """금테 금색 샘플 → 분석 → 새 글자로 재현하면 금색·프레임 그대로 ('이거처럼 똑같이')."""
    samp = B.build_tiles({"cols": 2, "rows": 1, "bg": "transparent",
                          "frames": {"fill": [17, 18, 20], "border": 0.05, "border_color": [172, 143, 47], "radius": 0.11, "gap": 0.08},
                          "layers": [{"type": "text", "text": t, "style": "gold", "at": [(i + 0.5) / 2, 0.5], "height": 0.6}
                                     for i, t in enumerate("로얄")]})
    spec = AZ.reproduce_spec(samp["tiles"], "클럽보증")
    assert spec["frames"] is not None and len(spec["layers"]) == 4
    assert all(l["style"] == "gold" for l in spec["layers"])
    r = B.build_tiles(spec)
    assert r["qa_error"] == 0.0 and AZ.guess_style(r["tiles"][0]) == "gold"


@test
async def reproduce_no_frame_when_sample_has_none():
    samp = B.build_tiles({"cols": 2, "rows": 1, "bg": "transparent",
                          "layers": [{"type": "text", "text": t, "style": "neon", "at": [(i + 0.5) / 2, 0.5], "height": 0.75}
                                     for i, t in enumerate("AB")]})
    spec = AZ.reproduce_spec(samp["tiles"], "CD")
    assert spec["frames"] is None                    # 샘플에 테두리 없으면 프레임 없음 (IM 무관)
    if SX.HAVE_IM:                                    # 스타일 되맞히기는 IM 있을 때만 (폴백은 네온도 금속 비슷)
        assert all(l["style"] == "neon" for l in spec["layers"])


@test
async def empty_tile_safe():
    from PIL import Image
    tok = AZ.analyze_tile(Image.new("RGBA", (100, 100), (0, 0, 0, 0)))
    assert tok["style"] in STYLES and len(tok["vector"]) == AZ.VEC_DIM


if __name__ == "__main__":
    run_all()
