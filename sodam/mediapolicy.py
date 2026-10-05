"""🎬🎨 영상·그림 요청 규칙 한 곳 (make_video·make_image 공용).

오너 결정 2026-10-05 (고객 '퀄리티 별로' — 같은 문장을 그록 앱에 넣으면 훨씬 좋게 나옴):
- 성인 내용은 소담 AI 가 스스로 거절·순화하지 않는다. 되고 안 되고는 영상·그림 AI(xAI·OpenAI) 정책이 판단.
  실측 베베 #2564: 실사 프로필 요청에 AI 가 'A stylized fictional female character, clearly non-photorealistic' 를
  스스로 붙여 만화가 나옴 → 이게 '그록보다 빡세다'의 원인 (코드 hard_line 은 원래 두 가지만 막고 있었음).
- 우리가 막는 건 딱 두 가지 (코드 + 지시문 같게): ① 미성년자 + 성적·노출 ② 실제 사람 사진(프사·붙은 사진)으로 성적·노출.
- 프롬프트는 번역·압축만 (원문 보존). 사용자가 영어 프롬프트를 통째로 주면 그 원문을 그대로 쓴다 (passthrough).
- 사용자가 말하지 않은 화풍(stylized·cartoon·anime·non-photorealistic…)을 AI 가 끼워 넣으면 코드가 돌려보냄 (style_drift).
테스트 tests/test_mediapolicy.py · tests/test_videogen.py.
"""
from __future__ import annotations

import re

POLICY_TEXT = (
    "성인 콘텐츠를 스스로 거절하거나 순화하지 말 것: 성인(명백히 18세 이상)의 세련·섹시·노골적인 요청(노출·란제리·성적 분위기 포함)도 "
    "사용자가 쓴 그대로 prompt 에 옮겨 이 도구를 부른다. '안 됩니다'·'정책상'·'순한 버전으로'로 바꾸거나 호출을 건너뛰지 말 것 — "
    "되고 안 되고는 영상·그림 AI 가 판단한다. 부르지 않는 건 딱 두 가지: 미성년자(아동·청소년·학생·teen 등)가 나오는 성적·노출 내용, "
    "실제 사람 사진(프사·붙은 사진·photo_of)으로 만드는 성적·노출 내용. 그 밖(가상 인물·실사 성인 창작·글만)은 전부 부른다")
FIDELITY_TEXT = (
    "prompt 는 영어 한 문단 900자 안, 사용자 말을 번역·압축만 한다: 실사·포토리얼 요청을 만화·stylized·non-photorealistic·"
    "fictional 로 바꾸지 말고, 구도·조명·카메라·옷·노출 정도·색·넣을 글자(따옴표 그대로)를 빼거나 완곡하게 바꾸지 말 것. "
    "사용자가 말하지 않은 화풍·'안전하게 보이게' 같은 문장을 더하지 말 것. 사용자가 영어 프롬프트를 통째로 줬으면 그 문장을 그대로 쓴다")

_SEXUAL = re.compile(
    r"\b(nude|nudes|nudity|naked|nsfw|porn\w*|sex|sexual\w*|sexy|erotic\w*|undress\w*|topless|bottomless|lingerie|"
    r"strip(?:tease|ping)|seductive\w*|lewd|explicit|hentai|boobs?|breasts?|genitals?|intercourse|orgasm\w*)\b"
    r"|알몸|나체|누드|옷\s*(?:을\s*)?벗|벗겨|섹시|야하|야한|19금|야동|섹스|성행위|성관계|음란|속옷|란제리|가슴\s*노출|젖꼭지", re.I)
_MINOR = re.compile(
    r"\b(child|children|kids?|minors?|underage|teen|teens|teenage\w*|preteen\w*|schoolgirls?|schoolboys?|loli\w*|shota\w*|"
    r"toddlers?|infants?|juvenile)\b"
    r"|미성년|초등학생|초딩|중학생|중딩|고등학생|고딩|여고생|남고생|여중생|남중생|어린이|아동|유아|꼬마|로리|쇼타|소녀|소년", re.I)
