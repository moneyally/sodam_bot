"""🎁 미니앱 웹 서버 (sodam/miniapp.py + panels/brand.py API): 텔레그램 서명 확인·결과는 서버가 정함·요청 크기 제한.
python tests/run_all.py miniapp"""
import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from fakes import FakeBot, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import brand, lottery, miniapp

test, run_all = runner()
ROOM = -100901
A, OUT = fake_user(71, "가나"), fake_user(72, "밖")
TOKEN = "123456:TEST-token"


def sign(user: dict, token: str = TOKEN, auth: int | None = None, extra: dict | None = None) -> str:
    data = {"auth_date": str(int(time.time()) if auth is None else auth), "query_id": "AAE",
            "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False), **(extra or {})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


@test
async def init_data_signature_is_checked():
    u = {"id": 71, "first_name": "가나"}
    assert miniapp.check_init_data(sign(u), TOKEN)["id"] == 71
    assert miniapp.check_init_data(sign(u), "999:other") is None, "다른 봇 토큰"
    tampered = sign(u).replace("71", "72", 1)
    assert miniapp.check_init_data(tampered, TOKEN) is None, "값을 바꾸면 서명이 안 맞음"
    assert miniapp.check_init_data(sign(u, auth=int(time.time()) - 2 * 86400), TOKEN) is None, "오래된 값"
    assert miniapp.check_init_data(sign(u) + "&user=x", TOKEN) is None, "같은 이름 두 번"
    assert miniapp.check_init_data(sign(u) + "&query_id=AAE", TOKEN) is None, "같은 값이 두 번이어도 (끼워 넣기) 거절"
    assert miniapp.check_init_data(sign({"id": 5, "is_bot": True}), TOKEN) is None
    assert miniapp.check_init_data("", TOKEN) is None and miniapp.check_init_data("a" * 5000, TOKEN) is None


async def http(port: int, method: str, path: str, body: bytes = b"", raw: bytes | None = None) -> tuple[int, bytes]:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw if raw is not None else
            (f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
    await w.drain()
    data = await r.read()
    w.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), rest


async def setup():
    db = await make_db()
    svc = await make_svc(db)
    svc.cfg = SimpleNamespace(**{**svc.cfg.__dict__, "telegram_token": TOKEN}) if not hasattr(svc.cfg, "__dataclass_fields__") \
        else svc.cfg.__class__(**{**svc.cfg.__dict__, "telegram_token": TOKEN})
    await db.ensure_chat(ROOM, "방")
    await db.set_setting(ROOM, "lotto_enabled", True)
    await db.set_setting(ROOM, "lotto_odds", 100.0)
    await db.set_setting(ROOM, "lotto_min_days", 0)
    now = time.time()
    await db.upsert_user(A)
    await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)", (ROOM, A.id, int(now - 9e5), int(now)))
    await db.log_message(ROOM, A.id, 1, "안녕하세요", ts=int(now - 60))
    room = await brand.ensure(db, ROOM, "방")
    await brand.update(db, ROOM, 1, enabled=True)
    bot = FakeBot()
    miniapp.PORT, miniapp._hits = 0, {}
    srv = await asyncio.start_server(lambda r, w: miniapp._handle(svc, bot, r, w), "127.0.0.1", 0, limit=miniapp.MAX_HEAD)
    return SimpleNamespace(svc=svc, db=db, bot=bot, room=room, srv=srv, port=srv.sockets[0].getsockname()[1])


