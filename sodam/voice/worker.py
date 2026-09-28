"""📞 통화 담당 프로세스: python -m sodam.voice.worker (systemd sodam-voice).

어시스턴트(사람) 계정 1개 = Telethon 세션(data/voice_assistant.session, 0600) + py-tgcalls.
봇(소담 본체)과는 DB 의 voice_jobs 로만 이야기함 → ntgcalls(네이티브) 가 죽어도 본체는 그대로.

흐름 (오픈소스 음악봇 YukkiMusicBot 의 '어시스턴트 계정' 구조를 참고해 새로 씀):
  join  : 봇이 만든 1회용 초대링크로 방에 들어감 (이미 멤버면 통과)
  start : play(외부 오디오 48k 모노, auto_start=True → 음성채팅이 없고 어시스턴트가 '음성채팅 관리' 권한이 있으면 새로 엶)
          + record(외부 오디오) → 들어온 10 ms 조각을 Bridge.feed, Bridge 가 만든 소리를 send_frame
  stop  : Bridge.stop → leave_call
  login_phone / login_code / login_pw / logout : 봇 🎙 화면에서 오너가 넣은 값으로 로그인 (값은 처리 즉시 DB 에서 지움)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from .. import mtproto
from . import store
from .bridge import Bridge
from .video import FPS as VFPS, H as VH, W as VW, to_i420

log = logging.getLogger("sodam.voice")

MODEL = os.getenv("VOICE_MODEL", "gpt-realtime-2.1-mini")
POLL = 1.0
VIDEO = os.getenv("VOICE_VIDEO", "1") != "0"             # 영상 칸에 도우미 계정 프사 (끄기: VOICE_VIDEO=0)
MAX_CALLS = int(os.getenv("VOICE_MAX_CALLS", "3"))      # 동시에 여는 통화 (2 vCPU 서버)
INVITE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/)([\w-]+)")


def session_path(cfg) -> Path:
    return Path(cfg.db_path).parent / "voice_assistant.session"


def api_path(cfg) -> Path:
    """도우미 계정 전용 api_id/api_hash (my.telegram.org 에서 그 계정으로 발급, 0600). 없으면 .env MTPROTO_API_ID/HASH."""
    return Path(cfg.db_path).parent / "voice_assistant.api"


def read_api(cfg) -> tuple[int, str]:
    raw = mtproto.read_session(api_path(cfg))
    if raw and ":" in raw:
        i, h = raw.split(":", 1)
        if i.isdecimal() and h:
            return int(i), h
    return int(cfg.mtproto_api_id or 0), cfg.mtproto_api_hash or ""


def user_client(session: str, api_id: int, api_hash: str):
    """어시스턴트 계정 클라이언트 — 업데이트를 받아야 py-tgcalls 가 통화 신호를 받음 (봇 세션과 별개 계정이라 충돌 없음)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or None), api_id, api_hash, device_model="Sodam Voice",
                          system_version="Linux", app_version="1.0")


