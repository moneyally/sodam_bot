"""오너 우선권 (방 설정 owner_override 기본 켬, 2026-10-16 오너 '타 방에서도 오너면 관리자에게 경고·밴'):
오너는 관리자에게도 경고 · 소담이 임명한 관리자면 강등 뒤 뮤트·밴 · 남이 임명한 관리자 뮤트·밴은 텔레그램 제한 안내 ·
방장·봇은 늘 보호 · 오너 아닌 관리자는 예전처럼 관리자 제재 불가."""
import asyncio

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

import sodam.__main__  # noqa: F401  (실제 시작 순서로 모든 설정 등록)
from sodam import commands
from sodam.commands import CmdCtx
from sodam.permissions import Role

test, run_all = runner()

CHAT = -1001000000777
OWNER, ADMIN, OTHER_ADMIN, BOSS, MEMBER = 900, 901, 902, 903, 904


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN, OTHER_ADMIN, BOSS})
    svc.perms.owner_ids = {OWNER}
    bot = FakeBot()
    bot.member_status = {(CHAT, a): "administrator" for a in (ADMIN, OTHER_ADMIN)} | {(CHAT, BOSS): "creator"}
    for uid, name in ((OWNER, "자이"), (ADMIN, "문주"), (OTHER_ADMIN, "미우"), (BOSS, "방장"), (MEMBER, "멤버")):
        await db.upsert_user(fake_user(uid, name))
    return db, svc, bot


async def run(svc, bot, actor, role, text, target):
    user = fake_user(actor, "부른 사람")
    parsed = commands.parse(text, bot.username)
    msg = FakeMsg(CHAT, user, text, reply_to=FakeMsg(CHAT, fake_user(target, f"대상{target}"), "글"))
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, role, parsed[1], parsed[2]), parsed[0])
    return msg.replies


@test
async def owner_can_warn_an_admin():
    db, svc, bot = await setup()
    replies = await run(svc, bot, OWNER, Role.OWNER, ".warn", ADMIN)
    assert await db.warning_count(CHAT, ADMIN) == 1, replies
    assert any("경고 1회" in r for r in replies), replies


@test
async def non_owner_admin_still_cannot_warn_an_admin():
    db, svc, bot = await setup()
    replies = await run(svc, bot, OTHER_ADMIN, Role.ADMIN, ".warn", ADMIN)
    assert await db.warning_count(CHAT, ADMIN) == 0
    assert any("관리자나 봇은 제재할 수 없어요" in r for r in replies), replies


@test
async def creator_and_room_switch_still_protect():
    db, svc, bot = await setup()
    replies = await run(svc, bot, OWNER, Role.OWNER, ".warn", BOSS)
    assert await db.warning_count(CHAT, BOSS) == 0 and any("방장" in r for r in replies), replies
    await db.set_setting(CHAT, "owner_override", False)
    replies = await run(svc, bot, OWNER, Role.OWNER, ".warn", ADMIN)
    assert await db.warning_count(CHAT, ADMIN) == 0 and any("오너 우선권이 꺼져" in r for r in replies), replies


@test
async def owner_ban_on_admin_not_appointed_by_bot_is_explained_not_tried():
    db, svc, bot = await setup()
    replies = await run(svc, bot, OWNER, Role.OWNER, ".ban", ADMIN)
    assert not [c for c in bot.calls if c[0] in ("ban", "promote")], bot.calls
    assert any("방장(또는 임명한 사람)만" in r for r in replies), replies


@test
async def owner_ban_on_admin_appointed_by_bot_demotes_first():
    db, svc, bot = await setup()
    bot.editable_admins = {(CHAT, ADMIN)}
    await run(svc, bot, OWNER, Role.OWNER, ".ban", ADMIN)
    kinds = [c[0] for c in bot.calls if c[0] in ("promote", "ban")]
    assert kinds == ["promote", "ban"], bot.calls
    rights = next(c[3] for c in bot.calls if c[0] == "promote")
    assert rights and not any(rights.values()), rights          # 전부 False = 관리자 해제


@test
async def warn_escalation_on_locked_admin_keeps_warning_and_explains():
    db, svc, bot = await setup()
    await db.set_setting(CHAT, "warn_mute_at", 1)
    replies = await run(svc, bot, OWNER, Role.OWNER, ".warn", ADMIN)
    assert await db.warning_count(CHAT, ADMIN) == 1
    assert not [c for c in bot.calls if c[0] == "restrict"], bot.calls
    assert any("방장(또는 임명한 사람)만" in r for r in replies), replies


@test
async def plain_member_unchanged():
    db, svc, bot = await setup()
    await run(svc, bot, OTHER_ADMIN, Role.ADMIN, ".ban", MEMBER)
    assert [c[0] for c in bot.calls if c[0] in ("promote", "ban")] == ["ban"], bot.calls


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
