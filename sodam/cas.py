"""스팸 계정 명단 조회. 이미 알려진 스팸 계정이면 입장 즉시 차단.

- CAS: GET https://api.cas.chat/check?user_id=ID → {"ok": true, ...} 이면 등록
- lols: GET https://api.lols.bot/account?id=ID → {"ok": true, "banned": true|false}
두 곳을 동시에 조회해서 한 곳이라도 등록이면 스팸. 조회 실패는 '등록 아님'으로 본다 (정상 사용자를 막지 않도록).
"""
import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

TTL_HIT = 24 * 3600    # 스팸 판정은 하루 캐시
TTL_MISS = 6 * 3600    # 정상 판정은 6시간 캐시 (한 곳이라도 조회 실패면 캐시 안 함)
LOLS_API = "https://api.lols.bot/account"


class Cas:
    def __init__(self, api: str, http: httpx.AsyncClient | None = None, lols_api: str | None = None):
        self.api = api
        self.lols_api = lols_api
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

    async def _listed(self, url: str, params: dict, key: str) -> bool | None:
        """등록 여부. 조회 실패면 None."""
        try:
            r = await self.http.get(url, params=params)
            r.raise_for_status()
            return bool(r.json().get(key))
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.info("spam list check failed (%s) for %s: %s", url, params, e)
            return None

    async def is_banned(self, user_id: int) -> bool:
        cached = self.cached(user_id)
        if cached is not None:
            return cached
        checks = [self._listed(self.api, {"user_id": user_id}, "ok")]
        if self.lols_api:
            checks.append(self._listed(self.lols_api, {"id": user_id}, "banned"))
        results = await asyncio.gather(*checks)
        banned = any(results)
        if banned or None not in results:
            self._cache[user_id] = (time.time(), banned)
        return banned
