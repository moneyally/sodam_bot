"""🎵 뮤직봇 대기열·세션 (DB) — 봇(panels/music.py)과 음성 담당(voice/music.py)이 같이 씀. 무거운 import 없음.

music_queue    한 줄 = 신청한 곡 하나. state: queued(기다림) → playing(지금) → done/skipped/removed/failed.
               '대기열 #n' = 이 방 queued 를 신청 순(id)으로 센 n번째. 재시작해도 남음 (음성 담당이 이어서 틂).
music_sessions 노래 틀기 한 번(도우미가 음성채팅에 들어가 있던 동안) = 한 줄. 끝난 이유·곡 수.
music_lyrics   곡(vid)마다 가사 전문 — 소담 AI 가 읽음 (방엔 일부만, 저작권). 공개 가사 DB 에서 같은 곡일 때만.
chat_state(방, music_modes) = {loopq 대기열 전체 반복, autoplay 자동 재생, vc_title 음성채팅 제목} — 봇·음성 담당 같이 읽음.
music_choices  딱 맞는 곡이 없을 때·버전(커버·라이브)을 고를 때 올린 버튼 한 장 = 한 줄 (신청한 사람만, CHOICE_SEC 안, 한 번만).
chat_state(0, music_health) = 음성 담당이 마지막으로 본 음원 상태 {ok_ts, err, err_ts} (오너 🎵 화면).
"""
from __future__ import annotations

import json
import time

from .. import db as dbm

dbm.register_schema("""
CREATE TABLE IF NOT EXISTS music_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
    vid TEXT, duration INTEGER NOT NULL DEFAULT 0, by_id INTEGER, by_name TEXT, state TEXT NOT NULL DEFAULT 'queued',
    pos_ms INTEGER NOT NULL DEFAULT 0, path TEXT, msg_id INTEGER, ts INTEGER NOT NULL, started_ts INTEGER, end_ts INTEGER,
    sort REAL, auto INTEGER NOT NULL DEFAULT 0, orig_vid TEXT);
CREATE INDEX IF NOT EXISTS music_queue_chat ON music_queue(chat_id, state, id);
CREATE TABLE IF NOT EXISTS music_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, by_id INTEGER, start_ts INTEGER NOT NULL,
    end_ts INTEGER, reason TEXT, tracks INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0, stats TEXT);
CREATE INDEX IF NOT EXISTS music_sessions_chat ON music_sessions(chat_id, start_ts);
CREATE TABLE IF NOT EXISTS music_choices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, by_id INTEGER, by_name TEXT, query TEXT,
    reason TEXT, items TEXT NOT NULL, ts INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, msg_id INTEGER);
CREATE TABLE IF NOT EXISTS music_lyrics (
    vid TEXT PRIMARY KEY, title TEXT, track TEXT, artist TEXT, plain TEXT NOT NULL, source TEXT, ts INTEGER NOT NULL);
""", migrate={"music_queue": "plain", "music_sessions": "plain", "music_choices": "plain"})
# sort = 섞기 순서 (없으면 id) · auto = 자동 재생으로 들어온 곡 · orig_vid = 대체 음원으로 바뀌기 전 기본 음원 ID (자동 재생 기준·통계)
dbm.register_columns("music_queue", {"sort": "REAL", "auto": "INTEGER NOT NULL DEFAULT 0", "orig_vid": "TEXT"})
dbm.register_columns("music_sessions", {"stats": "TEXT"})   # 끊김 숫자 (JSON) — 원격 점검으로 세션마다 봄

HEALTH_KEY = "music_health"
QUEUE_MAX = 30               # 방마다 기다리는 곡
PER_USER = 5                 # 한 사람이 동시에 걸어 둘 수 있는 곡
KEEP_DAYS = 90               # 끝난 곡 기록 보관 (방 인기곡·통계)
ORDER = "COALESCE(sort, id), id"   # 대기열 순서 (섞기 뒤엔 sort)
MODES_KEY = "music_modes"
MODE_DEFAULT = {"loopq": False, "autoplay": False, "vc_title": True}
AUTO_MAX = 10                # 사람 신청 없이 자동 재생으로 이어 트는 곡 수
CHOICE_SEC = 600             # 고르기 버튼 유효 시간


