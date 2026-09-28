"""이름 기록 × MTProto 도우미 (namehist.mt_sweep · resolve_remote): python tests/run_all.py namehist_mtproto

가짜 Telethon 클라이언트(seed_mtproto.FakeClient)로만 돈다. 실제 텔레그램 연결 없음.
"""
import asyncio
import dataclasses
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner
from seed_mtproto import FakeClient, factory

from sodam import commands, mtproto, namehist
from sodam.commands import CmdCtx
from sodam.menu import PanelCtx
from sodam.panels import namehist as nh_panel
from sodam.permissions import Role

test, run_all = runner()
mtproto.MIN_GAP = mtproto.PAGE_GAP = 0
CH, OTHER = -1001234567001, -1001234567002   # 슈퍼그룹 (-100 + 채널 ID)


async def world(members=(), helper=True, chats=(CH,)):
    db = await make_db()
    svc = await make_svc(db, admins={1}, telegram_token="999:x", mtproto_api_id=1, mtproto_api_hash="h")
    for cid in chats:
        await db.ensure_chat(cid, "방")
    if helper:
        svc.mtproto = mtproto.MTProto(svc.cfg, db, factory=factory(members=members))
        await svc.mtproto.start()
    return db, svc, FakeBot()


def notices(bot, chat=CH):
    return [c[2] for c in bot.named("send_message") if c[1] == chat]


def pages(svc):
    return [c for c in svc.mtproto.bot.client.calls if c[0] == "page"]


async def past(db, table, cid, secs):
    await db._write(f"UPDATE {table} SET ts=ts-? WHERE chat_id=?", (secs, cid))


# ── 1. 전체 멤버 순찰: 말 안 한 멤버 기록 · 처음 본 사람은 알림 없음 · 바뀐 사람만 ─
@test
async def sweep_records_silent_members_without_first_sight_notice():
    db, svc, bot = await world([(10, "새이름", "kim", False, "성"), (11, "조용이", "quiet"), (12, "그대로", "same"),
                                (13, "봇", "somebot", True)])
    await namehist.record(db, fake_user(10, "옛이름", "kim"))   # 예전에 본 이름
    await namehist.record(db, SimpleNamespace(id=12, first_name="그대로", last_name=None, username="same", is_bot=False))
    await db._write("INSERT INTO members(chat_id, user_id, last_seen) VALUES(?,?,?)", (CH, 12, 111))
    n = await namehist.mt_sweep(svc, bot)
    assert n == 4, n
    out = notices(bot)
    assert len(out) == 1 and "옛이름" in out[0] and "새이름 성" in out[0], out   # 11(처음)·12(그대로)는 알림 없음
    assert [r["first_name"] for r in await namehist.history(db, 11)] == ["조용이"]
    assert not await namehist.history(db, 13), "봇은 기록 안 함"
    rows = {r["user_id"]: r for r in await db._all("SELECT user_id, last_seen, joined_at FROM members WHERE chat_id=?", (CH,))}
    assert set(rows) == {10, 11, 12} and rows[11]["last_seen"] is None and rows[12]["last_seen"] == 111, "last_seen 안 올림"
    u = await db._one("SELECT first_name, last_name, username FROM users WHERE user_id=11")
    assert tuple(u) == ("조용이", None, "quiet")
    # 같은 이름 그대로면 두 번째 순찰에도 알림·새 기록 없음 (Bot API 로 본 이름과 같은 형식)
    await past(db, "name_mt_scan", CH, namehist.MT_EVERY + 1)
    await namehist.mt_sweep(svc, bot)
    assert len(notices(bot)) == 1 and len(await namehist.history(db, 10)) == 2


