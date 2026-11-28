"""🗣 1:1 AI 허용 명단 (sodam/dmai.py). 2026-11-28 오너 요청. python tests/run_all.py dmai"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc, runner  # noqa: E402

from sodam import dmai, handlers  # noqa: E402
from sodam.commands import CmdCtx  # noqa: E402
from sodam.permissions import Role  # noqa: E402

test, run_all = runner()
BOSS = fake_user(1, "대표", "boss")       # make_svc admins={1}; 오너로도 쓸 것
GUEST = fake_user(777, "손님", "guest")


async def _svc_ctx(**kw):
    db = await make_db()
    svc = await make_svc(db, admins={1}, owner_ids={1}, dm_ai_owner_only=True, **kw)
    dmai._ALLOW = None   # 캐시 초기화 (테스트 간 전역 오염 방지)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    return db, svc, bot, ctx


@test
async def added_person_can_use_1to1_ai_others_blocked():
    db, svc, bot, ctx = await _svc_ctx()
    # 명단에 없으면 1:1 AI 막힘 (오너 전용)
    assert await handlers._within_ai_quota(ctx, GUEST.id, GUEST.id, Role.MEMBER) is False
    assert any("운영자 전용" in c[2] for c in bot.named("send_message"))
    # 오너가 추가하면 통과
    await dmai.add(db, GUEST.id, by=BOSS.id)
    assert await handlers._within_ai_quota(ctx, GUEST.id, GUEST.id, Role.MEMBER) is True
    assert await dmai.is_allowed(db, GUEST.id) is True
    # 빼면 다시 막힘
    await dmai.remove(db, GUEST.id)
    assert await handlers._within_ai_quota(ctx, GUEST.id, GUEST.id, Role.MEMBER) is False


@test
async def allowlisted_person_has_a_generous_daily_cap():
    db, svc, bot, ctx = await _svc_ctx()
    await db.set_state(0, "dm_ai_allow_per_day", "2")       # 하루 2번으로 줄여 검사
    await dmai.add(db, GUEST.id, by=BOSS.id)
    got = [await handlers._within_ai_quota(ctx, GUEST.id, GUEST.id, Role.MEMBER) for _ in range(3)]
    assert got == [True, True, False], got                  # 넉넉한 한도 안에서만
    assert any("다 썼어요" in c[2] for c in bot.named("send_message"))


@test
async def added_person_is_not_owner():
    # 허용은 '1:1 AI 대화'만 — 역할은 그대로 멤버, 오너 권한 0
    db, svc, bot, ctx = await _svc_ctx()
    await dmai.add(db, GUEST.id, by=BOSS.id)
    assert await handlers._within_ai_quota(ctx, GUEST.id, GUEST.id, Role.MEMBER) is True
    # 게이트는 넘지만 역할(MEMBER)은 안 올라감 → 오너/관리 도구는 tools.offered 가 안 줌
    from sodam import tools
    member_tools = {t.name for t in tools.offered(Role.MEMBER, in_dm=True)}
    assert "dm_ai_allow" not in member_tools and "owner_revenue" not in member_tools


@test
async def owner_always_allowed_regardless_of_list():
    db, svc, bot, ctx = await _svc_ctx()
    assert await handlers._within_ai_quota(ctx, BOSS.id, BOSS.id, Role.OWNER) is True
    assert await dmai.is_allowed(db, BOSS.id) is False       # 명단엔 없어도 오너라 통과


@test
async def per_day_default_when_unset_or_bad():
    db, svc, bot, ctx = await _svc_ctx()
    assert await dmai.per_day(db) == dmai.PER_DAY_DEFAULT
    await db.set_state(0, "dm_ai_allow_per_day", "0")        # 0·헛값은 기본으로
    assert await dmai.per_day(db) == dmai.PER_DAY_DEFAULT
    await db.set_state(0, "dm_ai_allow_per_day", "50")
    assert await dmai.per_day(db) == 50


@test
async def owner_command_adds_and_removes_by_id():
    db, svc, bot, ctx = await _svc_ctx()
    dmai.register_commands()
    msg = FakeMsg(BOSS.id, BOSS, ".AI허용 123456789")

    def cmd(args):
        return CmdCtx(svc=svc, bot=bot, msg=msg, chat_id=BOSS.id, user=BOSS, role=Role.OWNER,
                      args=args, argstr=" ".join(args))
    await dmai.c_allow(cmd(["123456789"]))
    assert await dmai.is_allowed(db, 123456789) is True
    await dmai.c_unallow(cmd(["123456789"]))
    assert await dmai.is_allowed(db, 123456789) is False
    # 멤버가 명령 치면 조용히 무시 (Role.MEMBER → 아무 변화 없음)
    member = CmdCtx(svc=svc, bot=bot, msg=msg, chat_id=BOSS.id, user=GUEST, role=Role.MEMBER,
                    args=["123456789"], argstr="123456789")
    await dmai.c_allow(member)
    assert await dmai.is_allowed(db, 123456789) is False


if __name__ == "__main__":
    run_all()
