"""🔒 오너 잠금 뮤트/밴 (sodam/ownerlock.py · moderation.OwnerLocked · handlers.on_chat_member · panels/ownerlock.py).
2026-10-22 오너 '오너 아니면 관리자도 절대 못 풀게' (일루왕 백문주). python tests/run_all.py ownerlock

뮤테이션: unmute 관문 빼면 admin_cannot_unmute FAIL / on_status_change 재적용 빼면 remute_* FAIL /
'오너가 직접 풀면 잠금 해제' 빼면 owner_unmute_in_app FAIL / add 가 kind 안 넣으면 ban 쪽 FAIL.
"""
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner

import sodam.__main__  # noqa: F401  (실제 시작 순서로 패널·도구·설정 등록)
from sodam import commands, handlers, ownerlock, tools
from sodam.commands import CmdCtx
from sodam.moderation import OwnerLocked
from sodam.permissions import Role

test, run_all = runner()

CHAT = -1001000000777
OWNER, ADMIN, MEMBER = 900, 901, 904


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    svc.perms.owner_ids = {OWNER}
    await db.ensure_chat(CHAT, "大일루왕")
    bot = FakeBot()
    bot.member_status = {(CHAT, ADMIN): "administrator"}
    for uid, name in ((OWNER, "오너"), (ADMIN, "관리자"), (MEMBER, "백문주")):
        await db.upsert_user(fake_user(uid, name))
        await add_member(db, CHAT, fake_user(uid, name))
    return db, svc, bot


def ctx_of(svc, bot):
    return SimpleNamespace(bot=bot, bot_data={"svc": svc, "joins": {}, "cas_seen": set(),
                                              "tasks": set(), "chats": set()})


async def status_change(svc, bot, old_status, new_status, by, *, old_send=None, new_send=None, target=MEMBER):
    """멤버 상태 변경(chat_member)을 handlers.on_chat_member 로 — 관리자가 앱에서 푼 걸 흉내."""
    user = fake_user(target, "백문주")

    def m(st, send):
        return SimpleNamespace(status=st, user=user, can_send_messages=send,
                               is_member=st in ("member", "restricted", "administrator", "creator"))
    upd = SimpleNamespace(chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup", title="大일루왕"),
        old_chat_member=m(old_status, old_send), new_chat_member=m(new_status, new_send),
        from_user=fake_user(by, "누군가")))
    await handlers.on_chat_member(upd, ctx_of(svc, bot))


async def run_cmd(svc, bot, actor, role, text, target=MEMBER):
    user = fake_user(actor, "부른 사람")
    parsed = commands.parse(text, bot.username)
    msg = FakeMsg(CHAT, user, text, reply_to=FakeMsg(CHAT, fake_user(target, "백문주"), "글"))
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, role, parsed[1], parsed[2]), parsed[0])
    return msg.replies


# ── 저장·캐시 ──────────────────────────────────────────────
@test
async def add_remove_cache_and_kind():
    db, svc, bot = await setup()
    assert await ownerlock.locked_kind(db, CHAT, MEMBER) is None
    await ownerlock.add(db, CHAT, MEMBER, "mute", OWNER)
    assert await ownerlock.is_locked(db, CHAT, MEMBER) and await ownerlock.is_locked(db, CHAT, MEMBER, "mute")
    assert not await ownerlock.is_locked(db, CHAT, MEMBER, "ban")      # 종류 구분
    assert [r["user_id"] for r in await ownerlock.members(db, CHAT)] == [MEMBER]
    assert await ownerlock.remove(db, CHAT, MEMBER) is True
    assert await ownerlock.locked_kind(db, CHAT, MEMBER) is None
    assert await ownerlock.remove(db, CHAT, MEMBER) is False


# ── 소담 통한 해제 관문: 오너만 ─────────────────────────────
@test
async def admin_cannot_unmute_but_owner_can():
    db, svc, bot = await setup()
    await ownerlock.add(db, CHAT, MEMBER, "mute", OWNER)
    try:
        await svc.mod.unmute(bot, CHAT, MEMBER, ADMIN)
        assert False, "관리자가 잠금 뮤트를 풀 수 있으면 안 됨"
    except OwnerLocked:
        pass
    await svc.mod.unmute(bot, CHAT, MEMBER, OWNER)        # 오너는 됨 (관문 통과)
    assert ("restrict", CHAT, MEMBER) in [(c[0], c[1], c[2]) for c in bot.calls]


@test
async def admin_cannot_unban_but_owner_can():
    db, svc, bot = await setup()
    await ownerlock.add(db, CHAT, MEMBER, "ban", OWNER)
    try:
        await svc.mod.unban(bot, CHAT, MEMBER, ADMIN)
        assert False, "관리자가 잠금 밴을 풀 수 있으면 안 됨"
    except OwnerLocked:
        pass
    await svc.mod.unban(bot, CHAT, MEMBER, OWNER)         # 오너는 됨
    assert bot.named("unban")


