"""방별 설정. 기본값 + 타입 검증. `.set 키 값` 으로 바꾼다."""
import re
from typing import Any, Callable

from .security import normalize_domain
from .styles import STYLES, resolve_style

DEFAULTS: dict[str, Any] = {
    # AI
    "ai_enabled": True,
    "style": "polite",              # 방 기본 말투
    "reply_max_chars": 400,         # 답변 최대 글자 수
    "user_rate_per_min": 5,         # 1인당 분당 AI 호출 (연달아 보낸 말은 한 번으로 셈 — handlers.BURST_SECONDS)
    "room_rate_per_min": 20,        # 방 전체 분당 AI 호출
    "web_search_daily": 30,         # 방당 하루 웹검색 횟수
    # 입장
    "greet_enabled": True,
    "greet_template": "",           # 비우면 AI가 인사. {names} 자리에 이름
    "delete_join_message": False,   # "OOO님이 입장했습니다" 서비스 메시지 삭제
    "captcha_enabled": True,        # 입장 캡차 (버튼 눌러야 채팅 가능)
    "captcha_minutes": 5,
    "captcha_action": "kick",       # 실패/시간초과 시: kick(재입장 가능) / ban / mute
    "cas_enabled": True,            # CAS 스팸 DB 조회
    "recent_account_captcha": True,  # 최근 만든 계정(ID 로 추정, accountage.py)은 캡차 설정과 상관없이 캡차
    # 도배
    "flood_count": 6,
    "flood_seconds": 8,
    "flood_mute_minutes": 30,
    "dup_limit": 3,                 # 같은 내용 연속 N번이면 삭제+경고
    # 링크 / 금지어
    "link_filter": True,            # 관리자 외 링크 삭제
    "newbie_link_hours": 24,        # link_filter 꺼져 있어도 신규는 N시간 링크 금지
    "whitelist_domains": [],
    # 경고 누적
    "warn_mute_at": 3,
    "warn_mute_minutes": 60,
    "warn_ban_at": 5,
    # 보안
    "injection_guard": True,
    "injection_warn": True,         # 인젝션 시도하면 경고 1회
    "impersonation_guard": True,    # 관리자 사칭 닉네임 입장 차단
    # 기능
    "games_enabled": True,
    "sports_enabled": True,
    "daily_report": True,           # 매일 밤 채팅 집계 리포트
    "rules": "",
}

# 사람이 읽는 설명 (.settings 출력용)
LABELS: dict[str, str] = {
    "ai_enabled": "AI 대화",
    "style": "기본 말투",
    "reply_max_chars": "답변 최대 글자",
    "user_rate_per_min": "1인 분당 AI 호출",
    "room_rate_per_min": "방 분당 AI 호출",
    "web_search_daily": "하루 웹검색 한도",
    "greet_enabled": "입장 인사",
    "greet_template": "인사 템플릿",
    "delete_join_message": "입장 메시지 삭제",
    "captcha_enabled": "입장 캡차",
    "captcha_minutes": "캡차 제한시간(분)",
    "captcha_action": "캡차 실패 시",
    "cas_enabled": "스팸 명단(CAS·lols) 차단",
    "recent_account_captcha": "최근 만든 계정은 캡차",
    "flood_count": "도배 기준(개)",
    "flood_seconds": "도배 기준(초)",
    "flood_mute_minutes": "도배 뮤트(분)",
    "dup_limit": "같은 말 반복 한도",
    "link_filter": "링크 차단",
    "newbie_link_hours": "신규 링크 금지(시간)",
    "whitelist_domains": "허용 도메인",
    "warn_mute_at": "경고 N회 뮤트",
    "warn_mute_minutes": "경고 뮤트(분)",
    "warn_ban_at": "경고 N회 밴",
    "injection_guard": "인젝션 방어",
    "injection_warn": "인젝션 시 경고",
    "impersonation_guard": "사칭 차단",
    "games_enabled": "게임",
    "sports_enabled": "스포츠 알림",
    "daily_report": "일일 리포트",
    "rules": "방 규칙",
}

