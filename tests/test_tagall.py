"""📣 전체 태그 (sodam/panels/tagall.py): python tests/run_all.py tagall

실제 사례 2026-10-02 일루왕방: '방전체인원 태그해줘 새로들어온사람말고' → 기능이 없어 새 멤버 4명만 태그.
본코드 경로: 관리자 말 → AI 도구 mention_all → 확인 카드 → 버튼(menu.on_callback) → 5명씩 전송.
"""
import asyncio

from fake_llm import Room, ScriptedLLM, reply, tool_call
from fakes import FakeQuery, fake_user, runner

from sodam import commands, menu, tools
from sodam.commands import CmdCtx
from sodam.panels import members as members_panel
from sodam.panels import tagall
from sodam.permissions import Role

test, run_all = runner()
BOSS = fake_user(1, "방장", "boss")
PEOPLE = [fake_user(100 + i, f"멤버{i}") for i in range(12)]
GONE = fake_user(99, "나간사람")
BOT = fake_user(50, "다른봇", "otherbot", is_bot=True)
tagall.SEND_GAP = 0


async def world(llm=None):
    r = Room()
    r.llm = llm or ScriptedLLM([])
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, "greet_enabled": False})
    for u in (BOSS, *PEOPLE, GONE, BOT):
        await r.join(u)
    await members_panel.mark(r.db, Room.CHAT, GONE.id, left=True)
    return r


async def press(r, user, data):
    r.svc.menu_limiter._hits.clear()
    q = FakeQuery(user.id, user, data)
    q.message = type("M", (), {"chat_id": Room.CHAT, "chat": type("C", (), {"id": Room.CHAT, "type": "supergroup"})()})()
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


def card(r):
    c = next(c for c in reversed(r.bot.named("send_message")) if "전체 태그" in c[2] and c[3].get("reply_markup"))
    return c, [b.callback_data for row in c[3]["reply_markup"].inline_keyboard for b in row]


def tag_msgs(r):
    return [c for c in r.bot.named("send_message") if "tg://user?id=" in c[2] and "전체 태그" not in c[2]]


async def done():
    await asyncio.gather(*list(tagall._tasks), return_exceptions=True)


@test
async def admin_says_tag_all_card_then_five_per_message_without_left_bot_or_requester():
    r = await world(ScriptedLLM([tool_call("mention_all", {"text": "공지 확인 부탁해요"}), reply("확인 버튼 눌러 주세요")]))
    await r.say(BOSS, "소담아 방전체인원 태그해줘 새로들어온사람말고")
    c, data = card(r)
    assert "12명" in c[2] and "5명씩 3개 메시지" in c[2] and "공지 확인 부탁해요" in c[2], c[2]
    assert not tag_msgs(r), "카드만 — 누르기 전엔 안 보냄"
    q = await press(r, BOSS, data[0])
    await done()
    sent = tag_msgs(r)
    assert [c[2].count("tg://user?id=") for c in sent] == [5, 5, 2], [c[2] for c in sent]
    ids = " ".join(c[2] for c in sent)
    for u in PEOPLE:
        assert f"id={u.id}\"" in ids, u.id
    for u in (BOSS, GONE, BOT):
        assert f"id={u.id}\"" not in ids, f"{u.first_name} 은 빠져야 함"
    assert any(c[2] == "📣 공지 확인 부탁해요" for c in r.bot.named("send_message")), "할 말 먼저"
    temp = await r.db._all("SELECT * FROM temp_msgs WHERE chat_id=?", (Room.CHAT,))
    assert len(temp) == 3, "태그 메시지 3개는 10분 뒤 자동 삭제 예약 (할 말은 남김)"
    log = await r.db._one("SELECT detail FROM mod_log WHERE action='tagall'")
    assert log and log["detail"] == "12/12명", log
    # 6시간에 한 번
    await tagall.offer(r.svc, r.bot, Room.CHAT, BOSS, "")
    _, data2 = card(r)
    q = await press(r, BOSS, data2[0])
    await done()
    assert len(tag_msgs(r)) == 3 and "6시간에 한 번" in str(q.edits[-1] if q.edits else q.answers), (q.edits, q.answers)


