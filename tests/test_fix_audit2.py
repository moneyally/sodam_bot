"""감사 2차 회귀 테스트 (스팸 방패·운영 인박스·확인 카드·hot path): python tests/run_all.py fix_audit2

각 테스트는 고친 줄을 되돌리면 FAIL 한다 (뮤테이션 검증).
"""
import asyncio
import time
from types import SimpleNamespace

from telegram.error import BadRequest

from fake_llm import Room, reply, tool_call
from fakes import FakeBot, FakeQuery, make_db, make_svc, runner
from test_sanction_multi import A, B, BOSS, ask, room

from sodam import db as dbmod, fedban, free, handlers, menu, opsdesk, rules, spamshield
from sodam.permissions import Permissions

test, run_all = runner()


async def _press(r, user, data):
    q = FakeQuery(Room.CHAT, user, data)   # 방에 뜬 확인 카드는 방에서 누름
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


@test
async def room_card_double_tap_after_restart_saves_once():
    """재시작 뒤(메모리 토큰 없음) 확인 카드 [✅]를 두 번 빨리 누르면(텔레그램이 콜백 2개를 동시에 줌) 두 번 저장됐음.
    DB 토큰은 SELECT 로만 확인해서 둘 다 통과 → 이제 DELETE 한 문장이 차지."""
    r = await room()
    await ask(r, BOSS, [tool_call("schedule_task", {"when": "매일 23:00", "action": "remind", "text": "마감"})])
    [card] = [c for c in r.bot.named("send_message") if c[1] == Room.CHAT and "예약할까요" in c[2]]
    ok = card[3]["reply_markup"].inline_keyboard[0][0].callback_data
    r.svc.menu_tokens.clear()                                    # 재시작 = 메모리 토큰 사라짐
    await asyncio.gather(_press(r, BOSS, ok), _press(r, BOSS, ok))
    assert len(await r.db.schedules(Room.CHAT)) == 1, [dict(x) for x in await r.db.schedules(Room.CHAT)]


@test
async def burst_answers_latest_message_even_if_processed_first():
    """연달아 보낸 두 말: 동시 처리라 늦게 보낸 말(ID 큼)이 먼저 처리될 수 있음 → 예전엔 먼저 보낸 말에 답장하고
    요청 순서도 뒤집혔음. 이제 메시지 ID 순."""
    r = await room()
    r.llm.script = [reply("둘 다 봤어요")]
    await r.say(A, "안녕하세요")                                  # 두 메시지가 같은 경로(기록 생략)로 처리되게
    m1, m2 = r.msg(A, "소담아 오늘 뭐 먹지"), r.msg(A, "소담아 매운 걸로")
    handlers.BURST_SECONDS = 0.05
    try:
        await asyncio.gather(*(handlers.on_group_message(SimpleNamespace(message=m), r.ctx) for m in (m2, m1)))
        await r.settle()
    finally:
        handlers.BURST_SECONDS = 0
    [call] = r.llm.of("chat")
    request = call["messages"][-1]["content"].split("<request", 1)[1]
    assert request.index("뭐 먹지") < request.index("매운 걸로"), request[:300]
    assert not m1.replies and m2.replies == ["둘 다 봤어요"], (m1.replies, m2.replies)



# ── hot path 캐시 (메시지마다 읽던 목록을 캐시) — 바뀌면 바로 보여야 함 ──────────
@test
async def hot_path_caches_see_changes_immediately():
    r = await room()
    db, cid, now = r.db, Room.CHAT, dbmod.now()
    await db._write("UPDATE members SET joined_at=? WHERE chat_id=? AND user_id=?", (now - 30 * 86400, cid, A.id))
    s = await db.get_settings(cid)
    assert await spamshield.newcomer_since(r.svc, cid, A.id, s, now) is None          # 오래된 멤버 (기억됨)
    await db.touch_member(cid, A.id, joined=True)                                       # 나갔다 다시 들어옴
    assert await spamshield.newcomer_since(r.svc, cid, A.id, s, dbmod.now()), "다시 들어오면 바로 신규"
    assert not await free.is_free(db, cid, A.id)
    await free.add(db, cid, A.id, BOSS.id)
    assert await free.is_free(db, cid, A.id)
    await free.remove(db, cid, A.id)
    assert not await free.is_free(db, cid, A.id)
    assert A.id not in await db.bot_admin_ids(cid)
    await db.set_bot_admin(cid, A.id, True)
    assert A.id in await db.bot_admin_ids(cid)
    assert "사기꾼" not in await db.banned_words(cid)
    await db.set_banned_word(cid, "사기꾼", True)
    assert "사기꾼" in await db.banned_words(cid)
    assert await fedban.lookup(db, 99001) is None
    await fedban.add(r.svc, r.bot, cid, 99001, "스패머", "도박 광고", BOSS.id)
    assert (await fedban.lookup(db, 99001))["rooms"] == 1


