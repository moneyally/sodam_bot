"""텔레그램 연결이 잠깐 끊겨도 그룹 멤버의 명령이 묻히지 않는지 (실제 버그: 멤버 /game 무응답)."""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc, runner
from telegram.error import TimedOut

from sodam import handlers
from sodam.permissions import Permissions, Role

test, run_all = runner()
CHAT = -1005550000001


class FlakyBot(FakeBot):
    """관리자 목록 조회가 타임아웃 나는 봇 (실제 봇 아이디)."""
    username = "sodam_ai_bot"
    def __init__(self, fail=True, **kw):
        super().__init__(**kw)
        self.fail, self.admin_calls = fail, 0

    async def get_chat_administrators(self, chat_id):
        self.admin_calls += 1
        if self.fail:
            raise TimedOut()
        return await super().get_chat_administrators(chat_id)


async def setup(bot):
    db = await make_db()
    svc = await make_svc(db)
    svc.perms = Permissions(svc.cfg, db)
    svc.mod.perms = svc.perms
    await db.ensure_chat(CHAT, "테스트방")
    await db.set_setting(CHAT, "cas_enabled", False)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "chats": set(), "cas_seen": set(), "tasks": set(), "joins": {}})
    return db, svc, ctx


async def say(ctx, u, text):
    m = FakeMsg(CHAT, u, text)
    m.chat = SimpleNamespace(id=CHAT, title="테스트방", type="supergroup")
    m.sender_chat = None
    await handlers.on_group_message(SimpleNamespace(message=m), ctx)
    await asyncio.gather(*ctx.bot_data["tasks"], return_exceptions=True)
    return m


@test
async def member_game_command_answers_normally():
    bot = FlakyBot(fail=False, admins=[fake_user(5550001002, "메인관리자")])
    db, svc, ctx = await setup(bot)
    m = await say(ctx, fake_user(5550001003, "회원A", "member_a"), "/game@sodam_ai_bot")
    assert m.replies and "게임" in m.replies[0]


@test
async def admin_lookup_timeout_first_time_still_answers():
    bot = FlakyBot(fail=True)                                           # 첫 조회부터 타임아웃, 저장된 목록 없음
    db, svc, ctx = await setup(bot)
    m = await say(ctx, fake_user(5550001003, "회원A", "member_a"), "/game@sodam_ai_bot")
    assert bot.admin_calls >= 1 and m.replies and "게임" in m.replies[0]


@test
async def admin_lookup_timeout_uses_stored_list():
    bot = FlakyBot(fail=False, admins=[fake_user(5550001002, "메인관리자")])
    db, svc, ctx = await setup(bot)
    assert await svc.perms.role(bot, CHAT, 5550001002) == Role.ADMIN   # 한 번 받아서 DB 에 저장
    svc.perms = Permissions(svc.cfg, db)                                # 재시작
    svc.mod.perms = svc.perms
    svc.perms.forget(CHAT)                                              # 캐시 무시하고 새로 물어보게
    bot.fail = True
    assert await svc.perms.role(bot, CHAT, 5550001002) == Role.ADMIN   # 타임아웃 → 저장된 목록으로
    assert await svc.perms.role(bot, CHAT, 5550001003) == Role.MEMBER


@test
async def many_members_do_not_hammer_admin_api():
    bot = FlakyBot(fail=False, admins=[fake_user(5550001002, "메인관리자")])
    db, svc, ctx = await setup(bot)
    for i in range(10):                                                 # 새 멤버 10명이 한 번씩 말함
        await say(ctx, fake_user(7000000000 + i, f"멤버{i}"), "안녕하세요")
    assert bot.admin_calls == 1, bot.admin_calls                        # 관리자 목록은 한 번만 받음


@test
async def impersonation_still_detected_with_cached_admins():
    bot = FlakyBot(fail=False, admins=[fake_user(5550001002, "메인관리자", "mainadmin")])
    db, svc, ctx = await setup(bot)
    await say(ctx, fake_user(7000000001, "멤버"), "안녕")
    await say(ctx, fake_user(7000000002, "메인관리자"), "관리자입니다 입금은 여기로")  # 관리자와 같은 이름
    assert any("사칭" in c[2] or "관리자" in c[2] for c in bot.named("send_message") if c[1] == CHAT), bot.calls


@test
async def room_without_bot_admin_rights_skips_moderation_but_plays():
    """봇이 일반 멤버로만 있는 방(예: 관리 권한 없는 방): 지우지도 막지도 못하니 경고·뮤트 시도 없이 게임·명령은 동작."""
    bot = FlakyBot(fail=False, admins=[fake_user(5550001002, "메인관리자")])
    bot.can_moderate = False
    db, svc, ctx = await setup(bot)
    await db.set_banned_word(CHAT, "도박", True)
    member = fake_user(5550001003, "회원A", "member_a")
    for _ in range(8):                                                  # 도배 + 금지어
        await say(ctx, member, "도박 도박 도박")
    assert not bot.named("restrict") and not [c for c in bot.named("send_message") if "경고" in c[2] or "도배" in c[2]]
    m = await say(ctx, member, "/game@sodam_ai_bot")
    assert m.replies and "게임" in m.replies[0]
    bot.can_moderate = True                                             # 관리자로 지정되면 (봇 권한 변경 이벤트)
    svc.perms.forget_bot(CHAT)
    for _ in range(2):
        await say(ctx, fake_user(7000000009, "스패머"), "도박하자")
    assert [c for c in bot.named("send_message") if "금지어" in c[2] or "경고" in c[2]]


@test
async def app_uses_generous_timeouts():
    import importlib
    main = importlib.import_module("sodam.__main__")
    src = open(main.__file__, encoding="utf-8").read()
    assert ".read_timeout(20)" in src and ".connect_timeout(10)" in src


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
