"""VPS 배포 스크립트 (deploy/): python tests/run_all.py deploy

① backup.sh 가 WAL 에만 있는(체크포인트 전) 내용까지 담은 온전한 복사본을 만들고 14일 지난 백업만 지움
② healthcheck.sh 가 하트비트 멈춘 봇만 재시작 (막 켜진 봇·꺼진 봇은 안 건드림, 메인·딜러 따로)
③ 딜러 봇은 하트비트 파일이 따로 (같은 data 폴더를 써도 서로 가리지 않게)
④ 셸 문법·유닛 파일 기본값
"""
import atexit
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from fakes import runner

from sodam import __main__ as main_mod

test, run_all = runner()
ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


def _tmp() -> Path:
    d = tempfile.mkdtemp(prefix="sodam-deploy-")
    atexit.register(shutil.rmtree, d, True)
    return Path(d)


@test
def backup_copies_uncheckpointed_wal_and_rotates():
    app = _tmp()
    (app / "data").mkdir()
    (app / "backups").mkdir()
    db = app / "data" / "sodam.db"
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")          # 전부 -wal 에만 남게
    w.execute("CREATE TABLE t(x INTEGER)")
    w.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(500)])
    w.commit()
    assert (app / "data" / "sodam.db-wal").stat().st_size > 0
    old = app / "backups" / "sodam-20000101-000000.db.gz"
    recent = app / "backups" / "sodam-20000102-000000.db.gz"
    for p, days in ((old, 20), (recent, 5)):
        p.write_bytes(b"x")
        t = time.time() - days * 86400
        os.utime(p, (t, t))
    try:
        r = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env={**os.environ, "APP_DIR": str(app),
                           "PYTHON": sys.executable}, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        w.close()
    assert not old.exists(), "14일 지난 백업이 남음"
    assert recent.exists(), "5일 된 백업을 지움"
    made = [p for p in (app / "backups").glob("sodam-*.db.gz") if p not in (recent,)]
    assert len(made) == 1, made
    out = app / "restored.db"
    out.write_bytes(gzip.decompress(made[0].read_bytes()))
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 500
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"   # 복원 때 파일 하나로
    c.close()
    assert not list((app / "backups").glob(".*part")), "임시 파일이 남음"


