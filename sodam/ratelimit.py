"""방마다 따로 멈추는 전송 제한기.

PTB 기본 AIORateLimiter 는 어느 요청이든 429(RetryAfter)를 받으면 봇 전체 요청을 그 시간만큼 멈춘다.
그래서 한 방이 게임 화면 수정으로 방 전송 한도(분당 약 20)에 걸리면 다른 모든 방의 답·도배 삭제·게임까지 같이 멈췄다
(사용자 보고: "한쪽 할 때 다른 쪽이 안 된다"). 그룹 429 는 그 방의 한도라서 그 방만 멈추면 된다.
그룹이 아닌 요청(1:1·chat_id 없음)은 봇 전체 한도일 수 있어 PTB 기본 동작 그대로.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta

from telegram.error import RetryAfter
from telegram.ext import AIORateLimiter

log = logging.getLogger(__name__)


def _seconds(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if isinstance(ra, timedelta) else float(ra)


GROUP_BURST = 3   # 한 방에 한꺼번에 나가도 되는 수 (나머지는 고르게 나눠 보냄)


class ChatRateLimiter(AIORateLimiter):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._chat_until: dict[int | str, float] = {}   # 그룹 → 이 시각(monotonic)까지 그 방만 쉼

    def _get_group_limiter(self, group_id):
        """PTB 기본은 '분당 18' 을 한꺼번에 18개까지 몰아 보내게 둠 → 게임이 몰리면 몇 초 안에 18개가 나가
        텔레그램이 그 방을 2~3분씩 막음 (2026-10-15 벳띵 19:15 212초·19:32 137초, 방 전체 먹통).
        같은 평균(분당 group_max_rate)을 지키되 한 번에 GROUP_BURST 개까지만 → 나머지는 3초쯤 간격으로 줄 세움."""
        if group_id not in self._group_limiters and self._group_max_rate:
            from aiolimiter import AsyncLimiter
            burst = min(GROUP_BURST, self._group_max_rate)
            self._group_limiters[group_id] = AsyncLimiter(
                max_rate=burst, time_period=self._group_time_period * burst / self._group_max_rate)
        return super()._get_group_limiter(group_id)

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        chat_id = data.get("chat_id")
        with contextlib.suppress(ValueError, TypeError):
            chat_id = int(chat_id)
        if not ((isinstance(chat_id, int) and chat_id < 0) or isinstance(chat_id, str)):
            return await super().process_request(callback, args, kwargs, endpoint, data, rate_limit_args)
        max_retries = rate_limit_args or self._max_retries
        for i in range(max_retries + 1):
            wait = self._chat_until.get(chat_id, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._run_request(chat=True, group=chat_id,
                                               allow_paid_broadcast=data.get("allow_paid_broadcast", False),
                                               callback=callback, args=args, kwargs=kwargs)
            except RetryAfter as e:
                if i == max_retries:
                    raise
                secs = _seconds(e) + 0.1
                self._chat_until[chat_id] = max(self._chat_until.get(chat_id, 0.0), time.monotonic() + secs)
                if len(self._chat_until) > 5000:
                    now = time.monotonic()
                    self._chat_until = {k: v for k, v in self._chat_until.items() if v > now}
                log.info("방 %s 전송 한도: %.1f초 그 방만 쉼", chat_id, secs)
        return None
