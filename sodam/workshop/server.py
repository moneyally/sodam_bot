"""🧪 소담 작업실 서버 — 격리 코드 실행 (설계: 클로드 아티팩트 '소담 작업실 설계', CLAUDE.md '🧪 작업실').

이 파일은 sodam 패키지를 import 하지 않는다: 서버에선 deploy/update.sh 가 /opt/sodam-sandbox/server.py 로 복사해
작업실 전용 venv 로 돈다 (봇 폴더 /opt/sodam 은 작업실 서비스에서 아예 안 보임 — deploy/sodam-workshop.service).

벽 두 겹:
- 바깥: systemd (DynamicUser · PrivateNetwork · ProtectSystem=strict · InaccessiblePaths=/opt/sodam · MemoryMax)
- 안쪽(작업마다): 미리 데운 일꾼을 fork → 새 user·net·mount·pid 네임스페이스 → 세션 폴더만 /tmp/w 로 보이게
  (다른 방 세션·소켓 폴더는 빈 칸으로 덮음) → rlimit(CPU·메모리·파일·프로세스 수) → 실행 → 결과 수거.
  pid 네임스페이스 안이라 작업 코드는 서버를 볼 수도 죽일 수도 없다 (시제품에서 실제로 뚫렸던 것).
격리를 못 걸면 실행하지 않는다 (fail closed). 테스트에서만 WS_ALLOW_NO_ISOLATION=1.

통신: 유닉스 소켓, 한 줄 JSON 요청 → 한 줄 JSON 답.
  요청 {"op":"run","session":"…","code":"…","files":{"이름":b64},"limits":{…}} · {"op":"ping"}
  답   {"status":"ok|error|timeout|cpu|memory|killed|crash|refused","output":"…","files":{"이름":b64},"ms":…}
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import resource
import shutil
import signal
import socket
import sys
import tempfile
import time
import traceback

T0 = time.time()
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")   # fork 전에 수학 라이브러리 스레드가 생기지 않게
os.environ.setdefault("OMP_NUM_THREADS", "1")
WARMED: list[str] = []
for _mod in ("numpy", "pandas", "matplotlib.pyplot", "openpyxl", "duckdb", "PIL.Image"):   # 미리 데우기 (없으면 건너뜀)
    try:
        __import__(_mod)
        WARMED.append(_mod)
    except Exception:   # noqa: BLE001 — 작업실 venv 가 아닌 곳(테스트)에서도 서버는 뜬다
        pass
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    _plt.rcParams["font.family"] = ["NanumGothic", "DejaVu Sans"]   # 한글 차트 (apt fonts-nanum)
    _plt.rcParams["axes.unicode_minus"] = False
except Exception:   # noqa: BLE001
    pass
WARM_SEC = round(time.time() - T0, 2)

BASE = os.environ.get("WS_BASE") or os.path.join(tempfile.gettempdir(), "ws")
MOUNT = os.environ.get("WS_MOUNT") or os.path.join(tempfile.gettempdir(), "w")   # 작업에서 보이는 세션 폴더 자리
HIDE = [p for p in os.environ.get("WS_HIDE", "").split(":") if p]
ALLOW_NO_ISO = os.environ.get("WS_ALLOW_NO_ISOLATION") == "1"
SESSION_TTL = int(os.environ.get("WS_SESSION_TTL", "1200"))   # 방마다 작업 폴더 20분 (OpenAI 컨테이너와 같은 감각)
OUT_EXT = {".png", ".jpg", ".jpeg", ".gif", ".csv", ".xlsx", ".pdf", ".txt", ".json", ".md", ".svg"}
OUT_MAX = 10 * 1024 * 1024
OUT_FILES = 5
TEXT_MAX = 4000
DEFAULT = {"cpu": 10, "wall": 20, "mem_mb": 512, "nproc": 32}
CEIL = {"cpu": 30, "wall": 45, "mem_mb": 1024, "nproc": 64}

CLONE_NEWNS, CLONE_NEWUSER, CLONE_NEWNET, CLONE_NEWPID = 0x00020000, 0x10000000, 0x40000000, 0x20000000
MS_BIND, MS_REC, MS_PRIVATE = 0x1000, 0x4000, 0x40000
libc = ctypes.CDLL(None, use_errno=True)
_sessions: dict[str, float] = {}   # 스레드 없음: 작업은 한 번에 하나씩 (fork 는 단일 스레드에서만 안전 — 하루 사용량이 작아 충분)


def clip(s: str, n: int = TEXT_MAX) -> str:
    """앞뒤를 남기고 가운데를 자름 (코덱스 truncate 와 같은 방식 — 끝의 오류·합계 줄이 살게)."""
    if len(s) <= n:
        return s
    half = n // 2
    return s[:half] + f"\n…{len(s) - n}자 잘림…\n" + s[-half:]


def _limits(raw: dict | None) -> dict:
    out = dict(DEFAULT)
    for k, v in (raw or {}).items():
        if k in CEIL and isinstance(v, (int, float)) and v > 0:
            out[k] = min(int(v), CEIL[k])
    return out


def _session_dir(key: str) -> str:
    safe = "".join(c for c in str(key) if c.isalnum() or c in "-_")[:64] or "anon"
    path = os.path.join(BASE, safe)
    now = time.time()
    for old, ts in list(_sessions.items()):   # 20분 안 쓴 세션 정리
        if now - ts > SESSION_TTL and old != safe:
            shutil.rmtree(os.path.join(BASE, old), ignore_errors=True)
            _sessions.pop(old, None)
    _sessions[safe] = now
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _mount(src: bytes | None, dst: str, fstype: bytes | None, flags: int, data: bytes | None) -> None:
    if libc.mount(src, dst.encode(), fstype, flags, data) != 0:
        raise OSError(ctypes.get_errno(), f"mount {dst}")


def isolate(work: str, lim: dict) -> bool:
    """작업 자식에서: 네임스페이스 + 세션 폴더만 보이게 + 한도. 성공하면 True (테스트 허용 모드에선 실패해도 False 로 진행)."""
    uid, gid = os.getuid(), os.getgid()
    iso = libc.unshare(CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWPID) == 0
    if iso:
        for name, text in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
            with open(f"/proc/self/{name}", "w") as f:
                f.write(text)
        _mount(b"none", "/", None, MS_REC | MS_PRIVATE, None)
        os.makedirs(MOUNT, exist_ok=True)
        _mount(work.encode(), MOUNT, None, MS_BIND | MS_REC, None)     # 이 방 세션만
        for p in [BASE, os.path.dirname(os.environ.get("WS_SOCKET", "")) or "", *HIDE]:
            if p and os.path.isdir(p) and not os.path.realpath(MOUNT).startswith(os.path.realpath(p) + os.sep):
                _mount(b"tmpfs", p, b"tmpfs", 0, b"size=4k,mode=000")   # 다른 방 세션·소켓·숨길 폴더 = 빈 칸
        os.chdir(MOUNT)
    elif not ALLOW_NO_ISO:
        raise PermissionError("격리를 못 걸어서 실행 안 함 (user namespace 차단 — AppArmor 프로필 확인)")
    else:
        os.chdir(work)
    resource.setrlimit(resource.RLIMIT_CPU, (lim["cpu"], lim["cpu"] + 1))
    mem = lim["mem_mb"] * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUT_MAX, OUT_MAX))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if iso:   # 같은 사용자 전체에 걸리므로 격리됐을 때만 (네임스페이스 안 사용자 = 작업 하나)
        resource.setrlimit(resource.RLIMIT_NPROC, (lim["nproc"],) * 2)
    home = os.getcwd()
    os.environ.clear()
    os.environ.update({"HOME": home, "MPLCONFIGDIR": os.path.join(home, ".mpl"), "LANG": "C.UTF-8",
                       "OPENBLAS_NUM_THREADS": "1", "MPLBACKEND": "Agg", "TMPDIR": home})
    return iso


def _child(job: dict, work: str, wfd: int, lim: dict) -> None:
    out = io.StringIO()
    status = "ok"
    before: dict = {}
    try:
        iso = isolate(work, lim)
        if iso:   # pid 네임스페이스는 다음 자식부터 → 한 번 더 fork (안쪽은 PID 1, 서버가 안 보임)
            inner = os.fork()
            if inner != 0:
                os.close(wfd)
                _, st = os.waitpid(inner, 0)
                os._exit(128 + os.WTERMSIG(st) if os.WIFSIGNALED(st) else 0)
        for name, b64 in (job.get("files") or {}).items():
            with open(os.path.basename(str(name)) or "input", "wb") as f:
                f.write(base64.b64decode(b64))
        before.update(_stat_all())
        sys.stdout = sys.stderr = out
        exec(compile(str(job.get("code", "")), "<소담 코드>", "exec"), {"__name__": "__main__"})
    except MemoryError:
        status = "memory"
    except PermissionError as e:
        status, out = "refused", io.StringIO(str(e))
    except BaseException:   # noqa: BLE001 — 사용자 코드 오류는 모델에게 그대로 (고쳐서 다시)
        status = "error"
        out.write(traceback.format_exc(limit=4))
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    files = {}
    try:
        given = {os.path.basename(str(n)) for n in (job.get("files") or {})}
        for name in sorted(os.listdir(".")):
            if len(files) >= OUT_FILES or name in given or name.startswith("."):
                continue
            ok = os.path.splitext(name)[1].lower() in OUT_EXT and os.path.isfile(name)
            if ok and os.path.getsize(name) <= OUT_MAX and _stat(name) != before.get(name):   # 이번에 만든·바꾼 것만
                with open(name, "rb") as f:
                    files[name] = base64.b64encode(f.read()).decode()
    except OSError:
        pass
    os.write(wfd, json.dumps({"status": status, "output": clip(out.getvalue()), "files": files}).encode())
    os._exit(0)


def _stat(name: str):
    st = os.stat(name)
    return st.st_mtime_ns, st.st_size


def _stat_all() -> dict:
    out = {}
    for name in os.listdir("."):
        try:
            out[name] = _stat(name)
        except OSError:
            pass
    return out


def _why(st: int) -> str:
    sig = os.WTERMSIG(st) if os.WIFSIGNALED(st) else (os.WEXITSTATUS(st) - 128 if os.WEXITSTATUS(st) > 128 else 0)
    if not sig:
        return "crash"
    return {signal.SIGXCPU: "cpu", signal.SIGKILL: "killed", signal.SIGSEGV: "crash",
            signal.SIGXFSZ: "error"}.get(sig, f"signal {sig}")


def run(job: dict) -> dict:
    t0 = time.time()
    lim = _limits(job.get("limits"))
    work = _session_dir(job.get("session") or "anon")
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.setsid()
        try:
            _child(job, work, w, lim)
        finally:
            os._exit(1)
    os.close(w)
    buf, status, deadline = b"", None, t0 + lim["wall"]
    os.set_blocking(r, False)
    while True:
        try:
            chunk = os.read(r, 1 << 20)
            if not chunk:
                break
            buf += chunk
            continue
        except BlockingIOError:
            pass
        if time.time() > deadline:
            status = "timeout"
            break
        time.sleep(0.005)
    try:
        os.killpg(pid, signal.SIGKILL)   # 남은 손자까지 그룹째 (코덱스: 파이프 잡은 손자 때문에 멈추지 않게)
    except ProcessLookupError:
        pass
    _, st = os.waitpid(pid, 0)
    os.close(r)
    if status is None and buf:
        try:
            res = json.loads(buf)
        except ValueError:
            res = {"status": "crash", "output": "", "files": {}}
    else:
        res = {"status": status or _why(st), "output": "", "files": {}}
    res["ms"] = round((time.time() - t0) * 1000, 1)
    return res


def handle(conn: socket.socket) -> None:
    with conn:
        conn.settimeout(60)
        data = b""
        while not data.endswith(b"\n") and len(data) < 64 * 1024 * 1024:
            chunk = conn.recv(1 << 20)
            if not chunk:
                break
            data += chunk
        try:
            job = json.loads(data)
        except ValueError:
            conn.sendall(b'{"status":"refused","output":"bad request"}\n')
            return
        if job.get("op") == "ping":
            res = {"status": "ok", "warm": WARMED, "warm_sec": WARM_SEC, "sessions": len(_sessions)}
        else:
            res = run(job)
        conn.sendall(json.dumps(res).encode() + b"\n")


def serve(path: str) -> None:
    os.environ["WS_SOCKET"] = path
    os.makedirs(BASE, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o666)   # 봇(sodam 사용자)이 연결 — 작업실 서비스는 DynamicUser
    s.listen(8)
    print(json.dumps({"ready": True, "warm": WARMED, "warm_sec": WARM_SEC}), flush=True)
    while True:
        conn, _ = s.accept()
        try:
            handle(conn)
        except Exception as e:   # noqa: BLE001 — 요청 하나가 서버를 멈추지 않게
            print(json.dumps({"error": repr(e)[:200]}), flush=True)


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else "/run/sodam-workshop/sock")
