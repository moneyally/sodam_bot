"""재시작에도 이어져야 하는 상태 (배포마다·컨테이너 회수로 봇이 자주 다시 켜짐).

메모리는 캐시로만 쓰고, 재시작 뒤 사용자에게 차이가 보이는 것은 여기 DB 표에 둔다:
- temp_msgs: 잠깐 보였다 지울 안내 (send_temp·post_temp·게임 임시 답장·퇴장 인사). 프로세스 타이머가 제시간에 지우고,
  재시작으로 타이머가 사라진 것은 sweep(30초마다)이 지운다. 48시간 넘은 글은 텔레그램이 못 지우게 해서 버림.
- claims: '이 일은 한 번만' 차지 (퇴장 인사 두 경로 중복 등). 한 문장 UPSERT 라 동시에 와도 하나만.
- menu_inputs: 1:1 메뉴가 기다리는 글자 입력 (svc.inputs 쓰기 그대로 DB 에도) → 배포 중 입력하던 값이 다음 말로 이어짐.
- pending_actions: 경고·뮤트·밴 확인 카드 (svc.add_pending) → 재시작 뒤 눌러도 '만료' 아님 (카드 유효시간은 그대로).
- live_games: 진행 중인 게임이 있던 방 → 다시 켜질 때 '봇이 다시 시작돼서 게임을 끝냈어요' (방이 조용히 멈춰 보이지 않게).
  포인트 베팅 환불은 원래대로 casino.startup → core.recover_open.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from telegram.error import BadRequest, Forbidden, TelegramError

from .db import register_schema

if TYPE_CHECKING:
    from .services import PendingAction, Services

log = logging.getLogger(__name__)

TEMP_MAX_AGE = 48 * 3600    # 봇은 48시간 지난 글을 못 지움 → 그 전 것만
SWEEP_GRACE = 20            # 제때 지울 프로세스 타이머가 있을 수 있어 기한이 이만큼 지난 것만 sweep 이 지움
SWEEP_BATCH = 200
INPUT_KEEP = 600            # 입력 시간이 지난 뒤에도 이만큼은 남김 (menu.INPUT_GRACE: '시간 지남' 안내용)
RESTART_NOTICE = "🔌 봇이 다시 시작돼서 게임을 끝냈어요 🙏"
CASINO_NOTICE = RESTART_NOTICE + " 걸려 있던 포인트는 돌려드렸어요(하이로우는 그때까지 상금으로 정산)."

register_schema("""
CREATE TABLE IF NOT EXISTS temp_msgs (
    bot_id     INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    due        REAL NOT NULL,
    sent       REAL NOT NULL,
    PRIMARY KEY (bot_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS temp_msgs_due ON temp_msgs(bot_id, due);
CREATE TABLE IF NOT EXISTS claims (
    k     TEXT PRIMARY KEY,
    until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS menu_inputs (
    user_id INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    args    TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    k       TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    data    TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_games (
    chat_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,          -- word (끝말잇기, 메인 봇) · casino (! 게임, 게임 맡는 봇)
    title   TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, kind)
);
""", migrate={"temp_msgs": "drop", "menu_inputs": "plain", "pending_actions": "plain", "live_games": "composite"})


# ── 뒤에서 하는 DB 쓰기 (동기 코드에서 부를 때) ────────────
_BG: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task | None:
    """실패는 로그만. 돌아가는 이벤트 루프가 없으면(동기 테스트) 안 함."""
    try:
        task = asyncio.get_running_loop().create_task(_quiet(coro))
    except RuntimeError:
        coro.close()
        return None
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


async def _quiet(coro) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:   # DB 가 닫힌 뒤(종료·테스트 끝) 등
        log.debug("persist write failed: %r", e)


async def drain() -> None:
    """테스트·종료용: 뒤에서 도는 쓰기가 끝날 때까지."""
    while _BG:
        await asyncio.gather(*list(_BG), return_exceptions=True)


# ── 봇 → DB (db 를 안 받는 옛 함수들: util.post_temp · casino.core.delete_later · commands) ─
_DBS: dict[int, tuple[Any, Any]] = {}


def bind(bot, db) -> None:
    _DBS[id(bot)] = (bot, db)


def unbind(bot) -> None:
    _DBS.pop(id(bot), None)


def db_of(bot):
    hit = _DBS.get(id(bot))
    return hit[1] if hit and hit[0] is bot else None


def _bot_id(bot) -> int:
    try:
        return int(getattr(bot, "id", 0) or 0)
    except Exception:   # 초기화 전 PTB Bot
        return 0


# ── 차지 ──────────────────────────────────────────────────
async def claim(db, key: str, ttl: float) -> bool:
    """key 를 ttl 초 동안 처음 차지하면 True (프로세스·재시작과 무관하게 한 번만)."""
    now = time.time()
    return bool(await db.atomic(lambda c: c.execute(
        "INSERT INTO claims(k, until) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET until=excluded.until "
        "WHERE claims.until <= ?", (key, now + ttl, now)).rowcount))


# ── 잠깐 보이는 글 지우기 ─────────────────────────────────
async def remember_delete(db, bot, chat_id: int, message_id: int, secs: float) -> None:
    now = time.time()
    await db._write("INSERT OR REPLACE INTO temp_msgs(bot_id, chat_id, message_id, due, sent) VALUES(?,?,?,?,?)",
                    (_bot_id(bot), chat_id, message_id, now + secs, now))


async def forget_delete(db, bot, chat_id: int, message_id: int) -> None:
    await db._write("DELETE FROM temp_msgs WHERE bot_id=? AND chat_id=? AND message_id=?",
                    (_bot_id(bot), chat_id, message_id))


async def delete_now(db, bot, chat_id: int, message_id: int) -> None:
    """제때 온 타이머: 지우고 표에서 뺌 (연결 오류면 sweep 이 다시)."""
    try:
        await bot.delete_message(chat_id, message_id)
    except (BadRequest, Forbidden):
        pass                    # 이미 지워짐·권한 없음 → 다시 해도 안 됨
    except TelegramError:
        return                  # 네트워크 등 → 표에 남겨 sweep 이 다시
    if db is not None:
        try:
            await forget_delete(db, bot, chat_id, message_id)
        except Exception as e:
            log.debug("temp forget failed: %r", e)


def delete_later(bot, chat_id: int, message_id: int | None, secs: float, *, db=None, sleep=None) -> asyncio.Task | None:
    """secs 초 뒤 지움. DB 에도 적어서 그 사이 재시작돼도 sweep 이 지운다 (db 없으면 bind 된 DB, 그것도 없으면 메모리만)."""
    if message_id is None:
        return None
    db = db if db is not None else db_of(bot)

    async def run() -> None:
        if db is not None:
            try:
                await remember_delete(db, bot, chat_id, message_id, secs)
            except Exception as e:
                log.debug("temp remember failed: %r", e)
        await (sleep or asyncio.sleep)(secs)
        await delete_now(db, bot, chat_id, message_id)
    return spawn(run())


async def sweep(db, bot, now: float | None = None) -> int:
    """기한이 지났는데 남은 글 (재시작으로 타이머가 사라짐) 지우기 + 오래된 차지·입력·확인 카드 정리. 지운 글 수."""
    now = time.time() if now is None else now
    rows = await db._all("SELECT chat_id, message_id, sent FROM temp_msgs WHERE bot_id=? AND due<=? ORDER BY due LIMIT ?",
                         (_bot_id(bot), now - SWEEP_GRACE, SWEEP_BATCH))
    done, n = [], 0
    for r in rows:
        if now - r["sent"] < TEMP_MAX_AGE:
            try:
                await bot.delete_message(r["chat_id"], r["message_id"])
                n += 1
            except (BadRequest, Forbidden):
                pass
            except TelegramError as e:      # 연결 오류: 다음 sweep 에 (48시간까지)
                log.info("temp delete retry later: %s", e)
                continue
        done.append((_bot_id(bot), r["chat_id"], r["message_id"]))

    def run(c) -> None:
        c.executemany("DELETE FROM temp_msgs WHERE bot_id=? AND chat_id=? AND message_id=?", done)
        c.execute("DELETE FROM temp_msgs WHERE sent < ?", (now - TEMP_MAX_AGE,))
        c.execute("DELETE FROM claims WHERE until < ?", (now,))
        c.execute("DELETE FROM menu_inputs WHERE expires < ?", (now - INPUT_KEEP,))
        c.execute("DELETE FROM pending_actions WHERE expires < ?", (now,))
    await db.atomic(run)
    return n


# ── 메모리 dict + DB 거울 ─────────────────────────────────
class MirrorDict(dict):
    """넣기·빼기를 DB 에도 (뒤에서, 순서대로). 읽기는 메모리 (메시지마다 DB 안 읽음).
    값 객체를 고쳤으면(기한 연장·단계 진행) save(key) 로 다시 저장. write(key, value)/drop(key) 는 DB 쓰기 코루틴."""

    def __init__(self, write, drop):
        super().__init__()
        self._write_fn, self._drop_fn = write, drop

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        spawn(self._write_fn(key, value))

    def save(self, key) -> None:
        if key in self:
            spawn(self._write_fn(key, dict.__getitem__(self, key)))

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        spawn(self._drop_fn(key))

    def pop(self, key, *default):
        had = key in self
        out = super().pop(key, *default)
        if had:
            spawn(self._drop_fn(key))
        return out

    def clear(self) -> None:
        for key in list(self):
            self.pop(key)


class InputStore(MirrorDict):
    """svc.inputs (1:1 메뉴 글자 입력 대기)."""

    def __init__(self, db):
        self.db = db

        async def write(uid, p) -> None:
            await db._write("INSERT OR REPLACE INTO menu_inputs(user_id, kind, chat_id, args, expires) VALUES(?,?,?,?,?)",
                            (uid, p.kind, p.chat_id, json.dumps(list(p.args)), p.expires))

        async def drop(uid) -> None:
            await db._write("DELETE FROM menu_inputs WHERE user_id=?", (uid,))
        super().__init__(write, drop)

    async def load(self) -> int:
        from .services import PendingInput
        rows = await self.db._all("SELECT * FROM menu_inputs WHERE expires >= ?", (time.time() - INPUT_KEEP,))
        for r in rows:
            try:
                args = [str(a) for a in json.loads(r["args"])]
            except (ValueError, TypeError):
                continue
            dict.__setitem__(self, r["user_id"], PendingInput(r["kind"], r["chat_id"], args=args, expires=r["expires"]))
        return len(rows)


# ── 제재 확인 카드 (svc.pending) ──────────────────────────
_TAKEN: set[str] = set()   # 이미 처리한 카드 (저장이 뒤에서 늦게 돌아도 되살리지 않게)


def save_pending(db, key: str, action: PendingAction) -> None:
    data = asdict(action)
    data.pop("refused", None)

    async def run() -> None:
        if key not in _TAKEN:
            await db._write("INSERT OR REPLACE INTO pending_actions(k, chat_id, data, expires) VALUES(?,?,?,?)",
                            (key, action.chat_id, json.dumps(data, ensure_ascii=False), action.expires))
    spawn(run())


async def load_pending(db, key: str) -> PendingAction | None:
    from .services import PendingAction
    row = await db._one("SELECT data FROM pending_actions WHERE k=?", (key,))
    if not row:
        return None
    try:
        data = json.loads(row["data"])
        data["extra"] = tuple((int(u), str(n)) for u, n in data.get("extra") or ())
        return PendingAction(**data)
    except (ValueError, TypeError) as e:
        log.warning("pending action %s unreadable: %r", key, e)
        return None


async def take_pending(db, key: str) -> bool:
    """DB 줄을 지운 쪽만 True (재시작 뒤 두 번 빨리 눌러도 한 번만 실행)."""
    if len(_TAKEN) > 10_000:
        _TAKEN.clear()
    _TAKEN.add(key)
    return bool(await db.atomic(lambda c: c.execute("DELETE FROM pending_actions WHERE k=?", (key,)).rowcount))


# ── 진행 중이던 게임 ──────────────────────────────────────
async def game_started(db, chat_id: int, kind: str, title: str) -> None:
    await db._write("INSERT OR REPLACE INTO live_games(chat_id, kind, title, ts) VALUES(?,?,?,?)",
                    (chat_id, kind, title, int(time.time())))


async def game_ended(db, chat_id: int, kind: str) -> None:
    await db._write("DELETE FROM live_games WHERE chat_id=? AND kind=?", (chat_id, kind))


async def announce_ended(svc: Services, bot) -> int:
    """다시 켜질 때: 끝내지 못한 게임이 있던 방에 한 줄 (이 프로세스가 맡는 게임만). 알린 방 수."""
    role = getattr(svc.cfg, "bot_role", "all")
    kinds = [k for k, skip in (("word", "dealer"), ("casino", "main")) if role != skip]
    if not kinds:
        return 0
    rows = await svc.db._all(f"SELECT * FROM live_games WHERE kind IN ({','.join('?' * len(kinds))})", tuple(kinds))
    n = 0
    for r in rows:
        chat_id, kind = r["chat_id"], r["kind"]
        await game_ended(svc.db, chat_id, kind)          # 먼저 지움: 보내다 또 죽어도 두 번 알리지 않게
        if kind == "word" and svc.games is not None:   # AI 단서·game_control restart 용 (title = games.GAMES 키)
            from .games import GAMES
            svc.games.recent[chat_id] = (time.monotonic(), r["title"] if r["title"] in GAMES else "",
                                         f"{r['title']} 끝남: 봇이 다시 시작돼서 끝냄")
        try:
            await bot.send_message(chat_id, CASINO_NOTICE if kind == "casino" else RESTART_NOTICE)
            n += 1
        except TelegramError as e:
            log.info("restart game notice failed in %s: %s", chat_id, e)
    return n


# ── 시작 · 종료 ───────────────────────────────────────────
async def restore(svc: Services, bot) -> dict[str, int]:
    """post_init 에서 (casino.startup 뒤, 폴링 전): 메모리 상태를 DB 에서 다시 채우고 끝난 게임을 알린다."""
    bind(bot, svc.db)
    out: dict[str, int] = {}
    steps = [("inputs", lambda: svc.inputs.load() if isinstance(svc.inputs, InputStore) else _zero()),
             ("drafts", lambda: svc.announcer.restore() if svc.announcer else _zero()),
             ("games", lambda: announce_ended(svc, bot)),
             ("memory", lambda: _memory_resume(svc))]
    for name, fn in steps:
        try:
            out[name] = await fn()
        except Exception:
            log.exception("restore %s failed", name)
    if any(out.values()):
        log.info("재시작 복구: %s", out)
    return out


async def _zero() -> int:
    return 0


async def _memory_resume(svc: Services) -> int:
    if getattr(svc.cfg, "bot_role", "all") == "dealer":
        return 0
    from . import memory
    return await memory.resume(svc)


async def flush_on_stop(svc: Services, bot, bot_data: dict | None = None, timeout: float = 10.0) -> None:
    """정상 종료(배포) 때 post_stop 에서 (봇 연결이 아직 살아 있을 때): 몇 초 모아 보내던 입장·퇴장 인사를 지금 보냄."""
    async def run() -> None:
        greeter = svc.greeter
        for chat_id in list(getattr(greeter, "_pending", {}) or {}):
            try:
                await greeter.flush(bot, chat_id)
            except Exception:
                log.exception("greet flush on stop failed in %s", chat_id)
        from . import farewell
        await farewell.flush_all(svc, bot, bot_data or {"svc": svc})
    try:
        await asyncio.wait_for(run(), timeout)
    except Exception as e:
        log.info("flush on stop: %r", e)


async def job_sweep(context) -> None:
    """30초마다 (메인·딜러 봇 모두): 재시작으로 남은 임시 글 지우기·오래된 줄 정리."""
    svc = context.bot_data.get("svc")
    if svc is None:
        return
    try:
        await sweep(svc.db, context.bot)
    except Exception:
        log.exception("persist sweep failed")
    try:
        from . import aiqueue
        await aiqueue.sweep(context)
    except Exception:
        log.exception("ai queue sweep failed")
