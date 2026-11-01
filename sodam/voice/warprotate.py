"""🌐 WARP 출구 IP 자동 교체 — 유튜브가 고정 datacenter IP(WARP)를 학습해 '봇이냐?'로 막으면 새 IP 로.

왜: 서버(데이터센터) IP 는 유튜브가 막는데 WARP 로 나가면 쿠키 없이도 받아짐. 근데 WARP 계정/IP 가 한 달째 고정이면
그 IP 도 학습돼 막힘 → 쿠키 백업이 죽으면 음악 중단. 그래서 막히거나 오래되면 새 WARP 계정(= 새 IP)으로 자동 교체.

구조 (동시성 안전): Source(fetcher, 곡 받기 자식)는 막힘을 `mark_blocked` 로 **상태 파일에 기록만** 하고,
worker(단일 프로세스)가 `tick` 에서 읽어 **실제 교체는 하나만** 실행. root 불필요 — deploy/warp_rotate.sh 가
wgcf 새 등록 + pkill(systemd Restart=always 가 새 IP 로 재시작). 실패하면 기존 IP 유지(음악 안 멈춤).

안전: 두 길 동시 교체 X(하나는 살아있게) · 노래 트는 중인 길은 선제 교체 X · 쿨다운(Cloudflare rate limit) ·
MUSIC_PROXY 없으면(로컬·테스트) 아무것도 안 함. 결정은 pick()(순수 함수, 테스트), 실행은 tick().
"""
import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("sodam.voice")

STATE_FILE = "data/warp_rotate.json"
COOLDOWN = 1800          # 한 길 교체 후 이 초 동안 또 안 함 (Cloudflare rate limit·연속 재시도 방지)
MAX_AGE = 12 * 3600      # IP 가 이 초 넘으면 선제 교체 (노래 없는 틈에)
_SCRIPT = str(Path(__file__).resolve().parents[2] / "deploy" / "warp_rotate.sh")

# MUSIC_PROXY 포트 → WARP 디렉토리 (deploy/update.sh warp_setup 이 이렇게 설치: 40000=sodam-warp, 40001=sodam-warp2)
PORT_DIR = {"40000": "/opt/sodam-warp", "40001": "/opt/sodam-warp2"}


def _port(proxy: str) -> str | None:
    m = re.search(r":(\d+)\s*$", (proxy or "").strip())
    return m.group(1) if m else None


def ways() -> list[str]:
    return [p.strip() for p in os.getenv("MUSIC_PROXY", "").split(",") if p.strip()]


def _load(path: str) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict, path: str) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(d))
    except Exception:
        pass


def mark_blocked(proxy: str, path: str = STATE_FILE, now: int | None = None) -> None:
    """Source(fetcher)가 이 WARP 길로 '봇이냐?' 가 다 실패 → 기록만 (worker tick 이 읽어서 교체 결정)."""
    port = _port(proxy)
    if not port:
        return
    d = _load(path)
    d.setdefault(port, {})["blocked_at"] = int(now if now is not None else time.time())
    _save(d, path)


def pick(busy_ports: set, now: int, state: dict) -> str | None:
    """교체할 WARP 포트 하나 (없으면 None). 결정만 — 실제 교체는 tick.
       ① 막힘 기록 있고 쿨다운 지남 → 우선 (노래 중이어도: 그 길은 이미 실패 중이라 교체 OK)
       ② 선제: IP 가 MAX_AGE 넘고, 그 길로 노래 안 하는 중, 쿨다운 OK.
       한 번에 하나만 (두 길 동시 교체 금지 — 하나는 늘 살아있게). 길이 하나뿐이면 선제는 안 함(바꾸면 전부 끊김)."""
    ports = [p for w in ways() if (p := _port(w))]
    if not ports:
        return None

    def cool_ok(p: str) -> bool:
        return now - int(state.get(p, {}).get("rotated_at", 0)) >= COOLDOWN

    blocked = [p for p in ports if state.get(p, {}).get("blocked_at") and cool_ok(p)]
    if blocked:
        return blocked[0]
    if len(ports) >= 2:                                  # 길이 2개 이상일 때만 선제 (하나는 살려둠)
        for p in ports:
            if p in busy_ports or not cool_ok(p):
                continue
            since = int(state.get(p, {}).get("ip_since", 0))
            if since and now - since >= MAX_AGE:
                return p
    return None


