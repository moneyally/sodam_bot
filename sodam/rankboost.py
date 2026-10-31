"""🚀 채팅 집계 부스트 구간 — 잠깐 최소 글자수를 낮춰 집계한 '그 기간' 글을 영구 보존.

2026-10-31 뉴월드 NASA: 평소 5글자 집계인데 이벤트로 2글자 '부스트' → 되돌려도 그때 친 짧은 글은 순위에 남기고 싶다.
기존 chat_min_chars 는 '지금 설정값으로 전체 소급'이라 되돌리면 그 짧은 글이 빠진다 (count_filter).
→ 부스트는 **구간**(from_ts~to_ts)으로 저장해, 그 구간에 쳐진 글은 **그 당시 낮은 기준**으로 영구 집계.
   구간 밖 글은 방 기본 chat_min_chars 그대로(소급). 부스트 구간이 하나도 없는 방은 예전 빠른 경로(회귀 0).

집계 적용은 db.count_filter 가 한다: 글 ts 가 어떤 부스트 구간 안이면 그 구간 min_chars, 아니면 방 기본값.
여기선 구간 켜기/끄기·'이 방에 구간 있나(has_boost)'만. has_boost 가 True 면 CountRule.boosted → 서브쿼리 경로.
"""
from __future__ import annotations

import time

from . import db as dbmod

dbmod.register_schema("""
CREATE TABLE IF NOT EXISTS rank_boost (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    from_ts    INTEGER NOT NULL,
    to_ts      INTEGER,                      -- NULL = 진행 중(지금 켜진 부스트)
    min_chars  INTEGER NOT NULL DEFAULT 0,   -- 이 구간 동안 집계할 최소 글자 수 (0=전부)
    created_by INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rank_boost ON rank_boost(chat_id, from_ts);
""", migrate={"rank_boost": "plain"})

MAX_PER_CHAT = 100          # 방당 구간 상한 (너무 쌓이면 집계 서브쿼리가 느려짐)
_CACHE: dict[int, tuple[float, bool]] = {}   # chat_id → (만료 monotonic, 구간 있나)
_TTL = 30.0


def _now() -> int:
    return int(time.time())


async def has_boost(db, chat_id: int) -> bool:
    """이 방에 부스트 구간이 하나라도 있나 — 집계 쿼리가 구간별 min 경로를 쓸지 결정 (없으면 예전 빠른 경로)."""
    hit = _CACHE.get(chat_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    on = await db._one("SELECT 1 FROM rank_boost WHERE chat_id=? LIMIT 1", (chat_id,)) is not None
    _CACHE[chat_id] = (time.monotonic() + _TTL, on)
    return on


def _clear(chat_id: int) -> None:
    _CACHE.pop(chat_id, None)


async def current(db, chat_id: int):
    """진행 중(to_ts NULL) 구간 또는 None."""
    return await db._one(
        "SELECT * FROM rank_boost WHERE chat_id=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1", (chat_id,))


async def start(db, chat_id: int, min_chars: int, by: int | None) -> tuple[bool, str]:
    """부스트 켜기: 진행 중 구간이 있으면 먼저 닫고 새로 시작. (성공, 안내)."""
    mc = max(0, min(50, int(min_chars)))
    cnt = (await db._one("SELECT COUNT(*) AS n FROM rank_boost WHERE chat_id=?", (chat_id,)))["n"]
    if cnt >= MAX_PER_CHAT:
        return False, f"부스트 구간이 너무 많아요({cnt}개). 관리자에게 정리를 부탁하세요."
    now = _now()

    def run(c):
        c.execute("UPDATE rank_boost SET to_ts=? WHERE chat_id=? AND to_ts IS NULL", (now, chat_id))
        c.execute("INSERT INTO rank_boost(chat_id, from_ts, to_ts, min_chars, created_by, created_at) "
                  "VALUES(?,?,?,?,?,?)", (chat_id, now, None, mc, by, now))
    await db.atomic(run)
    _clear(chat_id)
    word = "모든 글" if mc == 0 else f"{mc}글자 이상"
    return True, f"🚀 집계 부스트 시작 — 지금부터 끌 때까지 친 글은 **{word}** 기준으로 순위에 영구 반영돼요."


async def stop(db, chat_id: int) -> tuple[bool, str]:
    """진행 중 부스트 끄기 (구간 닫음 — 그 동안 친 글은 순위에 그대로 남음)."""
    cur = await current(db, chat_id)
    if not cur:
        return False, "지금 켜진 부스트가 없어요."
    await db._write("UPDATE rank_boost SET to_ts=? WHERE id=?", (_now(), cur["id"]))
    _clear(chat_id)
    mc = cur["min_chars"]
    word = "모든 글" if mc == 0 else f"{mc}글자 이상"
    return True, f"부스트 껐어요. 켜 둔 동안 친 글({word})은 순위에 **그대로 남아요**. 이후 글은 방 기본 기준으로 집계돼요."


async def status(db, chat_id: int) -> str:
    cur = await current(db, chat_id)
    rows = await db._all("SELECT min_chars, from_ts, to_ts FROM rank_boost WHERE chat_id=? ORDER BY from_ts DESC LIMIT 5",
                         (chat_id,))
    if cur:
        mc = cur["min_chars"]
        word = "모든 글" if mc == 0 else f"{mc}글자 이상"
        head = f"🚀 지금 부스트 **켜짐** ({word} 집계 중). 끄려면 <code>.채팅부스트 끄기</code>"
    else:
        head = "지금 부스트 꺼짐. 켜려면 <code>.채팅부스트 2</code> (2글자 이상 집계)"
    if rows:
        past = "\n".join(f"• {('모든 글' if r['min_chars']==0 else str(r['min_chars'])+'글자↑')}"
                         f" {'진행 중' if r['to_ts'] is None else '지난 구간'}" for r in rows)
        head += "\n\n최근 구간:\n" + past
    return head
