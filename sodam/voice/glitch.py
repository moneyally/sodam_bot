"""🎧 `.끊김` 판정 — 패킷 블랙박스(sodam/voice/netwatch.py 가 쓰는 data/netwatch/*)를 읽어 '그 순간 어디서 멈췄나'.

- telegram : 텔레그램 음성 서버 → 우리 패킷이 끊김 (우리는 계속 보냄) — 2026-10-13 일루왕 실측 원인. 우리가 못 고침.
- ours      : 소담 서버가 못 보냄 (들어옴은 계속) → 운영자가 볼 일
- clean     : 둘 다 끊김 없음 → 듣는 사람 인터넷일 가능성
- off       : 블랙박스가 안 돌고 있음 (ring.json 이 오래됨)
통화가 여럿이면 어느 텔레그램 주소가 이 방 통화인지는 '세션 시작 시각 ↔ 그 주소를 처음 본 시각'으로 짝지음 (못 하면 전부 보고 '확실치 않음').
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .. import db as dbm

WINDOW = 40          # 신고 전 몇 초를 볼지 (듣고 → 채팅 치기까지)
STALE = 20           # ring.json 이 이보다 오래되면 블랙박스 꺼짐
MATCH = 120          # 세션 시작 뒤 이 안에 처음 본 주소 = 그 통화
KEEP_DAYS = 7

dbm.register_schema("""
CREATE TABLE IF NOT EXISTS music_glitch (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER, ts INTEGER NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS music_glitch_chat ON music_glitch(chat_id, ts);
""")


def read(net_dir: Path, tail: int = 400) -> tuple[dict | None, list[dict]]:
    ring = None
    try:
        ring = json.loads((net_dir / "ring.json").read_text())
    except (OSError, ValueError):
        pass
    evs: list[dict] = []
    for name in ("events.jsonl.1", "events.jsonl"):
        try:
            lines = (net_dir / name).read_text().splitlines()[-tail:]
        except OSError:
            continue
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("kind") in ("tg_stall", "our_stall"):
                evs.append(e)
    return ring, evs[-tail:]


def peer_for(chat_id: int, sessions: list[tuple[int, float]], ring: dict) -> str | None:
    """열린 세션 (방, 시작 시각) 들과 지금 보이는 텔레그램 주소(처음 본 시각)를 시작 순서대로 짝지음."""
    peers = sorted(((p.get("first", 0), r) for r, p in (ring or {}).get("peers", {}).items()
                    if any(p.get("out", [])[-10:])), key=lambda x: x[0])
    if not peers:
        return None
    if len(sessions) == 1 and len(peers) == 1:
        return peers[0][1] if sessions[0][0] == chat_id else None
    used: set[str] = set()
    for cid, start in sorted(sessions, key=lambda s: s[1]):
        cand = [(first - start, r) for first, r in peers if r not in used and -10 <= first - start <= MATCH]
        if not cand:
            continue
        r = min(cand)[1]
        used.add(r)
        if cid == chat_id:
            return r
    return None


def verdict(now: float, chat_id: int, sessions: list[tuple[int, float]], ring: dict | None,
            events: list[dict]) -> dict:
    if not ring or now - float(ring.get("ts") or 0) > STALE:
        return {"kind": "off"}
    peer = peer_for(chat_id, sessions, ring)
    near = [e for e in events if now - WINDOW <= float(e.get("end") or e.get("ts") or 0) <= now + 2]
    sure = peer is not None
    if sure:
        near = [e for e in near if e.get("peer") == peer]
    detail: dict = {"peer": peer, "sure": sure, "events": near[-6:]}
    p = (ring.get("peers") or {}).get(peer) if peer else None
    if p:
        n = WINDOW
        detail["out"] = p.get("out", [])[-n:]
        detail["in"] = p.get("in", [])[-n:]
    ours = [e for e in near if e["kind"] == "our_stall"]
    tg = [e for e in near if e["kind"] == "tg_stall"]
    if ours:
        detail["kind"] = "ours"
    elif tg:
        detail["kind"] = "telegram"
    elif not (ring.get("peers") or {}):
        detail["kind"] = "no_call"
    else:
        detail["kind"] = "clean"
    return detail


def _hms(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")


def text(v: dict, tz) -> str:
    k = v["kind"]
    if k == "off":
        return "🎧 끊김 기록기가 아직 안 켜져 있어요. 운영자에게 알렸어요."
    if k == "no_call":
        return "🎧 지금 소담 음성 통화 패킷이 안 보여요 (노래가 안 나오는 중?)."
    unsure = "" if v.get("sure") else "\n(이 방 통화를 정확히 못 골라서 소담 통화 전체 기준이에요)"
    if k == "ours":
        e = [x for x in v["events"] if x["kind"] == "our_stall"][-1]
        return (f"⚠️ {_hms(e['ts'], tz)} 에 <b>소담 서버</b>가 {e['dur']:.1f}초 동안 소리를 못 보냈어요. "
                f"운영자에게 알렸어요.{unsure}")
    if k == "telegram":
        e = [x for x in v["events"] if x["kind"] == "tg_stall"][-1]
        return (f"📡 {_hms(e['ts'], tz)} 에 <b>텔레그램 음성 서버</b>가 {e['dur']:.1f}초 동안 소리 전달을 멈췄어요. "
                f"소담은 그동안에도 계속 보내고 있었어요 — 밀린 소리를 폰이 빨리 감아서 2배속처럼 들려요. "
                f"텔레그램 쪽이라 소담이 고칠 수는 없어요.{unsure}")
    return ("🎧 방금 40초 동안 소담 서버 기록엔 멈춤이 없어요 (소담 보냄·텔레그램 서버 받음 둘 다 정상). "
            f"듣는 분 인터넷이 잠깐 느렸을 수 있어요.{unsure}")


async def save(db, chat_id: int, user_id: int, v: dict) -> int:
    now = int(time.time())

    def go(c):
        c.execute("DELETE FROM music_glitch WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        return c.execute("INSERT INTO music_glitch(chat_id, user_id, ts, kind, detail) VALUES(?,?,?,?,?)",
                         (chat_id, user_id, now, v["kind"], json.dumps(v, ensure_ascii=False)[:8000])).lastrowid
    return int(await db.atomic(go))
