"""🛡️ 보호 멤버 (sodam/protect.py · permissions.shield · panels/protect.py): 관리자가 소담으로 제재 못 하게.
2026-10-20 오너 '밴 못하게 처리'. python tests/run_all.py protect"""
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner

import sodam.__main__  # noqa: F401  (실제 시작 순서로 모든 설정·패널 등록)
from sodam import commands, protect
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.panels import protect as P

test, run_all = runner()

CHAT = -1001000000555
OWNER, ADMIN, MEMBER = 800, 801, 804


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    svc.perms.owner_ids = {OWNER}
    bot = FakeBot()
    bot.member_status = {(CHAT, ADMIN): "administrator"}
    for uid, name in ((OWNER, "오너"), (ADMIN, "문주"), (MEMBER, "쓰잔")):
        await db.upsert_user(fake_user(uid, name))
        await add_member(db, CHAT, fake_user(uid, name))
    return db, svc, bot


async def sanction(svc, bot, actor, role, text, target):
    user = fake_user(actor, "부른 사람")
    parsed = commands.parse(text, bot.username)
    msg = FakeMsg(CHAT, user, text, reply_to=FakeMsg(CHAT, fake_user(target, f"대상{target}"), "글"))
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, role, parsed[1], parsed[2]), parsed[0])
    return msg.replies


@test
async def protect_add_remove_and_cache():
    db, svc, bot = await setup()
    assert not await protect.is_protected(db, CHAT, MEMBER)
    await protect.add(db, CHAT, MEMBER, OWNER)
    assert await protect.is_protected(db, CHAT, MEMBER)        # 캐시도 바로 반영 (add 가 uncache)
    assert [r["user_id"] for r in await protect.members(db, CHAT)] == [MEMBER]
    assert await protect.remove(db, CHAT, MEMBER) is True
    assert not await protect.is_protected(db, CHAT, MEMBER)
    assert await protect.remove(db, CHAT, MEMBER) is False


@test
async def admin_cannot_sanction_protected_but_owner_can():
    db, svc, bot = await setup()
    await protect.add(db, CHAT, MEMBER, OWNER)
    # 관리자가 소담으로 경고 시도 → 거절
    replies = await sanction(svc, bot, ADMIN, Role.ADMIN, ".warn", MEMBER)
    assert await db.warning_count(CHAT, MEMBER) == 0, replies
    assert any("보호 멤버" in r for r in replies), replies
    # 오너는 보호 멤버도 제재 가능 (우선권)
    replies = await sanction(svc, bot, OWNER, Role.OWNER, ".warn", MEMBER)
    assert await db.warning_count(CHAT, MEMBER) == 1, replies


@test
async def auto_detect_sanction_also_blocked_on_protected():
    """자동탐지 버튼(스팸방패·사기의심·수배·패드립)은 shield 를 안 거치고 svc.mod.ban/mute 를 바로 부른다.
    그래도 보호 멤버는 막혀야 함 — moderation._protect_gate 가 단일 관문 (2026-10-27 버그 조사)."""
    from sodam.moderation import Protected
    db, svc, bot = await setup()
    await protect.add(db, CHAT, MEMBER, OWNER)
    # 관리자가 누른 자동탐지 밴/뮤트 (mod.ban/mute 직접 호출) → Protected 로 거절
    for call in (svc.mod.ban(bot, CHAT, MEMBER, ADMIN, "스팸 의심"),
                 svc.mod.mute(bot, CHAT, MEMBER, 60, ADMIN, "패드립"),
                 svc.mod.kick(bot, CHAT, MEMBER, ADMIN, "수배")):
        try:
            await call
            assert False, "보호 멤버인데 제재가 실행됨"
        except Protected:
            pass
    # 오너가 누르면 통과 (보호 우선권)
    await svc.mod.ban(bot, CHAT, MEMBER, OWNER, "오너 제재")
    assert bot.member_status.get((CHAT, MEMBER)) in ("kicked", "left"), bot.member_status


@test
async def protect_command_sets_and_lists():
    db, svc, bot = await setup()
    user = fake_user(ADMIN, "문주")
    parsed = commands.parse(".보호", bot.username)
    msg = FakeMsg(CHAT, user, ".보호", reply_to=FakeMsg(CHAT, fake_user(MEMBER, "쓰잔"), "글"))
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, Role.ADMIN, parsed[1], parsed[2]), parsed[0])
    assert await protect.is_protected(db, CHAT, MEMBER), msg.replies
    assert any("보호 멤버로 지정" in r for r in msg.replies), msg.replies
    # 목록
    parsed = commands.parse(".보호목록", bot.username)
    msg2 = FakeMsg(CHAT, user, ".보호목록")
    await commands.dispatch(CmdCtx(svc, bot, msg2, CHAT, user, Role.ADMIN, parsed[1], parsed[2]), parsed[0])
    assert any("쓰잔" in r for r in msg2.replies), msg2.replies


@test
async def ai_tool_protect_and_unprotect():
    from sodam import tools
    db, svc, bot = await setup()
    ctx = SimpleNamespace(svc=svc, bot=bot, chat_id=CHAT, caller=fake_user(ADMIN, "문주"), name_notes=[])
    out = await P.t_protect_member(ctx, {"name": "쓰잔", "on": True})
    assert await protect.is_protected(db, CHAT, MEMBER) and "보호 멤버로 지정" in out, out
    out = await P.t_protect_member(ctx, {"name": "쓰잔", "on": False})
    assert not await protect.is_protected(db, CHAT, MEMBER) and "해제" in out, out


@test
async def ai_tool_needs_restrict_right():
    db, svc, bot = await setup()
    # 관리자가 아닌 멤버는 거절 (may 실패)
    ctx = SimpleNamespace(svc=svc, bot=bot, chat_id=CHAT, caller=fake_user(MEMBER, "쓰잔"), name_notes=[])
    out = await P.t_protect_member(ctx, {"name": "쓰잔", "on": True})
    assert "권한" in out and not await protect.is_protected(db, CHAT, MEMBER), out