def _now() -> int:
    return int(time.time())


async def add(db, chat_id: int, *, title: str, url: str, vid: str | None, duration: int, by_id: int | None,
              by_name: str, path: str | None = None, msg_id: int | None = None,
              queue_max: int = QUEUE_MAX, per_user: int = PER_USER, auto: bool = False,
              orig_vid: str | None = None) -> tuple[int | None, int, str]:
    """대기열에 넣음 → (줄 id, 대기열 번호(1부터, 지금 아무것도 안 틀면 0), 이유).
    꽉 찼거나 같은 곡이 이미 있으면 (None, 0, 'full'|'per_user'|'dup')."""
    now = _now()

    def run(c):
        waiting = c.execute("SELECT COUNT(*) FROM music_queue WHERE chat_id=? AND state='queued'", (chat_id,)).fetchone()[0]
        if waiting >= queue_max:
            return None, 0, "full"
        if by_id is not None and per_user and c.execute(
                "SELECT COUNT(*) FROM music_queue WHERE chat_id=? AND by_id=? AND state='queued'",
                (chat_id, by_id)).fetchone()[0] >= per_user:
            return None, 0, "per_user"
        if vid and c.execute("SELECT 1 FROM music_queue WHERE chat_id=? AND vid=? AND state IN ('queued','playing')",
                             (chat_id, vid)).fetchone():
            return None, 0, "dup"                   # 같은 곡 두 번 (실측: AI 가 같은 링크를 세 번 넣음)
        playing = c.execute("SELECT 1 FROM music_queue WHERE chat_id=? AND state='playing'", (chat_id,)).fetchone()
        rid = c.execute("INSERT INTO music_queue(chat_id, title, url, vid, duration, by_id, by_name, path, msg_id, ts, "
                        "auto, orig_vid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (chat_id, title[:200], url[:500], vid, int(duration or 0), by_id, (by_name or "")[:60], path,
                         msg_id, now, int(bool(auto)), orig_vid)).lastrowid
        return rid, (waiting + 1 if playing else waiting), "ok"   # 0 = 바로 틀 차례 (앞에 아무것도 없음)
    return await db.atomic(run)


async def start_next(db, chat_id: int):
    """다음 곡을 playing 으로 (한 번만). 이미 playing 인 게 있으면 그것 (재시작 뒤 이어 틀기)."""
    def run(c):
        cur = c.execute("SELECT * FROM music_queue WHERE chat_id=? AND state='playing' ORDER BY id LIMIT 1",
                        (chat_id,)).fetchone()
        if cur:
            return cur
        nxt = c.execute(f"SELECT * FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY {ORDER} LIMIT 1",
                        (chat_id,)).fetchone()
        if not nxt:
            return None
        c.execute("UPDATE music_queue SET state='playing', started_ts=? WHERE id=? AND state='queued'", (_now(), nxt["id"]))
        return c.execute("SELECT * FROM music_queue WHERE id=?", (nxt["id"],)).fetchone()
    return await db.atomic(run)


async def finish(db, row_id: int, state: str = "done") -> None:
    await db._write("UPDATE music_queue SET state=?, end_ts=? WHERE id=? AND state IN ('playing','queued')",
                    (state, _now(), row_id))


async def requeue_front(db, row_id: int) -> None:
    """반복(loop): 같은 곡을 처음부터 다시 — playing 그대로 두고 위치만 0."""
    await db._write("UPDATE music_queue SET pos_ms=0 WHERE id=?", (row_id,))


async def save_pos(db, row_id: int, pos_ms: int) -> None:
    await db._write("UPDATE music_queue SET pos_ms=? WHERE id=?", (int(pos_ms), row_id))


async def set_path(db, row_id: int, path: str | None) -> None:
    await db._write("UPDATE music_queue SET path=? WHERE id=?", (path, row_id))


