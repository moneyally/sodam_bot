"""🔌 원격 점검 창구 — 클로드(작업 환경)가 서버 DB·로그를 **읽기만** 하게 (오너 결정 2026-09-29).

왜: 작업 환경 → 서버 SSH(22)는 막혀 있고 HTTPS(443)만 나감. 그래서 서버에 읽기 전용 HTTP 창구
(127.0.0.1:8787, 이 파일)를 두고 Caddy 가 https://<IP>.sslip.io 로 받아 넘김 (인증서 자동, deploy/update.sh diag_setup).

안전
- 정해진 조회만 (ROUTES). 자유 SQL·쓰기·설정 변경 없음. DB 는 mode=ro + query_only.
- 토큰(data/diag.token, 0600)은 서버가 만들고 봇이 **오너 1:1 로만** 보냄 (notify_token). 대화·저장소엔 안 나옴.
  오너 메뉴 🔌(panels/diag.py) 에서 끄기(chat_state 0 diag_enabled='0') · 토큰 바꾸기.
- 틀린 토큰은 IP 마다 10분 20번이면 막힘, 전체 분당 120번. 조회는 data/diag_access.log 에 한 줄씩 (오너 화면에 오늘 횟수).
- 로그·글 속 비밀값 모양(봇 토큰·sk- 키·32자 hex)은 가림.

클라이언트: tools/diag.py (환경변수 SODAM_DIAG_TOKEN).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
HOST, PORT = "127.0.0.1", int(os.getenv("DIAG_PORT", "8787"))
ENABLED_KEY, SENT_KEY = "diag_enabled", "diag_token_sent"
UNITS = ("sodam", "sodam-voice", "sodam-diag", "sodam-autoupdate", "sodam-dealer", "caddy")
MAX_ROWS, MAX_HOURS, MAX_LOG_LINES = 500, 24 * 14, 1000
FAIL_MAX, FAIL_WINDOW, RATE_PER_MIN = 20, 600, 120
SECRET_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b|\bsk-[A-Za-z0-9_-]{20,}\b|\b[0-9a-f]{32}\b")


def data_dir(db_path: str) -> Path:
    return Path(db_path).resolve().parent


def token_path(db_path: str) -> Path:
    return data_dir(db_path) / "diag.token"


def access_log_path(db_path: str) -> Path:
    return data_dir(db_path) / "diag_access.log"


def ensure_token(path: Path) -> str:
    """있으면 읽고, 없으면 만든다 (봇·창구 둘 다 불러도 하나만 생김 — O_EXCL)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        tok = secrets.token_urlsafe(32)
        f.write(tok)
    return tok


def rotate_token(path: Path) -> str:
    tok = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.replace(tmp, path)
    return tok


