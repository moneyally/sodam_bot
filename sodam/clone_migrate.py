"""🏷→🏷 기존 방을 클론봇으로 이관 — 본체 DB 의 한 방(chat_id) 데이터를 클론 DB 로 복사.
포인트·구독·설정·기억·기록이 안 날아가게. 설계: docs/WHITELABEL_ROOM_MIGRATION_DESIGN.md

표 목록은 db.chat_scoped_tables() 단일 출처(migrate_chat 과 공유) + 전역 users·name_history
(그 방에 등장한 user 만). 컬럼은 양쪽 공통만 명시(SELECT * 순서차·추가컬럼 흡수).

전제: **클론은 갓 만든(그 방이 첫 데이터인) 봇** — chat_scoped 표의 AUTOINCREMENT id 를 그대로
복사해 knowledge_docs↔chunks 같은 id 관계를 보존한다(빈 클론이라 id 충돌 없음). 여러 방을 한 클론에
몰아넣는(id 겹칠 수 있는) 이관은 2차. 돈(members.points) 걸린 작업이라 복사 전 열린 베팅 0 을 확인하고
복사 뒤 포인트 합을 대조한다.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3

from . import clone, db as dbmod
from .db import DB

# chat_id 가 없는 사람 단위 전역표 — 그 방에 등장한 user 만 복사해야 이름·멘션·사칭추적이 안 깨짐.
#   users: user_id PK → 이미 있으면 유지(INSERT OR IGNORE) · name_history: id AUTOINCREMENT → id 빼고 복사
GLOBAL_BY_USER = {"users": "user_id", "name_history": "user_id"}


def _cols(con: sqlite3.Connection, alias: str, table: str) -> list[str]:
    try:
        return [r[1] for r in con.execute(f'PRAGMA {alias}.table_info("{table}")')]
    except sqlite3.OperationalError:
        return []


def _copy_room(src_path: str, dst_path: str, chat_id: int) -> dict:
    """본체(src) → 클론(dst) 그 방 복사. 동기 sqlite3, 한 트랜잭션. dst 스키마 존재 전제.
    반환 {표: 복사행수, '_members': 등장 user 수, '_users': 새로 넣은 users 수}."""
    tables = [(t, k) for t, k in dbmod.chat_scoped_tables() if k != "drop"]
    counts: dict[str, int] = {}
    uids: set[int] = set()
    con = sqlite3.connect(src_path, timeout=60)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("ATTACH DATABASE ? AS dst", (dst_path,))
        con.execute("BEGIN IMMEDIATE")
        for t, _k in tables:
            dcols = _cols(con, "dst", t)
            if not dcols:                                   # 클론에 그 표가 아직 없음(구버전) → 건너뜀
                continue
            scols = set(_cols(con, "main", t))
            common = [c for c in dcols if c in scols]
            if "chat_id" not in common:
                continue
            cl = ",".join(f'"{c}"' for c in common)
            con.execute(f'DELETE FROM dst."{t}" WHERE chat_id=?', (chat_id,))          # 재이관 멱등
            cur = con.execute(
                f'INSERT INTO dst."{t}" ({cl}) SELECT {cl} FROM main."{t}" WHERE chat_id=?', (chat_id,))
            counts[t] = cur.rowcount
            if "user_id" in common:                         # 이 방에 등장한 user 수집 → 전역표 복사용
                uids |= {r[0] for r in con.execute(
                    f'SELECT DISTINCT user_id FROM dst."{t}" WHERE chat_id=?', (chat_id,)) if r[0]}
        n_users = 0
        if uids:
            ids = list(uids)
            for t, col in GLOBAL_BY_USER.items():
                dcols = _cols(con, "dst", t)
                if not dcols:
                    continue
                scols = set(_cols(con, "main", t))
                # id(AUTOINCREMENT)는 dst 가 새로 부여 — 다른 user 기록과 id 충돌·중복 방지 (users 엔 id 없음)
                common = [c for c in dcols if c in scols and c != "id"]
                cl = ",".join(f'"{c}"' for c in common)
                verb = "INSERT OR IGNORE" if t == "users" else "INSERT"
                for i in range(0, len(ids), 400):
                    chunk = ids[i:i + 400]
                    ph = ",".join("?" * len(chunk))
                    cur = con.execute(
                        f'{verb} INTO dst."{t}" ({cl}) SELECT {cl} FROM main."{t}" WHERE {col} IN ({ph})', chunk)
                    if t == "users":
                        n_users += cur.rowcount
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    counts["_members"] = len(uids)
    counts["_users"] = n_users
    return counts


async def open_bets(db: DB, chat_id: int) -> int:
    """그 방에 열린 베팅(에스크로) 수 — 0 이어야 돈 안 꼬이고 이관 가능."""
    row = await db._one("SELECT COUNT(*) AS n FROM casino_open WHERE chat_id=?", (chat_id,))
    return (row["n"] if row else 0) or 0


async def wallet_sum(db: DB, chat_id: int) -> int:
    """그 방 손님 포인트 합 (members.points) — 이관 전후 대조용."""
    row = await db._one("SELECT COALESCE(SUM(points),0) AS s FROM members WHERE chat_id=?", (chat_id,))
    return (row["s"] if row else 0) or 0


async def transfer_room(svc, chat_id: int, slug: str) -> tuple[bool, str, dict]:
    """오케스트레이션: 열린 베팅 확인 → 클론 DB 스키마 보장 → 복사 → 포인트 합 검증.
    반환 (성공?, 안내, 복사수). 복사 전 그 방을 '이관중' 잠금해 두는 건 호출부(UI)."""
    if await open_bets(svc.db, chat_id):
        return False, "🎰 게임 진행 중(열린 베팅)이라 못 옮겨. 게임 끝나고 다시 해줘.", {}
    dst = str(clone.clones_dir(svc.cfg.db_path) / slug / "sodam.db")
    os.makedirs(os.path.dirname(dst), exist_ok=True)   # 클론 봇이 아직 안 켜졌으면 폴더도 없음
    # 클론 DB 스키마 보장 (클론 봇이 아직 한 번도 안 켜졌을 수 있음 → 표가 없음)
    seed = DB(dst)
    await seed.open()
    await seed.close()
    before = await wallet_sum(svc.db, chat_id)
    counts = await asyncio.to_thread(_copy_room, svc.cfg.db_path, dst, chat_id)
    # 검증: 클론 쪽 포인트 합이 본체와 같은지 (다르면 복사가 틀어진 것 → 실패 보고)
    chk = DB(dst)
    await chk.open()
    try:
        after = await wallet_sum(chk, chat_id)
    finally:
        await chk.close()
    if before != after:
        return False, f"⚠️ 포인트 합이 안 맞아 (본체 {before:,} ≠ 클론 {after:,}). 이관 멈췄어 — 손대지 말고 알려줘.", counts
    n_tables = len([k for k in counts if not k.startswith("_")])
    return True, (f"✅ 복사 끝 — 멤버 {counts.get('_members', 0)}명 · 포인트 {after:,} · 표 {n_tables}개.\n"
                  "이제 방에서 본체 소담 내보내고 클론 봇을 관리자로 넣으면 돼."), counts
