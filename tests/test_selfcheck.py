"""자기 점검 (claude식 조각2): 뮤트·밴 하고 나서 진짜 걸렸나 get_chat_member 로 확인,
텔레그램이 씹었으면(drop_sanction) 한 번 더. AI 비용 0 (코드만). 오너 2026-10-21."""
from fakes import FakeBot, make_db, make_svc, runner

test, run_all = runner()
CHAT, ADMIN, BAD = -100777, 1, 9


def _bans(bot):
    return [c for c in bot.calls if c[0] == "ban"]


def _restr(bot):
    return [c for c in bot.calls if c[0] == "restrict"]


@test
async def mute_applied_no_retry():
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    bot = FakeBot()
    await svc.mod.mute(bot, CHAT, BAD, 60, ADMIN, "도배")
    assert len(_restr(bot)) == 1, "한 번에 걸리면 재시도 없음"


@test
async def mute_retried_once_when_dropped():
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    bot = FakeBot()
    bot.drop_sanction = True   # 텔레그램이 씹는 상황 흉내 → 상태 안 바뀜
    await svc.mod.mute(bot, CHAT, BAD, 60, ADMIN, "도배")
    assert len(_restr(bot)) == 2, "안 걸렸으면 한 번 더 (딱 1회)"


@test
async def ban_retried_once_when_dropped():
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    bot = FakeBot()
    bot.drop_sanction = True
    await svc.mod.ban(bot, CHAT, BAD, ADMIN, "사기")
    assert len(_bans(bot)) == 2, "밴도 안 걸렸으면 한 번 더"


@test
async def ban_applied_no_retry():
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    bot = FakeBot()
    await svc.mod.ban(bot, CHAT, BAD, ADMIN, "사기")
    assert len(_bans(bot)) == 1


@test
async def verify_failure_is_swallowed():
    # get_chat_member 자체가 터져도 제재는 그대로 (원래 동작 유지, 무한 재시도 없음)
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,))
    bot = FakeBot()

    async def boom(*a, **k):
        raise RuntimeError("권한 없음")
    bot.get_chat_member = boom
    await svc.mod.mute(bot, CHAT, BAD, 60, ADMIN, "도배")
    assert len(_restr(bot)) == 1, "확인 실패 = 재시도 안 함"


if __name__ == "__main__":
    run_all()
