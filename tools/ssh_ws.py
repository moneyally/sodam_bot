"""🔐 클로드 작업 환경 → 서버 SSH (웹소켓 다리 sodam/sshws.py 경유, 22번이 막힌 곳에서).

    python tools/ssh_ws.py "명령"                 # 명령 하나 실행, 출력·종료 코드
    python tools/ssh_ws.py --put 로컬 원격경로      # 파일 올리기 (sftp)
    python tools/ssh_ws.py --get 원격경로 로컬      # 파일 받기

필요한 것 (환경변수): SODAM_DIAG_TOKEN (다리 문 열기) · SODAM_SSH_KEY (클로드 전용 ed25519 개인키 PEM 전체, 또는 파일 경로)
주소: SODAM_DIAG_URL (기본 https://178-104-55-232.sslip.io) + /sshws. 사용자: SODAM_SSH_USER (기본 root).
설치: pip install paramiko websockets (venv).
"""
from __future__ import annotations

import asyncio
import io
import os
import socket
import ssl
import sys
import threading

URL = (os.getenv("SODAM_DIAG_URL", "https://178-104-55-232.sslip.io").rstrip("/").replace("https://", "wss://", 1)
       .replace("http://", "ws://", 1) + "/sshws")


def _ssl() -> ssl.SSLContext:
    for f in (os.getenv("SSL_CERT_FILE", ""), "/root/.ccr/ca-bundle.crt"):
        if f and os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


def _key():
    import paramiko
    raw = os.getenv("SODAM_SSH_KEY", "").strip()
    if not raw:
        sys.exit("SODAM_SSH_KEY 없음 — 클로드 전용 개인키(PEM)를 환경 비밀값에 넣어야 해요")
    if not raw.startswith("-----") and os.path.exists(raw):
        raw = open(raw).read()
    return paramiko.Ed25519Key.from_private_key(io.StringIO(raw.replace("\\n", "\n")))


def open_socket() -> socket.socket:
    """웹소켓 ↔ 로컬 소켓 쌍을 잇는 뒤 스레드를 띄우고, paramiko 에 줄 소켓 한쪽을 돌려준다."""
    token = os.getenv("SODAM_DIAG_TOKEN", "").strip()
    if not token:
        sys.exit("SODAM_DIAG_TOKEN 없음")
    mine, theirs = socket.socketpair()
    ready, err = threading.Event(), []

    async def bridge():
        from websockets.asyncio.client import connect
        try:   # websockets 15+ 는 HTTPS_PROXY 를 알아서 씀 (클로드 작업 환경 프록시)
            tls = {"ssl": _ssl()} if URL.startswith("wss://") else {}
            ws = await connect(URL, additional_headers={"Authorization": f"Bearer {token}"},
                               max_size=2**20, open_timeout=20, **tls)
        except Exception as e:
            err.append(e)
            ready.set()
            return
        ready.set()
        loop = asyncio.get_running_loop()
        theirs.setblocking(False)

        async def up():
            while True:
                data = await loop.sock_recv(theirs, 65536)
                if not data:
                    await ws.close()
                    return
                await ws.send(data)

        async def down():
            async for data in ws:
                await loop.sock_sendall(theirs, data if isinstance(data, bytes) else data.encode())
            theirs.close()
        await asyncio.gather(up(), down(), return_exceptions=True)

    threading.Thread(target=lambda: asyncio.run(bridge()), daemon=True).start()
    ready.wait(30)
    if err:
        sys.exit(f"다리 연결 실패: {err[0]}")
    return mine


def client():
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())   # 토큰으로 연 다리 안쪽 127.0.0.1:22 — 호스트 키는 처음 본 것 기억 안 함
    c.connect("127.0.0.1", username=os.getenv("SODAM_SSH_USER", "root"), pkey=_key(), sock=open_socket(),
              allow_agent=False, look_for_keys=False, timeout=30, banner_timeout=30, auth_timeout=30)
    return c


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    c = client()
    try:
        if argv[0] == "--put" and len(argv) == 3:
            c.open_sftp().put(argv[1], argv[2])
            return 0
        if argv[0] == "--get" and len(argv) == 3:
            c.open_sftp().get(argv[1], argv[2])
            return 0
        _, out, err = c.exec_command(" ".join(argv), timeout=600)
        sys.stdout.write(out.read().decode(errors="replace"))
        sys.stderr.write(err.read().decode(errors="replace"))
        return out.channel.recv_exit_status()
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