# 정해진 값만 받는 설정: 키 → {입력 별칭: 저장값}
CHOICES: dict[str, dict[str, str]] = {
    "captcha_action": {"kick": "kick", "킥": "kick", "내보내기": "kick",
                       "ban": "ban", "밴": "ban", "mute": "mute", "뮤트": "mute"},
}
# 선택지 설정의 화면 글자: 설정 키 → {저장값: 글자} (같은 값 'kick' 도 설정마다 뜻이 달라서 키별로)
CHOICE_LABELS: dict[str, dict[str, str]] = {"captcha_action": {"kick": "내보내기(재입장 가능)", "ban": "밴", "mute": "뮤트"}}


def choice_label(key: str, value: str) -> str:
    return CHOICE_LABELS.get(key, {}).get(value, str(value))
TIME_KEYS: set[str] = set()  # HH:MM 형식 설정 (지금은 없음, 추가 시 여기에)
# 숫자 설정의 허용 범위
RANGES: dict[str, tuple[int, int]] = {
    "reply_max_chars": (50, 3500),
    "user_rate_per_min": (1, 60),
    "room_rate_per_min": (1, 600),
    "web_search_daily": (0, 1000),
    "captcha_minutes": (1, 60),
    "flood_count": (2, 100),
    "flood_seconds": (1, 120),
    "flood_mute_minutes": (1, 525600),
    "dup_limit": (2, 50),
    "newbie_link_hours": (0, 720),
    "warn_mute_at": (1, 100),
    "warn_mute_minutes": (1, 525600),
    "warn_ban_at": (1, 100),
}

# 특수한 값의 표시 방법 (목록 안에 목록 등). register_setting(render_fn=…) 로 추가
RENDERERS: dict[str, Callable[[Any], str]] = {}
# 형식 검사가 따로 필요한 값 (글자 → 저장값, 틀리면 ValueError). register_setting(validator=…) / register_validator 로 추가.
# .설정변경·AI change_setting 이 같은 coerce 를 거치므로 여기서 막으면 둘 다 막힘 (실제 버그: 허용 도메인에 URL 이 그대로 저장)
VALIDATORS: dict[str, Callable[[str], Any]] = {}
MAX_DOMAINS = 50

