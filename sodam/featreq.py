"""💡 기능 요청 받기 — 소담이 지금 못 하는 일을 멤버·관리자가 말하면 운영자(오너)에게 모아 전달한다.

- 받기: AI 도구 feature_request(summary, detail) (panels/featreq.py). 글은 **데이터로만** 저장 (≤500자, 제어 글자 제거,
  화면에선 전부 esc — 실행·AI 지시로 쓰지 않음).
- 묶기: 같은 사람 + 비슷한 글 24시간 안 = 그 줄 count +1 (새 줄 없음) · 다른 사람의 비슷한 요청 = 같은 묶음(group) →
  '👍 n명'(서로 다른 요청자 수). 비슷함 = 정규화 글(NFKC·소문자·흔한 말 빼고 글자·숫자만)의 글자 3-gram
  Jaccard ≥ 0.5 또는 겹침 계수 ≥ 0.8, 또는 2-gram(4개↑) Jaccard ≥ 0.45 + 겹침 ≥ 0.8 (짧은 말은 포함 관계).
  끝난(완료·안 함) 묶음엔 안 붙음.
- 한도: 한 사람 24시간 FR_PER_DAY(5)번 (묶임·한 번 더 셈 포함). 한도 확인 + 기록은 한 번의 db.atomic.
- 상태: new → doing → done/wont. 바꾸기는 status 를 조건에 넣은 UPDATE 한 문장 → 두 번 눌러도 한 번만.
  완료 알림은 그 UPDATE 가 바꾼 경우에만 → 요청자마다 1:1 DM 1통 (items.notified 로 한 번만),
  1:1 막힘(Forbidden)이면 그 사람이 요청한 방에 짧은 안내 — 묶음·방마다 1통(막힌 사람 여러 명이면 한 줄에 같이 멘션).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass

from telegram import Bot
from telegram.error import Forbidden, TelegramError

from .db import DB, register_schema
from .util import esc, mention

log = logging.getLogger(__name__)

MAX_TEXT = 500
SUMMARY_CHARS = 80
NOTE_CHARS = 200
FR_PER_DAY = 5
DEDUPE_SECONDS = 86400
MATCH_GROUPS = 500            # 비교할 최근 요청 글 수 (아주 많아져도 요청 1번이 느려지지 않게)
MAX_NOTIFY = 300              # 완료 알림 최대 인원 (한 번 누름에 텔레그램 호출이 끝없이 늘지 않게)
ROOM_MENTIONS = 5
OPEN = ("new", "doing")
STATUS_LABEL = {"new": "🆕 새 요청", "doing": "🛠 진행 중", "done": "✅ 완료", "wont": "🙅 안 함"}

register_schema("""
CREATE TABLE IF NOT EXISTS featreq_groups (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    summary  TEXT NOT NULL,
    norm     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'new',     -- new / doing / done / wont
    note     TEXT NOT NULL DEFAULT '',        -- 완료 때 오너가 쓴 짧은 메모
    created  INTEGER NOT NULL,
    updated  INTEGER NOT NULL,
    done_by  INTEGER
);
CREATE INDEX IF NOT EXISTS featreq_groups_status ON featreq_groups(status, updated);
CREATE TABLE IF NOT EXISTS featreq_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    chat_id   INTEGER,                        -- 요청한 방 (1:1 이면 NULL)
    text      TEXT NOT NULL,
    norm      TEXT NOT NULL,
    count     INTEGER NOT NULL DEFAULT 1,     -- 같은 사람이 24시간 안에 또 말한 횟수 포함
    created   INTEGER NOT NULL,
    last_ts   INTEGER NOT NULL,
    notified  TEXT                            -- NULL / dm / room / skip (완료 알림 한 번만)
);
CREATE INDEX IF NOT EXISTS featreq_items_group ON featreq_items(group_id);
CREATE INDEX IF NOT EXISTS featreq_items_user ON featreq_items(user_id, last_ts);
CREATE INDEX IF NOT EXISTS featreq_items_chat ON featreq_items(chat_id);
CREATE TABLE IF NOT EXISTS featreq_log (
    user_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS featreq_log_user ON featreq_log(user_id, ts);
""", migrate={"featreq_items": "plain"})

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")
_FILLER = re.compile(r"(소담아|소담이|소담|기능|요청|있어요|있나요|있어|해주세요|해줘|주세요|같은|이런|좀|거|것)")
_NON_WORD = re.compile(r"[^0-9a-z가-힣]")


def clean(text: str, limit: int = MAX_TEXT) -> str:
    """저장용: 제어·방향 글자 제거, 공백 정리, 길이 제한. (화면에 보일 땐 따로 esc)"""
    return " ".join(_CTRL.sub(" ", unicodedata.normalize("NFKC", str(text or ""))).split())[:limit]


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = _FILLER.sub(" ", t)
    return _NON_WORD.sub("", t)[:MAX_TEXT]


def _grams(s: str, k: int = 3) -> set[str]:
    return {s[i:i + k] for i in range(len(s) - k + 1)} or ({s} if s else set())


def similar(a: str, b: str) -> bool:
    """정규화된 두 글이 같은 요청인지 (간단한 글자 3-gram)."""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 3:                   # '투표' 같은 짧은 말은 포함 관계로
        return a in b or b in a
    ga, gb = _grams(a), _grams(b)
    inter = len(ga & gb)
    if inter / len(ga | gb) >= 0.5 or (min(len(ga), len(gb)) >= 2 and inter / min(len(ga), len(gb)) >= 0.8):
        return True
    # 조사·어미 한 글자 차이('입장 때' / '입장할 때')는 3-gram 이 많이 깨짐 → 2-gram 이 둘 다 높을 때만
    ba, bb = _grams(a, 2), _grams(b, 2)
    inter = len(ba & bb)
    return min(len(ba), len(bb)) >= 4 and inter / len(ba | bb) >= 0.45 and inter / min(len(ba), len(bb)) >= 0.8


@dataclass
class Submitted:
    outcome: str        # new / grouped / again / limit / short
    group_id: int = 0
    voters: int = 0
    status: str = "new"


def _voters(c, gid: int) -> int:
    return c.execute("SELECT COUNT(DISTINCT user_id) FROM featreq_items WHERE group_id=?", (gid,)).fetchone()[0]


async def submit(db: DB, user_id: int, user_name: str, chat_id: int | None, summary: str, detail: str = "",
                 now: int | None = None) -> Submitted:
    summary = clean(summary, SUMMARY_CHARS)
    detail = clean(detail)
    text = clean(summary + (" — " + detail if detail and detail != summary else ""))
    norm = normalize(summary) or normalize(text)
    if len(norm) < 2:
        return Submitted("short")
    now = int(now or time.time())
    name = clean(user_name, 64)
    room = chat_id if chat_id is not None and chat_id < 0 else None

    def run(c) -> Submitted:
        used = c.execute("SELECT COUNT(*) FROM featreq_log WHERE user_id=? AND ts>?", (user_id, now - 86400)).fetchone()[0]
        if used >= FR_PER_DAY:
            return Submitted("limit")
        c.execute("INSERT INTO featreq_log(user_id, ts) VALUES(?,?)", (user_id, now))
        c.execute("DELETE FROM featreq_log WHERE ts<?", (now - 2 * 86400,))
        # 1) 같은 사람 + 비슷한 글 24시간 안 → 한 번 더 셈 (새 줄 없음)
        mine = c.execute("SELECT i.id, i.group_id, i.norm, g.status FROM featreq_items i JOIN featreq_groups g ON g.id=i.group_id "
                         "WHERE i.user_id=? AND i.last_ts>? ORDER BY i.last_ts DESC", (user_id, now - DEDUPE_SECONDS)).fetchall()
        for iid, gid, inorm, status in mine:
            if similar(norm, inorm):
                c.execute("UPDATE featreq_items SET count=count+1, last_ts=? WHERE id=?", (now, iid))
                return Submitted("again", gid, _voters(c, gid), status)
        # 2) 다른 사람의 비슷한 요청(열린 묶음) → 같은 묶음에
        #    (묶음 안 모든 요청 글과 비교 — 묶음 첫 글과 표현이 달라도 누군가의 글과 비슷하면 같은 묶음)
        rows = c.execute("SELECT DISTINCT i.group_id, i.norm FROM featreq_items i JOIN featreq_groups g ON g.id=i.group_id "
                         "WHERE g.status IN ('new','doing') ORDER BY i.last_ts DESC LIMIT ?", (MATCH_GROUPS,)).fetchall()
        gid = next((g for g, inorm in rows if similar(norm, inorm)), None)
        outcome = "grouped"
        if gid is None:
            gid = c.execute("INSERT INTO featreq_groups(summary, norm, status, created, updated) VALUES(?,?,?,?,?)",
                            (summary or text[:SUMMARY_CHARS], norm, "new", now, now)).lastrowid
            outcome = "new"
        else:
            c.execute("UPDATE featreq_groups SET updated=? WHERE id=?", (now, gid))
        c.execute("INSERT INTO featreq_items(group_id, user_id, user_name, chat_id, text, norm, count, created, last_ts) "
                  "VALUES(?,?,?,?,?,?,1,?,?)", (gid, user_id, name, room, text, norm, now, now))
        status = c.execute("SELECT status FROM featreq_groups WHERE id=?", (gid,)).fetchone()[0]
        return Submitted(outcome, gid, _voters(c, gid), status)

    return await db.atomic(run)


# ── 조회 ──────────────────────────────────────────────────
_GROUP_COLS = ("SELECT g.id, g.summary, g.status, g.note, g.created, g.updated, "
               "COUNT(DISTINCT i.user_id) AS voters, COALESCE(SUM(i.count),0) AS asks, MAX(i.last_ts) AS last_ts "
               "FROM featreq_groups g LEFT JOIN featreq_items i ON i.group_id=g.id ")
STATUS_SETS = {"o": OPEN, "d": ("done",), "w": ("wont",)}


async def list_groups(db: DB, statuses=OPEN, sort: str = "v", limit: int = 10, offset: int = 0, chat_id: int | None = None):
    marks = ",".join("?" * len(statuses))
    where = f"WHERE g.status IN ({marks})"
    params: tuple = tuple(statuses)
    if chat_id is not None:
        where += " AND g.id IN (SELECT group_id FROM featreq_items WHERE chat_id=?)"
        params += (chat_id,)
    order = "voters DESC, last_ts DESC, g.id DESC" if sort == "v" else "g.created DESC, g.id DESC"
    return await db._all(f"{_GROUP_COLS}{where} GROUP BY g.id ORDER BY {order} LIMIT ? OFFSET ?", params + (limit, offset))


async def count_groups(db: DB, statuses=OPEN, chat_id: int | None = None) -> int:
    marks = ",".join("?" * len(statuses))
    sql = f"SELECT COUNT(*) AS n FROM featreq_groups g WHERE g.status IN ({marks})"
    params: tuple = tuple(statuses)
    if chat_id is not None:
        sql += " AND g.id IN (SELECT group_id FROM featreq_items WHERE chat_id=?)"
        params += (chat_id,)
    return (await db._one(sql, params))["n"]


async def get_group(db: DB, gid: int):
    return await db._one(f"{_GROUP_COLS}WHERE g.id=? GROUP BY g.id", (gid,))


async def items(db: DB, gid: int):
    return await db._all("SELECT * FROM featreq_items WHERE group_id=? ORDER BY created", (gid,))


# ── 상태 바꾸기 (한 번만) ────────────────────────────────────
_FROM = {"doing": ("new",), "done": OPEN, "wont": OPEN}


async def set_status(db: DB, gid: int, status: str, actor: int, note: str = "") -> bool:
    """status 조건이 붙은 UPDATE 한 문장 → 바뀌었으면 True (두 번 눌러도·동시에 눌러도 한 번만)."""
    allowed = _FROM[status]
    marks = ",".join("?" * len(allowed))

    def run(c) -> bool:
        cur = c.execute(f"UPDATE featreq_groups SET status=?, note=?, updated=?, done_by=? WHERE id=? AND status IN ({marks})",
                        (status, clean(note, NOTE_CHARS), int(time.time()), actor, gid, *allowed))
        return cur.rowcount == 1
    return await db.atomic(run)


async def delete(db: DB, gid: int) -> bool:
    def run(c) -> bool:
        n = c.execute("DELETE FROM featreq_groups WHERE id=?", (gid,)).rowcount
        c.execute("DELETE FROM featreq_items WHERE group_id=?", (gid,))
        return n == 1
    return await db.atomic(run)


async def _claim(db: DB, item_id: int, how: str) -> bool:
    def run(c) -> bool:
        return c.execute("UPDATE featreq_items SET notified=? WHERE id=? AND notified IS NULL", (how, item_id)).rowcount == 1
    return await db.atomic(run)


def done_text(summary: str, note: str) -> str:
    lines = [f"✅ 요청하신 '<b>{esc(summary)}</b>' 기능이 추가됐어요!"]
    if note:
        lines.append(f"📝 {esc(note)}")
    lines.append("소담에게 필요한 걸 알려주셔서 고마워요 🙏")
    return "\n".join(lines)


async def notify_done(db: DB, bot: Bot, gid: int) -> tuple[int, int]:
    """완료 알림: 요청자마다 1:1 한 통. 막힘이면 그 사람이 요청한 방에 묶음·방마다 한 줄. → (1:1 보낸 수, 방 안내 수)."""
    g = await get_group(db, gid)
    if not g or g["status"] != "done":
        return 0, 0
    text = done_text(g["summary"], g["note"])
    seen: set[int] = set()
    blocked: dict[int, list[tuple[int, str]]] = {}
    sent = 0
    for it in (await items(db, gid))[:MAX_NOTIFY]:
        uid = it["user_id"]
        if uid in seen or it["notified"] is not None:
            if uid in seen:
                await _claim(db, it["id"], "skip")     # 같은 사람 다른 줄 — 이미 한 통
            continue
        seen.add(uid)
        if not await _claim(db, it["id"], "dm"):
            continue
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Forbidden:
            if it["chat_id"]:
                blocked.setdefault(it["chat_id"], []).append((uid, it["user_name"] or "멤버"))
                await db._write("UPDATE featreq_items SET notified='room' WHERE id=?", (it["id"],))
        except TelegramError as e:
            log.warning("featreq notify %s failed: %s", uid, e)
    rooms = 0
    for cid, people in blocked.items():
        who = ", ".join(mention(u, n) for u, n in people[:ROOM_MENTIONS]) + (f" 외 {len(people) - ROOM_MENTIONS}명"
                                                                              if len(people) > ROOM_MENTIONS else "")
        try:
            await bot.send_message(cid, f"💡 {who}님, 요청하신 '{esc(g['summary'])}' 기능이 추가됐어요.", parse_mode="HTML",
                                   disable_notification=True)
            rooms += 1
        except TelegramError as e:
            log.info("featreq room notice %s failed: %s", cid, e)
    return sent, rooms