class Worker:
    def __init__(self, cfg, db, *, client_factory: Callable | None = None, calls_factory: Callable | None = None,
                 realtime_connect: Callable | None = None, media: Any = None):
        self.cfg, self.db = cfg, db
        self.client_factory = client_factory or user_client
        self.calls_factory = calls_factory
        self.realtime_connect = realtime_connect
        self.media = media                      # py-tgcalls 형식 모음 (테스트는 가짜)
        self.client = None                      # 로그인된 어시스턴트
        self.calls = None                       # PyTgCalls
        self.pending = None                     # 로그인 중 (client, phone, phone_code_hash)
        self.frame: bytes | None = None         # 영상 칸 I420 한 장 (로그인 때 1번 변환)
        self.bridges: dict[int, Bridge] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    # ── 로그인 ──────────────────────────────────────────
    async def resume(self) -> None:
        s = mtproto.read_session(session_path(self.cfg))
        if not s:
            return
        client = self.client_factory(s, *read_api(self.cfg))
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("어시스턴트 세션이 풀렸어요 — 🎙 화면에서 다시 로그인")
            await self.db.set_state(0, store.ASSISTANT_KEY, None)
            await client.disconnect()
            return
        await self._ready(client)

    async def _ready(self, client) -> str:
        self.client = client
        mtproto.write_session(session_path(self.cfg), client.session.save())
        me = await client.get_me()
        name = " ".join(x for x in (me.first_name, me.last_name) if x) or "assistant"
        await self.db.set_state(0, store.ASSISTANT_KEY, {"id": me.id, "name": name, "username": me.username})
        self.frame = None
        if VIDEO and self.media is not None:
            try:
                photo = await client.download_profile_photo("me", file=bytes)
            except Exception:
                photo = None
            self.frame = await asyncio.to_thread(to_i420, photo)      # 한 번만 변환 (통화마다 같은 bytes 재사용)
        if self.calls_factory:
            self.calls = self.calls_factory(client)
            await self.calls.start()
            self._hook_frames()
        return f"ok:{name}"

    async def _login_phone(self, p: dict) -> tuple[bool, str]:
        if str(p.get("api_id") or "").isdecimal() and p.get("api_hash"):
            mtproto.write_session(api_path(self.cfg), f"{int(p['api_id'])}:{p['api_hash']}")
        api_id, api_hash = read_api(self.cfg)
        if not (api_id and api_hash):
            return False, "no_api_id"
        client = self.client_factory("", api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(p["phone"])
        self.pending = (client, p["phone"], sent.phone_code_hash)
        return True, "code_sent"

    async def _login_code(self, p: dict) -> tuple[bool, str]:
        if not self.pending:
            return False, "no_pending"
        client, phone, h = self.pending
        try:
            await client.sign_in(phone, code=p["code"], phone_code_hash=h)
        except Exception as e:
            if type(e).__name__ == "SessionPasswordNeededError":
                return True, "need_password"
            raise
        self.pending = None
        return True, await self._ready(client)

    async def _login_pw(self, p: dict) -> tuple[bool, str]:
        if not self.pending:
            return False, "no_pending"
        client = self.pending[0]
        await client.sign_in(password=p["password"])
        self.pending = None
        return True, await self._ready(client)

    async def _logout(self, p: dict) -> tuple[bool, str]:
        for chat_id in list(self.bridges):
            await self._stop(chat_id, "logout")
        if self.client:
            try:
                await self.client.log_out()
            except Exception as e:
                log.warning("로그아웃: %s", e)
        self.client = self.calls = None
        session_path(self.cfg).unlink(missing_ok=True)
        await self.db.set_state(0, store.ASSISTANT_KEY, None)
        return True, "logged_out"

    # ── 방 들어가기 · 통화 ─────────────────────────────────
    async def _join(self, p: dict) -> tuple[bool, str]:
        if not self.client:
            return False, "no_assistant"
        m = INVITE.search(p.get("link") or "")
        if not m:
            return False, "bad_link"
        from telethon.tl.functions.messages import ImportChatInviteRequest
        try:
            await self.client(ImportChatInviteRequest(m.group(1)))
        except Exception as e:
            if type(e).__name__ != "UserAlreadyParticipantError":
                raise
        return True, "joined"

    async def _start(self, chat_id: int, p: dict) -> tuple[bool, str]:
        if not self.client or not self.calls:
            return False, "no_assistant"
        if chat_id in self.bridges:
            return True, "already"
        if len(self.bridges) >= MAX_CALLS:
            return False, "busy"
        if p.get("link"):
            await self._join(p)
        md = self.media
        bridge = Bridge(lambda: self.realtime_connect(MODEL),
                        lambda f: self.calls.send_frame(chat_id, md.Device.MICROPHONE, f),
                        instructions=p.get("instructions") or "", voice=p.get("voice") or "marin",
                        reply=p.get("reply") or "all", greet=p.get("greet"),
                        max_sec=float(p.get("max_sec") or 900), idle_sec=float(p.get("idle_sec") or 60))
        params = md.AudioParameters(48000, 1)
        video = self.frame is not None
        try:
            if video:
                try:
                    await self.calls.play(chat_id, md.MediaStream(md.ExternalMedia.AUDIO | md.ExternalMedia.VIDEO, params,
                                                                  md.VideoParameters(VW, VH, VFPS)),
                                          md.GroupCallConfig(auto_start=True))
                except Exception as e:
                    if type(e).__name__ in ("NoActiveGroupCall", "ChatAdminRequired", "UserBannedInChannel"):
                        raise
                    log.warning("영상 칸 없이 소리만으로 다시 (%s)", e)
                    video = False
            if not video:
                await self.calls.play(chat_id, md.MediaStream(md.ExternalMedia.AUDIO, params), md.GroupCallConfig(auto_start=True))
            await self.calls.record(chat_id, md.RecordStream(True, params))
        except Exception as e:
            name = type(e).__name__
            await self._leave(chat_id)
            return False, {"NoActiveGroupCall": "no_voice_chat", "ChatAdminRequired": "no_voice_right",
                           "UserBannedInChannel": "banned"}.get(name, f"error:{name}")
        self.bridges[chat_id] = bridge
        call_id = await store.call_started(self.db, chat_id, p.get("by"))
        self.tasks[chat_id] = asyncio.create_task(self._run_call(chat_id, call_id, bridge, video))
        return True, "started"

    async def _video_loop(self, chat_id: int, bridge: Bridge) -> None:
        """1초에 한 번 같은 사진 (새 bytes 안 만듦). 통화가 끝나면 _run_call 이 취소."""
        md, frame = self.media, self.frame
        info = md.Frame.Info(width=VW, height=VH)
        while not bridge.done:
            try:
                await self.calls.send_frame(chat_id, md.Device.CAMERA, frame, info)
            except Exception as e:
                log.debug("영상 칸 전송 실패 (소리는 계속): %s", e)
                return
            await asyncio.sleep(1 / VFPS)

    async def _run_call(self, chat_id: int, call_id: int, bridge: Bridge, video: bool = False) -> None:
        vt = asyncio.create_task(self._video_loop(chat_id, bridge)) if video else None
        try:
            res = await bridge.run()
        finally:
            if vt:
                vt.cancel()
            self.bridges.pop(chat_id, None)
            self.tasks.pop(chat_id, None)
            await self._leave(chat_id)
        await store.call_ended(self.db, call_id, res.seconds, res.reason, res.user_turns, res.bot_turns)
        await store.record_cost(self.db, getattr(self.cfg, "tz", None), chat_id, res.seconds)
        log.info("통화 끝 %s %.0f초 %s", chat_id, res.seconds, res.reason)

    async def _leave(self, chat_id: int) -> None:
        try:
            await self.calls.leave_call(chat_id)
        except Exception as e:
            log.debug("leave_call: %s", e)

    async def _stop(self, chat_id: int, reason: str = "admin") -> tuple[bool, str]:
        b = self.bridges.get(chat_id)
        if not b:
            return True, "not_in_call"
        b.stop(reason)
        t = self.tasks.get(chat_id)
        if t:
            await asyncio.wait({t}, timeout=10)
        return True, "stopped"

    def _hook_frames(self) -> None:
        md = self.media
        if md is None or not hasattr(self.calls, "on_update"):
            return

        @self.calls.on_update(md.filters.stream_frame(md.Direction.INCOMING, md.Device.MICROPHONE))
        async def _frames(_, update):
            b = self.bridges.get(update.chat_id)
            if b:
                b.feed([f.frame for f in update.frames])

        @self.calls.on_update(md.filters.chat_update(md.ChatUpdate.Status.LEFT_CALL | md.ChatUpdate.Status.KICKED
                                                     | md.ChatUpdate.Status.CLOSED_VOICE_CHAT))
        async def _gone(_, update):
            b = self.bridges.get(update.chat_id)
            if b:
                b.stop("closed")

    # ── 일 처리 ────────────────────────────────────────
    async def handle(self, job: dict) -> None:
        kind, p, chat_id = job["kind"], job["payload"], job["chat_id"]
        p.setdefault("by", job.get("by"))
        try:
            if kind == "login_phone":
                ok, res = await self._login_phone(p)
            elif kind == "login_code":
                ok, res = await self._login_code(p)
            elif kind == "login_pw":
                ok, res = await self._login_pw(p)
            elif kind == "logout":
                ok, res = await self._logout(p)
            elif kind == "join":
                ok, res = await self._join(p)
            elif kind == "start":
                ok, res = await self._start(chat_id, p)
            elif kind == "stop":
                ok, res = await self._stop(chat_id)
            else:
                ok, res = False, "unknown"
        except Exception as e:
            log.warning("음성 일 %s 실패: %s", kind, e)
            ok, res = False, f"error:{type(e).__name__}"
        await store.finish(self.db, job["id"], ok, res)

    async def step(self) -> None:
        await self.db.set_state(0, store.WORKER_BEAT, time.time())
        for job in await store.take_jobs(self.db):
            await self.handle(job)

    async def run(self) -> None:
        await store.close_orphans(self.db)
        try:
            await self.resume()
        except Exception as e:
            log.warning("어시스턴트 세션 열기 실패: %s", e)
        log.info("📞 음성 담당 시작 (어시스턴트 %s)", "있음" if self.client else "없음")
        while True:
            try:
                await self.step()
            except Exception as e:
                log.warning("음성 담당 반복 오류: %s", e)
            await asyncio.sleep(POLL)


def _media():
    """py-tgcalls 형식들 — 이 프로세스에서만 import."""
    from types import SimpleNamespace

    from pytgcalls import filters
    from pytgcalls.types import (ChatUpdate, Device, Direction, ExternalMedia, GroupCallConfig, MediaStream,
                                 RecordStream)
    from pytgcalls.types import Frame
    from pytgcalls.types.raw import AudioParameters, VideoParameters
    return SimpleNamespace(filters=filters, ChatUpdate=ChatUpdate, Device=Device, Direction=Direction,
                           ExternalMedia=ExternalMedia, GroupCallConfig=GroupCallConfig, MediaStream=MediaStream,
                           RecordStream=RecordStream, AudioParameters=AudioParameters, VideoParameters=VideoParameters,
                           Frame=Frame)


async def main() -> None:
    from openai import AsyncOpenAI
    from pytgcalls import PyTgCalls

    from ..config import load_config
    from ..db import DB
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()
    db = DB(cfg.db_path)
    await db.open()
    oai = AsyncOpenAI(api_key=cfg.openai_api_key)
    w = Worker(cfg, db, calls_factory=PyTgCalls, realtime_connect=lambda model: oai.realtime.connect(model=model),
               media=_media())
    await w.run()


if __name__ == "__main__":
    asyncio.run(main())
