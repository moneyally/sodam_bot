"""⏰ '11시55분에 나한테 메시지 보내줘': python tests/run_all.py remind

실제 2026-10-05 일루왕: 관리자 '23시55분에 나 불러줘' → 작은 모델이 안내서만 읽고 '불러드릴게요' (예약 없음, 안 보냄).
- 시각만 말해도(11시55분·23:55·오후 3시 반) 지금 이후 가장 가까운 그 시각으로 한 번 (announce.parse_time)
- 시각 + 불러/알려/보내 = 큰 모델 (route 'remind'), 예약 도구는 처음부터 실림 (CORE_TOOLS)
- 시각 약속('~에 불러드릴게요')은 쓰기 도구 없이 하면 보내기 전 검사가 한 번 다시 물음 (조회 도구만 썼어도)
- 멤버(고객)도 나한테 오는 알람은 됨 (카드 없이 바로, 한 사람 3개), 1:1 이 막혀 있으면 방에서 이름을 불러 알림
"""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fake_llm import Room
from fakes import fake_user, runner
from test_sanction_multi import A, BOSS, room

from sodam import agent, cron, route, tools
from sodam.announce import parse_time
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
TZ = ZoneInfo("Asia/Seoul")


def at(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


@test
def bare_time_means_the_next_such_time():
    night, morning, noon = at("2026-10-05 22:52"), at("2026-10-06 10:00"), at("2026-10-06 12:10")
    assert parse_time("11시55분", TZ, night)[1] == "10-05 23:55"          # 밤에 '11시55분' = 오늘 밤 11시55분
    assert parse_time("11시55분", TZ, morning)[1] == "10-06 11:55"        # 오전 10시엔 = 오늘 낮 11시55분
    assert parse_time("11:55", TZ, noon)[1] == "10-06 23:55"
    assert parse_time("23시55분", TZ, morning)[1] == "10-06 23:55"
    assert parse_time("오후 3시 반", TZ, morning)[1] == "10-06 15:30"
    assert parse_time("내일 아침 8시", TZ, morning)[1] == "10-07 08:00"
    assert parse_time("오늘 9시", TZ, morning)[1] == "10-06 21:00"        # 오늘 9시는 지났으니 밤 9시
    assert parse_time("매일 09:00", TZ, morning) == ("daily", "09:00", None, None)   # 반복은 그대로
    for bad in ("오늘 9시 30분 뒤 뒤", "25시"):
        try:
            parse_time(bad, TZ, morning)
            raise AssertionError(bad)
        except ValueError:
            pass


@test
def timed_requests_go_to_the_big_model_and_tool_is_loaded():
    for t in ("23시55분에 나 불러줘", "11시55분에 나한테 메시지 보내", "내일 아침 8시에 깨워줘", "오후 3시에 알려줘"):
        assert route.decide(route.Req(t), light_model="luna").lane == "heavy", t
    assert route.decide(route.Req("12시에 보자"), light_model="luna").lane == "light"   # 약속 말만 = 잡담
    assert "schedule_task" in tools.CORE_TOOLS


@test
def timed_promise_without_write_tool_is_checked_once():
    ctx = ToolCtx(None, None, -1, fake_user(5, "루피"), Role.ADMIN, {})
    text = "23시55분에 불러드릴게요, 대표님."
    kind, _ = agent._final_check(ctx, text, "23시55분에 나 불러줘", True, {"schedule_task"}, ["안내서 내용"], wrote=False)
    assert kind == "claim", "안내서(조회)만 읽고 '불러드릴게요' = 검사 대상 (실제 #2587)"
    kind, _ = agent._final_check(ctx, text, "23시55분에 나 불러줘", True, {"schedule_task"}, [], wrote=True)
    assert kind == "", "예약 도구를 실제로 썼으면 통과"


async def member_asks(r, user, **args):
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, user, Role.MEMBER, await r.db.get_settings(Room.CHAT))
    return await tools.execute("schedule_task", json.dumps(args, ensure_ascii=False), ctx)


@test
async def member_can_set_own_reminder_without_card_and_it_arrives():
    r = await room()
    out = await member_asks(r, A, when="30분 뒤", action="remind", text="메시지 보내기로 한 시간")
    assert "알람 저장함" in out and not r.bot.named("send_message"), out             # 카드 없이 바로
    [row] = [x for x in await r.db.schedules(Room.CHAT) if x["created_by"] == A.id]
    assert row["deliver"] == "me" and row["action"] == "remind"
    out = await member_asks(r, A, when="매일 09:00", action="post", text="공지")
    assert "나한테 오는 알람" in out                                                 # 방 공지는 관리자만
    for i in range(2):
        await member_asks(r, A, when=f"{i + 1}시간 뒤", action="remind", text="x")
    out = await member_asks(r, A, when="5시간 뒤", action="remind", text="x")
    assert "3개까지" in out, out
    await r.db._write("UPDATE schedules SET at_ts=? WHERE id=?", (int(time.time()) - 5, row["id"]))
    row = await r.db.get_schedule(Room.CHAT, row["id"])
    assert await cron.fire(r.svc, r.bot, row) is True                               # 멤버라도 관리자 검사로 꺼지지 않음
    sent = r.bot.named("send_message")[-1]
    assert sent[1] == A.id and "메시지 보내기로 한 시간" in sent[2], sent


@test
async def blocked_dm_falls_back_to_calling_in_the_room():
    r = await room()
    await member_asks(r, A, when="30분 뒤", action="remind", text="약속 시간")
    [row] = [x for x in await r.db.schedules(Room.CHAT) if x["created_by"] == A.id]
    r.bot.dm_blocked = {A.id}
    assert await cron.fire(r.svc, r.bot, row) is True
    sent = r.bot.named("send_message")[-1]
    assert sent[1] == Room.CHAT and "약속 시간" in sent[2] and str(A.id) in sent[2], sent   # 방에서 이름 불러 알림
    # 관리자가 만든 방 알림은 그대로 관리자 검사 (관리자가 아니게 되면 끔)
    sid = await r.db.add_schedule(Room.CHAT, kind="once", at_time="x", interval_min=None, title="", text="t",
                                  media_type=None, media_id=None, pin=False, created_by=A.id, action="remind",
                                  skill=None, at_ts=int(time.time()) - 5)
    assert await cron.fire(r.svc, r.bot, await r.db.get_schedule(Room.CHAT, sid)) is False
    assert BOSS.id in r.svc.perms.admins


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
