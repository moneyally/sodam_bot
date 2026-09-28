"""버튼 메뉴를 전부 눌러서 실제 화면을 docs/SCREENS.md 로 뽑는다: python tools/render_screens.py"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fakes import close_open_dbs  # noqa: E402
from harness import crawl, render_markdown  # noqa: E402


async def main() -> int:
    rep, db, svc = await crawl()
    out = ROOT / "docs" / "SCREENS.md"
    out.write_text(render_markdown(rep), encoding="utf-8")
    await close_open_dbs()
    print(f"{out} — 버튼 {rep.presses}번 누름, 문제 {len(rep.issues)}개")
    for i in rep.issues:
        print(" -", i)
    return 1 if rep.issues else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
