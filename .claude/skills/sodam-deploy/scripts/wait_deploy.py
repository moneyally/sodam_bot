"""서버가 그 커밋(또는 뒤 커밋)으로 올라갈 때까지 기다림. 서버 테스트 실패면 바로 멈추고 알림.

    python wait_deploy.py 14ad3df [--timeout 3600]
필요: SODAM_DIAG_TOKEN. 저장소 루트에서 실행 (tools/diag.py 사용, git 으로 커밋 순서 확인).
"""
import json
import subprocess
from datetime import datetime
import sys
import time

PY = "/home/user/venv/bin/python"


def diag(*args) -> dict:
    out = subprocess.run([PY, "tools/diag.py", *args], capture_output=True, text=True, timeout=60).stdout
    try:
        return json.loads(out)
    except ValueError:
        return {"raw": out[-500:]}


def includes(server: str, want: str) -> bool:
    """서버 버전이 want 를 포함하는지 (같거나 뒤 커밋)."""
    if not server:
        return False
    subprocess.run(["git", "fetch", "-q", "origin", "main"], capture_output=True)
    return subprocess.run(["git", "merge-base", "--is-ancestor", want, server], capture_output=True).returncode == 0


def main() -> None:
    want = sys.argv[1]
    timeout = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 3600
    start = time.time() - 120
    end, seen_fail = time.time() + timeout, set()
    while time.time() < end:
        h = diag("health")
        ver = h.get("version")
        if includes(ver, want):
            print(f"✅ 서버 버전 {ver} (포함: {want}) · 봇 {h.get('units', {}).get('sodam')}")
            return
        logs = diag("logs", "unit=sodam-autoupdate", "lines=60")
        for line in logs.get("lines", []) if isinstance(logs, dict) else []:
            try:   # 이번에 기다리기 시작한 뒤의 줄만 (예전 실패 줄에 속지 않게)
                if datetime.fromisoformat(line[:25]).timestamp() < start:
                    continue
            except ValueError:
                continue
            if ("FAIL" in line or "tests_failed" in line) and line not in seen_fail:
                seen_fail.add(line)
                print("❌ 서버 테스트 실패:", line)
        if seen_fail:
            sys.exit(1)
        print(f"⏳ 지금 {ver} · 갱신 {h.get('units', {}).get('sodam-autoupdate')} — 60초 뒤 다시", flush=True)
        time.sleep(60)
    print("⌛ 시간 초과 — 아직 반영 안 됨 (미확인)")
    sys.exit(2)


if __name__ == "__main__":
    main()
