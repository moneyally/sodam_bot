"""검증된 조합(레시피) 데이터 + 변주.

소담이가 매번 비슷한 디자인을 내지 않게, 고객이 승인했던 조합을 계열(family)별로 두고
`pick` 으로 요청·원본 종류에 맞는 것을 고른 뒤 `vary(seed)` 로 파라미터를 살짝 흔든다.
같은 요청이라도 seed 가 다르면 다른 사람이 손으로 만든 것처럼 조금씩 다르다.

계열: calm(잔잔) · beat(강조·펀치) · bounce(점프·젤리) · impact(쿵·반동·잽) · float(둥둥) · photo(사진 카메라)
원본 종류(kinds): cutout(단색 배경 캐릭터) · photo(실사·꽉 찬 그림) · glow(검은 배경 발광) · mono(흑백 고대비)
"""
from __future__ import annotations

import random

FAMILY = {"idle": "calm", "breathe": "calm", "float": "float", "punch": "beat", "shake": "beat", "wobble": "bounce",
          "hop": "bounce", "pop": "bounce", "slam": "impact", "recoil": "impact", "jab": "impact",
          "zoom": "photo", "pan": "photo", "still": "calm"}

# 파라미터 변주 폭: (키, 최소, 최대). 없는 키는 그대로.
JITTER = {"amp_rot": (0.6, 3.0), "amp_bob": (4, 16), "amount": (0.04, 0.10), "hits": (2, 3), "amp": (4, 8), "freq": (3, 6),
          "cycles": (1, 2), "jumps": (1, 2), "height": (80, 120), "strength": (0.3, 0.55), "width": (3, 6),
          "bursts": (2, 3), "sway": (4, 8), "lunge": (0.08, 0.13), "kick": (18, 30)}
PALETTES = ["gold", "silver", "ice", "fire", "pink", "neon", "white"]
SUBSETS = [[0, 2, 4, 6, 9], [1, 4, 6, 8], [0, 3, 5, 8], [1, 3, 7], [0, 2, 5, 9], [2, 5, 9]]


def R(name, moods, kinds, motion, fx, palette="gold", margin=0.10):
    return {"name": name, "moods": moods, "kinds": kinds, "motion": motion, "fx": fx, "palette": palette, "margin": margin}


