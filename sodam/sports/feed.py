"""여러 소스 중 쓸 것 고르기 + 공유 캐시. 방마다 따로 부르지 않는다: (리그, 날짜) 하나당 한 번 받아 모든 방·명령·알림이 같이 씀."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from .leagues import League
from .providers import Game, Provider, Row, SportsError

log = logging.getLogger(__name__)
STANDINGS_TTL = 1800
TEAM_TTL = 600


class Feed:
    def __init__(self, providers: list[Provider], clock=time.time):
        self.providers = providers
        self.clock = clock
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self.fetches: dict[tuple, int] = {}   # (종류, 리그, …) → 실제로 소스를 부른 횟수 (테스트·점검용)

    def sources(self, lg: League) -> list[Provider]:
        return [p for p in self.providers if p.enabled() and p.supports(lg)]

    def available(self, lg: League) -> bool:
        return bool(self.sources(lg))

    async def _try(self, lg: League, fn):
        last: Exception | None = None
        for p in self.sources(lg):
            try:
                return await fn(p)
            except SportsError as e:     # 한 소스가 막히면 다음 소스로
                last = e
            except Exception as e:       # 응답 모양이 바뀐 경우 등 — 조용히 다음으로 (로그는 남김)
                log.exception("sports %s %s 파싱 실패", p.name, lg.code)
                last = SportsError("경기 정보를 읽지 못했어요.")
                last.__cause__ = e
        if last is None:
            raise SportsError(f"{lg.name} 정보는 아직 볼 수 없어요.")
        raise last

    async def _cached(self, key: tuple, max_age: float, make):
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < max_age:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:                  # 동시에 여러 방이 물어도 한 번만 받음
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < max_age:
                return hit[1]
            value = await make()
            self.fetches[key] = self.fetches.get(key, 0) + 1
            self._cache[key] = (self.clock(), value)
            return value

    async def day(self, lg: League, d: date, max_age: float = 120, only: set[str] | None = None) -> list[Game]:
        key = ("day", lg.code, d)
        if only is not None:    # 라이브: 진행 중 경기가 있는 요청만 다시 → 캐시의 같은 경기를 바꿔 끼움
            fresh = await self._try(lg, lambda p: p.day(lg, d, only))
            self.fetches[key + ("only",)] = self.fetches.get(key + ("only",), 0) + 1
            old = self._cache.get(key, (0, []))[1]
            merged = {g.key: g for g in old} | {g.key: g for g in fresh}
            games = sorted(merged.values(), key=lambda g: (g.start, g.key))
            self._cache[key] = (self.clock(), games)
            return games
        return await self._cached(key, max_age, lambda: self._try(lg, lambda p: p.day(lg, d)))

    def cached_day(self, lg: League, d: date) -> list[Game] | None:
        hit = self._cache.get(("day", lg.code, d))
        return hit[1] if hit else None

    async def standings(self, lg: League) -> list[Row]:
        return await self._cached(("st", lg.code), STANDINGS_TTL, lambda: self._try(lg, lambda p: p.standings(lg)))

    async def team_games(self, lg: League, team: str) -> list[Game]:
        return await self._cached(("team", lg.code, team), TEAM_TTL,
                                  lambda: self._try(lg, lambda p: p.team_games(lg, team)))

    def forget_old(self, keep_after: date) -> None:
        for k in [k for k in self._cache if k[0] == "day" and k[2] < keep_after]:
            self._cache.pop(k, None)
            self._locks.pop(k, None)