async def set_track(db, row_id: int, title: str, url: str, vid: str, duration: int) -> None:
    """기본 음원이 막혀 대체 음원의 같은 노래로 바꿈 (원래 ID 는 orig_vid 에 남김 — 자동 재생 기준·통계)."""
    await db._write("UPDATE music_queue SET orig_vid=COALESCE(orig_vid, vid), title=?, url=?, vid=?, duration=? WHERE id=?",
                    (title[:200], url[:500], vid, int(duration or 0), row_id))


async def set_msg(db, row_id: int, msg_id: int | None) -> None:
    await db._write("UPDATE music_queue SET msg_id=? WHERE id=?", (msg_id, row_id))


async def current(db, chat_id: int):
    return await db._one("SELECT * FROM music_queue WHERE chat_id=? AND state='playing' ORDER BY id LIMIT 1", (chat_id,))


async def waiting(db, chat_id: int, limit: int = QUEUE_MAX) -> list:
    return await db._all(f"SELECT * FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY {ORDER} LIMIT ?",
                         (chat_id, limit))


async def remove_nth(db, chat_id: int, n: int, uid: int, admin: bool):
    """대기열 n번째(1부터)를 뺌. 관리자는 아무 곡, 아니면 자기가 신청한 곡만 → (줄, 이유 ok|none|not_yours)."""
    def run(c):
        rows = c.execute(f"SELECT * FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY {ORDER} LIMIT 1 OFFSET ?",
                         (chat_id, max(0, n - 1))).fetchall() if n >= 1 else []
        if not rows:
            return None, "none"
        r = rows[0]
        if not admin and r["by_id"] != uid:
            return r, "not_yours"
        c.execute("UPDATE music_queue SET state='removed', end_ts=? WHERE id=? AND state='queued'", (_now(), r["id"]))
        return r, "ok"
    return await db.atomic(run)


async def clear(db, chat_id: int, reason: str = "removed") -> int:
    """대기열·지금 곡 전부 끝 (끝내기). 지운 곡 수."""
    cur = await db.conn.execute("UPDATE music_queue SET state=?, end_ts=? WHERE chat_id=? AND state IN ('queued','playing')",
                                (reason, _now(), chat_id))
    await db.conn.commit()
    return cur.rowcount or 0


async def clear_waiting(db, chat_id: int) -> int:
    """기다리는 곡만 전부 뺌 (지금 곡은 그대로) — '나머지 다 취소'."""
    def run(c):
        return c.execute("UPDATE music_queue SET state='removed', end_ts=? WHERE chat_id=? AND state='queued'",
                         (_now(), chat_id)).rowcount
    return await db.atomic(run)


async def shuffle(db, chat_id: int, rng=None) -> int:
    """기다리는 곡 순서 섞기 → 섞은 곡 수. 지금 곡·새로 들어올 곡(뒤에 붙음)은 그대로."""
    import random
    rng = rng or random.Random()

    def run(c):
        rows = c.execute(f"SELECT id, COALESCE(sort, id) k FROM music_queue WHERE chat_id=? AND state='queued' ORDER BY {ORDER}",
                         (chat_id,)).fetchall()
        if len(rows) < 2:
            return len(rows)
        keys = [r["k"] for r in rows]
        ids = [r["id"] for r in rows]
        while True:
            rng.shuffle(ids)
            if len(ids) < 3 or ids != [r["id"] for r in rows]:    # 섞었는데 그대로면 한 번 더
                break
        for k, i in zip(keys, ids):
            c.execute("UPDATE music_queue SET sort=? WHERE id=?", (k, i))
        return len(rows)
    return await db.atomic(run)


async def requeue_end(db, row) -> int | None:
    """대기열 전체 반복: 다 튼 곡을 맨 뒤에 다시 (같은 신청자·자동 표시 그대로, 한도·중복 검사 없이)."""
    def run(c):
        return c.execute("INSERT INTO music_queue(chat_id, title, url, vid, duration, by_id, by_name, path, ts, auto, orig_vid) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (row["chat_id"], row["title"], row["url"], row["vid"], row["duration"], row["by_id"], row["by_name"],
                          row["path"], _now(), row.get("auto") or 0, row.get("orig_vid"))).lastrowid
    return await db.atomic(run)


