"""테스트 실행기 동시 실행 (tests/run_all.py --jobs) + 빠른 배포 설정: python tests/run_all.py run_all

서버 배포 테스트 25분(1코어, 순서대로) → 2개 동시·1.5코어 (2026-10-06 오너 '서버 반영 왜 이렇게 느려').
① 묶음은 무거운 모듈이 한쪽에 몰리지 않게 ② 진짜 실패는 그대로 실패(종료 코드·'^FAIL' 줄 — update.sh 가 이걸로 보고)
③ 동시에 돌 때만 난 실패는 혼자 다시 돌려 통과하면 통과 ④ FAIL 줄 없이 죽은 묶음은 모듈마다 혼자 다시
⑤ update.sh·유닛: --jobs 2 · requirements 안 바뀌면 pip 건너뜀 · 1.5코어 · 2분 타이머 · 재시도 설치는 10분에 한 번
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fakes import runner

import run_all as ra

test, run_all = runner()
ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

# 가짜 묶음 실행: 모듈 이름에 따라 통과·실패. flaky = 처음 한 번만 실패(동시 실행 때만 나는 실패 흉내), crash = 묶음째 FAIL 줄 없이 죽음(처음만)
FAKE = r'''
import sys, pathlib
d = pathlib.Path(sys.argv[1]); bad = 0
for m in sys.argv[2:]:
    print(f"===== {m} =====")
    if m.startswith("crash") and not (d / m).exists():
        (d / m).touch(); print("Traceback: boom"); sys.exit(2)
    if m.startswith("flaky") and not (d / m).exists():
        (d / m).touch(); print(f"FAIL {m}_case: timing"); bad += 1
    elif m.startswith("broken"):
        print(f"FAIL {m}_case: real bug"); bad += 1
    else:
        print(f"PASS {m}_case")
sys.exit(1 if bad else 0)
'''


def _fake(names, jobs=2):
    d = Path(tempfile.mkdtemp(prefix="sodam-runall-"))
    script = d / "fake.py"
    script.write_text(FAKE)
    old = ra.CMD
    ra.CMD = [sys.executable, str(script), str(d)]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = ra.parallel(names, jobs)
    finally:
        ra.CMD = old
        shutil.rmtree(d, True)
    return code, out.getvalue()


@test
def split_balances_heavy_modules_and_keeps_order():
    names = ra.discover([])
    groups = ra.split(names, 2)
    assert sorted(sum(groups, [])) == sorted(names), "빠지거나 두 번 도는 모듈 없음"
    loads = [sum(ra.HEAVY.get(n, 5) for n in g) for g in groups]
    assert max(loads) - min(loads) <= max(ra.HEAVY.values()) // 4, loads
    heavy = sorted(ra.HEAVY, key=lambda n: -ra.HEAVY[n])[:2]
    assert not any(heavy[0] in g and heavy[1] in g for g in groups), "제일 무거운 둘은 다른 묶음"
    pos = {n: i for i, n in enumerate(names)}
    assert all([pos[n] for n in g] == sorted(pos[n] for n in g) for g in groups), "묶음 안은 원래 순서"
    assert ra.split(["test_a"], 3) == [["test_a"]], "빈 묶음은 안 만듦"


@test
def failed_modules_reads_section_headers():
    out = "===== test_a =====\nPASS x\n===== test_b =====\nFAIL y\nFAIL z\n===== test_c =====\nPASS\n"
    assert ra.failed_modules(out) == ["test_b"]
    assert ra.failed_modules("FAIL 머리 없음\n") == []


@test
def all_pass_exit_zero():
    code, out = _fake(["ok_a", "ok_b", "ok_c"])
    assert code == 0 and "모두 통과" in out and "\nFAIL" not in out, out


@test
def real_failure_still_fails_with_fail_line():
    code, out = _fake(["ok_a", "broken_b", "ok_c"])
    assert code == 1, out
    assert "FAIL broken_b (혼자 다시 돌려도 실패)" in out and "또 실패" in out
    assert out.rstrip().endswith("실패 1개 모듈")


@test
def flaky_only_in_parallel_passes_after_solo_retry():
    code, out = _fake(["ok_a", "flaky_b", "ok_c"])
    assert code == 0, out
    assert "다시 혼자: flaky_b → 통과" in out and "다시 돌린 모듈 1개" in out


@test
def crashed_group_retries_each_module_alone():
    code, out = _fake(["crash_a", "ok_b", "broken_c", "ok_d"], jobs=1 + 1)
    assert "다시 혼자: crash_a → 통과" in out, out
    assert code == 1 and "FAIL broken_c" in out and "FAIL crash_a" not in out


@test
def cli_jobs_flag_runs_real_modules():
    env = dict(os.environ)
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "run_all.py"), "--jobs", "2", "name_key", "harness_codex"],
                       capture_output=True, text=True, cwd=ROOT, env=env, timeout=600)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    assert "===== test_name_key =====" in p.stdout and "===== test_harness_codex =====" in p.stdout and "동시 2개" in p.stdout


@test
def deploy_uses_parallel_tests_and_fast_timer():
    up = (DEPLOY / "update.sh").read_text()
    assert "tests/run_all.py --jobs 2" in up
    assert 'REQ_STAMP="$APP_DIR/data/requirements.installed"' in up and up.count('rm -f "$REQ_STAMP"') == 2, \
        "pip 건너뛰기 표시: 설치 전·되돌릴 때 지움"
    install = up.index('"$PY" -m pip install -q --disable-pip-version-check -r "$TMP/requirements.txt"')
    assert up.index('rm -f "$REQ_STAMP"') < install < up.index('echo "$REQ_SUM" > "$REQ_STAMP"'), "설치 성공 뒤에만 표시"
    assert "find \"$RETRY_STAMP\" -mmin +9" in up, "새 커밋 없을 때 설치 재시도는 10분에 한 번"
    svc = (DEPLOY / "sodam-autoupdate.service").read_text()
    assert "CPUQuota=150%" in svc and "CPUWeight=20" in svc and "Nice=10" in svc
    tm = (DEPLOY / "sodam-autoupdate.timer").read_text()
    assert "OnUnitActiveSec=2min" in tm
    for u in (svc, tm):
        assert not any("#" in line.split("=", 1)[1] for line in u.splitlines() if "=" in line and not line.startswith("#"))


if __name__ == "__main__":
    import asyncio
    sys.exit(1 if asyncio.run(run_all()) else 0)
