"""게임 상태·끝내기·다시 시작 AI 도구 + 숫자 섞인 답 🤔: python tests/run_all.py game_control
실제 사례: '죄인러브3' 을 쳤는데 아무 반응이 없어 '고장났다' → '소담아 고쳐라' 에 AI 가 '제가 고칠 수 없어요'."""
import asyncio

import test_wordchain2 as W
from fakes import fake_user, runner

from sodam import games
from sodam.permissions import Role
from sodam.tools import ToolCtx, execute

test, run_all = runner()
ADMIN = fake_user(1, "방장")


async def tool(svc, bot, user, role, args):
    ctx = ToolCtx(svc, bot, W.CHAT, user, role, await svc.db.get_settings(W.CHAT))
    import json
    out = await execute("game_control", json.dumps(args, ensure_ascii=False), ctx)
    return out, ctx


@test
async def digit_mixed_attempt_gets_reaction_not_silence():
    db, svc, bot, g = await W.setup()
    g.cancel_timer()
    g.last, g.used = "기차", {"기차"}
    ok, m = await W.text(svc, W.B, "차표3")
    assert ok and (m.message_id, "🤔") in W.reactions(bot), W.reactions(bot)
    ok, _ = await W.text(svc, W.B, "차 한잔 하실분")                   # 띄어쓴 평범한 말은 그대로 채팅
    assert not ok
    ok, _ = await W.text(svc, W.B, "ok")                               # 첫 글자도 안 맞으면 채팅
    assert not ok
    g.cancel_timer()


@test
async def ended_game_is_remembered_and_explained():
    db, svc, bot, g = await W.setup()
    g.cancel_timer()
    g.last, g.used = "우산", {"우산"}
    await g._timeout()
    st = svc.games.status(W.CHAT)
    assert "진행 중인 게임 없음" in st and "시간 초과" in st and "'우산'" in st, st
    out, _ = await tool(svc, bot, W.B, Role.MEMBER, {"action": "status"})
    assert "시간 초과" in out


@test
async def stop_only_starter_or_admin_and_restart_works():
    db, svc, bot, g = await W.setup()                                   # A 가 시작
    g.cancel_timer()
    out, _ = await tool(svc, bot, W.B, Role.MEMBER, {"action": "stop"})
    assert "시작한 사람이나 관리자만" in out and not g.finished
    out, _ = await tool(svc, bot, W.B, Role.MEMBER, {"action": "restart"})
    assert "진행 중" in out and not g.finished
    out, ctx = await tool(svc, bot, ADMIN, Role.ADMIN, {"action": "stop"})
    assert g.finished and ctx.quiet, out
    out, ctx = await tool(svc, bot, W.B, Role.MEMBER, {"action": "restart"})   # 끝난 뒤엔 누구나 다시 시작
    g2 = svc.games.active[W.CHAT]
    assert g2 is not g and not g2.finished and ctx.quiet and "시작" in out, out
    assert type(g2) is games.WordChain                                   # 방금 하던 종류로
    g2.cancel_timer()


@test
async def restart_keeps_turn_mode_kind():
    db, svc, bot, g = await W.setup("끝말잇기 차례")
    await svc.games.stop(W.CHAT)
    await tool(svc, bot, W.A, Role.MEMBER, {"action": "restart"})
    assert type(svc.games.active[W.CHAT]) is games.WordChainTurn
    svc.games.active[W.CHAT].cancel_timer()
    await asyncio.sleep(0)


if __name__ == "__main__":
    run_all()
