"""MTProto 도우미 (선택 기능): Bot API 로는 못 얻는 데이터를 Telethon 으로.

① 봇 세션 — 같은 봇 토큰 + MTPROTO_API_ID/HASH(.env) 로 로그인. `participants`·`member_diff` (channels.getParticipants:
   core.telegram.org "Both users and bots can use this method", 봇이 관리자여야 함).
② 사용자 세션 — 기본 꺼짐. 오너가 서버에서 `python tools/mtproto_login.py` 로 전용 계정 세션을 만들면(data/mtproto_user.session, 0600)
   켜짐. `views` (messages.getMessagesViews: "Only users can use this method"). 채널마다 30분에 1번만 부르고 DB 에 캐시.
   로그인 코드는 절대 텔레그램 채팅으로 받지 않음 (채팅에 올라온 코드는 텔레그램이 무효화함) → 서버 터미널에서만.

PTB 폴링과 충돌 없음: 두 클라이언트 모두 receive_updates=False → Telethon 이 모든 요청을 invokeWithoutUpdates 로 감싸고
업데이트 루프도 요청하지 않음 (Telethon 문서: "Turning this off means that Telegram will not send updates at all").
MTProto 업데이트 상태는 세션(authorization)마다 따로라 Bot API 서버의 getUpdates 큐를 가져가지 않는다.
설정이 없거나 로그인이 실패하면 모든 API 가 None (봇은 계속 돈다). 다른 모듈은 `svc.mtproto` 로 쓴다 (없으면 None).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import db as dbm

log = logging.getLogger(__name__)

MAX_FLOOD_SLEEP = 30        # FloodWait 이 이 초 이하면 기다렸다 1번 다시, 넘으면 포기(None) + 그 시각까지 호출 안 함
MIN_GAP = 1.0               # 같은 세션 요청 사이 최소 간격 (초)
CALL_TIMEOUT = 120          # 한 번의 조회 (참가자 1만 명 = 요청 ~50번)
CONNECT_TIMEOUT = 30
VIEWS_GAP = 30 * 60         # 채널마다 조회수 호출 간격
VIEWS_MAX_IDS = 100
RESTART_GAP = 30            # [🔄 다시 연결] 연타 방지 (봇 로그인 반복은 FloodWait 을 부름)
_sleep = asyncio.sleep      # 테스트가 바꿔 끼움

dbm.register_schema("""
CREATE TABLE IF NOT EXISTS mtproto_members (
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, first_name TEXT, username TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0, seen_ts INTEGER NOT NULL, PRIMARY KEY (chat_id, user_id));
CREATE TABLE IF NOT EXISTS mtproto_snap (chat_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mtproto_views (
    chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, views INTEGER NOT NULL, ts INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id));
