"""⚽ 스포츠: 일정·스코어·순위·팀 경기 + 자동 경기 알림. 배당·베팅 정보는 만들지 않는다.

구조 (sodam/sports/):
- leagues.py  리그·종목·팀 한국어 별칭표 (네트워크 없음)
- providers.py 데이터 소스: ESPN(키 없음, 해외 기본) · 네이버(국내, SPORTS_NAVER=1 일 때만) · TheSportsDB(유료 키일 때만) ·
               API-Sports(APISPORTS_KEY, 연결 전 자리)
- feed.py     소스 고르기·실패 시 다음 소스·(리그, 날짜) 공유 캐시
- alerts.py   구독·공유 폴링·경기 스냅샷 비교·중복 방지·조용한 시간
- ui.py       '.스포츠' 명령·AI 도구 sports 가 쓰는 글
"""
from __future__ import annotations

from .alerts import Alerts
from .feed import Feed
from .leagues import LEAGUES, SPORT_KO
from .providers import ESPN, APISports, Http, Naver, SportsDB, SportsError

# 예전 이름 (AI 도구 enum 등에서 쓰던 것)
SPORTS_KO = {v: k for k, v in SPORT_KO.items()}

__all__ = ["Sports", "SportsError", "SPORTS_KO", "LEAGUES"]


class Sports:
    def __init__(self, api_key: str, db, tz, fetch=None, naver: bool | None = None, clock=None):
        self.http = None
        if fetch is None:
            self.http = fetch = Http()
        self.db, self.tz = db, tz
        self.providers = [APISports(fetch), ESPN(fetch), Naver(fetch, naver), SportsDB(fetch, api_key)]
        kw = {"clock": clock} if clock else {}
        self.feed = Feed(self.providers, **kw)
        self.alerts = Alerts(self.feed, db, **kw)

    async def close(self) -> None:
        if self.http is not None:
            await self.http.close()

    async def run_alerts(self, bot, is_active=None) -> int:
        return await self.alerts.tick(bot, is_active)
