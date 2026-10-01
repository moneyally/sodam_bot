"""봇 ↔ 통화 담당 프로세스(worker) 가 DB 로 주고받는 것.

voice_jobs  봇이 넣고 worker 가 1초마다 가져감: join(초대링크로 방 들어가기)·start(통화 시작)·stop·login_phone/login_code/login_pw/logout.
            결과(status done/failed + result 글)를 봇이 30초 틱에서 읽어 안내. 로그인 코드·비밀번호는 처리 즉시 payload 를 지움.
voice_calls 통화 한 번 = 한 줄 (시간·끝난 이유·말 횟수). voice_lines 대화(받아쓰기·소담 답·도구) = 점검용 7일, 오너만 봄.
chat_state(0, voice_assistant) = 로그인된 어시스턴트 {id, name, username} (worker 가 씀).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from .. import db as dbm

log = logging.getLogger(__name__)

JOB_TTL = 120            # 이만큼 안 가져가면 worker 가 안 도는 것 → failed(no_worker)
ASSISTANT_KEY = "voice_assistant"
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
    bot_turns INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0, stats TEXT);
CREATE INDEX IF NOT EXISTS voice_calls_chat ON voice_calls(chat_id, start_ts);
CREATE TABLE IF NOT EXISTS voice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, ts INTEGER NOT NULL,
    who TEXT NOT NULL, user_id INTEGER, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS voice_lines_call ON voice_lines(call_id, id);
CREATE INDEX IF NOT EXISTS voice_lines_ts ON voice_lines(ts);
CREATE INDEX IF NOT EXISTS voice_lines_chat ON voice_lines(chat_id, ts);
""", migrate={"voice_jobs": "plain", "voice_calls": "plain", "voice_lines": "plain"})
dbm.register_columns("voice_calls", {"stats": "TEXT"})   # 통화 계측 JSON (bridge.Result.stats, diag voice 로 봄)
LINES_KEEP_DAYS = 7          # 통화 대화(받아쓰기·소담 답·도구) 점검용 보관 (오너 결정 2026-09-29), 오너만 봄
LINE_MAX = 500