async def modes(db, chat_id: int) -> dict:
    cur = await db.get_state(chat_id, MODES_KEY)
    out = dict(MODE_DEFAULT)
    if isinstance(cur, dict):
        out.update({k: bool(v) for k, v in cur.items() if k in MODE_DEFAULT})
    return out


async def set_mode(db, chat_id: int, key: str, value: bool) -> dict:
    if key not in MODE_DEFAULT:
        raise KeyError(key)
    cur = await modes(db, chat_id)
    cur[key] = bool(value)
    await db.set_state(chat_id, MODES_KEY, cur)
    return cur


async def recent_tracks(db, chat_id: int, limit: int = 50) -> list:
    """이 방에서 최근 튼(또는 넣은) 곡 — 자동 재생이 같은 곡을 또 고르지 않게."""
    return await db._all("SELECT vid, orig_vid, title, auto, by_id, state FROM music_queue WHERE chat_id=? "
                         "ORDER BY id DESC LIMIT ?", (chat_id, limit))


async def auto_streak(db, chat_id: int) -> int:
    """맨 끝부터 이어진 자동 재생 곡 수 (사람이 신청하면 0부터)."""
    n = 0
    for r in await db._all("SELECT auto FROM music_queue WHERE chat_id=? AND state NOT IN ('removed') ORDER BY id DESC LIMIT ?",
                           (chat_id, AUTO_MAX + 1)):
        if not r["auto"]:
            break
        n += 1
    return n


async def top_tracks(db, chat_id: int, days: int = 30, limit: int = 10) -> list[dict]:
    """방 인기곡: 사람이 신청해서 실제로 튼 곡 (자동 재생·뺀 곡 빼고), 같은 곡은 원래 ID 로 묶음."""
    since = _now() - max(1, min(KEEP_DAYS, int(days))) * 86400
    rows = await db._all(
        "SELECT COALESCE(orig_vid, vid, title) k, MAX(id) last, COUNT(*) n, COUNT(DISTINCT by_id) people FROM music_queue "
        "WHERE chat_id=? AND auto=0 AND started_ts IS NOT NULL AND ts>=? GROUP BY k ORDER BY n DESC, last DESC LIMIT ?",
        (chat_id, since, limit))
    out = []
    for r in rows:
        t = await db._one("SELECT title, duration FROM music_queue WHERE id=?", (r["last"],))
        out.append({"title": t["title"] if t else "?", "duration": t["duration"] if t else 0, "n": r["n"], "people": r["people"]})
    return out


async def popular_vids(db, days: int = 30, limit: int = 20, min_n: int = 2) -> list[str]:
    """모든 방에서 사람이 신청해 실제로 튼 곡 중 많이 튼 것 (새벽에 미리 받아 둘 곡) — 실제로 받은 ID 기준."""
    since = _now() - max(1, min(KEEP_DAYS, int(days))) * 86400
    rows = await db._all("SELECT vid, COUNT(*) n FROM music_queue WHERE auto=0 AND started_ts IS NOT NULL AND ts>=? "
                         "AND vid IS NOT NULL AND vid<>'' GROUP BY vid HAVING n>=? ORDER BY n DESC, MAX(id) DESC LIMIT ?",
                         (since, min_n, limit))
    return [r["vid"] for r in rows]


async def top_requesters(db, chat_id: int, days: int = 30, limit: int = 5) -> list[dict]:
    since = _now() - max(1, min(KEEP_DAYS, int(days))) * 86400
    rows = await db._all("SELECT by_id, MAX(by_name) name, COUNT(*) n FROM music_queue WHERE chat_id=? AND auto=0 AND by_id IS NOT NULL "
                         "AND started_ts IS NOT NULL AND ts>=? GROUP BY by_id ORDER BY n DESC LIMIT ?", (chat_id, since, limit))
    return [dict(r) for r in rows]


async def save_lyrics(db, vid: str, title: str, track: str, artist: str, plain: str, source: str) -> None:
    await db._write("INSERT OR REPLACE INTO music_lyrics(vid, title, track, artist, plain, source, ts) VALUES(?,?,?,?,?,?,?)",
                    (vid, title[:200], track[:200], artist[:200], plain[:20000], source[:40], _now()))


