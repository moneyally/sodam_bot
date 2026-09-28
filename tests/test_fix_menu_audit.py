"""메뉴 감사 수정 (2026-09-28): 권한 P1~P3 · 두 번 눌림 B1~B6. python tests/run_all.py test_fix_menu_audit"""
import asyncio
import time

from fakes import FakeQuery, fake_user, runner
from test_menu import CHAT, OTHER, press, setup

from sodam import menu, raid
from sodam.menu import PanelCtx, Screen

test, run_all = runner()


async def _false(*a, **k):
    return False


# ── P1 · P2: 제재 성격 버튼은 지금 '사용자 차단' 권한으로 ─────────
@test
async def raid_button_needs_restrict_right_and_fresh():
    db, svc, bot, state = await setup()
    svc.perms.can = _false                                             # 관리자지만 '사용자 차단' 권한 없음
    q = await press(svc, bot, 1, f"m:rdm:{CHAT}:1")
    assert not await raid.active(svc, CHAT) and "사용자 차단" in q.answers[0][0], q.answers
    assert menu.ROUTES["rdm"].fresh and state.forgets >= 1


@test
def sanction_alert_buttons_check_rights_now():
    for code in ("sgx", "fbx", "gtb", "rdm"):
        assert menu.ROUTES[code].fresh, code                          # 강등된 관리자 캐시(5분)로 밴·뮤트 못 하게


# ── P3: 방에서는 그 방에 올린 카드 토큰만 ───────────────────────
@test
async def dm_menu_token_cannot_be_pressed_in_a_group():
    db, svc, bot, _ = await setup()
    tok = menu.token(svc, 1, CHAT, "ask_bw", "스팸")                   # 1:1 메뉴에서 만든 토큰 (메모리)
    q = FakeQuery(CHAT, fake_user(1, "방장"), f"m:k:{tok}")              # 콜백 데이터만 바꿔 그룹 메시지에서
    await menu.on_callback(svc, bot, q, ["k", tok])
    assert not q.edits and "이 방의 카드" in q.answers[0][0], q.answers
    assert tok in svc.menu_tokens, "막힌 누름이 토큰을 써 버리지 않음"
    room = await menu.lasting_token(svc, 1, OTHER, "ask_bw", "스팸")      # 다른 방 카드도 이 방에선 안 됨
    q = FakeQuery(CHAT, fake_user(1, "방장"), f"m:k:{room}")
    await menu.on_callback(svc, bot, q, ["k", room])
    assert "이 방의 카드" in q.answers[0][0]
    mine = await menu.lasting_token(svc, 1, CHAT, "ask_bw", "스팸")       # 이 방에 올린 카드는 됨
    q = FakeQuery(CHAT, fake_user(1, "방장"), f"m:k:{mine}")
    await menu.on_callback(svc, bot, q, ["k", mine])
    assert q.edits, q.answers


# ── B1 · B2 · B5: 두 번 눌러도 한 번 ───────────────────────────
@test
async def copy_schedule_twice_makes_one():
    db, svc, bot, _ = await setup()
    await db.ensure_chat(-1003333, "셋째 방")
    svc.perms.is_admin = lambda b, cid, uid: _true()
    sid = await db.add_schedule(CHAT, kind="interval", at_time=None, interval_min=30, title="t", text="x",
                                media_type=None, media_id=None, pin=False, created_by=1)
    before = len(await db.schedules(-1003333))
    await asyncio.gather(press(svc, bot, 1, f"m:scx:{CHAT}:{sid}:-1003333"), press(svc, bot, 1, f"m:scx:{CHAT}:{sid}:-1003333"))
    assert len(await db.schedules(-1003333)) == before + 1


async def _true(*a, **k):
    return True


@test
async def raid_on_by_two_admins_at_once_posts_one_notice():
    db, svc, bot, state = await setup(tg_admins=(1, 2))
    await asyncio.gather(press(svc, bot, 1, f"m:rdm:{CHAT}:1"), press(svc, bot, 2, f"m:rdm:{CHAT}:1"))
    notices = [c for c in bot.named("send_message") if c[1] == CHAT]
    assert await raid.active(svc, CHAT) and len(notices) == 1, notices


@test
async def join_rule_twice_makes_one():
    db, svc, bot, _ = await setup()
    await asyncio.gather(press(svc, bot, 1, f"m:rla:{CHAT}"), press(svc, bot, 1, f"m:rla:{CHAT}"))
    rows = await db._all("SELECT id FROM alert_rules WHERE chat_id=? AND trig='join'", (CHAT,))
    assert len(rows) == 1, rows


# ── B3: 오래 걸리는 AI 미리보기는 답 먼저 + 60초에 한 번 ──────────
@test
async def ai_preview_answers_first_and_has_cooldown():
    from sodam import cron
    db, svc, bot, _ = await setup()
    sid = await db.add_schedule(CHAT, kind="interval", at_time=None, interval_min=60, title="요약", text="",
                                media_type=None, media_id=None, pin=False, created_by=1, action="ai", skill="summary")
    gate = asyncio.Event()
    calls = []

    async def slow(svc_, row):
        calls.append(1)
        await gate.wait()
        return "오늘 요약"
    orig, cron.run_skill = cron.run_skill, slow
    try:
        q = await asyncio.wait_for(press(svc, bot, 1, f"m:scp:{CHAT}:{sid}"), 2)   # 작업이 안 끝나도 바로 답
        assert "만드는 중" in q.answers[0][0], q.answers
        q2 = await press(svc, bot, 1, f"m:scp:{CHAT}:{sid}")
        assert "초에 한 번" in q2.answers[0][0] and len(calls) == 1
        gate.set()
        for _ in range(50):
            if any("오늘 요약" in c[2] for c in bot.named("send_message")):
                break
            await asyncio.sleep(0.02)
        assert any(c[1] == 1 and "오늘 요약" in c[2] for c in bot.named("send_message"))
    finally:
        cron.run_skill = orig


# ── B4 · B6 ──────────────────────────────────────────────────
@test
def member_search_back_button_has_route():
    assert "mbq" in menu.ROUTES


@test
async def unexpected_error_still_answers():
    db, svc, bot, _ = await setup()

    async def boom(c: PanelCtx) -> Screen:
        raise RuntimeError("database is locked")
    menu.register_route("zzboom", menu.Route(boom))
    try:
        q = await press(svc, bot, 1, f"m:zzboom:{CHAT}")
        assert q.answers and "잠시 후" in q.answers[0][0], q.answers
    finally:
        menu.ROUTES.pop("zzboom", None)


# ── S1: 관리자 방 목록은 동시에 묻고 잠깐 기억 ─────────────────
@test
async def admin_groups_parallel_and_cached():
    db, svc, bot, _ = await setup()
    rooms = [-1005000 - i for i in range(24)]
    for cid in rooms:
        await db.ensure_chat(cid, f"방{cid}")
    asked = []

    async def slow_admin(b, cid, uid):
        asked.append(cid)
        await asyncio.sleep(0.05)
        return cid in rooms[:5]
    svc.perms.is_admin = slow_admin

    async def cands(uid):
        return rooms
    svc.perms.candidate_chats = cands
    t = time.monotonic()
    got = await menu.admin_groups(svc, bot, 1)
    assert [c for c, _ in got] == rooms[:5] and time.monotonic() - t < 0.6, time.monotonic() - t   # 하나씩이면 1.2초
    n = len(asked)
    assert await menu.admin_groups(svc, bot, 1) == got and len(asked) == n                          # 바로 또 부르면 안 물음
