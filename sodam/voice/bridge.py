"""OpenAI Realtime ↔ 텔레그램 음성채팅 다리.

듣기:   통화에서 받은 48 kHz 조각(feed) → 24 kHz 로 → 100 ms 씩 input_audio_buffer.append
말하기: response.output_audio.delta(24 kHz) → 48 kHz 10 ms 조각 줄 → 페이서가 절대 시각으로 10 ms 마다 play(조각) (말 없으면 무음)
끊기:   input_audio_buffer.speech_started(누가 말 시작) → 줄 비우고 conversation.item.truncate(지금까지 실제로 들려준 ms)
        → 모델이 '끝까지 말했다'고 착각하지 않음 (OpenAI Realtime 문서의 WebSocket 끊기 방식). 응답 취소는 서버 VAD interrupt_response.
한도:   max_sec 넘거나, idle_sec 동안 아무도(소담 포함) 말 안 하면 끝 (누가 말하는 중·소담 소리 재생 중엔 안 끝냄 —
        단 말 이벤트 없는 큰 소리는 LOUD_MAX, 안 끝나는 '말하는 중'은 SPEAK_MAX 까지만: 음악봇·켜 둔 마이크로 15분 꽉 채우지 않게).
        '소담아 나가/끊어' 도 끝. Realtime 오류는 무해한 것(이미 답하는 중 등)은 안 세고, 60초 안에 5번이면 끝.
끊김:   OpenAI 연결이 통화 중에 끊기면 한 번만 다시 연결(session.update 다시) → 또 끊기면 ws_closed.
계측:   stats (조각 수·늦은 재생·이벤트 루프 지연·오류 코드·끼어들기·첫 소리 지연·CPU) → voice_calls.stats (조각마다 로그 없음).

연결(connect)·재생(play)·시계는 밖에서 넣는다 → 테스트는 가짜로, 실제 통화는 worker.py 가 py-tgcalls 로.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import audio

log = logging.getLogger(__name__)

SEND_MS = 100                                   # 모델에 보내는 단위 (너무 잘게 보내면 WS 메시지만 많음)
SEND_BYTES = audio.AI_RATE * 2 * SEND_MS // 1000
SILENCE = bytes(audio.FRAME_BYTES)
MAX_BACKLOG = 50                                # 보낼 줄이 이만큼(5초) 밀리면 오래된 것부터 버림 (네트워크 막힘)
MAX_ERRORS = 5                                  # ERROR_WINDOW 초 안에 '진짜' 오류가 이만큼이면 끊음
ERROR_WINDOW = 60.0
# 통화를 끊을 이유가 아닌 Realtime 오류 — 세기만 하고 통화는 계속:
#  · 이미 답하는 중에 response.create (response.done 전엔 새 답 불가 — community.openai.com/t/1005582,
#    github.com/livekit/agents/issues/7514 'response.create 직렬화')
#  · truncate audio_end_ms 가 실제 길이보다 큼 → 서버 오류 (developers.openai.com/api/reference/resources/realtime/client-events
#    'If the audio_end_ms is greater than the actual audio duration, the server will respond with an error')
#  · 빈 버퍼 commit ('This event will produce an error if the input audio buffer is empty') · 취소할 답 없음
# 코드 이름은 문서에 목록이 없어 param/메시지(audio_end_ms)로도 봄 (benign)
BENIGN_ERRORS = frozenset({"conversation_already_has_active_response", "response_cancel_not_active",
                           "input_audio_buffer_commit_empty", "item_truncate_invalid_audio_end_ms",
                           "invalid_audio_end_ms", "item_not_found"})
MAX_RECONNECTS = 1                              # 통화 중 OpenAI 연결이 끊기면 다시 연결하는 횟수
LOUD = 600                                      # 들어온 100 ms 소리 크기(RMS)가 이만큼이면 '누가 소리 냄' (idle 아님)
# 음악봇·켜 둔 마이크: 큰 소리가 계속 들어오고 VAD 는 speech_stopped 를 안 보냄 → 예전엔 idle 이 영영 안 와서 15분 꽉 채움(요금).
LOUD_MAX = 60.0                                 # 말 이벤트(speech_started/stopped·받아쓰기) 없이 큰 소리만으로 활동이라 보는 최대 시간
SPEAK_MAX = 120.0                               # speech_started 뒤 speech_stopped 없이 '말하는 중'으로 봐 주는 최대 시간 (사람 말은 숨 쉬느라 끊김)
PENDING_MAX = 10.0                              # response.create 보낸 뒤 response.created 를 기다려 주는 최대 시간 (안 오면 잊음)
LATE_MS = 20                                    # 재생 조각이 이만큼 늦으면 '늦음' 한 번 (계측만 — 박자는 안 바꿈)
LAG_EVERY = 0.1                                 # 이벤트 루프 지연 재기 (0.1초마다 잠깐 깨어남)
MAX_OUT = 600                                   # 음성 출력 토큰 ≈ 초당 30 (실측 1,070토큰/33초) → 약 20초 (400 은 도구 결과 설명이 문장 중간에 잘림 — 실측)
BYE = re.compile(r"소담.{0,6}(나가|끊어|그만|잘\s*가|바이|종료)")


@dataclass
class Result:
    reason: str = ""
    seconds: float = 0.0
    user_turns: int = 0
    bot_turns: int = 0
    usage: dict = field(default_factory=dict)        # response.done usage 합 (input/output 토큰)
    stats: dict = field(default_factory=dict)        # 통화 계측 (voice_calls.stats — diag voice 로 봄)


def benign(code: str, message: str = "", param: str = "") -> bool:
    """통화를 끊을 오류가 아님 (이미 답하는 중·취소할 답 없음·truncate 범위 등)."""
    return (code in BENIGN_ERRORS or "truncat" in code or "audio_end_ms" in param
            or "already has an active response" in message or "audio_end_ms" in message)


def _steal() -> int | None:
    """/proc/stat 전체 CPU steal (가상 서버에서 다른 손님이 CPU 를 가져간 시간, 틱). 없으면 None."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        return int(parts[8]) if parts[0] == "cpu" and len(parts) > 8 else None
    except (OSError, ValueError, IndexError):
        return None


