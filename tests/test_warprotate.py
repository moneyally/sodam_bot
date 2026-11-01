"""🌐 WARP 자동 IP 교체 — 막힘/노후 감지 → 교체, 두 길 동시 X, 노래 중 선제 X, 실패 롤백, 하트비트 전부 죽으면 알림.
실제 wgcf·유튜브는 안 씀 (rotate_fn·check_fn 가짜). 2026-11-01 오너 요청 'WARP 강화해서 쿠키 졸업'.
python tests/run_all.py warprotate"""
import json
import os
import tempfile
from pathlib import Path

from fakes import runner

from sodam.voice import warprotate as W

test, run_all = runner()

TWO = "socks5://127.0.0.1:40000,socks5://127.0.0.1:40001"
ONE = "socks5://127.0.0.1:40000"


def _tmp() -> str:
    return str(Path(tempfile.mkdtemp()) / "warp.json")


def _st(p: str) -> dict:
    return json.loads(Path(p).read_text()) if Path(p).exists() else {}


@test
async def no_proxy_does_nothing():
    """MUSIC_PROXY 없으면(로컬·테스트) 아무것도 안 함."""
    os.environ.pop("MUSIC_PROXY", None)
    assert await W.tick(path=_tmp()) is None
    assert await W.heartbeat(lambda p: True, path=_tmp()) == {}


@test
async def blocked_path_is_rotated_and_cleared():
    """막힘 기록 → tick 이 그 길 교체 → ip_since 새로, 막힘 기록 지움."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    W.mark_blocked("socks5://127.0.0.1:40000", path=p, now=1000)
    calls = []

    async def rot(port):
        calls.append(port)
        return True

    out = await W.tick(now=2000, rotate_fn=rot, path=p)
    assert out == "40000" and calls == ["40000"], (out, calls)
    st = _st(p)
    assert "blocked_at" not in st["40000"], "교체 후 막힘 기록 지움"
    assert st["40000"]["ip_since"] == 2000 and st["40000"]["rotated_at"] == 2000


@test
async def only_one_path_rotated_per_tick():
    """두 길 다 막혀도 한 틱에 하나만 (하나는 늘 살아있게). 나머지는 다음 틱."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    W.mark_blocked("socks5://127.0.0.1:40000", path=p, now=1000)
    W.mark_blocked("socks5://127.0.0.1:40001", path=p, now=1000)
    calls = []

    async def rot(port):
        calls.append(port)
        return True

    await W.tick(now=2000, rotate_fn=rot, path=p)
    assert len(calls) == 1, calls


@test
async def rotate_failure_keeps_old_ip_and_cooldown():
    """교체 실패(rate limit 등) → None(교체 안 됨) + 쿨다운만 걸어 연속 재시도 방지. 막힘 기록은 남아 다음 기회."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    W.mark_blocked("socks5://127.0.0.1:40000", path=p, now=1000)

    async def rot(port):
        return False

    out = await W.tick(now=2000, rotate_fn=rot, path=p)
    assert out is None
    st = _st(p)
    assert st["40000"]["rotated_at"] == 2000, "쿨다운 걸림"
    assert st["40000"].get("blocked_at") == 1000, "막힘 기록 유지 (교체 실패라)"


@test
async def cooldown_blocks_refire():
    """막혀도 쿨다운(COOLDOWN) 안 지났으면 교체 안 함 (rate limit)."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    W._save({"40000": {"blocked_at": 1000, "rotated_at": 1900, "ip_since": 1900}}, p)

    async def rot(port):
        return True

    out = await W.tick(now=2000, rotate_fn=rot, path=p)   # 2000-1900=100 < COOLDOWN
    assert out is None, "쿨다운 중엔 교체 X"


@test
async def preemptive_rotates_old_idle_path():
    """선제: IP 가 MAX_AGE 넘고, 그 길로 노래 안 하는 중(busy 아님)이면 교체."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    old = 1000
    W._save({"40000": {"ip_since": old}, "40001": {"ip_since": old}}, p)
    now = old + W.MAX_AGE + 10
    calls = []

    async def rot(port):
        calls.append(port)
        return True

    out = await W.tick(busy_ports=set(), now=now, rotate_fn=rot, path=p)
    assert out in ("40000", "40001") and len(calls) == 1, (out, calls)


@test
async def preemptive_skips_busy_path():
    """노래 트는 중인 길(busy)은 선제 교체 안 함 (받던 곡 끊김 방지)."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    old = 1000
    W._save({"40000": {"ip_since": old}, "40001": {"ip_since": old}}, p)
    now = old + W.MAX_AGE + 10
    calls = []

    async def rot(port):
        calls.append(port)
        return True

    out = await W.tick(busy_ports={"40000", "40001"}, now=now, rotate_fn=rot, path=p)
    assert out is None and calls == [], "전부 노래 중 → 선제 X"


@test
async def single_path_no_preemptive():
    """길이 하나뿐이면 선제 교체 안 함 (바꾸면 전부 끊김). 막힘 교체는 됨."""
    os.environ["MUSIC_PROXY"] = ONE
    p = _tmp()
    W._save({"40000": {"ip_since": 1000}}, p)
    now = 1000 + W.MAX_AGE + 10

    async def rot(port):
        return True

    assert await W.tick(busy_ports=set(), now=now, rotate_fn=rot, path=p) is None, "길 하나면 선제 X"


@test
async def heartbeat_marks_dead_path():
    """하트비트: check_fn 이 죽었다고 한 길은 mark_blocked (→ tick 이 교체)."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()

    async def check(proxy):
        return "40001" not in proxy        # 40000 살고 40001 죽음

    alive = await W.heartbeat(check, now=1000, path=p)
    assert alive == {"40000": True, "40001": False}, alive
    st = _st(p)
    assert st["40001"].get("blocked_at") == 1000 and "blocked_at" not in st.get("40000", {})


@test
async def heartbeat_alerts_when_all_dead():
    """모든 길이 죽으면 오너 긴급 알림 (하루 1번)."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    alerts = []

    async def check(proxy):
        return False

    async def alert():
        alerts.append(1)

    await W.heartbeat(check, now=1000, path=p, alert_fn=alert)
    assert alerts == [1], "전부 죽음 → 알림"
    await W.heartbeat(check, now=2000, path=p, alert_fn=alert)   # 같은 날 → 또 안 함
    assert alerts == [1], "하루 1번"
    await W.heartbeat(check, now=1000 + 90000, path=p, alert_fn=alert)  # 하루 넘음
    assert alerts == [1, 1], "하루 지나면 다시"


@test
async def heartbeat_no_alert_if_one_alive():
    """하나라도 살아 있으면 알림 안 함."""
    os.environ["MUSIC_PROXY"] = TWO
    p = _tmp()
    alerts = []

    async def check(proxy):
        return "40000" in proxy        # 40000 만 삶

    async def alert():
        alerts.append(1)

    await W.heartbeat(check, now=1000, path=p, alert_fn=alert)
    assert alerts == [], "하나 살면 알림 X"


if __name__ == "__main__":
    import sys
    sys.exit(1 if __import__("asyncio").run(run_all()) else 0)
