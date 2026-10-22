"""분할(타일) 커스텀 이모지 처음부터 만들기 (2026-10-22 오너 요청, sodam/stickerforge/banner.py).
핵심: 전체 캔버스에 한 번 그려 → 한 번만 축소 → 격자 crop → 재조립 오차 0 (이음새 없음).

뮤테이션 포인트:
- render_banner 의 '한 번만 축소'(최종 크기로 resize)를 빼거나 타일별로 축소하면 → reassembly_is_lossless 가 FAIL
- make_tiles 의 crop 좌표(c*tile)가 틀리면 → reassembly_is_lossless·tile_count_and_size FAIL
- cols/rows 클램프를 빼면 → clamps_grid FAIL
"""
from fakes import runner
from PIL import Image

from sodam.stickerforge import banner as B

test, run_all = runner()

TEXT_SPEC = {"cols": 8, "rows": 1, "bg": {"gradient": [[20, 24, 40], [60, 20, 90]], "dir": "h"},
             "layers": [{"type": "text", "text": "사이트제휴", "font": "bold", "at": [0.5, 0.5], "height": 0.72,
                         "colors": [[255, 240, 180], [255, 180, 40]], "stroke": 6, "stroke_color": [30, 10, 0]}]}


@test
async def reassembly_is_lossless():
    """한 번만 축소 + 정확한 crop → 재조립 오차 0 (경계 이음새 없음)."""
    r = B.build_tiles(TEXT_SPEC)
    assert r["qa_error"] == 0.0, r["qa_error"]


@test
async def tile_count_and_size():
    r = B.build_tiles(TEXT_SPEC)
    assert r["count"] == 8 and r["cols"] == 8 and r["rows"] == 1
    assert all(t.size == (100, 100) for t in r["tiles"])
    assert r["preview"].size == (800, 100)


@test
async def two_rows_grid():
    r = B.build_tiles({"cols": 4, "rows": 2, "bg": [10, 10, 10],
                       "layers": [{"type": "text", "text": "제휴\n업체", "font": "round", "height": 0.4}]})
    assert r["count"] == 8 and r["preview"].size == (400, 200) and r["qa_error"] == 0.0


@test
async def make_tiles_rejects_wrong_size():
    bad = Image.new("RGBA", (750, 100))        # 8*100=800 이어야 하는데 750
    try:
        B.make_tiles(bad, 8, 1)
        raise AssertionError("크기 안 맞는데 통과함")
    except ValueError:
        pass


@test
async def clamps_grid():
    # cols>10 → 10, rows>3 → 3
    _, cols, rows = B.render_banner({"cols": 50, "rows": 9, "bg": "transparent"})
    assert cols == 10 and rows == 3


@test
async def transparent_background_keeps_alpha():
    r = B.build_tiles({"cols": 2, "rows": 1, "bg": "transparent",
                       "layers": [{"type": "text", "text": "AB", "font": "bold", "height": 0.6}]})
    import numpy as np
    # 투명 배경이면 어떤 타일엔 완전 투명(알파 0) 픽셀이 있어야 함
    assert any(np.asarray(t)[:, :, 3].min() == 0 for t in r["tiles"])
    assert r["qa_error"] == 0.0


@test
async def missing_glyph_falls_back_no_crash():
    # 이모지·특수문자는 글꼴에 없음 → 대체 글꼴로 (터지지 않음)
    r = B.build_tiles({"cols": 3, "rows": 1, "bg": [0, 0, 0],
                       "layers": [{"type": "text", "text": "★제휴★", "font": "round", "height": 0.6}]})
    assert r["count"] == 3 and r["qa_error"] == 0.0


@test
async def metal_ops_change_pixels():
    """베벨·반사밴드·안쪽그림자(ChatGPT 역설계)가 실제로 평평한 그라데이션과 다른 픽셀을 만들어야 함."""
    import numpy as np
    base = {"cols": 1, "rows": 1, "bg": "transparent"}
    gold = [[240, 240, 179], [242, 217, 23], [130, 113, 42]]
    flat = B.build_tiles({**base, "layers": [{"type": "text", "text": "보", "font": "bold", "height": 0.7, "colors": gold}]})
    metal = B.build_tiles({**base, "layers": [{"type": "text", "text": "보", "font": "bold", "height": 0.7, "colors": gold,
                                               "band": 0.5, "bevel": 0.7, "inner": 0.45}]})
    a = np.asarray(flat["tiles"][0], np.int16)
    b = np.asarray(metal["tiles"][0], np.int16)
    assert np.abs(a - b).mean() > 1.0, "금속 처리가 픽셀을 안 바꿈"
    assert metal["qa_error"] == 0.0


@test
async def shape_and_image_layers():
    logo = Image.new("RGBA", (80, 80), (255, 0, 0, 255))
    r = B.build_tiles({"cols": 4, "rows": 1, "bg": [20, 20, 20], "images": [logo],
                       "layers": [{"type": "shape", "kind": "round", "at": [0.5, 0.5], "wh": [0.8, 0.8], "fill": [40, 40, 60]},
                                  {"type": "image", "i": 0, "at": [0.12, 0.5], "scale": 0.7},
                                  {"type": "text", "text": "EVENT", "font": "bold", "at": [0.6, 0.5], "height": 0.5}]})
    assert r["count"] == 4 and r["qa_error"] == 0.0


if __name__ == "__main__":
    run_all()
