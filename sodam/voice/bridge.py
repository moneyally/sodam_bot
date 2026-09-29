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
MAX_OUT = 600                                   # 음성 출력 토큰 ≈ 초당 30 (실측 1,070토큰/33초) → 약 20초 (400 은 도구 결과 설명이 문장 중간에 잘림 — 실측)
BYE = re.compile(r"소담.{0,6}(나가|끊어|그만|잘\s*가|바이|종료)")


@dataclass
class Result:
    reason: str = ""
    seconds: float = 0.0
    user_turns: int = 0
    bot_turns: int = 0
    usage: dict = field(default_factory=dict)        # response.done usage 합 (input/output 토큰)


# 영상대화 중 쓸 수 있는 도구 (worker 가 실제 실행 함수를 넣음 — 채팅 소담의 격리 웹 검색과 같은 것)
WEB_SEARCH = {"type": "function", "name": "web_search",
              "description": "날씨·뉴스·시세·경기 결과·영업시간처럼 최신 정보가 필요할 때 인터넷 검색. 부르기 전에 '잠깐만요, 찾아볼게요' 한마디.",
              "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "검색어 (한국어 가능)"}},
                             "required": ["query"]}}
TOOL_TIMEOUT = 15


SPEAKER_SHARE = 0.7        # 한 사람 소리가 이만큼 넘어야 '그 사람 말' (여럿이 섞이면 모름 → 쓰기 도구 안 됨)


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
        self.out: deque[bytes] = deque()                # 들려줄 48 kHz 10 ms 조각
        self.item: str | None = None                    # 지금 들려주는 모델 답 item_id
        self.played_ms = 0                              # 그 item 에서 실제로 play 한 ms
        self._inbuf = bytearray()
        self._sendq: deque[bytes] = deque(maxlen=MAX_BACKLOG)
        self._send_evt = asyncio.Event()
        self._done = asyncio.Event()
        self.result = Result()
        self.transcript: list[tuple[str, str]] = []     # (who, text) — 저장은 on_line 이 (worker → voice_lines 7일)
        self.on_line = on_line                          # (who, text, ssrc) — user·sodam·tool
        self._item_ssrc: dict[str, int | None] = {}     # 말 item → 그 말의 주인 ssrc (받아쓰기는 늦게 옴)
        self._t0 = self._last_voice = 0.0
        self._errors = 0
        self._last_reply = -1e9
        self._speaking = False
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
                specs = self.tool_specs if self.tool_specs is not None else ([WEB_SEARCH] if "web_search" in self.tools else [])
                await conn.session.update(session=session_config(self.instructions, self.voice, self.reply, tools=specs,
                                                               transcribe_prompt=self.transcribe_prompt))
                if self.greet:   # response.instructions 는 세션 지시를 '대신'함 → 캐릭터를 같이 넣음
                    await conn.response.create(response={"instructions": f"{self.instructions}\n\n{self.greet}"})
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
            self._speaking, self._energy = True, {}
            await self._interrupt()
        elif t == "input_audio_buffer.speech_stopped":
            self._speaking = False
            self.speaker = dominant(self._energy)
            if getattr(ev, "item_id", None) and len(self._item_ssrc) < 500:
                self._item_ssrc[ev.item_id] = self.speaker
        elif t == "conversation.item.input_audio_transcription.completed":
            text = (ev.transcript or "").strip()
            if not text:
                return
            self._last_voice = self.clock()
            self.transcript.append(("user", text))
            self._emit("user", text, self._item_ssrc.pop(getattr(ev, "item_id", None), self.speaker))
            self.result.user_turns += 1
            if BYE.search(text):
                self.stop("bye")
            elif self.reply == "name" and ("소담" in text or self.clock() - self._last_reply < 20):
                await self.conn.response.create()        # '소담' 을 불렀거나 방금 이어지던 대화
        elif t == "response.output_audio_transcript.done":
            self.transcript.append(("sodam", ev.transcript or ""))
            self._emit("sodam", ev.transcript or "", None)
            self.result.bot_turns += 1
            self._last_reply = self._last_voice = self.clock()
        elif t == "response.function_call_arguments.done":
            meta = {"ssrc": self.speaker, "response_id": getattr(ev, "response_id", None)}
            asyncio.create_task(self._call_tool(ev.call_id, ev.name, ev.arguments, meta))   # 듣기·말하기는 계속
        elif t == "response.done":
            usage = getattr(getattr(ev, "response", None), "usage", None)
            for k in ("input_tokens", "output_tokens"):
                self.result.usage[k] = self.result.usage.get(k, 0) + int(getattr(usage, k, 0) or 0)
            det = getattr(usage, "input_token_details", None)        # 앞 턴을 다시 읽는 부분은 자동 캐시 (할인)
            self.result.usage["cached_tokens"] = (self.result.usage.get("cached_tokens", 0)
                                                  + int(getattr(det, "cached_tokens", 0) or 0))
        elif t == "error":
            self._errors += 1
            log.warning("Realtime 오류: %s", getattr(getattr(ev, "error", None), "message", ev))
            if self._errors >= MAX_ERRORS:
                self.stop("error:realtime")

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
            await self.conn.response.create()
        except Exception as e:
            log.warning("도구 결과 전달 실패: %s", e)

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
