"""🔑 오너가 소담 1:1 에 붙인 API 키를 서버에 저장 (SSH 없이).

실제 사례 2026-09-30: 오너가 xAI 영상 키를 받아 "서버 .env 에 넣어줘" — 클로드 작업 환경은 서버에 쓸 수 없고,
오너는 폰이라 SSH 가 번거로움. 그래서 오너가 1:1 에 키를 그대로 붙이거나 `.키 이름 값` 을 보내면:
- 봇이 그 메시지를 **바로 지우고**, 값은 로그·DB(messages)·AI 어디에도 안 남김 (on_private 맨 앞에서 가로챔).
- 저장 = data/keys.env (서비스가 쓸 수 있는 곳은 data/ 뿐 — ProtectSystem=strict), 권한 600. os.environ 에도 바로 넣어 재시작 없이 씀.
  봇이 켜질 때 config.load_config 가 .env 다음에 이 파일을 읽음 (같은 이름이면 이 파일이 이김).
- 오너가 아니면 저장 안 하고 메시지만 지움 (방에 붙인 키도 지움 — handlers).
- 받는 이름은 목록만 (아무 환경변수나 바꾸면 봇 토큰·DB 경로까지 바뀔 수 있음).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# 이름 → 값 모양. 키만 붙였을 때 알아보는 모양(PREFIX)도 같이
ALLOWED: dict[str, re.Pattern] = {
    "XAI_API_KEY": re.compile(r"xai-[A-Za-z0-9]{20,200}"),
    "GEMINI_API_KEY": re.compile(r"AIza[0-9A-Za-z_-]{30,60}"),
    "NEWSAPI_AI_KEY": re.compile(r"[A-Za-z0-9-]{20,80}"),
    "APISPORTS_KEY": re.compile(r"[A-Za-z0-9]{20,80}"),
    "SPORTSDB_KEY": re.compile(r"[A-Za-z0-9]{1,40}"),
}
BARE = {"XAI_API_KEY": re.compile(r"\s*(xai-[A-Za-z0-9]{20,200})\s*"),
        "GEMINI_API_KEY": re.compile(r"\s*(AIza[0-9A-Za-z_-]{30,60})\s*")}
_CMD = re.compile(r"^[./](?:키|key|apikey)(?:@\w+)?\s+([A-Za-z_][A-Za-z0-9_]*)\s*=?\s*(\S+)\s*$", re.I)
# 방·1:1 어디든 이런 모양이 보이면 일단 지울 대상 (오너가 실수로 방에 붙여도)
LOOKS_SECRET = re.compile(r"xai-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9_-]{20,}")


def detect(text: str) -> tuple[str, str] | tuple[str, None] | None:
    """(이름, 값) · 모르는 이름/값 모양이 틀리면 (이름, None) · 키 메시지가 아니면 None."""
    t = (text or "").strip()
    m = _CMD.match(t)
    if m:
        name, value = m.group(1).upper(), m.group(2)
        pat = ALLOWED.get(name)
        return (name, value) if pat and pat.fullmatch(value) else (name, None)
    for name, pat in BARE.items():
        b = pat.fullmatch(t)
        if b:
            return name, b.group(1)
    return None


def keys_path(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "keys.env"


def save(db_path: str, name: str, value: str) -> Path:
    """data/keys.env 에 NAME=값 (있으면 바꿈), 권한 600, 원자적으로 교체. os.environ 에도."""
    if name not in ALLOWED or not ALLOWED[name].fullmatch(value):
        raise ValueError("허용 안 된 키 이름이나 값 모양")
    path = keys_path(db_path)
    lines = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith(name + "=")]
    lines.append(f"{name}={value}")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".keys.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.environ[name] = value
    return path


def masked(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 8 else "…"
