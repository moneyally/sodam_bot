"""봇 ↔ 통화 담당 프로세스(worker) 가 DB 로 주고받는 것.

voice_jobs  봇이 넣고 worker 가 1초마다 가져감: join(초대링크로 방 들어가기)·start(통화 시작)·stop·login_phone/login_code/login_pw/logout.
            결과(status done/failed + result 글)를 봇이 30초 틱에서 읽어 안내. 로그인 코드·비밀번호는 처리 즉시 payload 를 지움.
voice_calls 통화 한 번 = 한 줄 (시간·끝난 이유·말 횟수). 대화 내용은 저장 안 함.
chat_state(0, voice_assistant) = 로그인된 어시스턴트 {id, name, username} (worker 가 씀).
"""
from __future__ import annotations

import json
import time
from datetime import datetime

from .. import db as dbm

JOB_TTL = 120            # 이만큼 안 가져가면 worker 가 안 도는 것 → failed(no_worker)
ASSISTANT_KEY = "voice_assistant"
RTMP_KEY = "voice_rtmp"                # chat_state(방, …) = {url, key} 📡 방송 모드 (관리자가 등록, 화면엔 안 보임)
MODE_KEY = "voice_mode"                # chat_state(방, …) = "radio" 방송 중 (통화 끝나면 틱이 지움)
WORKER_BEAT = "voice_worker_beat"      # chat_state(0, …) = worker 가 마지막으로 살아 있던 시각
WORKER_ALIVE = 30

dbm.register_schema("""
CREATE TABLE IF NOT EXISTS voice_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    by_user INTEGER, status TEXT NOT NULL DEFAULT 'pending', result TEXT, ts INTEGER NOT NULL, done_ts INTEGER,
    notified INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS voice_jobs_status ON voice_jobs(status, id);
CREATE TABLE IF NOT EXISTS voice_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, started_by INTEGER, start_ts INTEGER NOT NULL,
    end_ts INTEGER, seconds INTEGER NOT NULL DEFAULT 0, reason TEXT, user_turns INTEGER NOT NULL DEFAULT 0,
    bot_turns INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS voice_calls_chat ON voice_calls(chat_id, start_ts);
""", migrate={"voice_jobs": "plain", "voice_calls": "plain"})


async def add_job(db, chat_id: int, kind: str, payload: dict | None = None, by: int | None = None,
                  dedup: bool = True) -> int | None:
    """같은 방·같은 종류 일이 이미 대기 중이면 None (연타). dedup=False = 줄 세움 (방송 말하기)."""
    now = int(time.time())

    def run(c):
        if dedup and c.execute("SELECT 1 FROM voice_jobs WHERE chat_id=? AND kind=? AND status IN ('pending','running') AND ts>?",
                     (chat_id, kind, now - JOB_TTL)).fetchone():
            return None
        return c.execute("INSERT INTO voice_jobs(chat_id, kind, payload, by_user, ts) VALUES(?,?,?,?,?)",
                         (chat_id, kind, json.dumps(payload or {}, ensure_ascii=False), by, now)).lastrowid
    return await db.atomic(run)


async def take_jobs(db, limit: int = 10) -> list[dict]:
    """worker: 대기 중 일을 running 으로 바꾸며 가져감 (한 번만)."""
    def run(c):
        rows = c.execute("SELECT id, chat_id, kind, payload, by_user FROM voice_jobs WHERE status='pending' ORDER BY id LIMIT ?",
                         (limit,)).fetchall()
        out = []
        for r in rows:
            if c.execute("UPDATE voice_jobs SET status='running' WHERE id=? AND status='pending'", (r[0],)).rowcount:
                out.append({"id": r[0], "chat_id": r[1], "kind": r[2], "payload": json.loads(r[3] or "{}"), "by": r[4]})
        return out
    return await db.atomic(run)


async def finish(db, job_id: int, ok: bool, result: str = "") -> None:
    await db._write("UPDATE voice_jobs SET status=?, result=?, done_ts=?, payload='{}' WHERE id=?",
                    ("done" if ok else "failed", result[:300], int(time.time()), job_id))