# ── 2. 방마다 12시간 1번 · 시간당 요청 상한 · 관리자 아닌 방 건너뜀 ─
@test
async def sweep_throttle_per_group_and_hourly_cap():
    db, svc, bot = await world([(10, "a", "a")], chats=(CH, OTHER))
    mt = svc.mtproto
    assert await namehist.mt_sweep(svc, bot) == 1 and len(pages(svc)) == 2      # 한 번에 방 하나 (쪽 1 + 빈 쪽)
    assert await namehist.mt_sweep(svc, bot) == 1                                # 다음 차례에 다른 방
    n = len(pages(svc))
    assert await namehist.mt_sweep(svc, bot) == 0 and len(pages(svc)) == n, "12시간 안엔 다시 안 함"
    await past(db, "name_mt_scan", CH, namehist.MT_EVERY - 60)
    assert await namehist.mt_sweep(svc, bot) == 0
    await past(db, "name_mt_scan", CH, 120)
    # 시간당 상한: 이번 시간 몫을 다 쓰면 때가 된 방도 미룸 (차지도 안 함)
    now = time.time()
    mt.call_log.extend([now] * (namehist.MT_CALLS_PER_HOUR - mt.calls_last_hour() - 1))   # 1 남음 < 예상 2
    assert await namehist.mt_sweep(svc, bot) == 0 and len(pages(svc)) == n
    mt.call_log.clear()
    mt.call_log.extend([now - 3601] * 1000)   # 1시간 지난 요청은 안 셈
    assert await namehist.mt_sweep(svc, bot) == 1 and len(pages(svc)) == n + 2
    # 큰 방은 지난 스냅샷 크기로 비용 예상 (1만 명 = 쪽 50)
    await db._write("UPDATE mtproto_snap SET count=10000 WHERE chat_id=?", (OTHER,))
    await past(db, "name_mt_scan", OTHER, namehist.MT_EVERY + 1)
    mt.call_log.extend([time.time()] * (namehist.MT_CALLS_PER_HOUR - 40 - mt.calls_last_hour()))
    assert await namehist.mt_sweep(svc, bot) == 0
    # 관리자 아닌 방: 건너뛰고 12시간 뒤에 (참가자 요청 없음)
    db, svc, bot = await world([(10, "a", "a")], chats=(CH, OTHER))
    bot.can_moderate = False
    assert await namehist.mt_sweep(svc, bot) == 0 and not pages(svc)
    assert (await db._one("SELECT COUNT(*) AS n FROM name_mt_scan"))["n"] == 2
    # 도우미 꺼짐 / FloodWait 쉬는 중이면 아무것도 안 함
    db, svc, bot = await world(helper=False)
    assert await namehist.mt_sweep(svc, bot) == 0
    db, svc, bot = await world([(10, "a", "a")])
    svc.mtproto.bot.flood_until = time.time() + 60
    assert await namehist.mt_sweep(svc, bot) == 0 and not pages(svc)
    assert not await db._all("SELECT 1 FROM name_mt_scan"), "쉬는 동안엔 방을 차지하지 않음 (12시간 밀리지 않게)"


# ── 3. getChatMember 순찰은 대체용: MTProto 가 최근 전체를 본 방은 건너뜀 ─
@test
async def getchatmember_sweep_is_fallback():
    for helper in (True, False):
        db, svc, bot = await world([(10, "a", "a")], helper=helper, chats=(CH, OTHER))
        for cid in (CH, OTHER):
            await db.upsert_user(fake_user(10, "a", "a"))
            await db.touch_member(cid, 10)
        calls = []

        async def gcm(chat_id, user_id):
            calls.append(chat_id)
            return SimpleNamespace(status="member", user=fake_user(user_id, "a", "a"))
        bot.get_chat_member = gcm
        await namehist.sweep(svc, bot)   # 앞에서 mt_sweep 이 CH 한 방을 전체 순찰
        assert sorted(calls) == ([CH] if helper else [OTHER, CH]), (helper, calls)   # mt_sweep 은 OTHER(작은 ID)부터
    # 1만 명 넘어 일부만 본 방(partial)은 getChatMember 도 계속
    db, svc, bot = await world([(10, "a", "a")], chats=(CH, OTHER))
    await svc.mtproto.snapshot(CH, [{"id": 10, "first_name": "a", "last_name": "", "username": "a", "is_bot": False}], limit=1)
    await db.upsert_user(fake_user(10, "a", "a"))
    await db.touch_member(CH, 10)
    await db._write("INSERT INTO name_mt_scan(chat_id, ts) VALUES(?,?),(?,?)", (CH, int(time.time()), OTHER, int(time.time())))
    calls = []

    async def gcm2(chat_id, user_id):
        calls.append(chat_id)
        return SimpleNamespace(status="member", user=fake_user(user_id, "a", "a"))
    bot.get_chat_member = gcm2
    await namehist.sweep(svc, bot)
    assert calls == [CH], calls


# ── 4. 나간 사람 표시 · 알림 폭주 방지 ─────────────────────────
@test
async def left_members_and_notice_cap():
    people = [(100 + i, f"새{i}", f"u{i}") for i in range(8)]
    db, svc, bot = await world(people)
    for uid, _, un in people:
        await namehist.record(db, fake_user(uid, "옛", un))
    await namehist.mt_sweep(svc, bot)
    out = notices(bot)
    assert len(out) == namehist.MT_NOTICES + 1 and "그 밖에 3명" in out[-1], out
    svc.mtproto.bot.client.members = people[1:]
    await past(db, "name_mt_scan", CH, namehist.MT_EVERY + 1)
    await namehist.mt_sweep(svc, bot)
    left = [r["user_id"] for r in await db._all("SELECT user_id FROM member_left WHERE chat_id=?", (CH,))]
    assert left == [100], left
    svc.mtproto.bot.client.members = people   # 다시 들어오면 표시 지움
    await past(db, "name_mt_scan", CH, namehist.MT_EVERY + 1)
    await namehist.mt_sweep(svc, bot)
    assert not await db._all("SELECT 1 FROM member_left WHERE chat_id=?", (CH,))
    # 이름 알림을 끈 방은 요약도 안 보냄
    db, svc, bot = await world(people)
    await db.set_setting(CH, "name_change_notice", False)
    for uid, _, un in people:
        await namehist.record(db, fake_user(uid, "옛", un))
    await namehist.mt_sweep(svc, bot)
    assert not notices(bot)


