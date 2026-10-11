"""🎁 텔레그램 미니앱 웹 서버 — 1:1 에서 열리는 작은 웹 화면 (손가락으로 긁는 복권·출석 달력·운세·추천픽).

공식 문서 core.telegram.org/bots/webapps:
- 미니앱은 아래쪽 메뉴 버튼(KeyboardButton.web_app, 1:1 만)·메뉴 버튼(setChatMenuButton)·글 버튼으로 엶. 주소는 https 여야 함.
- 화면이 보내는 Telegram.WebApp.initData 는 텔레그램이 서명한 값 → 서버가 '봇 토큰으로 만든 열쇠'로 서명을 확인해야
  누가 열었는지 믿을 수 있음 (secret = HMAC_SHA256(key="WebAppData", msg=봇 토큰), hash = HMAC_SHA256(secret, 정렬한 key=value 줄)).
- 화면을 조작해도 결과는 못 바꾸게: 긁기 결과·출석은 전부 서버(이 프로세스)가 DB 로 정하고, 화면은 보여 주기만.

구조: 봇 프로세스 안 asyncio 서버 127.0.0.1:MINIAPP_PORT(기본 8790) ← Caddy https://<IP>.sslip.io/app/ (deploy/update.sh).
기능 모듈이 API 를 등록 (register_api) — 이 파일은 서명·속도 제한·파일만 알고 출석·복권 로직은 모름 (sodam/panels/brand.py).
표준 라이브러리만 (aiohttp 없음): 요청 한 번에 연결 한 번, 머리 16KB·몸 16KB 상한, 10초 안에 다 와야 함.
테스트 tests/test_miniapp.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from . import hooks

log = logging.getLogger(__name__)

PORT = int(os.getenv("MINIAPP_PORT", "8790") or 8790)
HOST = "127.0.0.1"
STATIC = Path(__file__).parent / "miniapp_static"
MAX_HEAD = 16 * 1024
MAX_BODY = 16 * 1024
IO_TIMEOUT = 10
AUTH_MAX_AGE = 24 * 3600          # initData 가 이보다 오래되면 거절 (훔친 값 재사용 방지)
USER_PER_MIN = 40
IP_PER_MIN = 120

# action → async fn(svc, bot, user: dict, body: dict) -> (status, dict)
API: dict = {}
# 파일: 이름 → async fn(svc, bot, name) -> (bytes, content_type) | None  (방 배경 그림 등)
FILES: dict = {}
_server: asyncio.base_events.Server | None = None
_hits: dict = {}


def register_api(action: str, fn) -> None:
    API[action] = fn


def register_files(prefix: str, fn) -> None:
    FILES[prefix] = fn


# ── 서명 확인 ─────────────────────────────────────────────
def check_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict | None:
    """텔레그램 initData 서명 확인 → user dict (id·first_name·username…). 틀리거나 오래됐으면 None."""
    if not init_data or not bot_token or len(init_data) > 4096:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    if len(data) != len(pairs):          # 같은 이름이 두 번 = 조작
        return None
    got = data.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", got):
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        auth = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    now = time.time() if now is None else now
    if auth <= 0 or now - auth > AUTH_MAX_AGE or auth - now > 300:
        return None
    try:
        user = json.loads(data.get("user") or "null")
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user.get("is_bot"):
        return None
    return user


def _allow(key, per_min: int) -> bool:
    now = time.monotonic()
    q = [t for t in _hits.get(key, []) if now - t < 60]
    if len(q) >= per_min:
        _hits[key] = q
        return False
    q.append(now)
    _hits[key] = q
    if len(_hits) > 20000:
        _hits.clear()
    return True


# ── 주소 ──────────────────────────────────────────────────
def public_base(svc) -> str | None:
    """미니앱 https 주소 (끝 '/app/'). MINIAPP_URL 이 있으면 그것, 없으면 점검 창구 설치 결과(data/diag_setup.status)의 주소."""
    env = os.getenv("MINIAPP_URL", "").strip()
    if env:
        return env.rstrip("/") + "/"
    try:
        st = (Path(svc.cfg.db_path).resolve().parent / "diag_setup.status").read_text().split()
    except (OSError, AttributeError):
        return None
    if len(st) >= 2 and st[0] == "diag_ok" and re.fullmatch(r"https://[a-z0-9.-]+", st[1]):
        return st[1] + "/app/"
    return None


def running() -> bool:
    return _server is not None


# ── HTTP ─────────────────────────────────────────────────
_REASON = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           413: "Payload Too Large", 429: "Too Many Requests", 500: "Internal Server Error"}
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}


def _response(status: int, body: bytes, ctype: str, cache: str = "no-store") -> bytes:
    head = [f"HTTP/1.1 {status} {_REASON.get(status, 'OK')}", f"Content-Type: {ctype}", f"Content-Length: {len(body)}",
            f"Cache-Control: {cache}", "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer",
            "Connection: close"]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def _json(status: int, obj: dict) -> bytes:
    return _response(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")


async def _route(svc, bot, method: str, path: str, headers: dict, body: bytes) -> bytes:
    path = unquote(urlsplit(path).path)
    if path.startswith("/app"):
        path = path[4:] or "/"
    ip = (headers.get("x-forwarded-for", "").split(",")[0].strip() or "local")
    if not _allow(("ip", ip), IP_PER_MIN):
        return _json(429, {"error": "잠시 후 다시 해 주세요"})
    if method == "GET":
        if path in ("/", "/index.html"):
            try:
                return _response(200, (STATIC / "index.html").read_bytes(), _TYPES[".html"], "no-cache")
            except OSError:
                return _response(404, b"not found", "text/plain")
        m = re.fullmatch(r"/f/([a-z]+)/([A-Za-z0-9_.-]{1,80})", path)
        if m and m.group(1) in FILES:
            got = await FILES[m.group(1)](svc, bot, m.group(2))
            if got:
                return _response(200, got[0], got[1], "public, max-age=600")
        return _response(404, b"not found", "text/plain")
    if method != "POST":
        return _response(405, b"", "text/plain")
    m = re.fullmatch(r"/api/([a-z_]{1,20})", path)
    if not m or m.group(1) not in API:
        return _json(404, {"error": "없는 요청"})
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        return _json(400, {"error": "형식 오류"})
    if not isinstance(req, dict):
        return _json(400, {"error": "형식 오류"})
    user = check_init_data(str(req.get("initData") or ""), svc.cfg.telegram_token)
    if user is None:
        return _json(403, {"error": "텔레그램에서 다시 열어 주세요 (확인 실패)"})
    if not _allow(("user", user["id"]), USER_PER_MIN):
        return _json(429, {"error": "너무 빨라요. 잠시 후 다시"})
    try:
        status, out = await API[m.group(1)](svc, bot, user, req)
    except Exception:
        log.exception("miniapp api %s failed", m.group(1))
        return _json(500, {"error": "잠깐 문제가 생겼어요. 다시 해 주세요"})
    return _json(status, out)


async def _handle(svc, bot, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IO_TIMEOUT)
        except asyncio.LimitOverrunError:
            writer.write(_response(413, b"", "text/plain"))
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            writer.write(_response(400, b"", "text/plain"))
            return
        method, path = parts[0], parts[1]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if "chunked" in headers.get("transfer-encoding", "").lower():
            writer.write(_response(400, b"", "text/plain"))
            return
        try:
            n = int(headers.get("content-length", "0") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            writer.write(_response(413, b"", "text/plain"))
            return
        try:
            body = await asyncio.wait_for(reader.readexactly(n), IO_TIMEOUT) if n else b""
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return
        writer.write(await _route(svc, bot, method, path, headers, body))
        await writer.drain()
    except Exception:
        log.exception("miniapp request failed")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start(svc, bot) -> None:
    global _server
    if _server is not None or os.getenv("MINIAPP", "1") == "0":
        return
    try:
        _server = await asyncio.start_server(lambda r, w: _handle(svc, bot, r, w), HOST, PORT, limit=MAX_HEAD)
        log.info("🎁 미니앱 웹 서버 %s:%d", HOST, PORT)
    except OSError as e:   # 포트가 쓰이는 중 (봇이 두 개?) — 봇은 그대로, 미니앱만 꺼짐
        log.warning("미니앱 웹 서버 못 켬 (%s) — 미니앱 버튼은 채팅 버튼으로 대신", e)


async def stop(svc, bot) -> None:
    global _server
    if _server is not None:
        _server.close()
        try:
            await asyncio.wait_for(_server.wait_closed(), 3)
        except (asyncio.TimeoutError, Exception):
            pass
        _server = None


hooks.add_startup_hook(start)
hooks.add_shutdown_hook(stop)
