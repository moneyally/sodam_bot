"""비용 한도(이미지·웹검색 하루 횟수)는 방 관리자가 기본값까지만, 더 올리는 건 오너만: python tests/run_all.py owner_cap

실제 사례 2026-09-30: 오너 "관리자가 이미지 한도를 올려 사진 생성을 악용하면?" — 예전엔 방 관리자가 20장까지 올릴 수 있었음
(이미지는 한 장이 대화 수십 번 값). 막는 길 전부: .설정변경 · 말로(change_setting) · ✏️ 숫자 직접 입력 · 버튼(menu._set).
"""
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeMsg, fake_user, runner

import sodam.panels  # noqa: F401
from sodam import menu, tools
from sodam.panels import room as room_panel
from sodam.permissions import Role
from sodam.settings import OWNER_CAP, over_cap
from sodam.tools import ToolCtx

test, run_all = runner()
ADMIN, OWNER = fake_user(1, "방장"), fake_user(9, "오너")


async def world():
    r = await Room().open(admins={1})
    r.svc.perms.owner_ids = {9}
    return r


async def image_daily(r):
    return (await r.db.get_settings(Room.CHAT))["image_daily"]


@test
async def admin_cannot_raise_but_can_lower_owner_can_raise_by_command():
    r = await world()
    cmd = await r.say(ADMIN, ".설정변경 image_daily 11")
    assert await image_daily(r) == 5 and "봇 오너만" in cmd.replies[-1], cmd.replies
    await r.say(ADMIN, ".설정변경 image_daily 3")
    assert await image_daily(r) == 3, "줄이기는 됨"
    await r.say(ADMIN, ".설정변경 image_daily 5")
    assert await image_daily(r) == 5, "기본값까지는 됨"
    await r.say(ADMIN, ".설정변경 web_search_daily 100")
    assert (await r.db.get_settings(Room.CHAT))["web_search_daily"] == 30
    await r.say(OWNER, ".설정변경 image_daily 11")
    assert await image_daily(r) == 11, "오너는 올림"


@test
async def admin_cannot_raise_by_speech_or_buttons():
    r = await world()
    change = tools._BY_NAME["change_setting"].fn
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, ADMIN, Role.ADMIN, await r.db.get_settings(Room.CHAT))
    out = await change(ctx, {"key": "image_daily", "value": "20"})
    assert "안 바꿈" in out and await image_daily(r) == 5, out
    octx = ToolCtx(r.svc, r.bot, Room.CHAT, OWNER, Role.OWNER, await r.db.get_settings(Room.CHAT))
    await change(octx, {"key": "image_daily", "value": "8"})
    assert await image_daily(r) == 8
    await r.db.set_setting(Room.CHAT, "image_daily", 5)
    c = menu.PanelCtx(r.svc, r.bot, ADMIN.id, Room.CHAT, ["image_daily"])
    ok, text = await room_panel._input_num(c, FakeMsg(ADMIN.id, ADMIN, "15"))
    assert not ok and "봇 오너만" in text and await image_daily(r) == 5, text
    assert not await menu._set(c, "image_daily", 15) and await image_daily(r) == 5, "버튼 공통 저장도 막음"
    oc = menu.PanelCtx(r.svc, r.bot, OWNER.id, Room.CHAT, ["image_daily"])
    ok, _ = await room_panel._input_num(oc, FakeMsg(OWNER.id, OWNER, "15"))
    assert ok and await image_daily(r) == 15
    assert "image_daily" in room_panel.NUM_KEYS, "오너가 버튼으로 올릴 곳"


@test
async def cap_rule_itself():
    assert over_cap("image_daily", 6) and not over_cap("image_daily", 5) and not over_cap("flood_count", 99)
    assert not over_cap("image_daily", True), "bool 은 int 로 치지 않음"
    assert OWNER_CAP == {"image_daily": 5, "web_search_daily": 30}


if __name__ == "__main__":
    run_all()
