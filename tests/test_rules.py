"""알림 규칙 (정해진 부품 조립 · 확인 버튼 저장 · 코드 실행): python tests/run_all.py rules"""
import time
from datetime import datetime
from types import SimpleNamespace

from fake_llm import Room, tool_call
from fakes import FakeJobQueue, FakeMsg, FakeQuery, fake_user, runner
from test_sanction_multi import A, B, BOSS, ask, room

from sodam import handlers, menu, rules
from sodam.permissions import Role

test, run_all = runner()


async def press(r, user, data):
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


async def make(r, **spec):
    return await rules.add(r.svc, Room.CHAT, BOSS.id, {"action": "dm", **spec})


def dms(r, uid=BOSS.id):
    return [c[2] for c in r.bot.named("send_message") if c[1] == uid]


@test
async def ai_tool_card_saves_only_for_requester():
    r = await room()
    res = await ask(r, BOSS, [tool_call("alert_rule", {"trigger": "keyword", "value": "입금"})])
    assert "확인 버튼을 보냈음" in res[0] and not await rules.room_rules(r.db, Room.CHAT), res
    [card] = [c for c in r.bot.named("send_message") if "규칙을 만들까요" in c[2]]
    assert "입금" in card[2] and "나오면" in card[2]
    ok, _ = [b.callback_data for b in card[3]["reply_markup"].inline_keyboard[0]]
    await press(r, A, ok)
    assert not await rules.room_rules(r.db, Room.CHAT), "다른 사람은 못 누름"
    await press(r, BOSS, ok)
    [rule] = await rules.room_rules(r.db, Room.CHAT)
    assert (rule["trig"], rule["arg"], rule["created_by"]) == ("keyword", "입금", BOSS.id)
    res = await ask(r, A, [tool_call("alert_rule", {"trigger": "join"})], role=Role.MEMBER)
    assert "사용할 수 없음" in res[0]
    res = await ask(r, BOSS, [tool_call("alert_rule", {"trigger": "keyword", "value": "a"})])
    assert "못 만듦" in res[0], "한 글자 낱말 안 됨"


@test
async def keyword_fires_dm_with_link_once_per_cooldown():
    r = await room()
    await make(r, trig="keyword", arg="입 금")                        # 띄어쓰기·대소문자 무시
    await r.say(BOSS, "입금 확인했어요")                               # 만든 사람 말은 안 울림
    await r.say(A, "오늘 입금 언제 돼요?")
    await r.say(B, "입금 저도요")                                     # 쿨다운 10분
    await r.say(A, "그냥 대화")
    [dm] = dms(r)
    assert "입금 언제" in dm and "캎이바라요" in dm and "https://t.me/c/777/" in dm, dm


@test
async def newbie_only_and_user_rule_with_call():
    r = await room()
    await r.db._write("UPDATE members SET joined_at=? WHERE user_id=?", (int(time.time()) - 3 * 86400, A.id))
    await make(r, trig="keyword", arg="코인", who="newbie")
    await r.say(A, "코인 얘기")                                       # 오래된 멤버
    await r.say(B, "코인 사세요")                                     # 방금 들어온 사람
    assert len(dms(r)) == 1 and "코인 사세요" in dms(r)[0]
    await make(r, trig="user", arg=str(A.id), action="call")
    await r.say(A, "안녕하세요")
    calls = [c[2] for c in r.bot.named("send_message") if c[1] == Room.CHAT and "🔔" in c[2]]
    assert calls and f"tg://user?id={BOSS.id}" in calls[0] and "안녕하세요" in calls[0], calls


@test
async def join_and_quiet_rules():
    r = await room()
    await make(r, trig="join")
    ctx = SimpleNamespace(bot=r.bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": r.svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})
    await handlers.handle_new_member(ctx, Room.CHAT, "방", fake_user(77, "새사람"))
    assert any("새사람님이 들어왔어요" in d for d in dms(r))
    rid = await make(r, trig="quiet", arg="3", action="post", text="다들 뭐해요?", cooldown=1)
    now = int(time.time())
    await r.db.log_message(Room.CHAT, A.id, 1, "마지막 말", ts=now - 2 * 3600)
    assert await rules.check_quiet(r.svc, r.bot, now) == 0
    await r.db.log_message(Room.CHAT, A.id, 2, "마지막 말", ts=now - 4 * 3600)
    await r.db._write("DELETE FROM messages WHERE msg_id=1 AND chat_id=?", (Room.CHAT,))
    assert await rules.check_quiet(r.svc, r.bot, now) == 1
    await r.db._write("UPDATE alert_rules SET last_fired=? WHERE id=?", (now - 3 * 3600, rid))   # 쿨다운은 지남
    assert await rules.check_quiet(r.svc, r.bot, now) == 0, "같은 조용함(마지막 말 뒤 이미 울림)엔 한 번"
    assert any(c[2] == "다들 뭐해요?" for c in r.bot.named("send_message") if c[1] == Room.CHAT)


@test
async def creator_demoted_turns_rule_off_and_daily_cap():
    r = await room()
    rid = await rules.add(r.svc, Room.CHAT, A.id, {"trig": "keyword", "arg": "입금", "action": "dm"})
    await r.say(B, "입금")
    assert not dms(r, A.id) and not (await rules.room_rules(r.db, Room.CHAT))[0]["enabled"]
    rid = await make(r, trig="keyword", arg="출금", cooldown=1)
    day = datetime.now(r.svc.cfg.tz).strftime("%Y-%m-%d")
    await r.db._write("UPDATE alert_rules SET day=?, fired=? WHERE id=?", (day, rules.DAILY_CAP, rid))
    await r.say(B, "출금")
    assert not dms(r), "하루 상한"


@test
async def panel_add_toggle_and_other_room_ids_are_ignored():
    r = await room()
    await press(r, BOSS, f"m:in:{Room.CHAT}:rlk")
    msg = FakeMsg(BOSS.id, BOSS, "입금 | 신규")
    await menu.handle_input(r.svc, r.bot, msg)
    [rule] = await rules.room_rules(r.db, Room.CHAT)
    assert (rule["arg"], rule["who"]) == ("입금", "newbie"), dict(rule)
    await press(r, BOSS, f"m:rlt:{Room.CHAT}:{rule['id']}:0")
    assert not (await rules.room_rules(r.db, Room.CHAT))[0]["enabled"]
    other = await rules.add(r.svc, -1005555, BOSS.id, {"trig": "join", "action": "dm"})
    await press(r, BOSS, f"m:rld:{Room.CHAT}:{other}:1")
    assert await rules.room_rules(r.db, -1005555), "다른 방 규칙은 못 지움"
