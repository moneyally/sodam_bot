"""🧪 작업실 (격리 코드 실행): python tests/run_all.py workshop

작업실 서버(sodam/workshop/server.py)를 실제로 띄워서 요청한다. 이 환경에서 user namespace 가 되면 격리까지
(인터넷·서버 죽이기·다른 방 세션), 안 되면(배포 서버 테스트처럼 AppArmor 가 막으면) 프로토콜·한도만.
"""
import asyncio
import atexit
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import fake_user, make_db, runner  # noqa: E402

from sodam.workshop import client, snapshot  # noqa: E402
from sodam.workshop.server import clip  # noqa: E402

test, run_all = runner()
SERVER = Path(__file__).resolve().parent.parent / "sodam" / "workshop" / "server.py"
_srv: dict = {}


def _userns_ok() -> bool:
    try:
        # 작업실이 쓰는 것과 같게: user+net+mount+pid 네임스페이스 안에서 tmpfs 마운트까지 (AppArmor 가 막으면 여기서 실패)
        cmd = ["unshare", "-Urnmp", "--fork", "sh", "-c", "mount -t tmpfs none /tmp && true"]
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


ISO = _userns_ok()


def server() -> str:
    if _srv.get("proc") and _srv["proc"].poll() is None:
        return _srv["sock"]
    d = tempfile.mkdtemp(prefix="wstest-")
    secret = os.path.join(d, "secret")
    os.makedirs(secret)
    Path(secret, ".env").write_text("BOT_TOKEN=x")
    sock = os.path.join(d, "run", "sock")
    os.makedirs(os.path.dirname(sock))
    env = {**os.environ, "WS_BASE": os.path.join(d, "ws"), "WS_MOUNT": os.path.join(d, "w"), "WS_HIDE": secret,
           "WS_SESSION_TTL": "2"}
    if not ISO:
        env["WS_ALLOW_NO_ISOLATION"] = "1"
    proc = subprocess.Popen([sys.executable, str(SERVER), sock], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(200):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    _srv.update(proc=proc, sock=sock, secret=secret, dir=d)
    return sock


def call(code, session="room1", files=None, **limits):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(server())
    req = {"op": "run", "session": session, "code": code, "limits": limits,
           "files": {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}}
    s.sendall(json.dumps(req).encode() + b"\n")
    data = b""
    while not data.endswith(b"\n"):
        data += s.recv(1 << 20)
    s.close()
    return json.loads(data)


@test
def runs_code_returns_output_errors_and_new_files_only():
    r = call("print(6*7)")
    assert r["status"] == "ok" and r["output"].strip() == "42" and r["files"] == {}, r
    r = call("1/0")
    assert r["status"] == "error" and "ZeroDivisionError" in r["output"], r          # 모델이 고쳐서 다시 하게 그대로
    r = call("open('표.csv','w').write('a,b\\n1,2')\nopen('memo.bin','wb').write(b'x')", files={"in.txt": b"hi"})
    assert set(r["files"]) == {"표.csv"}, r["files"]                                # 확장자 허용 목록만, 받은 파일은 안 돌려줌
    assert base64.b64decode(r["files"]["표.csv"]) == b"a,b\n1,2"
    r = call("print(open('표.csv').read().splitlines()[1]); print(open('in.txt').read())")
    assert r["output"].split() == ["1,2", "hi"] and r["files"] == {}, r              # 같은 방 = 20분 같은 폴더, 다시 안 보냄


@test
def limits_stop_runaway_code_and_server_survives():
    r = call("while True: pass", cpu=1, wall=4)
    assert r["status"] in ("cpu", "killed", "timeout") and r["ms"] < 4500, r
    r = call("x = bytearray(2 * 1024 ** 3)\nprint('뚫림')", mem_mb=256)
    assert r["status"] == "memory" and "뚫림" not in r["output"], r
    r = call("open('big.txt','wb').write(b'x' * 20 * 1024 * 1024)\nprint('뚫림')")
    assert r["status"] in ("error", "signal 25") and "뚫림" not in r["output"], r
    r = call("import time\ntime.sleep(10)", wall=1)
    assert r["status"] == "timeout" and r["ms"] < 2000, r
    assert call("print('alive')")["output"].strip() == "alive"


@test
def isolation_blocks_network_secrets_other_rooms_and_the_server():
    if not ISO:
        print("  (user namespace 없음 → 격리 검사 건너뜀: 서버에선 sodam-workshop 서비스·AppArmor 가 담당)")
        return
    r = call("import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=2)\nprint('뚫림')")
    assert r["status"] == "error" and "뚫림" not in r["output"] and "unreachable" in r["output"].lower(), r
    r = call(f"print(open({os.path.join(_srv['secret'], '.env')!r}).read())")
    assert r["status"] == "error" and "BOT_TOKEN" not in r["output"], r
    call("open('mine.txt','w').write('room2 비밀')", session="room2")
    r = call(f"import os\nprint(os.listdir({os.path.join(_srv['dir'], 'ws')!r}))", session="room1")
    assert "room2" not in r["output"], r                                             # 다른 방 세션 폴더는 빈 칸
    r = call("import os, signal\nos.kill(os.getppid(), signal.SIGKILL)\nprint('보냄')")
    assert call("print('alive')")["output"].strip() == "alive", "작업 코드가 서버를 죽이면 안 됨"
    r = call("import os\nn = 0\nwhile True:\n    os.fork(); n += 1", nproc=16, wall=4)
    assert call("print('alive')")["output"].strip() == "alive", "포크 폭탄 뒤에도 서버 살아 있음"


@test
async def client_round_trip_and_down_detection():
    sock = server()
    assert (await client.ping(sock))["status"] == "ok"
    r = await client.run("room3", "open('a.json','w').write('{}')\nprint('ok')", path=sock)
    assert r["status"] == "ok" and r["files"] == {"a.json": b"{}"}, r
    try:
        await client.run("x", "print(1)", path=os.path.join(_srv["dir"], "없는소켓"))
        raise AssertionError("작업실이 없으면 WorkshopDown")
    except client.WorkshopDown:
        pass


@test
def failed_isolation_refuses_and_returns_no_files():
    """격리 설정이 중간에 실패하면 실행 안 하고(refused), 서버가 뜬 폴더의 파일을 결과로 돌려주지 않음 (2026-10-05 서버 실측 버그)."""
    if not ISO:
        return
    d = tempfile.mkdtemp(prefix="wsbad-")
    Path(d, "secret.md").write_text("서버 폴더 파일")
    sock = os.path.join(d, "sock")
    env = {**os.environ, "WS_BASE": os.path.join(d, "ws"), "WS_MOUNT": "/proc/sodam-cannot-mkdir"}
    env.pop("WS_ALLOW_NO_ISOLATION", None)
    proc = subprocess.Popen([sys.executable, str(SERVER), sock], env=env, cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock)
        s.sendall(json.dumps({"op": "run", "session": "x", "code": "print('뚫림')", "files": {}, "limits": {}}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            data += s.recv(1 << 20)
        r = json.loads(data)
        assert r["status"] == "refused" and r["files"] == {} and "뚫림" not in r["output"], r
    finally:
        proc.kill()


@test
def output_is_clipped_head_and_tail():
    s = "가" * 3000 + "끝줄"
    out = clip(s, 1000)
    assert out.startswith("가" * 500) and out.endswith("끝줄") and "자 잘림" in out
    r = call("print('x' * 20000)\nprint('합계 42')")
    assert r["output"].rstrip().endswith("합계 42") and len(r["output"]) < 4200, len(r["output"])


@test
async def room_snapshot_holds_only_that_room_and_text_only_for_admins():
    import sqlite3
    from fakes import TZ
    db = await make_db()
    a, b = fake_user(1, "가나"), fake_user(2, "다라")
    for u in (a, b):
        await db.upsert_user(u)
    for chat in (-100, -200):
        for u in (a, b):
            await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                            (chat, u.id, 1, int(time.time())))
    await db.log_message(-100, a.id, 1, "우리방 비밀 얘기")
    await db.log_message(-200, b.id, 2, "다른방 얘기")
    snapshot._cache.clear()

    def open_snap(data):
        c = sqlite3.connect(":memory:")
        c.deserialize(data)
        return c
    c = open_snap(await snapshot.build(db, -100, True, TZ))
    rows = c.execute("SELECT name, text FROM messages").fetchall()
    assert rows == [("가나", "우리방 비밀 얘기")], rows                                 # 다른 방 메시지 없음
    assert {r[0] for r in c.execute("SELECT name FROM people")} == {"가나", "다라"}
    c = open_snap(await snapshot.build(db, -100, False, TZ))
    assert c.execute("SELECT name, text FROM messages").fetchall() == [("가나", None)], "멤버 요청 = 원문 없음"
    assert c.execute("SELECT COUNT(*) FROM moderation").fetchone()[0] == 0
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"people", "messages", "casino", "moderation"}, tables              # 설정·토큰·결제 표 없음


@test
def deploy_installs_workshop_behind_both_walls():
    root = SERVER.parent.parent.parent
    up = (root / "deploy" / "update.sh").read_text()
    body = up[up.index("workshop_setup() {"):up.index("\n}\n", up.index("workshop_setup() {"))]
    assert "venv --copies" in body and '[ -L "$vpy" ]' in body                  # 심볼릭 링크 venv 면 AppArmor 프로필이 안 붙음
    assert "apparmor_parser -r" in body and "workshop_check" in body
    assert "workshop_setup\n    unit_setup" in up                               # 본체 재시작 성공 뒤
    chk = up[up.index("workshop_check() {"):up.index("workshop_setup() {")]
    assert "NET_OPEN" in chk and "APP_SEEN" in chk                              # 설치 뒤 인터넷·봇 폴더 막힘 실제 확인
    unit = (root / "deploy" / "sodam-workshop.service").read_text()
    for need in ("DynamicUser=yes", "PrivateNetwork=yes", "InaccessiblePaths=/opt/sodam ", "RestrictAddressFamilies=AF_UNIX",
                 "CapabilityBoundingSet=\n", "MemoryMax=", "ProtectSystem=strict"):
        assert need in unit, need
    prof = (root / "deploy" / "apparmor-sodam-workshop").read_text()
    assert "/opt/sodam-sandbox/venv/bin/python3" in prof and "userns," in prof and "/opt/sodam-sandbox/venv/bin/python3" in unit


def _stop():
    p = _srv.get("proc")
    if p and p.poll() is None:
        p.kill()


atexit.register(_stop)   # 테스트가 끝나면 작업실 서버도 끔 (run_all.py 가 이 모듈을 불러서 돌려도)

if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
