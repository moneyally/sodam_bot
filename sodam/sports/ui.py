"""'.스포츠' 명령과 AI 도구 sports 가 쓰는 글 만들기. 결과는 텔레그램 HTML."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta

from ..util import esc
from . import fmt
from .alerts import LEVELS, MAX_FOLLOWS, QUIET
from .leagues import (LEAGUES, POPULAR, SPORT_KO, League, Team, find_group, find_league, find_sport, find_team, leagues_of_sport,
                      same_team)
from .providers import KST, SportsError

EXAMPLES = "예: EPL · 라리가 · 챔스 · MLB · NBA · KBO · 토트넘 · 다저스"
NOT_READY = "국내 리그(KBO·K리그·KBL·V리그 등)는 아직 준비 중이에요."
PER_LEAGUE = 10
WEEK = "월화수목금토일"


def resolve(q: str) -> tuple[str, object] | None:
    """('league', League) / ('sport', [League]) / ('team', Team) / None."""
    q = (q or "").strip()
    if not q:
        return None
    group = find_group(q)
    if group:
        return "sport", group
    lg = find_league(q)
    if lg:
        return "league", lg
    sport = find_sport(q)
    if sport:
        return "sport", leagues_of_sport(sport)
    team = find_team(q)
    if team:
        return "team", team
    return None


def parse_day(word: str, today: date) -> date | None:
    w = (word or "").strip()
    rel = {"오늘": 0, "today": 0, "내일": 1, "tomorrow": 1, "모레": 2, "어제": -1, "yesterday": -1, "그제": -2}
    if w in rel:
        return today + timedelta(days=rel[w])
    m = re.fullmatch(r"(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2})", w)
    if m:
        try:
            return date(int(m.group(1) or today.year), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _day_label(d: date, today: date) -> str:
    name = {0: "오늘", 1: "내일", -1: "어제"}.get((d - today).days)
    base = f"{d.month}/{d.day}({WEEK[d.weekday()]})"
    return f"{name} {base}" if name else base


class UI:
    def __init__(self, sports):
        self.sp = sports
        self.feed = sports.feed

    def today(self) -> date:
        return datetime.fromtimestamp(self.feed.clock(), KST).date()

    def _usable(self, leagues: list[League]) -> tuple[list[League], list[League]]:
        ok = [lg for lg in leagues if self.feed.available(lg)]
        return ok, [lg for lg in leagues if lg not in ok]

    async def _days(self, leagues: list[League], d: date) -> list[tuple[League, list | None]]:
        async def one(lg):
            try:
                return lg, await self.feed.day(lg, d)
            except SportsError:
                return lg, None
        return list(await asyncio.gather(*(one(lg) for lg in leagues)))

    # ── 일정·스코어 ─────────────────────────────────────
    async def games_text(self, query: str = "", d: date | None = None, live_only: bool = False,
                         extra: list[str] | None = None) -> str:
        today = self.today()
        d = d or today
        found = resolve(query) if query else None
        if query and not found:
            return f"'{esc(query)}' 리그·팀을 못 찾았어요.\n{EXAMPLES}"
        team: Team | None = None
        if found is None:
            codes = list(dict.fromkeys(list(POPULAR) + list(extra or [])))
            leagues = [LEAGUES[c] for c in codes if c in LEAGUES]
            name = "주요 리그"
        elif found[0] == "league":
            leagues, name = [found[1]], found[1].name
        elif found[0] == "sport":
            leagues, name = found[1], SPORT_KO.get(found[1][0].sport, "") if found[1] else ""
        else:
            team = found[1]
            leagues, name = [LEAGUES[team.league]], team.ko
        ok, missing = self._usable(leagues)
        if not ok:
            return f"{esc(name)} 정보는 아직 볼 수 없어요. {NOT_READY if any(lg.korean for lg in missing) else ''}".strip()
        head = "🔴 <b>지금 진행 중</b>" if live_only else f"📅 <b>{_day_label(d, today)} {esc(name)} 경기</b>"
        out, failed, total = [head], [], 0
        for lg, games in await self._days(ok, d):
            if games is None:
                failed.append(lg.name)
                continue
            if team:
                games = [g for g in games if same_team(g.home, team.src, team.league) or same_team(g.away, team.src, team.league)]
            if live_only:
                games = [g for g in games if g.state == "in"]
            if not games:
                continue
            total += len(games)
            out.append(f"\n{fmt.emoji(lg.code)} <b>{esc(lg.name)}</b>")
            out += [fmt.line(g) for g in games[:PER_LEAGUE]]
            if len(games) > PER_LEAGUE:
                out.append(f"… 외 {len(games) - PER_LEAGUE}경기")
        if not total:
            if failed and len(failed) == len(ok):
                return "경기 정보를 가져오지 못했어요. 잠시 후 다시 해주세요."
            msg = "지금 진행 중인 경기가 없어요." if live_only else f"{_day_label(d, today)} {esc(name)} 경기가 없어요."
            if team and not live_only:
                msg += f"\n다음 경기는 <code>.스포츠 팀 {esc(team.ko)}</code>"
            out = [msg]
        if failed and total:
            out.append(f"\n(못 가져온 리그: {esc(', '.join(failed))})")
        if missing and found is None:
            out.append(f"\n<i>{NOT_READY}</i>")
        return "\n".join(out)

    # ── 순위 ───────────────────────────────────────────
    async def standings_text(self, query: str) -> str:
        found = resolve(query) if query else None
        lg = found[1] if found and found[0] == "league" else LEAGUES.get(found[1].league) if found and found[0] == "team" else None
        if not lg:
            return f"어느 리그 순위요? <code>.스포츠 순위 EPL</code>\n{EXAMPLES}"
        if not self.feed.available(lg):
            return f"{esc(lg.name)} 순위는 아직 볼 수 없어요." + (f" {NOT_READY}" if lg.korean else "")
        if lg.sport == "mma":
            return "UFC 는 순위표가 없어요. 대회 일정은 <code>.스포츠 UFC</code>"
        try:
            rows = await self.feed.standings(lg)
        except SportsError as e:
            return esc(str(e))
        return fmt.standings_text(lg.code, rows)

    # ── 팀 ────────────────────────────────────────────
    async def team_text(self, query: str) -> str:
        team = find_team(query) if query else None
        if not team:
            found = resolve(query) if query else None
            if found and found[0] != "team":
                return await self.games_text(query)
            return f"'{esc(query)}' 팀을 못 찾았어요. 한국어·영어 이름 둘 다 돼요 (예: 토트넘, 레알, 다저스, 레이커스, LG)."
        lg = LEAGUES[team.league]
        if not self.feed.available(lg):
            return f"{esc(team.ko)} ({esc(lg.name)}) 경기는 아직 볼 수 없어요." + (f" {NOT_READY}" if lg.korean else "")
        try:
            games = await self.feed.team_games(lg, team.src)
        except SportsError as e:
            return esc(str(e))
        now = self.feed.clock()
        past = [g for g in games if g.state in ("post", "cancel", "postponed") and g.start <= now][-3:]
        live = [g for g in games if g.state == "in"]
        nxt = [g for g in games if g.state == "pre" and g.start >= now - 3600][:3]
        out = [f"{fmt.emoji(lg.code)} <b>{esc(team.ko)}</b> ({esc(lg.name)})"]
        if live:
            out += ["🔴 지금"] + [fmt.line(g) for g in live]
        if past:
            out += ["🏁 최근 결과"] + [fmt.line(g, with_date=True) for g in reversed(past)]
        if nxt:
            out += ["📅 다음 경기"] + [fmt.line(g, with_date=True) for g in nxt]
        if len(out) == 1:
            out.append("가까운 경기 정보가 없어요.")
        return "\n".join(out)

    # ── 구독 ───────────────────────────────────────────
    def _target(self, query: str) -> tuple[str, str, str] | str:
        """(리그, 팀 소스 이름 또는 '', 표시) 또는 오류 글."""
        q = re.sub(r"^(리그|팀)\s+", "", (query or "").strip())
        found = resolve(q)
        if not found:
            return f"'{esc(q)}' 리그·팀을 못 찾았어요.\n{EXAMPLES}" if q else "무엇을 구독할까요? <code>.스포츠 구독 EPL</code> / <code>.스포츠 구독 토트넘</code>"
        if found[0] == "sport":
            names = ", ".join(lg.name for lg in found[1] if self.feed.available(lg))
            return f"종목 말고 리그를 골라주세요: {esc(names) or '(지금 볼 수 있는 리그 없음)'}"
        if found[0] == "league":
            lg = found[1]
            return lg.code, "", lg.name
        t = found[1]
        return t.league, t.src, f"{t.ko} ({LEAGUES[t.league].name})"

    async def follow(self, chat_id: int, query: str, by: int | None) -> str:
        t = self._target(query)
        if isinstance(t, str):
            return t
        code, team, label = t
        lg = LEAGUES[code]
        if not self.feed.available(lg):
            return f"{esc(lg.name)} 알림은 아직 준비 중이에요." + (f" {NOT_READY}" if lg.korean else "")
        r = await self.sp.alerts.follow(chat_id, code, team, label, by)
        if r == "exists":
            return f"이미 {esc(label)} 알림을 받고 있어요."
        if r == "full":
            return f"구독은 방마다 {MAX_FOLLOWS}개까지예요. <code>.스포츠 해제 이름</code> 으로 정리해 주세요."
        s = await self.sp.db.get_settings(chat_id)
        return (f"🔔 {esc(label)} 경기 알림을 켰어요.\n"
                f"알림 종류: {LEVELS[s.get('sports_alerts', 'goals')][0]} · 조용한 시간: {QUIET.get(s.get('sports_quiet', '01-07'), '')}\n"
                "바꾸기: 1:1 메뉴 [⚽ 스포츠 알림]")

    async def unfollow(self, chat_id: int, query: str) -> str:
        q = (query or "").strip()
        rows = await self.sp.alerts.follows(chat_id)
        if q in ("전부", "모두", "all"):
            for r in rows:
                await self.sp.alerts.unfollow(chat_id, r["league"], r["team"])
            return f"🔕 스포츠 구독 {len(rows)}개를 모두 껐어요."
        t = self._target(q)
        if not isinstance(t, str):
            if await self.sp.alerts.unfollow(chat_id, t[0], t[1]):
                return f"🔕 {esc(t[2])} 알림을 껐어요."
        for r in rows:     # 저장된 이름 그대로 적은 경우
            if q and q.lower() in (r["label"].lower(), r["team"].lower()):
                await self.sp.alerts.unfollow(chat_id, r["league"], r["team"])
                return f"🔕 {esc(r['label'])} 알림을 껐어요."
        return "구독 중인 게 아니에요. <code>.스포츠 목록</code> 으로 확인해 보세요."

    async def follows_text(self, chat_id: int) -> str:
        rows = await self.sp.alerts.follows(chat_id)
        s = await self.sp.db.get_settings(chat_id)
        if not rows:
            return ("🔔 스포츠 알림 구독이 없어요.\n관리자: <code>.스포츠 구독 EPL</code> · <code>.스포츠 구독 토트넘</code> "
                    "또는 1:1 메뉴 [⚽ 스포츠 알림]")
        state = "켜짐" if s.get("sports_enabled") else "꺼짐 (🧩 기능에서 켜기)"
        return (f"🔔 <b>스포츠 알림</b> ({state})\n" + "\n".join(f"• {esc(r['label'])}" for r in rows)
                + f"\n알림 종류: {LEVELS[s.get('sports_alerts', 'goals')][0]} · 조용한 시간: {QUIET.get(s.get('sports_quiet', '01-07'), '')}")


HELP = ("⚽ <b>스포츠</b>\n"
        "<code>.스포츠</code> 오늘 주요 경기 · <code>.스포츠 오늘 EPL</code> · <code>.스포츠 내일 야구</code>\n"
        "<code>.스포츠 라이브</code> 진행 중 · <code>.스포츠 순위 라리가</code> · <code>.스포츠 팀 토트넘</code>\n"
        "관리자: <code>.스포츠 구독 EPL</code> / <code>구독 다저스</code> · <code>.스포츠 해제 EPL</code> · <code>.스포츠 목록</code>\n"
        "알림 종류: <code>.스포츠 알림종류 결과만|시작|골|점수</code> · 조용한 시간: <code>.스포츠 조용 01-07|끔</code>")


async def command(svc, chat_id: int, user_id: int, is_admin: bool, args: list[str]) -> str:
    """'.스포츠 …' → 답 글 (HTML)."""
    sp = svc.sports
    if sp is None:
        return "스포츠 기능이 꺼져 있어요."
    ui = UI(sp)
    sub = args[0] if args else "오늘"
    rest = " ".join(args[1:]).strip()
    today = ui.today()
    admin_only = "알림 설정은 관리자만 할 수 있어요."
    try:
        if sub in ("도움", "도움말", "help", "?"):
            return HELP
        d = parse_day(sub, today)
        if d is not None:
            follows = [r["league"] for r in await sp.alerts.follows(chat_id)] if chat_id < 0 else []
            return await ui.games_text(rest, d, extra=follows if not rest else None)
        if sub in ("라이브", "live", "지금", "진행", "진행중"):
            follows = [r["league"] for r in await sp.alerts.follows(chat_id)] if chat_id < 0 else []
            return await ui.games_text(rest, live_only=True, extra=follows)
        if sub in ("순위", "순위표", "standings"):
            return await ui.standings_text(rest)
        if sub in ("팀", "다음", "결과", "team", "next", "last"):
            return await ui.team_text(rest) if rest else "사용법: <code>.스포츠 팀 토트넘</code>"
        if sub in ("구독", "알림", "sub", "추가"):
            if not is_admin:
                return admin_only
            if chat_id > 0:
                return "알림 구독은 그룹방에서 해주세요 (또는 1:1 메뉴 [⚽ 스포츠 알림])."
            return await ui.follow(chat_id, rest, user_id)
        if sub in ("해제", "구독해제", "unsub", "끄기", "삭제"):
            if not is_admin:
                return admin_only
            return await ui.unfollow(chat_id, rest)
        if sub in ("목록", "list", "구독목록"):
            return await ui.follows_text(chat_id)
        if sub in ("알림종류", "종류"):
            return await _set(svc, chat_id, is_admin, "sports_alerts", rest,
                              "알림 종류: " + " / ".join(v[0] for v in LEVELS.values()))
        if sub in ("조용", "조용한시간", "quiet"):
            return await _set(svc, chat_id, is_admin, "sports_quiet", rest.replace("~", "-"),
                              "조용한 시간: " + " / ".join(QUIET) + " (한국 시각)")
        # '.스포츠 EPL' · '.스포츠 토트넘' 처럼 바로 이름
        whole = " ".join(args)
        found = resolve(whole)
        if found and found[0] == "team":
            return await ui.team_text(whole)
        return await ui.games_text(whole)
    except SportsError as e:
        return esc(str(e))


async def _set(svc, chat_id: int, is_admin: bool, key: str, raw: str, usage: str) -> str:
    from ..settings import coerce, render
    if not is_admin:
        return "알림 설정은 관리자만 할 수 있어요."
    if not raw:
        return usage
    try:
        value = coerce(key, raw)
    except ValueError:
        return usage
    await svc.db.set_setting(chat_id, key, value)
    return f"✅ {render(key, value)} 로 바꿨어요."
