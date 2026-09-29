"""운영 보강 회귀 테스트: 전송 한도·결제 탐지 빈틈·배포 스크립트·복원 검증·시작/종료 알림·하트비트·예외 알림.
python tests/run_all.py fix_ops
"""
import asyncio
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from telegram.error import TimedOut

from fakes import FakeBot, cfg, make_db, make_svc, runner
from test_billing import CHAT, PAY, FakeTronGrid, setup

from sodam import __main__ as main_mod
from sodam import billing as billing_mod
from sodam import subscription
from sodam.backup import Backup
from sodam.db import DB

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import restore_check  # noqa: E402

test, run_all = runner()
PY = sys.executable


async def _owners():
    return {99}


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="sodam-ops-"))


# ── 1. 텔레그램 전송 한도 ─────────────────────────────────
@test
def rate_limiter_attached_to_bot():
    from telegram.ext import AIORateLimiter
    d = _tmp()
    app = main_mod.build_app(cfg(str(d / "t.db")), DB(str(d / "t.db")))
    rl = app.bot.rate_limiter
    assert isinstance(rl, AIORateLimiter), rl
    assert rl._base_limiter.max_rate == 25 and rl._group_max_rate == 18 and rl._max_retries == 2


@test
def requirements_pin_rate_limiter_and_aiosqlite():
    req = (ROOT / "requirements.txt").read_text()
    assert "rate-limiter" in req.split("python-telegram-bot[")[1].split("]")[0]
    line = next(l for l in req.splitlines() if l.startswith("aiosqlite"))
    assert "<0.23" in line, line
    import aiosqlite
    from importlib.metadata import version
    major, minor = map(int, version("aiosqlite").split(".")[:2])
    assert (major, minor) < (0, 23)                      # 설치본이 상한 안
    assert hasattr(aiosqlite.Connection, "_execute")     # db.pay_invoice 가 쓰는 내부 API


# ── 2. 결제 탐지 빈틈 ─────────────────────────────────────
@test
async def no_invoice_deposit_is_scanned_and_reported():
    """청구서가 하나도 없어도 조회해서, 들어온 입금을 오너에게 보고 (예전엔 조회 자체를 안 함)."""
    db, svc, grid = await setup()
    svc.perms.owners = _owners
    grid.transfer("tx-noinv", 30_000_000, ts=time.time() - 60)
    bot = FakeBot()
    await subscription.run_check(svc, bot)
    assert grid.requests, "청구서가 없어도 TronGrid 를 조회해야 함"
    sent = [c[2] for c in bot.named("send_message")]
    assert any("확인 필요 입금" in t and "tx-noinv" in t for t in sent), sent
    # 같은 tx 는 다시 보고하지 않음 (UNIQUE), 10분 안엔 다시 조회도 안 함
    n = len(grid.requests)
    await subscription.run_check(svc, bot)
    assert len(grid.requests) == n
    svc.billing._last_scan -= billing_mod.IDLE_SCAN + 1
    await subscription.run_check(svc, bot)
    assert len(grid.requests) == n + 1
    assert sum("tx-noinv" in c[2] for c in bot.named("send_message")) == len(sent)


@test
async def deposit_after_invoice_expired_is_reported():
    """청구서 만료(+유예) 뒤 늦게 온 입금 → 청구서가 없어졌어도 커서 기준으로 조회해서 보고."""
    db, svc, grid = await setup(invoice_minutes=10)
    b = svc.billing
    inv = await b.create_invoice(CHAT, 1)
    await b.check_pending()                              # 첫 조회 → 커서 생성
    cursor = await db.get_state(0, billing_mod.CURSOR_KEY)
    assert cursor is None or isinstance(cursor, int)
    # 청구서를 만료시키고(유예 지남) 한참 뒤 입금
    old = int(time.time()) - billing_mod.LATE_GRACE - 3600
    await db.conn.execute("UPDATE invoices SET created=?, expires=? WHERE id=?", (old - 600, old, inv["id"]))
    await db.conn.commit()
    await db.set_state(0, billing_mod.CURSOR_KEY, (old - 600) * 1000)
    grid.transfer("tx-late2", inv["amount_units"], ts=time.time() - 30)
    b._last_scan -= billing_mod.IDLE_SCAN + 1
    paid, unmatched = await b.check_pending()
    assert paid == [] and [u["tx_id"] for u in unmatched] == ["tx-late2"], (paid, unmatched)
    sub = await db.get_subscription(CHAT)
    assert sub is None or sub["paid_until"] is None                    # 자동 연장은 안 함 (오너 확인)
    # 커서가 본 입금 시각으로 전진, 다음 조회는 커서-여유 부터
    cur = await db.get_state(0, billing_mod.CURSOR_KEY)
    assert cur == grid.transfers[-1]["block_timestamp"], cur
    b._last_scan -= billing_mod.IDLE_SCAN + 1
    await b.check_pending()
    assert int(grid.requests[-1].url.params["min_timestamp"]) == cur - billing_mod.CURSOR_OVERLAP * 1000


