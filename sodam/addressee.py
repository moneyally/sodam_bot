"""'누구 얘기인지' 단서 모으기. 코드는 단서만 모으고(★ 강도), 판단은 AI 가 한다.

단서: 답장 대상·직접 태그 ★★★ / 요청에 나온 이름·방금 입장 ★★. (방금 말한 사람은 대화 기록에 이미 있어서 후보로 넣지 않음 —
넣으면 '대표님' 만 있어도 그 사람을 고르게 됨) 섞여 있는 말투 변경 부탁도 알려준다.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from .util import display_name, name_key

if TYPE_CHECKING:
    from .services import Services

HONORIFICS = ("대표님", "사장님", "실장님", "이사님", "회장님", "팀장님", "부장님", "형님", "누님", "선생님",
              "대표", "사장", "실장", "이사", "회장", "팀장", "부장", "님", "씨", "형", "누나", "언니", "오빠")
GENERIC = {"대표", "사장", "실장", "이사", "회장", "팀장", "부장", "형", "누나", "언니", "오빠", "선생", "여기", "다들",
           "모두", "방사람", "방사람들", "새로", "오신", "분", "여러분", "소담", "소담아", "소담이"}
MAX_LINES = 8
RECENT_JOIN_MIN = 60
NAME_SCAN_LIMIT = 400
_STYLE_ASK = re.compile(r"(?:^|\s)[./]\s?(?:말투|style)\s+(\S+)|말투(?:를|는)?\s*(\S+?)(?:로|으로)\s*(?:바꿔|해|변경)")
_WORD = re.compile(r"[0-9A-Za-z가-힣_]{2,}")
# 지시어 ('걔 뮤트해', '그 사람 누구야') → 답장한 글의 작성자, 없으면 바로 전 대화(ai_turns)에서 마지막으로 나온 사람
_PRONOUN = re.compile(r"(?<![가-힣])(그\s?사람|저\s?사람|이\s?사람|그\s?분|이\s?분|저\s?분|걔|쟤|얘)"
                      r"(?:은|는|이|가|을|를|한테|에게|랑|이랑|하고|도|의|좀|께)?(?![가-힣])")
PRONOUN_TURN_MIN = 30     # 이 시간 안의 대화만
PRONOUN_TURNS = 4


JOSA = ("한테서", "한테", "에게", "께서", "께", "이랑", "랑", "하고", "처럼", "까지", "부터", "은", "는", "이", "가",
        "을", "를", "의", "도", "와", "과")


def _core(word: str) -> str:
    """'철수형한테' → '철수', '하늘대표님께' → '하늘' (조사 → 호칭 순서로 뗌)."""
    w = word.strip().lstrip("@")
    for _ in range(3):
        before = w
        for suffix in (*JOSA, *HONORIFICS):
            if w.endswith(suffix) and len(w) > len(suffix) + 1:
                w = w[: -len(suffix)]
                break
        if w == before:
            break
    return w


def _name(first, last, username) -> str:
    return display_name(first, last, username)


def _squash(s: str) -> str:
    """이름 비교용 (투명 글자·이모지·꾸밈 글꼴 정리 — util.name_key)."""
    return name_key(s)


async def collect(svc: Services, bot, msg, chat_id: int, caller, request: str) -> list[str]:
    """AI 에게 넘길 단서 줄들 (최대 8줄). 그룹에서만. 실패해도 빈 목록."""
    if chat_id > 0:
        return []
    now = int(time.time())
    db = svc.db
    cands: dict[int, dict] = {}   # user_id → {"name", "score", "why": [..]}

    def add(uid: int, name: str, score: int, why: str) -> None:
        if uid in (caller.id, getattr(bot, "id", None)):
            return
        c = cands.setdefault(uid, {"name": name, "score": 0, "why": []})
        c["score"] = max(c["score"], score)
        if why not in c["why"]:
            c["why"].append(why)

    notes: list[str] = []
    # 1) 답장 대상 ★★★
    r = getattr(msg, "reply_to_message", None)
    if r is not None and getattr(r, "from_user", None):
        if r.from_user.id == getattr(bot, "id", None):
            notes.append("이 요청은 소담의 이전 메시지에 한 답장이다.")
        elif not r.from_user.is_bot:
            add(r.from_user.id, _name(r.from_user.first_name, r.from_user.last_name, r.from_user.username), 3,
                "이 요청이 답장한 메시지의 작성자")
    # 2) 직접 태그 ★★★
    for ent in [*(getattr(msg, "entities", None) or ())]:
        if ent.type == "text_mention" and getattr(ent, "user", None) and not ent.user.is_bot:
            add(ent.user.id, _name(ent.user.first_name, ent.user.last_name, ent.user.username), 3, "요청에서 직접 태그함")
    for uname in re.findall(r"@([A-Za-z0-9_]{3,32})", request):
        row = await db._one("SELECT u.user_id, u.first_name, u.last_name, u.username FROM users u JOIN members m "
                            "ON m.user_id=u.user_id AND m.chat_id=? WHERE u.username=? COLLATE NOCASE AND u.is_bot=0",
                            (chat_id, uname))
        if row:
            add(row["user_id"], _name(row["first_name"], row["last_name"], row["username"]), 3, f"요청에서 @{uname} 로 태그함")
    # 3) 요청에 나온 이름 ★★ (호칭 떼고 이름·아이디와 비교)
    words = {_core(w) for w in _WORD.findall(request)}
    words = {w for w in words if len(w) >= 2 and w not in GENERIC and w.lower() not in GENERIC}
    if words:
        rows = await db._all(
            "SELECT u.user_id, u.first_name, u.last_name, u.username FROM members m JOIN users u ON u.user_id=m.user_id "
            "WHERE m.chat_id=? AND u.is_bot=0 AND COALESCE(m.last_seen,0) > ? ORDER BY m.last_seen DESC LIMIT ?",
            (chat_id, now - 90 * 86400, NAME_SCAN_LIMIT))
        for row in rows:
            full = _squash(f"{row['first_name'] or ''}{row['last_name'] or ''}")
            uname = (row["username"] or "").lower()
            hit = next((w for w in words if (_squash(w) and (_squash(w) in full or (uname and _squash(w) in uname)))), None)
            if hit:
                add(row["user_id"], _name(row["first_name"], row["last_name"], row["username"]), 2,
                    f"요청의 '{hit}' 와 이름이 겹침")
    # 4) 방금 들어온 사람 ★★
    for row in await db._all(
            "SELECT u.user_id, u.first_name, u.last_name, u.username, m.joined_at FROM members m JOIN users u "
            "ON u.user_id=m.user_id WHERE m.chat_id=? AND u.is_bot=0 AND m.joined_at > ? ORDER BY m.joined_at DESC LIMIT 5",
            (chat_id, now - RECENT_JOIN_MIN * 60)):
        add(row["user_id"], _name(row["first_name"], row["last_name"], row["username"]), 2,
            f"{max(1, (now - row['joined_at']) // 60)}분 전에 새로 들어옴")
    # 5) 지시어 → 가리키는 사람 ★★★ (답장 대상 > 바로 전 대화에서 마지막으로 나온 사람)
    pm = _PRONOUN.search(request)
    if pm:
        word = pm.group(1)
        if r is not None and getattr(r, "from_user", None) and not r.from_user.is_bot and r.from_user.id != caller.id:
            notes.append(f"요청의 '{word}' = 답장한 메시지의 작성자 "
                         f"{_name(r.from_user.first_name, r.from_user.last_name, r.from_user.username)} (ID {r.from_user.id}).")
        else:
            who = await last_named(db, chat_id, caller.id, getattr(bot, "id", None), now)
            if who:
                add(who[0], who[1], 3, f"요청의 '{word}' = 바로 전 대화에서 마지막으로 말한 사람")
                notes.append(f"요청의 '{word}' = {who[1]} (ID {who[0]}) (바로 전 대화 기준). 도구엔 이 ID 를 넣을 것.")
            else:
                notes.append(f"요청의 '{word}' 가 누구인지 단서가 없다 → 추측하지 말고 누구인지 짧게 되물을 것.")
    lines = []
    for uid, c in sorted(cands.items(), key=lambda kv: -kv[1]["score"])[:MAX_LINES]:
        lines.append(f"{'★' * c['score']} {c['name']} (ID {uid}): {', '.join(c['why'])}")
    if not lines:
        lines.append("후보 없음: 방 기록에서 가리키는 사람을 못 찾았다. 요청에 이름이 적혀 있으면 멘션 없이 그 이름 그대로 부르고, "
                     "없으면 특정인을 지어내지 말 것.")
    m = _STYLE_ASK.search(request)
    if m:
        want = m.group(1) or m.group(2)
        notes.append(f"요청에 말투 변경 부탁이 섞여 있다: '{want}'. 누구 말투인지(특정인·방 전체·요청자 본인) 판단해 "
                     "다른 부탁과 함께 처리할 것.")
    return lines + notes


async def last_named(db, chat_id: int, caller_id: int, bot_id: int | None, now: int) -> tuple[int, str] | None:
    """부른 사람과 소담의 최근 대화(ai_turns, 30분·4번)에서 마지막으로 이름·@아이디가 나온 이 방 멤버 (본인·봇 제외).
    최신 대화부터, 한 대화 안에선 글 뒤쪽(답 > 요청)에 나온 사람."""
    turns = await db._all("SELECT request, answer FROM ai_turns WHERE chat_id=? AND user_id=? AND ts>=? "
                          "ORDER BY id DESC LIMIT ?", (chat_id, caller_id, now - PRONOUN_TURN_MIN * 60, PRONOUN_TURNS))
    if not turns:
        return None
    rows = await db._all(
        "SELECT u.user_id, u.first_name, u.last_name, u.username FROM members m JOIN users u ON u.user_id=m.user_id "
        "WHERE m.chat_id=? AND u.is_bot=0 AND COALESCE(m.last_seen,0) > ? ORDER BY m.last_seen DESC LIMIT ?",
        (chat_id, now - 90 * 86400, NAME_SCAN_LIMIT))
    people = []
    for row in rows:
        if row["user_id"] in (caller_id, bot_id):
            continue
        keys = {_squash(f"{row['first_name'] or ''}{row['last_name'] or ''}"), _squash(row["first_name"] or ""),
                _squash(row["username"] or "")}
        people.append((row, {k for k in keys if len(k) >= 2 and k not in GENERIC}))
    for t in turns:   # 최신 대화부터
        flat = _squash(f"{t['request']}\n{t['answer']}")
        best, pos = None, -1
        for row, keys in people:
            p = max((flat.rfind(k) for k in keys), default=-1)
            if p > pos:
                best, pos = row, p
        if best is not None:
            return best["user_id"], _name(best["first_name"], best["last_name"], best["username"])
    return None
