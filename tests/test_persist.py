"""재시작해도 이어지는 상태 (sodam/persist.py): python tests/run_all.py persist

재시작 = 같은 DB 파일에 새 Services (+ 모듈 메모리 비움) → persist.restore. 배포마다·컨테이너 회수로 봇이 자주 다시 켜짐.
각 테스트는 고친 줄을 되돌리면 FAIL 한다 (뮤테이션 검증).
"""
import asyncio
import time
from types import SimpleNamespace

import httpx
from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import casino, diskguard, farewell, handlers, memory, menu, persist, util
from sodam.casino import core
from sodam.greet import Greeter
from sodam.permissions import Permissions
from sodam.services import PendingAction, PendingInput

test, run_all = runner()
CHAT = -100555
ADMIN, MEMBER = fake_user(1, "방장"), fake_user(50, "멤버")


def ctx_of(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})


async def world(**kw):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN.id}, **kw)
    await db.ensure_chat(CHAT, "방")
    return db, svc, FakeBot()


async def restart(db, **kw):
    """같은 DB 로 새 프로세스: 메모리(서비스·모듈 상태)는 비고, post_init 처럼 persist.restore."""
    await persist.drain()                  # 죽기 전에 끝난 뒤쪽 DB 쓰기
    farewell._rooms.clear()
    svc = await make_svc(db, admins={ADMIN.id}, **kw)
    bot = FakeBot()
    await persist.restore(svc, bot)
    return svc, bot


def sent(bot, chat=CHAT):
    return [c[2] for c in bot.named("send_message") if c[1] == chat]


# ── 잠깐 보이는 안내 ──────────────────────────────────────
@test
async def temp_notice_is_deleted_after_restart():
    """send_temp(도배 경고 등): job_queue 타이머는 재시작으로 사라짐 → 예전엔 방에 영영 남음."""
    db, svc, bot = await world()
    await handlers.send_temp(ctx_of(svc, bot), CHAT, "⚠️ 도배 경고", 60)
    msg_id = bot._next_id                                                  # 방금 보낸 안내
    svc2, bot2 = await restart(db)
    assert await persist.sweep(db, bot2, now=time.time() + 30) == 0          # 아직 기한 전: 안 지움
    assert await persist.sweep(db, bot2, now=time.time() + 60 + persist.SWEEP_GRACE + 1) == 1
    assert ("delete", CHAT, msg_id) in bot2.calls
    assert not await db._all("SELECT 1 FROM temp_msgs")
    assert await persist.sweep(db, bot2, now=time.time() + 999) == 0          # 한 번만


@test
async def temp_notice_on_time_delete_clears_row_and_old_rows_are_dropped():
    db, svc, bot = await world()
    ctx = ctx_of(svc, bot)
    await handlers.send_temp(ctx, CHAT, "잠깐", 10)
    cb, _, data = ctx.job_queue.once[-1]
    await cb(SimpleNamespace(job=SimpleNamespace(data=data), bot=bot, bot_data=ctx.bot_data))   # 제때 울린 타이머
    assert ("delete", CHAT, data[1]) in bot.calls and not await db._all("SELECT 1 FROM temp_msgs")
    await persist.remember_delete(db, bot, CHAT, 77, 10)
    await db._write("UPDATE temp_msgs SET sent=sent-?, due=due-?", (persist.TEMP_MAX_AGE + 5,) * 2)
    assert await persist.sweep(db, bot) == 0 and ("delete", CHAT, 77) not in bot.calls   # 48시간 넘은 글은 못 지움 → 버림
    assert not await db._all("SELECT 1 FROM temp_msgs")


@test
async def other_bots_rows_are_left_alone():
    """메인·딜러 봇이 DB 를 같이 씀 → 자기가 보낸 글만 지움."""
    db, svc, bot = await world()
    dealer = FakeBot()
    dealer.id = 4242
    await persist.remember_delete(db, dealer, CHAT, 5, 1)
    assert await persist.sweep(db, bot, now=time.time() + 999) == 0 and not bot.named("delete")
    assert await persist.sweep(db, dealer, now=time.time() + 999) == 1


