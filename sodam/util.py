import html
import re
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

esc = html.escape


def display_name(first: str | None, last: str | None = None, username: str | None = None) -> str:
    name = " ".join(x for x in (first, last) if x).strip()
    return name or (f"@{username}" if username else "알 수 없음")


def user_name(user) -> str:
    """telegram.User 또는 DB Row 둘 다 받는다."""
    get = user.get if isinstance(user, dict) else (lambda k: user[k] if k in user.keys() else None) \
        if hasattr(user, "keys") else (lambda k: getattr(user, k, None))
    return display_name(get("first_name"), get("last_name"), get("username"))


_INT = re.compile(r"-?\d{1,18}")  # SQLite INTEGER(int64) 범위 안


STALE_SEC = 300


def is_stale(msg, sec: int = STALE_SEC) -> bool:
    """재시작 동안 쌓였다 늦게 받은 메시지인지 (AI 답·끼어들기는 건너뜀, 입장·관리는 그대로 처리)."""
    d = getattr(msg, "date", None)
    ts = d.timestamp() if isinstance(d, datetime) else None
    return ts is not None and time.time() - ts > sec


def to_int(s: str) -> int | None:
    """'--5', '²', 19자리 이상 같은 값에서 int()·SQLite 가 터지지 않게. 정수 문자열이 아니면 None."""
    return int(s) if isinstance(s, str) and _INT.fullmatch(s) else None


def iyeyo(name: str) -> str:
    """'소담' → '소담이에요', '나비' → '나비예요' (받침 유무에 맞춰)."""
    last = name[-1:] or " "
    has_final = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return name + ("이에요" if has_final else "예요")


def josa(word: str, pair: str = "을를") -> str:
    """받침에 맞는 조사: josa('스팸') → '스팸을', josa('도박사이트') → '도박사이트를'. pair 는 '을를','이가','은는','과와'.
    한글이 아니면 '을(를)' 식으로."""
    last = word[-1:] or " "
    if not "가" <= last <= "힣":
        return f"{word}{pair[0]}({pair[1]})"
    return word + (pair[0] if (ord(last) - 0xAC00) % 28 else pair[1])


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(name)}</a>'


_DURATION = re.compile(r"^(\d+)\s*(m|min|분|h|시간|d|일|w|주)?$", re.I)
_UNIT_MIN = {"m": 1, "min": 1, "분": 1, "h": 60, "시간": 60, "d": 1440, "일": 1440, "w": 10080, "주": 10080}


def parse_duration(text: str) -> int | None:
    """'30m', '2시간', '1d', '45' → 분. 형식이 아니면 None."""
    m = _DURATION.match(text.strip())
    if not m:
        return None
    unit = (m.group(2) or "m").lower()
    return int(m.group(1)) * _UNIT_MIN[unit]


def human_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}일"
    if minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes}분"


def fmt_time(ts: int, tz: ZoneInfo, fmt: str = "%m/%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz).strftime(fmt)


def day_start(tz: ZoneInfo, days_ago: int = 0) -> int:
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()) - days_ago * 86400


PERIODS = {
    "오늘": 0, "today": 0,
    "어제": 1, "yesterday": 1,
    "주간": 6, "이번주": 6, "week": 6, "7일": 6,
    "월간": 29, "이번달": 29, "month": 29, "30일": 29,
    "전체": -1, "all": -1,
}


def period_since(word: str, tz: ZoneInfo) -> tuple[int, str]:
    """기간 단어 → (시작 타임스탬프, 표시 이름)."""
    days = PERIODS.get(word.strip().lower(), 0) if word else 0
    if days < 0:
        return 0, "전체"
    label = {0: "오늘", 1: "어제부터", 6: "최근 7일", 29: "최근 30일"}.get(days, "오늘")
    return day_start(tz, days), label


class RateLimiter:
    """키별 최근 60초 호출 수 제한."""
    def __init__(self):
        self._hits: dict[tuple, deque[float]] = {}
        self._calls = 0

    def allow(self, key: tuple, per_minute: int) -> bool:
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            return False
        q.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:  # 가끔 빈 키 정리 (사람 수만큼 계속 쌓이지 않게)
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                del self._hits[k]
        return True