@test
async def trongrid_failure_alert_once_and_recovery():
    db, svc, grid = await setup()
    state = {"fail": True}

    def handler(req):
        if state["fail"]:
            return httpx.Response(500, json={})
        return grid.handler(req)
    svc.billing.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc.perms.owners = _owners
    bot = FakeBot()

    def alerts(word):
        return [c for c in bot.named("send_message") if word in c[2]]
    for i in range(5):
        await subscription.run_check(svc, bot)
        assert len(alerts("결제 확인 장애")) == (1 if i >= 2 else 0), (i, bot.calls)   # 3번째에 딱 1번
    assert not alerts("결제 확인 복구")
    state["fail"] = False
    await subscription.run_check(svc, bot)
    await subscription.run_check(svc, bot)
    assert len(alerts("결제 확인 복구")) == 1 and len(alerts("결제 확인 장애")) == 1
    assert svc.billing.fail_streak == 0


# ── 3. 배포 파일 ─────────────────────────────────────────
@test
def systemd_unit_fields():
    unit = (ROOT / "deploy" / "sodam.service").read_text()
    kv = dict(l.split("=", 1) for l in unit.splitlines() if "=" in l and not l.startswith("#"))
    assert kv["Restart"] == "always" and kv["RestartSec"] == "5"
    assert kv["User"] and kv["User"] != "root"
    assert kv["WorkingDirectory"].startswith("/")
    assert kv["ExecStart"].endswith("/bin/python -m sodam")
    assert kv["StandardOutput"] == "journal"


_GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]  # 테스트용 저장소는 서명 없이


