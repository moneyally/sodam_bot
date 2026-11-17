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
import time
from pathlib import Path

from . import clone, db as dbmod, mediastore
from .db import DB

# chat_id 가 없는 사람 단위 전역표 — 그 방에 등장한 user 만 복사해야 이름·멘션·사칭추적이 안 깨짐.
#   users: user_id PK → 이미 있으면 유지(INSERT OR IGNORE) · name_history: id AUTOINCREMENT → id 빼고 복사
GLOBAL_BY_USER = {"users": "user_id", "name_history": "user_id"}
# 방 번호 없이 '모든 방 공용'으로 쓰는 배운 목록 — 통째로 (클론에 이미 있는 건 그대로, INSERT OR IGNORE).
#   음란물 차단(파일·지문·스티커 팩) · 공동 차단 명단. 2026-11-17 전수조사: 이관된 방이 차단 목록 없이 돌고 있었음.
GLOBAL_ALL = ("nsfw_bad", "nsfw_phash", "nsfw_set", "nsfw_set_hits", "fedban_entries")
# 오너 전체 기본값(chat_state 0) 중 방 동작을 바꾸는 것 — 게임 난이도(방 설정 casino_difficulty=owner 면 이걸 따름).
#   빠지면 클론에선 '공정'으로 돌아 본체 때와 달라짐. 클론에 이미 값이 있으면 안 덮음.
GLOBAL_STATE = ("casino_lose_pct",)


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
        for t in GLOBAL_ALL:
            dcols = _cols(con, "dst", t)
            scols = set(_cols(con, "main", t))
            common = [c for c in dcols if c in scols]
            if not common:
                continue
            cl = ",".join(f'"{c}"' for c in common)
            counts[t] = con.execute(f'INSERT OR IGNORE INTO dst."{t}" ({cl}) SELECT {cl} FROM main."{t}"').rowcount
        ph = ",".join("?" * len(GLOBAL_STATE))
        counts["_global_state"] = con.execute(
            "INSERT OR IGNORE INTO dst.chat_state(chat_id, key, value) "
            f"SELECT chat_id, key, value FROM main.chat_state WHERE chat_id=0 AND key IN ({ph})", GLOBAL_STATE).rowcount
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


async def copy_media(src: DB, dst: DB) -> int:
    """클론 DB 가 쓰는 미디어(예약·인사·자동답글·퇴장·수배)의 본체 보관 원본을 클론 보관함으로.
    file_id 는 봇마다 달라서 원본 없으면 클론이 '사진 없이 글만' 보냄 (2026-11-17 뉴월드 제휴광고). 복사한 수."""
    n = 0
    dst_dir = Path(dst.path).resolve().parent / "media"
    for kind, fid in dict.fromkeys(await mediastore.references(dst)):
        if await dst._one("SELECT 1 FROM media_store WHERE file_id=?", (fid,)):
            continue
        row = await src._one("SELECT kind, path FROM media_store WHERE file_id=?", (fid,))
        if not row:
            continue
        try:
            data = (Path(src.path).resolve().parent / "media" / row["path"]).read_bytes()
        except OSError:
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        p = dst_dir / row["path"]
        if not p.exists():
            p.write_bytes(data)
        await dst._write("INSERT OR REPLACE INTO media_store(file_id, kind, path, size, ts) VALUES(?,?,?,?,?)",
                         (fid, row["kind"] or kind, row["path"], len(data), int(time.time())))
        n += 1
    return n


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
        counts["_media"] = await copy_media(svc.db, chk)
    finally:
        await chk.close()
    if before != after:
        return False, f"⚠️ 포인트 합이 안 맞아 (본체 {before:,} ≠ 클론 {after:,}). 이관 멈췄어 — 손대지 말고 알려줘.", counts
    n_tables = len([k for k in counts if not k.startswith("_")])
    return True, (f"✅ 복사 끝 — 멤버 {counts.get('_members', 0)}명 · 포인트 {after:,} · 표 {n_tables}개.\n"
                  "이제 방에서 본체 소담 내보내고 클론 봇을 관리자로 넣으면 돼."), counts


# ── 넘기기 마무리 (본체·클론 둘이 같은 방에서 겹쳐 답하지 않게) ─────────────────
ADMIN = ("administrator", "creator")


def clone_token(db_path: str, slug: str) -> str:
    return clone._read_env(clone.env_path(db_path, slug)).get("TELEGRAM_BOT_TOKEN", "")


async def _status(bot, chat, user_id: int) -> str:
    """그 봇의 그 방/채널 상태 — administrator·creator·member·left·kicked, 못 물으면 'none'."""
    try:
        m = await bot.get_chat_member(chat, user_id)
        return str(getattr(m, "status", "") or "none")
    except Exception:   # 방에 없음·채널 비공개·네트워크 — 다 '안 됨'
        return "none"


async def room_channels(db: DB, chat_id: int) -> list:
    """이 방에 걸린 채널: 📢 구독 필수(subgate_channel) + 채널 관리에 연결한 채널(channels.room_id)."""
    out: list = []
    s = await db.get_settings(chat_id)
    ch = (s.get("subgate_channel") or "").strip() if s.get("subgate_mode") == "on" else ""
    if ch:
        out.append(ch)
    try:
        for r in await db._all("SELECT chat_id FROM channels WHERE room_id=? AND active=1", (chat_id,)):
            if r["chat_id"] not in out:
                out.append(r["chat_id"])
    except Exception:   # 채널 표 없는 옛 DB
        pass
    return out


async def handover_check(main_bot, clone_bot, db: DB, chat_id: int) -> dict:
    """넘기기 점검: 클론이 방 관리자인지 · 채널마다 클론이 관리자인지 · 본체가 아직 방에 있는지.
    ready = 클론이 방 관리자이고 채널도 전부 관리자 → 이제 본체를 빼도 끊김 없음."""
    room = await _status(clone_bot, chat_id, clone_bot.id)
    main = await _status(main_bot, chat_id, main_bot.id)
    chans = [(ch, await _status(clone_bot, ch, clone_bot.id)) for ch in await room_channels(db, chat_id)]
    ready = room in ADMIN and all(st in ADMIN for _, st in chans)
    return {"clone_room": room, "main_room": main, "channels": chans, "ready": ready,
            "main_in": main in ("administrator", "member", "restricted")}


async def main_leave(main_bot, clone_bot, db: DB, chat_id: int) -> tuple[bool, str]:
    """점검을 다시 해서 준비됐을 때만 본체가 방을 나감 (클론 없이 비는 시간 0)."""
    chk = await handover_check(main_bot, clone_bot, db, chat_id)
    if not chk["ready"]:
        return False, "아직 클론 봇이 방(또는 채널) 관리자가 아니야 — 먼저 넣고 다시 눌러줘."
    if not chk["main_in"]:
        return True, "본체 소담은 이미 방에 없어."
    try:
        await main_bot.leave_chat(chat_id)
    except Exception as e:
        return False, f"본체가 못 나갔어 ({e}). 방에서 직접 내보내줘."
    return True, "🚪 본체 소담이 방에서 나갔어. 이제 클론 봇만 답해."
