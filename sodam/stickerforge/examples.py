"""요청 → spec 예시 (카탈로그가 AI 에게 보여 주는 '이렇게 조합한다' 견본).

**정답 목록이 아니라 조합하는 법의 예**다 — AI 는 사용자 말의 낱말(무엇이·어디서·어느 쪽으로·어떤 색·빠르기)을 그대로 부품 값으로
옮기고, 예시를 그대로 베끼지 않는다. 실제 방 요청(2026-09-27~30, 17건 중 8건이 같은 '줌+네온+별빛')에서 실패했던 말을 넣었다.
tests/test_animation.py 가 예시마다 sanitize 통과 + 진짜 렌더를 검사한다 (부품이 바뀌면 여기가 먼저 깨짐).
"""
from __future__ import annotations

# make_sticker·make_profile_video 공통 규칙 (짧게 — 부품·값·예시는 sticker_catalog 결과로만: 도구 설명이 길면 AI 가 안 읽고 옛 목록만 골랐음)
COMPOSE = (
    "spec = 부품 조합: 먼저 sticker_catalog(요청 원문)로 부품·값 범위·예시를 보고, 사용자 말의 낱말(무엇이·어디서·어느 쪽으로·색·빠르기·개수)을 "
    "motion(keyframes 또는 이름)·layers(particles·grade·flash·lightning·transition·효과 이름, 순서=그리는 순서, 각 start·end)로 옮긴다. "
    "말에 없는 효과를 습관처럼 넣지 말고, 같은 사람에게 직전과 같은 조합 금지, seed 는 매번 새로. "
    "경고(warnings)가 오면 말한 것 하나만 고쳐 한 번 더(두 번째는 accept_warnings). 정말 못 하는 것(사진 속 사람 팔다리·표정이 실제로 "
    "움직이기 = 생성형 영상 모델 필요)은 가장 가까운 조합 + wanted 에 원래 말 + 한계 한마디. request 에 요청 원문."
)

