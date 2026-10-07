"""한글 제재 명령 별명 (2026-10-08 고객 요청 '.채금 .경고 .강퇴 이런 식으로'): python tests/run_all.py kor_aliases
기존 이름(.뮤트·.킥·.밴·/mute)은 그대로, 같은 명령으로 이어짐."""
from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

from sodam import commands
from sodam.commands import CmdCtx
from sodam.permissions import Role

test, run_all = runner()
CHAT = -100990
ADMIN, BAD = fake_user(1, "대표"), fake_user(20, "도배범")


def same(a: str, b: str) -> bool:
    return commands.parse(a, "sodambot")[0] is commands.parse(b, "sodambot")[0]


@test
def korean_aliases_reach_the_same_commands_and_old_names_stay():
    assert same(".채금 @x 1h", ".뮤트 @x 1h") and same(".채팅금지 @x", "/mute @x")
    assert same(".채금해제 @x", ".뮤트해제 @x") and same(".채금풀기 @x", "/unmute @x")
    assert same(".강퇴 @x", ".킥 @x") and same(".강제퇴장 @x", "/kick @x")
    assert same(".벤 @x", ".밴 @x") and same(".경고 @x", "/warn @x")
    cmd, args, _ = commands.parse(".채금 @x 30m 도배", "sodambot")
    assert cmd.names[0] == "뮤트" and args == ["@x", "30m", "도배"] and cmd.role == Role.ADMIN and cmd.right == "restrict"
    assert commands.parse(".강퇴 @x", "sodambot")[0].names[0] == "킥"


@test
async def chaegeum_actually_mutes_and_gangtoe_kicks():
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN.id,))
    await db.ensure_chat(CHAT, "방")
    await db.upsert_user(BAD)
    await db.touch_member(CHAT, BAD.id)
    bot = FakeBot(admins=(ADMIN.id,))

    async def run(text):
        cmd, args, argstr = commands.parse(text, "sodambot")
        reply = FakeMsg(CHAT, BAD, "도배")
        msg = FakeMsg(CHAT, ADMIN, text, reply_to=reply)
        await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, ADMIN, Role.ADMIN, args, argstr), cmd)
        return msg
    await run(".채금 1h")
    assert any(c[0] == "restrict" and c[2] == BAD.id for c in bot.calls), bot.calls
    await run(".강퇴")
    assert any(c[0] == "ban" and c[2] == BAD.id for c in bot.calls) and any(c[0] == "unban" for c in bot.calls), "강퇴 = 내보내기(재입장 가능)"


if __name__ == "__main__":
    run_all()
