"""글자·도형 레이어 (sodam/stickerforge/layout.py) — 위치·여러 줄·글꼴·말풍선·배치·정지 스티커 한 장 렌더.
python tests/run_all.py sticker_layout"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import runner  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from sodam import stickerforge as SF  # noqa: E402
from sodam.stickerforge import engine, layout as L  # noqa: E402

test, run_all = runner()


def mascot():
    im = Image.new("RGB", (400, 400), "white")
    ImageDraw.Draw(im).ellipse((100, 80, 300, 320), fill=(240, 120, 40))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def frame_of(spec):
    s, err = SF.sanitize(spec)
    assert not err, err
    res = SF.render_static(mascot(), s)
    assert res.ok, res.summary()
    return Image.open(io.BytesIO(res.preview)).convert("RGBA"), s


@test
def sanitize_text_and_shape_values():
    s, err = SF.sanitize({"layers": [{"type": "text", "text": "포인트\\n지급   완료\n셋\n넷\n다섯", "font": "cute", "size": 999,
                                      "at": [2, -1], "colors": [[255, 0, 0], "#00ff00"], "enter": "warp"},
                                     {"type": "shape", "kind": "bubble", "wh": [3, 0.01], "tail": [0.4, 0.5]}]})
    assert not err, err
    t, sh = s["layers"]
    assert t["text"] == "포인트\n지급 완료\n셋\n넷" and t["font"] == "cute" and t["size"] == 420 and t["at"] == (1, 0)
    assert t["colors"] == [(255, 0, 0), (0, 255, 0)] and "enter" not in t          # 없는 선택지는 버림(기본값)
    assert sh["wh"] == (1, 0.05) and sh["tail"] == (0.4, 0.5)
    assert "글꼴에 없는" in SF.sanitize({"layers": [{"type": "text", "text": "안녕😀"}]})[1]
    s, _ = SF.sanitize({"layers": [{"type": "text", "text": "똠양꿍", "font": "cute"}]})
    assert s["layers"][0]["font"] != "cute" and not L.missing_glyphs("똠양꿍", s["layers"][0]["font"])   # 없는 글자면 다른 글꼴로
    assert "글자" in SF.sanitize({"layers": [{"type": "text", "text": "  "}]})[1]


@test
def light_layers_counted_separately():
    heavy = [{"type": "grade", "contrast": 1.1}] * SF.MAX_LAYERS
    texts = [{"type": "text", "text": f"줄{i}"} for i in range(3)]
    assert not SF.sanitize({"layers": heavy + texts})[1]                              # 무거운 6 + 글자 3 = 됨
    assert SF.sanitize({"layers": heavy + [{"type": "grade"}]})[1]                    # 무거운 7 = 안 됨
    assert SF.sanitize({"layers": [{"type": "text", "text": "가"}] * (SF.MAX_LIGHT + 1)})[1]


@test
def text_lands_where_asked_and_shrinks_to_width():
    img = L.render_text("아주 아주 긴 문장을 한 줄로 넣어 보기", "bold", 120, 0.5)
    assert img.width <= 0.5 * 512 + 40, img.size                                     # 폭에 맞게 줄어듦 (테두리 여유)
    two = L.render_text("한 줄\n두 줄", "bold", 60)
    one = L.render_text("한 줄", "bold", 60)
    assert two.height > one.height * 1.6
    f, _ = frame_of({"mode": "cutout", "layers": [{"type": "text", "text": "가나다", "at": [0.5, 0.1], "size": 50,
                                                   "color": [0, 200, 0], "stroke": 0, "enter": "none"}]})
    r, g, b, a = f.getpixel((256, 51))
    top = [f.getpixel((x, y)) for x in range(200, 312) for y in range(30, 75)]
    assert any(p[1] > 150 and p[0] < 80 and p[3] > 200 for p in top), "초록 글자가 위쪽(at y=0.1)에"
    bottom = [f.getpixel((x, y)) for x in range(200, 312) for y in range(440, 500)]
    assert not any(p[1] > 150 and p[0] < 80 and p[3] > 200 for p in bottom)


@test
def bubble_then_text_order_and_subject_placement():
    spec = {"mode": "cutout", "motion": [{"type": "keyframes", "keys": [{"t": 0, "scale": 0.6, "x": -0.2, "y": 0.15}]}],
            "layers": [{"type": "shape", "kind": "bubble", "at": [0.7, 0.25], "wh": [0.5, 0.3], "fill": [255, 255, 0],
                        "tail": [0.45, 0.5], "enter": "none"},
                       {"type": "text", "text": "가", "at": [0.7, 0.25], "size": 70, "color": [0, 0, 255], "stroke": 0, "enter": "none"}]}
    f, _ = frame_of(spec)
    box = [f.getpixel((x, y)) for x in range(340, 380) for y in range(110, 150)]
    assert any(p[2] > 200 and p[0] < 80 for p in box), "말풍선 위에 글자 (나중에 그린 게 위)"
    assert f.getpixel((int(0.7 * 512) - 110, int(0.25 * 512)))[:3] == (255, 255, 0)  # 말풍선 노랑
    orange = [(x, y) for x in range(0, 512, 4) for y in range(0, 512, 4) if f.getpixel((x, y))[0] > 200 and 90 < f.getpixel((x, y))[1] < 150]
    cx = sum(x for x, _ in orange) / len(orange)
    assert cx < 220, cx                                                              # 피사체가 왼쪽으로 (keyframes 한 점)


@test
def static_renders_one_frame_only():
    calls = []
    real = engine.build

    def spy(*a, **k):
        calls.append(k.get("last_only"))
        return real(*a, **k)
    engine.build = spy
    try:
        frame_of({"mode": "cutout", "layers": [{"type": "text", "text": "정지", "enter": "pop"}]})
    finally:
        engine.build = real
    assert calls == [True]


@test
def every_font_has_hangul():
    for name in L.FONTS:
        assert not L.missing_glyphs("포인트 지급완료 출근 방가 ABC 123!?", name), name


@test
def dark_caption_colors_are_not_broken_glyphs():
    """실측 2026-10-07 루피 '출근완료': 남색 그라데이션 글자 → 글자 색을 '먹힌 자리'로 세서 glyphs 2.48% 로 거절됨."""
    from sodam.stickerforge import caption as C
    style = C.Style(top=(245, 247, 252), mid=(175, 184, 204), bottom=(70, 84, 112), extrude=(25, 36, 57), stroke=6, depth=10,
                    size_max=78, anims=("bounce", "punch", "shine"))
    assert C.glyph_check("출근완료", SF.FONT, style)["ok"]
    s, err = SF.sanitize({"mode": "cutout", "caption": {"text": "출근완료", "top": [245, 247, 252], "mid": [175, 184, 204],
                                                       "bottom": [70, 84, 112], "extrude": [25, 36, 57], "stroke": 6, "depth": 10,
                                                       "size_max": 78}})
    res = SF.render_static(mascot(), s)
    assert res.ok, res.summary()


if __name__ == "__main__":
    run_all()