async def add_job(db, chat_id: int, kind: str, payload: dict | None = None, by: int | None = None) -> int | None:
    """같은 방·같은 종류 일이 이미 대기 중이면 None (연타)."""
    now = int(time.time())

    def run(c):
        if c.execute("SELECT 1 FROM voice_jobs WHERE chat_id=? AND kind=? AND status IN ('pending','running') AND ts>?",
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


async def cancel_if_pending(db, job_id: int) -> bool:
    """봇이 기다리다 포기할 때: 아직 안 가져간 일이면 취소 (늦게 시작돼서 '안 된다' 안내 뒤에 들어가는 일 방지)."""
    cur = await db.conn.execute("UPDATE voice_jobs SET status='failed', result='no_worker', done_ts=?, payload='{}' "
                                "WHERE id=? AND status='pending'", (int(time.time()), job_id))
    await db.conn.commit()
    return bool(cur.rowcount)


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


async def call_ended(db, call_id: int, seconds: float, reason: str, user_turns: int = 0, bot_turns: int = 0,
                     stats: dict | None = None) -> None:
    """reason: idle·time(15분)·bye(멤버가 '나가')·admin·chat_closed(음성채팅 닫힘)·kicked·ws_closed(OpenAI 연결 끊김)·
    error:realtime·error:play·error:<예외>·restart·logout."""
    await db._write("UPDATE voice_calls SET end_ts=?, seconds=?, reason=?, user_turns=?, bot_turns=?, stats=? "
                    "WHERE id=? AND end_ts IS NULL",
                    (int(time.time()), int(seconds), reason[:40], user_turns, bot_turns,
                     json.dumps(stats, ensure_ascii=False)[:4000] if stats else None, call_id))


async def add_line(db, call_id: int, chat_id: int, who: str, user_id: int | None, text: str) -> None:
    """who = user(멤버 말)·sodam(소담 답)·tool(도구 이름·결과 앞부분)."""
    text = (text or "").strip()
    if text:
        await db._write("INSERT INTO voice_lines(call_id, chat_id, ts, who, user_id, text) VALUES(?,?,?,?,?,?)",
                        (call_id, chat_id, int(time.time()), who[:8], user_id, text[:LINE_MAX]))


async def purge_lines(db, days: int = LINES_KEEP_DAYS) -> int:
    cur = await db.conn.execute("DELETE FROM voice_lines WHERE ts<?", (int(time.time()) - days * 86400,))
    await db.conn.commit()
    return cur.rowcount or 0


async def call_lines(db, call_id: int, limit: int = 300) -> list:
    return await db._all("SELECT l.ts, l.who, l.user_id, l.text, u.first_name FROM voice_lines l "
                         "LEFT JOIN users u ON u.user_id=l.user_id WHERE l.call_id=? ORDER BY l.id LIMIT ?", (call_id, limit))


LINK_GAP = 30                # 같은 통화에서 30초 안에 다른 사람이 이어 말하면 '음성으로 주고받음' 1번


async def voice_links(db, chat_id: int, since: int, user_id: int | None = None) -> list:
    """(from_id, to_id, n): from 이 to 의 말 바로 뒤에 이어 말한 횟수 (채팅 답장 관계의 음성판, 저장된 7일 안에서만).
    누군지 모르는 말·소담 말·도구 줄은 빼고 사람 말끼리만."""
    who = "" if user_id is None else "AND (from_id=? OR to_id=?) "
    return await db._all(
        "SELECT from_id, to_id, COUNT(*) n FROM ("
        " SELECT user_id from_id, ts, LAG(user_id) OVER w to_id, LAG(ts) OVER w prev_ts FROM voice_lines"
        " WHERE chat_id=? AND ts>=? AND who='user' AND user_id IS NOT NULL WINDOW w AS (PARTITION BY call_id ORDER BY id))"
        " WHERE to_id IS NOT NULL AND to_id<>from_id AND ts-prev_ts<=? " + who +
        "GROUP BY from_id, to_id ORDER BY n DESC LIMIT 200",
        (chat_id, since, LINK_GAP, *(() if user_id is None else (user_id, user_id))))


async def member_voice(db, chat_id: int, user_id: int, since: int) -> tuple[int, int]:
    """(참여한 통화 수, 한 말 수) — 그 사람이 말한 게 확인된 것만."""
    r = await db._one("SELECT COUNT(DISTINCT call_id) c, COUNT(*) n FROM voice_lines "
                      "WHERE chat_id=? AND user_id=? AND who='user' AND ts>=?", (chat_id, user_id, since))
    return (r["c"] or 0, r["n"] or 0) if r else (0, 0)


USD_PER_MIN = float(__import__("os").getenv("VOICE_USD_PER_MIN", "0.08"))   # 사용량을 모를 때만 쓰는 추정 (넉넉히)
# 실제 요금표 (USD / 1M 토큰, OpenAI pricing 2026-10-01): (글자 입력, 글자 캐시, 글자 출력, 음성 입력, 음성 캐시, 음성 출력)
REALTIME_PRICES = {"gpt-realtime-2.1-mini": (0.60, 0.06, 2.40, 10.00, 0.30, 20.00),
                   "gpt-realtime-2.1": (4.00, 0.40, 24.00, 32.00, 0.40, 64.00)}
TRANSCRIBE_USD_PER_MIN = 0.003      # gpt-4o-mini-transcribe 받아쓰기 (토큰 usage 에 안 들어감 → 통화 시간으로)
LIVE_USD_PER_MIN = {"gpt-live-1": 0.05}   # GPT-Live: 세션 시간(조용해도) 초 단위 과금 (voice-latency-cost 가이드)
# Live 백엔드(Responses) 요금 (입력, 캐시, 출력 USD/1M, 짧은 문맥) — 모르는 모델은 가장 비싼 줄로
BACKEND_PRICES = {"gpt-6-luna": (0.10, 0.01, 0.50), "gpt-5.6-luna": (0.20, 0.02, 1.20)}


def cost_micro(model: str, usage: dict | None, seconds: float) -> tuple[int, str]:
    """(마이크로달러, 근거). 모델 요금표와 음성·글자별 토큰이 있으면 실제 요금 + 받아쓰기, 아니면 분당 추정.
    예전엔 항상 분당 0.08 추정만 써서 usage 를 버렸음 (2026-10-01 점검)."""
    u = usage or {}
    if (model or "") in LIVE_USD_PER_MIN:
        secs = float(u.get("live_seconds") or 0) or seconds        # 서버가 센 초가 있으면 그것 (없으면 통화 길이)
        usd = secs / 60 * LIVE_USD_PER_MIN[model]
        bm = str(u.get("backend_model") or "")
        bp = next((v for k, v in BACKEND_PRICES.items() if bm == k or bm.startswith(k + "-")), max(BACKEND_PRICES.values()))
        cached = u.get("backend_cached", 0)
        usd += (max(0, u.get("backend_in", 0) - cached) * bp[0] + cached * bp[1] + u.get("backend_out", 0) * bp[2]) / 1_000_000
        return int(usd * 1_000_000), "live"
    p = REALTIME_PRICES.get(model or "")
    if p and any(u.get(k) for k in ("in_audio", "in_text", "out_audio", "out_text")):
        ca, ct = u.get("cached_audio", 0), u.get("cached_text", 0)
        usd = (max(0, u.get("in_text", 0) - ct) * p[0] + ct * p[1] + u.get("out_text", 0) * p[2]
               + max(0, u.get("in_audio", 0) - ca) * p[3] + ca * p[4] + u.get("out_audio", 0) * p[5]) / 1_000_000
        usd += seconds / 60 * TRANSCRIBE_USD_PER_MIN
        return int(usd * 1_000_000), "tokens"
    return int(seconds / 60 * USD_PER_MIN * 1_000_000), "per_min"


async def record_cost(db, tz, chat_id: int, seconds: float, model: str = "", usage: dict | None = None) -> int:
    """통화 요금을 하루 예산(usd_micro)·방 요금(room_usd_micro)에 더함 → 예산이 다 차면 다른 AI 처럼 막힘."""
    from .. import costs
    micro, how = cost_micro(model, usage, seconds)
    log.info("통화 요금 %s %.0f초 → $%.4f (%s)", chat_id, seconds, micro / 1e6, how)
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