CREATE TABLE IF NOT EXISTS mtproto_view_calls (chat_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL);
""", migrate={"mtproto_members": "drop", "mtproto_snap": "drop", "mtproto_views": "drop", "mtproto_view_calls": "drop"})


# ── chat_id · 세션 파일 ─────────────────────────────────────
def to_peer(chat_id: int):
    """Bot API chat_id → Telethon Peer. -100XXXXXXXXXX = 채널·슈퍼그룹, 음수 = 기본 그룹, 양수 = 사용자."""
    from telethon.tl.types import PeerChannel, PeerChat, PeerUser
    if chat_id <= -1_000_000_000_000:
        return PeerChannel(-chat_id - 1_000_000_000_000)
    if chat_id < 0:
        return PeerChat(-chat_id)
    return PeerUser(chat_id)


def write_session(path: Path, value: str) -> None:
    """세션 문자열 = 계정 열쇠. 처음부터 0600 으로 만들고(umask 무관) 원자적으로 교체."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, value.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_session(path: Path) -> str:
    try:
        if path.stat().st_mode & 0o077:
            log.warning("mtproto: %s 권한이 넓어서 0600 으로 고침", path)
            os.chmod(path, 0o600)
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def make_client(session: str, api_id: int, api_hash: str):
    """Telethon 클라이언트. 업데이트는 받지 않음(PTB 폴링과 분리), FloodWait 은 직접 처리(flood_sleep_threshold=0)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or None), api_id, api_hash, receive_updates=False,
                          flood_sleep_threshold=0, device_model="sodam-helper", connection_retries=2)


def _flood_seconds(e: BaseException) -> int | None:
    secs = getattr(e, "seconds", None)
    return secs if "Wait" in type(e).__name__ and isinstance(secs, int) else None


@dataclass
class Helper:
    name: str
    session_path: Path
    configured: bool = False
    client: Any = None
    me: str = ""
    last_error: str = ""
    last_error_ts: int = 0
    last_flood: tuple[int, int] | None = None   # (시각, 초)
    flood_until: float = 0.0
    next_ok: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def connected(self) -> bool:
        try:
            return bool(self.client and self.client.is_connected())
        except Exception:
            return False

    def fail(self, what: str, e: BaseException | str) -> None:
        self.last_error, self.last_error_ts = f"{what}: {e}"[:200], int(time.time())
        log.warning("mtproto %s %s: %s", self.name, what, e)


class MTProto:
    def __init__(self, cfg, db, *, factory: Callable[[str, int, str], Any] | None = None):
        self.cfg, self.db = cfg, db
        self.factory = factory   # 테스트는 가짜 클라이언트 (None = make_client)
        base = Path(cfg.db_path).resolve().parent
        self.bot = Helper("bot", base / "mtproto_bot.session", configured=bool(cfg.mtproto_api_id and cfg.mtproto_api_hash))
        self.user = Helper("user", base / "mtproto_user.session")
        self._task: asyncio.Task | None = None
        self._restart_at = 0.0
        self._warmed = False

    @property
    def enabled(self) -> bool:
        return self.bot.configured

    # ── 시작 · 종료 ─────────────────────────────────────────
    def start_background(self) -> None:
        """post_init 에서: 로그인은 뒤에서 (네트워크가 느려도 봇 시작·폴링을 막지 않게)."""
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.start())

    async def start(self) -> None:
        if not self.enabled:
            return
        await self._start_bot()
        await self._start_user()

    async def _connect(self, h: Helper, session: str):
        client = (self.factory or make_client)(session, self.cfg.mtproto_api_id, self.cfg.mtproto_api_hash)
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        return client

    async def _start_bot(self) -> None:
        h, token, client = self.bot, self.cfg.telegram_token, None
        try:
            client = await self._connect(h, read_session(h.session_path))
            me = await client.get_me() if await client.is_user_authorized() else None
            if me is not None and str(me.id) != token.split(":")[0]:   # 토큰이 바뀜 → 옛 세션 버리고 새로
                await client.disconnect()
                client, me = await self._connect(h, ""), None
            if me is None:
                me = await asyncio.wait_for(client.sign_in(bot_token=token), CONNECT_TIMEOUT)
            write_session(h.session_path, client.session.save())
            h.client, h.me, h.last_error = client, f"@{getattr(me, 'username', '') or me.id}", ""
            log.info("mtproto 봇 세션 연결됨 (%s)", h.me)
        except Exception as e:   # 로그인 실패는 기능만 꺼짐, 봇은 계속
            h.fail("봇 로그인 실패", e)
            await self._drop(h, client)

    async def _start_user(self) -> None:
        h, client = self.user, None
        session = read_session(h.session_path)
        h.configured = bool(session)
        if not session:
            return
        try:
            client = await self._connect(h, session)
            if not await client.is_user_authorized():
                raise RuntimeError("세션 만료 — 서버에서 tools/mtproto_login.py 다시 실행")
            me = await client.get_me()
            if getattr(me, "bot", False):
                raise RuntimeError("사용자 세션 파일에 봇 계정이 들어 있음")
            h.client, h.me, h.last_error = client, f"@{me.username}" if me.username else str(me.id), ""
            log.info("mtproto 사용자 세션 연결됨 (%s)", h.me)
        except Exception as e:
            h.fail("사용자 세션 실패", e)
            await self._drop(h, client)

    @staticmethod
    async def _drop(h: Helper, client) -> None:
        h.client = None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 10)
            except Exception:
                pass

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
        for h in (self.bot, self.user):
            client, h.client = h.client, None
            await self._drop(h, client)

    def restart(self) -> bool:
        """오너 [🔄 다시 연결]. RESTART_GAP 안에 또 누르면 False."""
        now = time.monotonic()
        if now - self._restart_at < RESTART_GAP or (self._task and not self._task.done()):
            return False
        self._restart_at = now
        self._warmed = False

        async def run():
            await self.stop()
            await self.start()
        self._task = asyncio.create_task(run())
        return True

    # ── 공통 호출: 간격 · FloodWait · 오류 ────────────────────
    async def _run(self, h: Helper, fn: Callable[[Any], Awaitable[Any]]):
        async with h.lock:
            for attempt in range(2):
                if h.client is None or time.time() < h.flood_until:   # 기다리는 동안 다른 호출이 FloodWait 을 받았을 수 있음
                    return None
                wait = h.next_ok - time.monotonic()
                if wait > 0:
                    await _sleep(wait)
                try:
                    return await asyncio.wait_for(fn(h.client), CALL_TIMEOUT)
                except Exception as e:
                    secs = _flood_seconds(e)
                    if secs is None:
                        h.fail("호출 실패", repr(e))
                        return None
                    h.last_flood = (int(time.time()), secs)
                    if secs > MAX_FLOOD_SLEEP or attempt:
                        h.flood_until = time.time() + secs
                        h.fail("FloodWait 포기", f"{secs}초")
                        return None
                    log.info("mtproto %s FloodWait %d초 기다림", h.name, secs)
                    await _sleep(secs)
                finally:
                    h.next_ok = time.monotonic() + MIN_GAP
        return None

    # ── ① 봇: 참가자 ─────────────────────────────────────────
    async def participants(self, chat_id: int, limit: int = 10000) -> list[dict] | None:
        async def fetch(client):
            entity = await client.get_input_entity(to_peer(chat_id))
            return [{"id": u.id, "first_name": u.first_name or "", "username": u.username or "", "is_bot": bool(u.bot)}
                    async for u in client.iter_participants(entity, limit=limit)]
        return await self._run(self.bot, fetch)

    async def member_diff(self, chat_id: int, limit: int = 10000) -> dict | None:
        """지난 스냅샷 대비 들어온·나간 사람. {first, joined, left, count, partial}. 첫 스냅샷은 first=True·빈 목록.
        partial(limit 에 걸림) 이면 '나감'은 알 수 없어서 비우고 스냅샷에서 지우지도 않음."""
        members = await self.participants(chat_id, limit)
        if members is None:
            return None
        partial, now = len(members) >= limit, int(time.time())

        def run(c: sqlite3.Connection) -> dict:
            first = c.execute("SELECT 1 FROM mtproto_snap WHERE chat_id=?", (chat_id,)).fetchone() is None
            old = {r[0]: r for r in c.execute(
                "SELECT user_id, first_name, username, is_bot FROM mtproto_members WHERE chat_id=?", (chat_id,))}
            cur = {m["id"]: m for m in members}
            joined = [] if first else [m for uid, m in cur.items() if uid not in old]
            left = [] if first or partial else [{"id": r[0], "first_name": r[1] or "", "username": r[2] or "", "is_bot": bool(r[3])}
                                                for uid, r in old.items() if uid not in cur]
            if not partial:
                c.executemany("DELETE FROM mtproto_members WHERE chat_id=? AND user_id=?",
                              [(chat_id, uid) for uid in old if uid not in cur])
            c.executemany("INSERT INTO mtproto_members(chat_id, user_id, first_name, username, is_bot, seen_ts) "
                          "VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id, user_id) DO UPDATE SET first_name=excluded.first_name, "
                          "username=excluded.username, is_bot=excluded.is_bot, seen_ts=excluded.seen_ts",
                          [(chat_id, m["id"], m["first_name"], m["username"], int(m["is_bot"]), now) for m in members])
            c.execute("INSERT INTO mtproto_snap(chat_id, ts, count) VALUES(?,?,?) ON CONFLICT(chat_id) "
                      "DO UPDATE SET ts=excluded.ts, count=excluded.count", (chat_id, now, len(members)))
            return {"first": first, "joined": joined, "left": left, "count": len(members), "partial": partial}
        return await self.db.atomic(run)

    # ── ② 사용자: 조회수 ──────────────────────────────────────
    async def views(self, chat_id: int, msg_ids) -> dict[int, int] | None:
        """채널 글 조회수. 채널마다 VIEWS_GAP 에 한 번만 실제 호출, 그 사이엔 DB 캐시 (없는 글은 빠짐)."""
        if self.user.client is None:
            return None
        ids = sorted({int(i) for i in msg_ids if int(i) > 0})[:VIEWS_MAX_IDS]
        if not ids:
            return {}
        now = int(time.time())
        claimed = await self.db.atomic(lambda c: c.execute(   # 한 문장으로 차지 → 동시에 불러도 1번만
            "INSERT INTO mtproto_view_calls(chat_id, ts) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET ts=excluded.ts "
            "WHERE mtproto_view_calls.ts <= ?", (chat_id, now, now - VIEWS_GAP)).rowcount)
        if not claimed:
            return await self._cached_views(chat_id, ids)

        async def fetch(client):
            from telethon.tl.functions.messages import GetMessagesViewsRequest
            if not self._warmed:   # 사용자 세션은 채널 access_hash 가 필요 → 대화 목록으로 한 번 채움
                try:
                    await client.get_input_entity(to_peer(chat_id))
                except ValueError:
                    await client.get_dialogs(limit=200)
                    self._warmed = True
            entity = await client.get_input_entity(to_peer(chat_id))
            return await client(GetMessagesViewsRequest(peer=entity, id=ids, increment=False))
        res = await self._run(self.user, fetch)
        if res is None:
            return None
        out = {mid: int(v.views or 0) for mid, v in zip(ids, res.views)}
        await self.db.atomic(lambda c: c.executemany(
            "INSERT OR REPLACE INTO mtproto_views(chat_id, msg_id, views, ts) VALUES(?,?,?,?)",
            [(chat_id, mid, v, now) for mid, v in out.items()]))
        return out

    async def _cached_views(self, chat_id: int, ids: list[int]) -> dict[int, int]:
        marks = ",".join("?" * len(ids))
        rows = await self.db._all(f"SELECT msg_id, views FROM mtproto_views WHERE chat_id=? AND msg_id IN ({marks})",
                                  (chat_id, *ids))
        return {r["msg_id"]: r["views"] for r in rows}

    # ── 상태 (오너 화면) ──────────────────────────────────────
    def status(self) -> dict:
        def one(h: Helper) -> dict:
            return {"configured": h.configured, "connected": h.connected and h.client is not None, "me": h.me,
                    "last_error": h.last_error, "last_error_ts": h.last_error_ts, "last_flood": h.last_flood,
                    "flood_until": h.flood_until if h.flood_until > time.time() else 0}
        return {"bot": one(self.bot), "user": one(self.user), "starting": bool(self._task and not self._task.done())}