@test
async def member_cannot_and_tool_hidden_for_members():
    r = await world()
    names = {t.name for t in tools.available(Role.MEMBER, {}, False)}
    assert "mention_all" not in names and "mention_all" in {t.name for t in tools.available(Role.ADMIN, {}, False)}
    await tagall.offer(r.svc, r.bot, Room.CHAT, BOSS, "")
    _, data = card(r)
    q = await press(r, PEOPLE[0], data[0])                       # 남(멤버)이 카드를 눌러도
    await done()
    assert not tag_msgs(r), "요청한 관리자만"
    r.svc.perms.admins.discard(BOSS.id)                          # 카드 띄운 뒤 관리자에서 내려감
    q = await press(r, BOSS, data[0])
    await done()
    assert not tag_msgs(r), "누를 때 관리자 다시 확인"


class FakeMT:
    bot_ready = True

    def __init__(self, users):
        self.users = users

    async def participants(self, chat_id):
        return self.users


def part(uid, name, **kw):
    return {"id": uid, "first_name": name, "last_name": "", "username": "", "is_bot": False, "deleted": False,
            "min": False, **kw}


@test
async def full_roster_from_mtproto_fills_db_then_tags_everyone():
    r = await world()
    before = await r.db._one("SELECT last_seen FROM members WHERE chat_id=? AND user_id=?", (Room.CHAT, PEOPLE[0].id))
    users = ([part(BOSS.id, "방장")] + [part(u.id, u.first_name) for u in PEOPLE[:11]]      # PEOPLE[11] 은 명단에 없음 = 나감
             + [part(200 + i, f"말없던사람{i}") for i in range(3)]                          # 한 번도 말 안 한 사람
             + [part(300, "", deleted=True), part(BOT.id, "다른봇", is_bot=True), part(GONE.id, "돌아온사람")])
    r.svc.mtproto = FakeMT(users)

    async def count(chat_id):
        return len(users)
    r.bot.get_chat_member_count = count
    await tagall.offer(r.svc, r.bot, Room.CHAT, BOSS, "")
    c, data = card(r)
    # 11(남은 기존) + 3(새로 앎) + 돌아온 사람 1 = 15 (방장·봇·탈퇴 계정 제외)
    assert "텔레그램 전체 18명 중 멤버 <b>15명</b>" in c[2], c[2]
    left = {x["user_id"] for x in await r.db._all("SELECT user_id FROM member_left WHERE chat_id=?", (Room.CHAT,))}
    assert left == {PEOPLE[11].id}, left
    new = await r.db._one("SELECT m.last_seen, m.joined_at, u.first_name FROM members m JOIN users u USING(user_id) "
                          "WHERE m.chat_id=? AND m.user_id=200", (Room.CHAT,))
    assert new and new["first_name"] == "말없던사람0" and new["last_seen"] is None and new["joined_at"] is None
    after = await r.db._one("SELECT last_seen FROM members WHERE chat_id=? AND user_id=?", (Room.CHAT, PEOPLE[0].id))
    assert after["last_seen"] == before["last_seen"], "마지막 말한 때는 안 바뀜"
    assert not await r.db._one("SELECT 1 FROM users WHERE user_id=300"), "탈퇴 계정은 안 넣음"
    await press(r, BOSS, data[0])
    await done()
    ids = " ".join(x[2] for x in tag_msgs(r))
    assert all(f"id={i}\"" in ids for i in (200, 201, 202, GONE.id)) and f"id={PEOPLE[11].id}\"" not in ids, ids


