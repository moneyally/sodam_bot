"""📡 노래·통화 패킷 블랙박스 (systemd sodam-netwatch, `python -m sodam.voice.netwatch`, CAP_NET_RAW 만).

2026-10-13 일루왕 '노래가 몇 초 멈췄다가 2배속으로 따라잡고 원래대로, 한 곡에 3번': 우리 박자 기록(music_sessions.stats)은 깨끗.
손으로 잰 패킷(tcpdump)에서 오너가 들은 14:08:02 에 **텔레그램 음성 서버 → 우리** 패킷이 1.64초 끊겼고 우리가 보내는 건 빈틈 0,
같은 길 ping 은 손실 0·164ms 일정 → 텔레그램 서버가 전달을 멈췄다 몰아서 보냄 (듣는 폰이 밀린 걸 빨리 감음).
이걸 매번 손으로 재지 않게: 텔레그램 주소와 오가는 UDP 를 세서
- 들어옴이 GAP_IN 초 넘게 끊겼는데 우리는 계속 보내는 중 → 'tg_stall' (텔레그램 쪽 멈춤)
- 나감이 GAP_OUT 초 넘게 끊겼는데 들어옴은 계속 → 'our_stall' (소담 서버 쪽 멈춤)
을 data/netwatch/events.jsonl 에, 최근 RING 초 초당 개수를 ring.json(5초마다) 에 남김. 봇의 `.끊김` 이 이걸 읽어 판정 (panels/music).
표준 라이브러리만 · 패킷 내용은 안 봄(앞 40바이트 주소만) · 텔레그램 주소가 아니면 바로 버림.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import time
from pathlib import Path

GAP_IN = 0.8        # 들어옴 이만큼 끊기면 (평소 통화마다 초당 ~11개 → 0.1초 간격)
GAP_OUT = 0.5       # 나감 이만큼 끊기면 (우리는 초당 ~18개)
ALIVE = 0.3         # 반대쪽은 이 안에 패킷이 있어야 '끊김' (둘 다 끊기면 통화가 끝난 것)
MIN_RATE = 5        # 최근 2초 동안 그쪽으로 초당 이만큼은 오갔던 통화만 (드문드문 오가는 건 끊김이 아님)
RING = 180          # 초당 개수 보관 (초)
FORGET = 60         # 이만큼 조용한 주소는 잊음
EVENTS_MAX = 2_000_000   # events.jsonl 이 이보다 크면 .1 로 밀고 새로
WRITE_EVERY = 5.0

# 텔레그램 주소 (core.telegram.org/resources/cidr.txt)
TG_NETS = tuple(ipaddress.ip_network(n) for n in (
    "91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22", "91.108.16.0/22", "91.108.20.0/22", "91.108.56.0/22",
    "91.105.192.0/23", "149.154.160.0/20", "185.76.151.0/24", "95.161.64.0/20",
    "2001:b28:f23c::/47", "2001:b28:f23f::/48", "2001:67c:4e8::/48", "2a0a:f280::/32"))
PACKET_OUTGOING = 4
ETH_P_ALL, ETH_P_IP, ETH_P_IPV6 = 0x0003, 0x0800, 0x86DD


def is_tg(raw: bytes) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return any(ip.version == n.version and ip in n for n in TG_NETS)


def parse(data: bytes, proto: int) -> tuple[bytes, bytes] | None:
    """UDP 패킷이면 (보낸 주소, 받는 주소) 바이트, 아니면 None."""
    if proto == ETH_P_IPV6 and len(data) >= 40 and data[0] >> 4 == 6 and data[6] == 17:
        return data[8:24], data[24:40]
    if proto == ETH_P_IP and len(data) >= 20 and data[0] >> 4 == 4 and data[9] == 17:
        return data[12:16], data[16:20]
    return None


class Peer:
    __slots__ = ("first", "last_in", "last_out", "sin", "sout", "stall_in", "stall_out")

    def __init__(self, t: float):
        self.first = t
        self.last_in = self.last_out = 0.0
        self.sin: dict[int, int] = {}
        self.sout: dict[int, int] = {}
        self.stall_in: float | None = None
        self.stall_out: float | None = None

    def rate(self, sec: dict[int, int], t: float) -> float:
        s = int(t)
        return (sec.get(s - 1, 0) + sec.get(s - 2, 0)) / 2


class Watch:
    """패킷 → 끊김 사건. 시간은 밖에서 넣음 (시험에서 가짜 시계)."""

    def __init__(self):
        self.peers: dict[str, Peer] = {}
        self.tg_cache: dict[bytes, bool] = {}

    def tg(self, raw: bytes) -> bool:
        v = self.tg_cache.get(raw)
        if v is None:
            if len(self.tg_cache) > 4096:
                self.tg_cache.clear()
            v = self.tg_cache[raw] = is_tg(raw)
        return v

    def packet(self, t: float, remote: str, outgoing: bool) -> list[dict]:
        p = self.peers.get(remote)
        if p is None:
            p = self.peers[remote] = Peer(t)
        out: list[dict] = []
        s = int(t)
        if outgoing:
            if p.stall_out is not None:
                out.append(self._ev("our_stall", remote, p.stall_out, t))
                p.stall_out = None
            p.last_out = t
            p.sout[s] = p.sout.get(s, 0) + 1
        else:
            if p.stall_in is not None:
                out.append(self._ev("tg_stall", remote, p.stall_in, t))
                p.stall_in = None
            p.last_in = t
            p.sin[s] = p.sin.get(s, 0) + 1
        return out

    @staticmethod
    def _ev(kind: str, peer: str, start: float, end: float) -> dict:
        return {"kind": kind, "peer": peer, "ts": round(start, 2), "end": round(end, 2), "dur": round(end - start, 2)}

    def check(self, t: float) -> None:
        """끊김이 시작됐나 (끝은 다음 패킷이 packet() 에서 알림). 반대쪽도 끊기면 통화가 끝난 것 → 잊음."""
        for remote, p in list(self.peers.items()):
            if t - max(p.last_in, p.last_out) > FORGET:
                del self.peers[remote]
                continue
            if p.stall_in is None:
                if (t - p.last_in > GAP_IN and t - p.last_out < ALIVE and p.last_in
                        and p.rate(p.sin, p.last_in) >= MIN_RATE and p.rate(p.sout, t) >= MIN_RATE):
                    p.stall_in = p.last_in
            elif t - p.last_out > 2:                      # 우리도 안 보냄 = 통화 끝
                p.stall_in = None
            if p.stall_out is None:
                if (t - p.last_out > GAP_OUT and t - p.last_in < ALIVE and p.last_out
                        and p.rate(p.sout, p.last_out) >= MIN_RATE and p.rate(p.sin, t) >= MIN_RATE):
                    p.stall_out = p.last_out
            elif t - p.last_in > 2:
                p.stall_out = None
            lo = int(t) - RING
            for sec in (p.sin, p.sout):
                for k in [k for k in sec if k < lo]:
                    del sec[k]

    def snapshot(self, t: float) -> dict:
        s = int(t)
        return {"ts": round(t, 2), "end": s - 1, "peers": {
            r: {"first": round(p.first, 2), "in": [p.sin.get(k, 0) for k in range(s - RING, s)],
                "out": [p.sout.get(k, 0) for k in range(s - RING, s)]}
            for r, p in self.peers.items() if p.sin or p.sout}}


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, path)


def _append(path: Path, evs: list[dict]) -> None:
    try:
        if path.exists() and path.stat().st_size > EVENTS_MAX:
            os.replace(path, path.with_suffix(".jsonl.1"))
    except OSError:
        pass
    with path.open("a") as f:
        for e in evs:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def run(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    events, ring = out_dir / "events.jsonl", out_dir / "ring.json"
    s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_ALL))
    s.settimeout(0.1)
    w = Watch()
    last_check = last_write = 0.0
    print("📡 netwatch 시작", flush=True)
    while True:
        try:
            data, addr = s.recvfrom(64)
        except socket.timeout:
            data, addr = b"", None
        t = time.time()
        evs: list[dict] = []
        if data and addr and addr[0] != "lo":
            ends = parse(data, addr[1])
            if ends:
                outgoing = addr[2] == PACKET_OUTGOING
                raw = ends[1] if outgoing else ends[0]
                if w.tg(raw):
                    evs = w.packet(t, str(ipaddress.ip_address(raw)), outgoing)
        if t - last_check >= 0.1:
            w.check(t)
            last_check = t
        if evs:
            _append(events, evs)
            for e in evs:
                print("끊김", json.dumps(e, ensure_ascii=False), flush=True)
        if t - last_write >= WRITE_EVERY:
            _write_json(ring, w.snapshot(t))
            last_write = t


if __name__ == "__main__":
    run(Path(sys.argv[1] if len(sys.argv) > 1 else "data/netwatch"))
