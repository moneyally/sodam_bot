"""AI change_setting 이 고를 수 있는 방 설정 키 (목록은 부를 때마다 settings.DEFAULTS 에서 만든다).

실제 버그 (2026-09-30 전수 점검): 도구 정의가 tools.py import 시점에 `list(DEFAULTS)` 로 굳어 그 뒤 register_setting 한
51개(잠금·사기 의심·스팸 방패·퇴장 인사·뉴스·음성·연동…)가 목록에서 빠짐 → "사진 막아줘"가 '못 해요'로.
이제 모든 설정은 **목록에 들어가거나 EXCLUDED 에 이유와 함께** 있어야 한다 (tests/test_admin_nl.py 가 검사 — 새 설정이 조용히 빠지지 않게).
"""
from __future__ import annotations

import re

from .settings import CHOICES, DEFAULTS, LABELS

# 말로 바꾸면 안 되는 키 → AI 에게 돌려줄 이유 (대신 할 방법)
EXCLUDED: dict[str, str] = {
    "gt_setter": "알림 받을 관리자는 game_alert 로 정함 (요청한 관리자가 받음)",
    "greet_media_type": "인사 사진·영상은 관리자 1:1 메뉴 ✏️ 인사 편집기에서만",
    "greet_media_id": "인사 사진·영상은 관리자 1:1 메뉴 ✏️ 인사 편집기에서만",
    "greet_copy": "글 복사 인사는 관리자 1:1 메뉴 ✏️ 인사 편집기에서만 (글을 전달해서 정함)",
    "greet_buttons": "인사 URL 버튼은 관리자 1:1 메뉴 ✏️ 인사 편집기에서만",
    "farewell_buttons": "퇴장 인사 URL 버튼은 관리자 1:1 메뉴 👋 퇴장 인사에서만",
    "whitelist_domains": "허용 도메인은 edit_list 로 더하기·빼기 (통째로 바꾸면 기존 도메인이 사라짐)",
}
AI_OFF_NOTE = (" AI 대화를 끄면 소담은 이 방에서 말로 부르는 요청에 답하지 않음 → 다시 켜는 건 말로 안 되고 "
               "관리자가 '.AI대화 켜기' 명령이나 1:1 메뉴 🧩 기능에서 켜야 한다고 꼭 안내할 것.")
_cache: dict[int, tuple[str, dict]] = {}


def reachable() -> list[str]:
    """지금 등록된 설정 중 AI 가 바꿀 수 있는 키 (등록 순서)."""
    return [k for k in DEFAULTS if k not in EXCLUDED]


def _squash(s: str) -> str:
    return re.sub(r"[\s_()·/,.-]+", "", s).lower()


def resolve(raw: str) -> str | None:
    """키 이름 또는 한국어 이름('사진 막기', '입장 캡차') → 설정 키. 없으면 None (제외 키도 그대로 돌려줌 → 이유 안내)."""
    raw = str(raw or "").strip()
    if raw in DEFAULTS:
        return raw
    want = _squash(raw)
    if not want:
        return None
    hits = [k for k in DEFAULTS if _squash(LABELS.get(k, "")) == want or _squash(k) == want]
    return hits[0] if len(hits) == 1 else None


def _line(key: str) -> str:
    label = LABELS.get(key, key)
    if key in CHOICES:
        values = list(dict.fromkeys(CHOICES[key].values()))
        return f"{key}={label}({'|'.join(values)})"
    return f"{key}={label}"


def schema() -> tuple[str, dict]:
    """(설명, 인자) — 도구 목록을 만들 때마다 부른다 (tools.Tool.build). 키가 늘면 다시 만들고, 같으면 글자까지 같게(캐시)."""
    hit = _cache.get(len(DEFAULTS))
    if hit:
        return hit
    keys = reachable()
    desc = ("[관리자] 방 설정 하나를 바꾼다 (바로 저장). value 는 켜기/끄기·숫자·선택지 값·글. "
            "금지어·허용 도메인 더하기/빼기는 edit_list. 키=뜻(선택지): " + ", ".join(_line(k) for k in keys))
    params = {"key": {"type": "string", "enum": keys}, "value": {"type": "string"}}
    _cache.clear()
    _cache[len(DEFAULTS)] = (desc, params)
    return desc, params