@test
async def roster_not_complete_does_not_mark_anyone_left():
    r = await world()
    r.svc.mtproto = FakeMT([part(PEOPLE[0].id, "멤버0")])

    async def count(chat_id):
        return 290                                               # 명단이 덜 옴 → 나간 사람 판단 안 함
    r.bot.get_chat_member_count = count
    await tagall.offer(r.svc, r.bot, Room.CHAT, BOSS, "")
    left = {x["user_id"] for x in await r.db._all("SELECT user_id FROM member_left WHERE chat_id=?", (Room.CHAT,))}
    assert left == {GONE.id}, left
    r.svc.mtproto = FakeMT(None)
    assert await tagall.sync_members(r.svc, r.bot, Room.CHAT) is None, "헬퍼 실패 = DB 명단 그대로"
    r.svc.mtproto = FakeMT([part(777, "새사람")])
    r.svc.mtproto.bot_ready = False                              # 헬퍼 연결 전·flood 대기 중
    assert await tagall.sync_members(r.svc, r.bot, Room.CHAT) is None
    assert not await r.db._one("SELECT 1 FROM users WHERE user_id=777")


@test
async def stop_button_halts_midway():
    r = await world()
    tagall._running[Room.CHAT] = {"stop": False, "sent": 0, "total": 0}

    async def sleep(_):
        tagall._running[Room.CHAT]["stop"] = True               # 첫 묶음 뒤 [⏹ 멈추기]
    sent = await tagall.run(r.svc, r.bot, Room.CHAT, BOSS.id, "", sleep=sleep)
    assert sent == 5 and len(tag_msgs(r)) == 1 and Room.CHAT not in tagall._running
    log = await r.db._one("SELECT detail FROM mod_log WHERE action='tagall'")
    assert log["detail"] == "5/12명 (멈춤)", log


@test
async def anyone_can_say_stop_and_false_claim_is_rechecked():
    # 실제 2026-10-03 일루왕: 전체 태그 중 멤버 '소담아 멈춰' → 멈추는 기능이 없어 '멈췄습니다' 거짓말, 256명 끝까지
    # → 오너 결정: 일반 멤버도 멈출 수 있게 (태그 폭탄을 맞는 쪽이 멤버)
    r = await world(ScriptedLLM([tool_call("stop_tag_all"), reply("멈췄어요")]))
    tagall._running[Room.CHAT] = {"stop": False, "sent": 10, "total": 12}
    await r.say(PEOPLE[0], "소담아 멈춰")
    assert tagall._running[Room.CHAT]["stop"] is True
    tool_out = [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]
    assert "10/12명" in tool_out[0], tool_out
    log = await r.db._one("SELECT actor_id, detail FROM mod_log WHERE action='tagall_stop'")
    assert log and log["actor_id"] == PEOPLE[0].id, "누가 멈췄는지 기록"
    tagall._running.pop(Room.CHAT, None)
    assert "없음" in tagall.stop(Room.CHAT) and "말하지 말 것" in tagall.stop(Room.CHAT)
    assert "stop_tag_all" in {t.name for t in tools.available(Role.MEMBER, {}, False)}
    # 도구 없이 '멈췄습니다' → 보내기 전 검사가 한 번 다시 물음
    r2 = await world(ScriptedLLM([reply("멈췄습니다, 대표님."), tool_call("stop_tag_all"), reply("지금 도는 태그는 없어요.")]))
    m = await r2.say(PEOPLE[1], "소담아 멈춰")
    assert m.replies == ["지금 도는 태그는 없어요."], m.replies


@test
async def dot_command_for_admins():
    r = await world()
    msg = r.msg(BOSS, ".전체태그 모여주세요")
    cmd, args, argstr = commands.parse(msg.text, "sodambot")
    await commands.dispatch(CmdCtx(r.svc, r.bot, msg, Room.CHAT, BOSS, Role.ADMIN, args, argstr), cmd)
    c, _ = card(r)
    assert "모여주세요" in c[2]
    assert cmd.role == Role.ADMIN


if __name__ == "__main__":
    asyncio.run(run_all())
