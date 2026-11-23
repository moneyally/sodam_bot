"""🖼 월 이미지 한도 (image_monthly) — python tests/run_all.py imagecap

2026-11-23 오너 요청: 특정 방을 한 달에 N장으로. 하루 한도(image_daily)는 그대로, 월 상한을 따로 추가(둘 다 걸림).
영상 주간 한도(video_weekly)와 같은 틀 — counters 'YYYY-MM' 칸이라 매달 1일 자동 리셋. 기본 0=끔, 오너는 무시."""
from datetime import datetime, timedelta

from fake_llm import Room
from fakes import fake_user, runner

import sodam.__main__  # noqa: F401  (설정·도구 등록)
from sodam import imagecap, tools
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
MEMBER = fake_user(5, "쓰잔")
OWNER = fake_user(9, "오너")
CHAT = Room.CHAT


async def world(**over):
    r = await Room().open(admins=())
    r.svc.perms.owner_ids = {9}
    s = {**await r.db.get_settings(CHAT), **over}
    r._s = s
    return r


async def make(r, user=MEMBER, role=Role.MEMBER):
    ctx = ToolCtx(r.svc, r.bot, CHAT, user, role, r._s)
    return await tools.t_make_image(ctx, {"prompt": "고양이", "mode": "create"})


@test
async def monthly_counter_resets_by_month():
    r = await world()
    assert imagecap._month(r.svc.cfg.tz) == datetime.now(r.svc.cfg.tz).strftime("%Y-%m")
    # 지난달과 이번달은 다른 칸 → 자동 리셋 (칸 문자열이 다름)
    now = datetime.now(r.svc.cfg.tz)
    last = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    assert last != now.strftime("%Y-%m")


@test
async def over_helper():
    r = await world(image_monthly=3)
    assert await imagecap.over(r.svc, CHAT, r._s) == 0           # 아직 0장
    for _ in range(3):
        await imagecap.bump(r.svc, CHAT)
    assert await imagecap.used(r.svc, CHAT) == 3
    assert await imagecap.over(r.svc, CHAT, r._s) == 3           # 3장 == 한도 → 막음(한도값 돌려줌)
    # 끔(0)이면 아무리 써도 0
    r._s["image_monthly"] = 0
    assert await imagecap.over(r.svc, CHAT, r._s) == 0


@test
async def monthly_off_does_not_block():
    r = await world(image_monthly=0, image_daily=20)
    for _ in range(3):
        out = await make(r)
        assert "이번 달" not in out, out
    assert await imagecap.used(r.svc, CHAT) == 3                 # 월 카운터는 올라가도(기록), 막진 않음


@test
async def monthly_cap_blocks_after_limit():
    r = await world(image_monthly=2, image_daily=20)
    assert "이번 달" not in await make(r)                        # 1장
    assert "이번 달" not in await make(r)                        # 2장
    out = await make(r)                                          # 3장째 → 월 한도
    assert "이번 달" in out and "2장" in out, out
    assert await imagecap.used(r.svc, CHAT) == 2                 # 막힌 건 안 셈


@test
async def owner_ignores_monthly():
    r = await world(image_monthly=1, image_daily=20)
    for _ in range(3):
        out = await make(r, OWNER, Role.OWNER)
        assert "이번 달" not in out, out                        # 오너는 월 한도 무시


@test
async def daily_still_blocks_even_with_monthly_room():
    r = await world(image_monthly=50, image_daily=1)
    assert "오늘" not in await make(r)                          # 1장
    out = await make(r)                                          # 2장째 → 하루 한도 먼저
    assert "오늘" in out and "이번 달" not in out, out          # 하루가 먼저 막음


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
