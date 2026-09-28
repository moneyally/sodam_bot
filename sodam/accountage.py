"""계정 생성 시기 추정 (사용자 ID → 날짜). 텔레그램 사용자 ID 는 대체로 가입 순서대로 커진다.

기준점: npm telegram-id-age 1.0.0 의 dataset.json (MIT, github.com/jobians/telegram-id-age, 2013-08 ~ 2025-11, 212개).
최근 ID 는 순서가 섞여 있어서, 시간 순서가 맞는 최대 부분(최장 비감소 부분수열) 99개만 accountage.json 에 남겼다.
두 기준점 사이는 선형 보간, 마지막 기준점보다 큰 ID 는 '그 이후 가입'. 오차가 수개월이라 '최근 계정' 판단(캡차 강제)에만 쓴다.
"""
import bisect
import json
import time
from datetime import datetime, timezone
from pathlib import Path

_POINTS = [(i, datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
           for i, d in json.loads((Path(__file__).with_name("accountage.json")).read_text())]
_IDS = [i for i, _ in _POINTS]
RECENT_DAYS = 180


def estimate(user_id: int) -> tuple[float, bool]:
    """(가입 추정 시각 epoch, 마지막 기준점보다 새 ID 인지). 새 ID 면 시각은 '이 날짜 이후'."""
    k = bisect.bisect_left(_IDS, user_id)
    if k >= len(_POINTS):
        return _POINTS[-1][1], True
    if _IDS[k] == user_id or k == 0:
        return _POINTS[k][1], False
    (i0, t0), (i1, t1) = _POINTS[k - 1], _POINTS[k]
    return t0 + (user_id - i0) / (i1 - i0) * (t1 - t0), False


def is_recent(user_id: int, days: int = RECENT_DAYS, now: float | None = None) -> bool:
    """최근 계정 추정: 기준 데이터보다 새 ID 이거나, 가입 추정이 days 일 안."""
    ts, newer = estimate(user_id)
    return newer or ts >= (now or time.time()) - days * 86400