# ── 5. 기록에 없는 @아이디 → resolveUsername (알림 없음 · '지금 이름만') ─
async def cmd(svc, bot, chat, u, text):
    c, args, argstr = commands.parse(text, "sodambot")
    m = FakeMsg(chat, u, text)
    await commands.dispatch(CmdCtx(svc, bot, m, chat, u, Role.MEMBER, args, argstr), c)
    return m.replies[0]


@test
async def resolve_username_fallback():
    db, svc, bot = await world()
    c = svc.mtproto.bot.client
    c.usernames = {"newguy": (77, "처음", "NewGuy", False, "사람"), "somechan": "channel", "abot": (78, "봇", "abot", True)}
    me = fake_user(5, "나", "me")
    r = await cmd(svc, bot, CH, me, ".기록 @newguy")
    assert "처음 사람" in r and namehist.REMOTE_NOTE in r and "77" in r, r
    assert not bot.named("send_message"), "어느 방에도 알림 없음"
    assert len(await namehist.history(db, 77)) == 1
    assert (await db._one("SELECT username FROM users WHERE user_id=77"))["username"] == "NewGuy"
    # 이제 DB 에 있으니 텔레그램에 다시 안 물어봄 · 안내 문구도 평소대로
    n = len([x for x in c.calls if x[0] == "resolve"])
    r = await cmd(svc, bot, CH, me, ".기록 @newguy")
    assert namehist.REMOTE_NOTE not in r and len([x for x in c.calls if x[0] == "resolve"]) == n
    # 없는 아이디·채널·봇은 못 찾음, 없는 아이디는 하루 기억
    for name in ("ghost", "somechan", "abot"):
        assert "못 찾았" in await cmd(svc, bot, CH, fake_user(6 + len(name), "x"), f".기록 @{name}")
    assert svc.mtproto.bot.last_error == "", "없는 아이디·채널은 오류가 아님"
    k = len([x for x in c.calls if x[0] == "resolve"])
    assert "못 찾았" in await cmd(svc, bot, CH, fake_user(40, "y"), ".기록 @ghost")
    assert len([x for x in c.calls if x[0] == "resolve"]) == k, "없는 아이디 캐시"
    # '@' 없는 낱말은 안 물어봄 (이름으로 남의 계정 찾지 않게)
    assert "못 찾았" in await cmd(svc, bot, CH, me, ".기록 someone") and len([x for x in c.calls if x[0] == "resolve"]) == k
    # 사람당 1분 3번
    c.usernames.update({f"p{i}x": (200 + i, f"p{i}", f"p{i}x") for i in range(5)})
    got = [await namehist.resolve_remote(svc, f"@p{i}x", 55) for i in range(5)]
    assert [g[0] is not None for g in got] == [True, True, True, False, False], got
    # 전체 시간당 상한
    svc.mtproto.resolve_log.extend([time.time()] * mtproto.RESOLVE_PER_HOUR)
    assert await svc.mtproto.resolve_username("p4x") is None
    # 1:1 🕵️ 입력 화면도 같은 길
    db, svc, bot = await world()
    svc.mtproto.bot.client.usernames = {"dmguy": (88, "디엠", "dmguy")}
    ok, text = await nh_panel._lookup(PanelCtx(svc, bot, 5, 0, []), FakeMsg(5, me, "@dmguy"))
    assert ok and "디엠" in text and namehist.REMOTE_NOTE in text
    # 도우미 꺼짐이면 예전처럼 '못 찾음'
    db, svc, bot = await world(helper=False)
    assert "못 찾았" in await cmd(svc, bot, CH, me, ".기록 @newguy")
    assert await namehist.resolve_remote(svc, "@newguy", 5) == (None, False)


# ── 6. 이미 기록된 사람이 아이디만 새로 바꾼 경우: 전체 기록 + 안내 없음 ─
@test
async def resolve_known_person_new_username():
    db, svc, bot = await world()
    await namehist.record(db, fake_user(90, "민지", "oldname"))
    svc.mtproto.bot.client.usernames = {"brandnew": (90, "민지", "brandnew")}
    r = await cmd(svc, bot, CH, fake_user(5, "나"), ".기록 @brandnew")
    assert "oldname" in r and "brandnew" in r and namehist.REMOTE_NOTE not in r and "아이디 변경 1회" in r, r
    assert not bot.named("send_message")


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
