"""전체 오프라인 테스트: python tests/run_all.py [모듈이름 ...] [--jobs N]

tests/test_*.py 를 전부 찾아서 각 모듈의 run_all() 을 돌린다 (새 테스트 파일은 등록 없이 자동 포함).

--jobs N: 모듈을 N 묶음으로 나눠 프로세스 N 개가 동시에 (서버 배포 테스트 25분 → 반쯤, 2026-10-06).
  묶음은 무거운 모듈(그림·영상 렌더)을 먼저 고르게 나눔(HEAVY 대략 초). 한 묶음 안에선 지금처럼 한 프로세스·같은 순서.
  동시에 돌 때만 실패한 모듈은 **혼자 한 번 더** 돌려 보고 그때도 실패하면 실패 (CPU 경쟁에 민감한 타이밍 테스트 —
  진짜 버그는 혼자 돌려도 실패). 끝에 '^FAIL' 줄은 최종 실패한 것만 다시 찍음 (update.sh 가 이 줄로 보고).
"""
import asyncio
import importlib
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CMD = [sys.executable, str(HERE / "run_all.py")]     # 묶음 하나 = 이 파일을 모듈 이름들로 (테스트는 가짜 명령으로 바꿈)

# 기존 순서 유지, 나머지는 이름순
ORDER = ["test_offline", "test_features", "test_admin_kb", "test_billing", "test_menu"]
# 대략 걸리는 초 (2026-10-06 측정, 그림·영상 렌더). 없는 모듈 = 5초로 셈
HEAVY = {"test_sticker_parts": 130, "test_avatar": 95, "test_sticker_upgrade": 83, "test_animation": 48,
         "test_voice": 48, "test_sticker": 29, "test_botskills": 28, "test_anim": 18, "test_vision": 15,
         "test_card_approvals": 14, "test_game_visual2": 13, "test_workshop": 12, "test_fix_ops": 12,
         "test_stickerpack": 33, "test_sticker_layout": 11}


def discover(only: list[str]) -> list[str]:
    found = sorted(p.stem for p in HERE.glob("test_*.py"))
    names = [n for n in ORDER if n in found] + [n for n in found if n not in ORDER]
    return [n for n in names if not only or n in only or n.removeprefix("test_") in only]


def split(names: list[str], jobs: int) -> list[list[str]]:
    """무거운 것부터 가장 가벼운 묶음에 (LPT). 묶음 안 순서는 원래 순서."""
    loads, groups = [0.0] * jobs, [[] for _ in range(jobs)]
    for n in sorted(names, key=lambda n: -HEAVY.get(n, 5)):
        i = loads.index(min(loads))
        groups[i].append(n)
        loads[i] += HEAVY.get(n, 5)
    pos = {n: k for k, n in enumerate(names)}
    return [sorted(g, key=pos.get) for g in groups if g]


def failed_modules(out: str) -> list[str]:
    """출력에서 FAIL 이 나온 모듈 이름 ('===== test_x =====' 아래 'FAIL ...')."""
    bad, cur = [], None
    for line in out.splitlines():
        if line.startswith("===== ") and line.endswith(" ====="):
            cur = line[6:-6]
        elif line.startswith("FAIL") and cur and cur not in bad:
            bad.append(cur)
    return bad


def run_one(modules: list[str]) -> tuple[int, str]:
    p = subprocess.run([*CMD, *modules], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def parallel(names: list[str], jobs: int) -> int:
    t0 = time.monotonic()
    procs = [(g, subprocess.Popen([*CMD, *g], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)) for g in split(names, jobs)]
    suspect: list[str] = []
    for g, p in procs:
        out, _ = p.communicate()
        print(out, end="")
        bad = failed_modules(out)
        if p.returncode and not bad:          # 모듈 import 실패·충돌 등 FAIL 줄 없이 죽음 → 묶음 전체를 다시
            bad = list(g)
        suspect += bad
    final = []
    for m in suspect:                         # 혼자 한 번 더 (동시에 돌 때만 나는 타이밍 실패 거르기)
        code, out = run_one([m])
        print(f"\n----- 다시 혼자: {m} → {'통과 (동시 실행 때만 실패)' if code == 0 else '또 실패'} -----")
        if code:
            print(out, end="")
            final.append(m)
    print(f"\n동시 {jobs}개 · {time.monotonic() - t0:.0f}초 · 다시 돌린 모듈 {len(suspect)}개")
    for m in final:
        print(f"FAIL {m} (혼자 다시 돌려도 실패)")
    print(f"\n전체 결과: {'실패 ' + str(len(final)) + '개 모듈' if final else '모두 통과'}")
    return 1 if final else 0


async def main(only: list[str]) -> int:
    failed = 0
    for name in discover(only):
        suite = importlib.import_module(name)
        print(f"\n===== {name} =====")
        failed += await suite.run_all()
    print(f"\n전체 결과: {'실패 ' + str(failed) + '개' if failed else '모두 통과'}")
    return failed


if __name__ == "__main__":
    args = sys.argv[1:]
    jobs = 1
    if "--jobs" in args:
        i = args.index("--jobs")
        jobs = max(1, int(args[i + 1]))
        del args[i:i + 2]
    if jobs > 1:
        sys.exit(parallel(discover(args), jobs))
    sys.exit(1 if asyncio.run(main(args)) else 0)
