"""OpenAI Realtime ↔ 텔레그램 음성채팅 다리.

듣기:   통화에서 받은 48 kHz 조각(feed) → 24 kHz 로 → 100 ms 씩 input_audio_buffer.append
말하기: response.output_audio.delta(24 kHz) → 48 kHz 10 ms 조각 줄 → 페이서가 절대 시각으로 10 ms 마다 play(조각) (말 없으면 무음)
끊기:   input_audio_buffer.speech_started(누가 말 시작) → 줄 비우고 conversation.item.truncate(지금까지 실제로 들려준 ms)
        → 모델이 '끝까지 말했다'고 착각하지 않음 (OpenAI Realtime 문서의 WebSocket 끊기 방식). 응답 취소는 서버 VAD interrupt_response.
한도:   max_sec 넘거나, idle_sec 동안 아무도(소담 포함) 말 안 하면 끝. '소담아 나가/끊어' 도 끝.

연결(connect)·재생(play)·시계는 밖에서 넣는다 → 테스트는 가짜로, 실제 통화는 worker.py 가 py-tgcalls 로.
"""
from __future__ import annotations

import asyncio
import base64
import logging
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
MAX_ERRORS = 5
BYE = re.compile(r"소담.{0,6}(나가|끊어|그만|잘\s*가|바이|종료)")


@dataclass
class Result:
    reason: str = ""
    seconds: float = 0.0
    user_turns: int = 0
    bot_turns: int = 0
    usage: dict = field(default_factory=dict)        # response.done usage 합 (input/output 토큰)


def session_config(instructions: str, voice: str, reply: str = "all", language: str = "ko") -> dict:
    """session.update 에 넣을 설정. reply=all 이면 말 끝날 때마다 답, name 이면 '소담' 이 들어간 말에만 (코드가 response.create)."""
    return {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": audio.AI_RATE},
                "noise_reduction": {"type": "far_field"},           # 여러 사람 폰 마이크
                "transcription": {"model": "gpt-4o-mini-transcribe", "language": language},
                "turn_detection": {"type": "server_vad", "threshold": 0.6, "prefix_padding_ms": 300,
                                   "silence_duration_ms": 700, "create_response": reply == "all",
                                   "interrupt_response": True},
            },
            "output": {"format": {"type": "audio/pcm", "rate": audio.AI_RATE}, "voice": voice},
        },
    }