def _cpu() -> float:
    t = os.times()
    return t.user + t.system


# 영상대화 중 쓸 수 있는 도구 (worker 가 실제 실행 함수를 넣음 — 채팅 소담의 격리 웹 검색과 같은 것)
WEB_SEARCH = {"type": "function", "name": "web_search",
              "description": "날씨·뉴스·시세·경기 결과·영업시간처럼 최신 정보가 필요할 때 인터넷 검색. 부르기 전에 '잠깐만요, 찾아볼게요' 한마디.",
              "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "검색어 (한국어 가능)"}},
                             "required": ["query"]}}
TOOL_TIMEOUT = 15


SPEAKER_SHARE = 0.7        # 한 사람 소리가 이만큼 넘어야 '그 사람 말' (여럿이 섞이면 모름 → 쓰기 도구 안 됨)


def add_usage(acc: dict, usage) -> None:
    """response.done 의 usage 를 통화 합계에 더함. 요금은 음성·글자·캐시가 따로라 나눠서 (store.cost_micro).
    input_tokens = 글자+음성(+그림), cached_tokens 는 그 안의 일부 — OpenAI Realtime usage 형식 (SDK RealtimeResponseUsage)."""
    if usage is None:
        return
    g = lambda o, k: int(getattr(o, k, 0) or 0)  # noqa: E731
    det, out = getattr(usage, "input_token_details", None), getattr(usage, "output_token_details", None)
    cdet = getattr(det, "cached_tokens_details", None)
    for k, v in (("input_tokens", g(usage, "input_tokens")), ("output_tokens", g(usage, "output_tokens")),
                 ("cached_tokens", g(det, "cached_tokens")),
                 ("in_audio", g(det, "audio_tokens")), ("in_text", g(det, "text_tokens")),
                 ("cached_audio", g(cdet, "audio_tokens")), ("cached_text", g(cdet, "text_tokens")),
                 ("out_audio", g(out, "audio_tokens")), ("out_text", g(out, "text_tokens"))):
        acc[k] = acc.get(k, 0) + v


