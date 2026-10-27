"""🏆 순위 고정 지정 (오너만, 2026-10-27 오너 요청 '일루왕 1~3등을 지정한 사람으로 한 달').

초활발방(하루 1등이 수천 개)에선 가짜 채팅 행을 넣어 실제 집계를 이기는 건 비현실적
(한 달이면 수십만 행 → DB·AI 기억·검색 오염·대놓고 조작 티). 그래서 **집계는 그대로 두고
보여줄 때만** 지정한 사람을 상단에 고정한다 — 보이는 횟수는 그날 실제 1등보다 자동으로
살짝 높게(날짜별 고정 시드), 실제 사람들은 그 아래로 밀린다.

savage 와 같은 꼴: 방 설정(settings)이 아니라 chat_state 에 숨긴다 → `.설정`·`.설정변경`·
AI change_setting·📊 꾸미기 어디에도 안 뜨고 **대화 속 어떤 지시문(인젝션)으로도 못 켜고 못 끈다**.
오너가 `.순위고정 @a @b @c [일수]` 명령으로만 (commands.c_rankfix). 기간 지나면 자동 해제.

적용: chatrank.standings 가 rows 를 돌려주기 직전에 _apply(), my_place 도 같이. 자정 시상식은
panels/chatrank.award 가 standings 를 타므로 기록(rank_awards)에도 그대로 들어간다.
"""
from __future__ import annotations

import json
import time
import zlib

from .db import DB

KEY = "rank_fixed"
MAX_IDS = 10            # 1~N등까지 (순위 top 과 같은 한도)
MAX_DAYS = 120          # 한 번에 거는 최대 기간


async def get(db: DB, chat_id: int) -> list[int] | None:
    """지금 걸려 있는 고정 명단 [1등, 2등, …] 또는 None (기간 지났거나 없음). 방마다 캐시."""
    async def load() -> list[int] | None:
        raw = await db.get_state(chat_id, KEY)
        if not raw:
            return None
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return None
        ids = [int(x) for x in d.get("ids", [])][:MAX_IDS]
        until = int(d.get("until", 0))
        if not ids or (until and time.time() > until):
            return None
        return ids
    return await db.cached(("rankfix", chat_id), load)


async def status(db: DB, chat_id: int) -> tuple[list[int] | None, int]:
    """(명단, 끝 시각) — 상태 보기용. 캐시 안 거침 (끝 시각까지 필요)."""
    raw = await db.get_state(chat_id, KEY)
    if not raw:
        return None, 0
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None, 0
    ids = [int(x) for x in d.get("ids", [])][:MAX_IDS]
    until = int(d.get("until", 0))
    if not ids or (until and time.time() > until):
        return None, until
    return ids, until


async def set_fixed(db: DB, chat_id: int, ids: list[int], days: int) -> int:
    """명단 저장. days 일 뒤 자동 해제. 끝 시각(ts) 돌려줌."""
    days = max(1, min(MAX_DAYS, int(days)))
    until = int(time.time()) + days * 86400
    ids = [int(x) for x in ids][:MAX_IDS]
    await db.set_state(chat_id, KEY, json.dumps({"ids": ids, "until": until}, ensure_ascii=False))
    db.uncache(("rankfix", chat_id))
    return until


async def clear(db: DB, chat_id: int) -> None:
    await db.set_state(chat_id, KEY, None)
    db.uncache(("rankfix", chat_id))


def display_counts(chat_id: int, date_key: str, n: int, real_max: int) -> list[int]:
    """고정 상단 n명의 '보이는 횟수' — 전부 실제 1등(real_max)보다 크고 내림차순.
    (chat_id, 날짜) 시드로 그날 안에선 고정 (30초 캐시 새로고침마다 안 튐), real_max 가 커지면 같이 커짐.
    1등부터 내림차순이 되게 아래 등수부터 쌓아 올린다."""
    seed = zlib.adler32(f"{chat_id}:{date_key}".encode())
    gap = max(4, real_max // 45)          # 방이 활발할수록 등수 간격도 조금 넓게 (티 안 나게)
    counts = [0] * n
    v = real_max
    for i in range(n - 1, -1, -1):        # 꼴찌(아래)부터 위로 쌓음 → counts[0](1등)이 가장 큼
        jit = (seed >> (i * 4)) % (gap + 3)
        v += gap + 2 + jit
        counts[i] = v
    return counts