async def lyrics(db, vid: str | None):
    return await db._one("SELECT * FROM music_lyrics WHERE vid=?", (vid,)) if vid else None


async def chats_with_queue(db, since: int) -> list[int]:
    """재시작 뒤 이어 틀 방: 최근(since 이후)에 쓴 곡이 남아 있는 방."""
    rows = await db._all("SELECT DISTINCT chat_id FROM music_queue WHERE state IN ('queued','playing') "
                         "AND COALESCE(started_ts, ts)>=?", (since,))
    return [r["chat_id"] for r in rows]


async def drop_except(db, keep: list[int]) -> int:
    """음성 담당이 다시 켜짐: 이어 틀 방(keep) 말고 남은 대기열은 정리 + 끝난 곡 기록 KEEP_DAYS."""
    now = _now()
    keep = [int(c) for c in keep]

    def run(c):
        marks = ",".join("?" * len(keep))
        n = c.execute("UPDATE music_queue SET state='removed', end_ts=? WHERE state IN ('queued','playing')"
                      + (f" AND chat_id NOT IN ({marks})" if keep else ""), (now, *keep)).rowcount
        c.execute("DELETE FROM music_queue WHERE state NOT IN ('queued','playing') AND COALESCE(end_ts, ts)<?",
                  (now - KEEP_DAYS * 86400,))
        return n
    return await db.atomic(run)


# ── 세션 ──────────────────────────────────────────────
REJOIN_SEC = 900                  # 음성채팅이 닫혀 멈춘 노래: 이 안에 다시 열리면 이어서 (음성 담당이 15초마다 확인)
REJOIN_KEY = "music_rejoin"       # chat_state(0): {방: 기다림 끝 시각}


async def has_queue(db, chat_id: int) -> bool:
    return bool(await db._one("SELECT 1 FROM music_queue WHERE chat_id=? AND state IN ('queued','playing') LIMIT 1",
                              (chat_id,)))


async def session_start(db, chat_id: int, by: int | None) -> int:
    return await db._write("INSERT INTO music_sessions(chat_id, by_id, start_ts) VALUES(?,?,?)", (chat_id, by, _now()))


async def session_end(db, sid: int, reason: str, tracks: int, stats: dict | None = None) -> None:
    await db._write("UPDATE music_sessions SET end_ts=?, reason=?, tracks=?, stats=? WHERE id=? AND end_ts IS NULL",
                    (_now(), reason[:40], tracks, json.dumps(stats) if stats else None, sid))


async def active_session(db, chat_id: int):
    return await db._one("SELECT * FROM music_sessions WHERE chat_id=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1",
                         (chat_id,))


async def close_orphans(db) -> list[int]:
    """음성 담당이 다시 켜짐 → 끝 표시 없는 세션은 restart 로 닫고 그 방들 (이어 틀기·남은 통화 정리용)."""
    rows = await db._all("SELECT DISTINCT chat_id FROM music_sessions WHERE end_ts IS NULL")
    await db._write("UPDATE music_sessions SET end_ts=?, reason='restart' WHERE end_ts IS NULL", (_now(),))
    return [r["chat_id"] for r in rows]


async def restart_chats(db, since: int) -> list[int]:
    """재시작으로 끊긴 방: 마지막 세션이 'restart' 로 끝남 (정상 종료든 kill -9 뒤 close_orphans 든), since 이후."""
    rows = await db._all("SELECT chat_id, reason, end_ts FROM music_sessions s WHERE id=(SELECT MAX(id) FROM music_sessions "
                         "WHERE chat_id=s.chat_id)")
    return [r["chat_id"] for r in rows if r["reason"] == "restart" and (r["end_ts"] or 0) >= since]


async def ended_unnotified(db) -> list:
    return await db._all("SELECT * FROM music_sessions WHERE end_ts IS NOT NULL AND notified=0 ORDER BY id LIMIT 20")


