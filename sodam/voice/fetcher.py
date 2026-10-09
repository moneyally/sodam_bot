"""🎵 노래 찾기·받기를 다른 프로그램(자식 프로세스)에서 — 소리 보내는 쪽(Player 박자)이 막히지 않게.

왜 (서버 실측 2026-10-09, 가짜 노래 일꾼 하네스 — 0.01초 박자로 1분 동안 조각 6,000개 보내며 늦음을 셈):
  서버 시험만 돌 때               → 30ms 넘게 늦음 0번 (노래 쪽 우선순위가 높아서 안 밀림)
  + 같은 프로그램 스레드에서 받기  → 1분에 6~10번, 최대 0.06초 (처음 불러올 땐 0.49초) — 파이썬은 한 번에 한 줄만
                                     (GIL) 이라 받기 일이 박자를 붙잡음. 막히면 쿠키·대체 음원까지 줄줄이 → 실제 0.23초
  + 다른 프로그램에서 받기         → 0번, 최대 0.007초
그래서 `music.Source` 의 무거운 일(resolve·pick·playlist·mix_for·related·lyrics·fetch·fallback·alt_for)은 여기서
자식 프로그램에 맡기고, 부르는 쪽(asyncio.to_thread)은 그대로 — 스레드는 결과를 기다리기만(파이프 읽기는 GIL 을 놓음).

- 자식은 `python -m sodam.voice.fetcher` 2개(SLOTS), 각자 Source 하나를 오래 들고 있음(쿠키·막힘 기억). 낮은 우선순위(nice 5).
- 주고받기 = 길이(4바이트) + pickle. 자식의 표준 출력은 받기 라이브러리가 뭘 찍어도 섞이지 않게 표준 오류로 돌림.
- 막힘 시각(blocked_at)은 부를 때마다 주고받아 두 자식·부모가 같이 앎 → `primary_ok()` 는 부모에서 바로 (이벤트 루프에서 불림).
- 한 번 일이 TIMEOUT 넘거나 자식이 죽으면 그 자식을 죽이고 다음에 새로 띄움 → MusicError("download").
"""
from __future__ import annotations

import os
import pickle
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from . import music

SLOTS = 2                    # 동시에 도는 자식 수 (미리 받기 + 다른 방 신청)
TIMEOUT = 300.0              # 한 번 일의 상한 (분위기 신청 mix_for 가 가장 김 ~20초)
METHODS = ("resolve", "pick", "playlist", "mix_for", "related", "lyrics", "fetch", "fallback", "alt_for", "loudness")
FACTORY = "sodam.voice.music:Source"


def _send(f, obj) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    f.write(struct.pack(">I", len(data)) + data)
    f.flush()


def _recv(f):
    head = f.read(4)
    if len(head) < 4:
        raise EOFError("fetcher closed")
    n = struct.unpack(">I", head)[0]
    data = f.read(n)
    if len(data) < n:
        raise EOFError("fetcher closed")
    return pickle.loads(data)


class _Slot:
    """자식 하나. 한 번에 일 하나 (lock)."""

    def __init__(self, data_dir: str, factory: str, env: dict | None):
        self.data_dir, self.factory, self.env = data_dir, factory, env
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def _start(self) -> None:
        env = dict(os.environ)
        env.update(self.env or {})
        root = str(Path(__file__).resolve().parents[2])        # 저장소 맨 위 (sodam 패키지가 있는 곳)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.proc = subprocess.Popen([sys.executable, "-m", "sodam.voice.fetcher", self.data_dir, self.factory],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=root)

    def kill(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()
            try:
                p.wait(5)
            except Exception:
                pass

    def call(self, method: str, args: tuple, kwargs: dict, blocked_at: float, timeout: float):
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        p = self.proc
        try:
            _send(p.stdin, (method, args, kwargs, blocked_at))
        except (BrokenPipeError, OSError):
            self.kill()
            raise music.MusicError("download", "노래 받는 쪽이 잠깐 멈췄어요. 다시 신청해 주세요.")
        box: dict = {}

        def read():
            try:
                box["r"] = _recv(p.stdout)
            except Exception as e:
                box["e"] = e
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or "e" in box:                         # 시간 초과·자식 죽음 → 죽이고 다음에 새로
            self.kill()
            t.join(1)
            raise music.MusicError("download", "노래 받기가 너무 오래 걸렸어요. 다시 신청해 주세요.")
        return box["r"]


class ProcSource:
    """music.Source 대신 쓰는 대리인 — 같은 메서드, 일은 자식 프로그램이."""

    def __init__(self, data_dir: Path, *, slots: int = SLOTS, timeout: float = TIMEOUT,
                 factory: str = FACTORY, env: dict | None = None):
        self.local = music.Source(Path(data_dir))              # 가벼운 것(primary_ok·cookies)만 부모에서
        self.timeout = timeout
        self._slots = [_Slot(str(data_dir), factory, env) for _ in range(max(1, slots))]
        self._pick = threading.Lock()

    def __getattr__(self, name: str):
        if name in METHODS:
            return lambda *a, **k: self._call(name, a, k)
        return getattr(self.local, name)

    def primary_ok(self) -> bool:
        return self.local.primary_ok()

    def cookies(self) -> list[str]:
        return self.local.cookies()

    def _slot(self) -> _Slot:
        with self._pick:                                       # 놀고 있는 자식 먼저, 다 바쁘면 첫 자식을 기다림
            for s in self._slots:
                if s.lock.acquire(blocking=False):
                    return s
        s = self._slots[0]
        s.lock.acquire()
        return s

    def _call(self, method: str, args: tuple, kwargs: dict) -> Any:
        s = self._slot()
        try:
            ok, value, blocked_at = s.call(method, args, kwargs, self.local.blocked_at, self.timeout)
        finally:
            s.lock.release()
        self.local.blocked_at = max(self.local.blocked_at, float(blocked_at or 0))
        if ok:
            return value
        raise value

    def close(self) -> None:
        for s in self._slots:
            s.kill()


def _child(data_dir: str, factory: str) -> None:
    proto_out = os.fdopen(os.dup(1), "wb")                     # 주고받기 전용 통로
    os.dup2(2, 1)                                              # 받기 라이브러리가 찍는 글은 표준 오류로
    proto_in = sys.stdin.buffer
    try:
        os.nice(5)                                             # 소리 쪽보다 낮게
    except OSError:
        pass
    mod, _, cls = factory.partition(":")
    __import__(mod)
    src = getattr(sys.modules[mod], cls)(Path(data_dir))
    while True:
        try:
            method, args, kwargs, blocked_at = _recv(proto_in)
        except EOFError:
            return
        if hasattr(src, "blocked_at"):
            src.blocked_at = max(float(getattr(src, "blocked_at", 0) or 0), float(blocked_at or 0))
        try:
            res = (True, getattr(src, method)(*args, **kwargs))
        except (music.MusicError, music.MusicChoice) as e:
            res = (False, e)
        except Exception as e:                                 # 모르는 오류는 글자로만 (자식 쪽 클래스를 부모가 못 열 수도)
            res = (False, RuntimeError(f"{type(e).__name__}: {e}"[:500]))
        _send(proto_out, (*res, getattr(src, "blocked_at", 0.0)))


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else FACTORY)
