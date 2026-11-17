"""🏷 클론봇도 노래 — 음성 담당 하나가 본체 + 클론 봇들의 노래 일감을 같이 받는다 (2026-11-17 오너 '클론도 자이 계정으로 노래').

왜 이렇게: 노래는 도우미(자이) 사람 계정 세션 하나로 음성채팅에 들어감. 같은 세션을 프로세스 두 곳에서 열면 텔레그램이
AUTH_KEY_DUPLICATED 로 세션을 무효로 만들어 버림 (core.telegram.org/api/errors · Telethon FAQ '세션은 한 곳에서만').
그래서 클론마다 음성 담당을 따로 띄우지 않고, 지금 음성 담당(hub) 안에 클론 몫(TenantWorker)을 붙인다.
다른 오픈소스 클론 노래봇(Clonify·KavyaClone)도 같은 구조 — 봇 토큰은 여럿, 도우미 세션·재생 상태는 하나.

TenantWorker = Worker 와 같은 코드인데
- 도우미 계정(client·calls)·DJ(players)·통화 표(in_call·bridges)·잠금은 hub 것을 그대로 같이 씀 (전부 방 번호 키라 안 섞임,
  동시 통화 한도 MAX_CALLS 도 전체 합계로)
- 장부(db)·봇(bot, 클론 봇 이름으로 글)·오너만 그 클론 것 → 클론 방의 대기열·재생 카드·노래 기록은 클론 장부와 클론 봇으로
- 도우미 로그인·로그아웃은 막음 (클론 오너가 자이 계정을 끊으면 본체 노래까지 다 멈춤), AI 음성 대화(start)도 아직 안 함(노래만)
클론 목록 = data/clones/<slug>.env (clone.write_env 가 만듦) — 60초마다 다시 봐서 새 클론은 재시작 없이 붙임.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from . import musicq, store
from .worker import HEALTH_EVERY, REJOIN_EVERY, Worker

log = logging.getLogger("sodam.voice")

SCAN_EVERY = 60
# hub 와 같이 쓰는 것 (방 번호 키 · 동시 통화 한도 · 일감 손잡이)
SHARED = ("ssrc_users", "bridges", "tasks", "jobs", "locks", "players", "ptasks", "_ptasks_all", "_plocks",
          "_mic_win", "_mic_warned", "_swin", "in_call", "_orphan_since", "psessions", "_rejoin_said")
LOGIN_KINDS = {"login_phone", "login_code", "login_pw", "logout"}
VOICE_KINDS = {"start", "stop"}


@dataclass
class Spec:
    slug: str
    token: str
    db_path: str
    owner_ids: frozenset
    name: str


def discover(data_dir: Path) -> dict[str, Spec]:
    """data/clones/*.env → 켤 수 있는 클론 (토큰·DB 가 있고 DB 파일이 이미 만들어진 것 = 클론 봇이 한 번은 켜짐)."""
    from ..clone import _read_env
    out: dict[str, Spec] = {}
    for env in sorted((Path(data_dir) / "clones").glob("*.env")):
        v = _read_env(env)
        token, db_path = v.get("TELEGRAM_BOT_TOKEN", ""), v.get("DB_PATH", "")
        if not token or not db_path or not Path(db_path).is_file():
            continue
        owners = frozenset(int(x) for x in v.get("OWNER_IDS", "").replace(" ", "").split(",") if x.isdecimal())
        out[env.stem] = Spec(env.stem, token, db_path, owners, v.get("BOT_NAME", ""))
    return out


class TenantWorker(Worker):
    """클론 봇 하나 몫 (머리말). hub 의 도우미·DJ 표를 같이 쓰고 장부·봇만 자기 것."""

    def __init__(self, hub: Worker, slug: str, cfg: Any, db: Any, bot: Any):
        self.hub = hub                          # 아래 property 가 hub 를 보므로 super().__init__ 보다 먼저
        super().__init__(cfg, db, client_factory=hub.client_factory, calls_factory=None, realtime_connect=None,
                         media=None, web_search=None, svc=None, bot=bot, engine=hub.engine,
                         music_source=hub.music_source, music_decoder=hub.music_decoder, music_opts=hub.music_opts)
        self.slug = slug
        for name in SHARED:
            setattr(self, name, getattr(hub, name))
        self._assistant_seen: Any = object()    # 클론 장부에 적어 둔 도우미 표시 (바뀔 때만 다시 씀)

    # 도우미 계정·소리 형식은 hub 것 (Worker.__init__ 이 None 을 넣으려 해도 무시 — hub 를 덮으면 안 됨)
    client = property(lambda self: self.hub.client, lambda self, v: None)
    calls = property(lambda self: self.hub.calls, lambda self, v: None)
    media = property(lambda self: self.hub.media, lambda self, v: None)
    frame = property(lambda self: self.hub.frame, lambda self, v: None)

    async def handle(self, job: dict) -> None:
        kind = job["kind"]
        if kind in LOGIN_KINDS:                 # 도우미 연결은 본체 오너 🎙 에서만
            await store.finish(self.db, job["id"], False, "clone_no_login")
            return
        if kind in VOICE_KINDS:                 # AI 음성 대화는 아직 클론 X (노래만 — 오너 결정 '음성 제외')
            await store.finish(self.db, job["id"], False, "clone_voice_off")
            return
        await super().handle(job)

    async def startup(self) -> None:
        """hub 의 run() 앞부분과 같게: 끝 표시 없이 남은 통화·노래 정리 → 재시작으로 끊긴 방은 이어서."""
        orphans = [r["chat_id"] for r in await self.db._all("SELECT DISTINCT chat_id FROM voice_calls WHERE end_ts IS NULL")]
        await store.close_orphans(self.db)
        orphan_music = await musicq.close_orphans(self.db)
        music_chats = await musicq.restart_chats(self.db, int(time.time()) - 1800)
        await self.leave_orphans(sorted(set(orphans) | set(orphan_music)))
        await self.music_resume(music_chats)

    async def tick(self) -> None:
        """hub.step() 이 1초마다 부름: 살아 있음·도우미 표시 → 일감 가져가기 → 닫힌 음성채팅 다시 들어가기."""
        await self.db.set_state(0, store.WORKER_BEAT, time.time())
        a = await self.hub.db.get_state(0, store.ASSISTANT_KEY) if self.hub.client else None
        if a != self._assistant_seen:           # 클론 봇은 자기 장부의 이 표시로 '도우미 연결됨' 을 봄
            await self.db.set_state(0, store.ASSISTANT_KEY, a)
            self._assistant_seen = a
        for job in await store.take_jobs(self.db):
            t = asyncio.create_task(self._run_job(job))
            self.jobs.add(t)
            t.add_done_callback(self.jobs.discard)
        if self.client and time.monotonic() - self._rejoin_at >= REJOIN_EVERY:
            self._rejoin_at = time.monotonic()
            try:
                await self._music_rejoin()
            except Exception as e:
                log.warning("클론 %s 노래 다시 들어가기 확인 실패: %s", self.slug, e)
        if time.monotonic() - self._health_at > HEALTH_EVERY:
            self._health_at = time.monotonic()
            try:
                await store.purge_lines(self.db)
            except Exception as e:
                log.warning("클론 %s 음성 기록 정리 실패: %s", self.slug, e)


Opener = Callable[[Spec], Awaitable[tuple[Any, Any, Any]]]      # spec → (cfg, db, bot)


async def open_tenant(spec: Spec, tz=None) -> tuple[Any, Any, Any]:
    """실제 서버: 클론 장부를 열고 클론 봇(글 보내기만, 폴링 없음)을 준비. 봇 글의 '소담' 은 클론 이름으로."""
    from telegram import Bot

    from .. import brandname
    from ..db import DB
    db = DB(spec.db_path)
    await db.open()
    bot = Bot(spec.token)
    await bot.initialize()
    brandname.set_bot_name(bot, spec.name)
    cfg = SimpleNamespace(db_path=spec.db_path, tz=tz, owner_ids=spec.owner_ids, telegram_token=spec.token,
                          mtproto_api_id=0, mtproto_api_hash="")
    return cfg, db, bot


class Tenants:
    """hub 에 붙는 클론 목록. 시작 때 한 번 + SCAN_EVERY 마다 새 클론을 찾아 붙임 (지워진 클론은 다음 재시작에 빠짐)."""

    def __init__(self, hub: Worker, data_dir: Path, opener: Opener, *, discover_fn: Callable = discover,
                 scan_every: float = SCAN_EVERY):
        self.hub, self.data_dir, self.opener, self.discover_fn = hub, Path(data_dir), opener, discover_fn
        self.scan_every = scan_every
        self.workers: dict[str, TenantWorker] = {}
        self._dbs: list = []
        self._scan_at = 0.0
        self._started = False

    async def _scan(self) -> None:
        self._scan_at = time.monotonic()
        hub_db = str(Path(getattr(self.hub.cfg, "db_path", "")).resolve())
        for slug, spec in self.discover_fn(self.data_dir).items():
            if slug in self.workers or str(Path(spec.db_path).resolve()) == hub_db:
                continue
            try:
                cfg, db, bot = await self.opener(spec)
            except Exception as e:
                log.warning("클론 %s 노래 연결 실패 (다음에 다시): %r", slug, e)
                continue
            self._dbs.append(db)
            tw = TenantWorker(self.hub, slug, cfg, db, bot)
            self.workers[slug] = tw
            if self._started:
                try:
                    await tw.startup()
                except Exception as e:
                    log.warning("클론 %s 노래 시작 정리 실패: %r", slug, e)
            log.info("🏷 클론 %s 노래도 맡음 (%s)", slug, spec.name or "-")

    async def start(self) -> None:
        await self._scan()
        for slug, tw in self.workers.items():
            try:
                await tw.startup()
            except Exception as e:
                log.warning("클론 %s 노래 시작 정리 실패: %r", slug, e)
        self._started = True

    async def tick(self) -> None:
        if time.monotonic() - self._scan_at >= self.scan_every:
            await self._scan()
        for slug, tw in list(self.workers.items()):
            try:
                await tw.tick()
            except Exception as e:
                log.warning("클론 %s 노래 일감 처리 오류: %s", slug, e)

    async def close(self) -> None:
        """종료: hub.shutdown() 이 DJ 를 다 멈춘 뒤에 장부를 닫음."""
        for db in self._dbs:
            try:
                await db.close()
            except Exception:
                pass
