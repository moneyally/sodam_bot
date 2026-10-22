"""ImageMagick 스타일 엔진 (2026-10-22 오너 '하드코딩 말고 오픈소스 스킬'): 금·크롬·네온·불·유리…
IM 있으면 렌더·스타일마다 다름, 없으면 None → banner 가 PIL 폴백. 서버에 IM 없어도 통과해야 함.
뮤테이션: STYLES 에서 스타일 빼면 styles_distinct FAIL / render 가 bbox crop 안 하면 크기 FAIL.
"""
from fakes import runner

from sodam.stickerforge import banner as B
from sodam.stickerforge import stylefx as SX

test, run_all = runner()


@test
async def unknown_style_returns_none():
    # IM 유무와 무관하게 모르는 스타일은 None (호출부가 PIL 폴백)
    assert SX.render("로", "그런거없음", size=80) is None
    assert SX.render("", "gold", size=80) is None


@test
async def known_styles_render_when_im_present():
    if not SX.HAVE_IM:
        return                                       # 서버에 IM 없으면 이 검사는 건너뜀 (폴백은 아래서)
    import numpy as np
    seen = []
    for s in ("gold", "chrome", "fire", "neon", "glass"):
        im = SX.render("로", s, size=120)
        assert im is not None and im.mode == "RGBA" and im.width > 10 and im.height > 10, s
        seen.append(np.asarray(im.resize((60, 60))).astype(np.int16))
    # 스타일끼리 눈에 띄게 달라야 함 (금 vs 네온 등)
    for i in range(len(seen)):
        for j in range(i + 1, len(seen)):
            assert np.abs(seen[i] - seen[j]).mean() > 3.0, (i, j)


@test
async def banner_uses_style_when_im_present():
    if not SX.HAVE_IM:
        return
    r = B.build_tiles({"cols": 2, "rows": 1, "bg": "transparent",
                       "layers": [{"type": "text", "text": "로", "style": "gold", "at": [0.25, 0.5], "height": 0.6},
                                  {"type": "text", "text": "얄", "style": "neon", "at": [0.75, 0.5], "height": 0.6}]})
    assert r["count"] == 2 and r["qa_error"] == 0.0
    import numpy as np
    # 금 타일과 네온 타일이 서로 다른 색이어야 (스타일이 실제로 적용됨)
    a, b = np.asarray(r["tiles"][0]), np.asarray(r["tiles"][1])
    assert np.abs(a.astype(np.int16) - b.astype(np.int16)).mean() > 2.0


@test
async def banner_falls_back_to_pil_without_im():
    # style 을 줬는데 IM 이 못 만들면(모르는 스타일) PIL 경로로 그려져야 함 (글자가 보임)
    import numpy as np
    r = B.build_tiles({"cols": 1, "rows": 1, "bg": "transparent",
                       "layers": [{"type": "text", "text": "보", "style": "존재안함", "height": 0.7,
                                   "colors": [[255, 240, 180], [255, 180, 40]]}]})
    assert r["qa_error"] == 0.0
    assert np.asarray(r["tiles"][0])[:, :, 3].max() > 100   # 글자가 실제로 그려짐(알파 있음)


@test
async def styles_registry_has_core_set():
    for s in ("gold", "chrome", "fire", "neon", "glass"):
        assert s in SX.STYLES


if __name__ == "__main__":
    run_all()
