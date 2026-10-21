"""근거(영수증): 제재하거나 '쟤가 ~했다' 단정할 때 기록에서 '원문+시각'을 뽑아 멤버가 반박 못하게.

오너 2026-10-21 '거짓말했으면 근거 제시해서 아 그렇구나 하게'. 소담 원칙 그대로 **사실은 코드가 기록에서만** (AI 비용 0).
링크·지갑은 가려서 보여 줌(도메인만 `bit[.]ly`) — 방에서 다시 눌리거나 재도배되지 않게. 멤버 글을 읽으니 ctx.tainted.
"""
import re
import time

from .security import strip_unsafe
from .util import fmt_time

LOOKBACK_SEC = 6 * 3600   # 최근 6시간 글에서만 근거 (오래된 건 '지금 왜' 와 안 맞음)
SCAN = 40                 # 이 사람 최근 글 몇 개를 훑나
MAX_LINES = 3             # 영수증은 짧게 (3줄 안)
QUOTE = 24                # 원문 인용 길이

_URL = re.compile(r"https?://([^\s/]+)[^\s]*", re.I)
_TME = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(\S+)", re.I)


def _mask(text: str) -> str:
    """링크는 '눌리지 않게' 도메인만 (bit[.]ly), t.me 초대는 't[.]me/...' — 근거로는 '링크 올림'이 보여야 함."""
    text = _TME.sub(lambda m: "t[.]me/" + m.group(1)[:12], text)
    text = _URL.sub(lambda m: m.group(1).replace(".", "[.]") + " 링크", text)
    return text


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


async def receipts(svc, chat_id: int, user_id: int, *, max_lines: int = MAX_LINES) -> str:
    """이 사람의 최근 기록에서 근거 줄(원문+시각)을 뽑는다. 없으면 '' (그러면 소담은 단정하지 않음).

    - 도배: 같은 글을 여러 번 → '14:03~14:05 같은 글 3번: "..."'
    - 링크: 링크 들어간 글 → 시각 + 가린 원문
    - 그 밖: 가장 최근 글 몇 개 → 시각 + 원문 (따질 때 '너 이렇게 말했잖아')
    - 누적: mod_log 경고/뮤트 횟수 → '전에 경고 2번'
    """
    db = svc.db
    tz = svc.cfg.tz
    now = int(time.time())
    rows = await db._all(
        "SELECT msg_id, text, ts FROM messages WHERE chat_id=? AND user_id=? AND is_bot=0 AND flagged=0 "
        "AND ts>=? ORDER BY id DESC LIMIT ?",
        (chat_id, user_id, now - LOOKBACK_SEC, SCAN))
    lines: list[str] = []
    used_ts: set[int] = set()

    # ① 도배 — 같은 글(정리한 글자) 2번 이상
    groups: dict[str, list] = {}
    for r in rows:
        key = _norm(r["text"] or "")
        if len(key) >= 2:
            groups.setdefault(key, []).append(r)
    dup = max((g for g in groups.values() if len(g) >= 2), key=len, default=None)
    if dup:
        first, last = dup[-1], dup[0]   # rows 는 최신 DESC → 끝이 먼저
        span = fmt_time(first["ts"], tz, "%H:%M")
        if last["ts"] != first["ts"]:
            span += "~" + fmt_time(last["ts"], tz, "%H:%M")
        lines.append(f"{span} 같은 글 {len(dup)}번: “{_mask((first['text'] or '')[:QUOTE])}”")
        used_ts.update(r["ts"] for r in dup)

    # ② 링크 글 (도배로 안 잡힌 것)
    for r in rows:
        if len(lines) >= max_lines:
            break
        if r["ts"] in used_ts:
            continue
        if _URL.search(r["text"] or "") or _TME.search(r["text"] or ""):
            lines.append(f"{fmt_time(r['ts'], tz, '%H:%M')} “{_mask((r['text'] or '')[:QUOTE])}”")
            used_ts.add(r["ts"])

    # ③ 아무것도 특별한 게 없으면 '네가 이렇게 말했잖아' 용으로 가장 최근 글 1~2개
    if not lines:
        for r in rows[:2]:
            t = (r["text"] or "").strip()
            if t:
                lines.append(f"{fmt_time(r['ts'], tz, '%H:%M')} “{_mask(t[:QUOTE])}”")

    # ④ 누적 제재 (mod_log)
    warns = await db._one(
        "SELECT COUNT(*) c FROM mod_log WHERE chat_id=? AND target_id=? AND action='warn' AND ts>=?",
        (chat_id, user_id, now - 90 * 86400))
    if warns and warns["c"]:
        lines.append(f"전에 경고 {warns['c']}번")

    if not lines:
        return ""
    body = strip_unsafe("\n".join("· " + ln for ln in lines[:max_lines + 1]))
    return "📋 근거 (기록):\n" + body
