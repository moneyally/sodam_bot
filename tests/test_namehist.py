"""이름·아이디 변경 기록 (SangMata 방식): python tests/test_namehist.py"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from harness import html_errors

from sodam import commands, handlers, menu, namehist
from sodam.commands import CmdCtx
from sodam.permissions import Role

test, run_all = runner()
CHAT, OTHER = -1001111, -1002222


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    await db.ensure_chat(CHAT, "방")
    await db.ensure_chat(OTHER, "다른 방")
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "chats": set(), "cas_seen": set(), "tasks": set(), "joins": {}})
    await db.set_setting(CHAT, "cas_enabled", False)
    return db, svc, bot, ctx


def user(uid, first, username=None):
    return fake_user(uid, first, username)


async def group_say(ctx, u, text="안녕하세요", chat=CHAT):
    m = FakeMsg(chat, u, text)
    m.chat = SimpleNamespace(id=chat, title="방", type="supergroup")
    m.sender_chat = None
    await handlers.on_group_message(SimpleNamespace(message=m), ctx)
    return m


def notices(bot, chat=CHAT):
    return [c[2] for c in bot.named("send_message") if c[1] == chat and "이름 변경" in c[2]]


@test
async def change_is_recorded_and_announced():
    db, svc, bot, ctx = await setup()
    await group_say(ctx, user(5, "김대표", "kim"))
    assert not notices(bot)                                             # 처음 본 사람은 기록만
    await group_say(ctx, user(5, "김대표", "kim"))
    assert not notices(bot) and len(await namehist.history(db, 5)) == 1  # 그대로면 기록 안 늘어남
    await group_say(ctx, user(5, "<b>관리자</b>", "admin_real"))       # 사칭 시도
    n = notices(bot)
    assert len(n) == 1 and "김대표" in n[0] and "&lt;b&gt;관리자&lt;/b&gt;" in n[0] and "@admin_real" in n[0]
    assert not html_errors(n[0]) and "<code>5</code>" in n[0]
    await db.set_setting(CHAT, "name_change_notice", False)
    await group_say(ctx, user(5, "또바뀜", "admin_real"))
    assert len(notices(bot)) == 1 and len(await namehist.history(db, 5)) == 3  # 알림 꺼도 기록은 남음


@test
async def bots_and_channel_posts_are_ignored():
    db, svc, bot, ctx = await setup()
    assert await namehist.record(db, fake_user(9, "봇", "x_bot", is_bot=True)) is None
    assert not await namehist.history(db, 9)


async def cmd(svc, bot, chat, u, text, role=Role.MEMBER, reply_to=None):
    c, args, argstr = commands.parse(text, "sodambot")
    m = FakeMsg(chat, u, text, reply_to=reply_to)
    await commands.dispatch(CmdCtx(svc, bot, m, chat, u, role, args, argstr), c)
    return m.replies[0]


@test
async def group_command_by_reply_mention_and_old_username():
    db, svc, bot, ctx = await setup()
    kim, me = user(5, "김대표", "kim"), user(6, "나", "me")
    await group_say(ctx, kim)
    await group_say(ctx, user(5, "박대표", "park"))
    await group_say(ctx, me)
    r = await cmd(svc, bot, CHAT, me, ".이름기록", reply_to=SimpleNamespace(from_user=user(5, "박대표", "park")))
    assert "박대표" in r and "김대표" in r and "변경 1회" in r and "← 지금" in r
    assert "김대표" in await cmd(svc, bot, CHAT, me, ".이름기록 @park")
    assert "박대표" in await cmd(svc, bot, CHAT, me, ".이름기록 @kim")     # 예전 아이디로도 찾음
    assert "내 이름 기록" in await cmd(svc, bot, CHAT, me, ".이름기록")
    r = await cmd(svc, bot, CHAT, me, ".이름기록 @nobody")
    assert "못 찾았" in r


@test
async def dm_lookup_limited_to_shared_groups():
    db, svc, bot, ctx = await setup()
    a, b, stranger = user(5, "A", "aaa"), user(6, "B", "bbb"), user(7, "외부인", "ccc")
    await group_say(ctx, a)
    await group_say(ctx, b)
    await group_say(ctx, stranger, chat=OTHER)
    assert "A" in await cmd(svc, bot, 6, b, ".이름기록 @aaa")               # 같은 방
    assert "같은 그룹" in await cmd(svc, bot, 6, b, ".이름기록 @ccc")      # 다른 방 사람
    assert "같은 그룹" in await cmd(svc, bot, 6, b, ".이름기록 7")
    assert "외부인" in await cmd(svc, bot, 1, user(1, "오너"), ".이름기록 7", role=Role.OWNER)
    assert "기록에 없" in await cmd(svc, bot, 6, b, ".이름기록 99999999999999999999")  # 이상한 숫자


@test
async def dm_panel_mine_and_query():
    db, svc, bot, ctx = await setup()
    a, b = user(5, "A", "aaa"), user(6, "B", "bbb")
    await group_say(ctx, a)
    await group_say(ctx, user(5, "A2", "aaa2"))
    await group_say(ctx, b)
    text, kb = await menu.main_menu(svc, bot, 5)
    assert any(x.callback_data == "m:nh" for row in kb.inline_keyboard for x in row)
    q = FakeQuery(5, a)
    await menu.on_callback(svc, bot, q, ["nh"])
    assert "내 이름 기록" in q.edits[-1] and "A2" in q.edits[-1] and not html_errors(q.edits[-1])
    q = FakeQuery(6, b)
    await menu.on_callback(svc, bot, q, ["nhq"])
    assert 6 in svc.inputs
    m = FakeMsg(6, b, "@aaa")                                             # 예전 아이디로 조회
    await handlers.on_private(SimpleNamespace(message=m), ctx)
    assert "A2" in m.replies[0] and "변경 1회" in m.replies[0] and 6 not in svc.inputs


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
