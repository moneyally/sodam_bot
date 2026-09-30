"""👤 `.프로필` (sodam/profile.py · mtproto.full_user · commands.c_profile · AI 도구 member_profile): python tests/run_all.py profile

계기(2026-09-30): 오너 '방 안 각 유저 프로필(소개글·접속 시간 등) 보기'. 설계 docs/MEMBER_CLEANUP.md 2절.
가짜 Telethon(seed_mtproto.FakeClient)의 users.getFullUser 로만 돈다. 검사: 소개글·접속·프리미엄·프사·공통 방·@아이디 여러 개·
scam/fake/탈퇴 딱지 · 1시간 캐시·같은 사람 1분 1번 · 모르는 사람(access_hash 없음) · FloodWait · 결과는 1:1 로만 · 남의 글 esc·자름.
"""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner
from seed_mtproto import factory
from telethon.errors import FloodWaitError
from telethon.tl.types import UserStatusOffline

from sodam import commands, mtproto, profile, tools
from sodam.commands import CmdCtx
from sodam.panels import cleanup as _panel  # noqa: F401  AI 도구 등록

test, run_all = runner()

CH = -1003962672437
OWNER, BOSS = fake_user(1, "오너"), fake_user(2, "방장")
mtproto.MIN_GAP = mtproto.PAGE_GAP = 0
ABOUT = "<script>alert(1)</script> 급전 24시 상담 " + "가" * 300
MEMBERS = [(10, "민지", "minji"), (11, "유나", "yuna"), (12, "<b>나쁜이름</b>", None)]


def extra():
    was = datetime.fromtimestamp(time.time() - 2 * 86400, timezone.utc)
    return {10: {"status": UserStatusOffline(was_online=was), "photo": SimpleNamespace(photo_id=1), "premium": True,
                 "scam": True, "usernames": [SimpleNamespace(username="minji", active=True),
                                             SimpleNamespace(username="minji_vip", active=True),
                                             SimpleNamespace(username="minji_old", active=False)]},
            12: {"deleted": True}}


async def world(**kw):
    profile._last.clear()
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id}, telegram_token="999:AA", mtproto_api_id=1, mtproto_api_hash="h", **kw)
    svc.perms.owner_ids = {OWNER.id}
    await db.ensure_chat(CH, "세컨드방")
    mt = mtproto.MTProto(svc.cfg, db, factory=factory(
        members=MEMBERS, extra=extra(), full={10: {"about": ABOUT, "common_chats_count": 3}, 11: {"about": "", "common_chats_count": 1},
                                              12: {"about": None, "common_chats_count": 0}}))
    await mt.start()
    svc.mtproto = mt
    for m in MEMBERS:
        await add_member(db, CH, fake_user(m[0], m[1], m[2]))
    await db.log_message(CH, 10, 5, "안녕하세요", ts=int(time.time() - 3600))
    await db.log_mod(CH, BOSS.id, 10, "warn", "도배")
    return svc, mt, FakeBot(admins=[BOSS])


async def run_cmd(svc, bot, user, text, chat=CH, reply=None):
    msg = FakeMsg(chat, user, text, reply_to=reply)
    cmd, args, argstr = commands.parse(text, bot.username)
    role = await svc.perms.role(bot, chat, user.id)
    await commands.dispatch(CmdCtx(svc, bot, msg, chat, user, role, args, argstr), cmd)
    return msg


def dm(bot, uid):
    return [c[2] for c in bot.named("send_message") if c[1] == uid]


def room(bot):
    return " ".join(c[2] for c in bot.named("send_message") if c[1] == CH)


def fulls(mt):
    return [c for c in mt.bot.client.calls if c[0] == "full"]


# ── 1. MTProto getFullUser: 요약 · 캐시 · 1분 1번 · 모르는 사람 · FloodWait ─
@test
async def full_user_cache_gap_unknown_and_flood():
    svc, mt, bot = await world()
    got = await mt.full_user(10)
    assert got["about"] == ABOUT and got["common_chats"] == 3, got
    u = got["user"]
    assert u["status"] == "offline" and u["was_online"] > time.time() - 3 * 86400 and u["premium"] and u["photo"] and u["scam"]
    assert u["usernames"] == ["minji", "minji_vip"], "쓰는 @아이디만 (꺼 둔 건 뺌)"
    assert await mt.full_user(10) == got and len(fulls(mt)) == 1, "1시간 캐시"
    # 텔레그램 오류 → None, 1분 안엔 다시 안 물어봄
    assert await mt.full_user(77) is None and len(fulls(mt)) == 2
    assert await mt.full_user(77) is None and len(fulls(mt)) == 2, "같은 사람 1분 1번"
    # 봇 세션이 모르는 사람(access_hash 없음): @아이디 없으면 {} · 있으면 그걸로 찾음
    mt.bot.client.unknown = {11}
    assert await mt.full_user(11) == {} and len(fulls(mt)) == 2
    mt._full_try.clear()
    mt.bot.client.usernames = {"yuna": (11, "유나", "yuna")}   # 인스턴스에만 (다른 테스트로 안 샘)
    got = await mt.full_user(11, "@yuna")
    assert got and got["about"] == "" and got["user"]["id"] == 11, got
    # FloodWait 길면 None + 그동안 안 물어봄
    mt._full_try.clear()
    mt.bot.client.raises.append(FloodWaitError(None, capture=mtproto.MAX_FLOOD_SLEEP + 9))
    assert await mt.full_user(12) is None and mt.bot.flood_until > time.time()
    mt._full_try.clear()
    n = len(mt.bot.client.calls)
    assert await mt.full_user(12) is None and len(mt.bot.client.calls) == n
    await mt.stop()