async def mark_notified(db, sid: int) -> bool:
    cur = await db.conn.execute("UPDATE music_sessions SET notified=1 WHERE id=? AND notified=0", (sid,))
    await db.conn.commit()
    return bool(cur.rowcount)


def fmt_dur(sec: int | float | None) -> str:
    s = int(sec or 0)
    if s <= 0:
        return "?"
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── 방에 보일 글 (봇·음성 담당 같이) ─────────────────────────
def _esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card_text(kind: str, row: dict, *, pos: int = 0, why: str = "") -> str:
    title, dur = _esc(row.get("title"))[:120], fmt_dur(row.get("duration"))
    by = _esc(row.get("by_name") or "")[:40]
    tail = f"⏱ {dur}" + (f" · 신청: {by}" if by else "")
    if kind == "now":
        return f"🎶 <b>재생 시작</b>\n<b>{title}</b>\n{tail}"
    if kind == "queued":
        return f"➕ <b>대기열 추가</b> (#{pos})\n{title}\n{tail}"
    return f"⚠️ 못 틀었어요: {title}\n{_esc(why)}"


def card_kb():
    from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[B("⏸ 일시정지", callback_data="mu:pause"), B("▶️ 다시", callback_data="mu:resume"),
                                  B("⏭ 다음", callback_data="mu:skip")],
                                 [B("📃 대기열", callback_data="mu:queue"), B("⏹ 끝내기", callback_data="mu:end")]])


# ── 고르기 버튼 (딱 맞는 곡이 없을 때·버전 고르기) ─────────────────
async def save_choice(db, chat_id: int, by_id: int | None, by_name: str, query: str, reason: str, items: list[dict]) -> int:
    import json
    now = _now()

    def run(c):
        c.execute("DELETE FROM music_choices WHERE ts<?", (now - 86400,))
        return c.execute("INSERT INTO music_choices(chat_id, by_id, by_name, query, reason, items, ts) VALUES(?,?,?,?,?,?,?)",
                         (chat_id, by_id, (by_name or "")[:60], (query or "")[:200], reason,
                          json.dumps(items[:4], ensure_ascii=False), now)).lastrowid
    return await db.atomic(run)


async def set_choice_msg(db, cid: int, msg_id: int | None) -> None:
    await db._write("UPDATE music_choices SET msg_id=? WHERE id=?", (msg_id, cid))


async def get_choice(db, cid: int) -> dict | None:
    import json
    row = await db._one("SELECT * FROM music_choices WHERE id=?", (cid,))
    if not row:
        return None
    out = dict(row)
    try:
        out["items"] = [x for x in json.loads(out["items"]) if isinstance(x, dict)]
    except ValueError:
        out["items"] = []
    return out


async def claim_choice(db, cid: int) -> bool:
    """한 번만 (연타·두 사람이 같이 눌러도)."""
    def run(c):
        return c.execute("UPDATE music_choices SET used=1 WHERE id=? AND used=0", (cid,)).rowcount == 1
    return await db.atomic(run)


def choice_text(reason: str, query: str) -> str:
    q = _esc(query)[:60]
    if reason == "intent":
        return f"🎵 <b>{q}</b> — 어떤 버전으로 틀까요?\n(신청한 분이 골라 주세요 · 10분)"
    return (f"🎵 <b>{q}</b> — 딱 맞는 곡을 못 찾았어요. 이 중에 있으면 골라 주세요.\n"
            "(신청한 분이 골라 주세요 · 10분 · 없으면 가수와 제목을 같이 써서 다시)")


def choice_kb(cid: int, items: list[dict]):
    from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup
    from .musicmatch import KIND_LABEL
    rows = []
    for i, it in enumerate(items[:4]):
        label = KIND_LABEL.get(it.get("kind") or "", "🎵")
        title = str(it.get("title") or "?")
        title = title if len(title) <= 40 else title[:39] + "…"
        rows.append([B(f"{label} {title} ({fmt_dur(it.get('duration'))})", callback_data=f"mu:pk:{cid}:{i}")])
    rows.append([B("❌ 전부 아님", callback_data=f"mu:pk:{cid}:x")])
    return InlineKeyboardMarkup(rows)
