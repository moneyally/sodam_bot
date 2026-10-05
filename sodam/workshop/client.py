"""봇 쪽: 작업실 서버(유닉스 소켓)에 코드를 보내고 결과를 받는다. 작업실이 없거나 꺼져 있으면 WorkshopDown."""
from __future__ import annotations

import asyncio
import base64
import json
import os

SOCKET = os.environ.get("WORKSHOP_SOCKET", "/run/sodam-workshop/sock")
WAIT = 60   # 작업 한도(최대 45초) + 여유


class WorkshopDown(Exception):
    """작업실 서비스가 없거나 응답이 없음."""


async def call(req: dict, path: str | None = None, wait: float = WAIT) -> dict:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path or SOCKET, limit=64 * 1024 * 1024), 3)
    except (OSError, asyncio.TimeoutError) as e:
        raise WorkshopDown(str(e) or "연결 실패") from e
    try:
        writer.write(json.dumps(req, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), wait)
    except (OSError, asyncio.TimeoutError) as e:
        raise WorkshopDown(str(e) or "응답 없음") from e
    finally:
        writer.close()
    if not line:
        raise WorkshopDown("빈 응답")
    return json.loads(line)


async def ping(path: str | None = None) -> dict:
    return await call({"op": "ping"}, path, wait=5)


async def run(session: str, code: str, files: dict[str, bytes] | None = None, limits: dict | None = None,
              path: str | None = None) -> dict:
    """결과: status·output·files(이름 → bytes)·ms."""
    req = {"op": "run", "session": session, "code": code, "limits": limits or {},
           "files": {k: base64.b64encode(v).decode() for k, v in (files or {}).items()}}
    res = await call(req, path)
    res["files"] = {k: base64.b64decode(v) for k, v in (res.get("files") or {}).items()}
    return res
