"""🔌 원격 점검 창구 (sodam/diag.py): 토큰 없으면 거절 · 읽기만 · 오너가 끄면 403 · 비밀값 가림 · 토큰은 오너 1:1 로 한 번."""
import asyncio
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam import diag

test, run_all = runner()
CHAT = -100777


async def world():
    db = await make_db()
    await db.ensure_chat(CHAT, "벳블리 소통방")
    await db.ensure_chat(-100778, "다른방")
    await db.upsert_user(fake_user(5, "철수"), commit=True)
    await db.log_message(CHAT, 5, 1, "소담아 멍청아")
    await db.log_message(CHAT, 5, 2, "봇토큰 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA 이거")
    await db.set_setting(CHAT, "ai_comeback", "mirror")
    return db


class Server:
    def __init__(self, db_path):
        self.srv = diag.serve(db_path, port=0)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def get(self, path, token=None, method="GET"):
        path = urllib.parse.quote(path, safe="/?=&$()")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


async def call(s, *a, **kw):
    return await asyncio.to_thread(s.get, *a, **kw)


@test
async def token_file_is_private_and_needed_and_wrong_token_is_rate_limited():
    db = await world()
    s = Server(db.path)
    try:
        tp = diag.token_path(db.path)
        assert stat.S_IMODE(os.stat(tp).st_mode) == 0o600
        tok = tp.read_text().strip()
        assert len(tok) >= 40
        assert (await call(s, "/v1/health"))[0] == 401
        assert (await call(s, "/v1/health", "wrong"))[0] == 401
        st, body = await call(s, "/v1/rooms", tok)
        assert st == 200 and {r["title"] for r in body["rooms"]} >= {"벳블리 소통방", "다른방"}, body
        for _ in range(diag.FAIL_MAX):
            await call(s, "/v1/health", "x")
        assert (await call(s, "/v1/health", tok))[0] == 429, "틀린 토큰을 많이 보낸 곳은 맞는 토큰도 잠시 막힘"
    finally:
        s.close()


@test
async def read_only_routes_resolve_room_by_name_and_redact_secrets():
    db = await world()
    s = Server(db.path)
    tok = diag.token_path(db.path).read_text().strip()
    try:
        st, body = await call(s, "/v1/settings?chat=벳블리", tok)
        assert st == 200 and body["chat_id"] == CHAT and body["settings"]["ai_comeback"] == "mirror", body
        st, body = await call(s, "/v1/messages?chat=벳블리&hours=1", tok)
        texts = [m["text"] for m in body["messages"]]
        assert texts[0] == "소담아 멍청아" and "AAAAAAAA" not in texts[1] and "[가림]" in texts[1], texts
        assert (await call(s, "/v1/messages?chat=없는방", tok))[0] == 400
        assert (await call(s, "/v1/messages?chat=방", tok))[0] == 400, "여러 방이면 고르라고"
        assert (await call(s, "/v1/logs?unit=ssh", tok))[0] == 400, "정해진 서비스 로그만"
        assert (await call(s, "/v1/logs?unit=sodam&since=$(rm)", tok))[0] == 400
        assert (await call(s, "/v1/sql?q=DELETE", tok))[0] == 404
        assert (await call(s, "/v1/rooms", tok, method="POST"))[0] == 405
        st, body = await call(s, "/v1/tables", tok)
        assert st == 200 and body["tables"]["messages"] == 2
        assert (await call(s, "/v1/voice?chat=벳블리", tok))[0] == 200
        assert (await call(s, "/v1/counters?chat=벳블리&day=2026-01-01", tok))[0] == 200
        assert diag.today_count(db.path) >= 8
    finally:
        s.close()
    assert (await db._one("SELECT COUNT(*) n FROM messages"))["n"] == 2, "창구는 아무것도 안 바꿈"


@test
async def owner_can_switch_off_and_rotate_token():
    db = await world()
    s = Server(db.path)
    tp = diag.token_path(db.path)
    tok = tp.read_text().strip()
    try:
        await db.set_state(0, diag.ENABLED_KEY, False)
        assert (await call(s, "/v1/health", tok))[0] == 403
        await db.set_state(0, diag.ENABLED_KEY, None)
        assert (await call(s, "/v1/rooms", tok))[0] == 200
        time.sleep(0.01)
        new = diag.rotate_token(tp)
        os.utime(tp, (time.time() + 5, time.time() + 5))       # 파일 시각이 바뀐 걸 확실히
        assert (await call(s, "/v1/rooms", tok))[0] == 401, "바꾼 뒤 예전 토큰은 즉시 무효"
        assert (await call(s, "/v1/rooms", new))[0] == 200
        assert stat.S_IMODE(os.stat(tp).st_mode) == 0o600
    finally:
        s.close()


@test
async def token_goes_only_to_owner_dm_once_per_token():
    db = await world()
    svc = await make_svc(db)
    svc.perms.owner_ids = {7}
    bot = FakeBot()
    assert not await diag.notify_token(svc, bot), "창구가 아직 없으면(토큰 파일 없음) 안 보냄"
    tok = diag.ensure_token(diag.token_path(db.path))
    assert await diag.notify_token(svc, bot)
    [(_, uid, text, kw)] = bot.named("send_message")
    assert uid == 7 and tok in text and kw.get("protect_content") is True
    assert not await diag.notify_token(svc, bot), "같은 토큰은 한 번만"
    diag.rotate_token(diag.token_path(db.path))
    assert await diag.notify_token(svc, bot) and len(bot.named("send_message")) == 2


