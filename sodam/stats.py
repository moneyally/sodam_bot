"""채팅 집계 문구. 명령어·AI 도구·일일 리포트가 같이 쓴다."""
import re
from datetime import datetime

from .db import DB, CountRule, count_filter
from .settings import register_setting
from .util import display_name, esc, fmt_time, period_range

MEDALS = ["🥇", "🥈", "🥉"]

# 2026-10-10 뉴월드 '채팅 집계 5글자부터만' — 'ㅋㅋ'·'ㅇㅇ' 로 순위 올리기 막기. 띄어쓰기 빼고 셈, 0 = 전부.
register_setting("chat_min_chars", 0, "채팅 집계 최소 글자 수 (0=전부)", range_=(0, 50))
# 2026-10-11 FOX '채팅집계 … 도배 X' — 같은 사람이 같은 글을 10분 안에 또 쓴 건 1번만 셈 (설계 docs/SPORTS_ENGAGE_DESIGN.md §3 F).
register_setting("chat_no_repeat", False, "채팅 집계 도배 빼기")


# 2026-10-26 뉴월드 '새벽 1~7시 채팅 집계 X' — 이 시간대(한국 시각) 글은 순위·통계·리포트·미니앱에서 안 셈. '' = 없음.
_QUIET = re.compile(r"\s*(\d{1,2})\s*[-~]\s*(\d{1,2})\s*")


def parse_quiet(v) -> "tuple[int, int] | None":
    """'1-7' → (1, 7) (한국 시각, 끝 미포함). 빈 값·형식 오류·같은 시각 = None (제외 없음)."""
    m = _QUIET.fullmatch(v) if isinstance(v, str) else None
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if 0 <= a <= 23 and 0 <= b <= 23 and a != b else None


def valid_quiet(v) -> str:
    """설정 검사: 'off'(끔) 또는 '1-7' 꼴. 빈 값·'없음'·'끔'·'0' = off. parse_quiet 가 'off'·빈 값은 None 으로 읽음."""
    s = str(v or "").strip()
    if not s or s in ("없음", "끔", "off", "0"):
        return "off"
    q = parse_quiet(s)
    if not q:
        raise ValueError("시간대는 '1-7' 처럼 써 주세요 (0~23시, 끝 시각 미포함, 같은 시각 X)")
    return f"{q[0]}-{q[1]}"


register_setting("chat_quiet_hours", "", "채팅 집계 제외 시간대 (예: 1-7, 빈 값=없음)", validator=valid_quiet)


async def min_chars(db: DB, chat_id: int) -> CountRule:
    """이 방 채팅 집계 거르기 (최소 글자 수 + 도배 빼기 + 제외 시간대)."""
    s = await db.get_settings(chat_id)
    try:
        n = max(0, int(s.get("chat_min_chars") or 0))
    except (TypeError, ValueError):
        n = 0
    return CountRule(n, bool(s.get("chat_no_repeat")), parse_quiet(s.get("chat_quiet_hours")))


def _rule(r: CountRule) -> str:
    bits = ([f"{r.min_chars}글자 이상만"] if r.min_chars > 0 else []) + (["도배 빼고"] if r.no_repeat else []) \
        + ([f"{r.quiet[0]}~{r.quiet[1]}시 빼고"] if r.quiet else [])
    return f" ({' · '.join(bits)})" if bits else ""


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
                         + count_filter(), (chat_id, user_id, since, end, *m.args))
    n = int(mine["n"] if mine else 0)
    people = await db._one("SELECT COUNT(DISTINCT user_id) n FROM messages WHERE chat_id=? AND ts>=? AND ts<? AND is_bot=0 "
                           + count_filter(), (chat_id, since, end, *m.args))
    if not n:
        return f"📊 {esc(name)} — {label} 채팅 0개{_rule(m)} (기록 기준, 참여 {people['n'] if people else 0}명)"
    above = await db._one("SELECT COUNT(*) n FROM (SELECT user_id, COUNT(*) c FROM messages WHERE chat_id=? AND ts>=? AND ts<? "
                          "AND is_bot=0 " + count_filter() + " GROUP BY user_id) WHERE c>?", (chat_id, since, end, *m.args, n))
    return f"📊 {esc(name)} — {label} 채팅 {n}개{_rule(m)} · {int(above['n']) + 1}위 (참여 {people['n']}명 중)"


async def over_text(db: DB, chat_id: int, tz, period: str, threshold: int) -> str:
    """'오늘 N개 넘는 사람 몇 명' — 그 기간 N개 이상 친 사람 수 + 참여 + 해당하는 사람 이름 (코드 집계, AI·run_code 불필요).
    2026-10-20 뉴월드 '100개 넘는 사람 몇명' → 소담이 run_code 로 세려다 멤버 권한에 막힘 → chat_stats 로 바로."""
    since, until, label = period_range(period, tz)
    end = until if until is not None else 1 << 62
    thr = max(1, int(threshold))
    m = await min_chars(db, chat_id)
    row = await db._one("SELECT COUNT(*) n FROM (SELECT user_id, COUNT(*) c FROM messages WHERE chat_id=? AND ts>=? "
                        "AND ts<? AND is_bot=0 " + count_filter() + " GROUP BY user_id) WHERE c>=?",
                        (chat_id, since, end, *m.args, thr))
    cnt = int(row["n"]) if row else 0
    ppl = await db._one("SELECT COUNT(DISTINCT user_id) n FROM messages WHERE chat_id=? AND ts>=? AND ts<? AND is_bot=0 "
                        + count_filter(), (chat_id, since, end, *m.args))
    total = int(ppl["n"]) if ppl else 0
    if cnt == 0:
        return f"📊 {label} 채팅 {thr}개 이상 친 사람은 없어요{_rule(m)} (참여 {total}명)"
    rows = await db.top_chatters(chat_id, since, min(cnt, 10), until, m)
    names = ", ".join(f"{esc(_name(r))}({r['n']})" for r in rows if r["n"] >= thr)
    tail = f" — {names}{' …' if cnt > 10 else ''}" if names else ""
    return f"📊 {label} 채팅 {thr}개 이상 친 사람 {cnt}명{_rule(m)} (참여 {total}명 중){tail}"


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
