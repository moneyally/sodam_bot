"""🔐 SSH 웹소켓 다리 (별도 프로세스 sodam-sshws, 127.0.0.1:8023) — 클로드 작업 환경은 밖으로 HTTPS(443)만 나가서 22번 SSH 가 막힘.
Caddy https://<IP>.sslip.io/sshws → 여기 → 127.0.0.1:22 (바이트 그대로). 오너 결정 2026-10-01 ('지금 방식 너무 불편').

지키는 것 (3중):
1. 웹소켓 여는 요청에 원격 점검 창구 토큰 (Authorization: Bearer <data/diag.token>) — 틀리면 IP 당 10분 10번까지, 전체 분당 60번.
2. sshd: 127.0.0.1 에서 온 접속(= 이 다리)은 비밀번호 로그인 금지 (/etc/ssh/sshd_config.d/60-sodam-sshws.conf, update.sh 가 설치).
3. 클로드 키(deploy/claude_ssh.pub)는 authorized_keys 에 from="127.0.0.1,::1" 로만 → 인터넷에서 22번으로 그 키로는 못 들어옴.
끄기: 오너가 서버에서 `systemctl disable --now sodam-sshws` (또는 이 파일·유닛을 지우고 배포).
접속 기록: data/sshws_access.log (시각·IP·결과, 토큰 X).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger("sodam.sshws")

HOST, PORT = "127.0.0.1", int(os.getenv("SSHWS_PORT", "8023"))
SSH_HOST, SSH_PORT = "127.0.0.1", 22
PATH = "/sshws"
FAIL_PER_IP, FAIL_WINDOW = 10, 600
ALL_PER_MIN = 60
MAX_SESSIONS = 4
CHUNK = 65536
ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    db = os.getenv("DB_PATH", "data/sodam.db")
    p = Path(db)
    return (p if p.is_absolute() else ROOT / p).resolve().parent


def read_token() -> str:
    try:
        return (data_dir() / "diag.token").read_text().strip()
    except OSError:
        return ""


class Gate:
    """토큰 검사 + 실패 제한 (IP 당·전체)."""

    def __init__(self, token_fn=read_token, now=time.monotonic):
        self.token_fn, self.now = token_fn, now
        self.fails: dict[str, deque] = defaultdict(deque)
        self.recent: deque = deque()

    def check(self, ip: str, auth: str) -> str | None:
        """None = 통과, 아니면 거절 이유."""
        t = self.now()
        while self.recent and t - self.recent[0] > 60:
            self.recent.popleft()
        if len(self.recent) >= ALL_PER_MIN:
            return "too_many"
        self.recent.append(t)
        f = self.fails[ip]
        while f and t - f[0] > FAIL_WINDOW:
            f.popleft()
        if len(f) >= FAIL_PER_IP:
            return "locked"
        tok = self.token_fn()
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not tok or not given or not hmac.compare_digest(tok.encode(), given.encode()):
            f.append(t)
            return "bad_token"
        return None


def _audit(ip: str, result: str) -> None:
    try:
        with open(data_dir() / "sshws_access.log", "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} {result}\n")
    except OSError:
        pass


async def _pump_ws_to_tcp(ws, writer) -> None:
    try:
        async for data in ws:
            if isinstance(data, str):
                data = data.encode()
            writer.write(data)
            await writer.drain()
    except Exception:   # 끊김(닫기 프레임 없이 끊은 클라이언트 등) = 세션 끝
        pass
    finally:
        writer.close()


async def _pump_tcp_to_ws(reader, ws) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            await ws.send(data)
    except Exception:
        pass
    finally:
        await ws.close()


def make_handler(gate: Gate, ssh_host: str = SSH_HOST, ssh_port: int = SSH_PORT):
    active = {"n": 0}

    async def handler(ws) -> None:
        req = ws.request
        ip = (req.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or str(ws.remote_address[0])
        if req.path.split("?")[0] != PATH:
            _audit(ip, "bad_path")
            await ws.close(1008, "not found")
            return
        why = gate.check(ip, req.headers.get("Authorization", ""))
        if why or active["n"] >= MAX_SESSIONS:
            _audit(ip, why or "busy")
            await ws.close(1008, "denied")
            return
        active["n"] += 1
        _audit(ip, "open")
        try:
            reader, writer = await asyncio.open_connection(ssh_host, ssh_port)
            tasks = [asyncio.create_task(_pump_ws_to_tcp(ws, writer)), asyncio.create_task(_pump_tcp_to_ws(reader, ws))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            writer.close()
        except Exception as e:   # 연결 오류는 그 세션만
            log.info("sshws 세션 끝: %s", e)
        finally:
            active["n"] -= 1
            _audit(ip, "close")
    return handler


async def serve(host: str = HOST, port: int = PORT) -> None:
    from websockets.asyncio.server import serve as ws_serve
    gate = Gate()
    async with ws_serve(make_handler(gate), host, port, max_size=2**20, ping_interval=20):
        log.info("sshws 다리: ws://%s:%s%s → %s:%s", host, port, PATH, SSH_HOST, SSH_PORT)
        await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())
