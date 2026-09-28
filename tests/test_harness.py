"""하네스로 버튼 메뉴 전체를 눌러 보고 문제가 하나도 없어야 통과: python tests/test_harness.py"""
import asyncio
import sys

from fakes import runner
from harness import crawl, html_errors

test, run_all = runner()


@test
async def crawl_every_button_as_every_role():
    rep, db, svc = await crawl()
    assert rep.presses > 30, rep.presses
    assert not rep.issues, "\n".join(rep.issues[:20])


@test
async def harness_catches_bad_html():
    # 하네스 자체 검증: 일부러 틀린 HTML 을 잡아내는지
    assert html_errors("<b>ok</b> &amp; <code>x</code>") == []
    assert html_errors("<b>열림")
    assert html_errors("<div>x</div>")
    assert html_errors("a & b")
    assert html_errors("<b><i>x</b></i>")


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
