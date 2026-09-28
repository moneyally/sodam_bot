"""🎓 다른 봇 명령 프로필 (방마다 · 봇마다) — "소담아 멜론에 밤편지 신청해줘" 를 그 봇의 '/play@봇 밤편지' 로.

표 botlink_skills(chat_id, bot_id, command, args_hint, source, intent, updated, count) — 스키마는 sodam/botlink.py.
알게 되는 곳 (source):
- preset / manual 📌: 관리자 1:1 🤝 → 봇 → 🎓 명령 배우기 — 🎵 음악봇·📺 유튜브봇·🎲 게임봇 묶음, '명령 설명' 입력·✏️·🗑.
- seen 👀: 사람이 '/cmd@그봇 …' 또는 그 봇 글에 답장으로 '/cmd …' → 그 봇이 LEARN_WINDOW 초 안에 그 글에 답장하면 배움 (코드만,
  AI 없음). 이 방에 등록된(무시 아님) 봇만 · 봇마다 MAX_SKILLS 개 · 인자 글자는 저장 안 함('{…}' 표시만) · 봇이 보낸 명령은 안 배움.
- helper 🔧: svc.mtproto 봇 세션으로 users.getFullUser → bot_info.commands (core.telegram.org: "Both users and bots can use
  this method"). 봇마다 하루 1번, 실패는 무시. 관리자가 적은 줄·본 줄은 덮지 않음.
어느 명령을 쓸지: 같은 intent 가 여럿이면 updated 가 가장 최근인 것 (newest wins — 새로 적용·수정·다시 쓰인 명령).
이 모듈은 방에 아무것도 보내지 않는다 (보내기는 panels/botlink.py bot_command → botlink.send).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import botlink, hooks

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

INTENTS = ("play", "skip", "pause", "resume", "stop", "queue", "remove", "search", "dice", "bet", "other")
INTENT_LABEL = {"play": "재생·신청", "skip": "건너뛰기", "pause": "일시정지", "resume": "다시 재생", "stop": "정지",
                "queue": "대기열", "remove": "대기열에서 빼기", "search": "검색", "dice": "주사위", "bet": "베팅", "other": "기타"}
WIDE = ("play", "search")          # 인자 100자 + 유튜브 링크 허용 (botlink.build wide)
SOURCE_BADGE = {"preset": "📌직접", "manual": "📌직접", "seen": "👀본 것", "helper": "🔧헬퍼", "help": "📖안내"}
# 봇이 올린 사용법 글의 '/명령 설명' 줄 (실제 사례: 멜론봇 /help 에 /play·/skip… 이 다 있는데 /help 만 배움)
_INVITE = re.compile(r"(으로|로)\s*\S*\s*(신청|입력|사용|요청|써|쓰|보내|이용|재생)")
HELP_CMD = re.compile(r"(?<![\w/@<])/([A-Za-z가-힣ㄱ-ㅎ][A-Za-z0-9_가-힣ㄱ-ㅣ]{0,31})(?![\w@])([^\n/]*)")
# 사용법 줄 = 줄 맨 앞(글머리표·번호·이모지 뒤)의 명령. 글 중간의 '/ban 해' '</tool_result>' 같은 건 사용법이 아님 (봇 글 속 주입)
HELP_LINE = re.compile(r"(?m)^[^\w/\n]{0,6}(?:\d{1,2}[.)]\s*)?/([A-Za-z가-힣ㄱ-ㅎ][A-Za-z0-9_가-힣ㄱ-ㅣ]{0,31})(?![\w@])([^\n/]*)")


def help_commands(text: str) -> list[tuple[str, str]]:
    """사용법 글 → [(명령, 설명)]. 명령이 2개 이상일 때, 하나뿐이면 흔한 이름(/play 등)만
    (실제 사례: '새로운 노래는 /play 로 신청해 주세요!' 뿐인 음악봇 — 못 배워서 재생 요청이 막힘)."""
    def scan(rx):
        found = {}
        for name, rest in rx.findall(text or ""):
            found.setdefault(name.lower(), " ".join(rest.replace("—", " ").replace("-", " ").split()))
        return found
    found = scan(HELP_CMD)
    if len(scan(HELP_LINE)) >= 2:   # 사용법 글이면 한 줄에 둘('⏸ /pause · ▶️ /resume')인 것까지
        return list(found.items())
    if len(found) != 1:
        return []
    return [(n, d) for n, d in found.items() if n in _BY_NAME and _INVITE.match(d)]   # '/play 로 신청해 주세요' 꼴만
MAX_SKILLS = 30                    # 봇마다
MAX_HINT = 30
LEARN_WINDOW = 10.0                # 사람 명령 → 그 봇 답장까지
HELPER_EVERY = 86400
HELPER_FORCE_GAP = 60              # [🔧 불러오기] 연타 방지
SEEN_HINT = "{…}"
ADD_HOW = "관리자 1:1 → 🤝 → 봇 → 🎓 명령 배우기"

PRESETS = {
    "mu": ("🎵 음악봇", [("/play", "{곡}", "play"), ("/skip", "", "skip"), ("/pause", "", "pause"),
                        ("/resume", "", "resume"), ("/stop", "", "stop"), ("/queue", "", "queue")]),
    "yt": ("📺 유튜브봇", [("/play", "{검색어 또는 링크}", "play"), ("/skip", "", "skip"), ("/stop", "", "stop")]),
    "gm": ("🎲 게임봇", [("/dice", "", "dice"), ("/bet", "{금액}", "bet")]),
}

# 명령 이름 그대로 → intent
_BY_NAME = {
    **dict.fromkeys(("play", "p", "add", "request", "req", "song", "music", "sr"), "play"),
    **dict.fromkeys(("skip", "next", "s", "n", "fs", "forceskip"), "skip"),
    **dict.fromkeys(("pause",), "pause"),
    **dict.fromkeys(("resume", "unpause", "continue", "r"), "resume"),
    **dict.fromkeys(("stop", "leave", "end", "disconnect", "dc", "exit"), "stop"),
    **dict.fromkeys(("queue", "q", "list", "playlist", "np", "nowplaying"), "queue"),
    **dict.fromkeys(("remove", "rm", "delete", "del", "cancel", "unqueue"), "remove"),
    **dict.fromkeys(("search", "find", "yt", "youtube", "ytsearch"), "search"),
    **dict.fromkeys(("dice", "roll", "d"), "dice"),
    **dict.fromkeys(("bet", "b", "wager"), "bet"),
}
# 설명 낱말 → intent (순서 중요: '일시정지' ⊃ '정지', '재생목록' ⊃ '재생', '다시 재생'·'재개' 먼저)
_BY_WORD = (("remove", ("빼기", "빼", "삭제", "취소", "remove")), ("pause", ("일시정지", "일시 정지", "pause")), ("resume", ("재개", "다시 재생", "이어서", "이어 재생", "resume")),
            ("queue", ("대기열", "재생목록", "목록", "queue", "playlist")), ("skip", ("건너", "스킵", "다음 곡", "skip")),
            ("stop", ("정지", "멈춤", "종료", "stop")), ("search", ("검색", "search")),
            ("play", ("재생", "신청", "틀어", "노래", "play")), ("dice", ("주사위", "dice")),
            ("bet", ("베팅", "배팅", "bet")))

# '멜론'·'유튜브' 처럼 말한 이름 → 봇 @아이디·이름에서 찾을 말 (짧은 영문은 아이디 앞·'_' 뒤에서만)
_ALIASES = (("멜론", "melon"), ("유튜브", "유튭", "youtube", "yt"), ("지니", "genie"), ("벅스", "bugs"),
            ("스포티파이", "spotify"), ("사운드클라우드", "soundcloud"), ("음악", "노래", "music", "song"),
            ("주사위", "dice"), ("카지노", "casino"))

MEMBER_CMD = re.compile(r"/([A-Za-z0-9_가-힣ㄱ-ㅣ]{1,32})(?:@(\w{3,64}))?(?:\s+(\S.*))?", re.S)


def guess_intent(command: str, hint: str = "") -> str:
    name = command.lstrip("/").lower()
    if name in _BY_NAME:
        return _BY_NAME[name]
    text = f"{name} {hint}".lower()
    for intent, words in _BY_WORD:
        if any(w in text for w in words):
            return intent
    return "other"


def skill_id(command: str) -> str:
    """버튼용 짧은 확인값 (명령이 바뀐 옛 버튼 거절)."""
    return f"{zlib.crc32(command.encode()) & 0xffff:04x}"


# ── 조회 ──────────────────────────────────────────────────
async def skills(db, chat_id: int, bot_id: int) -> list:
    return await db._all("SELECT * FROM botlink_skills WHERE chat_id=? AND bot_id=? ORDER BY command", (chat_id, bot_id))


async def for_intent(db, chat_id: int, bot_id: int, intent: str):
    """그 봇에서 intent 를 하는 명령 — 여럿이면 가장 최근 것 (newest wins)."""
    # 관리자가 정한 것(최근 것) 먼저 → 저절로 배운 것 중엔 이름이 intent 와 같은 명령 먼저
    # (실제 사례: 사용법 글의 /queue·/remove 가 둘 다 '대기열' → /remove 를 고를 수 있었음)
    return await db._one("SELECT * FROM botlink_skills WHERE chat_id=? AND bot_id=? AND intent=? "
                         "ORDER BY source IN ('manual','preset') DESC, "
                         "CASE WHEN source IN ('manual','preset') THEN updated ELSE 0 END DESC, "
                         "command = '/' || intent DESC, updated DESC, count DESC LIMIT 1", (chat_id, bot_id, intent))


async def intent_of(db, chat_id: int, bot_id: int, command: str) -> str:
    r = await db._one("SELECT intent FROM botlink_skills WHERE chat_id=? AND bot_id=? AND command=?",
                      (chat_id, bot_id, command))
    return r["intent"] if r else guess_intent(command)


def describe(rows, limit: int = 12) -> str:
    return ", ".join(f"{r['command']}" + (f" {r['args_hint']}" if r["args_hint"] else "") + f"({r['intent']})"
                     + (f" [예: {r['example']}]" if r["example"] else "") for r in rows[:limit]) or "(없음)"


def _terms(q: str) -> list[str]:
    base = re.sub(r"(봇|bot|_bot)$", "", q) or q
    out = [base]
    for group in _ALIASES:
        if any(g in q for g in group):
            out += [g for g in group if g not in out]
    return out


def _hit(term: str, r) -> bool:
    user, name = (r["username"] or "").lower(), (r["name"] or "").lower()
    if len(term) <= 2 and term.isascii():
        return bool(re.search(rf"(^|_){re.escape(term)}", user))
    return term in user or term in name


async def pick_bot(db, chat_id: int, query: str, intent: str = ""):
    """(봇 행, 후보들). 이름·@아이디·별칭(멜론→melon 등)으로 찾고, 여럿이면 intent 명령이 있는 봇으로 좁힘.
    후보가 없으면 (None, []) = 이 방에서 본 봇이 하나도 없음."""
    rows = [r for r in await botlink.bots(db, chat_id) if r["status"] != "ignored"]
    if not rows:
        return None, []
    q = str(query or "").strip().lstrip("@").lower()
    pool = rows
    if q:
        exact = [r for r in rows if (r["username"] or "").lower() == q or str(r["bot_id"]) == q]
        if exact:
            return exact[0], exact
        terms = _terms(q)
        part = [r for r in rows if any(_hit(t, r) for t in terms)]
        if len(part) == 1:
            return part[0], part
        if not part:
            return None, rows
        pool = part
    if intent and intent != "other":
        able_ids = {r["bot_id"] for r in await db._all(
            "SELECT DISTINCT bot_id FROM botlink_skills WHERE chat_id=? AND intent=?", (chat_id, intent))}
        able = [r for r in pool if r["bot_id"] in able_ids]
        if len(able) == 1:
            return able[0], able
        if able:
            return None, able
    if len(pool) == 1:
        return pool[0], pool
    return None, pool


# ── 쓰기 ──────────────────────────────────────────────────
def _cap_ok(c, chat_id: int, bot_id: int, command: str) -> bool:
    if c.execute("SELECT 1 FROM botlink_skills WHERE chat_id=? AND bot_id=? AND command=?",
                 (chat_id, bot_id, command)).fetchone():
        return True
    return c.execute("SELECT COUNT(*) FROM botlink_skills WHERE chat_id=? AND bot_id=?",
                     (chat_id, bot_id)).fetchone()[0] < MAX_SKILLS


_UPSERT = ("INSERT INTO botlink_skills(chat_id, bot_id, command, args_hint, source, intent, updated) VALUES(?,?,?,?,?,?,?) "
           "ON CONFLICT(chat_id, bot_id, command) DO UPDATE SET args_hint=excluded.args_hint, source=excluded.source, "
           "intent=excluded.intent, updated=excluded.updated")


async def apply_preset(db, chat_id: int, bot_id: int, code: str) -> int:
    """관리자 묶음 적용 — 같은 명령은 새 값으로(newest wins). 넣은 수."""
    items = PRESETS[code][1]
    now = int(time.time())

    def work(c):
        n = 0
        for cmd, hint, intent in items:
            if _cap_ok(c, chat_id, bot_id, cmd):
                c.execute(_UPSERT, (chat_id, bot_id, cmd, hint, "preset", intent, now))
                n += 1
        return n
    return await db.atomic(work)


def parse_input(text: str, username: str | None) -> tuple[str, str] | str:
    """관리자 입력 '명령 설명' (앞 '/' 는 있어도 없어도) → ('/명령', '설명'), 틀리면 안내문. botlink.build 규칙으로 검사."""
    raw = " ".join(str(text or "").split())
    if not raw or "\n" in str(text).strip():
        return "한 줄로 보내주세요. 예: <code>play {곡}</code>"
    head, _, hint = raw.lstrip("/").partition(" ")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", head):
        return "명령은 영문·숫자·_ 32자까지예요. 예: <code>play {곡}</code>"
    if len(hint) > MAX_HINT:
        return f"설명은 {MAX_HINT}자까지예요."
    if botlink.build(f"/{head} {hint}".strip(), username or "bot") is None:
        return "설명에 링크·@·/·&lt;&gt; 는 넣을 수 없어요."
    return "/" + head.lower(), hint


async def set_manual(db, chat_id: int, bot_id: int, command: str, hint: str, old: str | None = None) -> bool:
    """관리자 추가·수정 (old = 고치는 원래 명령). 봇당 MAX_SKILLS 넘으면 False."""
    now, intent = int(time.time()), guess_intent(command, hint)

    def work(c):
        if old and old != command:
            c.execute("DELETE FROM botlink_skills WHERE chat_id=? AND bot_id=? AND command=?", (chat_id, bot_id, old))
        if not _cap_ok(c, chat_id, bot_id, command):
            raise _Full
        c.execute(_UPSERT, (chat_id, bot_id, command, hint, "manual", intent, now))
    try:
        await db.atomic(work)
    except _Full:
        return False
    return True


class _Full(Exception):
    pass


async def delete(db, chat_id: int, bot_id: int, command: str) -> bool:
    return bool(await db.atomic(lambda c: c.execute(
        "DELETE FROM botlink_skills WHERE chat_id=? AND bot_id=? AND command=?", (chat_id, bot_id, command)).rowcount))


def example_of(sent: str, reply: str) -> str:
    """사람이 보낸 명령 + 그 봇의 답 첫 줄 → 예시 한 줄 (AI 가 한글·줄임 명령의 뜻을 알게).
    멤버가 쓴 인자 글자(곡명 등)는 저장 안 함 → 명령엔 {…}, 답에 그대로 나온 인자 낱말도 '…'."""
    first = next((ln.strip() for ln in (reply or "").splitlines() if ln.strip()), "")
    if not first:
        return ""
    head, _, args = " ".join(sent.split()).partition(" ")
    for w in sorted(set(args.split()), key=len, reverse=True):
        if len(w) >= 2 and not w.replace(",", "").isdecimal():
            first = first.replace(w, "…")
    return f"'{head.split('@')[0][:32]}{' {…}' if args else ''}' → {first[:70]}"


async def record_seen(db, chat_id: int, bot_id: int, command: str, has_args: bool, sent: str = "", reply: str = "") -> bool:
    """본 것: 있으면 횟수·시각·예시만(관리자가 적은 설명·intent 는 그대로, 모르던 intent 만 채움), 없으면 새로 (상한 안에서).
    intent 는 이름으로 모르면 그 봇의 답으로 ('/ㅂㅋ' → '뱅커에 … 배팅' = bet)."""
    now, example = int(time.time()), example_of(sent, reply)
    intent = guess_intent(command)
    if intent == "other" and reply:
        intent = guess_intent("", reply[:80])

    def work(c):
        if c.execute("UPDATE botlink_skills SET count=count+1, updated=?, example=CASE WHEN ?='' THEN example ELSE ? END, "
                     "intent=CASE WHEN intent='other' THEN ? ELSE intent END WHERE chat_id=? AND bot_id=? AND command=?",
                     (now, example, example, intent, chat_id, bot_id, command)).rowcount:
            return True
        if not _cap_ok(c, chat_id, bot_id, command):
            return False
        c.execute("INSERT INTO botlink_skills(chat_id, bot_id, command, args_hint, source, intent, updated, count, example) "
                  "VALUES(?,?,?,?,?,?,?,1,?)", (chat_id, bot_id, command, SEEN_HINT if has_args else "", "seen", intent, now,
                                                example))
        return True
    return await db.atomic(work)


async def save_helper(db, chat_id: int, bot_id: int, cmds, source: str = "helper") -> int:
    """헬퍼·사용법 글에서 얻은 [(명령, 설명)] — 새 명령만 넣고, 같은 출처가 넣은 줄만 고침 (관리자·본 것은 안 덮음)."""
    now, rows = int(time.time()), []
    for name, desc in list(cmds)[:100]:
        name = str(name or "").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
            continue
        hint = " ".join(str(desc or "").split())[:MAX_HINT]
        if botlink.build(f"/{name} {hint}".strip(), "bot") is None:
            hint = ""
        rows.append(("/" + name, hint, guess_intent(name, hint)))

    def work(c):
        n = 0
        for cmd, hint, intent in rows:
            if not _cap_ok(c, chat_id, bot_id, cmd):
                continue
            n += c.execute("INSERT INTO botlink_skills(chat_id, bot_id, command, args_hint, source, intent, updated) "
                           "VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id, bot_id, command) DO UPDATE SET "
                           "args_hint=excluded.args_hint, intent=excluded.intent, updated=excluded.updated "
                           "WHERE botlink_skills.source=excluded.source", (chat_id, bot_id, cmd, hint, source, intent, now)).rowcount
        return n
    return await db.atomic(work)


# ── 배우기 (코드만) ────────────────────────────────────────
@dataclass
class _Learn:
    pending: dict = field(default_factory=dict)   # (방, 사람 글 ID) → (봇 ID, '/명령', 인자 있음?, 시각)
    helper: dict = field(default_factory=dict)    # (방, 봇) → 마지막 헬퍼 조회 시각
    tasks: set = field(default_factory=set)


def state(svc: Services) -> _Learn:
    st = getattr(svc, "_botskills", None)
    if st is None:
        st = svc._botskills = _Learn()
    return st


async def on_member_message(svc: Services, bot, msg, role) -> None:
    """사람이 다른 봇에게 보낸 '/명령' 을 잠깐 기억 (그 봇이 답해야 배움). 그룹 메시지 훅 — 봇 글은 여기 안 옴."""
    user, text = msg.from_user, (msg.text or "")
    if not text.startswith("/") or user is None or user.is_bot:
        return
    m = MEMBER_CMD.fullmatch(text.strip())
    if not m or await botlink.active(svc, msg.chat_id) is None:
        return
    target, r = (m.group(2) or "").lower(), msg.reply_to_message
    if target:
        if target == (bot.username or "").lower():
            return
        row = await svc.db._one("SELECT bot_id, status FROM botlink_bots WHERE chat_id=? AND lower(username)=?",
                                (msg.chat_id, target))
    elif r is not None and r.from_user is not None and r.from_user.is_bot and r.from_user.id != bot.id:
        row = await botlink.get_bot(svc.db, msg.chat_id, r.from_user.id)
    else:   # '/플 30000000' 처럼 대상 없이 → 10초 안에 이 글에 답장한 봇의 명령으로 (실제 사례: 게임봇 한글 명령)
        row = {"bot_id": None, "status": "seen"}
    if not row or row["status"] == "ignored":
        return
    st, now = state(svc), time.time()
    st.pending[(msg.chat_id, msg.message_id)] = (row["bot_id"], "/" + m.group(1).lower(), bool(m.group(3)), now, text)
    if len(st.pending) > 500:
        for k in [k for k, v in st.pending.items() if now - v[3] > LEARN_WINDOW] or list(st.pending)[:100]:
            st.pending.pop(k, None)


async def learn_from_history(db, chat_id: int, bot_id: int) -> int:
    """이미 기록된 그 봇 글(최근 50개, 7일)에서 사용법을 다시 배움 — 기능이 생기기 전에 올라온 /help 도 DB 로."""
    rows = await db._all("SELECT text FROM botlink_msgs WHERE chat_id=? AND bot_id=? ORDER BY ts DESC LIMIT 50",
                         (chat_id, bot_id))
    n = 0
    for r in rows:
        cmds = help_commands(r["text"])
        if cmds:
            n += await save_helper(db, chat_id, bot_id, cmds[:MAX_SKILLS], source="help")
    # 사람 '/명령' → LEARN_WINDOW 안에 그 사람에게 온 이 봇의 답 (기능이 생기기 전 대화도 · 이미 아는 명령은 예시만 채움)
    pairs = await db._all(   # 명령마다 그 뒤 첫 답 (가까운 다른 명령의 답과 섞이지 않게)
        "SELECT m.text AS sent, (SELECT b.text FROM botlink_msgs b WHERE b.chat_id=m.chat_id AND b.bot_id=? "
        "AND b.to_user=m.user_id AND b.ts BETWEEN m.ts AND m.ts+? ORDER BY b.ts, b.msg_id LIMIT 1) AS reply "
        "FROM messages m WHERE m.chat_id=? AND m.is_bot=0 AND m.text LIKE '/%' AND m.ts>=? ORDER BY m.ts DESC LIMIT 100",
        (bot_id, int(LEARN_WINDOW), chat_id, int(time.time()) - 7 * 86400))
    known = {k["command"]: k for k in await skills(db, chat_id, bot_id)}
    for p in pairs:
        if not p["reply"]:
            continue
        m = MEMBER_CMD.fullmatch(p["sent"].strip())
        if not m or (m.group(2) and m.group(2).lower() != (await _username(db, chat_id, bot_id))):
            continue
        cmd = "/" + m.group(1).lower()
        if cmd in known and known[cmd]["example"]:
            continue
        if await record_seen(db, chat_id, bot_id, cmd, bool(m.group(3)), p["sent"], p["reply"]):
            n += cmd not in known
            known[cmd] = {"example": "x"}
    return n


async def _username(db, chat_id: int, bot_id: int) -> str:
    r = await db._one("SELECT username FROM botlink_bots WHERE chat_id=? AND bot_id=?", (chat_id, bot_id))
    return ((r["username"] if r else "") or "").lower()


async def on_bot_seen(svc: Services, bot, msg, status: str, now: float) -> None:
    """botlink.SEEN_HOOKS: 봇 글이 기록된 뒤. ① 기억해 둔 사람 명령에 LEARN_WINDOW 안에 답장했으면 배움 ② 헬퍼 하루 1번."""
    st, r, cid = state(svc), msg.reply_to_message, msg.chat_id
    if r is not None and r.from_user is not None and not r.from_user.is_bot:
        got = st.pending.pop((cid, r.message_id), None)
        if got and got[0] in (None, msg.from_user.id) and now - got[3] <= LEARN_WINDOW and status != "ignored":
            await record_seen(svc.db, cid, msg.from_user.id, got[1], got[2], got[4], msg.text or msg.caption or "")
    cmds = help_commands(msg.text or msg.caption or "")
    if cmds:
        await save_helper(svc.db, cid, msg.from_user.id, cmds[:MAX_SKILLS], source="help")
    if getattr(svc, "mtproto", None) is not None and msg.from_user.username:
        key = (cid, msg.from_user.id)
        if now - st.helper.get(key, 0) >= HELPER_EVERY:
            st.helper[key] = now
            t = asyncio.create_task(_helper_bg(svc, cid, msg.from_user.id, msg.from_user.username))
            st.tasks.add(t)
            t.add_done_callback(st.tasks.discard)


async def _helper_bg(svc: Services, cid: int, bot_id: int, username: str) -> None:
    try:
        await helper_fetch(svc, cid, bot_id, username)
    except Exception as e:   # 헬퍼는 덤 — 실패는 무시
        log.info("botskills helper %s failed: %s", username, e)


async def helper_fetch(svc: Services, cid: int, bot_id: int, username: str) -> int | None:
    """MTProto 봇 세션으로 그 봇의 명령 목록. 넣은/고친 줄 수, 못 물어보면 None."""
    mt = getattr(svc, "mtproto", None)
    if mt is None or not username or not getattr(mt, "bot_ready", False):
        return None

    async def fetch(client):
        from telethon.tl.functions.users import GetFullUserRequest
        res = await client(GetFullUserRequest(await client.get_input_entity(username)))
        info = getattr(getattr(res, "full_user", None), "bot_info", None)
        return [(c.command, c.description) for c in (getattr(info, "commands", None) or ())]
    got = await mt._run(mt.bot, fetch)
    if got is None:
        return None
    return await save_helper(svc.db, cid, bot_id, got)


async def helper_now(svc: Services, cid: int, row) -> int | None:
    """[🔧 불러오기] — HELPER_FORCE_GAP 안에 또 누르면 None."""
    st, now = state(svc), time.time()
    key = (cid, row["bot_id"])
    if now - st.helper.get(key, 0) < HELPER_FORCE_GAP:
        return None
    st.helper[key] = now
    try:
        return await helper_fetch(svc, cid, row["bot_id"], row["username"])
    except Exception as e:
        log.info("botskills helper %s failed: %s", row["username"], e)
        return None


hooks.add_group_message_hook(on_member_message)
if on_bot_seen not in botlink.SEEN_HOOKS:
    botlink.SEEN_HOOKS.append(on_bot_seen)
