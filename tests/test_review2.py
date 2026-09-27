"""2차 검토 회귀 테스트: python tests/run_all.py review2

① 오너 1:1 제재 방 찾기 — 이름이 정확히 같은 방 우선, 한 글자 방 이름이 아무 말에나 걸리지 않음, 후보에 ID
② 봇이 강퇴된 방을 '권한 있음'으로 봄  ③ 자동 인사 전송이 실패해도 10분간 AI 인사를 막음
④ 무기한(사칭) 뮤트 보고의 '1일로 연장' 버튼 = 실제로는 1일로 단축
"""
from telegram.error import Forbidden, NetworkError

from fake_llm import Room, tool_call
from fakes import FakeBot, add_member, cfg, fake_user, make_db, runner

from sodam import greet
from sodam.moderation import owner_kb
from sodam.permissions import Permissions, Role
from sodam.tools import ToolCtx, execute
from test_sanction_multi import OWNER, ask, room

test, run_all = runner()


async def owner_room(r, name):
    return (await ask(r, OWNER, [tool_call("owner_sanction", {"room": name, "action": "mute", "names": ["조이킨"],
                                                              "reason": "x"})], chat_id=OWNER.id, role=Role.OWNER))[0]


@test
async def owner_sanction_room_match_exact_first_and_ids():
    r = await room()
    r.svc.perms.owner_ids = {OWNER.id}
    await r.db._write("UPDATE chats SET title='FIRST' WHERE chat_id=?", (Room.CHAT,))
    await r.db.ensure_chat(-1009999, "FIRST 2호점")
    await r.db.ensure_chat(-1008888, "A")
    assert "확인 버튼을 보냈음" in await owner_room(r, "first"), "이름이 정확히 같은 방이 있으면 그 방"
    r.svc.pending.clear()
    res = await owner_room(r, "FIRS")                                   # 두 방 다 포함 → 후보를 ID 와 함께
    assert "여러 개" in res and "(-1009999)" in res and f"({Room.CHAT})" in res and "(-1008888)" not in res, res
    r.svc.pending.clear()
    assert "확인 버튼을 보냈음" in await owner_room(r, "Alpha First방"), "한 글자 방 'A' 는 말 안에 있어도 안 걸림"


@test
async def kicked_room_has_no_bot_rights():
    db = await make_db()
    bot = FakeBot()

    async def gone(*a, **k):
        raise Forbidden("Forbidden: bot was kicked from the supergroup chat")
    bot.get_chat_member = gone
    assert await Permissions(cfg(db.path), db).bot_can_moderate(bot, -100123) is False


@test
async def failed_auto_greet_does_not_block_ai_greet():
    r = await Room().open(settings={"greet_enabled": True, "greet_template": "{names} 환영"})
    new, boss = fake_user(30, "김희중"), fake_user(1, "방장")
    await add_member(r.db, Room.CHAT, new)
    g = r.svc.greeter = greet.Greeter(r.svc)
    ok = r.bot.send_message

    async def boom(*a, **k):
        raise NetworkError("httpx.ReadError: ")
    r.bot.send_message = boom
    g.queue(r.bot, Room.CHAT, new.id, "김희중")
    g._tasks.pop(Room.CHAT).cancel()
    await g.flush(r.bot, Room.CHAT)
    r.bot.send_message = ok
    assert not g.auto_greeted(Room.CHAT, new.id)
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, boss, Role.ADMIN, await r.db.get_settings(Room.CHAT))
    assert "자동 입장 인사" not in await execute("greet_members", '{"names":["김희중"]}', ctx)


@test
async def indefinite_mute_report_has_no_shorten_button():
    r = await Room().open(admins={1})
    r.bot.admins = [fake_user(1, "방장")]
    reports = []

    async def report(bot, text, kb=None):
        reports.append(kb)
    r.svc.mod.report = report
    assert await r.svc.mod.check_impersonation(r.bot, Room.CHAT, fake_user(66, "방장"))
    [kb] = reports
    texts = [b.text for b in kb.inline_keyboard[0]]
    assert "🔊 풀기" in texts and not any("연장" in t for t in texts), texts
    assert any("연장" in b.text for b in owner_kb(-100, 5, "mute").inline_keyboard[0])