async def _call(fn, port: str) -> bool:
    r = fn(port)
    if inspect.isawaitable(r):
        r = await r
    return bool(r)


def _rotate_shell(port: str) -> bool:
    d = PORT_DIR.get(port)
    if not d:
        return False
    try:
        out = subprocess.run(["bash", _SCRIPT, d], capture_output=True, text=True, timeout=90)
        ok = out.returncode == 0 and "rotated" in out.stdout
        log.info("WARP %s IP 교체 %s: %s", port, "성공" if ok else "실패", (out.stdout + out.stderr).strip()[:80])
        return ok
    except Exception as e:
        log.info("WARP %s IP 교체 오류: %r", port, e)
        return False


async def heartbeat(check_fn, now: int | None = None, path: str = STATE_FILE, alert_fn=None) -> dict:
    """WARP 길이 실제로 살아 있는지 능동 확인 (아무도 음악 안 틀어도 주기적으로 — rotate 는 막혀야 발동하므로 이게 보강).
       check_fn(proxy)->bool 로 그 길이 유튜브로 나가는지 확인 (테스트는 가짜). 죽은 길은 mark_blocked → tick 이 교체.
       **모든 길이 죽으면** alert_fn()(오너 긴급 — 쿠키까지 죽었으면 음악 완전 중단)을 하루 1번. → {port: 살았나}."""
    ways_ = ways()
    if not ways_:
        return {}
    now = int(now if now is not None else time.time())
    alive: dict[str, bool] = {}
    for w in ways_:
        ok = await _call(check_fn, w)
        alive[_port(w)] = ok
        if not ok:
            mark_blocked(w, path, now)                    # 죽은 길 → tick 이 교체
    if any(alive.values()):
        return alive
    st = _load(path)                                       # 전부 죽음 → 긴급 알림 (처음 + 그 뒤 하루 1번)
    last = st.get("_alerted_at")
    if alert_fn and (last is None or now - int(last) >= 86400):
        st["_alerted_at"] = now
        _save(st, path)
        await _call(lambda _: alert_fn(), None)
    return alive


async def tick(busy_ports: set | None = None, now: int | None = None,
               rotate_fn=None, path: str = STATE_FILE) -> str | None:
    """worker 가 주기 호출. busy_ports = 지금 노래 트는 WARP 포트 집합. 교체한 포트 or None.
       rotate_fn(port)->bool 로 실제 교체 (안 주면 deploy/warp_rotate.sh 를 스레드에서). 처음 본 길은 ip_since 를 지금으로."""
    if not ways():
        return None
    now = int(now if now is not None else time.time())
    state = _load(path)
    changed = False
    for w in ways():                                     # 처음 보는 길은 이 시점부터 나이 셈
        p = _port(w)
        if p and "ip_since" not in state.get(p, {}):
            state.setdefault(p, {})["ip_since"] = now
            changed = True
    port = pick(busy_ports or set(), now, state)
    if not port:
        if changed:
            _save(state, path)
        return None
    ok = await _call(rotate_fn, port) if rotate_fn else await asyncio.to_thread(_rotate_shell, port)
    if ok:
        state[port] = {"ip_since": now, "rotated_at": now}   # 새 IP: 나이 초기화 + 막힘 기록 지움
    else:
        state.setdefault(port, {})["rotated_at"] = now        # 실패해도 쿨다운 (연속 재시도·rate limit 방지)
    _save(state, path)
    return port if ok else None