@test
async def alert_rule_change_applies_at_once():
    """켜진 규칙 목록은 메시지마다 읽어서 캐시 → 🔔 화면에서 받는 방법(1:1 → 방에서 부르기)을 바꾸면 바로 반영.
    (끄기·삭제는 울릴 때 _claim 이 DB 에서 enabled 를 다시 봐서 캐시와 상관없이 안 울림)"""
    r = await room()
    rid = await rules.add(r.svc, Room.CHAT, BOSS.id, {"action": "dm", "trig": "keyword", "arg": "입금"})
    await r.say(A, "안녕하세요")                                                         # 규칙 목록 캐시됨
    q = FakeQuery(BOSS.id, BOSS, f"m:rlx:{Room.CHAT}:{rid}:call")
    await menu.on_callback(r.svc, r.bot, q, q.data.split(":")[1:])
    await r.say(B, "입금 언제 돼요?")
    sent = r.bot.named("send_message")
    assert not [c for c in sent if c[1] == BOSS.id] and [c for c in sent if c[1] == Room.CHAT], sent


@test
async def hot_path_db_round_trips_per_message():
    """그룹 메시지 hot path (tools/hotpath_bench.py, 가짜 텔레그램·AI 없음): 감사 전 DB 왕복 13.6/메시지 → 2.6.
    캐시가 빠지거나 훅이 메시지마다 조회를 늘리면 여기서 잡힌다 (대화 기록 쓰기 1번은 꼭 필요)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import hotpath_bench
    res = await hotpath_bench.run(600, 2)
    assert res["trips_per_msg"] < 5, res



@test
async def scam_alert_double_tap_acts_once():
    """🕵️ 사기 의심 알림 [🚫 밴]을 두 번 빨리 누르거나 두 관리자가 [🚫 밴]·[✅ 괜찮음]을 같이 누르면 둘 다 실행됐음
    (처리 표시를 텔레그램 호출 뒤에 따로 적어서). 이제 한 문장으로 먼저 차지 → 하나만."""
    r = await room()
    aid = await r.db._write("INSERT INTO scam_alerts(chat_id, user_id, msg_id, name, reason, ts) VALUES(?,?,?,?,?,?)",
                            (Room.CHAT, A.id, 5, "수상한 계정", "지갑주소", dbmod.now()))

    async def press(act):
        q = FakeQuery(BOSS.id, BOSS, f"m:sgx:{Room.CHAT}:{aid}:{act}")
        await menu.on_callback(r.svc, r.bot, q, q.data.split(":")[1:])
        return q
    await asyncio.gather(press("b"), press("b"), press("t"))
    done = (await r.db._one("SELECT done FROM scam_alerts WHERE id=?", (aid,)))["done"]
    bans = len(r.bot.named("ban"))
    trusted = bool(await r.db._one("SELECT 1 FROM scam_trust WHERE chat_id=? AND user_id=?", (Room.CHAT, A.id)))
    assert (bans, trusted, done) in ((1, False, "b"), (0, True, "t")), (bans, trusted, done)
    # 텔레그램이 실패하면 차지를 되돌려 다시 누를 수 있음
    aid2 = await r.db._write("INSERT INTO scam_alerts(chat_id, user_id, msg_id, name, reason, ts) VALUES(?,?,?,?,?,?)",
                             (Room.CHAT, B.id, 6, "또 다른 계정", "초대 링크", dbmod.now()))
    orig = r.bot.restrict_chat_member

    async def fail(*a, **kw):
        raise BadRequest("Not enough rights")
    r.bot.restrict_chat_member = fail
    q = FakeQuery(BOSS.id, BOSS, f"m:sgx:{Room.CHAT}:{aid2}:m")
    await menu.on_callback(r.svc, r.bot, q, q.data.split(":")[1:])
    assert "실패" in q.answers[-1][0] and (await r.db._one("SELECT done FROM scam_alerts WHERE id=?", (aid2,)))["done"] is None
    r.bot.restrict_chat_member = orig
    await menu.on_callback(r.svc, r.bot, q, q.data.split(":")[1:])
    assert (await r.db._one("SELECT done FROM scam_alerts WHERE id=?", (aid2,)))["done"] == "m"



@test
async def command_center_checks_rooms_concurrently():
    """🧭 운영센터: 방마다 봇 권한을 텔레그램에 묻는데(캐시 10분) 방을 하나씩 차례로 물어서, 방이 많으면 버튼 응답이
    텔레그램 콜백 제한(약 15초)을 넘겨 화면이 안 떴음 (방 100개 × 0.1초). 이제 방들을 동시에 (최대 OPS_PARALLEL)."""
    db = await make_db()
    svc = await make_svc(db)
    svc.perms = Permissions(svc.cfg, db)
    bot = FakeBot()
    orig = bot.get_chat_member

    async def slow(chat_id, user_id):
        await asyncio.sleep(0.05)
        return await orig(chat_id, user_id)
    bot.get_chat_member = slow
    for i in range(40):
        await db.ensure_chat(-1002000000000 - i, f"방 {i}")
    t0 = time.monotonic()
    statuses, tot = await opsdesk.command_center(svc, bot, 1)
    took = time.monotonic() - t0
    assert tot["rooms"] == 40 and len(statuses) == 40
    assert [s.title for s in statuses] == sorted(s.title for s in statuses), "방 이름순 그대로"
    assert took < 1.6, f"방 40개에 {took:.2f}초 (하나씩이면 2초+ — 서버 배포 테스트는 Nice 10 이라 여유)"



@test
async def home_menu_does_not_ask_telegram_about_kicked_rooms_every_time():
    """메인 메뉴(/start·⬅️ 처음으로)가 📥 버튼을 보일지 정하려고 '내 그룹'을 찾는데, 봇이 강퇴된 방은 관리자 목록 조회가
    Forbidden 으로 실패해 기록이 안 남아 누를 때마다(누구든) 그 방들을 텔레그램에 다시 물었음 (방 10개면 10번).
    이제 강퇴·없는 방은 '관리자 없음'으로 기억 (봇이 다시 들어오면 on_my_chat_member 가 forget)."""
    from telegram.error import Forbidden
    db = await make_db()
    svc = await make_svc(db)
    svc.perms = Permissions(svc.cfg, db)
    bot = FakeBot()
    calls = []

    async def admins(chat_id):
        calls.append(chat_id)
        raise Forbidden("Forbidden: bot was kicked from the supergroup chat")
    bot.get_chat_administrators = admins
    for i in range(10):
        await db.ensure_chat(-1003000000000 - i, f"옛 방 {i}")
    await menu.main_menu(svc, bot, 20)
    first = len(calls)
    await menu.main_menu(svc, bot, 20)
    await menu.main_menu(svc, bot, 21)
    assert first == 10 and len(calls) == first, (first, len(calls))
    assert not await svc.perms.is_admin(bot, -1003000000000, 20)

    async def back(chat_id):                                    # 봇이 다시 초대됨 → 바로 새 목록
        return [SimpleNamespace(user=SimpleNamespace(id=20), status="administrator")]
    bot.get_chat_administrators = back
    assert await svc.perms.is_admin(bot, -1003000000000, 20)



@test
async def edited_join_service_message_is_ignored():
    """입장 메시지가 수정돼 edited_message 로 다시 오면 update.message 가 None → on_join 이 AttributeError (운영 2026-10-01 14:44)."""
    upd = SimpleNamespace(message=None, edited_message=SimpleNamespace(new_chat_members=[]))
    await handlers.on_join(upd, None)

if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(run_all()))