@test
async def api_needs_signed_member_and_scratch_result_comes_from_server():
    e = await setup()
    try:
        st, body = await http(e.port, "GET", "/app/?r=" + e.room.code)
        assert st == 200 and b"telegram-web-app.js" in body, "화면 파일"
        post = lambda action, data: http(e.port, "POST", f"/app/api/{action}", json.dumps(data).encode())  # noqa: E731
        st, body = await post("state", {"initData": "bad", "room": e.room.code})
        assert st == 403, "서명 없으면 거절"
        e.bot.member_status = {(ROOM, OUT.id): "left"}
        st, body = await post("state", {"initData": sign({"id": OUT.id, "first_name": "밖"}), "room": e.room.code})
        assert st == 403 and "멤버" in json.loads(body)["error"], "방 멤버만"
        good = sign({"id": A.id, "first_name": "가나"})
        st, body = await post("state", {"initData": good, "room": e.room.code})
        s = json.loads(body)
        assert st == 200 and s["name"] == "방" and s["lotto"]["left"] == 0 and s["colors"]["accent"].startswith("#"), s
        st, body = await post("checkin", {"initData": good, "room": e.room.code})
        r = json.loads(body)
        assert st == 200 and r["ticket"] and r["state"]["lotto"]["left"] == 1 and r["state"]["checkin"]["today"], r
        st, body = await post("scratch", {"initData": good, "room": e.room.code, "result": "win"})
        r = json.loads(body)
        assert r["result"] == "win" and r["title"] == "당첨!" and r["left"] == 0, r     # 확률 100% 방 — 화면이 보낸 'result' 는 무시
        assert await e.db._one("SELECT 1 FROM lotto_wins WHERE chat_id=? AND user_id=?", (ROOM, A.id)), "당첨은 서버 DB 에"
        st, body = await post("scratch", {"initData": good, "room": e.room.code})
        assert json.loads(body)["result"] == "none", "복권 없으면 또 못 긁음"
        await brand.update(e.db, ROOM, 1, buttons=[{"k": "checkin"}, {"k": "fortune"}])   # 메뉴판에 복권 버튼이 없어도
        assert "lotto" in json.loads((await post("state", {"initData": good, "room": e.room.code}))[1])["tabs"], \
            "복권 켠 방은 복권 칸 늘 있음 (출석 카드 ✋ 버튼이 복권 칸으로 엶)"
        await e.db._write("UPDATE attend SET day='2026-01-01' WHERE chat_id=?", (ROOM,))
        st, body = await post("state", {"initData": good, "room": e.room.code})
        assert json.loads(body)["checkin"]["streak"] == 0, "이틀 넘게 쉬면 연속 0"
        e.bot.member_status = {(ROOM, A.id): "kicked"}                 # 소담이 못 본 사이 밴 (DB 엔 멤버)
        st, body = await post("checkin", {"initData": good, "room": e.room.code})
        assert st == 403, "미니앱으로도 밴된 사람 출석 X"
        e.bot.member_status = {}
        await brand.update(e.db, ROOM, 1, enabled=False)
        st, _ = await post("state", {"initData": good, "room": e.room.code})
        assert st == 403, "메뉴를 끄면 미니앱도 닫힘"
    finally:
        e.srv.close()


@test
async def requests_are_bounded():
    e = await setup()
    try:
        st, _ = await http(e.port, "POST", "/app/api/state", b"x" * (miniapp.MAX_BODY + 1))
        assert st == 413, "큰 몸"
        st, _ = await http(e.port, "GET", "/app/f/bg/../../etc/passwd")
        assert st == 404, "경로 밖 파일"
        st, _ = await http(e.port, "GET", "/app/f/bg/" + e.room.code + ".jpg")
        assert st == 404, "배경 없으면 404 (화면은 색으로)"
        st, _ = await http(e.port, "POST", "/app/api/nothing", b"{}")
        assert st == 404
        st, _ = await http(e.port, "", "", raw=b"POST /app/api/state HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        assert st == 400, "chunked 거절"
        bad = sign({"id": A.id})
        codes = [(await http(e.port, "POST", "/app/api/state", json.dumps({"initData": bad, "room": e.room.code}).encode()))[0]
                 for _ in range(miniapp.USER_PER_MIN + 2)]
        assert 429 in codes, "한 사람 분당 상한"
    finally:
        e.srv.close()


@test
async def public_url_comes_from_env_or_diag_status():
    import os
    import tempfile
    d = tempfile.mkdtemp()
    svc = SimpleNamespace(cfg=SimpleNamespace(db_path=os.path.join(d, "sodam.db")))
    os.environ.pop("MINIAPP_URL", None)
    assert miniapp.public_base(svc) is None
    open(os.path.join(d, "diag_setup.status"), "w").write("2026-10-11 19:00:01 diag_ok https://1-2-3-4.sslip.io\n")   # update.sh report() 실제 모양
    assert miniapp.public_base(svc) == "https://1-2-3-4.sslip.io/app/"
    open(os.path.join(d, "diag_setup.status"), "w").write("2026-10-11 19:00:01 diag_fail caddy 설치 실패\n")
    assert miniapp.public_base(svc) is None
    os.environ["MINIAPP_URL"] = "https://x.example/app"
    try:
        assert miniapp.public_base(svc) == "https://x.example/app/"
    finally:
        os.environ.pop("MINIAPP_URL", None)


if __name__ == "__main__":
    run_all()
