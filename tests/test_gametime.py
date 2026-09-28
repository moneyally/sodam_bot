"""장시간 게임 알림: python tests/run_all.py gametime

실제 요청: '소담아 이 방에서 12시간 이상 게임하는 사람 있으면 나 호출해' → 도구가 없어서 못 한다고 답함.
"""
import time
from types import SimpleNamespace

from fake_llm import Room, tool_call
from fakes import FakeQuery, fake_user, runner
from test_sanction_multi import A, B, BOSS, ask, room

from sodam import gametime, menu
from sodam.permissions import Role

test, run_all = runner()
H = 3600


async def gt_room(**kw):
    r = await room(gt_enabled=True, gt_setter=BOSS.id, **kw)
    r.bot.admins = [BOSS]
    return r


async def play(r, user, start, end, step=600):
    for ts in range(start, end + 1, step):
        await gametime.record(r.db, Room.CHAT, user.id, 30, ts)


async def press(r, user, data):
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


def sent_to(r, chat):
    return [c for c in r.bot.named("send_message") if c[1] == chat]


@test
async def game_messages_are_counted_only_when_on():
    msg = lambda t, dice=None: SimpleNamespace(text=t, dice=dice)  # noqa: E731
    assert gametime.is_game(msg("/ㅅㅌㅊ"), "") and gametime.is_game(msg("!슬롯 100"), "")
    assert gametime.is_game(msg("", dice=object()), "") and not gametime.is_game(msg("안녕 /ㅅㅌㅊ"), "")
    assert gametime.is_game(msg("/ㄱㄹㅈ@gamebot"), "/ㄱㄹㅈ /ㄷㄹ") and not gametime.is_game(msg("/ㅅㅌㅊ"), "/ㄱㄹㅈ")
    r = await room()
    await r.say(A, "/ㅅㅌㅊ")
    assert not await r.db._all("SELECT * FROM game_sessions"), "꺼져 있으면 기록 안 함"
    await r.db.set_setting(Room.CHAT, "gt_enabled", True)
    await r.say(A, "/ㅅㅌㅊ")
    await r.say(B, "그냥 대화")
    assert [x["user_id"] for x in await r.db._all("SELECT * FROM game_sessions")] == [A.id]


@test
async def session_resets_after_break():
    r = await room()
    t0 = 1_000_000
    await play(r, A, t0, t0 + 5 * H)
    await play(r, A, t0 + 5 * H + 31 * 60, t0 + 6 * H)          # 31분 쉼 → 새로 셈
    row = await r.db._one("SELECT * FROM game_sessions")
    assert row["start"] == t0 + 5 * H + 31 * 60, row


@test
async def alerts_once_after_threshold_with_buttons():
    r = await gt_room()
    now = int(time.time())
    await play(r, A, now - 11 * H, now)
    assert await gametime.check(r.svc, r.bot, now) == 0, "11시간은 아직"
    await play(r, A, now, now + H)
    assert await gametime.check(r.svc, r.bot, now + H) == 1
    assert await gametime.check(r.svc, r.bot, now + H + 600) == 0, "같은 세션은 한 번만"
    [room_msg] = sent_to(r, Room.CHAT)
    assert "12시간" in room_msg[2] and f"tg://user?id={BOSS.id}" in room_msg[2], room_msg[2]
    [dm] = sent_to(r, BOSS.id)
    data = [b.callback_data for row in dm[3]["reply_markup"].inline_keyboard for b in row]
    assert data == [f"m:gtb:{Room.CHAT}:{A.id}:m", f"m:gtb:{Room.CHAT}:{A.id}:ok"], data
    q = await press(r, A, data[0])                                   # 관리자 아님 → 못 누름
    assert not r.bot.named("restrict") and q.answers
    await press(r, BOSS, data[0])
    [mute] = r.bot.named("restrict")
    assert mute[2] == A.id and mute[4] is not None, mute


@test
async def auto_mute_skips_admins_and_notify_only_has_no_buttons():
    r = await gt_room(gt_action="auto", gt_mute_hours=1)
    now = int(time.time())
    await play(r, A, now - 13 * H, now)
    await play(r, BOSS, now - 13 * H, now)
    assert await gametime.check(r.svc, r.bot, now) == 2
    assert [c[2] for c in r.bot.named("restrict")] == [A.id], "관리자는 뮤트 안 함"
    assert any("채팅 금지했어요" in c[2] for c in sent_to(r, Room.CHAT))
    r2 = await gt_room(gt_action="notify")
    await play(r2, A, now - 13 * H, now)
    await gametime.check(r2.svc, r2.bot, now)
    assert sent_to(r2, BOSS.id)[0][3]["reply_markup"] is None


@test
async def off_or_unpaid_room_is_quiet():
    now = int(time.time())
    r = await gt_room()
    await r.db.set_setting(Room.CHAT, "gt_enabled", False)
    await play(r, A, now - 13 * H, now)
    assert await gametime.check(r.svc, r.bot, now) == 0

    async def unpaid(_):
        return False
    r = await gt_room()
    r.svc.paid_features = unpaid
    await play(r, A, now - 13 * H, now)
    assert await gametime.check(r.svc, r.bot, now) == 0


@test
async def ai_tool_turns_on_for_caller_admin_only():
    r = await room()
    res = await ask(r, BOSS, [tool_call("game_alert", {"on": True, "hours": 12})])
    s = await r.db.get_settings(Room.CHAT)
    assert s["gt_enabled"] and s["gt_setter"] == BOSS.id and s["gt_hours"] == 12 and "12시간" in res[0], res
    res = await ask(r, A, [tool_call("game_alert", {"on": False})], role=Role.MEMBER)
    assert "사용할 수 없음" in res[0] and (await r.db.get_settings(Room.CHAT))["gt_enabled"]


@test
async def copy_settings_only_to_rooms_i_admin():
    r = await gt_room(gt_hours=8)
    other, foreign = -1007777, -1006666
    await r.db.ensure_chat(other, "둘째 방")
    await r.db.ensure_chat(foreign, "남의 방")

    async def groups(svc, bot, uid):
        return [(Room.CHAT, "대표님들 소통방"), (other, "둘째 방")]
    orig, menu.admin_groups = menu.admin_groups, groups
    try:
        await press(r, BOSS, f"m:gtx:{Room.CHAT}:all")
        q = await press(r, BOSS, f"m:gtx:{Room.CHAT}:{foreign}")
    finally:
        menu.admin_groups = orig
    assert (await r.db.get_settings(other))["gt_hours"] == 8 and (await r.db.get_settings(other))["gt_enabled"]
    assert not (await r.db.get_settings(foreign))["gt_enabled"] and q.answers[-1][1] is True