@test
async def owner_menu_toggle_and_not_for_others():
    import sodam.panels  # noqa: F401
    from sodam.menu import PanelCtx
    from sodam.panels import diag as P
    db = await world()
    svc = await make_svc(db)
    svc.perms.owner_ids = {7}
    diag.ensure_token(diag.token_path(db.path))
    bot = FakeBot()
    c = PanelCtx(svc, bot, 7, None, [])
    sc = await P.s_diag(c)
    assert "켜짐" in sc.text and "읽기만" in sc.text and diag.token_path(db.path).read_text().strip() not in sc.text
    await P.r_toggle(c)
    assert await db.get_state(0, diag.ENABLED_KEY) is False
    await P.r_toggle(c)
    assert await db.get_state(0, diag.ENABLED_KEY) is None
    other = PanelCtx(svc, bot, 8, None, [])
    assert (await P.s_diag(other)).alert and (await P.r_rotate(other)).alert


@test
async def redact_hides_secret_shapes():
    s = "tok 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12 key sk-proj-abcdefghijklmnopqrstu hash 0123456789abcdef0123456789abcdef ok"
    out = diag.redact(s)
    assert "ABCDEFGH" not in out and "sk-proj" not in out and "0123456789abcdef0123" not in out and out.endswith("ok")


if __name__ == "__main__":
    run_all()


@test
async def server_update_failures_reach_owner_once_success_stays_quiet():
    db = await world()
    svc = await make_svc(db)
    svc.perms.owner_ids = {7}
    bot = FakeBot()
    d = diag.data_dir(db.path)
    (d / "update.status").write_text("2026-09-29 10:00:00 ok abc123\n")
    assert await diag.report_server_status(svc, bot) == 0 and not bot.named("send_message")
    (d / "update.status").write_text("2026-09-29 10:10:00 tests_failed def456: FAIL test_x FAIL test_y\n")
    (d / "diag_setup.status").write_text("2026-09-29 10:11:00 diag_fail caddy 설치 실패\n")
    assert await diag.report_server_status(svc, bot) == 2
    texts = [c[2] for c in bot.named("send_message")]
    assert any("test_x" in t for t in texts) and any("caddy" in t for t in texts)
    assert await diag.report_server_status(svc, bot) == 0, "같은 줄은 한 번만"


def _update_run(tests_pass):
    import subprocess
    from test_fix_ops import _fake_repo, _run_update
    clone, log = _fake_repo(tests_pass=tests_pass)
    if tests_pass:
        (log.parent / "start_ok").write_text("")
    r = _run_update(clone, log)
    return clone, r


@test
def update_sh_writes_status_for_bot():
    clone, r = _update_run(False)
    st = (clone / "data" / "update.status").read_text()
    assert "tests_failed" in st, (st, r.stdout, r.stderr)
    clone, r = _update_run(True)
    assert " ok " in (clone / "data" / "update.status").read_text()


@test
def update_sh_prints_failing_test_name_even_if_log_is_long():
    """2026-09-29: 서버가 3번 연속 '실패 1개' 로 막혔는데 끝 40줄에 어떤 테스트인지 없었음."""
    import subprocess
    from test_fix_ops import _GIT, _fake_repo, _run_update
    clone, log = _fake_repo(tests_pass=False)
    origin = clone.parent / "origin"
    (origin / "tests" / "run_all.py").write_text(
        "import sys\nprint('FAIL flaky_thing')\nprint('Traceback: boom')\n"
        "for i in range(200): print('PASS x', i)\nsys.exit(1)\n")
    subprocess.run(_GIT + ["-C", str(origin), "commit", "-qam", "fail"], check=True)
    r = _run_update(clone, log)
    assert r.returncode != 0 and "FAIL flaky_thing" in r.stdout + r.stderr, (r.stdout + r.stderr)[-500:]


@test
async def user_route_finds_by_id_at_and_old_at_with_rooms():
    from sodam import namehist
    db = await world()
    await namehist.record(db, fake_user(77777, "옛날", "oldname"))
    await namehist.record(db, fake_user(77777, "우영미", "SAKE_LLL"))
    await db.upsert_user(fake_user(77777, "우영미", "SAKE_LLL"), commit=True)
    await db.touch_member(CHAT, 77777)
    s = Server(db.path)
    tok = diag.token_path(db.path).read_text().strip()
    try:
        for q in ("77777", "@sake_lll", "@oldname"):
            st, body = await call(s, f"/v1/user?q={q}", tok)
            p = body["people"][0]
            assert st == 200 and p["user"]["username"] == "SAKE_LLL" and p["rooms"][0]["title"] == "벳블리 소통방", (q, body)
            assert any(h["username"] == "oldname" for h in p["name_history"])
        st, body = await call(s, "/v1/user?q=@nobody", tok)
        assert st == 200 and body["found"] == 0
        assert (await call(s, "/v1/user?q=77777"))[0] == 401
    finally:
        s.close()
