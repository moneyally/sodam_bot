"""🧹 멤버 정리 (설계 docs/MEMBER_CLEANUP.md): 방 참가자 전체를 스캔해서 탈퇴 계정·다른 봇·잠수·가라 의심을 골라
관리자가 확인한 사람만 뒤에서 천천히 내보낸다 (ban → unban = 다시 들어올 수 있는 내보내기).

흐름: 스캔(MTProto 봇 세션 participants, 방당 10분 1번, 결과 DB 30분 유효) → 분류 → 보호 명단 빼기 → 미리보기(1:1)
→ 확인 카드(만든 관리자만·한 번만·10분) → 작업(방마다 1개, DB 에 남은 명단 → 재시작해도 이어서) → 끝 보고(1:1).

안전 규칙 (깊은 버그 막기 — tests/test_member_cleanup.py 가 하나씩 검사):
- 접속 상태 '모름'(UserStatusEmpty·없음)은 '오래전'이 아니다 → 잠수 N일엔 **정확한 마지막 접속**(Offline.was_online)만,
  그리고 소담 기록(글·마지막 활동)도 N일 안에 없어야. LastMonth(한 달 안 접속)는 잠수 30 에 안 넣음. 모름은 따로 '❔ 접속 모름'.
- 보호(절대 안 내보냄): 방 관리자·오너·봇 관리자·소담 자신·자유 멤버·제외 명단·**최근 14일 안에 이 방에서 글 쓴 사람**.
  스캔 때 한 번, 실행 때 사람마다 한 번 더 (그 사이 바뀐 것).
- 실행 때 사람마다 get_chat_member 로 아직 멤버인지·관리자가 아닌지 다시 확인 (미리보기와 실행 사이 명단이 바뀜).
- 스캔이 1만 명에서 잘리면(partial) 받은 사람만 대상, '나감' 판단 안 함.
- 1.2초에 1명, RetryAfter 면 그만큼 쉼(5분 넘게 기다리라면 멈춤 — 무한 재시도 없음), 봇의 '사용자 차단' 권한이 빠지면 멈추고 알림.
- 명단은 방에 절대 안 뿌림 — 결과·CSV 는 관리자 1:1 로만. 남이 정한 이름은 esc + 길이 자름.
- 이 작업이 낸 퇴장은 작별 인사를 건너뛰고 '~님을 내보냄' 서비스 메시지를 지운다 (handlers → job_leave).
화면·버튼·AI 도구: sodam/panels/cleanup.py · 명령: commands.c_cleanup · 원격 점검: diag /v1/cleanup (숫자만).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from . import accountage, free, persist
from .db import register_schema
from .util import display_name, esc, fmt_time

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

DAY = 86400
SCAN_GAP = 600              # 방당 스캔 간격 (그 안엔 저장된 결과를 다시 씀)
SCAN_VALID = 1800           # 스캔 결과로 내보내기를 시작할 수 있는 시간
SCAN_LIMIT = 10000          # participants (Recent) 가 주는 최대
PROTECT_DAYS = 14           # 최근 이만큼 안에 이 방에 글 쓴 사람은 보호
IDLE_DEFAULTS = (14, 30)
IDLE_MAX = 365
FAKE_MIN = 3                # 가라 의심 신호 이만큼 이상
BURST_WINDOW, BURST_MIN = 600, 5   # 같은 10분에 5명 넘게 들어온 무더기 입장
KICK_GAP = 1.2              # 사람 사이 간격 (초)
PROGRESS_EVERY = 20         # 진행 메시지 고치는 간격 (사람 수)
PROGRESS_MIN_SEC = 10       # 진행 메시지 고치는 최소 간격 (초)
RIGHTS_EVERY = 20           # 봇 권한 다시 확인 간격 (사람 수)
RETRY_MAX = 3               # RetryAfter 한 사람당 최대
RETRY_MAX_WAIT = 300        # 이보다 오래 기다리라면 멈춤 (나중에 이어서)
LEASE = 120                 # 작업이 이만큼 소식 없으면 다른 틱이 이어 받음 (죽은 작업)
LEAVE_TTL = 300             # 이 작업이 낸 퇴장으로 보는 시간
CARD_TTL = 600              # 확인 카드 유효
PREVIEW_NAMES = 20
NAME_CHARS = 24
_sleep = asyncio.sleep      # 테스트가 바꿔 끼움

CATS = {"d": "🪦 탈퇴 계정", "b": "🤖 다른 봇", "i": "💤 잠수", "u": "❔ 접속 모름", "f": "👻 가라 의심"}
REASON = {"d": "탈퇴 계정", "b": "다른 봇", "i": "잠수", "u": "접속 모름", "f": "가라 의심"}
STATUS_KO = {"online": "지금 접속 중", "recently": "최근 접속(1~3일 안, 정확한 시각은 숨김)",
             "last_week": "1주 안 접속(숨김)", "last_month": "한 달 안 접속(숨김)",
             "unknown": "모름 (숨겼거나 오래전 — 구분 불가)"}
NO_MT = ("전체 멤버 명단은 텔레그램 봇 기본 기능(Bot API)으로는 못 받아요. 운영자가 MTProto 헬퍼를 켜야 해요 "
         "(오너 1:1 🔧 헬퍼 화면).")

register_schema("""
CREATE TABLE IF NOT EXISTS cleanup_scans (
    chat_id   INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    by_id     INTEGER,
    total     INTEGER NOT NULL,
    partial   INTEGER NOT NULL DEFAULT 0,
    counts    TEXT NOT NULL DEFAULT '{}',   -- 분류별 인원 (보호 뺀 뒤)
    prot      TEXT NOT NULL DEFAULT '{}',   -- 보호 이유별 인원
    dist      TEXT NOT NULL DEFAULT '{}',   -- 접속 상태 종류별 인원 (전체)
    rec_since INTEGER                       -- 소담 기록(대화) 시작
);
CREATE TABLE IF NOT EXISTS cleanup_cands (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    username   TEXT NOT NULL DEFAULT '',
    deleted    INTEGER NOT NULL DEFAULT 0,
    is_bot     INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'unknown',
    was_online INTEGER,
    last_act   INTEGER,
    msgs       INTEGER NOT NULL DEFAULT 0,
    photo      INTEGER NOT NULL DEFAULT 0,
    recent     INTEGER NOT NULL DEFAULT 0,
    burst      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS cleanup_excl (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    by_id   INTEGER,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS cleanup_jobs (
    chat_id  INTEGER PRIMARY KEY,
    by_id    INTEGER NOT NULL,
    sel      TEXT NOT NULL,
    state    TEXT NOT NULL,                 -- running · stopped · halted · done
    why      TEXT NOT NULL DEFAULT '',      -- 멈춘 이유
    created  INTEGER NOT NULL,
    beat     REAL NOT NULL DEFAULT 0,
    total    INTEGER NOT NULL DEFAULT 0,
    kicked   INTEGER NOT NULL DEFAULT 0,
    gone     INTEGER NOT NULL DEFAULT 0,
    skipped  INTEGER NOT NULL DEFAULT 0,
    failed   INTEGER NOT NULL DEFAULT 0,
    fails    TEXT NOT NULL DEFAULT '{}',    -- 실패 이유 → 수
    before_n INTEGER,
    dm_msg   INTEGER,
    ended    INTEGER
);
CREATE TABLE IF NOT EXISTS cleanup_queue (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    pos     INTEGER NOT NULL,
    reason  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"cleanup_scans": "drop", "cleanup_cands": "drop", "cleanup_excl": "composite",
              "cleanup_jobs": "drop", "cleanup_queue": "drop"})


# ── 분류 (값만 보고 판단, DB·네트워크 없음) ─────────────────
def parse_sel(raw: str) -> list[str]:
    """'d.b.i30.u.f' → ['d', 'b', 'i30', 'u', 'f']. 모르는 조각은 버림, 잠수는 하나만 (1~365일)."""
    out: list[str] = []
    for t in (raw or "").split("."):
        if t in ("d", "b", "u", "f") and t not in out:
            out.append(t)
        elif t[:1] == "i" and t[1:].isdecimal() and 1 <= int(t[1:]) <= IDLE_MAX and not any(x[0] == "i" for x in out):
            out.append(f"i{int(t[1:])}")
    return out


def sel_str(sel: list[str]) -> str:
    return ".".join(sel)


def cat_label(tok: str) -> str:
    return f"{CATS['i']} {tok[1:]}일" if tok[0] == "i" else CATS[tok]


def idle(r, days: int, now: float) -> bool:
    """잠수 N일 = 정확한 마지막 접속이 N일보다 전 **이고** 소담 기록상 N일 안에 활동 없음.
    '모름'·대략(Recently·LastWeek·LastMonth)·지금 접속 중은 절대 아님 (모름 ≠ 오래전)."""
    cut = now - days * DAY
    return r["status"] == "offline" and bool(r["was_online"]) and r["was_online"] < cut and (r["last_act"] or 0) < cut


def fake_signals(r) -> list[str]:
    """가라 의심 신호 (전부 기록된 사실). FAKE_MIN 개 이상이면 '의심' — 확정 아님."""
    sig = []
    if not r["msgs"]:
        sig.append("글 0개(소담 기록)")
    if not r["photo"]:
        sig.append("프사 없음")
    if not r["username"]:
        sig.append("@아이디 없음")
    if r["recent"]:
        sig.append("최근 계정")
    if r["burst"]:
        sig.append("무더기 입장")
    if r["status"] != "online" and r["status"] != "offline":
        sig.append("접속 숨김·모름")
    return sig


def cats_of(r, sel: list[str], now: float) -> list[str]:
    """이 사람이 걸리는 고른 분류 (순서 = sel). 탈퇴·봇은 그 분류만."""
    if r["deleted"]:
        return ["d"] if "d" in sel else []
    if r["is_bot"]:
        return ["b"] if "b" in sel else []
    out = []
    for t in sel:
        if t[0] == "i" and idle(r, int(t[1:]), now):
            out.append(t)
        elif t == "u" and r["status"] == "unknown":
            out.append(t)
        elif t == "f" and len(fake_signals(r)) >= FAKE_MIN:
            out.append(t)
    return out


def status_text(kind: str, was: int | None, tz) -> str:
    if kind == "offline" and was:
        return f"마지막 접속 {fmt_time(was, tz, '%y.%m.%d %H:%M')} (정확)"
    return STATUS_KO.get(kind, STATUS_KO["unknown"])


def clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def name_html(r, n: int = NAME_CHARS) -> str:
    name = esc(clip(r["name"] or ("탈퇴 계정" if r["deleted"] else str(r["user_id"])), n))
    return name + (f" @{esc(clip(r['username'], 32))}" if r["username"] else "")


async def _rc(db, sql: str, params: tuple = ()) -> int:
    """바뀐 줄 수 (db._write 는 lastrowid 를 돌려줘서 '했나?' 판단에 못 씀 — 한 문장으로 차지)."""
    return await db.atomic(lambda c: c.execute(sql, params).rowcount)


# ── 보호 ───────────────────────────────────────────────────
async def excluded_ids(db, chat_id: int) -> set[int]:
    return {r["user_id"] for r in await db._all("SELECT user_id FROM cleanup_excl WHERE chat_id=?", (chat_id,))}


async def free_ids(db, chat_id: int) -> set[int]:
    return {r["user_id"] for r in await db._all("SELECT user_id FROM free_members WHERE chat_id=?", (chat_id,))}


async def recent_writer(db, chat_id: int, user_id: int, now: float | None = None) -> bool:
    row = await db._one("SELECT 1 FROM messages WHERE chat_id=? AND user_id=? AND is_bot=0 AND ts>=? LIMIT 1",
                        (chat_id, user_id, int((now or time.time()) - PROTECT_DAYS * DAY)))
    return row is not None


async def _admin_ids(svc: Services, bot, chat_id: int) -> set[int]:
    from .panels import members as M   # 늦게 import (panels → cleanup). 관리자 목록 캐시·8초 제한 재사용
    svc.perms.forget(chat_id)
    return await M.admin_ids(svc, bot, chat_id)


async def still_protected(svc: Services, bot, chat_id: int, uid: int) -> str:
    """실행 직전 사람마다 다시: 보호 이유 (없으면 ''). 스캔 뒤 제외·자유 멤버 지정·새 글·관리자 승격을 잡는다."""
    if uid == bot.id:
        return "소담"
    if uid in await svc.perms.owners():
        return "오너"
    if uid in await excluded_ids(svc.db, chat_id):
        return "제외 명단"
    if await free.is_free(svc.db, chat_id, uid):
        return "자유 멤버"
    if await recent_writer(svc.db, chat_id, uid):
        return f"최근 {PROTECT_DAYS}일 글"
    try:
        if await svc.perms.protected(bot, chat_id, uid):
            return "관리자"
    except TelegramError:
        return "관리자 확인 실패"   # 모르면 안 내보냄
    return ""


# ── 스캔 ───────────────────────────────────────────────────
_locks: dict[tuple, asyncio.Lock] = {}


def _lock(svc: Services, chat_id: int) -> asyncio.Lock:
    return _locks.setdefault((svc.db.path, chat_id), asyncio.Lock())


def _scan_dict(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("counts", "prot", "dist"):
        try:
            d[k] = json.loads(d[k] or "{}")
        except ValueError:
            d[k] = {}
    return d


async def last_scan(db, chat_id: int) -> dict | None:
    return _scan_dict(await db._one("SELECT * FROM cleanup_scans WHERE chat_id=?", (chat_id,)))


def scan_fresh(scan: dict | None, now: float | None = None) -> bool:
    return bool(scan) and (now or time.time()) - scan["ts"] < SCAN_VALID


async def scan(svc: Services, bot, chat_id: int, by_id: int, *, force: bool = False) -> tuple[dict | None, str]:
    """(스캔 요약, 오류 글). 방당 SCAN_GAP 안엔 저장된 결과 (force 여도). 스캔 요약에 'reused'."""
    mt = getattr(svc, "mtproto", None)
    if mt is None or not getattr(mt, "enabled", False):
        return None, NO_MT
    async with _lock(svc, chat_id):
        now = time.time()
        last = await last_scan(svc.db, chat_id)
        if last and now - last["ts"] < SCAN_GAP:
            return {**last, "reused": True}, ""
        members = await mt.participants(chat_id, SCAN_LIMIT)
        if members is None:
            return None, ("텔레그램에서 멤버 명단을 못 받았어요 (MTProto 연결 안 됨·속도 제한·소담이 이 방 관리자가 아님). "
                          "잠시 뒤 다시 해주세요.")
        return await _classify_store(svc, bot, chat_id, by_id, members, now), ""


async def _classify_store(svc: Services, bot, chat_id: int, by_id: int, members: list[dict], now: float) -> dict:
    db = svc.db
    admins = await _admin_ids(svc, bot, chat_id)
    admins |= set(await db.bot_admin_ids(chat_id)) | set(await svc.perms.owners()) | {bot.id}
    excl, frees = await excluded_ids(db, chat_id), await free_ids(db, chat_id)
    acts = {r["user_id"]: (r["last"], r["n"]) for r in await db._all(
        "SELECT user_id, MAX(ts) AS last, COUNT(*) AS n FROM messages WHERE chat_id=? AND is_bot=0 GROUP BY user_id",
        (chat_id,))}
    mem = {r["user_id"]: (r["last_seen"], r["joined_at"]) for r in await db._all(
        "SELECT user_id, last_seen, joined_at FROM members WHERE chat_id=?", (chat_id,))}
    rec = await db._one("SELECT MIN(ts) AS t FROM messages WHERE chat_id=?", (chat_id,))
    buckets: dict[int, int] = {}
    for _, joined in mem.values():
        if joined:
            buckets[joined // BURST_WINDOW] = buckets.get(joined // BURST_WINDOW, 0) + 1
    writer_cut = now - PROTECT_DAYS * DAY
    prot = {"관리자·오너·봇": 0, f"최근 {PROTECT_DAYS}일 글": 0, "자유 멤버": 0, "제외 명단": 0}
    dist: dict[str, int] = {}
    rows = []
    for m in members:
        uid = m["id"]
        kind = m.get("status") or "unknown"
        dist[kind] = dist.get(kind, 0) + 1
        last_msg, n_msgs = acts.get(uid, (None, 0))
        if uid in admins:
            prot["관리자·오너·봇"] += 1
            continue
        if last_msg and last_msg >= writer_cut:
            prot[f"최근 {PROTECT_DAYS}일 글"] += 1
            continue
        if uid in frees:
            prot["자유 멤버"] += 1
            continue
        if uid in excl:
            prot["제외 명단"] += 1
            continue
        seen, joined = mem.get(uid, (None, None))
        last_act = max([t for t in (last_msg, seen) if t] or [0]) or None
        rows.append({"user_id": uid, "name": display_name(m.get("first_name"), m.get("last_name"), None)
                     if (m.get("first_name") or m.get("last_name")) else "",
                     "username": m.get("username") or "", "deleted": int(bool(m.get("deleted"))),
                     "is_bot": int(bool(m.get("is_bot"))), "status": kind, "was_online": m.get("was_online"),
                     "last_act": last_act, "msgs": n_msgs, "photo": int(bool(m.get("photo"))),
                     "recent": int(accountage.is_recent(uid, now=now)),
                     "burst": int(bool(joined) and buckets.get(joined // BURST_WINDOW, 0) > BURST_MIN)})
    sel = ["d", "b", "i14", "u", "f"]
    counts = {"d": 0, "b": 0, "i14": 0, "i30": 0, "u": 0, "f": 0}
    for r in rows:
        for t in cats_of(r, sel, now):
            counts[t] += 1
        if idle(r, 30, now) and not r["deleted"] and not r["is_bot"]:
            counts["i30"] += 1
    partial = len(members) >= SCAN_LIMIT
    summary = {"chat_id": chat_id, "ts": int(now), "by_id": by_id, "total": len(members), "partial": int(partial),
               "counts": counts, "prot": prot, "dist": dist, "rec_since": rec["t"] if rec else None}
    cols = ("user_id", "name", "username", "deleted", "is_bot", "status", "was_online", "last_act", "msgs", "photo",
            "recent", "burst")

    def run(c) -> None:
        c.execute("DELETE FROM cleanup_cands WHERE chat_id=?", (chat_id,))
        c.executemany(f"INSERT INTO cleanup_cands(chat_id, {', '.join(cols)}) VALUES(?{', ?' * len(cols)})",
                      [(chat_id, *(r[k] for k in cols)) for r in rows])
        c.execute("INSERT OR REPLACE INTO cleanup_scans(chat_id, ts, by_id, total, partial, counts, prot, dist, rec_since) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (chat_id, int(now), by_id, len(members), int(partial), json.dumps(counts), json.dumps(prot, ensure_ascii=False),
                   json.dumps(dist), summary["rec_since"]))
    await db.atomic(run)
    log.info("멤버 정리 스캔 chat %s: %d명 (partial=%s) %s", chat_id, len(members), partial, counts)
    return {**summary, "reused": False}


async def selection(svc: Services, chat_id: int, sel: list[str], now: float | None = None) -> list[dict]:
    """저장된 스캔에서 고른 분류에 걸리는 사람 (스캔 뒤 제외·자유 멤버로 바뀐 사람은 뺌). 각 줄에 'cats'."""
    now = now or time.time()
    if not sel:
        return []
    skip = await excluded_ids(svc.db, chat_id) | await free_ids(svc.db, chat_id)
    out = []
    for r in await svc.db._all("SELECT * FROM cleanup_cands WHERE chat_id=? ORDER BY user_id", (chat_id,)):
        if r["user_id"] in skip:
            continue
        cats = cats_of(r, sel, now)
        if cats:
            d = dict(r)
            d["cats"] = cats
            out.append(d)
    return out


def dist_line(dist: dict) -> str:
    exact = sum(dist.get(k, 0) for k in ("online", "offline"))
    rough = sum(dist.get(k, 0) for k in ("recently", "last_week", "last_month"))
    return f"정확 {exact} · 대략(숨김) {rough} · 모름 {dist.get('unknown', 0)}"


# ── 제외 명단 ───────────────────────────────────────────────
async def exclude(db, chat_id: int, user_id: int, by_id: int, on: bool) -> bool:
    if on:
        n = await _rc(db, "INSERT OR IGNORE INTO cleanup_excl(chat_id, user_id, by_id, ts) VALUES(?,?,?,?)",
                      (chat_id, user_id, by_id, int(time.time())))
    else:
        n = await _rc(db, "DELETE FROM cleanup_excl WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    if n:
        await db.log_mod(chat_id, by_id, user_id, "cleanup_excl", "추가" if on else "해제")
    return bool(n)


async def excluded_rows(db, chat_id: int) -> list:
    return await db._all("SELECT e.user_id, COALESCE(u.first_name, NULLIF(c.name, '')) AS first_name, u.last_name, "
                         "COALESCE(u.username, NULLIF(c.username, '')) AS username FROM cleanup_excl e "
                         "LEFT JOIN users u ON u.user_id=e.user_id "
                         "LEFT JOIN cleanup_cands c ON c.chat_id=e.chat_id AND c.user_id=e.user_id "
                         "WHERE e.chat_id=? ORDER BY e.ts", (chat_id,))


# ── 작업 (뒤에서 한 명씩) ───────────────────────────────────
@dataclass
class Outcome:
    kind: str        # kicked · gone · skipped · failed · halt
    why: str = ""


_TASKS: dict[tuple, asyncio.Task] = {}
_leaving: dict[tuple, float] = {}


def _key(svc: Services, chat_id: int) -> tuple:
    return (svc.db.path, chat_id)


async def get_job(db, chat_id: int) -> dict | None:
    row = await db._one("SELECT * FROM cleanup_jobs WHERE chat_id=?", (chat_id,))
    if row is None:
        return None
    d = dict(row)
    try:
        d["fails"] = json.loads(d["fails"] or "{}")
    except ValueError:
        d["fails"] = {}
    return d


async def remaining(db, chat_id: int) -> int:
    return (await db._one("SELECT COUNT(*) AS n FROM cleanup_queue WHERE chat_id=?", (chat_id,)))["n"]


def running_here(svc: Services, chat_id: int) -> bool:
    t = _TASKS.get(_key(svc, chat_id))
    return bool(t and not t.done())


async def bot_can_ban(bot, chat_id: int) -> bool | None:
    """봇이 그 방에서 '사용자 차단' 권한이 있는지. 연결 오류면 None (모름)."""
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except (Forbidden, BadRequest):
        return False
    except TelegramError:
        return None
    return me.status == "creator" or (me.status == "administrator" and bool(getattr(me, "can_restrict_members", False)))


async def job_leave(svc: Services, chat_id: int, user_id: int, by_id: int | None = None, bot_id: int | None = None) -> bool:
    """이 퇴장이 멤버 정리 작업이 낸 것인지 (작별 인사·서비스 메시지 처리용). 내보내기 직전에 적어 둔 기록,
    또는 소담이 내보냈고 이 방 작업이 도는 중 (재시작 직후 기록이 없어도)."""
    t = _leaving.get((svc.db.path, chat_id, user_id))
    if t and time.time() - t < LEAVE_TTL:
        return True
    if by_id is not None and bot_id is not None and by_id == bot_id:
        job = await get_job(svc.db, chat_id)
        return bool(job and job["state"] == "running")
    return False


def _mark_leaving(svc: Services, chat_id: int, user_id: int) -> None:
    now = time.time()
    if len(_leaving) > 20000:
        for k in [k for k, v in _leaving.items() if now - v > LEAVE_TTL]:
            del _leaving[k]
    _leaving[(svc.db.path, chat_id, user_id)] = now


async def start_job(svc: Services, bot, chat_id: int, by_id: int, sel: list[str], scan_ts: int) -> tuple[bool, str]:
    """확인 카드 [🧹 내보내기] → 작업 시작. 누를 때 모든 조건을 다시 본다."""
    from .permissions import may
    scan_row = await last_scan(svc.db, chat_id)
    if not scan_row or scan_row["ts"] != scan_ts or not scan_fresh(scan_row):
        return False, "스캔이 바뀌었거나 30분이 지났어요. [🔄 다시 스캔] 뒤 다시 확인해주세요."
    svc.perms.forget(chat_id)
    if not await may(svc.perms, bot, chat_id, by_id, "restrict"):
        return False, "텔레그램 '사용자 차단' 권한이 있는 관리자만 할 수 있어요."
    if not await bot_can_ban(bot, chat_id):
        return False, "소담에게 이 방 '사용자 차단' 권한이 없어요. 텔레그램 관리자 설정에서 켜 주세요."
    rows = await selection(svc, chat_id, sel)
    if not rows:
        return False, "내보낼 사람이 없어요 (그 사이 제외·자유 멤버로 바뀌었을 수 있어요)."
    try:
        before = await bot.get_chat_member_count(chat_id)
    except TelegramError:
        before = None
    now = int(time.time())

    def run(c) -> bool:
        row = c.execute("SELECT state FROM cleanup_jobs WHERE chat_id=?", (chat_id,)).fetchone()
        if row and row[0] == "running":
            return False   # 방마다 작업 1개 (두 관리자가 동시에 눌러도 하나만)
        c.execute("DELETE FROM cleanup_queue WHERE chat_id=?", (chat_id,))
        c.execute("INSERT OR REPLACE INTO cleanup_jobs(chat_id, by_id, sel, state, created, beat, total, before_n) "
                  "VALUES(?,?,?,?,?,?,?,?)", (chat_id, by_id, sel_str(sel), "running", now, time.time(), len(rows), before))
        c.executemany("INSERT INTO cleanup_queue(chat_id, user_id, pos, reason) VALUES(?,?,?,?)",
                      [(chat_id, r["user_id"], i, " · ".join(REASON[t[0]] + (f" {t[1:]}일" if t[0] == "i" else "")
                                                               for t in r["cats"]))
                       for i, r in enumerate(rows)])
        return True
    if not await svc.db.atomic(run):
        return False, "이 방은 이미 멤버 정리가 진행 중이에요. 끝나거나 멈춘 뒤에 다시 해주세요."
    await svc.db.log_mod(chat_id, by_id, None, "member_cleanup", f"시작 {len(rows)}명 ({', '.join(cat_label(t) for t in sel)})")
    launch(svc, bot, chat_id)
    return True, (f"🧹 시작했어요 — {len(rows)}명을 한 명씩 ({KICK_GAP:g}초 간격) 내보내요. "
                  "진행 상황은 이 1:1 로 알려드려요.")


def launch(svc: Services, bot, chat_id: int) -> bool:
    """이 프로세스에서 작업을 돌린다 (이미 돌면 False)."""
    if running_here(svc, chat_id):
        return False
    task = persist.spawn(_runner(svc, bot, chat_id))
    if task is None:
        return False
    _TASKS[_key(svc, chat_id)] = task
    return True


async def stop_job(svc: Services, chat_id: int, by_id: int) -> bool:
    n = await _rc(svc.db, "UPDATE cleanup_jobs SET state='stopped', why=? WHERE chat_id=? AND state='running'",
                  ("관리자가 멈춤", chat_id))
    if n:
        await svc.db.log_mod(chat_id, by_id, None, "member_cleanup", "중지")
    return bool(n)


async def resume_job(svc: Services, bot, chat_id: int, by_id: int) -> tuple[bool, str]:
    """[▶️ 이어서]: 멈춘(중지·권한 빠짐·속도 제한) 작업을 남은 사람부터."""
    from .permissions import may
    job = await get_job(svc.db, chat_id)
    if not job or job["state"] not in ("stopped", "halted") or not await remaining(svc.db, chat_id):
        return False, "이어서 할 작업이 없어요."
    svc.perms.forget(chat_id)
    if not await may(svc.perms, bot, chat_id, by_id, "restrict"):
        return False, "텔레그램 '사용자 차단' 권한이 있는 관리자만 할 수 있어요."
    if not await bot_can_ban(bot, chat_id):
        return False, "소담에게 아직 '사용자 차단' 권한이 없어요."
    n = await _rc(svc.db, "UPDATE cleanup_jobs SET state='running', why='', beat=? WHERE chat_id=? AND state IN "
                  "('stopped','halted')", (time.time(), chat_id))
    if not n:
        return False, "이미 다시 시작됐어요."
    await svc.db.log_mod(chat_id, by_id, None, "member_cleanup", "이어서")
    launch(svc, bot, chat_id)
    return True, f"▶️ 이어서 해요 (남은 {await remaining(svc.db, chat_id)}명)."


async def resume_all(svc: Services, bot, *, stale_only: bool = False) -> int:
    """봇 시작 때(post_init) · 30초 틱: DB 에 '진행 중'인데 이 프로세스에서 안 도는 작업을 이어 돌린다.
    틱에선 LEASE 동안 소식 없는 것만 (다른 프로세스가 돌리는 중일 수 있어서)."""
    if getattr(svc.cfg, "bot_role", "all") == "dealer":
        return 0
    rows = await svc.db._all("SELECT chat_id, beat FROM cleanup_jobs WHERE state='running'")
    n = 0
    for r in rows:
        if stale_only and time.time() - (r["beat"] or 0) < LEASE:
            continue
        if launch(svc, bot, r["chat_id"]):
            n += 1
    if n:
        log.info("멤버 정리 작업 %d개 이어서", n)
    return n


def _secs(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def _tg(fn):
    """RetryAfter 면 그만큼 쉬고 다시 (최대 RETRY_MAX 번, RETRY_MAX_WAIT 넘게 기다리라면 그대로 올림)."""
    for i in range(RETRY_MAX):
        try:
            return await fn()
        except RetryAfter as e:
            if i == RETRY_MAX - 1 or _secs(e) > RETRY_MAX_WAIT:
                raise
            await _sleep(_secs(e) + 0.5)


def _rights_error(e: TelegramError) -> bool:
    m = str(getattr(e, "message", e)).lower()
    return isinstance(e, Forbidden) or "not enough rights" in m or "rights" in m or "chat_admin_required" in m \
        or "have no rights" in m


async def kick_one(svc: Services, bot, chat_id: int, uid: int, reason: str, by_id: int) -> Outcome:
    """한 사람: 보호 다시 확인 → 아직 멤버·관리자 아님 확인 → ban → unban → 기록."""
    why = await still_protected(svc, bot, chat_id, uid)
    if why:
        return Outcome("skipped", why)
    try:
        m = await _tg(lambda: bot.get_chat_member(chat_id, uid))
    except RetryAfter:
        return Outcome("halt", "텔레그램 속도 제한이 길어요")
    except BadRequest as e:
        if _rights_error(e):
            return Outcome("halt", "소담의 권한이 빠졌어요")
        if "chat" in str(getattr(e, "message", e)).lower():   # chat not found = 방이 없어짐 → 전원 '나감'으로 세지 않게
            return Outcome("halt", "방을 찾을 수 없어요 (소담이 빠졌거나 방이 바뀜)")
        return Outcome("gone", "")   # user not found = 이미 없음
    except Forbidden:
        return Outcome("halt", "소담이 방에서 빠졌거나 권한이 없어요")
    except TelegramError as e:
        return Outcome("failed", f"확인 실패: {e.message[:40]}")
    status = str(getattr(m, "status", ""))
    if status in ("left", "kicked") or (status == "restricted" and getattr(m, "is_member", True) is False):
        return Outcome("gone")
    if status in ("administrator", "creator"):
        return Outcome("skipped", "관리자")
    _mark_leaving(svc, chat_id, uid)
    try:
        await _tg(lambda: bot.ban_chat_member(chat_id, uid))
    except RetryAfter:
        return Outcome("halt", "텔레그램 속도 제한이 길어요")
    except TelegramError as e:
        if _rights_error(e):
            return Outcome("halt", "소담의 '사용자 차단' 권한이 빠졌어요")
        return Outcome("failed", f"내보내기 실패: {getattr(e, 'message', str(e))[:40]}")
    try:
        await _tg(lambda: bot.unban_chat_member(chat_id, uid, only_if_banned=True))
    except TelegramError as e:
        await svc.db.log_mod(chat_id, by_id, uid, "ban", f"멤버 정리: {reason} (밴 풀기 실패 — 직접 풀어주세요)")
        return Outcome("failed", f"밴 풀기 실패(ID {uid} — .밴해제 로 풀어주세요): {getattr(e, 'message', str(e))[:30]}")
    await svc.db.log_mod(chat_id, by_id, uid, "kick", f"멤버 정리: {reason}")
    return Outcome("kicked")


def _stop_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏸ 중지", callback_data=f"m:mch:{chat_id}")]])


def _resume_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ 이어서", callback_data=f"m:mcg:{chat_id}"),
                                  InlineKeyboardButton("🧹 멤버 정리", callback_data=f"m:mc:{chat_id}")]])


def progress_text(job: dict, title: str, left: int) -> str:
    done = job["kicked"] + job["gone"] + job["skipped"] + job["failed"]
    return (f"🧹 <b>멤버 정리 중</b> · {esc(clip(title, 40))}\n"
            f"처리 {done}/{job['total']} · 내보냄 {job['kicked']} · 이미 나감 {job['gone']} · 건너뜀 {job['skipped']} · "
            f"실패 {job['failed']}\n남은 {left}명 · 한 명씩 {KICK_GAP:g}초 간격")


def report_text(job: dict, title: str, after: int | None, tz) -> str:
    mins = max(0, int(((job.get("ended") or time.time()) - job["created"]) // 60))
    lines = [f"✅ <b>멤버 정리 끝</b> · {esc(clip(title, 40))}",
             f"내보냄 <b>{job['kicked']}</b> · 이미 나감 {job['gone']} · 건너뜀(보호·관리자) {job['skipped']} · 실패 {job['failed']}",
             f"걸린 시간 약 {mins}분"
             + (f" · 방 인원 {job['before_n']} → {after}" if job.get("before_n") is not None and after is not None else "")]
    if job["fails"]:
        lines.append("실패 이유: " + " · ".join(f"{esc(k)} {v}" for k, v in list(job["fails"].items())[:5]))
    lines.append("<i>내보낸 사람은 다시 들어올 수 있어요 (밴 아님). 관리 기록에 한 사람씩 남았어요.</i>")
    return "\n".join(lines)


async def _title(svc: Services, chat_id: int) -> str:
    row = await svc.db._one("SELECT title FROM chats WHERE chat_id=?", (chat_id,))
    return (row["title"] if row else None) or str(chat_id)


async def _dm(svc: Services, bot, job: dict, text: str, kb, *, edit: bool) -> None:
    """관리자 1:1 로만 (방엔 명단·진행을 안 뿌림). 진행 메시지는 고치고, 못 고치면 새로."""
    uid = job["by_id"]
    if edit and job.get("dm_msg"):
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=job["dm_msg"], parse_mode="HTML", reply_markup=kb)
            return
        except TelegramError as e:
            if "not modified" in str(e).lower():
                return
    try:
        sent = await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        await svc.db._write("UPDATE cleanup_jobs SET dm_msg=? WHERE chat_id=?", (sent.message_id, job["chat_id"]))
        job["dm_msg"] = sent.message_id
    except TelegramError as e:
        log.info("멤버 정리 1:1 알림 실패 chat %s: %s", job["chat_id"], e)


async def _halt(svc: Services, bot, job: dict, why: str, title: str) -> None:
    if not await _rc(svc.db, "UPDATE cleanup_jobs SET state='halted', why=? WHERE chat_id=? AND state='running'",
                     (why, job["chat_id"])):
        return   # 그 사이 관리자가 멈췄거나 끝남
    await svc.db.log_mod(job["chat_id"], None, None, "member_cleanup", f"멈춤: {why}")
    left = await remaining(svc.db, job["chat_id"])
    await _dm(svc, bot, job, f"⏸ <b>멤버 정리 멈춤</b> · {esc(clip(title, 40))}\n이유: {esc(why)}\n"
                              f"내보냄 {job['kicked']} · 남은 {left}명. 고친 뒤 [▶️ 이어서] 를 눌러주세요.",
              _resume_kb(job["chat_id"]), edit=False)


async def _runner(svc: Services, bot, chat_id: int) -> None:
    db, key = svc.db, _key(svc, chat_id)
    title = await _title(svc, chat_id)
    try:
        job = await get_job(db, chat_id)
        if job and job["state"] == "running":
            await _dm(svc, bot, job, progress_text(job, title, await remaining(db, chat_id)), _stop_kb(chat_id), edit=True)
        n, last_edit = 0, time.monotonic()
        while True:
            job = await get_job(db, chat_id)
            if not job or job["state"] != "running":
                if job and job["state"] == "stopped":
                    await _dm(svc, bot, job, f"⏸ <b>멤버 정리 멈춤</b> · {esc(clip(title, 40))}\n내보냄 {job['kicked']} · "
                                             f"남은 {await remaining(db, chat_id)}명. [▶️ 이어서] 로 남은 사람부터.",
                              _resume_kb(chat_id), edit=True)
                return
            await db._write("UPDATE cleanup_jobs SET beat=? WHERE chat_id=?", (time.time(), chat_id))
            nxt = await db._one("SELECT user_id, reason FROM cleanup_queue WHERE chat_id=? ORDER BY pos LIMIT 1", (chat_id,))
            if nxt is None:
                await _finish(svc, bot, job, title)
                return
            if n % RIGHTS_EVERY == 0 and await bot_can_ban(bot, chat_id) is False:
                await _halt(svc, bot, job, "소담의 '사용자 차단' 권한이 빠졌어요", title)
                return
            out = await kick_one(svc, bot, chat_id, nxt["user_id"], nxt["reason"], job["by_id"])
            if out.kind == "halt":
                await _halt(svc, bot, job, out.why, title)
                return
            col = {"kicked": "kicked", "gone": "gone", "skipped": "skipped", "failed": "failed"}[out.kind]
            uid = nxt["user_id"]

            def step(c) -> None:
                c.execute("DELETE FROM cleanup_queue WHERE chat_id=? AND user_id=?", (chat_id, uid))
                c.execute(f"UPDATE cleanup_jobs SET {col}={col}+1 WHERE chat_id=?", (chat_id,))
                if out.kind in ("kicked", "gone"):
                    c.execute("DELETE FROM cleanup_cands WHERE chat_id=? AND user_id=?", (chat_id, uid))
                if out.kind == "failed":
                    row = c.execute("SELECT fails FROM cleanup_jobs WHERE chat_id=?", (chat_id,)).fetchone()
                    fails = json.loads(row[0] or "{}") if row else {}
                    k = out.why.split(":")[0][:40]
                    fails[k] = fails.get(k, 0) + 1
                    c.execute("UPDATE cleanup_jobs SET fails=? WHERE chat_id=?", (json.dumps(fails, ensure_ascii=False), chat_id))
            await db.atomic(step)
            n += 1
            if n % PROGRESS_EVERY == 0 and time.monotonic() - last_edit >= PROGRESS_MIN_SEC:
                last_edit = time.monotonic()
                job = await get_job(db, chat_id)
                await _dm(svc, bot, job, progress_text(job, title, await remaining(db, chat_id)), _stop_kb(chat_id), edit=True)
            await _sleep(KICK_GAP)
    except asyncio.CancelledError:
        raise   # 종료(배포): DB 엔 '진행 중'으로 남음 → 다음 시작 때 이어서
    except Exception:
        log.exception("멤버 정리 작업 오류 chat %s", chat_id)
        job = await get_job(db, chat_id)
        if job:
            await _halt(svc, bot, job, "뜻밖의 오류 (서버 로그)", title)
    finally:
        if _TASKS.get(key) is asyncio.current_task():
            _TASKS.pop(key, None)


async def _finish(svc: Services, bot, job: dict, title: str) -> None:
    chat_id = job["chat_id"]
    now = int(time.time())
    n = await _rc(svc.db, "UPDATE cleanup_jobs SET state='done', ended=? WHERE chat_id=? AND state='running'", (now, chat_id))
    if not n:
        return
    job = await get_job(svc.db, chat_id)
    try:
        after = await bot.get_chat_member_count(chat_id)
    except TelegramError:
        after = None
    await svc.db.log_mod(chat_id, job["by_id"], None, "member_cleanup",
                         f"끝: 내보냄 {job['kicked']} · 이미 나감 {job['gone']} · 건너뜀 {job['skipped']} · 실패 {job['failed']}")
    await _dm(svc, bot, job, report_text(job, title, after, svc.cfg.tz),
              InlineKeyboardMarkup([[InlineKeyboardButton("🧹 멤버 정리", callback_data=f"m:mc:{chat_id}")]]), edit=True)


async def shutdown(svc: Services) -> int:
    """봇 종료 때: 이 프로세스의 작업을 멈춤 (DB 엔 '진행 중' 그대로 → 다음 시작 때 이어서)."""
    tasks = [t for (path, _), t in list(_TASKS.items()) if path == svc.db.path and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    return 0


async def tick(svc: Services, bot) -> None:
    """30초 틱: 죽은(소식 없는) 작업을 이어 받는다."""
    await resume_all(svc, bot, stale_only=True)