@test
async def game_temp_reply_and_post_temp_survive_restart():
    """db 를 안 받는 옛 함수(casino.core.delete_later·util.post_temp)는 bind 된 DB 로."""
    db, svc, bot = await world()
    persist.bind(bot, db)
    try:
        core.delete_later(bot, CHAT, 901, 30)
        util.post_temp(bot, CHAT, "공동 차단 안내", 60)
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(await db._all("SELECT 1 FROM temp_msgs")) == 2
        for t in list(core._TEMP) + list(util._BG):
            t.cancel()                      # 재시작 = 기다리던 태스크 사라짐
        svc2, bot2 = await restart(db)
        assert await persist.sweep(db, bot2, now=time.time() + 999) == 2
    finally:
        persist.unbind(bot)


# ── 메뉴 글자 입력 ────────────────────────────────────────
@test
async def menu_text_input_continues_after_restart():
    """1:1 에서 [➕ 금지어] 누르고 입력하려는 사이 배포 → 예전엔 입력이 AI 대화로 새고 저장 안 됨."""
    db, svc, bot = await world()
    q = FakeQuery(ADMIN.id, ADMIN, f"m:in:{CHAT}:bw")
    await menu.on_callback(svc, bot, q, q.data.split(":")[1:])
    assert ADMIN.id in svc.inputs
    svc2, bot2 = await restart(db)
    assert svc2.inputs[ADMIN.id].kind == "bw" and svc2.inputs[ADMIN.id].chat_id == CHAT
    ai = []

    async def fake_ai(*a, **kw):
        ai.append(a)
    orig, handlers.ai_reply = handlers.ai_reply, fake_ai
    try:
        msg = FakeMsg(ADMIN.id, ADMIN, "도박, 스팸")
        await handlers.on_private(SimpleNamespace(message=msg), ctx_of(svc2, bot2))
    finally:
        handlers.ai_reply = orig
    assert "추가했어요" in msg.replies[0] and not ai, msg.replies
    assert sorted(await db.banned_words(CHAT)) == ["도박", "스팸"]
    svc3, _ = await restart(db)
    assert ADMIN.id not in svc3.inputs                                    # 끝난 입력은 DB 에서도 지워짐


@test
async def expired_menu_input_is_not_restored_and_cancel_is_persisted():
    db, svc, bot = await world()
    q = FakeQuery(ADMIN.id, ADMIN, f"m:in:{CHAT}:dom")
    await menu.on_callback(svc, bot, q, q.data.split(":")[1:])
    q = FakeQuery(ADMIN.id, ADMIN, f"m:g:{CHAT}")                          # 다른 버튼 = 입력 취소
    await menu.on_callback(svc, bot, q, q.data.split(":")[1:])
    svc2, _ = await restart(db)
    assert ADMIN.id not in svc2.inputs
    svc2.inputs[7] = PendingInput("bw", CHAT, expires=time.time() - persist.INPUT_KEEP - 5)   # 오래 지난 입력
    svc3, _ = await restart(db)
    assert 7 not in svc3.inputs


# ── 제재 확인 카드 ────────────────────────────────────────
@test
async def sanction_card_works_after_restart_once():
    """AI 가 올린 [✅ 뮤트] 카드 → 배포 → 눌렀더니 '만료된 요청' 이던 것. 두 번 빨리 눌러도 한 번만."""
    db, svc, bot = await world()
    key = svc.add_pending(PendingAction(CHAT, "mute", MEMBER.id, "멤버", "도배", ADMIN.id, minutes=10))
    svc2, bot2 = await restart(db)
    assert key not in svc2.pending
    qs = [FakeQuery(CHAT, ADMIN), FakeQuery(CHAT, ADMIN)]
    await asyncio.gather(*(handlers._confirm_action(svc2, bot2, q, [key, "y"]) for q in qs))
    mutes = [c for c in bot2.named("restrict") if c[2] == MEMBER.id]
    assert len(mutes) == 1, bot2.calls
    assert any("채팅 금지했어요" in e for q in qs for e in q.edits), [q.edits for q in qs]