RECIPES = [
    # ── 잔잔 (calm)
    R("인사_잔잔", ["안녕", "인사", "잔잔", "기본"], ["cutout"], [{"type": "idle", "amp_rot": 1.5, "amp_bob": 8}],
      [{"type": "glitch", "amp": 6, "bursts": 2}, {"type": "sparkle", "subset": [1, 4, 6, 8]}]),
    R("잘자요_별똥별", ["잘자", "굿나잇", "밤", "잔잔", "편안"], ["cutout", "photo"], [{"type": "breathe"}],
      [{"type": "meteors"}, {"type": "zzz", "origin": [300, 118]}, {"type": "sparkle", "subset": [1, 3, 7]}], "ice", 0.06),
    R("감사_은은", ["감사", "고마워", "고급", "잔잔"], ["cutout", "photo"], [{"type": "idle", "amp_rot": 1.0, "amp_bob": 6}],
      [{"type": "sweep", "strength": 0.45}, {"type": "sparkle", "subset": [0, 2, 5, 9]}], "gold", 0.08),
    R("네온_잔잔", ["네온", "은은", "고급", "밤"], ["cutout", "mono"], [{"type": "breathe"}],
      [{"type": "glow", "color": [150, 120, 255], "radius": 12, "strength": 0.6}, {"type": "sparkle", "subset": [2, 5, 9]}], "neon", 0.08),
    R("로고_정지광채", ["로고", "아이콘", "심플", "정지"], ["cutout", "photo"], [{"type": "still"}],
      [{"type": "sweep", "strength": 0.5}], "silver", 0.06),
    # ── 둥둥 (float)
    R("우주_부유", ["우주", "둥둥", "꿈", "몽환", "출근"], ["cutout"], [{"type": "float"}, {"type": "punch", "hits": 2, "amount": 0.04}],
      [{"type": "sweep", "strength": 0.5}, {"type": "sparkle", "subset": [0, 2, 4, 6, 9]}], "ice", 0.09),
    R("하트_둥둥", ["사랑", "하트", "좋아", "둥둥"], ["cutout", "photo"], [{"type": "float", "amp_bob": 12}],
      [{"type": "hearts"}, {"type": "glow", "color": [255, 120, 170], "strength": 0.5}], "pink", 0.10),
    # ── 강조 (beat)
    R("전송완료_스캔", ["전송", "완료", "보냄", "입금", "송금"], ["cutout"], [{"type": "punch", "hits": 3, "amount": 0.07}],
      [{"type": "scan"}, {"type": "sparkle", "subset": [0, 2, 5, 9]}]),
    R("출근_글리치테두리", ["출근", "완료", "강렬", "컬러"], ["cutout"], [{"type": "punch", "hits": 3, "amount": 0.06}],
      [{"type": "outline", "width": 4}, {"type": "glitch", "amp": 6, "bursts": 2}, {"type": "sparkle", "subset": [1, 3, 5, 8]}]),
    R("엄지척_테두리", ["엄지", "최고", "굿", "좋아", "인정"], ["cutout"], [{"type": "punch", "hits": 3, "amount": 0.09}],
      [{"type": "outline", "color": [255, 255, 255], "width": 5}, {"type": "sparkle"}]),
    R("식사_빛살", ["식사", "밥", "먹", "챙겨", "화사"], ["cutout"], [{"type": "shake", "amp": 5, "freq": 4}],
      [{"type": "rays"}, {"type": "sparkle", "subset": [0, 4]}]),
    R("사랑_하트펀치", ["사랑", "하트", "대표님"], ["cutout", "photo"], [{"type": "punch", "hits": 2, "amount": 0.08}],
      [{"type": "hearts"}, {"type": "glow", "color": [255, 100, 160], "strength": 0.5}, {"type": "sparkle", "subset": [1, 3, 7]}], "pink"),
    R("흑백_슬라이스", ["흑백", "고대비", "강렬", "디지털", "글리치"], ["mono", "glow"], [{"type": "punch", "hits": 2, "amount": 0.05}],
      [{"type": "slice_glitch", "bursts": 3}, {"type": "sparkle", "subset": [0, 3, 5, 8]}], "silver"),
    R("덜덜_신남", ["신남", "덜덜", "흥분", "떨림", "두근"], ["cutout", "photo"], [{"type": "shake", "amp": 6, "freq": 6}],
      [{"type": "sparkle", "subset": [0, 2, 4, 6, 9]}, {"type": "glitch", "amp": 5, "bursts": 2}]),
    # ── 점프·젤리 (bounce)
    R("인사_젤리", ["안녕", "귀엽", "흔들", "젤리", "다르게"], ["cutout"], [{"type": "wobble", "amp": 8, "cycles": 2}],
      [{"type": "glitch", "amp": 6, "bursts": 2}, {"type": "sparkle", "subset": [1, 4, 6, 8]}], "gold", 0.12),
    R("식사_점프", ["식사", "신남", "점프", "화사", "귀엽"], ["cutout"], [{"type": "hop", "jumps": 2, "height": 100}],
      [{"type": "rays"}, {"type": "sparkle", "subset": [0, 4]}], "gold", 0.16),
    R("짠_등장", ["짠", "등장", "깜짝", "짜잔"], ["cutout"], [{"type": "pop"}],
      [{"type": "outline", "width": 5}, {"type": "sparkle"}], "gold", 0.12),
    R("귀엽_하트젤리", ["귀엽", "사랑", "하트", "젤리"], ["cutout"], [{"type": "wobble", "amp": 7, "cycles": 2}],
      [{"type": "hearts"}, {"type": "sparkle", "subset": [2, 5, 9]}], "pink", 0.12),
    # ── 임팩트 (impact)
    R("전송완료_슬램", ["전송", "완료", "쿵", "강렬", "임팩트"], ["cutout"], [{"type": "slam"}],
      [{"type": "scan"}], "gold", 0.16),
    R("출근_슬램글리치", ["출근", "완료", "쿵", "강렬"], ["cutout"], [{"type": "slam"}],
      [{"type": "outline", "width": 4}, {"type": "glitch", "amp": 6, "bursts": 2}, {"type": "sparkle", "subset": [1, 3, 5, 8]}], "gold", 0.16),
    R("출근_번개잽", ["출근", "완료", "엄지", "번개", "강렬", "네온"], ["glow", "mono"], [{"type": "jab"}],
      [{"type": "aura"}, {"type": "shockwave", "center": [256, 236]}, {"type": "bolts", "center": [256, 236], "color": [150, 120, 255]},
       {"type": "slice_glitch"}], "silver", 0.10),
    R("사기꾼_총격", ["사기", "박제", "총", "처단", "빵"], ["photo"],
      [{"type": "recoil", "times": [0.10, 0.62, 1.14, 1.90, 2.42], "kick": 26, "direction": -1}],
      [{"type": "flash", "at": [374, 213]}, {"type": "flashbang", "amount": 55}, {"type": "glitch", "amp": 9, "on_hits": True},
       {"type": "sparkle", "subset": [2, 5, 9]}], "fire", 0.04),
    R("펀치_충격파", ["펀치", "찌르", "때려", "잽", "강렬"], ["cutout", "glow"], [{"type": "jab", "lunge": 0.11}],
      [{"type": "shockwave"}, {"type": "flashbang", "amount": 40}, {"type": "sparkle", "subset": [0, 3, 5, 8]}], "fire", 0.10),
    # ── 사진 (photo)
    R("사진_감사", ["감사", "고마워", "대표님", "사진", "고급"], ["photo"], [{"type": "zoom", "amount": 0.06}],
      [{"type": "sweep", "strength": 0.42}, {"type": "sparkle"}], "gold", 0.06),
    R("사진_전송글리치", ["전송", "완료", "글리치", "디지털", "사진"], ["photo"], [{"type": "punch", "hits": 3, "amount": 0.06}],
      [{"type": "glitch", "amp": 7, "bursts": 3}, {"type": "sparkle", "subset": [0, 2, 4, 6, 9]}], "ice", 0.06),
    R("사진_강렬펀치", ["강렬", "네온", "두근", "움프", "프로필"], ["photo"], [{"type": "punch", "hits": 2, "amount": 0.08}],
      [{"type": "sweep", "strength": 0.35}, {"type": "sparkle", "subset": [0, 2, 4, 6, 9]}, {"type": "glitch", "amp": 6, "bursts": 3}], "neon", 0.06),
    R("사진_빛살등장", ["등장", "화사", "축하", "빛", "움프"], ["photo"], [{"type": "zoom", "amount": 0.08}],
      [{"type": "rays", "strength": 0.2}, {"type": "sweep", "strength": 0.4}, {"type": "sparkle", "subset": [1, 3, 7]}], "gold", 0.06),
    R("사진_잔잔밤", ["잘자", "밤", "잔잔", "사진", "움프"], ["photo"], [{"type": "zoom", "amount": 0.04}],
      [{"type": "meteors"}, {"type": "sparkle", "subset": [1, 3, 7]}], "ice", 0.06),
    R("사진_흔들네온", ["네온", "덜덜", "두근", "강렬", "움프"], ["photo", "mono"], [{"type": "shake", "amp": 5, "freq": 5}],
      [{"type": "glow", "color": [180, 120, 255], "strength": 0.5}, {"type": "slice_glitch", "bursts": 2}, {"type": "sparkle", "subset": [2, 5, 9]}], "neon", 0.06),
    R("사진_좌우드리프트", ["잔잔", "여행", "풍경", "사진", "고급"], ["photo"], [{"type": "pan", "amount": 40}],
      [{"type": "sweep", "strength": 0.35}, {"type": "sparkle", "subset": [1, 3, 7]}], "silver", 0.06),
]
BY_NAME = {r["name"]: r for r in RECIPES}