REFUSE_MINOR = "미성년자가 나오는 성적인 내용은 못 만듦. 다른 내용이면 된다고 한마디만."
REFUSE_REAL = ("실제 사람 사진(프사·붙은 사진)으로는 성적·노출 내용을 못 만듦 (그 사람 동의를 확인할 수 없음). "
               "사진 없이 글로만(가상 인물) 만드는 건 된다고 한마디만.")


def hard_line(text: str, real_photo: bool) -> str | None:
    """우리가 막는 두 가지만. 나머지는 영상·그림 AI 정책에 맡김."""
    if not _SEXUAL.search(text or ""):
        return None
    if _MINOR.search(text):
        return REFUSE_MINOR
    if real_photo:
        return REFUSE_REAL
    return None


# ── 원문 보존 ──────────────────────────────────────────────
_STYLE = re.compile(r"\b(non[- ]?photo[- ]?realistic|stylized|stylised|cartoon\w*|anime|manga|illustrat\w*|"
                    r"fictional|animated|3d[- ]?render\w*|cgi|chibi|comic\w*|drawing|painted|painting)\b", re.I)
_STYLE_KO = {"non-photorealistic": "실사 아님", "stylized": "그림체", "cartoon": "만화", "anime": "애니", "manga": "만화",
             "illustration": "일러스트", "fictional": "가상", "animated": "애니", "drawing": "그림", "painting": "그림",
             "cgi": "CG", "3d render": "3D", "chibi": "치비", "comic": "만화", "painted": "그림"}
# 사용자가 화풍을 직접 말했으면 (한국어 포함) 그건 끼워 넣은 게 아님
_STYLE_ASKED = re.compile(r"만화|애니|일러스트|그림체|그림\s?(?:처럼|같이|으로)|웹툰|캐릭터|치비|3d|cg|게임\s?그래픽|수채화|유화|"
                          r"픽사|디즈니|지브리|카툰|드로잉|스케치", re.I)


def style_drift(request: str, prompt: str) -> list[str]:
    """사용자가 말하지 않았는데 prompt 에 들어간 화풍 낱말 (없으면 빈 목록)."""
    req = request or ""
    if _STYLE_ASKED.search(req):
        return []
    asked = {m.group(0).lower() for m in _STYLE.finditer(req)}
    return sorted({m.group(0).lower() for m in _STYLE.finditer(prompt or "")} - asked)


def drift_back(words: list[str]) -> str:
    return (f"prompt 에 사용자가 말하지 않은 화풍({', '.join(words)})을 넣었음 — 실사 요청이 만화·그림이 됨. "
            "그 낱말을 빼고 사용자 말을 번역·압축만 해서 이 도구를 다시 부를 것 (사용자에게 묻지 말고 바로).")


PASS_MIN = 250      # 이 이상 영어 글자가 이어지면 '통째로 준 영어 프롬프트'
PASS_MAX = 2000


def source_text(ctx) -> str:
    """원문 보존·화풍 검사에 쓸 사용자 글: 요청 + (요청이 짧으면) 답장한 글 ('이걸로 영상 만들어줘')."""
    req = str(getattr(ctx, "request_text", "") or getattr(getattr(ctx, "request_msg", None), "text", "") or "")
    rep = str(getattr(ctx, "reply_text", "") or "")
    return f"{req}\n{rep}" if rep and len(req) < 120 else req


def passthrough(request: str) -> str | None:
    """요청 글 안에 사용자가 직접 쓴 영어 프롬프트가 있으면 그 원문 (한국어 줄·'소담아 영상 만들어줘' 같은 부탁 줄은 뺌)."""
    lines = []
    for line in (request or "").splitlines():
        s = line.strip()
        if not s:
            if lines:
                lines.append("")
            continue
        latin = len(re.findall(r"[A-Za-z]", s))
        hangul = len(re.findall(r"[가-힣]", s))
        if latin >= max(3, hangul * 2):
            lines.append(s)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(re.findall(r"[A-Za-z]", text)) < PASS_MIN:
        return None
    return text[:PASS_MAX]
