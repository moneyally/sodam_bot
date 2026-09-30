"""🔐 SSH 웹소켓 다리 (sodam/sshws.py): 토큰 문 · 실패 잠금 · 바이트 그대로 전달 · 배포 스크립트 안전장치. python tests/run_all.py sshws"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import runner  # noqa: E402

from sodam import sshws  # noqa: E402

test, run_all = runner()
ROOT = Path(__file__).resolve().parent.parent


@test
def gate_needs_exact_token_and_locks_after_failures():
    t = [0.0]
    g = sshws.Gate(token_fn=lambda: "SECRET", now=lambda: t[0])
    assert g.check("1.1.1.1", "Bearer SECRET") is None
    assert g.check("1.1.1.1", "Bearer SECRE") == "bad_token"
    assert g.check("1.1.1.1", "SECRET") == "bad_token"                   # Bearer 없이
    assert g.check("1.1.1.1", "") == "bad_token"
    for _ in range(sshws.FAIL_PER_IP):
        g.check("2.2.2.2", "Bearer nope")
    assert g.check("2.2.2.2", "Bearer SECRET") == "locked"               # 틀린 뒤엔 맞는 토큰도 잠김 (무차별 대입 막기)
    assert g.check("3.3.3.3", "Bearer SECRET") is None                   # 다른 IP 는 그대로
    t[0] += sshws.FAIL_WINDOW + 1
    assert g.check("2.2.2.2", "Bearer SECRET") is None                   # 시간 지나면 풀림
    empty = sshws.Gate(token_fn=lambda: "", now=lambda: 0.0)
    assert empty.check("4.4.4.4", "Bearer ") == "bad_token"              # 서버 토큰이 없으면 절대 안 열림


@test
def gate_global_rate_limit():
    g = sshws.Gate(token_fn=lambda: "S", now=lambda: 0.0)
    for i in range(sshws.ALL_PER_MIN):
        g.check(f"10.0.0.{i % 200}", "Bearer x")
    assert g.check("9.9.9.9", "Bearer S") == "too_many"


@test
async def relays_bytes_both_ways_only_with_token():
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    got = []

    async def fake_sshd(reader, writer):                                  # 22번 대신: 받은 걸 대문자로 돌려줌
        data = await reader.read(100)
        got.append(data)
        writer.write(data.upper())
        await writer.drain()
        writer.close()
    tcp = await asyncio.start_server(fake_sshd, "127.0.0.1", 0)
    tport = tcp.sockets[0].getsockname()[1]
    gate = sshws.Gate(token_fn=lambda: "TOK", now=lambda: 0.0)
    async with serve(sshws.make_handler(gate, "127.0.0.1", tport), "127.0.0.1", 0) as ws_srv:
        wport = list(ws_srv.sockets)[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{wport}/sshws", additional_headers={"Authorization": "Bearer TOK"}) as ws:
            await ws.send(b"ssh-2.0-hello")
            assert await asyncio.wait_for(ws.recv(), 5) == b"SSH-2.0-HELLO"
        assert got == [b"ssh-2.0-hello"]
        for path, auth in (("/sshws", "Bearer WRONG"), ("/other", "Bearer TOK")):   # 틀린 토큰·다른 주소 = 닫힘, sshd 에 안 닿음
            async with connect(f"ws://127.0.0.1:{wport}{path}", additional_headers={"Authorization": auth}) as ws:
                try:
                    await asyncio.wait_for(ws.recv(), 5)
                    raise AssertionError("열리면 안 됨")
                except Exception as e:
                    assert "ConnectionClosed" in type(e).__name__, e
        assert got == [b"ssh-2.0-hello"]
    tcp.close()


@test
def deploy_script_keeps_the_three_locks():
    up = (ROOT / "deploy" / "update.sh").read_text()
    body = up[up.index("sshws_setup() {"):up.index("\n}\n", up.index("sshws_setup() {"))]
    assert 'from=\\"127.0.0.1,::1\\"' in body                                # 키는 다리(127.0.0.1)로만
    assert "PasswordAuthentication no" in body and "Match Address 127.0.0.1,::1" in body
    assert "sshd -t -f" in body                                                # 검사 통과해야 sshd 바꿈 (서버 잠김 방지)
    assert "handle /sshws" in up and "reverse_proxy 127.0.0.1:%s" in up
    pub = (ROOT / "deploy" / "claude_ssh.pub").read_text().strip()
    assert pub.startswith("ssh-ed25519 ") and "PRIVATE" not in pub               # 공개키만 저장소에
    unit = (ROOT / "deploy" / "sodam-sshws.service").read_text()
    assert "User=sodam" in unit and "python -m sodam.sshws" in unit and "NoNewPrivileges=true" in unit
    assert sshws.HOST == "127.0.0.1"                                           # 밖에서 직접 못 붙음 (Caddy 뒤)


if __name__ == "__main__":
    run_all()