def fingerprint(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()[:12]


def redact(text: str) -> str:
    return SECRET_RE.sub("[가림]", text or "")


def today_count(db_path: str, now: float | None = None) -> int:
    """오늘(서버 시각) 창구 조회 수 — 오너 화면용."""
    p = access_log_path(db_path)
    if not p.exists():
        return 0
    day = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
    with p.open(encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if line.startswith(day))


# ── 봇 쪽: 토큰을 오너 1:1 로 (지문이 바뀌었을 때만 한 번) ─────────────
TOKEN_TEXT = ("🔌 <b>원격 점검 토큰</b> (클로드 전용 — 다른 사람에게 보여주지 마세요)\n\n<code>{tok}</code>\n\n"
              "클로드 작업 환경 설정 → Edit → 환경변수 <code>SODAM_DIAG_TOKEN</code> 에 넣으면, 클로드가 서버 기록을 "
              "<b>읽기만</b> 할 수 있어요. 넣은 뒤 이 메시지는 지워도 돼요. 끄기·바꾸기: 1:1 메뉴 🔌 원격 점검.")


async def notify_token(svc, bot, *, force: bool = False) -> bool:
    """창구가 토큰을 만들어 둔 뒤(= 설치됨)에만 보냄. 오너마다 1:1, 막힌 오너는 건너뜀."""
    path = token_path(svc.cfg.db_path)
    if not path.exists():
        return False
    tok = path.read_text().strip()
    fp = fingerprint(tok)
    if not force and await svc.db.get_state(0, SENT_KEY) == fp:
        return False
    sent = False
    for uid in sorted(await svc.perms.owners()):
        try:
            await bot.send_message(uid, TOKEN_TEXT.format(tok=tok), parse_mode="HTML", protect_content=True)
            sent = True
        except Exception as e:   # 오너가 1:1 을 안 열었거나 막힘
            log.info("점검 토큰 전송 실패 %s: %s", uid, e)
    if sent:
        await svc.db.set_state(0, SENT_KEY, fp)
    return sent


# ── 서버 갱신 결과 (deploy/update.sh report → data/*.status) → 실패면 오너 1:1 ─────────
STATUS_FILES = {"update.status": "🛠 서버 갱신", "diag_setup.status": "🔌 원격 점검 설치"}
FAIL_MARKS = ("tests_failed", "rollback", "diag_fail")


async def report_server_status(svc, bot) -> int:
    """갱신·설치가 **실패**했으면 그 줄을 오너에게 한 번 (같은 줄은 다시 안 보냄). 성공은 조용히. 보낸 건수."""
    from .util import esc
    sent = 0
    for name, label in STATUS_FILES.items():
        p = data_dir(svc.cfg.db_path) / name
        try:
            line = p.read_text(encoding="utf-8", errors="replace").strip()[:600]
        except OSError:
            continue
        key = f"status_seen:{name}"
        if not line or await svc.db.get_state(0, key) == line:
            continue
        await svc.db.set_state(0, key, line)
        if not any(m in line for m in FAIL_MARKS):
            continue
        for uid in sorted(await svc.perms.owners()):
            try:
                await bot.send_message(uid, f"{label} 실패\n<code>{esc(redact(line))}</code>\n"
                                            "봇은 이전 버전으로 계속 돌아요. 클로드에게 이 메시지를 보여 주세요.", parse_mode="HTML")
                sent += 1
            except Exception as e:
                log.info("갱신 실패 알림 전송 실패 %s: %s", uid, e)
    return sent


# ── 창구 (별도 프로세스, 표준 라이브러리만) ────────────────────────
class Diag:
    def __init__(self, db_path: str, app_dir: Path = ROOT, run=subprocess.run):
        self.db_path, self.app_dir, self.run = db_path, app_dir, run
        self.token = ensure_token(token_path(db_path))
        self._token_mtime = token_path(db_path).stat().st_mtime
        self.fails: dict[str, deque] = {}
        self.hits: deque = deque()
        self.lock = threading.Lock()

    # 토큰 파일이 바뀌면(🔄 토큰 바꾸기) 다시 읽음
    def current_token(self) -> str:
        p = token_path(self.db_path)
        try:
            m = p.stat().st_mtime
            if m != self._token_mtime:
                self.token, self._token_mtime = p.read_text().strip(), m
        except OSError:
            pass
        return self.token

    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{Path(self.db_path).resolve()}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=1")
        return c

    def enabled(self) -> bool:
        try:
            with self.db() as c:
                r = c.execute("SELECT value FROM chat_state WHERE chat_id=0 AND key=?", (ENABLED_KEY,)).fetchone()
            return not (r and json.loads(r["value"]) is False)   # 오너 🔌 끄기 = set_state(0, ENABLED_KEY, False)
        except sqlite3.Error:
            return True

    def check(self, ip: str, auth: str) -> int:
        """0 = 통과, 아니면 HTTP 상태."""
        now = time.time()
        with self.lock:
            while self.hits and now - self.hits[0] > 60:
                self.hits.popleft()
            if len(self.hits) >= RATE_PER_MIN:
                return 429
            self.hits.append(now)
            f = self.fails.setdefault(ip, deque())
            while f and now - f[0] > FAIL_WINDOW:
                f.popleft()
            if len(f) >= FAIL_MAX:
                return 429
            given = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if not given or not hmac.compare_digest(given.encode(), self.current_token().encode()):
                f.append(now)
                if len(self.fails) > 10_000:
                    self.fails.clear()
                return 401
        return 0 if self.enabled() else 403

    def access(self, ip: str, path: str, status: int) -> None:
        try:
            with access_log_path(self.db_path).open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ip} {status} {path[:200]}\n")
        except OSError:
            pass

    # ── 조회 ──
    def handle(self, path: str, q: dict) -> tuple[int, dict]:
        fn = ROUTES.get(path)
        if not fn:
            return 404, {"error": "없는 조회", "routes": sorted(ROUTES)}
        try:
            return 200, fn(self, q)
        except BadRequest as e:
            return 400, {"error": str(e)}
        except sqlite3.OperationalError as e:
            return 404, {"error": f"DB: {e}"}

    def chat_id(self, c: sqlite3.Connection, q: dict) -> int:
        raw = q.get("chat", "").strip()
        if not raw:
            raise BadRequest("chat=방ID 또는 방 이름 일부")
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        rows = c.execute("SELECT chat_id, title FROM chats WHERE title LIKE ? LIMIT 10", (f"%{raw}%",)).fetchall()
        if len(rows) == 1:
            return rows[0]["chat_id"]
        raise BadRequest(("방 이름이 여러 개: " + ", ".join(f"{r['title']}({r['chat_id']})" for r in rows)) if rows
                         else f"'{raw}' 방 없음")