# ── 관리자가 앱에서 풀면 즉시 다시 ─────────────────────────
@test
async def remute_when_admin_unmutes_in_app():
    db, svc, bot = await setup()
    await ownerlock.add(db, CHAT, MEMBER, "mute", OWNER)
    await status_change(svc, bot, "restricted", "member", by=ADMIN, old_send=False)
    assert bot.named("restrict"), "관리자가 풀면 소담이 바로 다시 뮤트해야 함"
    row = await db._one("SELECT * FROM mod_log WHERE action='ownerlock' AND detail LIKE '자동 재적용%'")
    assert row and row["target_id"] == MEMBER
    assert any(c[0] == "send_message" and c[1] == OWNER for c in bot.calls), "오너에게 알림"
    assert await ownerlock.is_locked(db, CHAT, MEMBER)   # 잠금 유지


@test
async def reban_when_admin_unbans_in_app():
    db, svc, bot = await setup()
    await ownerlock.add(db, CHAT, MEMBER, "ban", OWNER)
    await status_change(svc, bot, "kicked", "left", by=ADMIN)
    assert bot.named("ban"), "관리자가 밴을 풀면 소담이 바로 다시 밴해야 함"
    assert await ownerlock.is_locked(db, CHAT, MEMBER, "ban")


@test
async def owner_unmute_in_app_removes_lock():
    db, svc, bot = await setup()
    await ownerlock.add(db, CHAT, MEMBER, "mute", OWNER)
    await status_change(svc, bot, "restricted", "member", by=OWNER, old_send=False)
    assert not bot.named("restrict"), "오너가 직접 풀면 다시 걸지 않음"
    assert not await ownerlock.is_locked(db, CHAT, MEMBER), "오너가 풀면 잠금도 해제"


@test
async def promoted_to_admin_notifies_owner_no_remute():
    db, svc, bot = await setup()
    await ownerlock.add(db, CHAT, MEMBER, "mute", OWNER)
    await status_change(svc, bot, "restricted", "administrator", by=ADMIN, old_send=False)
    assert not bot.named("restrict"), "관리자로 승격되면 소담이 뮤트 못 걺"
    assert any(c[0] == "send_message" and c[1] == OWNER and "승격" in c[2] for c in bot.calls)


# ── 명령 (.잠금뮤트 / .잠금해제) ────────────────────────────
@test
async def owner_command_locks_and_unlocks():
    db, svc, bot = await setup()
    replies = await run_cmd(svc, bot, OWNER, Role.OWNER, ".잠금뮤트")
    assert await ownerlock.is_locked(db, CHAT, MEMBER, "mute"), replies
    assert bot.named("restrict"), replies
    assert any("영구 잠금" in r for r in replies), replies
    # 해제
    replies = await run_cmd(svc, bot, OWNER, Role.OWNER, ".오너잠금해제")
    assert not await ownerlock.is_locked(db, CHAT, MEMBER), replies
    assert any("풀었어요" in r for r in replies), replies


@test
async def lockban_blocks_room_and_voice():
    db, svc, bot = await setup()
    replies = await run_cmd(svc, bot, OWNER, Role.OWNER, ".잠금밴")
    assert await ownerlock.is_locked(db, CHAT, MEMBER, "ban"), replies
    assert bot.named("ban"), replies
    assert any("음성방" in r for r in replies), replies


# ── AI 도구 (오너가 말로) ──────────────────────────────────
@test
async def ai_tool_owner_only_and_locks():
    db, svc, bot = await setup()
    P = __import__("sodam.panels.ownerlock", fromlist=["t_owner_lock"])
    # 관리자(오너 아님)는 거절
    ctx_admin = tools.ToolCtx(svc, bot, CHAT, fake_user(ADMIN, "관리자"), Role.ADMIN, {})
    out = await P.t_owner_lock(ctx_admin, {"name": "백문주"})
    assert "오너만" in out and not await ownerlock.is_locked(db, CHAT, MEMBER), out
    # 오너는 잠금
    ctx_owner = tools.ToolCtx(svc, bot, CHAT, fake_user(OWNER, "오너"), Role.OWNER, {})
    out = await P.t_owner_lock(ctx_owner, {"name": "백문주", "kind": "mute", "on": True})
    assert await ownerlock.is_locked(db, CHAT, MEMBER, "mute"), out
    # 오너가 풀기
    out = await P.t_owner_lock(ctx_owner, {"name": "백문주", "on": False})
    assert not await ownerlock.is_locked(db, CHAT, MEMBER), out


if __name__ == "__main__":
    run_all()
