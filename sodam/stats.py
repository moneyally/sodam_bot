"""채팅 집계 문구. 명령어·AI 도구·일일 리포트가 같이 쓴다."""
from datetime import datetime

from .db import DB
from .util import display_name, esc, fmt_time, period_range

MEDALS = ["🥇", "🥈", "🥉"]


def _name(row) -> str:
    return display_name(row["first_name"], None, row["username"])


async def ranking_text(db: DB, chat_id: int, tz, period: str = "오늘", limit: int = 10) -> str:
    since, until, label = period_range(period, tz)
    rows = await db.top_chatters(chat_id, since, limit, until)
    if not rows:
        return f"📊 {label} 채팅 기록이 아직 없어요."
    lines = [f"📊 {label} 채팅 랭킹"]
    for i, r in enumerate(rows):
        badge = MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{badge} {esc(_name(r))} — {r['n']}개")
    return "\n".join(lines)


async def summary_text(db: DB, chat_id: int, tz, period: str = "오늘") -> str:
    since, until, label = period_range(period, tz)   # '어제' = 어제 하루만 (오늘 것 안 섞임)
    totals = await db.chat_totals(chat_id, since, until)
    offset = int(datetime.now(tz).utcoffset().total_seconds())
    hours = await db.hourly_counts(chat_id, since, offset, until)
    top = await db.top_chatters(chat_id, since, 3, until)
    lines = [
        f"📈 {label} 방 통계",
        f"메시지 {totals['messages']}개 · 참여 {totals['users']}명",
    ]
    if hours:
        peak = max(hours, key=lambda r: r["n"])
        lines.append(f"가장 활발한 시간: {peak['hour']}시 ({peak['n']}개)")
    if top:
        lines.append("TOP3: " + ", ".join(f"{esc(_name(r))}({r['n']})" for r in top))
    return "\n".join(lines)


async def search_text(db: DB, chat_id: int, tz, keyword: str, days: int = 7, limit: int = 10, svc=None) -> str:
    """svc 를 주면 뜻이 비슷한 글도 (sodam/semsearch.py, ≈ 표시)."""
    since = 0 if days <= 0 else int(datetime.now(tz).timestamp()) - days * 86400
    if svc is not None:
        from . import semsearch
        rows = await semsearch.search(svc, chat_id, keyword, since, limit)
    else:
        rows = await db.search_messages(chat_id, keyword, since, limit)
    if not rows:
        return f"🔎 '{esc(keyword)}' 가 들어간 메시지를 못 찾았어요. (2글자 이상, 여러 낱말은 띄어서)"
    lines = [f"🔎 '{esc(keyword)}' 검색 결과 (최근 {len(rows)}개)"]
    for r in rows:
        text = r["text"].replace("\n", " ")
        mark = "≈ " if isinstance(r, dict) and r.get("semantic") else ""   # 뜻으로만 찾은 글
        lines.append(f"{mark}[{fmt_time(r['ts'], tz)}] {esc(_name(r))}: {esc(text[:80])}")
    return "\n".join(lines)