@test
async def expired_or_cancelled_card_stays_dead_after_restart():
    db, svc, bot = await world()
    old = svc.add_pending(PendingAction(CHAT, "ban", MEMBER.id, "멤버", "사기", ADMIN.id, expires=time.time() - 1))
    key = svc.add_pending(PendingAction(CHAT, "ban", MEMBER.id, "멤버", "사기", ADMIN.id))
    await handlers._confirm_action(svc, bot, FakeQuery(CHAT, ADMIN), [key, "n"])      # 취소
    svc2, bot2 = await restart(db)
    for k in (old, key):
        q = FakeQuery(CHAT, ADMIN)
        await handlers._confirm_action(svc2, bot2, q, [k, "y"])
        assert "만료" in q.answers[0][0] and not bot2.named("ban"), (k, q.answers)


# ── 진행 중이던 게임 ──────────────────────────────────────
@test
async def word_game_is_ended_with_notice_after_restart():
    """끝말잇기 도중 배포 → 예전엔 게임이 말없이 사라져 멤버들이 계속 낱말을 쳐도 반응 없음."""
    db, svc, bot = await world()
    assert "시작" in await svc.games.start(bot, CHAT, ADMIN.id, "끝말잇기")
    svc.games.active[CHAT].cancel_timer()
    svc2, bot2 = await restart(db)
    assert sent(bot2) == [persist.RESTART_NOTICE]
    assert "다시 시작" in svc2.games.status(CHAT) and svc2.games.recent[CHAT][1] == "끝말잇기"   # AI 단서·game_control
    _, bot3 = await restart(db)
    assert not sent(bot3)                                                  # 한 번만


@test
async def finished_word_game_gives_no_notice():
    db, svc, bot = await world()
    assert "시작" in await svc.games.start(bot, CHAT, ADMIN.id, "끝말잇기 차례")
    await svc.games.stop(CHAT)
    _, bot2 = await restart(db)
    assert not sent(bot2)


@test
async def killed_casino_bets_refunded_with_room_notice():
    """kill -9·컨테이너 회수: 열린 베팅은 원래도 환불됐지만 방엔 아무 말 없었음."""
    db, svc, bot = await world()
    await core.credit(db, CHAT, MEMBER.id, 5000, "test")
    assert await core.debit(db, CHAT, MEMBER.id, 1000, "bet:bj")           # 판 도중 강제 종료 (정산 없음)
    await persist.drain()
    assert await casino.startup(svc) == 1                                   # post_init: 환불 → 알림 줄
    svc2, bot2 = await restart(db)
    assert await core.balance(db, CHAT, MEMBER.id) == 5000
    assert sent(bot2) == [persist.CASINO_NOTICE]


@test
async def graceful_shutdown_records_live_casino_rooms_and_roles_split():
    db, svc, bot = await world()
    from sodam.casino import cards
    cards.HANDS[("bj", CHAT, MEMBER.id)] = SimpleNamespace(done=False)
    hooks, casino.SHUTDOWN_HOOKS[:] = list(casino.SHUTDOWN_HOOKS), []
    try:
        assert casino.live_rooms() == {CHAT}
        await casino.shutdown(svc)
    finally:
        casino.SHUTDOWN_HOOKS[:] = hooks
        cards.HANDS.pop(("bj", CHAT, MEMBER.id), None)
    _, main_bot = await restart(db, bot_role="main")                       # 메인 봇은 딜러의 게임을 알리지 않음
    assert not sent(main_bot)
    _, dealer = await restart(db, bot_role="dealer")
    assert sent(dealer) == [persist.CASINO_NOTICE]


# ── 캡차 (원래 DB — 재시작 뒤에도 시간 초과·승인 확인) ──────
@test
async def captcha_pending_before_restart_still_kicked_or_approved():
    db, svc, bot = await world()
    await db.set_setting(CHAT, "captcha_action", "kick")
    late, ok = fake_user(60, "늦은이"), fake_user(61, "통과")
    assert await svc.captcha.start(bot, CHAT, late) and await svc.captcha.start(bot, CHAT, ok)
    svc2, bot2 = await restart(db)
    row = await db.get_captcha(CHAT, ok.id)
    q = FakeQuery(CHAT, ok)
    await svc2.captcha.on_callback(bot2, q, [str(ok.id), str(row["answer"])])
    assert "환영" in q.answers[0][0]
    await db._write("UPDATE captcha SET expires_at=0 WHERE user_id=?", (late.id,))
    await svc2.captcha.expire(bot2)
    assert ("ban", CHAT, late.id) in bot2.calls and not await db.get_captcha(CHAT, late.id)   # kick = ban+unban
    assert ("ban", CHAT, ok.id) not in bot2.calls