_TRUE = {"on", "true", "1", "yes", "켜기", "켬", "예", "ㅇ"}
_FALSE = {"off", "false", "0", "no", "끄기", "끔", "아니오", "ㄴ"}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(raw: str) -> str:
    """'1:00' → '01:00'. 형식이 틀리면 ValueError."""
    m = _HHMM.match(raw.strip())
    if not m:
        raise ValueError("시간은 HH:MM 형식으로 입력해주세요 (예: 09:00)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


# 비용이 바로 늘어나는 한도: 방 관리자는 기본값까지만(줄이기만), 그 위로는 오너만.
# 실제 사례 2026-09-30: 오너가 "관리자가 이미지 한도를 20 으로 올려 악용하면?" — 이미지·웹검색은 한 번마다 요금.
# 영상(video_weekly·video_seconds, 2026-09-30 오너: 방마다 한 주 6개)도 같은 규칙 — sodam/panels/videogen.py
OWNER_CAP: dict[str, int] = {"image_daily": 5, "web_search_daily": 30, "video_weekly": 6, "video_seconds": 6}


def over_cap(key: str, value: Any) -> str | None:
    """방 관리자가 못 넘는 값이면 안내 문구, 아니면 None (오너는 부르지 말 것)."""
    cap = OWNER_CAP.get(key)
    if cap is not None and isinstance(value, int) and not isinstance(value, bool) and value > cap:
        return f"{LABELS.get(key, key)}은(는) 비용 때문에 방 관리자는 {cap}까지만 정할 수 있어요 (더 올리는 건 봇 오너만)."
    return None


def coerce(key: str, raw: str) -> Any:
    """문자열 입력을 기본값 타입에 맞게 변환. 잘못되면 ValueError."""
    if key not in DEFAULTS:
        raise ValueError(f"없는 설정이에요: {key}")
    default = DEFAULTS[key]
    raw = raw.strip()
    if key == "style":
        style = resolve_style(raw)
        if not style:
            raise ValueError("말투는 " + ", ".join(s.label for s in STYLES.values()) + " 중에서 골라주세요")
        return style
    if key in CHOICES:
        value = CHOICES[key].get(raw.lower())
        if not value:
            raise ValueError(" / ".join(sorted(set(CHOICES[key]))) + " 중에서 골라주세요")
        return value
    if key in VALIDATORS:
        return VALIDATORS[key](raw)
    if key in TIME_KEYS:
        return parse_hhmm(raw)
    if isinstance(default, bool):
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError("on / off 로 입력해주세요")
    if isinstance(default, int):
        try:
            value = int(raw)
        except ValueError:
            raise ValueError("숫자로 입력해주세요") from None
        lo, hi = RANGES.get(key, (0, 100_000))
        if not lo <= value <= hi:
            raise ValueError(f"{lo} ~ {hi} 사이로 입력해주세요")
        return value
    if isinstance(default, list):
        return [x.strip().lower() for x in raw.split(",") if x.strip()]
    return raw[:2000]


def render(key: str, value: Any) -> str:
    if key in RENDERERS:
        return RENDERERS[key](value)
    if isinstance(value, bool):
        return "켜짐" if value else "꺼짐"
    if key == "style":
        return STYLES[value].label if value in STYLES else str(value)
    if key in CHOICES:
        return choice_label(key, value)
    if isinstance(value, list):
        return ", ".join(value) if value else "(없음)"
    if value == "":
        return "(없음)"
    text = str(value)
    return text if len(text) <= 40 else text[:40] + "…"


def domains_value(raw: str) -> list[str]:
    """허용 도메인 목록 글자('youtube.com, https://www.Naver.com/x') → 정규화된 도메인 목록.
    도메인이 아닌 게 섞이면 ValueError (예전: URL 이 그대로 저장돼 security.link_allowed 와 절대 안 맞았음)."""
    items = [x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()]
    good = [normalize_domain(x) for x in items]
    if bad := [x for x, d in zip(items, good) if not d]:
        raise ValueError(f"도메인 형식이 아니에요: {', '.join(bad[:3])} (예: youtube.com)")
    out = list(dict.fromkeys(d for d in good if d))
    if len(out) > MAX_DOMAINS:
        raise ValueError(f"허용 도메인은 {MAX_DOMAINS}개까지예요")
    return out


def max_text(limit: int) -> Callable[[str], str]:
    """글자 수 한도 검사기 (인사말 편집기와 같은 한도)."""
    def check(raw: str) -> str:
        if len(raw) > limit:
            raise ValueError(f"{limit}자까지예요 (보낸 글: {len(raw)}자)")
        return raw
    return check


VALIDATORS["whitelist_domains"] = domains_value


def register_validator(key: str, fn: Callable[[str], Any]) -> None:
    VALIDATORS[key] = fn


def register_setting(key: str, default: Any, label: str, *, range_: tuple[int, int] | None = None,
                     choices: dict[str, str] | None = None, choice_labels: dict[str, str] | None = None,
                     render_fn: Callable[[Any], str] | None = None,
                     validator: Callable[[str], Any] | None = None) -> None:
    """기능 모듈이 자기 설정 키를 추가한다 (settings.py 를 직접 고치지 않게). import 시점에 호출."""
    DEFAULTS.setdefault(key, default)
    if validator:
        VALIDATORS[key] = validator
    LABELS.setdefault(key, label)
    if range_:
        RANGES[key] = range_
    if choices:
        CHOICES[key] = choices
    if choice_labels:
        CHOICE_LABELS[key] = choice_labels
    if render_fn:
        RENDERERS[key] = render_fn
