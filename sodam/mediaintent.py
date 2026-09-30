"""🎞️ 움프 vs 🎬 AI 영상 의도 판정 (코드, 돈 0) — '움직이게 해줘' 가 복불복으로 움프/AI 영상이 되던 것 (2026-10-01 오너 지적).

실제 사례 (agent_runs): 벳블리 '원형테두리 없애주고 영상으로 움직이게 해줘'(움프 고치던 중) → AI 영상(주 한도 차감),
2분 뒤 '흰줄만 없애죠' → 움프. 일루왕 '움직이는영상프로필' → 움프 → 사람이 '움직이는프로필말고 영상을 제작해'.

- ump   = 프로필용 움직이는 사진 (make_profile_video: 효과 조합, 싸고 한도 넉넉)
- video = 장면·동작·소리가 있는 AI 영상 (make_video: 한 주 한도, 한 개 $0.3)
- ambiguous = '움직이게' 처럼 어느 쪽 말도 없음 → AI 영상 기능이 켜진 방이면 버튼으로 물어봄 (추측으로 주 한도 쓰지 않게)
- None = 움직이는 것과 무관한 요청
판정은 신호 목록(낱말 무리 + 부정 '말고') — 새 표현은 무리에 낱말을 더하면 됨. 도구가 이 판정을 보고 다른 쪽이면 돌려보냄.
"""
from __future__ import annotations

import re

# '~말고' 부정: 앞 낱말 쪽이 아님
_NOT_UMP = re.compile(r"(움프|프사|프로필|움직이는\s?프로필|움직이는\s?프사|효과)\s?(말고|아니고|아니라|대신)")
_NOT_VIDEO = re.compile(r"(AI\s?영상|동영상|영상\s?제작|진짜\s?영상|영상)\s?(말고|아니고|아니라|대신)")
# 움프 쪽 말: 프로필·반복 이미지·효과 이름
_UMP = re.compile(r"움프|프사|프로필|gif|GIF|움짤|테두리|원형|동그라미|아이콘|반복|루프|글리치|네온|반짝|스파클|줌\s?효과|"
                  r"효과|파티클|번개|불꽃\s?효과|스티커|🎞")
# AI 영상 쪽 말: 영상 만들기·장면·사람/사물의 실제 동작·소리·이야기
_VIDEO = re.compile(r"동영상|AI\s?영상|영상\s?(으로\s?)?(제작|만들|생성|하나|찍)|영상을\s?(제작|만들|생성)|장면|걸어|걷는|뛰어|달리|"
                    r"춤|말하|대사|목소리|소리|음악|노래하|스토리|이야기|영화|광고|뮤비|찍은\s?듯|담배\s?(피|피우|때우)|"
                    r"휘두르|싸우|날아|운전|웃으면서|손\s?흔|고개|눈\s?깜|🎬")
# 실제 동작 (움프 효과로는 못 하는 것 — 생성형 영상이 필요)
_ACTION = re.compile(r"걸어|걷는|뛰어|달리|춤|말하|대사|노래하|담배\s?(피|피우|때우)|싸우|날아|운전|손\s?흔|고개|눈\s?깜|웃으면서")
_MOVE = re.compile(r"움직|애니메이션|살아\s?있|움짤|영상")


def classify(request: str, reply_to: str | None = None) -> str | None:
    """요청 글(+답장 대상 글)로 ump / video / ambiguous / None. 선택 버튼으로 이어진 요청('(선택: 🎬 AI 영상) …')도 여기서."""
    text = " ".join((request or "").split())
    if not text:
        return None
    if _NOT_UMP.search(text):
        return "video"
    if _NOT_VIDEO.search(text):
        return "ump"
    ump, vid = bool(_UMP.search(text)), bool(_VIDEO.search(text))
    if ump and not vid:
        return "ump"
    if vid and not ump:
        return "video"
    if ump and vid:
        # 둘 다: 사람·사물의 실제 동작(춤·걷기·말하기…)이 있으면 AI 영상 ('내 프사로 춤추는 영상' = 프사가 원본인 영상),
        # 아니면 '프로필/프사/움프' 가 있을 때 프로필용 ('움직이는 영상 프로필'), 그 밖엔 영상
        if _ACTION.search(text):
            return "video"
        return "ump" if re.search(r"움프|프사|프로필|gif|GIF|움짤", text) else "video"
    if _MOVE.search(text):
        ctx = " ".join((reply_to or "").split())
        if ctx and _UMP.search(ctx) and not _VIDEO.search(ctx):
            return "ump"
        return "ambiguous"
    return None


CHOICE_UMP = "🎞️ 움프 (프사용 움직이는 사진)"
CHOICE_VIDEO = "🎬 AI 영상 (장면·동작, 주 한도 차감)"


def redirect(intent: str | None, tool: str) -> str | None:
    """도구가 판정과 다를 때 모델에게 돌려줄 결과 (None = 그대로 실행)."""
    if tool == "make_video":
        if intent == "ump":
            return ("이 요청은 프로필용 움직이는 사진(움프)이다 → make_video 말고 make_profile_video 로 만든다 "
                    "(AI 영상은 한 주 한도를 쓰므로 영상이라고 분명히 말할 때만).")
        if intent == "ambiguous":
            return (f"움프인지 AI 영상인지 요청만으로는 모름 → 만들지 말고 ask_choice 로 물어본다: question '어떤 걸로 만들까요?', "
                    f"options ['{CHOICE_UMP}', '{CHOICE_VIDEO}'].")
    if tool == "make_profile_video" and intent == "video":
        return ("이 요청은 장면·동작이 있는 AI 영상이다 → make_profile_video 말고 make_video 로 만든다 "
                "(make_video 가 목록에 없으면 AI 영상은 지금 안 된다고 하고 움프로 할지 물어본다).")
    return None