async def job(db, job_id: int):
    return await db._one("SELECT * FROM voice_jobs WHERE id=?", (job_id,))


async def expire_stale(db, now: int | None = None) -> int:
    """worker 가 안 가져간 오래된 일 → failed(no_worker). 봇 틱이 부름."""
    now = int(now or time.time())
    return (await db.conn.execute(
        "UPDATE voice_jobs SET status='failed', result='no_worker', done_ts=?, payload='{}' WHERE status='pending' AND ts<?",
        (now, now - JOB_TTL))).rowcount or 0


async def finished_unnotified(db) -> list:
    return await db._all("SELECT * FROM voice_jobs WHERE status IN ('done','failed') AND notified=0 ORDER BY id LIMIT 20")


async def mark_notified(db, table: str, row_id: int) -> bool:
    assert table in ("voice_jobs", "voice_calls")
    cur = await db.conn.execute(f"UPDATE {table} SET notified=1 WHERE id=? AND notified=0", (row_id,))
    await db.conn.commit()
    return bool(cur.rowcount)


# ── 통화 기록 ─────────────────────────────────────────
async def call_started(db, chat_id: int, by: int | None) -> int:
    return await db._write("INSERT INTO voice_calls(chat_id, started_by, start_ts) VALUES(?,?,?)",
                           (chat_id, by, int(time.time())))


async def call_ended(db, call_id: int, seconds: float, reason: str, user_turns: int = 0, bot_turns: int = 0) -> None:
    await db._write("UPDATE voice_calls SET end_ts=?, seconds=?, reason=?, user_turns=?, bot_turns=? WHERE id=? AND end_ts IS NULL",
                    (int(time.time()), int(seconds), reason[:40], user_turns, bot_turns, call_id))


USD_PER_MIN = float(__import__("os").getenv("VOICE_USD_PER_MIN", "0.08"))   # 추정 (Realtime mini 음성 입·출력 + 받아쓰기, 넉넉히)


async def record_cost(db, tz, chat_id: int, seconds: float) -> int:
    """통화 요금 추정을 하루 예산(usd_micro)·방 요금(room_usd_micro)에 더함 → 예산이 다 차면 다른 AI 처럼 막힘."""
    from .. import costs
    micro = int(seconds / 60 * USD_PER_MIN * 1_000_000)
    if micro <= 0:
        return 0
    day = datetime.now(tz).strftime("%Y-%m-%d")
    rows = [(day, 0, costs.USD, micro), (day, chat_id, costs.ROOM_USD, micro), (day, 0, "voice_seconds", int(seconds))]
    await db.atomic(lambda c: c.executemany(
        "INSERT INTO counters(day, chat_id, key, n) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(day, chat_id, key) DO UPDATE SET n=n+excluded.n", rows))
    return micro


async def active_call(db, chat_id: int):
    return await db._one("SELECT * FROM voice_calls WHERE chat_id=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1", (chat_id,))


async def close_orphans(db) -> int:
    """worker 가 다시 시작됨 → 끝 표시 없이 남은 통화는 끝난 걸로 (이유 restart)."""
    cur = await db.conn.execute(
        "UPDATE voice_calls SET end_ts=?, reason='restart', seconds=MAX(0, ?-start_ts) WHERE end_ts IS NULL",
        (int(time.time()), int(time.time())))
    await db.conn.commit()
    return cur.rowcount or 0


async def ended_unnotified(db) -> list:
    return await db._all("SELECT * FROM voice_calls WHERE end_ts IS NOT NULL AND notified=0 ORDER BY id LIMIT 20")


async def month_seconds(db, chat_id: int, tz) -> int:
    start = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    row = await db._one("SELECT COALESCE(SUM(seconds),0) s FROM voice_calls WHERE chat_id=? AND start_ts>=?",
                        (chat_id, int(start)))
    return int(row["s"]) if row else 0


async def assistant(db) -> dict | None:
    return await db.get_state(0, ASSISTANT_KEY)


async def worker_alive(db, now: float | None = None) -> bool:
    beat = await db.get_state(0, WORKER_BEAT)
    return bool(beat) and (now or time.time()) - float(beat) < WORKER_ALIVE