def dominant(energy: dict[int, float]) -> int | None:
    total = sum(energy.values())
    if total <= 0:
        return None
    ssrc, top = max(energy.items(), key=lambda kv: kv[1])
    return ssrc if top / total >= SPEAKER_SHARE else None


def session_config(instructions: str, voice: str, reply: str = "all", language: str = "ko",
                   tools: list[dict] | None = None, transcribe_prompt: str = "") -> dict:
    """session.update 에 넣을 설정. reply=all 이면 말 끝날 때마다 답, name 이면 '소담' 이 들어간 말에만 (코드가 response.create)."""
    return {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": ["audio"],
        "max_output_tokens": MAX_OUT,        # 한 답 상한 (실측: '1~2문장' 규칙에도 4문장·10초↑ 답이 나옴)
        # gpt-realtime-2.1-mini 는 추론 모델 → 가장 낮게 (OpenAI 프롬프트 가이드: 'minimal: Lowest latency')
        "reasoning": {"effort": "minimal"},
        # 오래된 대화는 한 번에 20% 잘라냄 → 캐시가 덜 깨짐 (realtime-costs 가이드)
        "truncation": {"type": "retention_ratio", "retention_ratio": 0.8},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": audio.AI_RATE},
                "noise_reduction": {"type": "far_field"},           # 여러 사람 폰 마이크
                "transcription": {"model": "gpt-4o-mini-transcribe", "language": language,
                                  **({"prompt": transcribe_prompt[:500]} if transcribe_prompt else {})},
                "turn_detection": {"type": "server_vad", "threshold": 0.6, "prefix_padding_ms": 300,
                                   # silence 500 = 공식 예시값 (700 → 500: 말 끝 판단 0.2초 빨리)
                                   "silence_duration_ms": 500, "create_response": reply == "all",
                                   "interrupt_response": True},
            },
            "output": {"format": {"type": "audio/pcm", "rate": audio.AI_RATE}, "voice": voice},
        },
        **({"tools": tools, "tool_choice": "auto"} if tools else {}),
    }