# ── 예약공지 마법사 ───────────────────────────────────────
@test
async def announce_wizard_continues_after_restart():
    db, svc, bot = await world()
    await svc.announcer.start_dm(bot, ADMIN.id, CHAT)
    await persist.drain()                                                  # 사람이 답하기 전 (저장은 뒤에서 이미 끝남)
    msg = FakeMsg(ADMIN.id, ADMIN, "월말 정산")
    assert await svc.announcer.handle_message(bot, msg)                   # 1/5 제목 → 2/5 내용
    svc2, bot2 = await restart(db)
    d = svc2.announcer.drafts[(ADMIN.id, ADMIN.id)]
    assert d.step == "body" and d.title == "월말 정산" and d.chat_id == CHAT
    assert await svc2.announcer.handle_message(bot2, FakeMsg(ADMIN.id, ADMIN, "매달 말일 정산해요"))
    assert "3/5 사진" in sent(bot2, ADMIN.id)[-1]


@test
async def cancelled_announce_wizard_not_restored():
    """메뉴에서 글자 입력을 시작하면 마법사는 버림 (drafts.pop) → 재시작 뒤 되살아나 입력을 가로채면 안 됨."""
    db, svc, bot = await world()
    await svc.announcer.start_dm(bot, ADMIN.id, CHAT)
    q = FakeQuery(ADMIN.id, ADMIN, f"m:in:{CHAT}:bw")
    await menu.on_callback(svc, bot, q, q.data.split(":")[1:])
    svc2, _ = await restart(db)
    assert not svc2.announcer.drafts and svc2.inputs[ADMIN.id].kind == "bw"


# ── 퇴장 인사 · 입장 인사 ─────────────────────────────────
@test
async def farewell_claim_survives_restart():
    """나감은 서비스 메시지·멤버 상태 두 경로로 옴. 그 사이 재시작(못 끝낸 업데이트 재전송)해도 인사 1번."""
    db, svc, bot = await world()
    await db.set_setting(CHAT, "farewell_mode", "on")
    await farewell.on_leave(ctx_of(svc, bot), CHAT, MEMBER, MEMBER)
    assert len(sent(bot)) == 1
    svc2, bot2 = await restart(db)
    await farewell.on_leave(ctx_of(svc2, bot2), CHAT, MEMBER, MEMBER)
    assert not sent(bot2)


@test
async def graceful_stop_sends_waiting_greetings_and_farewells():
    """입장 인사(5초 모음)·퇴장 인사(20초 합침) 대기 중 배포 → 예전엔 인사가 사라짐."""
    db, svc, bot = await world()
    svc.greeter = Greeter(svc)
    await db.set_setting(CHAT, "farewell_mode", "on")
    await db.set_setting(CHAT, "farewell_delete_after", 60)
    svc.greeter.queue(bot, CHAT, 70, "새친구")
    ctx = ctx_of(svc, bot)
    await farewell.on_leave(ctx, CHAT, fake_user(71, "첫째"), fake_user(71, "첫째"))
    await farewell.on_leave(ctx, CHAT, fake_user(72, "둘째"), fake_user(72, "둘째"))   # 20초 창 → 기다림
    before = len(sent(bot))
    await persist.flush_on_stop(svc, bot, ctx.bot_data)
    new = sent(bot)[before:]
    assert any("새친구" in t for t in new) and any("둘째" in t for t in new), new
    assert len(await db._all("SELECT 1 FROM temp_msgs")) == 2              # 첫째(바로)·둘째(종료 때) 자동 삭제도 다음 실행이