def _health(app: Path, active: str, started_ago: int) -> list[str]:
    log = app / "restarts.log"
    log.write_text("")
    fake = app / "systemctl"
    uptime = 100000.0                                     # 가짜 부팅 시간 (컨테이너가 막 켜져도 같게)
    (app / "uptime").write_text(f"{uptime} 0")
    start_us = max(0, int((uptime - started_ago) * 1_000_000))
    fake.write_text(f"""#!/bin/sh
case "$1" in
  is-active) for u in {active}; do [ "$u" = "$3" ] && exit 0; done; exit 3 ;;
  show) echo {start_us} ;;
  restart) echo "$2" >> {log} ;;
esac
""")
    fake.chmod(0o755)
    r = subprocess.run(["bash", str(DEPLOY / "healthcheck.sh")], env={**os.environ, "APP_DIR": str(app),
                       "SYSTEMCTL": str(fake), "PROC_UPTIME": str(app / "uptime")}, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    return log.read_text().split()


@test
def healthcheck_restarts_only_stale_bot():
    app = _tmp()
    (app / "data").mkdir()
    hb, hbd = app / "data" / "heartbeat", app / "data" / "heartbeat-dealer"
    hb.write_text("1")
    hbd.write_text("1")
    t = time.time() - 600
    os.utime(hbd, (t, t))                                                   # 딜러만 10분 멈춤
    assert _health(app, "sodam sodam-dealer", 3600) == ["sodam-dealer"]
    assert _health(app, "sodam sodam-dealer", 30) == [], "막 켜진 봇을 재시작함"
    assert _health(app, "sodam", 3600) == [], "꺼진 딜러를 재시작함"
    hb.unlink()                                                             # 하트비트 파일 없음 = 멈춤
    assert _health(app, "sodam", 3600) == ["sodam"]


@test
def dealer_has_own_heartbeat():
    def hb(role):
        return main_mod.heartbeat_path(SimpleNamespace(db_path="data/sodam.db", bot_role=role))
    assert hb("all").name == hb("main").name == "heartbeat"
    assert hb("dealer").name == "heartbeat-dealer" and hb("dealer").parent == hb("main").parent
    assert "heartbeat-dealer" in (DEPLOY / "healthcheck.sh").read_text()


@test
def export_bundle_roundtrip_matches_install():
    app = _tmp()
    (app / "data").mkdir()
    (app / ".env").write_text("TELEGRAM_BOT_TOKEN=x\nDB_PATH=data/sodam.db\n")
    w = sqlite3.connect(app / "data" / "sodam.db")
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE chats(chat_id INTEGER PRIMARY KEY)")
    w.executemany("INSERT INTO chats VALUES(?)", [(-i,) for i in range(1, 8)])
    w.commit()
    try:
        r = subprocess.run(["bash", str(DEPLOY / "export_bundle.sh"), str(app)], capture_output=True, text=True,
                           env={**os.environ, "ALLOW_RUNNING": "1", "PYTHON": sys.executable}, timeout=60)
    finally:
        w.close()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "방 7개" in r.stdout
    pw = [l.split(": ", 1)[1] for l in r.stdout.splitlines() if l.startswith("비밀번호: ")][0]
    enc = next(app.glob("sodam-migrate-*.tar.gz.enc"))
    out = app / "x"
    out.mkdir()
    # install.sh 와 같은 옵션으로 풀림
    d = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-in", str(enc),
                        "-out", str(out / "b.tgz"), "-pass", "env:PW"], env={**os.environ, "PW": pw}, capture_output=True)
    assert d.returncode == 0, d.stderr
    subprocess.run(["tar", "-xzf", str(out / "b.tgz"), "-C", str(out)], check=True)
    assert (out / "bundle" / ".env").read_text().startswith("TELEGRAM_BOT_TOKEN=x")
    c = sqlite3.connect(out / "bundle" / "sodam.db")
    assert c.execute("SELECT count(*) FROM chats").fetchone()[0] == 7
    c.close()
    assert "TELEGRAM_BOT_TOKEN=x" not in enc.read_bytes().decode("latin-1"), "암호화 안 됨"


@test
def scripts_parse_and_units_sane():
    for sh in sorted(DEPLOY.glob("*.sh")):
        r = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
        assert r.returncode == 0, f"{sh.name}: {r.stderr}"
        assert os.access(sh, os.X_OK), f"{sh.name} 실행 권한 없음"
    for name in ("sodam.service", "sodam-dealer.service"):
        u = (DEPLOY / name).read_text()
        for need in ("Restart=always", "RestartSec=5", "ExecStart=/opt/sodam/.venv/bin/python -m sodam",
                     "ProtectSystem=strict", "ReadWritePaths=/opt/sodam/data", "NoNewPrivileges=true",
                     "PrivateTmp=true", "TimeoutStopSec=", "User=sodam"):
            assert need in u, f"{name}: {need} 없음"
    assert "SODAM_ENV=.env.dealer" in (DEPLOY / "sodam-dealer.service").read_text()
    v = (DEPLOY / "sodam-voice.service").read_text()      # 📞 음성 담당: update.sh voice_setup 이 설치
    for need in ("ExecStart=/opt/sodam/.venv/bin/python -m sodam.voice.worker", "Restart=always", "User=sodam",
                 "ReadWritePaths=/opt/sodam/data", "AF_NETLINK"):
        assert need in v, f"sodam-voice.service: {need} 없음"
    assert not any("#" in line.split("=", 1)[1] for line in v.splitlines() if "=" in line and not line.startswith("#")), \
        "systemd 값 줄 끝 주석은 값이 돼 버림"
    up = (DEPLOY / "update.sh").read_text()
    assert "voice_setup" in up and "requirements-voice.txt" in up and (ROOT / "requirements-voice.txt").exists()
    # update.sh 가 기다리는 줄이 실제 시작 로그와 같은 모양인지
    src = (ROOT / "sodam" / "__main__.py").read_text()
    assert '시작!%s' in src and '(버전 {version})' in src
    assert "시작! (버전" in (DEPLOY / "update.sh").read_text()


if __name__ == "__main__":
    run_all()
