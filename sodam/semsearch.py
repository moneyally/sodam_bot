"""🔎 의미 검색: 단어 검색(FTS5) + 뜻 검색(sqlite-vec) 을 RRF 로 합친다.

참고: Alex Garcia 'Hybrid full-text search and vector search with SQLite' (FTS5 + vec0, Reciprocal Rank Fusion k=60).
- 색인: 사람 글(봇·flagged 제외, MIN_LEN자↑, 최근 INDEX_DAYS일)을 30초마다 BATCH 개씩 임베딩 → msg_vec(rowid = messages.id,
  chat_id 파티션). text-embedding-3-small 256차원 ≈ 메시지 2천 개에 $0.001. 전체 하루 예산 안에서만, 방 한도엔 안 셈.
  두 프로세스(메인·딜러)가 같은 DB → claims 로 한 번에 한 곳만. 지운 메시지(보관 기간 정리)의 벡터는 한 시간에 한 번 정리.
- 검색: 단어 결과 + (색인 있으면) 질문 임베딩 1번 → 같은 방 가까운 글 → RRF 순위. 뜻으로만 찾은 글은 거리 MAX_DIST 안만.
  sqlite-vec 이 없거나 AI 가 꺼져 있으면 단어 검색 그대로.
"""
from __future__ import annotations

import logging
import os
import time

from . import hooks, persist

log = logging.getLogger(__name__)

MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
DIM = 256
BATCH = 64
MIN_LEN = 8
INDEX_DAYS = 30
RRF_K = 60
POOL = 20
MAX_DIST = float(os.getenv("SEM_MAX_DIST", "0.7"))   # cosine 거리 (0 = 같음, 1 = 무관)
CURSOR = "semsearch_cursor"


def enabled(svc) -> bool:
    return bool(getattr(svc.db, "vec", False)) and getattr(svc.llm, "enabled", False) and hasattr(svc.llm, "embed") \
        and os.getenv("SEMSEARCH", "1") != "0"


async def _table(db) -> None:
    if not getattr(db, "_vec_table", False):
        await db._write(f"CREATE VIRTUAL TABLE IF NOT EXISTS msg_vec USING vec0("
                        f"chat_id integer partition key, embedding float[{DIM}] distance_metric=cosine)")
        db._vec_table = True


def _blob(v: list[float]) -> bytes:
    import sqlite_vec
    return sqlite_vec.serialize_float32(v)


async def index_new(svc, now: float | None = None) -> int:
    """새 글 BATCH 개 색인. 넣은 수."""
    if not enabled(svc) or not await persist.claim(svc.db, "semsearch_index", 25):
        return 0
    db, now = svc.db, time.time() if now is None else now
    await _table(db)
    cur = await db.get_state(0, CURSOR, 0)
    rows = await db._all("SELECT id, chat_id, text FROM messages WHERE id>? AND is_bot=0 AND flagged=0 AND ts>=? "
                         "AND length(text)>=? ORDER BY id LIMIT ?", (cur, int(now) - INDEX_DAYS * 86400, MIN_LEN, BATCH))
    if not rows:
        return 0
    vecs = await svc.llm.embed([r["text"][:1000] for r in rows], dims=DIM, model=MODEL)
    data = [(r["id"], r["chat_id"], _blob(v)) for r, v in zip(rows, vecs)]
    await db.atomic(lambda c: c.executemany("INSERT OR REPLACE INTO msg_vec(rowid, chat_id, embedding) VALUES(?,?,?)", data))
    await db.set_state(0, CURSOR, rows[-1]["id"])
    if await persist.claim(db, "semsearch_prune", 3600):   # 보관 기간이 지나 지워진 글의 벡터
        await db._write("DELETE FROM msg_vec WHERE rowid NOT IN (SELECT id FROM messages)")
    return len(rows)


async def search(svc, chat_id: int, query: str, since: int, limit: int = 10) -> list:
    """단어 + 뜻 합친 결과 (messages 행 + username/first_name, 뜻으로만 찾은 글은 'semantic' 표시)."""
    words = list(await svc.db.search_messages(chat_id, query, since, POOL))
    if not enabled(svc):
        return words[:limit]
    try:
        await _table(svc.db)
        [qv] = await svc.llm.embed([query], dims=DIM, model=MODEL, purpose="embed_query")
        near = await svc.db._all("SELECT rowid, distance FROM msg_vec WHERE embedding MATCH ? AND k=? AND chat_id=?",
                                 (_blob(qv), POOL, chat_id))
    except Exception:   # 예산·연결 오류 → 단어 결과만
        log.exception("semantic search failed")
        return words[:limit]
    score: dict[int, float] = {}
    for rank, r in enumerate(words):
        score[r["id"]] = score.get(r["id"], 0) + 1 / (RRF_K + rank + 1)
    near = [r for r in near if r["distance"] <= MAX_DIST]
    for rank, r in enumerate(near):
        score[r["rowid"]] = score.get(r["rowid"], 0) + 1 / (RRF_K + rank + 1)
    by_id = {r["id"]: dict(r) for r in words}
    missing = [i for i in score if i not in by_id]
    if missing:
        marks = ",".join("?" * len(missing))
        for r in await svc.db._all(
                f"SELECT msg.*, u.username, u.first_name FROM messages msg LEFT JOIN users u ON u.user_id=msg.user_id "
                f"WHERE msg.id IN ({marks}) AND msg.chat_id=? AND msg.flagged=0 AND msg.is_bot=0 AND msg.ts>=?",
                (*missing, chat_id, since)):
            by_id[r["id"]] = {**dict(r), "semantic": True}
    ids = sorted((i for i in score if i in by_id), key=lambda i: -score[i])
    return [by_id[i] for i in ids[:limit]]


async def on_tick(svc, bot) -> None:
    try:
        await index_new(svc)
    except Exception:
        log.exception("semsearch index failed")


hooks.add_tick_hook(on_tick)
