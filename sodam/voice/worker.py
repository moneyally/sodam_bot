"""📞 통화 담당 프로세스: python -m sodam.voice.worker (systemd sodam-voice).

어시스턴트(사람) 계정 1개 = Telethon 세션(data/voice_assistant.session, 0600) + py-tgcalls.
봇(소담 본체)과는 DB 의 voice_jobs 로만 이야기함 → ntgcalls(네이티브) 가 죽어도 본체는 그대로.

흐름 ('어시스턴트 계정' 구조):
  join  : 봇이 만든 1회용 초대링크로 방에 들어감 (이미 멤버면 통과)
  start : play(외부 오디오 48k 모노, auto_start=True → 음성채팅이 없고 어시스턴트가 '음성채팅 관리' 권한이 있으면 새로 엶)
          + record(외부 오디오) → 들어온 10 ms 조각을 Bridge.feed, Bridge 가 만든 소리를 send_frame
  stop  : Bridge.stop → leave_call
  login_phone / login_code / login_pw / logout : 봇 🎙 화면에서 오너가 넣은 값으로 로그인 (값은 처리 즉시 DB 에서 지움)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .. import mtproto
from . import music, musicq, store
from .bridge import Bridge
from .live import BACKEND_MODEL, LIVE_MODEL, LiveBridge
from .video import FPS as VFPS, H as VH, W as VW, to_i420

log = logging.getLogger("sodam.voice")

MODEL = os.getenv("VOICE_MODEL", "gpt-realtime-2.1-mini")
# 음성 엔진: realtime(기본, bridge.py) | live(gpt-live-1, live.py — docs/VOICE_LIVE.md). 되돌리기 = realtime 으로 바꾸고 재시작
ENGINE = os.getenv("VOICE_ENGINE", "realtime").strip().lower()
LIVE = os.getenv("VOICE_LIVE_MODEL", LIVE_MODEL)
BACKEND = os.getenv("VOICE_BACKEND_MODEL", BACKEND_MODEL)
POLL = 1.0
VIDEO = os.getenv("VOICE_VIDEO", "0") == "1"             # 기본 소리만 (오너 결정 2026-09-29). 영상 칸에 프사: VOICE_VIDEO=1
MAX_CALLS = int(os.getenv("VOICE_MAX_CALLS", "3"))      # 동시에 여는 통화 (2 vCPU 서버)
INVITE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/)([\w-]+)")


# 텔레그램 오류 이름(Telethon: …Error) → 방에 보일 결과 코드 (감사 2026-09-29: 예전엔 'Error' 없는 이름이라 안 맞았음)
ERR_CODE = {"ChatAdminRequiredError": "no_voice_right", "GroupcallForbiddenError": "no_voice_right",
            "UserBannedInChannelError": "banned", "ChannelPrivateError": "not_member", "ChatForbiddenError": "not_member",
            "FloodWaitError": "flood", "NoActiveGroupCall": "no_voice_chat", "ValueError": "no_peer",
            "TimeoutError": "slow", "InviteHashExpiredError": "bad_link", "InviteHashInvalidError": "bad_link",
            "InviteRequestSentError": "join_request", "ChannelsTooMuchError": "too_many_chats"}
FATAL = {"ChatAdminRequiredError", "GroupcallForbiddenError", "UserBannedInChannelError", "ChannelPrivateError",
         "ChatForbiddenError", "FloodWaitError", "ValueError", "TimeoutError"}   # 영상 빼고 다시 해도 소용없는 것
PLAY_TIMEOUT = 30          # play() 가 연결을 무한정 기다리지 않게
HEALTH_EVERY = 300         # 도우미 세션 살아 있는지 (세션 폐기·연결 끊김 알아채기)


def err_code(e: BaseException) -> str:
    return ERR_CODE.get(type(e).__name__, f"error:{type(e).__name__}")


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
                          system_version="Linux", app_version="1.0",
                          flood_sleep_threshold=10)   # 긴 FloodWait 동안 조용히 멈추지 않고 오류로 (다른 방 일이 안 막히게)


MIC_OVER = 120                  # 1초에 이보다 많이 보내면 두 군데서 보내는 것 (정상 100)
OWNER_ALERT_SEC = 6 * 3600       # 🎵 음원 막힘 오너 알림 간격
# 🎵 끊김 감시: 1분마다 방마다 숫자를 보고, 최근 5분에 이만큼 넘으면 오너 1:1 (방마다 1시간에 1번)
STUTTER_EVERY = 60
STUTTER_WIN = 300
STUTTER_ALERT_SEC = 3600
VC_TITLE_TIMEOUT = 15.0     # 음성채팅 제목 바꾸기 — 텔레그램 답 기다리는 최대 초
STUTTER_LIMITS = {"jitter": 15, "late": 5, "underrun": 50, "send_slow": 30}
STUTTER_NAMES = {"jitter": "30ms 넘게 밀림", "late": "박자 다시 맞춤", "underrun": "소리 조각 빔", "send_slow": "보내기 20ms 넘게 걸림"}
STUTTER_KEY = "music_stutter_alert"
REJOIN_EVERY = 15                # 🎵 음성채팅이 닫혀 멈춘 방: 다시 열렸는지 확인 간격
# 🎵 새벽에 인기곡 미리 받기: 한국시각 4시대, 노래 트는 방이 없을 때만, 하루 한 번, 곡 사이 쉬면서
PREFETCH_HOUR = 4
PREFETCH_MAX = 20
PREFETCH_GAP = 20
PREFETCH_KEY = "music_prefetch_day"


class Worker:
    def __init__(self, cfg, db, *, client_factory: Callable | None = None, calls_factory: Callable | None = None,
                 realtime_connect: Callable | None = None, media: Any = None,
                 web_search: Callable | None = None, svc: Any = None, bot: Any = None, engine: str | None = None,
                 music_source: Any = None, music_decoder: Callable | None = None, music_opts: dict | None = None):
        self.cfg, self.db = cfg, db
        self.engine = (engine or ENGINE) if (engine or ENGINE) in ("realtime", "live") else "realtime"
        self.model = LIVE if self.engine == "live" else MODEL
        if self.engine == "live" and not self.model.startswith("gpt-live"):   # main 의 연결은 이름으로 live/realtime 을 고름
            log.warning("VOICE_ENGINE=live 인데 VOICE_LIVE_MODEL=%s — Live 모델이 아니라 realtime 으로", self.model)
            self.engine, self.model = "realtime", MODEL
        self.client_factory = client_factory or user_client
        self.calls_factory = calls_factory
        self.realtime_connect = realtime_connect
        self.media = media                      # py-tgcalls 형식 모음 (테스트는 가짜)
        self.web_search = web_search            # async (query, chat_id) -> 요약 글 (채팅 소담과 같은 격리 검색·같은 예산)
        self.svc, self.bot = svc, bot           # 채팅 소담 도구용 (toolset.py) — 없으면 검색만
        self.ssrc_users: dict[int, dict[int, int]] = {}   # 방 → 소리 번호(ssrc) → 계정 (말한 사람 확인)
        self.client = None                      # 로그인된 어시스턴트
        self.calls = None                       # PyTgCalls
        self.pending = None                     # 로그인 중 (client, phone, phone_code_hash)
        self.frame: bytes | None = None         # 영상 칸 I420 한 장 (로그인 때 1번 변환)
        self.bridges: dict[int, Bridge] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.jobs: set[asyncio.Task] = set()   # 일감마다 따로 (한 방 입장이 느려도 다른 방·로그인이 안 막힘)
        self.locks: dict[int, asyncio.Lock] = {}
        self._health_at = 0.0
        # 🎵 뮤직봇 (voice/music.py): 방마다 Player 하나. 통화(소리 줄)는 AI 대화·노래가 같이 씀 → in_call
        if music_source is None:                    # 찾기·받기는 다른 프로그램에서 (소리 박자가 안 막히게 — voice/fetcher.py 머리말)
            data_dir = Path(getattr(cfg, "db_path", "data/sodam.db")).parent
            if os.getenv("MUSIC_PROC", "1") != "0":
                from .fetcher import ProcSource
                music_source = ProcSource(data_dir)
            else:
                music_source = music.Source(data_dir)
        self.music_source = music_source
        self.music_decoder = music_decoder or music.Decoder
        self.music_opts = music_opts or {}          # 테스트: 가짜 시계·짧은 대기
        self.players: dict[int, music.Player] = {}
        self.ptasks: dict[int, asyncio.Task] = {}
        self._ptasks_all: set[asyncio.Task] = set()
        self._plocks: dict[int, asyncio.Lock] = {}
        self._mic_win: dict[int, list] = {}         # 방마다 [1초 창 시작, 조각 수, 보낸 곳]
        self._mic_warned: dict[int, float] = {}
        self._swin: dict[int, deque] = {}           # 끊김 감시: 방마다 [(시각, 숫자, 어느 DJ)]
        self._watch_at = 0.0
        self._rejoin_at = 0.0
        self._prefetch_task: asyncio.Task | None = None
        self.psessions: dict[int, int] = {}
        self.in_call: set[int] = set()

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
        for chat_id in list(self.bridges):          # 다시 로그인 = 옛 통화는 정리하고 새 클라이언트로
            await self._stop(chat_id, "logout")
        self.client = client
        mtproto.write_session(session_path(self.cfg), client.session.save())
        me = await client.get_me()
        try:
            await client.get_dialogs(limit=200)   # 속한 방을 세션에 기억 (막 로그인한 세션은 -100… ID 로 방을 못 찾음)
        except Exception as e:
            log.warning("방 목록 읽기 실패: %s", e)
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
        await self._drop_pending()
        client = self.client_factory("", api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(p["phone"])
        self.pending = (client, p["phone"], sent.phone_code_hash)
        return True, "code_sent"

    async def _drop_pending(self) -> None:
        """중간에 그만둔 로그인 클라이언트는 끊음 (연결이 계속 남던 것)."""
        if self.pending:
            old, self.pending = self.pending[0], None
            try:
                await old.disconnect()
            except Exception:
                pass

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
        await self._stop_music("logout")
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
        if p.get("username"):                        # 공개 방: 아이디로 (봇 초대 권한 없어도 됨)
            from telethon.tl.functions.channels import JoinChannelRequest
            await self.client(JoinChannelRequest(p["username"]))
            return True, "joined"
        m = INVITE.search(p.get("link") or "")
        if not m:
            return False, "bad_link"
        from telethon.tl.functions.messages import ImportChatInviteRequest
        try:
            r = await self.client(ImportChatInviteRequest(m.group(1)))
        except Exception as e:
            if type(e).__name__ != "UserAlreadyParticipantError":
                raise
            return True, "joined"
        if r is not None and type(r).__name__ == "ChatInviteJoinResultWebView":   # 들어간 게 아님 (Telethon 1.45)
            return False, "bad_link"
        return True, "joined"

    async def _start(self, chat_id: int, p: dict) -> tuple[bool, str]:
        if not self.client or not self.calls:
            return False, "no_assistant"
        if chat_id in self.bridges:
            return True, "already"
        if chat_id not in self.in_call and len(self.in_call | set(self.bridges)) >= MAX_CALLS:
            return False, "busy"
        if p.get("link"):
            await self._join(p)
        md = self.media
        cls, extra = (LiveBridge, {"model": self.model, "backend": BACKEND}) if self.engine == "live" else (Bridge, {})
        bridge = cls(lambda: self.realtime_connect(self.model),
                        lambda f: self._send(chat_id, f),
                        instructions=p.get("instructions") or "", voice=p.get("voice") or "marin",
                        reply=p.get("reply") or "all", greet=p.get("greet"),
                        transcribe_prompt=str(p.get("transcribe_prompt") or ""),
                        max_sec=float(p.get("max_sec") or 900), idle_sec=float(p.get("idle_sec") or 60),
                        **extra, **(await self._toolset(chat_id, p.get("by") or 0)))
        params = md.AudioParameters(48000, 1)
        video = self.frame is not None and chat_id not in self.in_call   # 노래 중이면 이미 소리 줄이 열려 있음
        try:
            if video:
                try:
                    await asyncio.wait_for(self.calls.play(
                        chat_id, md.MediaStream(md.ExternalMedia.AUDIO | md.ExternalMedia.VIDEO, params,
                                                md.VideoParameters(VW, VH, VFPS)),
                        md.GroupCallConfig(auto_start=True)), PLAY_TIMEOUT)
                except Exception as e:
                    if type(e).__name__ in FATAL:
                        raise
                    log.warning("영상 칸 없이 소리만으로 다시 (%s)", e)
                    video = False
            if not video and chat_id not in self.in_call:     # 노래 중이면 이미 열린 소리 줄을 같이 씀
                await asyncio.wait_for(self.calls.play(chat_id, md.MediaStream(md.ExternalMedia.AUDIO, params),
                                                       md.GroupCallConfig(auto_start=True)), PLAY_TIMEOUT)
            self.in_call.add(chat_id)
            await self.calls.record(chat_id, md.RecordStream(True, params))
            await self._load_participants(chat_id)
        except Exception as e:
            await self._maybe_leave(chat_id)
            log.warning("통화 시작 실패 %s: %r", chat_id, e)
            return False, err_code(e)
        try:
            call_id = await store.call_started(self.db, chat_id, p.get("by"))   # 기록 먼저 → 실패해도 dict 에 안 남음
        except Exception:
            await self._maybe_leave(chat_id)
            raise
        bridge.on_line = self._recorder(chat_id, call_id, bridge)
        self.bridges[chat_id] = bridge
        self.tasks[chat_id] = asyncio.create_task(self._run_call(chat_id, call_id, bridge, video))
        return True, "started"

    def _recorder(self, chat_id: int, call_id: int, bridge: Any = None):
        """통화 대화 → voice_lines (7일, 오너만 봄). 말한 사람은 소리 번호 → 계정 표로.
        처음 말한 사람이 확인되면 그 사람 이름·채팅 기억을 모델에 한 번 알려 줌 (voice/context.speaker_note)."""
        users = self.ssrc_users.setdefault(chat_id, {})
        bg = self.__dict__.setdefault("_line_tasks", set())
        noted: set[int] = set()

        def spawn(coro) -> None:
            t = asyncio.create_task(coro)
            bg.add(t)
            t.add_done_callback(bg.discard)

        def record(who: str, text: str, ssrc: int | None) -> None:
            uid = users.get(ssrc) if ssrc is not None else None
            spawn(store.add_line(self.db, call_id, chat_id, who, uid, text))
            if who == "user" and uid and uid not in noted and bridge is not None and len(noted) < 30:
                noted.add(uid)
                spawn(self._note_speaker(bridge, chat_id, uid))
        return record

    async def _note_speaker(self, bridge: Any, chat_id: int, uid: int) -> None:
        from . import context
        try:
            note = await context.speaker_note(self.db, chat_id, uid, await self.db.get_settings(chat_id))
            await bridge.note(note)
        except Exception as e:
            log.info("말한 사람 맥락 실패 (통화는 계속) %s/%s: %r", chat_id, uid, e)

    async def _toolset(self, chat_id: int, starter: int) -> dict:
        """Bridge 에 줄 도구: 채팅 소담 읽기 전용 도구 + 웹 검색 (toolset.py 방어 규칙)."""
        search = self._tools(chat_id).get("web_search")
        if self.svc is None:
            return {"tools": {"web_search": search} if search else {}}
        from . import toolset
        settings = await self.db.get_settings(chat_id)
        users = self.ssrc_users.setdefault(chat_id, {})
        specs, handlers = toolset.build(self.svc, self.bot, chat_id, starter, settings, web_search=search,
                                        speaker=lambda ssrc: users.get(ssrc) if ssrc is not None else None)
        return {"tools": handlers, "tool_specs": specs}

    def _tools(self, chat_id: int) -> dict:
        if not self.web_search:
            return {}

        async def web_search(args: dict) -> str:
            q = str(args.get("query") or "").strip()[:200]
            if not q:
                return "검색어 없음"
            from datetime import datetime       # '오늘' 기준이 한국 날짜가 되게 (실측: 새벽에 전날 날씨를 가져옴)
            now = datetime.now(getattr(self.cfg, "tz", None))
            return await self.web_search(f"{q} (기준: 한국 시각 {now:%Y-%m-%d %H시})", chat_id)
        return {"web_search": web_search}

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

    async def _load_participants(self, chat_id: int) -> None:
        """지금 음성채팅 참가자의 소리 번호(source=ssrc) → 계정. 이후 들어오는 사람은 call_participant 이벤트로."""
        users = self.ssrc_users.setdefault(chat_id, {})
        try:
            for part in await self.calls.get_participants(chat_id):
                if getattr(part, "source", None):
                    users[part.source] = part.user_id
        except Exception as e:
            log.debug("참가자 목록 %s: %s", chat_id, e)

    async def _run_call(self, chat_id: int, call_id: int, bridge: Bridge, video: bool = False) -> None:
        vt = asyncio.create_task(self._video_loop(chat_id, bridge)) if video else None
        try:
            res = await bridge.run()
        finally:
            if vt:
                vt.cancel()
            self.bridges.pop(chat_id, None)
            self.tasks.pop(chat_id, None)
            self.ssrc_users.pop(chat_id, None)
            await self._maybe_leave(chat_id)          # 노래가 남아 있으면 음성채팅엔 그대로
        await store.call_ended(self.db, call_id, res.seconds, res.reason, res.user_turns, res.bot_turns, res.stats)
        await store.record_cost(self.db, getattr(self.cfg, "tz", None), chat_id, res.seconds, self.model, res.usage)
        if self.svc is not None:                       # 통화에서 한 자기 얘기 → 채팅과 같은 멤버 기억
            await asyncio.gather(*self.__dict__.get("_line_tasks", ()), return_exceptions=True)   # 줄 저장 먼저
            from . import context
            try:
                n = await context.remember_call(self.svc, chat_id, call_id)
                if n:
                    log.info("통화 기억 정리 %s: %s명", chat_id, n)
            except Exception as e:
                log.warning("통화 기억 정리 실패 %s: %r", chat_id, e)
        st = res.stats or {}
        log.info("통화 끝 %s %.0f초 %s · 늦은 재생 %s번(최대 %sms) · 루프 지연 최대 %sms · 끼어들기 %s · 오류 %s · 재연결 %s"
                 " · 말 감지 %s → 받아씀 %s (빈 받아쓰기 %s)",
                 chat_id, res.seconds, res.reason, st.get("late_ticks"), st.get("max_late_ms"), st.get("loop_lag_max_ms"),
                 st.get("interrupts"), st.get("rt_errors"), st.get("reconnects"),
                 st.get("speech_events"), res.user_turns, st.get("empty_transcripts"))

    async def _leave(self, chat_id: int) -> None:
        self.in_call.discard(chat_id)
        try:
            await self.calls.leave_call(chat_id)
        except Exception as e:
            log.debug("leave_call: %s", e)

    async def _maybe_leave(self, chat_id: int) -> None:
        """AI 대화도 노래도 없을 때만 음성채팅에서 나감 (둘이 소리 줄 하나를 같이 씀)."""
        if chat_id not in self.bridges and chat_id not in self.players:
            await self._leave(chat_id)

    async def _send(self, chat_id: int, frame: bytes) -> None:
        """소담 AI 목소리 한 조각: 노래 중이면 DJ(Player)가 섞어서 보내고, 아니면 바로."""
        pl = self.players.get(chat_id)
        if pl is not None and not pl.done:
            pl.voice_frame(frame)
            return
        await self._mic(chat_id, frame, "voice")

    async def _mic(self, chat_id: int, frame: bytes, who: str) -> None:
        """소리 줄로 보내는 곳은 여기 하나. 1초에 조각이 MIC_OVER(정상 100)보다 많으면 두 군데서 보내는 것 → 기록
        (2026-10-09 '다시 켜니 2배속' — 노래 DJ·AI 목소리가 따로 보내면 정확히 2배)."""
        now = time.monotonic()
        w = self._mic_win.get(chat_id)
        if w is None or now - w[0] >= 1.0:
            if w is not None and w[1] > MIC_OVER and now - self._mic_warned.get(chat_id, 0) > 60:
                self._mic_warned[chat_id] = now
                log.warning("소리 조각 너무 많음 %s: 1초에 %d개 (%s) · DJ=%s 대화=%s", chat_id, w[1], ",".join(sorted(w[2])),
                            chat_id in self.players, chat_id in self.bridges)
            w = self._mic_win[chat_id] = [now, 0, set()]
        w[1] += 1
        w[2].add(who)
        await self.calls.send_frame(chat_id, self.media.Device.MICROPHONE, frame)

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
                b.feed([(f.ssrc, f.frame) for f in update.frames])   # ssrc = 누가 말했나 (말한 사람 확인)

        @self.calls.on_update(md.filters.chat_update(md.ChatUpdate.Status.LEFT_CALL))   # 음성채팅 닫힘·방에서 내보내짐
        async def _gone(_, update):
            b = self.bridges.get(update.chat_id)
            if b:
                b.stop("chat_closed")                  # OpenAI 연결 끊김(ws_closed)과 구분
            pl = self.players.get(update.chat_id)
            if pl:
                pl.stop("chat_closed")
            self.in_call.discard(update.chat_id)

        if hasattr(md.filters, "call_participant"):   # 새로 들어온·바뀐 참가자의 소리 번호 → 계정
            @self.calls.on_update(md.filters.call_participant(md.Action.JOINED | md.Action.UPDATED))
            async def _joined(_, update):
                part = update.participant
                if update.chat_id in self.bridges and getattr(part, "source", None):
                    self.ssrc_users.setdefault(update.chat_id, {})[part.source] = part.user_id

        if hasattr(md.filters, "call_participant"):   # 통화에서만 내보내짐 (방엔 남음) — ChatUpdate 로 안 옴
            @self.calls.on_update(md.filters.call_participant(md.Action.KICKED | md.Action.LEFT) & md.filters.me)
            async def _removed(_, update):
                b = self.bridges.get(update.chat_id)
                if b:
                    b.stop("kicked")
                pl = self.players.get(update.chat_id)
                if pl:
                    pl.stop("kicked")
                self.in_call.discard(update.chat_id)

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
            elif kind == "music_play":
                ok, res = await self._music_play(chat_id, p)
            elif kind.startswith("music_"):
                ok, res = await self._music_ctl(chat_id, kind[6:], p)
            else:
                ok, res = False, "unknown"
        except Exception as e:
            log.warning("음성 일 %s 실패: %s", kind, e)
            ok, res = False, f"error:{type(e).__name__}"
        await store.finish(self.db, job["id"], ok, res)

    async def _run_job(self, job: dict) -> None:
        kind = job["kind"]
        key = 0 if kind.startswith(("login", "logout")) else job["chat_id"]   # 로그인끼리·같은 방끼리만 차례로
        if kind == "music_play":            # 노래 찾기·받기는 느림 → 신청끼리만 차례로 (넘기기·일시정지를 안 막게)
            key = ("mplay", job["chat_id"])
        elif kind.startswith("music_"):
            key = ("mctl", job["chat_id"])
        async with self.locks.setdefault(key, asyncio.Lock()):
            await self.handle(job)

    async def step(self) -> None:
        await self.db.set_state(0, store.WORKER_BEAT, time.time())
        for job in await store.take_jobs(self.db):
            t = asyncio.create_task(self._run_job(job))
            self.jobs.add(t)
            t.add_done_callback(self.jobs.discard)
        if self.players and time.monotonic() - self._watch_at >= STUTTER_EVERY:
            self._watch_at = time.monotonic()
            try:
                await self._music_watch()
            except Exception as e:
                log.warning("끊김 감시 실패: %s", e)
        if self.client and time.monotonic() - self._rejoin_at >= REJOIN_EVERY:
            self._rejoin_at = time.monotonic()
            try:
                await self._music_rejoin()
            except Exception as e:
                log.warning("노래 다시 들어가기 확인 실패: %s", e)
        self._maybe_night_prefetch()
        if time.monotonic() - self._health_at > HEALTH_EVERY:
            self._health_at = time.monotonic()
            try:
                await store.purge_lines(self.db)          # 통화 대화 7일 지나면 지움
            except Exception as e:
                log.warning("음성 기록 정리 실패: %s", e)
            await self.health()

    async def drain(self) -> None:
        """돌고 있는 일감이 끝날 때까지 (테스트·종료용)."""
        while self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)

    async def health(self) -> None:
        """도우미 세션이 풀렸으면(다른 기기에서 세션 종료 등) 표시를 지워 봇이 '연결 안 됨' 으로 안내하게."""
        if not self.client:
            return
        try:
            if not self.client.is_connected():
                await self.client.connect()
            if await self.client.is_user_authorized():
                return
        except Exception as e:
            if type(e).__name__ not in ("AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError"):
                log.warning("도우미 상태 확인 실패(다음에 다시): %s", e)
                return
        log.warning("도우미 세션이 풀렸어요 — 🎙 에서 다시 연결")
        for chat_id in list(self.bridges):
            await self._stop(chat_id, "logout")
        await self._stop_music("logout")
        self.client = self.calls = None
        await self.db.set_state(0, store.ASSISTANT_KEY, None)

    async def shutdown(self) -> None:
        """SIGTERM(배포·재시작): 통화마다 나가기 → 방에 유령 참가자가 안 남음. 노래는 위치를 남기고 다시 켜지면 이어서."""
        for chat_id in list(self.bridges):
            await self._stop(chat_id, "restart")
        await self._stop_music("restart")
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            with contextlib.suppress(BaseException):
                await self._prefetch_task
        if hasattr(self.music_source, "close"):     # 받기 담당 자식 프로그램도 끝냄
            self.music_source.close()

    # ── 🎵 뮤직봇 ──────────────────────────────────────────
    async def _ensure_call(self, chat_id: int, p: dict) -> tuple[bool, str]:
        """노래용 소리 줄 (AI 대화가 이미 열었으면 그대로). 같은 방 AI 시작과 겹치지 않게 방 잠금 안에서."""
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            if chat_id in self.in_call:
                return True, "already"
            if not self.client or not self.calls:
                return False, "no_assistant"
            if len(self.in_call | set(self.bridges)) >= MAX_CALLS:
                return False, "busy"
            if p.get("link") or p.get("username"):
                await self._join(p)
            md = self.media
            try:
                await asyncio.wait_for(self.calls.play(chat_id, md.MediaStream(md.ExternalMedia.AUDIO, md.AudioParameters(48000, 1)),
                                                       md.GroupCallConfig(auto_start=True)), PLAY_TIMEOUT)
            except Exception as e:
                await self._leave(chat_id)
                log.warning("노래 통화 시작 실패 %s: %r", chat_id, e)
                return False, err_code(e)
            self.in_call.add(chat_id)
            return True, "joined"

    async def _get_player(self, chat_id: int, p: dict, by: int | None) -> tuple[Any, str]:
        """방마다 DJ 는 한 명. 동시에 두 신청이 오면(재생목록 + 다른 신청) 둘 다 '없네' 하고 DJ 를 둘 만들던 것 — 2026-10-09 실제:
        두 DJ 가 같은 소리 줄에 번갈아 보내 0.01초씩 끊기고, 덮인 쪽은 손잡이를 잃고 정리 도중 지워짐. 그래서 방마다 잠금."""
        lock = self._plocks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            return await self._get_player_locked(chat_id, p, by)

    async def _get_player_locked(self, chat_id: int, p: dict, by: int | None) -> tuple[Any, str]:
        pl = self.players.get(chat_id)
        if pl is not None and not pl.done:
            return pl, "ok"
        old = self.ptasks.get(chat_id)
        if old is not None and not old.done():      # 앞 DJ 가 끝나는 중 (멈췄지만 정리 중) → 정리 끝날 때까지 기다림.
            with contextlib.suppress(Exception):    # 안 기다리고 새 DJ 로 덮으면 옛 작업의 손잡이가 사라져 정리 도중 지워짐 (2026-10-09 실제)
                await asyncio.wait({old}, timeout=10)
        ok, res = await self._ensure_call(chat_id, p)
        if not ok:
            return None, res
        pl = music.Player(self.db, chat_id, lambda f: self._mic(chat_id, f, "dj"),
                          source=self.music_source, announce=self._music_say, decoder=self.music_decoder,
                          has_voice=lambda: chat_id in self.bridges, **self.music_opts)
        sid = await musicq.session_start(self.db, chat_id, by)
        self.players[chat_id] = pl
        t = asyncio.create_task(self._run_player(chat_id, pl, sid))
        self.ptasks[chat_id] = t
        self._ptasks_all.add(t)                     # 손잡이를 꼭 쥐고 있음 (ptasks 가 덮여도 끝날 때까지)
        t.add_done_callback(self._ptasks_all.discard)
        return pl, "ok"

    async def _run_player(self, chat_id: int, pl: Any, sid: int) -> None:
        reason = "error"
        try:
            reason = await pl.run()
        except Exception as e:
            log.warning("뮤직 재생 오류 %s: %r", chat_id, e)
            reason = f"error:{type(e).__name__}"
        finally:
            mine = self.players.get(chat_id) is pl      # 끝나는 사이 새 신청으로 새 DJ 가 생겼으면 그 대기열은 건드리지 않음
            if mine:
                self.players.pop(chat_id, None)
                self.ptasks.pop(chat_id, None)
            if reason not in music.RESUME_REASONS and mine and chat_id not in self.players:   # 재시작·닫힘이 아니면 남은 곡은 정리
                await musicq.clear(self.db, chat_id, "removed")
            if reason == "chat_closed" and mine and chat_id not in self.players and await musicq.has_queue(self.db, chat_id):
                st = await self.db.get_state(0, musicq.REJOIN_KEY) or {}      # 다시 열리면 이어서 (15분 안)
                st[str(chat_id)] = int(time.time()) + musicq.REJOIN_SEC
                await self.db.set_state(0, musicq.REJOIN_KEY, st)
            await musicq.session_end(self.db, sid, reason, pl.tracks, dict(pl.stats))
            if mine:
                self._swin.pop(chat_id, None)
            if mine and chat_id not in self.players:
                await self._vc_title(chat_id, None)       # 음성채팅 제목 원래대로
            await self._maybe_leave(chat_id)
        log.info("노래 끝 %s %s · %s곡 · 조각 %s · 늦음 %s · 흔들림 %s(최대 %sms) · 보내기 느림 %s(최대 %sms) · 비어 있음 %s · "
                 "겹쳐 넘김 %s · 바로 이어 붙임 %s",
                 chat_id, reason, pl.tracks, pl.stats["frames"], pl.stats["late"], pl.stats["jitter"], pl.stats["max_late_ms"],
                 pl.stats["send_slow"], pl.stats["send_max_ms"], pl.stats["underrun"], pl.stats.get("xfade", 0),
                 pl.stats.get("gapless", 0))

    def _music_path(self, path: str | None) -> str | None:
        """봇이 받아 둔 텔레그램 음악 파일 — data/music 안만 (DB 일감이라도 다른 파일을 열지 않게)."""
        if not path:
            return None
        base = (Path(self.cfg.db_path).parent / "music").resolve()
        try:
            real = Path(path).resolve()
        except OSError:
            return None
        return str(real) if real.is_relative_to(base) and real.is_file() else None

    async def _music_play(self, chat_id: int, p: dict) -> tuple[bool, str]:
        if not self.client or not self.calls:
            return False, "no_assistant"
        by, name, status = p.get("by"), str(p.get("by_name") or "")[:60], p.get("status_msg")
        try:
            if p.get("path"):
                path = self._music_path(p["path"])
                if not path:
                    raise music.MusicError("not_found", "음악 파일을 못 찾았어요.")
                info = {"title": str(p.get("title") or "음악 파일")[:200], "url": "", "vid": None,
                        "duration": int(p.get("duration") or 0), "path": path}
            elif isinstance(p.get("pick"), dict):            # 고르기 버튼으로 고른 곡 (봇이 그대로 넘김 — 다시 검사)
                item = p["pick"]
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(item.get("vid") or "")):
                    raise music.MusicError("not_found", "고른 곡을 못 찾았어요.")
                info = await asyncio.to_thread(self.music_source.pick, item)
            elif hasattr(self.music_source, "playlist") and music.playlist_id(str(p.get("query") or "")):
                return await self._music_playlist(chat_id, p, by, name, status)
            else:
                info = await asyncio.to_thread(self.music_source.resolve, str(p.get("query") or "")[:300])
                await self._music_health(None)
        except music.MusicChoice as c:
            await self._music_choice(chat_id, status, by, name, c)
            return False, "music:choice"
        except music.MusicError as e:
            if e.code != "mix" or not hasattr(self.music_source, "mix_for"):
                if e.code == "mix":
                    e = music.MusicError("not_found", "가수나 노래 제목을 같이 써 주세요.")
                if e.code == "blocked" or (e.code == "not_found" and hasattr(self.music_source, "primary_ok")
                                           and not self.music_source.primary_ok()):
                    await self._music_health(str(e))
                await self._music_edit(chat_id, status, str(e) if str(e).startswith("🎧") else f"⚠️ {e}")
                return False, f"music:{e.code}"
            try:                                         # '잔잔한 플리' → 분위기 곡 여러 개
                items = await asyncio.to_thread(self.music_source.mix_for, str(p.get("query") or "")[:300])
            except music.MusicError as e2:
                await self._music_edit(chat_id, status, f"⚠️ {e2}" if not str(e2).startswith("🎧") else str(e2))
                return False, f"music:{e2.code}"
            return await self._add_many(chat_id, p, by, name, status, items,
                                        lambda n, tail: f"🎧 <b>{musicq._esc(str(p.get('query') or ''))[:40]}</b> — "
                                                        f"분위기에 맞는 <b>{n}곡</b> 골라 넣었어요{tail}. <code>.대기열</code> · <code>.섞기</code>")
        rid, pos, why = await musicq.add(self.db, chat_id, title=info["title"], url=info["url"], vid=info["vid"],
                                         duration=info["duration"], by_id=by, by_name=name, path=info.get("path"),
                                         msg_id=status)
        if not rid:
            text = (f"⚠️ 대기열이 꽉 찼어요 ({musicq.QUEUE_MAX}곡)." if why == "full"
                    else f"🎶 이미 틀고 있거나 대기열에 있는 곡이에요: {musicq._esc(info['title'])[:80]}" if why == "dup"
                    else f"⚠️ 한 사람이 걸어 둘 수 있는 곡은 {musicq.PER_USER}개까지예요.")
            await self._music_edit(chat_id, status, text)
            return False, f"music:{why}"
        pl, res = await self._get_player(chat_id, p, by)
        if pl is None:                                  # '찾는 중' 글은 그대로 → 봇이 이유(권한·음성채팅 없음…)로 고침
            await musicq.finish(self.db, rid, "failed")
            return False, res
        if pos:                                         # 지금 다른 곡 중 → 대기열 카드 (시작할 땐 새 메시지)
            await musicq.set_msg(self.db, rid, None)
            row = {"title": info["title"], "duration": info["duration"], "by_name": name}
            await self._music_edit(chat_id, status, musicq.card_text("queued", row, pos=pos))
        pl.wake()
        return True, "queued" if pos else "playing"

    async def _music_playlist(self, chat_id: int, p: dict, by, name: str, status) -> tuple[bool, str]:
        """재생목록 링크 → 최대 PLAYLIST_MAX 곡을 대기열에 (받기는 차례가 오면). 한 사람 한도 대신 목록 한도."""
        items = await asyncio.to_thread(self.music_source.playlist, music.playlist_id(str(p.get("query") or "")))
        return await self._add_many(chat_id, p, by, name, status, items,
                                    lambda n, tail: f"📃 재생목록에서 <b>{n}곡</b> 넣었어요{tail}. <code>.대기열</code> 로 확인")

    async def _add_many(self, chat_id: int, p: dict, by, name: str, status, items: list[dict], done_text) -> tuple[bool, str]:
        added, skipped = [], 0
        for it in items:
            rid, pos, why = await musicq.add(self.db, chat_id, title=it["title"], url=it["url"], vid=it["vid"],
                                             duration=it["duration"], by_id=by, by_name=name, per_user=music.PLAYLIST_MAX)
            if why == "full":
                break
            if rid:
                added.append(rid)
            else:
                skipped += 1
        if not added:
            await self._music_edit(chat_id, status, f"⚠️ 넣을 곡이 없어요 (대기열 {musicq.QUEUE_MAX}곡까지·이미 있는 곡 빼고).")
            return False, "music:full"
        pl, res = await self._get_player(chat_id, p, by)
        if pl is None:
            for rid in added:
                await musicq.finish(self.db, rid, "failed")
            return False, res
        tail = f" (이미 있는 곡 {skipped}개 뺌)" if skipped else ""
        await self._music_edit(chat_id, status, done_text(len(added), tail))
        pl.wake()
        return True, "playlist"

    async def _vc_title(self, chat_id: int, title: str | None) -> None:
        """음성채팅 제목을 지금 곡으로 (방 설정 vc_title). title None = 처음 제목으로 되돌림.
        도우미에게 '음성채팅 관리' 권한이 있어야 함 (노래 신청 때 봇이 줌) — 안 되면 조용히 건너뜀.
        텔레그램이 답을 안 주면 VC_TITLE_TIMEOUT 만 기다림 (2026-10-10 DC 4 내부 오류 때 노래 끝 정리가 통째로 붙잡힘)."""
        try:
            await asyncio.wait_for(self._vc_title_now(chat_id, title), VC_TITLE_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("음성채팅 제목 못 바꿈 %s: 텔레그램 답 없음", chat_id)

    async def _vc_title_now(self, chat_id: int, title: str | None) -> None:
        if not self.client:
            return
        try:
            if title is not None and not (await musicq.modes(self.db, chat_id))["vc_title"]:
                return
            from telethon.tl.functions.channels import GetFullChannelRequest
            from telethon.tl.functions.phone import EditGroupCallTitleRequest, GetGroupCallRequest
            full = await self.client(GetFullChannelRequest(await self.client.get_input_entity(chat_id)))
            call = full.full_chat.call
            if not call:
                return
            saved = self.__dict__.setdefault("_vc_orig", {})
            if title is None:
                if chat_id not in saved:
                    return
                new = saved.pop(chat_id)
            else:
                if chat_id not in saved:                 # 처음 바꿀 때 원래 제목을 기억 (끝나면 되돌림)
                    got = await self.client(GetGroupCallRequest(call=call, limit=1))
                    saved[chat_id] = getattr(got.call, "title", None) or ""
                new = ("🎵 " + re.sub(r"\s+", " ", title)).strip()[:64]
            await self.client(EditGroupCallTitleRequest(call=call, title=new))
        except Exception as e:
            log.info("음성채팅 제목 못 바꿈 %s: %r", chat_id, e)

    async def _music_ctl(self, chat_id: int, op: str, p: dict) -> tuple[bool, str]:
        pl = self.players.get(chat_id)
        if pl is None or pl.done:
            return False, "no_music"
        if op == "skip":
            return pl.skip(), "skipped"
        if op == "pause":
            return pl.pause(), "paused"
        if op == "resume":
            return pl.resume(), "resumed"
        if op == "mute":
            pl.muted = True
            return True, "muted"
        if op == "unmute":
            pl.muted = False
            return True, "unmuted"
        if op == "volume":
            v = p.get("value")
            pl.volume = max(0, min(200, int(v) if v is not None else 100))   # 0 은 0 (예전: 'or 100' 이라 .볼륨 0 = 100%)
            return True, f"volume:{pl.volume}"
        if op == "loop":
            pl.loop = max(0, min(10, int(p.get("value") if p.get("value") is not None else 0)))
            return True, f"loop:{pl.loop}"
        if op == "seek":
            sec = float(p.get("value") or 0)
            if p.get("relative"):
                sec += pl.pos_ms / 1000
            return await pl.seek(max(0.0, sec)), f"seek:{int(max(0.0, sec))}"
        if op == "end":
            await musicq.clear(self.db, chat_id, "removed")
            pl.stop("end")
            t = self.ptasks.get(chat_id)
            if t:
                await asyncio.wait({t}, timeout=10)
            return True, "ended"
        return False, "unknown"

    async def _stop_music(self, reason: str) -> None:
        for chat_id, pl in list(self.players.items()):
            pl.stop(reason)
            t = self.ptasks.get(chat_id)
            if t:
                await asyncio.wait({t}, timeout=10)

    async def music_resume(self, chats: list[int]) -> None:
        """재시작으로 끊긴 방(세션이 열려 있던 방)은 남은 곡을 이어서, 나머지 방의 묵은 대기열은 정리.
        (예전: 신청 시각 30분 기준이라 긴 대기열 앞쪽 곡이 지워졌음) 음성채팅이 닫혀 기다리는 방의 곡도 남김."""
        waiting = await self.db.get_state(0, musicq.REJOIN_KEY) or {}
        now = int(time.time())
        await musicq.drop_except(self.db, list(chats) + [int(k) for k, dl in waiting.items() if int(dl or 0) > now])
        if not self.client or not self.calls:
            for chat_id in chats:
                await musicq.clear(self.db, chat_id, "removed")
            return
        live = set(await musicq.chats_with_queue(self.db, 0))
        for chat_id in [c for c in chats if c in live]:
            if not await self._call_open(chat_id):      # 그 사이 음성채팅이 닫힘 → 우리가 다시 켜지 않고 '다시 열리면 이어서'로
                st = await self.db.get_state(0, musicq.REJOIN_KEY) or {}   # (2026-10-09 18:44: 닫힌 방에 재시작 뒤 이어 틀기를 시도)
                st[str(chat_id)] = now + musicq.REJOIN_SEC
                await self.db.set_state(0, musicq.REJOIN_KEY, st)
                log.info("노래 이어 틀기 대기 %s (음성채팅이 닫혀 있음)", chat_id)
                continue
            pl, res = await self._get_player(chat_id, {}, None)
            if pl is None:
                log.info("노래 이어 틀기 못 함 %s: %s", chat_id, res)
                await musicq.clear(self.db, chat_id, "removed")
            else:
                pl.wake()
                log.info("노래 이어 틀기 %s", chat_id)

    def _maybe_night_prefetch(self, now: float | None = None) -> None:
        kst = time.gmtime((time.time() if now is None else now) + 9 * 3600)
        if kst.tm_hour != PREFETCH_HOUR or self.players or (self._prefetch_task and not self._prefetch_task.done()):
            return
        day = time.strftime("%Y-%m-%d", kst)
        if self.__dict__.get("_prefetch_day") == day:
            return
        self._prefetch_day = day
        self._prefetch_task = asyncio.create_task(self._night_prefetch(day))

    async def _night_prefetch(self, day: str, gap: float = PREFETCH_GAP) -> int:
        """많이 튼 곡을 미리 받고 크기도 재 둠 → 신청하면 바로 (받기 기다림·음원 막힘 영향 없음). 노래가 시작되면 멈춤.
        받기는 다른 프로그램(fetcher, 낮은 우선순위)이 해서 소리 박자에는 안 닿음."""
        if await self.db.get_state(0, PREFETCH_KEY) == day:
            return 0
        await self.db.set_state(0, PREFETCH_KEY, day)
        src, got = self.music_source, 0
        for vid in await musicq.popular_vids(self.db, limit=PREFETCH_MAX):
            if self.players:
                log.info("인기곡 미리 받기 멈춤 (노래 시작)")
                break
            cached = getattr(src, "_cached", None)
            if cached and cached(vid):
                continue
            try:
                path = await asyncio.to_thread(src.fetch, vid)
                if hasattr(src, "loudness"):
                    await asyncio.to_thread(src.loudness, path)
                got += 1
            except music.MusicError as e:
                log.info("인기곡 미리 받기 %s: %s", vid, e)
                if e.code == "blocked":
                    break
                continue
            except Exception as e:
                log.info("인기곡 미리 받기 %s: %r", vid, e)
                continue
            if gap:
                await asyncio.sleep(gap)
        log.info("인기곡 미리 받기: %s곡", got)
        return got

    async def _call_open(self, chat_id: int) -> bool:
        """그 방에 음성채팅이 열려 있나 (우리가 켜지 않고 보기만 — 관리자가 닫은 걸 다시 켜면 안 됨)."""
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await self.client(GetFullChannelRequest(await self.client.get_input_entity(chat_id)))
            return bool(full.full_chat.call)
        except Exception as e:
            log.info("음성채팅 열림 확인 실패 %s: %r", chat_id, e)
            return False

    async def _music_rejoin(self, now: int | None = None) -> None:
        """음성채팅이 닫혀 멈춘 방: 15분 안에 다시 열리면 들어가서 남은 곡을 이어서. 지나면 대기열 정리.
        (예전: 닫히는 순간 대기열을 지워서 관리자가 실수로 닫았다 열어도 처음부터 다시 신청해야 했음)"""
        st = await self.db.get_state(0, musicq.REJOIN_KEY) or {}
        if not st:
            return
        now = int(time.time()) if now is None else now
        keep = {}
        for key, deadline in st.items():
            chat_id = int(key)
            pl = self.players.get(chat_id)
            if pl is not None and not pl.done:          # 그 사이 새 신청으로 이미 다시 틂
                continue
            if now > int(deadline or 0):
                log.info("노래 다시 들어가기 포기 %s (음성채팅이 %s분 안 열림)", chat_id, musicq.REJOIN_SEC // 60)
                await musicq.clear(self.db, chat_id, "removed")
                continue
            if not await musicq.has_queue(self.db, chat_id):
                continue
            if not await self._call_open(chat_id):
                keep[key] = deadline
                continue
            pl, res = await self._get_player(chat_id, {}, None)
            if pl is None:
                log.info("노래 다시 들어가기 못 함 %s: %s", chat_id, res)
                keep[key] = deadline
                continue
            pl.wake()
            log.info("노래 다시 들어가기 %s", chat_id)
            if self.bot:
                with contextlib.suppress(Exception):
                    await self.bot.send_message(chat_id, "🎵 음성채팅이 다시 열려서 남은 곡을 이어서 틀어요.")
        await self.db.set_state(0, musicq.REJOIN_KEY, keep or None)

    async def _music_health(self, err: str | None) -> None:
        cur = await self.db.get_state(0, musicq.HEALTH_KEY) or {}
        now = int(time.time())
        if err:
            cur.update(err=err[:200], err_ts=now)
        else:
            cur.update(ok_ts=now)
        alert = bool(err) and now - int(cur.get("alert_ts") or 0) >= OWNER_ALERT_SEC
        if alert:
            cur["alert_ts"] = now
        await self.db.set_state(0, musicq.HEALTH_KEY, cur)
        if alert:                                  # 방엔 '잠시 뒤 다시'만, 운영자 할 일은 오너 1:1 로 (6시간에 한 번)
            await self._alert_owners("🎵 <b>뮤직봇: 기본 음원이 막혀 노래를 못 틀었어요.</b>\n"
                                     f"<code>{musicq._esc(err[:150])}</code>\n"
                                     "우회 길(sodam-warp)이 죽었거나 막혔을 수 있어요. 1:1 🎵 화면에서 쿠키를 넣으면 예비로 써요.")

    async def _music_watch(self, now: float | None = None) -> None:
        """끊김 숫자를 1분마다 찍어 두고 최근 5분 늘어난 양이 기준을 넘으면 오너에게 (사람이 '끊겨요' 하기 전에 앎)."""
        now = time.monotonic() if now is None else now
        for chat_id in list(self._swin):
            if chat_id not in self.players:
                self._swin.pop(chat_id, None)
        for chat_id, pl in list(self.players.items()):
            if pl.done:
                continue
            snap = {k: int(pl.stats.get(k, 0)) for k in STUTTER_LIMITS}
            snap["max_late"], pl.win_max_late = int(getattr(pl, "win_max_late", 0)), 0
            hist = self._swin.setdefault(chat_id, deque())
            if not hist or hist[-1][2] != id(pl):     # 처음 보는 DJ → 시작 때 0 에서 셈 (예전: 첫 1분 사이 끊김은 기준점에 묻혀 못 셈)
                hist.clear()
                hist.append((now - STUTTER_EVERY, {k: 0 for k in snap}, id(pl)))
            hist.append((now, snap, id(pl)))
            while hist and (now - hist[0][0] > STUTTER_WIN + 1 or hist[0][2] != id(pl)):
                hist.popleft()
            if len(hist) < 2:
                continue
            first = hist[0][1]
            grew = {k: snap[k] - first[k] for k in STUTTER_LIMITS}
            bad = [k for k, lim in STUTTER_LIMITS.items() if grew[k] >= lim]
            if bad:
                worst = max(s["max_late"] for _, s, _ in list(hist)[1:])
                await self._stutter_alert(chat_id, pl, grew, worst, int(now - hist[0][0]))

    async def _stutter_alert(self, chat_id: int, pl: Any, grew: dict, worst: int, span: int) -> None:
        sent = await self.db.get_state(0, STUTTER_KEY) or {}
        wall = int(time.time())
        if wall - int(sent.get(str(chat_id)) or 0) < STUTTER_ALERT_SEC:
            return
        sent = {k: v for k, v in sent.items() if wall - int(v or 0) < STUTTER_ALERT_SEC}
        sent[str(chat_id)] = wall
        await self.db.set_state(0, STUTTER_KEY, sent)
        try:
            load = "%.1f" % os.getloadavg()[0]
        except OSError:
            load = "?"
        nums = " · ".join(f"{STUTTER_NAMES[k]} {grew[k]}번" for k in STUTTER_LIMITS if grew[k])
        title = musicq._esc(str((pl.row or {}).get("title") or "")[:60]) if getattr(pl, "row", None) else ""
        log.warning("노래 끊김 감지 %s: %s (최대 %sms, 부하 %s)", chat_id, nums, worst, load)
        await self._alert_owners(f"🎵 <b>뮤직봇 소리 끊김 감지</b> (방 <code>{chat_id}</code>)\n"
                                 f"최근 {max(1, span // 60)}분: {nums}\n가장 크게 밀림 {worst}ms · 서버 부하 {load} (코어 2개)"
                                 + (f"\n지금 곡: {title}" if title else "")
                                 + "\n같은 방은 1시간에 한 번만 알려요.")

    async def _alert_owners(self, text: str) -> None:
        if not self.bot:
            return
        try:
            ids = set(getattr(self.cfg, "owner_ids", ()) or ()) | set(await self.db.owner_ids())
        except Exception:
            ids = set(getattr(self.cfg, "owner_ids", ()) or ())
        for uid in ids:
            with contextlib.suppress(Exception):
                await self.bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)

    async def _music_edit(self, chat_id: int, msg_id: int | None, text: str | None) -> None:
        """봇이 올린 '찾는 중' 글을 결과로 고침 (없으면 새로). text None = 그 글 지움."""
        if not self.bot:
            return
        try:
            if msg_id and text is None:
                await self.bot.delete_message(chat_id, msg_id)
            elif msg_id:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            elif text:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            log.info("뮤직 글 고치기 실패 %s: %r", chat_id, e)

    async def _music_choice(self, chat_id: int, msg_id: int | None, by, name: str, c) -> None:
        """딱 맞는 곡이 없음·버전 고르기 → '찾는 중' 글을 고르기 버튼으로 (신청한 사람이 누르면 봇이 pick 일감)."""
        cid = await musicq.save_choice(self.db, chat_id, by, name, c.query, c.reason, c.items)
        if not self.bot:
            return
        text, kb = musicq.choice_text(c.reason, c.query), musicq.choice_kb(cid, c.items)
        try:
            if msg_id:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=kb)
            else:
                msg_id = getattr(await self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb), "message_id", None)
            await musicq.set_choice_msg(self.db, cid, msg_id)
        except Exception as e:
            log.info("고르기 버튼 올리기 실패 %s: %r", chat_id, e)

    async def _music_say(self, kind: str, chat_id: int, row, why: str = "") -> None:
        """Player 가 부름: now(재생 시작 — 처음 곡은 '찾는 중' 글을 고침) · failed."""
        if not self.bot:
            return
        if kind == "failed":
            src = self.music_source
            if src is not None and hasattr(src, "primary_ok") and not src.primary_ok():
                await self._music_health(why or "기본 음원 막힘")
            await self._music_edit(chat_id, row.get("msg_id") if isinstance(row, dict) else None,
                                   musicq.card_text("failed", dict(row), why=why))
            return
        t = asyncio.create_task(self._vc_title(chat_id, str(row["title"] or "")))
        self.__dict__.setdefault("_bg", set()).add(t)
        t.add_done_callback(self.__dict__["_bg"].discard)
        text, kb = musicq.card_text("now", dict(row)), musicq.card_kb()
        prev = self.__dict__.setdefault("_now_msg", {}).get(chat_id)
        if prev:                                   # 지난 곡 카드의 버튼은 뗌 (누르면 엉뚱한 곡이 멈추지 않게)
            with contextlib.suppress(Exception):
                await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=prev, reply_markup=None)
        mid = None
        if row["msg_id"]:
            try:
                await self.bot.edit_message_text(text, chat_id=chat_id, message_id=row["msg_id"], parse_mode="HTML",
                                                 reply_markup=kb)
                mid = row["msg_id"]
            except Exception as e:                  # 지워졌거나 너무 오래됨 → 새로
                log.debug("재생 카드 고치기 실패 → 새로: %r", e)
        if mid is None:
            mid = getattr(await self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb), "message_id", None)
        self._now_msg[chat_id] = mid

    async def leave_orphans(self, chats: list[int]) -> None:
        """지난번에 인사 없이 꺼졌다면(kill -9 등) 그 방 음성채팅에서 나감 (ntgcalls 는 기록이 없어 leave_call 이 안 됨)."""
        if not self.client or not chats:
            return
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.phone import LeaveGroupCallRequest
        for chat_id in chats:
            try:
                full = await self.client(GetFullChannelRequest(await self.client.get_input_entity(chat_id)))
                if full.full_chat.call:
                    await self.client(LeaveGroupCallRequest(call=full.full_chat.call, source=0))
            except Exception as e:
                log.debug("남은 통화 정리 %s: %s", chat_id, e)

    async def run(self) -> None:
        orphans = [r["chat_id"] for r in await self.db._all("SELECT DISTINCT chat_id FROM voice_calls WHERE end_ts IS NULL")]
        await store.close_orphans(self.db)
        orphan_music = await musicq.close_orphans(self.db)      # kill -9 등으로 끝 표시 없이 남은 노래 세션
        music_chats = await musicq.restart_chats(self.db, int(time.time()) - 1800)   # 재시작으로 끊긴 방 (30분 안)
        orphans = sorted(set(orphans) | set(orphan_music))
        try:
            await self.resume()
            await self.leave_orphans(orphans)
            await self.music_resume(music_chats)
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
    from pytgcalls.types import Frame, GroupCallParticipant
    from pytgcalls.types.raw import AudioParameters, VideoParameters
    return SimpleNamespace(filters=filters, ChatUpdate=ChatUpdate, Device=Device, Direction=Direction,
                           Action=GroupCallParticipant.Action,
                           ExternalMedia=ExternalMedia, GroupCallConfig=GroupCallConfig, MediaStream=MediaStream,
                           RecordStream=RecordStream, AudioParameters=AudioParameters, VideoParameters=VideoParameters,
                           Frame=Frame)


async def main() -> None:
    from openai import AsyncOpenAI
    from pytgcalls import PyTgCalls

    from ..config import load_config
    from ..db import DB
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # 요청 줄에 봇 토큰이 든 URL 이 찍힘 (본체와 같게)
    cfg = load_config()
    db = DB(cfg.db_path)
    await db.open()
    oai = AsyncOpenAI(api_key=cfg.openai_api_key)
    from telegram import Bot

    from .. import panels  # noqa: F401 — 채팅 소담 도구 등록 (toolset 이 읽기 전용만 골라 씀)
    from ..__main__ import build_services
    svc = build_services(cfg, db)                  # 같은 DB·같은 예산 (폴링 없음 — Bot API 조회만)
    bot = Bot(cfg.telegram_token)
    await bot.initialize()
    w = Worker(cfg, db, calls_factory=PyTgCalls, realtime_connect=lambda model: (oai.live.connect(max_retries=0) if model.startswith("gpt-live")   # Live: 다시 연결은 우리가 (새 세션)
                                                    else oai.realtime.connect(model=model)),
               media=_media(), web_search=svc.llm.web_search, svc=svc, bot=bot)
    import signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runner = asyncio.create_task(w.run())
    await stop.wait()
    log.info("📞 음성 담당 끄는 중 — 통화에서 나가기")
    try:
        await asyncio.wait_for(w.shutdown(), 20)
    finally:
        runner.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
