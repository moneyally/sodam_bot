"""'소담아 소유자 호출해' → 방 만든 사람 태그 (2026-10-16 전국 소통방: '소유자'라는 사람을 찾다 실패하고 요청자를 태그함)."""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam.permissions import Role
from sodam.tools import ToolCtx, t_mention

test, run_all = runner()
CHAT = -100777


async def _ctx():
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot()
    owner, admin, me = fake_user(11, "율무"), fake_user(12, "부관리자"), fake_user(13, "자이")
    for u in (owner, admin, me):
        await db.upsert_user(u)
    robot = SimpleNamespace(id=99, is_bot=True, first_name="봇", last_name=None, username="bot")

    async def admins(chat_id):
        return [SimpleNamespace(user=owner, status="creator"), SimpleNamespace(user=admin, status="administrator"),
                SimpleNamespace(user=robot, status="administrator")]
    bot.get_chat_administrators = admins
    return ToolCtx(svc, bot, CHAT, me, Role.MEMBER, await db.get_settings(CHAT))


@test
async def owner_word_tags_creator():
    for word in ("소유자", "방장", "방 주인", "owner"):
        ctx = await _ctx()
        out = await t_mention(ctx, {"names": [word]})
        assert [uid for uid, _ in ctx.mentions] == [11], (word, ctx.mentions, out)
        assert "태그 대상: 율무" in out


@test
async def admins_word_tags_human_admins():
    ctx = await _ctx()
    await t_mention(ctx, {"names": ["관리자들"]})
    assert [uid for uid, _ in ctx.mentions] == [11, 12], ctx.mentions


@test
async def plain_name_still_resolves_normally():
    ctx = await _ctx()
    out = await t_mention(ctx, {"names": ["소유자김"]})
    assert ctx.mentions == [] and "못 찾은 이름" in out


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