def family(recipe_or_spec: dict) -> str:
    m = (recipe_or_spec.get("motion") or [{"type": "idle"}])[0]
    return FAMILY.get(m["type"] if isinstance(m, dict) else m, "calm")


def pick(text: str, kind: str | None = None, exclude_family: str | None = None, seed: int | None = None) -> dict | None:
    """요청 글의 분위기 단어와 원본 종류로 가장 맞는 레시피. 동점이면 seed 로 하나."""
    words = text or ""
    best, top = [], 0
    for r in RECIPES:
        if kind and kind not in r["kinds"]:
            continue
        if exclude_family and family(r) == exclude_family:
            continue
        score = sum(2 if m in words else 0 for m in r["moods"])
        if score > top:
            best, top = [r], score
        elif score == top:
            best.append(r)
    if not best:
        return None
    return best[random.Random(seed).randrange(len(best))] if seed is not None else best[0]


def vary(recipe: dict, seed: int = 0) -> dict:
    """같은 계열 안에서 파라미터를 살짝 흔든 spec (sanitize 전). 같은 seed = 같은 결과."""
    rnd = random.Random(f"{recipe['name']}:{seed}")

    def jit(item: dict) -> dict:
        out = {}
        for k, v in item.items():
            if k in JITTER and isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = JITTER[k]
                span = (hi - lo) * 0.25
                nv = min(hi, max(lo, v + rnd.uniform(-span, span)))
                out[k] = int(round(nv)) if isinstance(v, int) else round(nv, 3)
            elif k == "subset":
                out[k] = rnd.choice(SUBSETS)
            else:
                out[k] = v
        return out

    spec = {"mode": "photo" if "photo" in recipe["kinds"] and "cutout" not in recipe["kinds"] else "cutout",
            "motion": [jit(m) for m in recipe["motion"]], "fx": [jit(f) for f in recipe["fx"]],
            "margin": round(min(0.18, max(0.04, recipe["margin"] + rnd.uniform(-0.01, 0.01))), 3),
            "seed": rnd.randrange(1, 1000), "palette": recipe["palette"]}
    if "glow" in recipe["kinds"] and "cutout" not in recipe["kinds"]:
        spec["keying"] = "glow"
    return spec
