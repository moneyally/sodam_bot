"""전체 오프라인 테스트: python tests/run_all.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_admin_kb  # noqa: E402
import test_billing  # noqa: E402
import test_menu  # noqa: E402
import test_features  # noqa: E402
import test_offline  # noqa: E402


async def main() -> int:
    failed = 0
    for name, suite in (("기본", test_offline), ("기능·회귀", test_features), ("지식·보고·캐시", test_admin_kb),
                        ("구독 결제", test_billing), ("버튼 메뉴", test_menu)):
        print(f"\n===== {name} =====")
        failed += await suite.run_all()
    print(f"\n전체 결과: {'실패 ' + str(failed) + '개' if failed else '모두 통과'}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