class Bridge:
    def __init__(self, connect: Callable[[], Any], play: Callable[[bytes], Awaitable[Any]], *, instructions: str,
                 voice: str = "marin", reply: str = "all", greet: str | None = None, max_sec: float = 900,
                 idle_sec: float = 60, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 tools: dict[str, Callable[[dict], Awaitable[str]]] | None = None, tool_specs: list[dict] | None = None,
                 transcribe_prompt: str = "", on_line: Callable[[str, str, int | None], Any] | None = None):
        self._connect, self._play = connect, play
        self.instructions, self.voice, self.reply, self.greet = instructions, voice, reply, greet
        self.max_sec, self.idle_sec = max_sec, idle_sec
        self.clock, self.sleep = clock, sleep
        self.conn = None
        self.out: deque[tuple[str | None, bytes]] = deque()   # 들려줄 (답 item_id, 48 kHz 10 ms 조각)
        self.item: str | None = None                    # 지금 들려주는(마지막으로 play 한) 모델 답 item_id
        self._played: dict[str, int] = {}               # 답 item → 실제로 play 한 ms (답마다 따로 — 두 답이 이어져도 안 섞임)
        self._inbuf = bytearray()
        self._sendq: deque[bytes] = deque(maxlen=MAX_BACKLOG)
        self._send_evt = asyncio.Event()
        self._done = asyncio.Event()
        self.result = Result()
        self.transcript: list[tuple[str, str]] = []     # (who, text) — 저장은 on_line 이 (worker → voice_lines 7일)
        self.on_line = on_line                          # (who, text, ssrc) — user·sodam·tool
        self._item_ssrc: dict[str, int | None] = {}     # 말 item → 그 말의 주인 ssrc (받아쓰기는 늦게 옴)
        self._t0 = self._last_voice = 0.0
        self._errors: deque[float] = deque()            # 세는 오류가 난 시각 (ERROR_WINDOW 지나면 잊음)
        self._last_reply = -1e9
        self._speaking = False
        self._speech_t = 0.0                            # 마지막 speech_started 시각 (SPEAK_MAX 넘게 안 끝나면 음악 등 → 활동 아님)
        self._loud_from: float | None = None            # 마지막 말 이벤트 뒤 처음 큰 소리 시각 (LOUD_MAX 까지만 활동)
        self._responding = False                        # 모델이 답을 만드는 중 (response.created ~ response.done)
        self._pending: float | None = None              # response.create 보냄 → response.created 전 (그 사이 또 보내면 이미 답하는 중 오류)
        self._want_reply = False                        # 답하는 중에 도구 결과가 옴 → 그 답이 끝나면 response.create
        self._stack: contextlib.AsyncExitStack | None = None
        self._reconnects = 0
        self._stopped_at: float | None = None           # 말 끝(speech_stopped) 시각 → 첫 소리까지 지연
        self._first_audio: list[float] = []
        self._lags: list[float] = []
        self.stats: dict = {"frames_in": 0, "frames_out_voice": 0, "frames_out_silence": 0, "send_dropped": 0,
                            "late_ticks": 0, "max_late_ms": 0, "resyncs": 0, "interrupts": 0, "reconnects": 0,
                            "rt_errors": {}}
        self._energy: dict[int, float] = {}
        self.speaker: int | None = None                 # 마지막 말의 주인 ssrc (한 사람이 SPEAKER_SHARE 이상일 때만, 아니면 None)
        self.tools = tools or {}                        # 이름 → async (인자) -> 결과 글
        self.tool_specs = tool_specs
        self.transcribe_prompt = transcribe_prompt      # 받아쓰기 힌트 (방 멤버 이름)

    # ── 통화 쪽에서 부름 ──────────────────────────────────
    def feed(self, frames48: list) -> None:
        """통화에서 받은 사람들 소리 (같은 10 ms 의 여러 사람 → 섞음). 동기 — py-tgcalls 콜백에서 바로.
        조각이 (ssrc, bytes) 면 말하는 동안 사람(ssrc)별 소리 크기를 모아 '누가 말했나'를 정함 (speaker)."""
        if self._done.is_set():
            return
        self.stats["frames_in"] += 1
        if frames48 and isinstance(frames48[0], tuple):
            if self._speaking:
                for ssrc, f in frames48:
                    self._energy[ssrc] = self._energy.get(ssrc, 0.0) + audio.level(f)
            frames48 = [f for _, f in frames48]
        pcm = audio.down(audio.mix(frames48))
        if not pcm:
            return
        self._inbuf += pcm
        while len(self._inbuf) >= SEND_BYTES:
            chunk = bytes(self._inbuf[:SEND_BYTES])
            del self._inbuf[:SEND_BYTES]
            if len(self._sendq) == MAX_BACKLOG:
                self.stats["send_dropped"] += 1           # 가장 오래된 것이 버려짐 (네트워크 막힘)
            self._sendq.append(chunk)
            self._send_evt.set()
            if audio.level(chunk) >= LOUD:                # 100 ms 에 한 번 — 누가 소리 내는 중이면 idle 아님
                now = self.clock()                       # 단, 말 이벤트 없이 큰 소리만 LOUD_MAX 넘게 = 음악·잡음 → 활동 아님
                if self._loud_from is None:
                    self._loud_from = now
                if now - self._loud_from < LOUD_MAX:
                    self._last_voice = now

    def stop(self, reason: str) -> None:
        if not self._done.is_set():
            self.result.reason = reason
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def played_ms(self) -> int:
        """지금 들려주는 답에서 실제로 play 한 ms."""
        return self._played.get(self.item, 0) if self.item else 0

    # ── 실행 ────────────────────────────────────────────
    async def _open(self):
        """새 Realtime 연결 + 세션 설정 (처음·다시 연결 때 같음)."""
        stack = contextlib.AsyncExitStack()
        try:
            conn = await stack.enter_async_context(self._connect())
            specs = self.tool_specs if self.tool_specs is not None else ([WEB_SEARCH] if "web_search" in self.tools else [])
            await conn.session.update(session=session_config(self.instructions, self.voice, self.reply, tools=specs,
                                                           transcribe_prompt=self.transcribe_prompt))
        except BaseException:
            await self._close(stack)
            raise
        return stack, conn

    @staticmethod
    async def _close(stack) -> None:
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def run(self) -> Result:
        self._t0 = self._last_voice = self.clock()
        cpu0, steal0 = _cpu(), _steal()
        try:
            self._stack, self.conn = await self._open()
            try:
                if self.greet:   # response.instructions 는 세션 지시를 '대신'함 → 캐릭터를 같이 넣음
                    self._pending = self.clock()
                    await self.conn.response.create(response={"instructions": f"{self.instructions}\n\n{self.greet}"})
                tasks = [asyncio.create_task(f()) for f in (self._reader, self._sender, self._pacer, self._watch, self._lag)]
                await self._done.wait()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await self._close(self._stack)
                self._stack = None
        except Exception as e:                          # 연결 실패·끊김 — 통화는 worker 가 정리
            log.warning("음성 연결 끝남: %s", e)
            self.stop(f"error:{type(e).__name__}")
        self.result.seconds = self.clock() - self._t0
        self.result.stats = self._summary(cpu0, steal0)
        return self.result

    def _summary(self, cpu0: float, steal0: int | None) -> dict:
        """통화 계측 한 덩어리 (끝날 때 1번 계산)."""
        st = {**self.stats, "rt_errors": dict(self.stats["rt_errors"]), "end_reason": self.result.reason}
        lags = sorted(self._lags)
        if lags:
            st["loop_lag_max_ms"] = round(lags[-1] * 1000, 1)
            st["loop_lag_p99_ms"] = round(lags[min(len(lags) - 1, int(len(lags) * 0.99))] * 1000, 1)
        if self._first_audio:
            st["first_audio_ms_avg"] = round(sum(self._first_audio) / len(self._first_audio) * 1000)
            st["first_audio_ms_max"] = round(max(self._first_audio) * 1000)
            st["first_audio_n"] = len(self._first_audio)
        st["cpu_sec"] = round(_cpu() - cpu0, 2)           # 이 프로세스 전체 (같이 도는 다른 통화 포함)
        steal1 = _steal()
        if steal0 is not None and steal1 is not None:
            st["steal_ticks"] = steal1 - steal0
        with contextlib.suppress(OSError, AttributeError):
            st["loadavg"] = [round(x, 2) for x in os.getloadavg()]
        return st

    async def _reader(self) -> None:
        while True:
            try:
                async for ev in self.conn:
                    await self.on_event(ev)
                    if self._done.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("음성 읽기 오류: %s", e)
            if self._done.is_set():
                return
            if self._reconnects >= MAX_RECONNECTS or not await self._reconnect():
                break
        self.stop(self.result.reason or "ws_closed")

    async def _reconnect(self) -> bool:
        """통화 중 OpenAI 연결이 끊김 → 새로 연결하고 세션 설정을 다시 보냄 (대화 맥락은 새로 시작). 통화는 그대로."""
        self._reconnects += 1
        self.stats["reconnects"] = self._reconnects
        log.warning("Realtime 연결 끊김 → 다시 연결 (%d번째)", self._reconnects)
        old = self._stack
        try:
            self._stack, self.conn = await self._open()
        except Exception as e:
            log.warning("Realtime 다시 연결 실패: %s", e)
            return False
        finally:
            await self._close(old)
        self._responding = self._want_reply = False
        self._pending = None
        # 옛 연결에서 말하던 중이었으면 speech_stopped 는 영영 안 옴 → '말하는 중'을 풀고 지금부터 idle 을 셈
        self._speaking, self._energy, self._stopped_at, self._loud_from = False, {}, None, None
        self._last_voice = self.clock()
        # 옛 연결의 답 item 은 새 연결에 없음 → 남은 소리는 들려주되 truncate 는 안 함
        self.out = deque((None, f) for _, f in self.out)
        self._played.clear()
        return True

    async def on_event(self, ev) -> None:
        t = getattr(ev, "type", "")
        if t == "response.output_audio.delta":
            item = ev.item_id
            if item not in self._played:
                if len(self._played) > 50:                # 오래된 답 정리 (줄에 남은 것만 둠)
                    keep = {it for it, _ in self.out}
                    self._played = {k: v for k, v in self._played.items() if k in keep}
                self._played[item] = 0
            pcm = audio.up(base64.b64decode(ev.delta))
            for i in range(0, len(pcm), audio.FRAME_BYTES):
                self.out.append((item, pcm[i:i + audio.FRAME_BYTES].ljust(audio.FRAME_BYTES, b"\0")))
            now = self._last_voice = self.clock()
            if self._stopped_at is not None:              # 말 끝 → 첫 소리 지연 (계측)
                if len(self._first_audio) < 1000:
                    self._first_audio.append(now - self._stopped_at)
                self._stopped_at = None
        elif t == "input_audio_buffer.speech_started":
            self._last_voice = self._speech_t = self.clock()
            self._speaking, self._energy, self._loud_from = True, {}, None
            self._stopped_at = None
            await self._interrupt()
        elif t == "input_audio_buffer.speech_stopped":
            self._speaking, self._loud_from = False, None
            self._last_voice = self._stopped_at = self.clock()   # 방금까지 말했음 (긴 말 뒤 바로 idle 로 끊기지 않게)
            self.speaker = dominant(self._energy)
            if getattr(ev, "item_id", None) and len(self._item_ssrc) < 500:
                self._item_ssrc[ev.item_id] = self.speaker
        elif t == "conversation.item.input_audio_transcription.completed":
            text = (ev.transcript or "").strip()
            if not text:
                return
            self._last_voice = self.clock()
            self._loud_from = None
            self.transcript.append(("user", text))
            self._emit("user", text, self._item_ssrc.pop(getattr(ev, "item_id", None), self.speaker))
            self.result.user_turns += 1
            if BYE.search(text):
                self.stop("bye")
            elif self.reply == "name" and ("소담" in text or self.clock() - self._last_reply < 20):
                await self._reply()                      # '소담' 을 불렀거나 방금 이어지던 대화
        elif t == "response.output_audio_transcript.done":
            self.transcript.append(("sodam", ev.transcript or ""))
            self._emit("sodam", ev.transcript or "", None)
            self.result.bot_turns += 1
            self._last_reply = self._last_voice = self.clock()
        elif t == "response.function_call_arguments.done":
            meta = {"ssrc": self.speaker, "response_id": getattr(ev, "response_id", None)}
            asyncio.create_task(self._call_tool(ev.call_id, ev.name, ev.arguments, meta))   # 듣기·말하기는 계속
        elif t == "response.created":
            self._responding, self._pending = True, None
        elif t == "response.done":
            self._responding = False
            if self._want_reply and not self._busy():    # 답하는 중에 온 도구 결과 → 이제 말하게
                self._want_reply = False
                try:
                    await self._create()
                except Exception as e:
                    log.warning("도구 결과 뒤 답 요청 실패: %s", e)
            add_usage(self.result.usage, getattr(getattr(ev, "response", None), "usage", None))
        elif t == "error":
            # 보낸 response.create 가 거절됐을 수 있음 → 기다림을 풀어야 영영 조용해지지 않음 (진짜 답 중이면 created/done 이 옴)
            self._pending = None
            self._on_error(getattr(ev, "error", None))

    def _on_error(self, err) -> None:
        code = str(getattr(err, "code", None) or getattr(err, "type", None) or "unknown")[:60]
        msg = str(getattr(err, "message", None) or err or "")[:200]
        counts = self.stats["rt_errors"]
        if code in counts or len(counts) < 20:
            counts[code] = counts.get(code, 0) + 1
        if benign(code, msg, str(getattr(err, "param", None) or "")):
            log.info("Realtime 오류(무해, 통화 계속) %s: %s", code, msg)
            return
        log.warning("Realtime 오류 %s: %s", code, msg)
        now = self.clock()
        self._errors.append(now)
        while self._errors and now - self._errors[0] > ERROR_WINDOW:   # 오래된 오류는 잊음 (연달아 날 때만 끊음)
            self._errors.popleft()
        self.stats["last_error"] = code
        if len(self._errors) >= MAX_ERRORS:
            self.stop("error:realtime")

    async def _reply(self) -> None:
        """모델에게 말하라고 함. 이미 답하는 중이면 그 답이 끝난 뒤(response.done)로 미룸 (conversation_already_has_active_response 방지).
        도구 결과(function_call_output) 뒤 response.create = developers.openai.com/api/docs/guides/realtime-conversations."""
        if self._busy():                                 # 답하는 중 또는 보낸 요청의 response.created 를 기다리는 중
            self._want_reply = True
            return
        await self._create()

    def _busy(self) -> bool:
        if self._pending is not None and self.clock() - self._pending >= PENDING_MAX:
            self._pending = None                         # created·error 둘 다 안 옴 → 잊음 (영영 조용해지지 않게)
        return self._responding or self._pending is not None

    async def _create(self) -> None:
        """response.create — 보낸 순간부터 '기다리는 중' (created 전에 도구 결과가 와도 두 번째 create 를 안 보냄)."""
        self._pending = self.clock()
        try:
            await self.conn.response.create()
        except BaseException:
            self._pending = None
            raise

    def _emit(self, who: str, text: str, ssrc: int | None) -> None:
        if self.on_line and text:
            try:
                self.on_line(who, text, ssrc)
            except Exception as e:                       # 기록 실패해도 통화는 계속
                log.warning("음성 기록 실패: %s", e)

    async def _call_tool(self, call_id: str, name: str, arguments: str, meta: dict | None = None) -> None:
        """모델이 부른 도구 실행 → 결과를 대화에 넣고 이어서 말하게 (결과 속 지시는 데이터일 뿐)."""
        import json
        fn = self.tools.get(name)
        try:
            args = json.loads(arguments or "{}")
            if not fn:
                out = "그런 도구 없음 (쓸 수 있는 도구만 부를 것)"
            elif getattr(fn, "wants_meta", False):   # 누가 말했는지·어느 답인지 (toolset: 권한·오염 판단)
                out = await asyncio.wait_for(fn(args, meta or {}), TOOL_TIMEOUT)
            else:
                out = await asyncio.wait_for(fn(args), TOOL_TIMEOUT)
        except Exception as e:
            log.warning("음성 도구 %s 실패: %s", name, e)
            out = "검색이 지금 안 됨. 짧게 사과하고 채팅으로 물어보라고 안내."
        self.result.usage["tool_calls"] = self.result.usage.get("tool_calls", 0) + 1
        shown = str(out)
        shown = shown[shown.find("<tool_result"):] if "<tool_result" in shown else shown   # 앞 안내문은 빼고 결과만
        self._emit("tool", f"{name} {str(arguments or '')[:120]} → {shown[:300]}", (meta or {}).get("ssrc"))
        try:
            await self.conn.conversation.item.create(item={"type": "function_call_output", "call_id": call_id,
                                                           "output": str(out)[:2000]})
            await self._reply()
        except Exception as e:
            log.warning("도구 결과 전달 실패: %s", e)

    async def _interrupt(self) -> None:
        """누가 말을 시작함 → 들려주던 답을 멈추고, 모델 쪽 기록도 답마다 실제로 들려준 데까지 자름."""
        if not self.out:
            return
        items: list[str] = []
        for it, _ in self.out:                          # 줄에 남은 답들 (맨 앞 = 지금 들려주던 답)
            if it is not None and it not in items:
                items.append(it)
        self.out.clear()
        self.stats["interrupts"] += 1
        for it in items:
            try:
                await self.conn.conversation.item.truncate(item_id=it, content_index=0,
                                                            audio_end_ms=self._played.get(it, 0))
            except Exception as e:                      # 이미 끝난 item 등 — 끊기는 됐으니 무시
                log.debug("truncate 실패: %s", e)

    async def _sender(self) -> None:
        while True:
            await self._send_evt.wait()
            self._send_evt.clear()
            while self._sendq:
                chunk = self._sendq.popleft()
                try:
                    await self.conn.input_audio_buffer.append(audio=base64.b64encode(chunk).decode())
                except asyncio.CancelledError:
                    raise
                except Exception as e:                  # 연결 끊김 — 이 조각은 버리고 계속 (다시 연결은 _reader 가)
                    self.stats["send_dropped"] += 1
                    log.debug("소리 보내기 실패: %s", e)

    async def _pacer(self) -> None:
        """10 ms 마다 한 조각 — 절대 시각 기준이라 느려진 만큼 다음에 덜 잠 (많이 밀리면 다시 맞춤)."""
        step = audio.FRAME_MS / 1000
        nxt = self.clock()
        st = self.stats
        while True:
            if self.out:
                item, frame = self.out.popleft()
                self.item = item
                if item is not None:
                    self._played[item] = self._played.get(item, 0) + audio.FRAME_MS
                st["frames_out_voice"] += 1
                voiced = True
            else:
                frame, voiced = SILENCE, False
                st["frames_out_silence"] += 1
            try:
                await self._play(frame)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("재생 실패: %s", e)
                self.stop("error:play")
                return
            nxt += step
            now = self.clock()
            if voiced:
                self._last_voice = now                  # 소담 소리 재생 중 = 활동
            wait = nxt - now
            if wait > 0:
                await self.sleep(wait)
            else:
                late = -wait * 1000
                if late >= LATE_MS:                     # 계측만 (재생 박자는 그대로)
                    st["late_ticks"] += 1
                    if late > st["max_late_ms"]:
                        st["max_late_ms"] = round(late)
                if wait < -0.2:
                    st["resyncs"] += 1
                    nxt = self.clock()

    async def _watch(self) -> None:
        while True:
            now = self.clock()
            if now - self._t0 >= self.max_sec:
                return self.stop("time")
            talking = self._speaking and now - self._speech_t < SPEAK_MAX   # 끝없는 '말하는 중'(음악·끊긴 연결)은 안 셈
            if now - self._last_voice >= self.idle_sec and not self.out and not talking:
                return self.stop("idle")                # 누가 말하는 중(speech_started 뒤)·재생 중엔 안 끝냄 — max_sec 가 상한
            await self.sleep(1.0)

    async def _lag(self) -> None:
        """이벤트 루프가 얼마나 늦게 깨워 주는지 (CPU 경쟁·막는 작업이 있으면 커짐 → 재생 빈틈). 통화당 최대 1만 개."""
        while True:
            t = self.clock()
            await self.sleep(LAG_EVERY)
            if len(self._lags) < 10_000:
                self._lags.append(max(0.0, self.clock() - t - LAG_EVERY))
