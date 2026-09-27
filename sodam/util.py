import asyncio
import html
import re
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telegram.error import NetworkError, TelegramError

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


def sent_at(msg) -> int | None:
    """메시지를 보낸 시각(epoch). 받은 시각이 아니라서 재시작 뒤 밀린 메시지도 원래 시각 그대로. 모르면 None."""
    d = getattr(msg, "date", None)
    return int(d.timestamp()) if isinstance(d, datetime) else None


def is_stale(msg, sec: int = STALE_SEC) -> bool:
    """재시작 동안 쌓였다 늦게 받은 메시지인지 (AI 답·끼어들기는 건너뜀, 입장·관리는 그대로 처리)."""
    ts = sent_at(msg)
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


MAX_DURATION_MIN = 366 * 1440  # 이보다 길면 None (timedelta OverflowError·텔레그램 한도 방지)


def parse_duration(text: str) -> int | None:
    """'30m', '2시간', '1d', '45' → 분. 형식이 아니거나 366일을 넘으면 None."""
    m = _DURATION.match(text.strip())
    if not m:
        return None
    minutes = int(m.group(1)) * _UNIT_MIN[(m.group(2) or "m").lower()]
    return minutes if minutes <= MAX_DURATION_MIN else None


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


_BG: set = set()


def post_temp(bot, chat_id: int, text: str, seconds: int = 120) -> None:
    """방에 안내를 올리고 seconds 뒤 지운다 (백그라운드, 실패는 무시).
    지울 글은 DB 에도 적어 그 사이 재시작돼도 지운다 (sodam/persist.py, 봇에 bind 된 DB)."""
    from . import persist   # 늦게 import (persist → db → util)

    async def run():
        try:
            sent = await bot.send_message(chat_id, text, parse_mode="HTML")
        except TelegramError:
            return
        db = persist.db_of(bot)
        if db is not None:
            try:
                await persist.remember_delete(db, bot, chat_id, sent.message_id, seconds)
            except Exception:
                pass
        await asyncio.sleep(seconds)
        await persist.delete_now(db, bot, chat_id, sent.message_id)
    task = asyncio.create_task(run())
    _BG.add(task)
    task.add_done_callback(_BG.discard)


RETRY_DELAY = 3.0


def surely_unsent(e: BaseException) -> bool:
    """연결 자체가 안 된 네트워크 오류 = 텔레그램이 요청을 못 받음 → 다시 보내도 중복 없음.
    응답만 끊긴 경우(서버가 응답 없이 끊음·읽기 시간 초과)는 이미 보내졌을 수 있어서 False."""
    return isinstance(e, NetworkError) and isinstance(e.__cause__, (httpx.ConnectError, httpx.ConnectTimeout,
                                                                    httpx.PoolTimeout))


async def send_retry(make, *, dup_ok: bool = False, tries: int = 2):
    """make() = 보내기 코루틴을 새로 만드는 함수. 네트워크 오류면 RETRY_DELAY 뒤 다시 보낸다.
    dup_ok=False(방에 보이는 글): 확실히 안 보내진 경우만 다시 → 같은 글이 두 번 올라가지 않게."""
    for i in range(tries):
        try:
            return await make()
        except NetworkError as e:
            if i == tries - 1 or not (dup_ok or surely_unsent(e)):
                raise
            await asyncio.sleep(RETRY_DELAY)