class BadRequest(Exception):
    pass


def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(q.get(key, default)), hi))
    except ValueError:
        raise BadRequest(f"{key} 는 숫자")


def _rows(cur) -> list[dict]:
    return [{k: (redact(v) if isinstance(v, str) else v) for k, v in dict(r).items()} for r in cur.fetchall()]


def r_health(d: Diag, q: dict) -> dict:
    out = {"version": (d.app_dir / "VERSION").read_text().strip() if (d.app_dir / "VERSION").exists() else None,
           "now": int(time.time()), "units": {}}
    for u in UNITS:
        try:
            p = d.run(["systemctl", "is-active", u], capture_output=True, text=True, timeout=5)
            out["units"][u] = p.stdout.strip() or p.stderr.strip()[:80]
        except Exception as e:
            out["units"][u] = f"? {e}"[:80]
    hb = data_dir(d.db_path) / "heartbeat"
    out["heartbeat_age_sec"] = int(time.time() - hb.stat().st_mtime) if hb.exists() else None
    with d.db() as c:
        for key in ("voice_worker_beat", "voice_assistant"):
            r = c.execute("SELECT value FROM chat_state WHERE chat_id=0 AND key=?", (key,)).fetchone()
            out[key] = ("있음" if key == "voice_assistant" and r else (r["value"] if r else None))
    out["db_mb"] = round(Path(d.db_path).stat().st_size / 1e6, 1)
    return out


def r_rooms(d: Diag, q: dict) -> dict:
    like = f"%{q.get('q', '')}%"
    with d.db() as c:
        cur = c.execute("SELECT c.chat_id, c.title, s.trial_until, s.paid_until, "
                        "(SELECT MAX(ts) FROM messages m WHERE m.chat_id=c.chat_id) last_msg "
                        "FROM chats c LEFT JOIN subscriptions s USING(chat_id) WHERE c.title LIKE ? "
                        "ORDER BY last_msg DESC LIMIT ?", (like, MAX_ROWS))
        return {"rooms": _rows(cur)}


def r_settings(d: Diag, q: dict) -> dict:
    with d.db() as c:
        cid = d.chat_id(c, q)
        r = c.execute("SELECT title, settings FROM chats WHERE chat_id=?", (cid,)).fetchone()
        if not r:
            raise BadRequest("방 없음")
        return {"chat_id": cid, "title": r["title"], "settings": json.loads(r["settings"] or "{}"),
                "note": "없는 키 = 기본값"}


def r_messages(d: Diag, q: dict) -> dict:
    hours, limit = _int(q, "hours", 6, 1, MAX_HOURS), _int(q, "limit", 100, 1, MAX_ROWS)
    with d.db() as c:
        cid = d.chat_id(c, q)
        sql = ("SELECT m.id, m.ts, m.user_id, u.first_name, u.username, m.is_bot, m.flagged, m.text FROM messages m "
               "LEFT JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND m.ts>=?")
        args: list = [cid, int(time.time()) - hours * 3600]
        if q.get("q"):
            sql += " AND m.text LIKE ?"
            args.append(f"%{q['q']}%")
        if q.get("user"):
            sql += " AND m.user_id=?"
            args.append(_int(q, "user", 0, -10**15, 10**15))
        cur = c.execute(sql + " ORDER BY m.id DESC LIMIT ?", (*args, limit))
        return {"chat_id": cid, "messages": list(reversed(_rows(cur)))}


def r_agent_runs(d: Diag, q: dict) -> dict:
    limit = _int(q, "limit", 30, 1, 200)
    with d.db() as c:
        cid = d.chat_id(c, q)
        cur = c.execute("SELECT * FROM agent_runs WHERE chat_id=? ORDER BY id DESC LIMIT ?", (cid, limit))
        return {"chat_id": cid, "runs": _rows(cur)}


