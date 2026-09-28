"""전체 오프라인 테스트: python tests/run_all.py [모듈이름 ...]

tests/test_*.py 를 전부 찾아서 각 모듈의 run_all() 을 돌린다 (새 테스트 파일은 등록 없이 자동 포함).
"""
import asyncio
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 기존 순서 유지, 나머지는 이름순
ORDER = ["test_offline", "test_features", "test_admin_kb", "test_billing", "test_menu"]


def discover(only: list[str]) -> list[str]:
    found = sorted(p.stem for p in HERE.glob("test_*.py"))
    names = [n for n in ORDER if n in found] + [n for n in found if n not in ORDER]
    return [n for n in names if not only or n in only or n.removeprefix("test_") in only]


async def main(only: list[str]) -> int:
    failed = 0
    for name in discover(only):
        suite = importlib.import_module(name)
        print(f"\n===== {name} =====")
        failed += await suite.run_all()
    print(f"\n전체 결과: {'실패 ' + str(failed) + '개' if failed else '모두 통과'}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main(sys.argv[1:])) else 0)
