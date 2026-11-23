"""🖼 월 이미지 한도 (image_monthly) — 하루 한도(image_daily)와 **둘 다** 걸리는 '그 달' 누적 상한.

2026-11-23 오너 요청: 특정 방을 한 달에 N장으로 막기. 하루 한도(image_daily)는 그대로 두고 **월 상한을 따로 추가**
(하루도 막고 월도 막음). 영상 주간 한도(video_weekly)와 같은 틀 — counters 의 'YYYY-MM' 칸이라 **매달 1일 자동 리셋**.
기본 0 = 끔(하루 한도만). 오너만 켬(OWNER_CAP image_monthly=0). 이미지 세는 자리(make_image·그림체·브랜드 배경) 전부에서
하루 카운터('image') 옆에 월 카운터('image_month')도 같이 보고 올린다. 오너는 하루 한도와 똑같이 월 한도도 무시.
"""
from __future__ import annotations

from datetime import datetime

from .settings import register_setting

# 0 = 끔(하루 한도만 적용). 오너만 켜고 올림 (settings.OWNER_CAP["image_monthly"] = 0)
register_setting("image_monthly", 0, "한 달 이미지 만들기(장)", range_=(0, 5000))

KEY = "image_month"   # counters: ('YYYY-MM', 방) 칸 = 이번 달 만든 장수


def _month(tz) -> str:
    return datetime.now(tz).strftime("%Y-%m")


async def used(svc, chat_id: int) -> int:
    """이번 달 이 방이 만든 이미지 장수."""
    return await svc.db.counter(_month(svc.cfg.tz), chat_id, KEY)


async def over(svc, chat_id: int, settings: dict) -> int:
    """월 한도를 넘었으면 그 한도값(>0), 아니면 0. limit 0 = 끔(항상 0)."""
    limit = int(settings.get("image_monthly", 0) or 0)
    if limit <= 0:
        return 0
    return limit if await used(svc, chat_id) >= limit else 0


async def bump(svc, chat_id: int, n: int = 1) -> int:
    """이번 달 카운터 올리기(만든 뒤) — 되돌릴 땐 n=-1."""
    return await svc.db.bump(_month(svc.cfg.tz), chat_id, KEY, n)