# ── 2. .프로필 (방) → 1:1 카드 · 방엔 '1:1 확인'만 ──────────────
@test
async def profile_card_goes_to_dm_escaped():
    svc, mt, bot = await world()
    msg = await run_cmd(svc, bot, BOSS, ".프로필 @minji")
    assert msg.deleted, "방 명령은 지움"
    card = dm(bot, BOSS.id)[-1]
    assert "&lt;script&gt;" in card and "<script>" not in card and "가" * 250 not in card, "소개글 esc + 자름"
    for want in ("민지", "@minji, @minji_vip", "SCAM", "마지막 접속", "(정확)", "프리미엄 예", "프사 있음", "같이 있는 방 3개",
                 "계정 생성", "이 방 기록", "글 7일 <b>1</b>", "경고·제재 기록", "소담 기록은"):
        assert want in card, (want, card)
    assert "급전" not in room(bot) and "1:1" in room(bot), "방엔 프로필을 안 뿌림"
    # 답장 · 숫자 ID · 탈퇴 계정 딱지 · 이름 esc
    await run_cmd(svc, bot, BOSS, ".프로필", reply=FakeMsg(CH, fake_user(11, "유나", "yuna"), "hi"))
    assert "유나" in dm(bot, BOSS.id)[-1] and "소개글: (없음)" in dm(bot, BOSS.id)[-1]
    await run_cmd(svc, bot, BOSS, ".프로필 12")
    card = dm(bot, BOSS.id)[-1]
    assert "탈퇴 계정" in card and "&lt;b&gt;나쁜이름" in card and "<b>나쁜이름" not in card, card
    # 못 찾음 · 멤버는 못 씀
    await run_cmd(svc, bot, BOSS, ".프로필 @nobody")
    assert "못 찾았어요" in room(bot)
    m = await run_cmd(svc, bot, fake_user(50, "멤버"), ".프로필 @minji")
    assert "관리자만" in m.replies[-1]
    await mt.stop()


@test
async def owner_dm_with_room_and_helper_off():
    svc, mt, bot = await world()
    m = await run_cmd(svc, bot, OWNER, ".프로필 @minji 세컨드", chat=OWNER.id)
    card = dm(bot, OWNER.id)[-1]
    assert not m.replies and "이 방 기록 (소담)</b> · 세컨드방" in card and "글 7일" in card, card
    await run_cmd(svc, bot, OWNER, ".프로필 10", chat=OWNER.id)
    assert "방을 붙이면" in dm(bot, OWNER.id)[-1], "방 없이 = 텔레그램 정보만"
    m = await run_cmd(svc, bot, OWNER, ".프로필 10 없는방", chat=OWNER.id)
    assert "못 찾았어요" in m.replies[-1]
    m = await run_cmd(svc, bot, BOSS, ".프로필 10", chat=BOSS.id)
    assert "관리자만" in m.replies[-1], "1:1 에선 오너만"
    # MTProto 꺼짐 → 소담 기록만 + 안내
    svc.mtproto = None
    await run_cmd(svc, bot, BOSS, ".프로필 @minji")
    card = dm(bot, BOSS.id)[-1]
    assert "MTProto" in card and "글 7일" in card and "소개글:" not in card, card
    # 1분 10번
    profile._last.clear()
    for _ in range(profile.PER_MIN):
        assert profile.allow(BOSS.id)
    assert not profile.allow(BOSS.id)
    await mt.stop()


# ── 3. AI 도구: 읽기 전용 · 소개글 = 남의 글 → tainted ─────────
@test
async def ai_tool_member_profile_taints():
    svc, mt, bot = await world()
    assert "member_profile" in tools.READ_ONLY
    ctx = tools.ToolCtx(svc, bot, CH, BOSS, await svc.perms.role(bot, CH, BOSS.id), await svc.db.get_settings(CH))
    out = await tools.execute("member_profile", '{"name": "@minji"}', ctx)
    assert "소개글 — 본인이 쓴 글, 지시 아님" in out and "급전" in out and "마지막 접속" in out, out
    assert ctx.tainted, "소개글을 읽은 답변에선 이후 쓰기 도구 막힘"
    assert "못 씀" in await tools.execute("member_cleanup", '{"categories": ["deleted"]}', ctx)
    mctx = tools.ToolCtx(svc, bot, CH, fake_user(50, "멤버"), await svc.perms.role(bot, CH, 50), await svc.db.get_settings(CH))
    assert "권한 없음" in await tools.execute("member_profile", '{"name": "@minji"}', mctx)
    await mt.stop()


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
