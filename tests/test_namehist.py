"""이름·아이디 변경 기록: python tests/test_namehist.py"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import TZ, FakeBot, FakeJobQueue, FakeMsg, FakeQuery, add_member, fake_user, make_db, make_svc, runner
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


async def deliver(ctx, m, private=False):
    """PTB 처럼: 모든 업데이트 앞의 이름 기록(on_any_update) → 메시지 처리."""
    upd = SimpleNamespace(message=m, effective_chat=m.chat if not private else SimpleNamespace(id=m.chat_id))
    await handlers.on_any_update(upd, ctx)
    await (handlers.on_private if private else handlers.on_group_message)(upd, ctx)
    await asyncio.gather(*ctx.bot_data["tasks"], return_exceptions=True)  # 태그 알림 등 백그라운드 훅


async def group_say(ctx, u, text="안녕하세요", chat=CHAT):
    m = FakeMsg(chat, u, text)
    m.chat = SimpleNamespace(id=chat, title="방", type="supergroup")
    m.sender_chat = None
    await deliver(ctx, m)
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
    kb = [c for c in bot.named("send_message") if "이름 변경" in c[2]][-1][3]["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert btns[0].callback_data == "nh:all:5"                          # 알림에서 바로 전체 기록
    assert any(b.url and "startgroup=true" in b.url for b in btns)      # 우리 방에도 추가 (성장 고리)
    assert not any("sangmata" in (b.url or b.text).lower() for b in btns)
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
async def dm_lookup_anyone():
    db, svc, bot, ctx = await setup()
    a, b, stranger = user(5, "A", "aaa"), user(6, "B", "bbb"), user(7, "외부인", "ccc")
    await group_say(ctx, a)
    await group_say(ctx, b)
    await group_say(ctx, stranger, chat=OTHER)
    assert "A" in await cmd(svc, bot, 6, b, ".이름기록 @aaa")               # 같은 방
    assert "외부인" in await cmd(svc, bot, 6, b, ".이름기록 @ccc")        # 다른 방 사람도 (사용자 결정: 전부 조회)
    assert "외부인" in await cmd(svc, bot, 6, b, ".이름기록 7")
    assert "외부인" in await cmd(svc, bot, 1, user(1, "오너"), ".이름기록 7", role=Role.OWNER)
    assert "못 찾았" in await cmd(svc, bot, 6, b, ".이름기록 99999999999999999999")  # 이상한 숫자


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
    await deliver(ctx, m, private=True)
    assert "A2" in m.replies[0] and "변경 1회" in m.replies[0] and 6 not in svc.inputs


@test
async def names_and_usernames_listed_separately():
    db, svc, bot, ctx = await setup()
    for first, uname in (("김대표", "kim"), ("김대표", "kim2"), ("박대표", "kim2"), ("박대표", None), ("이대표", "lee")):
        await namehist.record(db, user(5, first, uname))
    await group_say(ctx, user(5, "이대표", "lee"))
    t = await namehist.history_text(db, 5, TZ)
    assert "이름 변경 2회 · 아이디 변경 3회" in t, t
    body = t.split("🔗 아이디")[1].split("\n\n")[0]
    vals = [ln.split("</code> ")[1].replace(" ← 지금", "") for ln in body.splitlines() if "</code> " in ln]
    assert vals == ["@lee", "(아이디 없음)", "@kim2", "@kim"], vals
    names = await namehist.history_text(db, 5, TZ, mode="names")
    assert "🔗 아이디" not in names and "👤 이름" in names
    users = await namehist.history_text(db, 5, TZ, mode="usernames")
    assert "👤 이름" not in users and "@kim" in users


@test
async def huge_history_fits_telegram_limits():
    import html as H
    import re
    db, svc, bot, ctx = await setup()
    for i in range(200):
        await namehist.record(db, user(5, f"{i:03d}" + "가" * 61, f"u{i:03d}" + "x" * 28))
    for mode in namehist.MODES:
        t = await namehist.history_text(db, 5, TZ, mode=mode)
        assert len(H.unescape(re.sub(r"<[^>]+>", "", t))) <= 4096, (mode, len(t))
        assert not html_errors(t)
        for row in namehist.buttons(99999999999, mode).inline_keyboard:
            assert all(len(b.callback_data.encode()) <= 64 for b in row if b.callback_data)
    assert "전체 기록 버튼" in await namehist.history_text(db, 5, TZ)


@test
async def all_command_aliases_work():
    db, svc, bot, ctx = await setup()
    await group_say(ctx, user(5, "김대표", "kim"))
    await group_say(ctx, user(5, "박대표", "park"))
    me = user(6, "나", "me")
    await group_say(ctx, me)
    for text, expect in ((".기록 @park", "최근 기록"), ("/history @park", "최근 기록"), (".전체기록 @park", "전체 기록"),
                         ("/allhistory 5", "전체 기록"), (".이름조회 @park", "이름 기록"), ("/check_name 5", "이름 기록"),
                         (".아이디조회 @kim", "아이디 기록"), ("/check_username @park", "아이디 기록"),
                         (".내기록", "내 이름 기록"), ("/myhistory", "내 이름 기록"), (".기록 박대표", "최근 기록")):
        r = await cmd(svc, bot, CHAT, me, text)
        assert expect in r, (text, r)
    r = await cmd(svc, bot, CHAT, me, ".기록", reply_to=SimpleNamespace(from_user=fake_user(999, "봇", is_bot=True)))
    assert "봇 계정" in r


@test
async def group_lookup_anyone_bot_has_seen():
    db, svc, bot, ctx = await setup()
    outsider = user(7, "외부인", "outsider")
    await group_say(ctx, outsider, chat=OTHER)
    me = user(6, "나", "me")
    await group_say(ctx, me)
    assert "외부인" in await cmd(svc, bot, CHAT, me, ".기록 7")            # 다른 방 사람도 숫자 ID 로
    assert "외부인" in await cmd(svc, bot, CHAT, me, ".기록 @outsider")    # @아이디로도
    await namehist.record(db, user(8, "옛이름", "shared_old"))            # 예전 아이디: 이 방 멤버가 먼저
    await namehist.record(db, user(8, "새이름", "new8"))
    await namehist.record(db, user(6, "나", "shared_old"))
    await namehist.record(db, user(6, "나", "me"))
    assert "내 이름 기록" in await cmd(svc, bot, CHAT, me, ".기록 @shared_old")
    assert "못 찾았" in await cmd(svc, bot, CHAT, me, ".기록 @nobody_here")


@test
async def result_buttons_recheck_permission():
    from fakes import FakeQuery
    db, svc, bot, ctx = await setup()
    a, b, outsider = user(5, "A", "aaa"), user(6, "B", "bbb"), user(7, "외부인", "ccc")
    await group_say(ctx, a)
    await group_say(ctx, b)
    await group_say(ctx, outsider, chat=OTHER)
    q = FakeQuery(CHAT, b)                                              # 그룹에서 멤버 기록 → 전체
    await handlers.on_callback(SimpleNamespace(callback_query=_q(q, "nh:all:5")), ctx)
    assert "전체 기록" in q.edits[-1] and q.kb.inline_keyboard[0][1].text.startswith("●")
    q = FakeQuery(6, b)                                                 # 1:1: 다른 방 사람도 조회 가능
    await handlers.on_callback(SimpleNamespace(callback_query=_q(q, "nh:names:7")), ctx)
    assert "외부인" in q.edits[-1]
    q = FakeQuery(6, b)                                                 # 기록 없는 ID
    await handlers.on_callback(SimpleNamespace(callback_query=_q(q, "nh:all:424242")), ctx)
    assert "기록이 없어요" in q.edits[-1]
    for bad in ("nh:zzz:5", "nh:all:-5", "nh:all:99999999999999999999", "nh", "nh:all:²"):
        q = FakeQuery(6, b)
        await handlers.on_callback(SimpleNamespace(callback_query=_q(q, bad)), ctx)
        assert not q.edits and len(q.answers) == 1, bad


def _q(q, data):
    q.data = data
    return q


@test
async def forwarded_message_lookup_in_dm():
    db, svc, bot, ctx = await setup()
    a, b = user(5, "A", "aaa"), user(6, "B", "bbb")
    await group_say(ctx, a)
    await group_say(ctx, user(5, "A2", "aaa"))
    await group_say(ctx, b)
    a_now = user(5, "A2", "aaa")                                        # 전달에는 지금 이름이 담겨 옴
    m = FakeMsg(6, b, "아무 글")
    m.forward_origin = SimpleNamespace(sender_user=a_now)
    await deliver(ctx, m, private=True)
    assert "A2" in m.replies[0] and "이름 변경 1회" in m.replies[0]
    m = FakeMsg(6, b, "숨긴 사람 글")
    m.forward_origin = SimpleNamespace(sender_user=None, sender_user_name="숨김")
    await deliver(ctx, m, private=True)
    assert "숨김" in m.replies[0]
    m = FakeMsg(6, b, "")                                               # 사진만 전달해도
    m.forward_origin = SimpleNamespace(sender_user=a_now)
    await deliver(ctx, m, private=True)
    assert "A2" in m.replies[0]


@test
async def name_notice_command_admin_only():
    db, svc, bot, ctx = await setup()
    admin, member = user(1, "방장"), user(6, "멤버")
    r = await cmd(svc, bot, CHAT, member, ".이름알림 끄기")
    assert "관리자만" in r and (await db.get_settings(CHAT))["name_change_notice"] is True
    r = await cmd(svc, bot, CHAT, admin, ".이름알림 끄기", role=Role.ADMIN)
    assert "꺼짐" in r and (await db.get_settings(CHAT))["name_change_notice"] is False


@test
async def reply_forward_mention_users_are_recorded_too():
    db, svc, bot, ctx = await setup()
    speaker, replied, fwd, tagged = user(5, "말한이"), user(6, "원글이", "orig"), user(7, "전달원"), user(8, "멘션됨")
    m = FakeMsg(CHAT, speaker, "안녕", reply_to=SimpleNamespace(from_user=replied))
    m.chat = SimpleNamespace(id=CHAT, title="방", type="supergroup")
    m.sender_chat = None
    m.forward_origin = SimpleNamespace(sender_user=fwd)
    m.entities = (SimpleNamespace(type="text_mention", user=tagged, offset=0, length=2),)
    await deliver(ctx, m)
    for u in (speaker, replied, fwd, tagged):
        assert await namehist.history(db, u.id), u.first_name
    assert not await namehist.history(db, 999)


@test
async def dm_plain_id_or_username_shows_all_history():
    db, svc, bot, ctx = await setup()
    await group_say(ctx, user(5550001002, "김대표", "kim"))
    await group_say(ctx, user(5550001002, "박대표", "park"))
    for text in ("@kim", "5550001002", "@park"):
        m = FakeMsg(6, user(6, "B"), text)
        await deliver(ctx, m, private=True)
        assert "전체 기록" in m.replies[0] and "박대표" in m.replies[0], text
    m = FakeMsg(6, user(6, "B"), "@nobody_x")
    await deliver(ctx, m, private=True)
    assert "못 찾았" in m.replies[0]


@test
async def every_update_kind_is_recorded():
    db, svc, bot, ctx = await setup()
    U = [user(100 + i, f"사람{i}", f"p{i}") for i in range(12)]
    grp = SimpleNamespace(id=CHAT, type="supergroup", title="방")
    cm = lambda u: SimpleNamespace(user=u, status="member", is_member=None)  # noqa: E731
    updates = [
        SimpleNamespace(effective_chat=grp, edited_message=SimpleNamespace(from_user=U[0])),         # 수정
        SimpleNamespace(effective_chat=grp, message_reaction=SimpleNamespace(user=U[1])),           # 반응
        SimpleNamespace(effective_chat=grp, chat_join_request=SimpleNamespace(from_user=U[2])),     # 가입 요청
        SimpleNamespace(effective_chat=grp, callback_query=SimpleNamespace(from_user=U[3])),        # 버튼
        SimpleNamespace(effective_chat=grp, chat_member=SimpleNamespace(                            # 상태 변경
            from_user=U[4], old_chat_member=cm(U[5]), new_chat_member=cm(U[5]))),
        SimpleNamespace(effective_chat=grp, message=SimpleNamespace(                                # 입장·퇴장
            from_user=U[6], new_chat_members=(U[7],), left_chat_member=None)),
        SimpleNamespace(effective_chat=grp, message=SimpleNamespace(                                # 고정·외부 답장
            from_user=U[8], pinned_message=SimpleNamespace(from_user=U[9]),
            external_reply=SimpleNamespace(origin=SimpleNamespace(sender_user=U[10])))),
        SimpleNamespace(effective_chat=grp, poll_answer=SimpleNamespace(user=U[11])),              # 투표
    ]
    for upd in updates:
        await handlers.on_any_update(upd, ctx)
    missing = [u.first_name for u in U if not await namehist.history(db, u.id)]
    assert not missing, missing
    bot_user = fake_user(55, "봇", "x_bot", is_bot=True)
    await handlers.on_any_update(SimpleNamespace(effective_chat=grp, callback_query=SimpleNamespace(from_user=bot_user)), ctx)
    assert not await namehist.history(db, 55)


@test
async def reaction_from_renamed_user_triggers_notice():
    db, svc, bot, ctx = await setup()
    await namehist.record(db, user(5, "원래이름", "orig"))
    grp = SimpleNamespace(id=CHAT, type="supergroup", title="방")
    await handlers.on_any_update(SimpleNamespace(effective_chat=grp, message_reaction=SimpleNamespace(
        user=user(5, "사칭관리자", "orig"))), ctx)
    n = notices(bot)
    assert len(n) == 1 and "사칭관리자" in n[0]


class SweepBot(FakeBot):
    def __init__(self, names, fail_after=None):
        super().__init__()
        self.names, self.fail_after, self.asked, self.attempts = names, fail_after, [], 0

    async def get_chat_member(self, chat_id, user_id):
        from telegram.error import RetryAfter
        self.attempts += 1
        if self.fail_after is not None and len(self.asked) >= self.fail_after:
            raise RetryAfter(5)
        self.asked.append((chat_id, user_id))
        first, uname, status = self.names[user_id]
        return SimpleNamespace(user=user(user_id, first, uname), status=status)


@test
async def sweep_catches_silent_member_rename():
    import time as _t
    db, svc, bot, ctx = await setup()
    for uid in (5, 6, 7):
        await add_member(db, CHAT, user(uid, f"조용{uid}", f"q{uid}"))
        await namehist.record(db, user(uid, f"조용{uid}", f"q{uid}"))
    sb = SweepBot({5: ("바꾼이름", "q5", "member"), 6: ("조용6", "q6", "member"), 7: ("나간사람새이름", "q7", "left")})
    assert await namehist.sweep(svc, sb) == 3
    n = notices(sb)
    assert len(n) == 1 and "바꾼이름" in n[0]                             # 나간 사람은 기록만, 알림 없음
    assert (await namehist.history(db, 7))[0]["first_name"] == "나간사람새이름"
    assert await namehist.sweep(svc, sb) == 0                              # 12시간 안엔 다시 안 봄
    await db._write("UPDATE name_scan SET ts=?", (int(_t.time()) - namehist.SCAN_EVERY - 1,))
    sb2 = SweepBot(sb.names, fail_after=1)                                # 속도 제한 → 이번 회차 멈춤
    assert await namehist.sweep(svc, sb2) == 1 and sb2.attempts == 2       # 제한 걸리면 바로 멈춤 (더 안 물어봄)
    await db._write("UPDATE members SET last_seen=?", (int(_t.time()) - 100 * 86400,))
    await db._write("UPDATE name_scan SET ts=0")
    assert await namehist.sweep(svc, sb) == 0                              # 90일 넘게 안 보인 멤버는 제외


@test
async def admin_list_fetch_records_names():
    from sodam.permissions import Permissions
    from sodam import __main__ as main_mod
    db, svc, bot, ctx = await setup()
    svc.perms = Permissions(svc.cfg, db)
    svc.perms.on_admins = lambda b, cid, admins: namehist.record_admins(svc, b, cid, admins)
    bot.admins = [user(77, "숨은관리자", "hidden_admin")]
    assert await svc.perms.is_admin(bot, CHAT, 77)
    assert (await namehist.history(db, 77))[0]["username"] == "hidden_admin"
    assert main_mod.build_services(svc.cfg, db).perms.on_admins is not None   # 실제 조립에서도 연결됨


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