def r_voice(d: Diag, q: dict) -> dict:
    limit = _int(q, "calls", 3, 1, 20)
    with d.db() as c:
        cid = d.chat_id(c, q)
        calls = _rows(c.execute("SELECT * FROM voice_calls WHERE chat_id=? ORDER BY id DESC LIMIT ?", (cid, limit)))
        for call in calls:
            call["lines"] = _rows(c.execute("SELECT ts, who, user_id, text FROM voice_lines WHERE call_id=? ORDER BY id",
                                            (call["id"],)))
        return {"chat_id": cid, "calls": calls}


def r_modlog(d: Diag, q: dict) -> dict:
    hours, limit = _int(q, "hours", 24, 1, MAX_HOURS), _int(q, "limit", 100, 1, MAX_ROWS)
    with d.db() as c:
        cid = d.chat_id(c, q)
        cur = c.execute("SELECT * FROM mod_log WHERE chat_id=? AND ts>=? ORDER BY id DESC LIMIT ?",
                        (cid, int(time.time()) - hours * 3600, limit))
        return {"chat_id": cid, "mod_log": _rows(cur)}


def r_counters(d: Diag, q: dict) -> dict:
    day = q.get("day") or time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise BadRequest("day=YYYY-MM-DD")
    with d.db() as c:
        cid = d.chat_id(c, q) if q.get("chat") else 0
        cur = c.execute("SELECT key, n FROM counters WHERE day=? AND chat_id=? ORDER BY key LIMIT ?", (day, cid, MAX_ROWS))
        return {"day": day, "chat_id": cid, "counters": _rows(cur)}


def r_tables(d: Diag, q: dict) -> dict:
    with d.db() as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                         "AND name NOT LIKE '%_fts%' AND name NOT LIKE 'vec_%' ORDER BY name")]
        return {"tables": {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}}


def r_logs(d: Diag, q: dict) -> dict:
    unit = q.get("unit", "sodam")
    if unit not in UNITS:
        raise BadRequest(f"unit 은 {', '.join(UNITS)}")
    n = _int(q, "lines", 200, 1, MAX_LOG_LINES)
    cmd = ["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"]
    if q.get("since"):
        if not re.fullmatch(r"[0-9 :\-]{1,19}|-?\d+ ?(min|h|hours?|days?) ago", q["since"]):
            raise BadRequest("since='2026-09-29 10:00' 또는 '30 min ago'")
        cmd += ["--since", q["since"]]
    p = d.run(cmd, capture_output=True, text=True, timeout=15)
    lines = redact(p.stdout).splitlines()
    if q.get("grep"):
        lines = [x for x in lines if q["grep"] in x]
    return {"unit": unit, "lines": lines, "stderr": p.stderr.strip()[:300]}


ROUTES = {"/v1/health": r_health, "/v1/rooms": r_rooms, "/v1/settings": r_settings, "/v1/messages": r_messages,
          "/v1/agent_runs": r_agent_runs, "/v1/voice": r_voice, "/v1/modlog": r_modlog, "/v1/counters": r_counters,
          "/v1/tables": r_tables, "/v1/logs": r_logs}


def make_handler(diag: Diag):
    class H(BaseHTTPRequestHandler):
        server_version = "sodam-diag"
        sys_version = ""

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            u = urlparse(self.path)
            status = diag.check(ip, self.headers.get("Authorization", ""))
            if status:
                body = {401: {"error": "토큰 필요"}, 403: {"error": "오너가 원격 점검을 꺼 둠"},
                        429: {"error": "너무 잦음"}}[status]
            else:
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                status, body = diag.handle(u.path, q)
            diag.access(ip, u.path, status)
            self._send(status, body)

        def do_POST(self) -> None:   # 쓰기 없음
            self._send(405, {"error": "읽기 전용"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt, *args) -> None:   # 기본 stderr 로그 끔 (조회는 access 로)
            pass
    return H


def serve(db_path: str, host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    diag = Diag(db_path)
    srv = ThreadingHTTPServer((host, port), make_handler(diag))
    srv.daemon_threads = True
    return srv


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    db_path = os.getenv("DB_PATH", str(ROOT / "data" / "sodam.db"))
    srv = serve(db_path)
    log.info("원격 점검 창구 %s:%s (토큰 지문 %s)", HOST, PORT, fingerprint(ensure_token(token_path(db_path))))
    srv.serve_forever()


if __name__ == "__main__":
    main()
