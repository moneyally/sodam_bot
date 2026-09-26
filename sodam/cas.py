"""CAS (Combot Anti-Spam) 조회. 이미 알려진 스팸 계정이면 입장 즉시 차단.

API: GET https://api.cas.chat/check?user_id=ID → {"ok": true, ...} 이면 등록된 스팸 계정.
조회 실패 시에는 차단하지 않는다 (정상 사용자를 막지 않도록).
"""
import logging
import time

import httpx

log = logging.getLogger(__name__)

TTL_HIT = 24 * 3600    # 스팸 판정은 하루 캐시
TTL_MISS = 6 * 3600    # 정상 판정은 6시간 캐시


class Cas:
    def __init__(self, api: str, http: httpx.AsyncClient | None = None):
        self.api = api
        self.http = http or httpx.AsyncClient(timeout=4)
        self._cache: dict[int, tuple[float, bool]] = {}

    async def close(self) -> None:
        await self.http.aclose()

    def cached(self, user_id: int) -> bool | None:
        hit = self._cache.get(user_id)
        if not hit:
            return None
        ts, banned = hit
        if time.time() - ts > (TTL_HIT if banned else TTL_MISS):
            del self._cache[user_id]
            return None
        return banned

    async def is_banned(self, user_id: int) -> bool:
        cached = self.cached(user_id)
        if cached is not None:
            return cached
        try:
            r = await self.http.get(self.api, params={"user_id": user_id})
            r.raise_for_status()
            banned = bool(r.json().get("ok"))
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.info("CAS check failed for %s: %s", user_id, e)
            return False
        self._cache[user_id] = (time.time(), banned)
        return banned
