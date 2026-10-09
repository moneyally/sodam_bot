"""채팅 집계 문구. 명령어·AI 도구·일일 리포트가 같이 쓴다."""
from datetime import datetime

from .db import DB, MIN_CHARS
from .settings import register_setting
from .util import display_name, esc, fmt_time, period_range

MEDALS = ["🥇", "🥈", "🥉"]

# 2026-10-10 뉴월드 '채팅 집계 5글자부터만' — 'ㅋㅋ'·'ㅇㅇ' 로 순위 올리기 막기. 띄어쓰기 빼고 셈, 0 = 전부.
register_setting("chat_min_chars", 0, "채팅 집계 최소 글자 수 (0=전부)", range_=(0, 50))


async def min_chars(db: DB, chat_id: int) -> int:
    try:
        return max(0, int((await db.get_settings(chat_id)).get("chat_min_chars") or 0))
    except (TypeError, ValueError):
        return 0


def _rule(n: int) -> str:
    return f" ({n}글자 이상만)" if n > 0 else ""


def _name(row) -> str:
    return display_name(row["first_name"], None, row["username"])


async def ranking_text(db: DB, chat_id: int, tz, period: str = "오늘", limit: int = 10) -> str:
    since, until, label = period_range(period, tz)
    n_min = await min_chars(db, chat_id)
    rows = await db.top_chatters(chat_id, since, limit, until, n_min)
    if not rows:
        return f"📊 {label} 채팅 기록이 아직 없어요." + _rule(n_min)
    lines = [f"📊 {label} 채팅 랭킹" + _rule(n_min)]
    for i, r in enumerate(rows):
        badge = MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{badge} {esc(_name(r))} — {r['n']}개")
    return "\n".join(lines)


async def member_text(db: DB, chat_id: int, tz, period: str, user_id: int, name: str) -> str:
    """한 사람의 그 기간 채팅 수와 순위 — 랭킹(상위 몇 명)에 없다고 0개가 아님 (2026-10-09 '이분 채팅집계' → 소담이 '0개'로 지어냄)."""
    since, until, label = period_range(period, tz)
    end = until if until is not None else 1 << 62
    m = await min_chars(db, chat_id)
    mine = await db._one("SELECT COUNT(*) n FROM messages WHERE chat_id=? AND user_id=? AND ts>=? AND ts<? AND is_bot=0 "
                         + MIN_CHARS, (chat_id, user_id, since, end, m, m))
    n = int(mine["n"] if mine else 0)
    people = await db._one("SELECT COUNT(DISTINCT user_id) n FROM messages WHERE chat_id=? AND ts>=? AND ts<? AND is_bot=0 "
                           + MIN_CHARS, (chat_id, since, end, m, m))
    if not n:
        return f"📊 {esc(name)} — {label} 채팅 0개{_rule(m)} (기록 기준, 참여 {people['n'] if people else 0}명)"
    above = await db._one("SELECT COUNT(*) n FROM (SELECT user_id, COUNT(*) c FROM messages WHERE chat_id=? AND ts>=? AND ts<? "
                          "AND is_bot=0 " + MIN_CHARS + " GROUP BY user_id) WHERE c>?", (chat_id, since, end, m, m, n))
    return f"📊 {esc(name)} — {label} 채팅 {n}개{_rule(m)} · {int(above['n']) + 1}위 (참여 {people['n']}명 중)"


async def summary_text(db: DB, chat_id: int, tz, period: str = "오늘") -> str:
    since, until, label = period_range(period, tz)   # '어제' = 어제 하루만 (오늘 것 안 섞임)
    m = await min_chars(db, chat_id)
    totals = await db.chat_totals(chat_id, since, until, m)
    offset = int(datetime.now(tz).utcoffset().total_seconds())
    hours = await db.hourly_counts(chat_id, since, offset, until, m)
    top = await db.top_chatters(chat_id, since, 3, until, m)
    lines = [
        f"📈 {label} 방 통계" + _rule(m),
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
