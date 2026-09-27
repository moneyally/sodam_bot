"""💾 디스크 여유 공간 감시 (1시간마다, handlers.job_disk).

SQLite 는 디스크가 가득 차도 DB 가 깨지지 않고 쓰기만 실패한다(SQLITE_FULL). 소담은 기록을 못 해도 관리·명령·AI 는
계속하지만(handlers._record), 경고·포인트·결제 기록이 막히니 미리 막는다:
여유 공간이 LOW_MB 아래면 대화·요청 기록을 EMERGENCY_DAYS 일만 남기고 WAL 을 비운 뒤 오너에게 알림 (ALERT_GAP 에 1번).
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .db import disk_full

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

LOW_MB = 500
EMERGENCY_DAYS = 30
ALERT_GAP = 6 * 3600
_last_alert = 0.0


def free_mb(path: str) -> int:
    return shutil.disk_usage(Path(path).resolve().parent).free // (1024 * 1024)


async def check(svc: Services, bot, free: int | None = None) -> bool:
    """여유가 모자라면 정리·알림 후 True."""
    global _last_alert
    free = free_mb(svc.cfg.db_path) if free is None else free
    if free >= LOW_MB:
        return False
    try:
        await svc.db.prune(int(time.time()), days=EMERGENCY_DAYS)
        note = f"대화 기록을 최근 {EMERGENCY_DAYS}일만 남기고 정리했어요."
    except Exception as e:   # 정리 자체가 막힐 만큼 가득 찬 경우
        log.warning("긴급 정리 실패: %s", e)
        note = "정리도 못 할 만큼 가득 찼어요." if disk_full(e) else f"정리 실패: {e}"
    if time.time() - _last_alert >= ALERT_GAP:
        _last_alert = time.time()
        await svc.mod.report(bot, f"💾 서버 디스크 여유 공간이 {free}MB 남았어요 (기준 {LOW_MB}MB). {note}\n"
                                  "서버의 오래된 로그·백업을 지우거나 디스크를 늘려주세요.")
    return True
