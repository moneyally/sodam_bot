"""사람이 읽는 문구 (한국 시각·한국어 팀 이름·짧게). HTML 로 보냄 → 바깥 글자는 전부 esc."""
from __future__ import annotations

from datetime import datetime

from ..util import esc
from .leagues import LEAGUES, SPORT_EMOJI, ko_name
from .providers import KST, Game, Row

STATE_KO = {"cancel": "취소", "postponed": "연기", "suspended": "중단"}
AWAY_FIRST = {"baseball", "basketball", "hockey"}   # 미국·한국 야구·농구 표기는 원정 먼저


def hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%H:%M")


def mdhm(ts: int) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%m/%d %H:%M")


def sides(g: Game) -> tuple[tuple[str, int | None], tuple[str, int | None]]:
    home, away = (ko_name(g.home, g.league), g.home_score), (ko_name(g.away, g.league), g.away_score)
    lg = LEAGUES.get(g.league)
    return (away, home) if lg and lg.sport in AWAY_FIRST else (home, away)


def matchup(g: Game, score: bool = True) -> str:
    """'토트넘 1-0 아스널' / '토트넘 vs 아스널' (점수 없으면)."""
    if g.title and not (g.home or g.away):
        return esc(g.title)
    (a, sa), (b, sb) = sides(g)
    if score and g.scored:
        return f"{esc(a)} {sa}-{sb} {esc(b)}"
    return f"{esc(a)} vs {esc(b)}"


def line(g: Game, with_date: bool = False) -> str:
    when = mdhm(g.start) if with_date else hhmm(g.start)
    if g.state == "in":
        return f"🔴 {esc(g.detail or '진행 중')} {matchup(g)}"
    if g.state == "post":
        return f"✅ 종료 {matchup(g)}" + (f" ({mdhm(g.start)[:5]})" if with_date else "")
    if g.state in STATE_KO:
        return f"{when} {matchup(g, score=g.state == 'suspended')} ({STATE_KO[g.state]})"
    return f"{when} {matchup(g, score=False)}"


def tag(g: Game) -> str:
    lg = LEAGUES.get(g.league)
    return f"[{lg.name}]" if lg else ""


def emoji(code: str) -> str:
    lg = LEAGUES.get(code)
    return SPORT_EMOJI.get(lg.sport, "🏟️") if lg else "🏟️"


def standings_text(code: str, rows: list[Row], limit: int = 20) -> str:
    lg = LEAGUES[code]
    out = [f"{emoji(code)} <b>{esc(lg.name)} 순위</b>"]
    groups = len({r.group for r in rows})
    group = None
    shown = 0
    per_group = limit if groups <= 1 else max(5, limit // 2)
    count: dict[str, int] = {}
    for r in rows:
        count[r.group] = count.get(r.group, 0) + 1
        if count[r.group] > per_group or shown >= limit * 2:
            continue
        if r.group != group and groups > 1:
            out.append(f"<i>{esc(r.group)}</i>")
        group = r.group
        if lg.sport == "soccer":
            rec = f"{r.wins or 0}승 {r.draws or 0}무 {r.losses or 0}패"
        else:
            rec = f"{r.wins or 0}승 {r.losses or 0}패" + (f" {r.draws}무" if r.draws else "")
        extra = ""
        if lg.sport == "soccer" and r.points is not None:
            extra = f" · {int(r.points)}점"
        elif r.pct:
            extra = f" · 승률 {esc(r.pct)}" + (f" · {esc(r.behind)}G" if r.behind not in ("", "-", "0", "0.0") else "")
        out.append(f"{r.rank}. {esc(ko_name(r.team, code))} {rec}{extra}")
        shown += 1
    return "\n".join(out)
