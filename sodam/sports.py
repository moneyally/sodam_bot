"""스포츠 일정·결과 (TheSportsDB). 배당·베팅 정보는 다루지 않는다."""
import logging
import time
from datetime import datetime, timezone

import httpx
from telegram import Bot
from telegram.error import TelegramError

from .db import DB
from .util import esc

log = logging.getLogger(__name__)

SPORTS_KO = {
    "축구": "Soccer", "야구": "Baseball", "농구": "Basketball", "배구": "Volleyball",
    "아이스하키": "Ice Hockey", "하키": "Ice Hockey", "미식축구": "American Football",
    "테니스": "Tennis", "격투기": "Fighting", "골프": "Golf", "모터스포츠": "Motorsport",
}


class SportsError(Exception):
    pass


def _event_time(ev: dict) -> datetime | None:
    ts = ev.get("strTimestamp")
    try:
        if ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if ev.get("dateEvent"):
            t = (ev.get("strTime") or "00:00:00")[:8]
            return datetime.fromisoformat(f"{ev['dateEvent']}T{t}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return None


def _has_score(ev: dict) -> bool:
    return ev.get("intHomeScore") not in (None, "") and ev.get("intAwayScore") not in (None, "")


def format_event(ev: dict, tz) -> str:
    dt = _event_time(ev)
    when = dt.astimezone(tz).strftime("%m/%d %H:%M") if dt else "시간 미정"
    home, away = ev.get("strHomeTeam") or "?", ev.get("strAwayTeam") or "?"
    league = ev.get("strLeague") or ""
    if _has_score(ev):
        body = f"{home} {ev['intHomeScore']} : {ev['intAwayScore']} {away}"
    else:
        body = f"{home} vs {away}"
    return f"{when} {esc(body)}" + (f" ({esc(league)})" if league else "")


class Sports:
    def __init__(self, api_key: str, db: DB, tz):
        self.base = f"https://www.thesportsdb.com/api/v1/json/{api_key}/"
        self.db = db
        self.tz = tz
        self.http = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params) -> dict:
        try:
            r = await self.http.get(self.base + path, params=params)
            r.raise_for_status()
            return r.json() or {}
        except (httpx.HTTPError, ValueError) as e:
            log.warning("sportsdb %s failed: %s", path, e)
            raise SportsError("스포츠 정보를 가져오지 못했어요. 잠시 후 다시 해주세요.") from e

    async def find_team(self, name: str) -> dict | None:
        data = await self._get("searchteams.php", t=name.strip())
        teams = data.get("teams") or []
        return teams[0] if teams else None

    async def next_events(self, team_id: str) -> list[dict]:
        data = await self._get("eventsnext.php", id=team_id)
        return data.get("events") or []

    async def last_events(self, team_id: str) -> list[dict]:
        data = await self._get("eventslast.php", id=team_id)
        return data.get("results") or []

    async def day_events(self, sport_ko: str = "축구", date: str | None = None) -> list[dict]:
        sport = SPORTS_KO.get(sport_ko, sport_ko)
        date = date or datetime.now(self.tz).strftime("%Y-%m-%d")
        data = await self._get("eventsday.php", d=date, s=sport)
        return data.get("events") or []

    # ── 사람이 읽는 문구 ──────────────────────────────────
    async def today_text(self, sport_ko: str = "축구", limit: int = 10) -> str:
        events = await self.day_events(sport_ko)
        if not events:
            return f"오늘 {esc(sport_ko)} 경기 정보가 없어요."
        events.sort(key=lambda e: _event_time(e) or datetime.max.replace(tzinfo=timezone.utc))
        lines = [f"🏟️ 오늘 {esc(sport_ko)} 경기 ({len(events)}개 중 {min(limit, len(events))}개)"]
        lines += [format_event(e, self.tz) for e in events[:limit]]
        return "\n".join(lines)

    async def team_text(self, team_name: str, which: str = "next") -> str:
        team = await self.find_team(team_name)
        if not team:
            return f"'{esc(team_name)}' 팀을 못 찾았어요. 영어 팀명으로 해주세요 (예: Tottenham, LA Dodgers)."
        events = await (self.next_events(team["idTeam"]) if which == "next" else self.last_events(team["idTeam"]))
        title = "다음 경기" if which == "next" else "최근 결과"
        if not events:
            return f"{esc(team['strTeam'])} {title} 정보가 없어요."
        return "\n".join([f"📅 {esc(team['strTeam'])} {title}"] + [format_event(e, self.tz) for e in events[:5]])

    # ── 구독 알림 (10분마다 실행) ─────────────────────────
    async def run_alerts(self, bot: Bot, is_active=None) -> None:
        subs = await self.db.sports_subs()
        by_team: dict[str, list] = {}
        for s in subs:
            settings = await self.db.get_settings(s["chat_id"])
            if settings["sports_enabled"] and (is_active is None or await is_active(s["chat_id"])):
                by_team.setdefault(s["team_id"], []).append(s)

        now = time.time()
        for team_id, team_subs in by_team.items():
            try:
                upcoming = await self.next_events(team_id)
                finished = await self.last_events(team_id)
            except SportsError:
                continue
            for ev in upcoming[:2]:
                dt = _event_time(ev)
                if dt and 0 < dt.timestamp() - now <= 40 * 60:
                    await self._notify(bot, team_subs, ev, "pre", "⏰ 곧 경기 시작!")
            for ev in finished[:1]:
                dt = _event_time(ev)
                if dt and _has_score(ev) and now - dt.timestamp() <= 12 * 3600:
                    await self._notify(bot, team_subs, ev, "result", "🏁 경기 결과")

    async def _notify(self, bot: Bot, subs: list, ev: dict, kind: str, title: str) -> None:
        for s in subs:
            if not await self.db.mark_sports_sent(s["chat_id"], str(ev.get("idEvent")), kind):
                continue
            try:
                await bot.send_message(s["chat_id"], f"{title}\n{format_event(ev, self.tz)}", parse_mode="HTML")
            except TelegramError as e:
                log.warning("sports alert send failed: %s", e)