def _fake_repo(tests_pass: bool, new_commit: bool = True) -> tuple[Path, Path]:
    """update.sh 를 돌릴 가짜 저장소 (원격 → 클론 → 원격에 새 커밋). 재시작은 가짜 systemctl 이 로그에 기록,
    가짜 journalctl 은 base/start_ok 가 있을 때만 '시작! (버전 <클론 HEAD>' 를 냄."""
    base = _tmp()
    origin = base / "origin"
    (origin / "tests").mkdir(parents=True)
    (origin / "deploy").mkdir()
    (origin / "requirements.txt").write_text("")
    (origin / "tests" / "run_all.py").write_text("import sys; sys.exit(0)\n")
    shutil.copy(ROOT / "deploy" / "update.sh", origin / "deploy" / "update.sh")
    subprocess.run(_GIT + ["init", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(_GIT + ["-C", str(origin), "add", "-A"], check=True)
    subprocess.run(_GIT + ["-C", str(origin), "commit", "-qm", "init"], check=True)
    clone = base / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    if new_commit:
        (origin / "tests" / "run_all.py").write_text(f"import sys; sys.exit({0 if tests_pass else 1})\n")
        (origin / "NEW").write_text("new\n")
        subprocess.run(_GIT + ["-C", str(origin), "add", "-A"], check=True)
        subprocess.run(_GIT + ["-C", str(origin), "commit", "-qm", "new"], check=True)
    log = base / "systemctl.log"
    for name, body in (("systemctl", f'echo "$@" >> {log}'),
                       ("journalctl", f'[ -f {base}/start_ok ] && echo "소담 (@t) 시작! (버전 $(git -C {clone} rev-parse --short HEAD)) 모델=x"; true')):
        (base / name).write_text(f"#!/bin/sh\n{body}\n")
        (base / name).chmod(0o755)
    return clone, log


def _run_update(clone: Path, log: Path, **extra: str) -> subprocess.CompletedProcess:
    base = log.parent
    env = {**os.environ, "PY": PY, "SYSTEMCTL": str(base / "systemctl"), "JOURNALCTL": str(base / "journalctl"),
           "RUN_AS": "", "START_WAIT": "1", "LOCK": str(base / "update.lock"), "TMPDIR": str(base), "PIP_NO_INDEX": "1",
           **extra}
    return subprocess.run(["bash", str(clone / "deploy" / "update.sh")], env=env, capture_output=True, text=True,
                          timeout=120)


def _head(clone: Path) -> str:
    return subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout


@test
def update_sh_stops_when_tests_fail():
    clone, log = _fake_repo(tests_pass=False)
    before = _head(clone)
    r = _run_update(clone, log)
    assert r.returncode != 0, r.stdout + r.stderr
    assert not log.exists() or "restart" not in log.read_text(), log.read_text()
    assert _head(clone) == before, "테스트 실패인데 코드가 바뀜"


@test
def update_sh_stops_when_pull_fails():
    clone, log = _fake_repo(tests_pass=True)
    shutil.rmtree(clone.parent / "origin")          # 원격 없음 → git fetch 실패
    r = _run_update(clone, log)
    assert r.returncode != 0 and not log.exists(), r.stdout + r.stderr


@test
def update_sh_noop_without_new_commit():
    clone, log = _fake_repo(tests_pass=True, new_commit=False)
    r = _run_update(clone, log)
    assert r.returncode == 0 and not log.exists(), r.stdout + r.stderr


@test
def update_sh_restarts_when_tests_pass():
    clone, log = _fake_repo(tests_pass=True)
    (log.parent / "start_ok").write_text("")
    r = _run_update(clone, log)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restart sodam" in log.read_text()
    short = subprocess.run(["git", "-C", str(clone), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout
    assert (clone / "VERSION").read_text() == short
    assert "new" in subprocess.run(["git", "-C", str(clone), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout


@test
def update_sh_rolls_back_when_start_line_missing():
    clone, log = _fake_repo(tests_pass=True)
    before = _head(clone)
    r = _run_update(clone, log)                     # start_ok 없음 → '시작! (버전' 안 뜸
    assert r.returncode != 0, r.stdout + r.stderr
    assert _head(clone) == before, "시작 실패인데 이전 커밋으로 안 돌아감"
    assert log.read_text().count("restart sodam") == 2, log.read_text()   # 새 버전 1번 + 되돌린 버전 1번


# ── 4. 복원 검증 ─────────────────────────────────────────
async def _make_backup() -> tuple[DB, Path]:
    db = await make_db()
    await db.ensure_chat(-100777, "복원 테스트방")
    await db.start_subscription(-100777, int(time.time()) + 3600, 1)
    bdir = _tmp()
    gz = await Backup(cfg(db.path, backup_dir=str(bdir)), db).run()
    return db, gz


@test
async def restore_check_ok_on_fresh_backup():
    _, gz = await _make_backup()
    out: list[str] = []
    rc = await restore_check.check(gz.parent, out.append)
    assert rc == 0, out
    text = "\n".join(out)
    assert "integrity_check: ok" in text and "subscriptions" in text and "복원 가능" in text
    assert any(l.split()[0] == "chats" and l.split()[1] == "1" for l in out if l.strip().startswith("chats")), out


@test
async def restore_check_fails_on_broken_backups():
    _, gz = await _make_backup()
    d = gz.parent
    # (a) 최신 파일이 SQLite 가 아님 → 최신을 골라서 실패해야 함
    junk = d / "sodam-29991231-235959.db.gz"
    with gzip.open(junk, "wb") as f:
        f.write(b"not a sqlite file" * 100)
    out: list[str] = []
    assert await restore_check.check(d, out.append) == 1, out
    # (b) 잘린 gzip
    raw = gz.read_bytes()
    cut = d / "cut.db.gz"
    cut.write_bytes(raw[: len(raw) // 2])
    assert await restore_check.check(cut, out.append) == 1
    # (c) 페이지가 깨졌지만 열리긴 하는 DB → integrity_check 에서 걸려야 함
    buf = bytearray(gzip.decompress(raw))
    buf[36:40] = (5).to_bytes(4, "big")     # 헤더의 freelist 페이지 수를 틀리게 (읽기는 되지만 무결성 위반)
    bad = d / "bad.db.gz"
    bad.write_bytes(gzip.compress(bytes(buf)))
    out = []
    assert await restore_check.check(bad, out.append) == 1, out
    assert any("integrity_check 실패" in l for l in out), out
    # (d) 백업 없음
    assert await restore_check.check(_tmp(), out.append) == 1


@test
def restore_check_cli_exit_code():
    d = _tmp()
    with gzip.open(d / "sodam-20000101-000000.db.gz", "wb") as f:
        f.write(b"garbage")
    r = subprocess.run([PY, str(ROOT / "tools" / "restore_check.py"), str(d)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr


# ── 5. 시작·종료 알림 + 하트비트 ────────────────────────────
async def _booted_app():
    """build_app → post_init 을 실제로 돌린 앱 (네트워크 쓰는 부분만 가짜)."""
    from telegram import User
    d = _tmp()
    c = cfg(str(d / "data" / "sodam.db"))
    db = DB(c.db_path)
    app = main_mod.build_app(c, db)
    app.bot._bot_user = User(id=999, first_name="소담", is_bot=True, username="sodambot")
    orig_profile = main_mod.set_profile

    async def no_profile(bot, dealer):
        return None
    main_mod.set_profile = no_profile
    try:
        await app.post_init(app)
    finally:
        main_mod.set_profile = orig_profile
    reports: list[str] = []

    async def report(bot, text):
        reports.append(text)
    app.bot_data["svc"].mod.report = report
    return app, c, reports


@test
async def heartbeat_job_registered_and_writes_file():
    app, c, _ = await _booted_app()
    try:
        hb = main_mod.heartbeat_path(c)
        assert hb == Path(c.db_path).resolve().parent / "heartbeat"
        jobs = app.job_queue.get_jobs_by_name("heartbeat")
        assert len(jobs) == 1 and jobs[0].trigger.interval.total_seconds() == main_mod.HEARTBEAT_SEC
        assert hb.exists()
        os.utime(hb, (0, 0))

        async def down():
            raise TimedOut()

        async def up():
            return None
        await jobs[0].callback(SimpleNamespace(job=jobs[0], bot=SimpleNamespace(get_webhook_info=down)))
        assert hb.stat().st_mtime == 0, "텔레그램에 안 닿으면 기록 안 함 (감시가 재시작하게)"
        await jobs[0].callback(SimpleNamespace(job=jobs[0], bot=SimpleNamespace(get_webhook_info=up)))
        assert time.time() - hb.stat().st_mtime < 5 and abs(int(hb.read_text()) - time.time()) < 5
        req = app.bot._request[0]
        assert isinstance(req, main_mod.PollRequest), "getUpdates 가 시각을 남기는 연결로"
        os.utime(hb, (0, 0))
        main_mod.PollRequest.last_ok = time.monotonic() - main_mod.POLL_STALE - 1   # 받기만 멈춤 (웹훅 조회는 됨)
        await jobs[0].callback(SimpleNamespace(job=jobs[0], bot=SimpleNamespace(get_webhook_info=up)))
        assert hb.stat().st_mtime == 0, "폴링이 멈췄으면 기록 안 함"

        async def ok(*a, **kw):
            return 200, b"{}"
        orig = main_mod.HTTPXRequest.do_request
        main_mod.HTTPXRequest.do_request = ok
        try:
            await req.do_request("u", "getUpdates")
        finally:
            main_mod.HTTPXRequest.do_request = orig
        await jobs[0].callback(SimpleNamespace(job=jobs[0], bot=SimpleNamespace(get_webhook_info=up)))
        assert time.time() - hb.stat().st_mtime < 5, "응답 오면 다시 기록"
    finally:
        await app.post_shutdown(app)


@test
async def start_and_stop_notice_to_owner():
    app, c, reports = await _booted_app()
    try:
        start = app.job_queue.get_jobs_by_name("start_notice")
        assert len(start) == 1
        await start[0].callback(SimpleNamespace())
        assert len(reports) == 1 and "시작" in reports[0], reports
        ver = main_mod.git_version()
        if ver:
            assert ver in reports[0]
        await app.post_stop(app)
        assert len(reports) == 2 and "정상 종료" in reports[1], reports
    finally:
        await app.post_shutdown(app)


@test
def git_version_falls_back_to_version_file():
    d = _tmp()
    assert main_mod.git_version(d) is None
    (d / "VERSION").write_text("abc1234\n")
    assert main_mod.git_version(d) == "abc1234"


# ── 6. 예외 알림 ─────────────────────────────────────────
@test
async def error_notifier_throttles_and_skips_network():
    from telegram.error import NetworkError, TimedOut
    db = await make_db()
    svc = await make_svc(db)
    reports = []

    async def report(bot, text):
        reports.append(text)
    svc.mod.report = report
    now = [1000.0]
    n = main_mod.ErrorNotifier(clock=lambda: now[0])

    def ctx(err):
        return SimpleNamespace(error=err, application=SimpleNamespace(bot_data={"svc": svc}), bot=None, job=None)
    await n(None, ctx(ValueError("a")))
    await n(None, ctx(ValueError("b")))           # 같은 종류 10분 안 → 무시
    await n(None, ctx(KeyError("k")))             # 다른 종류 → 알림
    await n(None, ctx(TimedOut()))
    await n(None, ctx(NetworkError("x")))         # 네트워크 오류 제외
    assert len(reports) == 2 and "ValueError" in reports[0] and "KeyError" in reports[1], reports
    now[0] += main_mod.ERROR_NOTIFY_GAP + 1
    await n(None, ctx(ValueError("<c>")))
    assert len(reports) == 3 and "&lt;c&gt;" in reports[2]


@test
async def error_notifier_runs_via_application_process_error():
    """PTB 가 handlers.on_error 와 함께 우리 ErrorNotifier 도 호출하는지 (build_app 경로 그대로)."""
    import logging
    app, c, reports = await _booted_app()
    lg = logging.getLogger("sodam.handlers")
    level, lg.disabled = lg.level, True    # on_error 의 traceback 로그 소음 억제
    try:
        await app.process_error(update=None, error=RuntimeError("boom"))
        await app.process_error(update=None, error=RuntimeError("boom2"))
    finally:
        lg.disabled = False
        await app.post_shutdown(app)
    assert len(reports) == 1 and "RuntimeError" in reports[0] and "boom" in reports[0], reports