EXAMPLES = [
    {"request": "수건으로 쌍절곤 휘둘러",
     "why": "빠른 회전 keyframes(손잡이=pivot 아래쪽) + 튀는 불티(바람 가르는 선 speedlines 는 cutout 에서만 보임 — 사진이면 뒤에 가려짐), 사진이면 cover(돌아도 검은 모서리 없게). 사진 속 팔이 따로 움직이진 않음(전체가 휘둘림)",
     "spec": {"cover": True, "motion": [{"type": "keyframes", "pivot": [0.5, 1.0], "keys": [
         {"t": 0, "rotate": -16, "ease": "in_out"}, {"t": 0.125, "rotate": 16, "ease": "in_out"}, {"t": 0.25, "rotate": -16, "ease": "in_out"},
         {"t": 0.375, "rotate": 16, "ease": "in_out"}, {"t": 0.5, "rotate": -16, "ease": "in_out"}, {"t": 0.625, "rotate": 16, "ease": "in_out"},
         {"t": 0.75, "rotate": -16, "ease": "in_out"}, {"t": 0.875, "rotate": 16, "ease": "in_out"}, {"t": 1, "rotate": -16}]}],
              "layers": [{"type": "grade", "contrast": 1.12},
                         {"type": "particles", "shape": "spark", "spawn": "around", "count": 20, "speed": 220, "angle": 0,
                          "angle_spread": 180, "life": 0.4, "size": [6, 12], "blend": "add"}]}},
    {"request": "상품권이 다 타서 없어지게",
     "why": "움프는 loop=false(6초 한 번). transition burn(가장자리 불 + 그을림 + 불씨) + 연기 입자 + 따뜻한 색",
     "spec": {"loop": False, "motion": [{"type": "keyframes", "keys": [{"t": 0, "scale": 1.0}, {"t": 1, "scale": 1.06}]}],
              "layers": [{"type": "grade", "tint": [255, 150, 60], "tint_amount": 0.12, "contrast": 1.1},
                         {"type": "transition", "kind": "burn", "origin": "bottom", "start": 0.12, "end": 0.8, "edge": 0.05, "embers": 50},
                         {"type": "particles", "shape": "smoke", "spawn": "bottom", "angle": -90, "speed": 60, "count": 20,
                          "size": [40, 80], "grow": 2.2, "blur": 4, "opacity": 0.5, "turbulence": 18, "start": 0.2, "end": 0.9}]}},
    {"request": "담배 피는 곰, 연기 나오게",
     "why": "연기 = smoke 입자: 담배 끝(사진을 보고 at 좌표)에서 위로(-90) 천천히, 커지며(grow) 흐려지고 사라짐 + 끝에 작은 불티",
     "spec": {"motion": [{"type": "breathe"}],
              "layers": [{"type": "particles", "shape": "smoke", "spawn": "point", "at": [0.63, 0.55], "spread": 0.01, "angle": -95,
                          "angle_spread": 12, "speed": 45, "count": 24, "size": [16, 30], "grow": 3.2, "life": 2.6, "blur": 3,
                          "opacity": 0.55, "turbulence": 14, "wind": 10, "fade": "both"},
                         {"type": "particles", "shape": "spark", "spawn": "point", "at": [0.63, 0.56], "spread": 0.005, "count": 6,
                          "speed": 15, "angle": -90, "size": [5, 8], "life": 0.6, "blend": "add"}]}},
    {"request": "더 화려하고 멋지게",
     "why": "화려 = 입자 많이 + 빛(bloom·rays) + 강한 움직임. 직전과 다른 모양·색·움직임으로 (같은 조합 반복 금지), seed 도 새로",
     "spec": {"motion": [{"type": "punch", "hits": 2, "amount": 0.08}],
              "layers": [{"type": "rays", "strength": 0.25},
                         {"type": "particles", "shape": "confetti", "spawn": "top", "angle": 90, "count": 60, "speed": 160,
                          "spin": 360, "colors": [[255, 80, 110], [80, 200, 255], [255, 220, 60], [140, 255, 150]], "size": [10, 18]},
                         {"type": "particles", "shape": "sparkle", "spawn": "around", "count": 18, "speed": 20, "life": 0.8,
                          "size": [18, 34], "blend": "add"},
                         {"type": "grade", "saturation": 1.35, "contrast": 1.12, "bloom": 0.5}]}},
    {"request": "잔잔하게",
     "why": "잔잔 = 작은 움직임(zoom·breathe) + 적은 입자 천천히 + 부드러운 색. 요란한 fx 없이",
     "spec": {"motion": [{"type": "zoom", "amount": 0.04}],
              "layers": [{"type": "particles", "shape": "petal", "spawn": "top", "angle": 100, "speed": 45, "count": 10,
                          "size": [12, 20], "spin": 90, "turbulence": 16, "opacity": 0.8},
                         {"type": "grade", "tint": [255, 210, 180], "tint_amount": 0.08, "bloom": 0.2, "vignette": 0.25}]}},
    {"request": "글리치 느낌",
     "why": "글리치 = 잠깐 R/B 어긋남(glitch) + 띠 찢김(slice_glitch) + 스캔라인 + 짧은 번쩍. 색이 계속 도는 건 글리치가 아님",
     "spec": {"motion": [{"type": "shake", "amp": 3, "freq": 8}],
              "layers": [{"type": "glitch", "amp": 7, "bursts": 3}, {"type": "slice_glitch", "bursts": 2},
                         {"type": "scanlines", "dark": 0.25}, {"type": "flash", "count": 2, "strength": 0.3, "decay": 0.03},
                         {"type": "grade", "contrast": 1.2, "saturation": 1.2}]}},
    {"request": "네온",
     "why": "네온 = 대비·채도 + 분홍/하늘 틴트 + 블룸(빛 번짐) + add 불티. 채도만 올리는 건 '별로' 였음",
     "spec": {"motion": [{"type": "zoom", "amount": 0.05}],
              "layers": [{"type": "grade", "contrast": 1.2, "saturation": 1.4, "tint": [255, 60, 200], "tint_amount": [0.05, 0.2],
                          "bloom": 0.7, "cycles": 2},
                         {"type": "particles", "shape": "spark", "spawn": "edges", "count": 60, "speed": 70, "life": 1.4,
                          "colors": [[255, 60, 200], [60, 220, 255]], "size": [12, 26], "blend": "add"},
                         {"type": "scanlines", "dark": 0.15}]}},
    {"request": "번개 치면서 등장",
     "why": "번개 = lightning(줄기+번쩍), 등장 = keyframes 작게→크게(back) + transition in",
     "spec": {"motion": [{"type": "keyframes", "keys": [{"t": 0, "scale": 0.6, "opacity": 0, "ease": "back"}, {"t": 0.25, "scale": 1.0, "opacity": 1},
                                                        {"t": 0.85, "scale": 1.0}, {"t": 1, "scale": 0.6, "opacity": 0}]}],
              "layers": [{"type": "lightning", "count": 2, "origin": [0.3, 0.0], "target": [0.5, 0.55]},
                         {"type": "grade", "brightness": -0.05, "tint": [120, 110, 255], "tint_amount": 0.12}]}},
    {"request": "사진 속 인물이 움직이면 (팔 흔들기·걷기)",
     "why": "사진 속 사람의 팔다리·표정이 실제로 움직이는 건 생성형 영상 모델이 필요 → 여기선 못 함. 사진 전체 움직임(끄덕·흔들)으로 "
            "가장 가깝게 만들고, 한계를 한마디 + wanted 에 원래 말",
     "wanted": "사진 속 인물이 실제로 움직이기",
     "spec": {"motion": [{"type": "nod", "amp": 4, "cycles": 2}],
              "layers": [{"type": "particles", "shape": "sparkle", "spawn": "around", "count": 10, "life": 0.9, "speed": 10,
                          "size": [14, 26], "blend": "add"}]}},
]
