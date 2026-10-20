"""인라인 버튼 색(Bot API 9.4 style) 자동 입히기 — 한 곳에서 전 버튼.

텔레그램 2026-02-09(Bot API 9.4)부터 InlineKeyboardButton 에 style 이 생김:
"" (기본·회색) / primary (파랑) / success (초록) / danger (빨강). 그 이전 클라이언트는 색 없이 보임.
소담은 버튼을 88개 파일에서 만들어서, 화면마다 색을 안 박고 **InlineKeyboardMarkup 이 만들어질 때**
버튼 글자·콜백 접두어를 보고 뜻에 맞는 색을 자동으로 채운다 (이미 색이 지정된 버튼은 그대로 둠).

- 🟢 success: 결제·구독·확인·실행·시작·지급·예약·저장·켜기·✅ 등 '하는' 버튼
- 🔴 danger: 밴·삭제·취소·차단·해제·내보내기·끄기·🚫·🗑·🙅 등 '없애는/위험' 버튼
- 🔵 primary: 조회·보기·다음·새로고침·🔎·📄 등 '넘어가는/보는' 버튼
- ⚪ 기본: 뒤로·닫기·나머지

install() 이 InlineKeyboardMarkup.__init__ 을 감싸 전역 적용. 되돌릴 땐 uninstall().
방 설정·테마(composer)로 명시한 색은 button.style 이 이미 있으니 건드리지 않는다.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"
VALID = {PRIMARY, SUCCESS, DANGER, ""}

# 글자/이모지로 판정 (대소문자 무시, 아래부터 위 순서로 우선 — danger 가 제일 세다).
# 값은 '이 낱말/이모지가 버튼 글자에 있으면 이 색'. 짧은 데이터 테이블 하나.
DANGER_WORDS = ("밴", "차단", "삭제", "취소", "해제", "내보내", "강퇴", "추방", "끄기", "거절", "거부",
                "그만", "중지", "정지", "폐기", "없애", "초기화", "리셋", "🚫", "🗑", "🙅", "⛔", "❌", "🔴", "🔒")
SUCCESS_WORDS = ("결제", "구독", "확인", "실행", "시작", "지급", "예약", "저장", "켜기", "등록", "승인",
                 "지급 완료", "완료", "추가", "받기", "적용", "연장", "충전", "보내기", "집합", "소집",
                 "✅", "💳", "🟢", "▶️", "➕", "🎉", "📣")
PRIMARY_WORDS = ("조회", "보기", "다음", "새로고침", "자세히", "상세", "미리보기", "열기", "찾기", "검색",
                 "원글", "목록", "불러오기", "최근", "픽", "🔎", "📄", "🔍", "🔵", "📊", "📈", "🔄", "➡️", "📋")

# 콜백 접두어(data.split(':')[0])로도 보강 — 제재/삭제 계열은 danger.
DANGER_PREFIX = ("fbx", "anmx", "um", "cas", "rngx", "rkp")  # 밴/무시/제재 계열(일부 성공 겹침은 글자 우선)


def _by_words(text: str) -> str | None:
    t = (text or "").lower()
    for w in DANGER_WORDS:
        if w.lower() in t:
            return DANGER
    for w in SUCCESS_WORDS:
        if w.lower() in t:
            return SUCCESS
    for w in PRIMARY_WORDS:
        if w.lower() in t:
            return PRIMARY
    return None


def _by_prefix(data: str | None) -> str | None:
    if not data:
        return None
    pre = data.split(":", 1)[0]
    if pre in DANGER_PREFIX:
        return DANGER
    return None


def style_for(btn: InlineKeyboardButton) -> str | None:
    """글자 우선, 없으면 콜백 접두어. 모르면 None(기본 회색)."""
    st = _by_words(getattr(btn, "text", "") or "")
    if st:
        return st
    return _by_prefix(getattr(btn, "callback_data", None))


def _styled(btn: InlineKeyboardButton) -> InlineKeyboardButton:
    """색이 없는 콜백 버튼이면 뜻에 맞는 색을 채운 복사본, 아니면 그대로.
    URL·웹앱 등 방장이 만드는 버튼은 안 건드림(composer 테마가 맡음)."""
    try:
        if getattr(btn, "style", None):          # 이미 지정(브랜드·테마) → 존중
            return btn
        if not getattr(btn, "callback_data", None):   # 콜백(소담 UI) 만 자동 색
            return btn
        st = style_for(btn)
        if not st:
            return btn
        data = {k: v for k, v in btn.to_dict().items() if k != "style"}
        data["style"] = st
        return InlineKeyboardButton.de_json(data, None)
    except Exception as e:                        # 버튼 하나 때문에 화면이 깨지면 안 됨
        log.debug("btnstyle skip: %s", e)
        return btn


def apply_rows(rows):
    """버튼 2차원 목록에 색 입힌 새 목록. 전송 전 어디서든 쓸 수 있음."""
    out = []
    for row in rows or []:
        out.append([_styled(b) if isinstance(b, InlineKeyboardButton) else b for b in row])
    return out


_ORIG_INIT = None


def install() -> None:
    """InlineKeyboardMarkup 이 만들어질 때마다 버튼 색 자동 채움 (전역, 한 번)."""
    global _ORIG_INIT
    if _ORIG_INIT is not None:
        return
    _ORIG_INIT = InlineKeyboardMarkup.__init__

    def __init__(self, inline_keyboard, *args, **kwargs):
        try:
            inline_keyboard = apply_rows(inline_keyboard)
        except Exception as e:
            log.debug("btnstyle install skip: %s", e)
        _ORIG_INIT(self, inline_keyboard, *args, **kwargs)

    InlineKeyboardMarkup.__init__ = __init__


def uninstall() -> None:
    global _ORIG_INIT
    if _ORIG_INIT is not None:
        InlineKeyboardMarkup.__init__ = _ORIG_INIT
        _ORIG_INIT = None