# ── 기억 정리 예약 ────────────────────────────────────────
@test
async def memory_extract_queue_resumes_after_restart():
    db, svc, bot = await world()
    await db.set_setting(CHAT, "ai_memory", True)
    assert memory.observe(svc, CHAT, MEMBER.id, "저는 강남에서 카페를 운영하고 있어요")
    for _ in range(20):
        await asyncio.sleep(0)
    await memory.shutdown(svc)                                             # 90초 기다리던 정리 취소 (배포)
    assert await db._all("SELECT 1 FROM memory_queue WHERE user_id=?", (MEMBER.id,))
    calls = []

    async def fake_extract(svc_, chat_id, user_id):
        calls.append((chat_id, user_id))
        return []
    orig, delay = memory.extract, memory.EXTRACT_DELAY
    memory.extract, memory.EXTRACT_DELAY = fake_extract, 0
    try:
        svc2, _ = await restart(db)
        await asyncio.wait(list(memory.state(svc2).tasks), timeout=5)
    finally:
        memory.extract, memory.EXTRACT_DELAY = orig, delay
    assert calls == [(CHAT, MEMBER.id)]
    assert not await db._all("SELECT 1 FROM memory_queue")


# ── 알림 한 번만 · 무작위 대입 ─────────────────────────────
@test
async def billing_recovery_alert_after_restart_during_outage():
    """TronGrid 장애 알림 뒤 재시작 → 연속 실패 수가 0 으로 돌아가 '복구' 알림이 영영 안 가던 것."""
    from test_billing import FakeTronGrid
    from sodam.billing import FAIL_ALERT, Billing
    db, svc, bot = await world(pay_address="TPAYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", trongrid_api_key="k")
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})))
    b = Billing(svc.cfg, db, down)
    for _ in range(FAIL_ALERT):
        try:
            await b.check_pending()
        except Exception:
            pass
    assert b.take_alert() == "down"
    b2 = Billing(svc.cfg, db, httpx.AsyncClient(transport=httpx.MockTransport(FakeTronGrid().handler)))   # 재시작
    await b2.check_pending()
    assert b2.take_alert() == "up" and b2.fail_streak == 0
    b3 = Billing(svc.cfg, db, httpx.AsyncClient(transport=httpx.MockTransport(FakeTronGrid().handler)))
    await b3.check_pending()
    assert b3.take_alert() is None


@test
async def owner_code_attempts_count_across_restarts():
    """재시작마다 새 코드 + 틀린 횟수 0 → 한 사람이 재시작마다 5번씩 더 시도하던 것."""
    from fakes import cfg
    db = await make_db()
    p = Permissions(cfg(db.path, owner_ids=frozenset()), db)
    code = await p.prepare_claim_code()
    for _ in range(Permissions.CLAIM_MAX_PER_USER):
        assert not await p.claim(9, "x" + code[1:])
    p2 = Permissions(cfg(db.path, owner_ids=frozenset()), db)             # 재시작
    code2 = await p2.prepare_claim_code()
    assert not await p2.claim(9, code2)                                    # 같은 사람은 오늘 더는 못 함
    assert await p2.claim(10, code2)                                       # 진짜 오너(다른 사람)는 됨


@test
async def disk_alert_not_repeated_by_restart():
    db, svc, bot = await world()
    reports = []

    async def report(_bot, text):
        reports.append(text)
    svc.mod.report = report
    diskguard._last_alert = 0.0
    assert await diskguard.check(svc, bot, free=10)
    diskguard._last_alert = 0.0                                             # 재시작 = 모듈 메모리 비움
    assert await diskguard.check(svc, bot, free=10)
    assert len(reports) == 1, reports


@test
async def mtproto_flood_and_hourly_caps_survive_restart():
    from sodam import mtproto
    db, svc, bot = await world()
    mt = mtproto.MTProto(svc.cfg, db)
    mt.bot.flood_until = time.time() + 3600
    for _ in range(mtproto.RESOLVE_PER_HOUR):
        mt.resolve_log.append(time.time())
    mt._count_call()
    await persist.drain()
    mt2 = mtproto.MTProto(svc.cfg, db)                                      # 재시작
    await mt2.load_rate()
    assert mt2.bot.flood_until > time.time() + 3000
    assert len(mt2.resolve_log) == mtproto.RESOLVE_PER_HOUR and mt2.calls_last_hour() == 1
