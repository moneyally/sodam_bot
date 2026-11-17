"""🏷 화이트라벨 봇 이름 틀 — 봇이 텔레그램으로 보내는 글의 '소담' 을 그 봇 이름으로 (받침까지 맞춰서).

코드·안내서·AI 지시문에 '소담' 이 600줄 넘게 박혀 있어서 하나씩 고치는 대신, 모든 보내기가 지나가는
길목 `telegram.Bot._post` 한 곳에서 바꾼다 (btnstyle.install 과 같은 방식). 그래서 메뉴·안내문·AI 답·
다른 방에서 넘겨온(clone_migrate) 저장 글(인사말·자동답글·예약공지)까지 전부 자동으로 그 봇 이름이 된다.
이름이 '소담' 이면(본체) 아무것도 안 한다. (2026-11-17 오너 '화이트라벨이면 그 봇 이름 먹히게, 프레임워크로')

바꾸는 곳: text·caption·question·explanation, 인라인 버튼 글자, 미디어 설명. 엔티티(글자 위치 표시)가 붙은
글은 길이가 바뀌면 위치가 틀어지니 건드리지 않는다. 링크·@아이디 같은 영문은 대상이 아니다(한글 '소담' 만).
"""
from __future__ import annotations

import re

from telegram import Bot, InlineKeyboardMarkup, InputMedia

SOURCE = "소담"
_NAME: str | None = None        # 바꿀 이름 (None = 끔)
_INSTALLED = False
# 한 프로세스에 봇이 여럿일 때 봇마다 이름 (음성 담당이 클론 봇 이름으로도 노래 카드를 보냄 — voice/tenants.py)
_BOT_NAMES: dict[int, str] = {}

# '소담' 뒤에 붙는 말 (받침 있는 이름 기준으로 쓰여 있음). 긴 것부터.
# '이'+조사 = 이름에 붙는 '이'(소담이가) → 받침 없는 이름이면 '이' 를 뺌.
_TAIL = re.compile(
    r"소담(이에요|이예요|이에게|이한테|이랑|이라고|이라서|이라면|이라|이란|이잖아|이지만|이지|이죠|이고|이나|"
    r"이가|이는|이를|이도|이의|이만|이야|이에|이와|이께|으로|이|아|은|을|과)?")

# 받침 있는 이름일 때(그대로 둠) / 받침 없는 이름일 때 바꿀 꼴
_NO_BATCHIM = {
    "이에요": "예요", "이예요": "예요", "이라고": "라고", "이라서": "라서", "이라면": "라면", "이라": "라",
    "이란": "란", "이잖아": "잖아", "이지만": "지만", "이지": "지", "이죠": "죠", "이고": "고", "이나": "나",
    "이야": "야", "이": "가", "아": "야", "은": "는", "을": "를", "과": "와", "으로": "로",
    # '이'+조사는 '이' 만 뺌
    "이에게": "에게", "이한테": "한테", "이랑": "랑", "이가": "가", "이는": "는", "이를": "를", "이도": "도",
    "이의": "의", "이만": "만", "이에": "에", "이와": "와", "이께": "께",
}


def _batchim(word: str) -> bool:
    last = word[-1:] if word else ""
    if "가" <= last <= "힣":
        return (ord(last) - 0xAC00) % 28 != 0
    return False        # 한글이 아니면 받침 없는 걸로 (SODA야 · SODA가)


def rename(text, name: str | None = None):
    """글 속 '소담' → name (받침에 맞게 조사 고침). name 없으면 설치된 이름, 그것도 없거나 '소담' 이면 그대로."""
    name = name if name is not None else _NAME
    if not text or not isinstance(text, str) or not name or name == SOURCE or SOURCE not in text:
        return text
    keep = _batchim(name)

    def sub(m: re.Match) -> str:
        tail = m.group(1) or ""
        if keep:
            return name + tail
        if tail == "이":
            nxt = m.string[m.end():m.end() + 1]
            # '소담이네·소담이었어' 처럼 뒤에 말이 붙으면 이름에 붙은 '이' 만 뺌, 홀로 '소담이 왔어' 는 '가'
            return name + ("" if "가" <= nxt <= "힣" else "가")
        return name + _NO_BATCHIM.get(tail, tail)
    return _TAIL.sub(sub, text)


def _fix_markup(markup, name: str | None = None) -> None:
    if not isinstance(markup, InlineKeyboardMarkup):
        return          # 답장 키보드는 누르면 그 글자가 되돌아와 버튼 매칭이 깨질 수 있어 안 건드림
    for row in markup.inline_keyboard:
        for btn in row:
            new = rename(btn.text, name)
            if new != btn.text:
                with btn._unfrozen():
                    btn.text = new


def _fix_media(media, name: str | None = None) -> None:
    items = media if isinstance(media, (list, tuple)) else [media]
    for m in items:
        if isinstance(m, InputMedia) and m.caption and not m.caption_entities:
            new = rename(m.caption, name)
            if new != m.caption:
                with m._unfrozen():
                    m.caption = new


def fix_data(data: dict | None, name: str | None = None) -> None:
    """보내기 직전 요청 값(_post 의 data)을 제자리에서 고침. name 없으면 프로세스 이름(install)."""
    name = name or _NAME
    if not data or not name or name == SOURCE:
        return
    if not data.get("entities") and not data.get("caption_entities"):
        for key in ("text", "caption", "question", "explanation"):
            if isinstance(data.get(key), str):
                data[key] = rename(data[key], name)
    if data.get("reply_markup") is not None:
        _fix_markup(data["reply_markup"], name)
    if data.get("media") is not None:
        _fix_media(data["media"], name)


def _patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    orig = Bot._post

    async def _post(self, endpoint, data=None, **kw):
        try:
            fix_data(data, _BOT_NAMES.get(id(self)) or _NAME)
        except Exception:       # 이름 바꾸다 실패해도 글은 원래대로 보냄
            pass
        return await orig(self, endpoint, data, **kw)
    Bot._post = _post
    _INSTALLED = True


def install(name: str) -> None:
    """봇 이름을 정하고(본체 '소담' 이면 끔) 보내기 길목을 한 번만 감싼다."""
    global _NAME
    _NAME = name if name and name != SOURCE else None
    if _NAME:
        _patch()


def set_bot_name(bot, name: str) -> None:
    """이 봇 객체가 보내는 글만 이 이름으로 (한 프로세스에 봇 여럿 — 음성 담당이 클론 봇으로 노래 카드)."""
    if name and name != SOURCE:
        _BOT_NAMES[id(bot)] = name
        _patch()