class Bridge:
    def __init__(self, connect: Callable[[], Any], play: Callable[[bytes], Awaitable[Any]], *, instructions: str,
                 voice: str = "marin", reply: str = "all", greet: str | None = None, max_sec: float = 900,
                 idle_sec: float = 60, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self._connect, self._play = connect, play
        self.instructions, self.voice, self.reply, self.greet = instructions, voice, reply, greet
        self.max_sec, self.idle_sec = max_sec, idle_sec
        self.clock, self.sleep = clock, sleep
        self.conn = None
        self.out: deque[bytes] = deque()                # 들려줄 48 kHz 10 ms 조각
        self.item: str | None = None                    # 지금 들려주는 모델 답 item_id
        self.played_ms = 0                              # 그 item 에서 실제로 play 한 ms
        self._inbuf = bytearray()
        self._sendq: deque[bytes] = deque(maxlen=MAX_BACKLOG)
        self._send_evt = asyncio.Event()
        self._done = asyncio.Event()
        self.result = Result()
        self.transcript: list[tuple[str, str]] = []     # (who, text) — 메모리만, 저장 안 함
        self._t0 = self._last_voice = 0.0
        self._errors = 0
        self._last_reply = -1e9

    # ── 통화 쪽에서 부름 ──────────────────────────────────
    def feed(self, frames48: list[bytes]) -> None:
        """통화에서 받은 사람들 소리 (같은 10 ms 의 여러 사람 → 섞음). 동기 — py-tgcalls 콜백에서 바로."""
        if self._done.is_set():
            return
        pcm = audio.down(audio.mix(frames48))
        if not pcm:
            return
        self._inbuf += pcm
        while len(self._inbuf) >= SEND_BYTES:
            self._sendq.append(bytes(self._inbuf[:SEND_BYTES]))
            del self._inbuf[:SEND_BYTES]
            self._send_evt.set()

    def stop(self, reason: str) -> None:
        if not self._done.is_set():
            self.result.reason = reason
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    # ── 실행 ────────────────────────────────────────────
    async def run(self) -> Result:
        self._t0 = self._last_voice = self.clock()
        try:
            async with self._connect() as conn:
                self.conn = conn
                await conn.session.update(session=session_config(self.instructions, self.voice, self.reply))
                if self.greet:
                    await conn.response.create(response={"instructions": self.greet})
                tasks = [asyncio.create_task(f()) for f in (self._reader, self._sender, self._pacer, self._watch)]
                await self._done.wait()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:                          # 연결 실패·끊김 — 통화는 worker 가 정리
            log.warning("음성 연결 끝남: %s", e)
            self.stop(f"error:{type(e).__name__}")
        self.result.seconds = self.clock() - self._t0
        return self.result

    async def _reader(self) -> None:
        try:
            async for ev in self.conn:
                await self.on_event(ev)
                if self._done.is_set():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("음성 읽기 오류: %s", e)
        self.stop(self.result.reason or "closed")

    async def on_event(self, ev) -> None:
        t = getattr(ev, "type", "")
        if t == "response.output_audio.delta":
            if ev.item_id != self.item:
                self.item, self.played_ms = ev.item_id, 0
            pcm = audio.up(base64.b64decode(ev.delta))
            for i in range(0, len(pcm), audio.FRAME_BYTES):
                self.out.append(pcm[i:i + audio.FRAME_BYTES].ljust(audio.FRAME_BYTES, b"\0"))
            self._last_voice = self.clock()
        elif t == "input_audio_buffer.speech_started":
            self._last_voice = self.clock()
            await self._interrupt()
        elif t == "conversation.item.input_audio_transcription.completed":
            text = (ev.transcript or "").strip()
            if not text:
                return
            self._last_voice = self.clock()
            self.transcript.append(("user", text))
            self.result.user_turns += 1
            if BYE.search(text):
                self.stop("bye")
            elif self.reply == "name" and ("소담" in text or self.clock() - self._last_reply < 20):
                await self.conn.response.create()        # '소담' 을 불렀거나 방금 이어지던 대화
        elif t == "response.output_audio_transcript.done":
            self.transcript.append(("sodam", ev.transcript or ""))
            self.result.bot_turns += 1
            self._last_reply = self._last_voice = self.clock()
        elif t == "response.done":
            usage = getattr(getattr(ev, "response", None), "usage", None)
            for k in ("input_tokens", "output_tokens"):
                self.result.usage[k] = self.result.usage.get(k, 0) + int(getattr(usage, k, 0) or 0)
        elif t == "error":
            self._errors += 1
            log.warning("Realtime 오류: %s", getattr(getattr(ev, "error", None), "message", ev))
            if self._errors >= MAX_ERRORS:
                self.stop("error:realtime")

    async def _interrupt(self) -> None:
        """누가 말을 시작함 → 들려주던 답을 멈추고, 모델 쪽 기록도 실제로 들려준 데까지 자름."""
        if not self.out or not self.item:
            return
        self.out.clear()
        try:
            await self.conn.conversation.item.truncate(item_id=self.item, content_index=0, audio_end_ms=self.played_ms)
        except Exception as e:                          # 이미 끝난 item 등 — 끊기는 됐으니 무시
            log.debug("truncate 실패: %s", e)

    async def _sender(self) -> None:
        while True:
            await self._send_evt.wait()
            self._send_evt.clear()
            while self._sendq:
                chunk = self._sendq.popleft()
                await self.conn.input_audio_buffer.append(audio=base64.b64encode(chunk).decode())

    async def _pacer(self) -> None:
        """10 ms 마다 한 조각 — 절대 시각 기준이라 느려진 만큼 다음에 덜 잠 (많이 밀리면 다시 맞춤)."""
        step = audio.FRAME_MS / 1000
        nxt = self.clock()
        while True:
            if self.out:
                frame = self.out.popleft()
                self.played_ms += audio.FRAME_MS
            else:
                frame = SILENCE
            try:
                await self._play(frame)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("재생 실패: %s", e)
                self.stop("error:play")
                return
            nxt += step
            wait = nxt - self.clock()
            if wait > 0:
                await self.sleep(wait)
            elif wait < -0.2:
                nxt = self.clock()

    async def _watch(self) -> None:
        while True:
            now = self.clock()
            if now - self._t0 >= self.max_sec:
                return self.stop("time")
            if now - self._last_voice >= self.idle_sec and not self.out:
                return self.stop("idle")
            await self.sleep(1.0)
