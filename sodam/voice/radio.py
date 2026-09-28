"""📡 방송 모드 — 텔레그램 계정 없이 음성채팅에 소담 목소리 내보내기 (RTMP).

왜: 봇 계정은 음성채팅에 못 들어감 (core.telegram.org/method/phone.joinGroupCall "Only users can use this method",
봇 토큰이면 400 BOT_METHOD_INVALID — MarshalX/tgcalls#188). 도우미 사람 계정이 없을 때의 길:
관리자가 음성채팅을 '다른 앱으로 방송(Stream with…)' 으로 열고 서버 URL·스트림 키를 한 번 복사해 주면,
서버의 ffmpeg 가 그 RTMP 로 소담 목소리를 보낸다 (계정 불필요). RTMP 방송은 한 방향이라 멤버는 음성채팅에서 말 못 함
(core.telegram.org/api/group-calls: "other participants cannot publish") → 멤버는 채팅 '소담아 …' 나 음성메시지로 말하고
소담은 음성채팅에서 목소리로 답한다 (panels/voice.py 가 AI 답을 radio_say 로, 음성메시지는 받아쓰기).

ffmpeg: 영상 = 단색 화면(2fps, -re 로 실시간), 소리 = stdin 24 kHz 모노 PCM 을 우리가 20 ms 마다 씀(말 없으면 무음) → AAC → FLV.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)

RATE = 24_000
CHUNK_MS = 20
CHUNK = RATE * 2 * CHUNK_MS // 1000          # 960 바이트
SILENCE = bytes(CHUNK)
MAX_QUEUE_SEC = 120                          # 말이 이만큼 밀리면 오래된 것 버림


def ffmpeg_cmd(target: str) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-re", "-f", "lavfi", "-i", "color=c=0x1b1f2a:s=640x360:r=2",
            "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-g", "4", "-b:v", "120k",
            "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
            "-f", "flv", target]


def target_of(url: str, key: str) -> str:
    """텔레그램 앱이 주는 '서버 URL'(rtmps://dc…/s/) + '스트림 키' → 한 주소."""
    url = url.strip()
    return url + ("" if url.endswith("/") else "/") + key.strip()


class Radio:
    def __init__(self, target: str, *, spawn: Callable | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable = asyncio.sleep, max_sec: float = 3600, idle_sec: float = 600):
        self.target = target
        self.spawn = spawn or (lambda cmd: asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE))
        self.clock, self.sleep = clock, sleep
        self.max_sec, self.idle_sec = max_sec, idle_sec
        self.proc: Any = None
        self.q: deque[bytes] = deque(maxlen=MAX_QUEUE_SEC * 1000 // CHUNK_MS)
        self.done = asyncio.Event()
        self.reason = ""
        self.spoken_sec = 0.0
        self._t0 = self._last = 0.0

    async def start(self) -> None:
        self.proc = await self.spawn(ffmpeg_cmd(self.target))
        self._t0 = self._last = self.clock()

    def say(self, pcm24: bytes) -> None:
        """TTS 로 만든 24 kHz 모노 PCM 을 줄에 (다음 말은 앞 말 끝난 뒤)."""
        for i in range(0, len(pcm24), CHUNK):
            self.q.append(pcm24[i:i + CHUNK].ljust(CHUNK, b"\0"))
        self.spoken_sec += len(pcm24) / (RATE * 2)
        self._last = self.clock()

    def stop(self, reason: str) -> None:
        if not self.done.is_set():
            self.reason = reason
            self.done.set()

    async def run(self) -> str:
        """끝날 때까지 20 ms 마다 소리를 씀. 돌려주는 값 = 끝난 이유."""
        step = CHUNK_MS / 1000
        nxt = self.clock()
        try:
            while not self.done.is_set():
                if getattr(self.proc, "returncode", None) is not None:
                    err = b""
                    try:
                        err = await asyncio.wait_for(self.proc.stderr.read(), 1) if self.proc.stderr else b""
                    except Exception:
                        pass
                    log.warning("방송 ffmpeg 끝남 %s: %s", self.proc.returncode, err[-300:])
                    self.stop("error:stream")
                    break
                now = self.clock()
                if now - self._t0 >= self.max_sec:
                    self.stop("time")
                    break
                if not self.q and now - self._last >= self.idle_sec:
                    self.stop("idle")
                    break
                chunk = self.q.popleft() if self.q else SILENCE
                if chunk is not SILENCE:
                    self._last = now
                try:
                    self.proc.stdin.write(chunk)
                    await self.proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    self.stop("error:stream")
                    break
                nxt += step
                wait = nxt - self.clock()
                if wait > 0:
                    await self.sleep(wait)
                elif wait < -0.5:
                    nxt = self.clock()
        finally:
            await self.close()
        return self.reason

    async def close(self) -> None:
        p = self.proc
        if not p or getattr(p, "returncode", None) is not None:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(p.wait(), 5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
