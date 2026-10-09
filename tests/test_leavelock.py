"""🚪 나간 사람 재입장 막기 (sodam/leavelock.py, 2026-10-09 뉴월드 '한 번 나가면 못 들어오게').
실제 handlers.on_left / on_chat_member 로 (가짜 텔레그램)."""
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner
from telegram.error import BadRequest
from test_farewell import CHAT, ctx_of, member_left, service_left

from sodam import free, leavelock
from sodam.panels import leavelock as P

test, run_all = runner()
ADMIN = 1


class BanBot(FakeBot):
    def __init__(self, fail=False, **kw):
        super().__init__(**kw)
        self.fail = fail
        self.bans = []

    async def ban_chat_member(self, chat_id, user_id, **kw):
        if self.fail:
            raise BadRequest("Not enough rights to restrict/unrestrict chat member")
        self.bans.append((chat_id, user_id, kw.get("until_date")))
        await super().ban_chat_member(chat_id, user_id, **kw)


async def setup(on=True, fail=False, **settings):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, "방")
    if on:
        await db.set_setting(CHAT, "leave_lock", "on")
    for k, v in settings.items():
        await db.set_setting(CHAT, k, v)
    bot = BanBot(fail=fail)
    return svc, bot, ctx_of(svc, bot)


@test
async def off_by_default_and_nobody_is_banned():
    svc, bot, ctx = await setup(on=False)
    s = await svc.db.get_settings(CHAT)
    assert s["leave_lock"] == "off" and s["leave_lock_hours"] == 168
    await service_left(ctx, fake_user(50, "떠난이"))
    assert not bot.bans


@test
async def voluntary_leave_is_banned_for_the_set_period_once():
    svc, bot, ctx = await setup()
    u = fake_user(50, "떠난이", "bye50")
    t0 = int(time.time())
    await service_left(ctx, u)
    await member_left(ctx, u)                              # 같은 나감이 두 길(서비스 메시지·멤버 상태)로 와도 한 번
    assert len(bot.bans) == 1, bot.bans
    chat, uid, until = bot.bans[0]
    assert (chat, uid) == (CHAT, 50) and abs(until - (t0 + 168 * 3600)) < 30, until
    row = await svc.db._one("SELECT * FROM mod_log WHERE action='leave_lock'")
    assert row and row["target_id"] == 50 and row["detail"] == "7일"


@test
async def forever_and_custom_period():
    svc, bot, ctx = await setup(leave_lock_hours=0)
    await service_left(ctx, fake_user(50, "떠난이"))
    assert bot.bans[0][2] is None, "0 = 영구 (기한 없음)"
    svc, bot, ctx = await setup(leave_lock_hours=24)
    await member_left(ctx, fake_user(51, "또떠남"))
    assert abs(bot.bans[0][2] - (time.time() + 86400)) < 30


@test
async def kicked_by_admin_free_member_and_owner_are_left_alone():
    svc, bot, ctx = await setup()
    await service_left(ctx, fake_user(50, "내보내짐"), by=fake_user(ADMIN, "관리"))     # 관리자가 내보냄
    await member_left(ctx, fake_user(51, "밴당함"), status="kicked")                    # 밴으로 나감
    await free.add(svc.db, CHAT, 52, ADMIN)
    await service_left(ctx, fake_user(52, "자유멤버"))
    owner = next(iter(await svc.perms.owners()), None)
    if owner:
        await service_left(ctx, fake_user(owner, "오너"))
    assert not bot.bans, bot.bans


@test
async def missing_rights_are_shown_on_the_screen():
    svc, bot, ctx = await setup(fail=True)
    await service_left(ctx, fake_user(50, "떠난이"))
    fail = await svc.db.get_state(CHAT, leavelock.FAIL_KEY)
    assert fail and "rights" in fail["why"]
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN)
    screen = await P.s_llk(c)
    assert "사용자 차단" in screen.text and "켜짐" in screen.text and "7일" in screen.text


@test
def period_words():
    assert leavelock.parse_period("영구") == 0 and leavelock.parse_period("3일") == 72
    assert leavelock.parse_period("12시간") == 12 and leavelock.parse_period(" 48 ") == 48
    for bad in ("366일", "내일", ""):
        try:
            leavelock.parse_period(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert leavelock.period_label(0) == "영구" and leavelock.period_label(72) == "3일" and leavelock.period_label(5) == "5시간"


@test
async def period_can_be_typed_in_the_menu():
    svc, bot, ctx = await setup()
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN, user=fake_user(ADMIN, "관리"))
    ok, text = await P.in_hours(c, FakeMsg(ADMIN, fake_user(ADMIN, "관리"), "3일"))
    assert ok and "3일" in text and (await svc.db.get_settings(CHAT))["leave_lock_hours"] == 72
    ok, text = await P.in_hours(c, FakeMsg(ADMIN, fake_user(ADMIN, "관리"), "내일"))
    assert not ok


if __name__ == "__main__":
    run_all()
