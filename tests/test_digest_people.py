"""🧠 AI 하루 요약 — 받는 사람(대표님) 기준 발송: python tests/run_all.py digest_people

실제 운영에서 본 문제: 관리자 대부분이 1:1 을 안 열어 조용히 실패 · 여러 방 관리자는 방마다 한 통 · 부관리자까지 전부 받고 끌 수 없음.
확인: 기본 받는 사람 = 등록한 대표님(지금도 관리자일 때) · 다른 관리자는 '받기 (나)' · 오너는 관리자인 방 묶음 ·
한 사람 하루 한 통(방 여럿이면 묶음 + [📊 방] 버튼) · 사람별 시각 · AI 는 방마다 하루 1번(받는 사람 수·재시작과 무관) ·
🔕 그만 받기 · 1:1 막힘(Forbidden) → 방에 금액 없는 안내 7일 1번 · 콜백 64바이트 · 하네스로 새 화면 도달.
"""
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import ScriptedLLM  # noqa: E402
from fakes import TZ, FakeBot, FakeQuery, fake_user, make_db, make_svc, runner  # noqa: E402

from sodam import menu, reports  # noqa: E402
from sodam.billing import Billing  # noqa: E402

test, run_all = runner()

BOSS, VICE, JUNIOR, OWNER, EXREG = (fake_user(1, "김대표", "boss"), fake_user(2, "부방장"), fake_user(3, "막내관리"),
                                    fake_user(7, "운영자"), fake_user(8, "예전대표"))
BOT = fake_user(999, "소담", "sodambot", is_bot=True)
R1, R2, R3 = -1007770000001, -1007770000002, -1007770000003
PAY = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class RoomBot(FakeBot):
    """방마다 관리자가 다른 가짜 봇."""

    def __init__(self, by_chat):
        super().__init__()
        self.by_chat = by_chat
        self.admin_lookups = 0

    async def get_chat_administrators(self, chat_id):
        self.admin_lookups += 1
        return [SimpleNamespace(user=u, status="administrator") for u in [*self.by_chat.get(chat_id, ()), BOT]]


def hour_now() -> int:
    return datetime.now(TZ).hour


async def world(rooms=None, script=None, lines=15):
    """rooms = {방: (등록한 사람, [관리자])}. 전부 체험 중(이용 가능)·방 기본 시각 = 지금."""
    rooms = rooms or {R1: (BOSS, [BOSS, VICE]), R2: (BOSS, [BOSS, JUNIOR]), R3: (EXREG, [JUNIOR])}
    db = await make_db()
    everyone = {u.id for _, adm in rooms.values() for u in adm}
    svc = await make_svc(db, admins=everyone, pay_address=PAY)
    svc.billing = Billing(svc.cfg, db)
    svc.llm = ScriptedLLM(json_script={"digest": script if script is not None else [
        {"topics": [f"화제{i} <b>"], "conflict": "", "unanswered": []} for i in range(len(rooms))]})
    now = int(time.time())
    for k, (cid, (reg, _)) in enumerate(rooms.items(), 1):
        await db.ensure_chat(cid, f"업자방{k}")
        await db._write("INSERT INTO subscriptions(chat_id, trial_until, paid_until, added_by, updated_at) "
                        "VALUES(?,?,?,?,?)", (cid, now + 2 * 86400, None, reg.id, now))
        for i in range(lines):
            await db.log_message(cid, 50 + i % 2, 100 + i, f"방{k} 대화 {i}")
        await db.set_setting(cid, "digest_hour", hour_now())
    for u in (BOSS, VICE, JUNIOR, OWNER, EXREG):
        await db.upsert_user(u)
    bot = RoomBot({cid: adm for cid, (_, adm) in rooms.items()})
    return db, svc, bot


def dms(bot, uid, marker=None):
    return [c for c in bot.named("send_message") if c[1] == uid and (marker is None or marker in c[2])]


def buttons(call):
    kb = call[3].get("reply_markup")
    return [b for row in kb.inline_keyboard for b in row] if kb else []


def ai_calls(svc):
    return svc.llm.of("json", "digest")


async def press(svc, bot, user, data):
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


# ── 받는 사람 ─────────────────────────────────────────────
@test
async def registrant_gets_it_other_admins_only_when_opted_in():
    db, svc, bot = await world()
    assert await reports.run_digests(svc, bot) == 1
    boss = dms(bot, BOSS.id)
    assert len(boss) == 1, boss                                     # 두 방 등록 → 한 통 묶음
    assert "(2개 방)" in boss[0][2] and "업자방1" in boss[0][2] and "업자방2" in boss[0][2], boss[0][2]
    assert not dms(bot, VICE.id) and not dms(bot, JUNIOR.id)        # 부관리자는 기본으로 안 받음
    assert not dms(bot, EXREG.id)                                   # 등록했지만 이제 관리자가 아님 → 안 받음
    assert not any(c[1] == R3 for c in bot.named("send_message"))
    assert {c["chat_id"] for c in ai_calls(svc)} == {R1, R2}        # 받을 사람이 없는 R3 는 AI 도 안 부름
    # 10분 뒤 다시 돌아도 (관리자 아닌 예전 대표님 포함) 텔레그램에 관리자 목록을 또 묻지 않음
    before = bot.admin_lookups
    assert await reports.run_digests(svc, bot) == 0 and bot.admin_lookups == before
    assert (await db._one("SELECT status FROM digest_log WHERE user_id=?", (EXREG.id,)))["status"] == "none"


@test
async def no_resend_on_deploy_day_when_old_version_already_sent():
    """예전 버전(방 단위)이 오늘 이미 보낸 방은 새 버전 첫날 다시 안 보냄 (AI 도 안 부름)."""
    db, svc, bot = await world()
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    await db.bump(day, R1, reports.LEGACY_SENT)
    await reports.run_digests(svc, bot)
    assert {c["chat_id"] for c in ai_calls(svc)} == {R2}, ai_calls(svc)
    assert "업자방1" not in " ".join(c[2] for c in dms(bot, BOSS.id))


@test
async def admin_opt_in_from_report_screen():
    db, svc, bot = await world()
    q = await press(svc, bot, VICE, f"m:rp:{R1}")
    toggle = [b for row in q.kb.inline_keyboard for b in row if "하루 요약 받기 (나)" in b.text]
    assert toggle and toggle[0].text.startswith("❌") and toggle[0].callback_data == f"m:rpme:{R1}:1", q.kb
    q = await press(svc, bot, VICE, toggle[0].callback_data)
    assert "✅ 🧠 하루 요약 받기 (나)" in str([b.text for r in q.kb.inline_keyboard for b in r]), q.kb
    await reports.run_digests(svc, bot)
    vice = dms(bot, VICE.id)
    assert len(vice) == 1 and "업자방1" in vice[0][2] and "업자방2" not in vice[0][2], vice   # 켠 방만
    assert "📌 <b>오늘 주요 화제</b>" in vice[0][2]                  # 방 하나 = 긴 요약
    assert len(ai_calls(svc)) == 2                                  # R1 요약은 대표님·부방장이 같이 씀
    # 등록한 대표님도 이 방만 끌 수 있음 (다른 방은 계속)
    db2, svc2, bot2 = await world()
    await press(svc2, bot2, BOSS, f"m:rpme:{R1}:0")
    await reports.run_digests(svc2, bot2)
    boss = dms(bot2, BOSS.id)
    assert len(boss) == 1 and "업자방2" in boss[0][2] and "업자방1" not in boss[0][2], boss


@test
async def rpme_is_tg_admin_only():
    db, svc, bot = await world()
    stranger = fake_user(55, "멤버")
    q = await press(svc, bot, stranger, f"m:rpme:{R1}:1")
    assert not q.edits and q.answers[0][1] is True
    assert not await db._all("SELECT * FROM digest_prefs")


@test
async def owner_combined_only_rooms_where_admin():
    db, svc, bot = await world({R1: (BOSS, [BOSS, OWNER]), R2: (VICE, [VICE, OWNER]), R3: (JUNIOR, [JUNIOR])})
    svc.perms.owner_ids = {OWNER.id}
    assert await reports.run_digests(svc, bot) == 4
    own = dms(bot, OWNER.id)
    assert len(own) == 1 and "(2개 방)" in own[0][2], own
    assert "업자방1" in own[0][2] and "업자방2" in own[0][2] and "업자방3" not in own[0][2]
    datas = [b.callback_data for b in buttons(own[0])]
    assert f"m:rp:{R1}" in datas and f"m:rp:{R2}" in datas and "m:dgp:new" in datas and "m:dgp:stop" in datas, datas
    assert len(ai_calls(svc)) == 3                                  # 방 3개 · 받는 사람 4명 → AI 3번


# ── 한 사람 한 통 · 버튼 · 나눠 보내기 ─────────────────────
@test
async def one_message_many_rooms_buttons_and_chunks():
    ds = [reports.Digest(-1001000000000 - i, f"아주 긴 방 이름 <{i}> & 친구들 소통방", 10, 3, 1, "AI 답변 3",
                         built_at=int(time.time()), topics=["가" * 70, "나" * 70], conflict="다" * 190) for i in range(40)]
    msgs = reports.person_messages(ds, TZ)
    assert len(msgs) > 1 and all(len(t) <= reports.TG_LIMIT for t, _ in msgs)
    rooms = [b.callback_data for _, kb in msgs for row in kb.inline_keyboard for b in row
             if b.callback_data.startswith("m:rp:")]
    assert rooms == [f"m:rp:{d.chat_id}" for d in ds]               # 방마다 버튼 하나, 그 방 글이 든 통에
    for text, kb in msgs:
        for row in kb.inline_keyboard:
            assert len(row) <= 2
            for b in row:
                assert len(b.callback_data.encode()) <= 64 and len(b.text) <= 24, b
        assert "<{" not in text and "&lt;" in text                  # 방 이름 esc
    last = [b.callback_data for row in msgs[-1][1].inline_keyboard for b in row]
    assert last[-2:] == ["m:dgp:new", "m:dgp:stop"]
    assert all("m:dgp" not in str(kb.to_dict()) for _, kb in msgs[:-1])
    one = reports.person_messages(ds[:1], TZ)
    assert len(one) == 1 and [b.callback_data for r in one[0][1].inline_keyboard for b in r] == \
        [f"m:rp:{ds[0].chat_id}", "m:dgp:new", "m:dgp:stop"]
    assert "오늘 주요 화제" in one[0][0] and "기준 최근 24시간" in one[0][0]


# ── 사람별 시각 · AI 1번 (재시작 포함) ──────────────────────
@test
async def per_person_hour_and_ai_once_per_room_across_restart():
    db, svc, bot = await world({R1: (BOSS, [BOSS, VICE, JUNIOR, OWNER])})
    svc.perms.owner_ids = {OWNER.id}
    h = hour_now()
    await reports.set_pref(db, VICE.id, R1, enabled=1)
    await reports.set_pref(db, JUNIOR.id, R1, enabled=1)
    await reports.set_pref(db, OWNER.id, reports.ALL_ROOMS, hour=(h + 5) % 24)   # 오너는 나중 시각
    assert await reports.run_digests(svc, bot) == 3
    assert len(ai_calls(svc)) == 1                                  # 3명이 받아도 AI 1번
    assert not dms(bot, OWNER.id)
    texts = {c[2] for uid in (BOSS.id, VICE.id, JUNIOR.id) for c in dms(bot, uid)}
    assert len(texts) == 1 and "화제0 &lt;b&gt;" in next(iter(texts))   # 같은 요약을 같이 씀
    # 재시작(새 Services·같은 DB) 뒤 오너 시각이 됨 → 저장된 요약을 그대로, AI 다시 안 부름
    svc2 = await make_svc(db, admins=svc.perms.admins, pay_address=PAY)
    svc2.billing, svc2.llm, svc2.perms.owner_ids = svc.billing, ScriptedLLM(), {OWNER.id}
    await reports.set_pref(db, OWNER.id, reports.ALL_ROOMS, hour=h)
    assert await reports.run_digests(svc2, bot) == 1
    assert not ai_calls(svc2) and len(ai_calls(svc)) == 1
    own = dms(bot, OWNER.id)
    assert len(own) == 1 and own[0][2] in texts, own
    # 같은 날 다시 돌아도 아무에게도 두 번 안 감
    assert await reports.run_digests(svc2, bot) == 0
    assert all(len(dms(bot, u.id)) == 1 for u in (BOSS, VICE, JUNIOR, OWNER))


@test
async def personal_hour_not_yet_means_not_sent_and_room_off_stops_everyone():
    db, svc, bot = await world({R1: (BOSS, [BOSS, VICE])})
    h = hour_now()
    await reports.set_pref(db, BOSS.id, reports.ALL_ROOMS, hour=(h + 5) % 24)
    await reports.set_pref(db, VICE.id, R1, enabled=1)
    await db.set_setting(R1, "digest_hour", reports.DIGEST_OFF)     # 방 전체 끔 → 켠 사람도 안 받음
    await reports.set_pref(db, VICE.id, reports.ALL_ROOMS, hour=h)
    assert await reports.run_digests(svc, bot) == 0 and not ai_calls(svc)
    await db.set_setting(R1, "digest_hour", h)
    assert await reports.run_digests(svc, bot) == 1
    assert dms(bot, VICE.id) and not dms(bot, BOSS.id)              # 대표님은 개인 시각(5시간 뒤)까지 기다림


@test
async def paid_only_and_quiet_room_skipped():
    db, svc, bot = await world({R1: (BOSS, [BOSS]), R2: (BOSS, [BOSS])})
    await db._write("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (int(time.time()) - 60, R2))
    await reports.run_digests(svc, bot)
    boss = dms(bot, BOSS.id)
    assert len(boss) == 1 and "업자방1" in boss[0][2] and "업자방2" not in boss[0][2]
    assert {c["chat_id"] for c in ai_calls(svc)} == {R1}


# ── 🔕 그만 받기 · ⏰ 받는 시각 ─────────────────────────────
@test
async def stop_button_keeps_message_and_stops_all_rooms():
    db, svc, bot = await world()
    q = await press(svc, bot, BOSS, "m:dgp:stop")
    assert not q.edits and q.answers[0][1] is True and "안 보내요" in q.answers[0][0]   # 요약 글은 그대로
    assert (await reports.my_prefs(db, BOSS.id))["enabled"] == 0
    assert await reports.run_digests(svc, bot) == 0 and not dms(bot, BOSS.id) and not ai_calls(svc)
    # ⏰ → 새 메시지로 설정 화면 (요약 글을 덮지 않음) → 21시 고르면 다시 받음
    q = await press(svc, bot, BOSS, "m:dgp:new")
    assert not q.edits
    screen = dms(bot, BOSS.id, "내 AI 하루 요약")
    assert len(screen) == 1 and "안 받음" in screen[0][2]
    datas = [b.callback_data for b in buttons(screen[0])]
    assert {"m:dgp:h:9", "m:dgp:h:18", "m:dgp:h:21", "m:dgp:h:23", "m:dgp:h:d", "m:dgp:off"} <= set(datas), datas
    q = await press(svc, bot, BOSS, "m:dgp:h:21")
    assert "매일 21:00" in q.edits[-1]
    assert await reports.my_prefs(db, BOSS.id) == {"enabled": None, "hour": 21}
    q = await press(svc, bot, BOSS, "m:dgp:h:7")                   # 목록에 없는 시각은 무시
    assert not q.edits and await reports.my_prefs(db, BOSS.id) == {"enabled": None, "hour": 21}
    await press(svc, bot, BOSS, "m:dgp:h:d")
    assert await reports.my_prefs(db, BOSS.id) == {"enabled": None, "hour": None}
    assert await reports.run_digests(svc, bot) == 1


@test
async def opting_into_a_room_after_stop_resumes():
    db, svc, bot = await world({R1: (BOSS, [BOSS, VICE])})
    await press(svc, bot, VICE, "m:dgp:stop")
    await press(svc, bot, VICE, f"m:rpme:{R1}:1")
    assert (await reports.my_prefs(db, VICE.id))["enabled"] is None
    await reports.run_digests(svc, bot)
    assert dms(bot, VICE.id)


# ── 1:1 을 안 연 대표님 ─────────────────────────────────────
@test
async def forbidden_dm_posts_room_notice_rarely():
    db, svc, bot = await world()
    bot.dm_blocked = {BOSS.id}
    assert await reports.run_digests(svc, bot) == 0
    notes = [c for c in bot.named("send_message") if c[1] in (R1, R2, R3)]
    assert sorted(c[1] for c in notes) == sorted([R1, R2]), notes    # 대표님이 등록한 방마다 1번
    for c in notes:
        text, kb = c[2], c[3]["reply_markup"]
        assert f'<a href="tg://user?id={BOSS.id}">김대표</a>' in text and "1:1" in text, text
        url = kb.inline_keyboard[0][0].url
        assert url == f"https://t.me/{bot.username}?start=cfg_{c[1]}", url
        for bad in ("USDT", "결제", "구독", "pay:", "30"):           # 방엔 금액·결제 얘기 없음
            assert bad not in text and bad not in str(kb.to_dict()), (bad, text)
    row = await db._one("SELECT status, rooms FROM digest_log WHERE user_id=?", (BOSS.id,))
    assert row["status"] == "forbidden" and str(R1) in row["rooms"], dict(row)
    q = await press(svc, bot, BOSS, f"m:rp:{R1}")                   # 📊 화면에도 표시
    assert "1:1 을 열지 않아" in q.edits[-1]
    # 다음 날도 막혀 있어도 7일 안엔 방 안내 다시 안 함
    await db._write("DELETE FROM digest_log")
    await db._write("DELETE FROM digest_cache")
    await reports.run_digests(svc, bot)
    assert len([c for c in bot.named("send_message") if c[1] in (R1, R2)]) == 2
    # 7일 지나면 한 번 더
    for cid in (R1, R2):
        await db.set_state(cid, reports.DIGEST_NOTICE_STATE, int(time.time()) - 8 * 86400)
    await db._write("DELETE FROM digest_log")
    await db._write("DELETE FROM digest_cache")
    await reports.run_digests(svc, bot)
    assert len([c for c in bot.named("send_message") if c[1] in (R1, R2)]) == 4
    # 1:1 을 열면(차단 풀림) 그날부터 정상
    bot.dm_blocked = set()
    await db._write("DELETE FROM digest_log")
    assert await reports.run_digests(svc, bot) == 1


@test
async def opted_in_admin_forbidden_does_not_ping_room():
    """방 안내는 등록한 대표님만 부름 (받기를 켠 다른 관리자가 봇을 차단했다고 방에 올리지 않음)."""
    db, svc, bot = await world({R1: (BOSS, [BOSS, VICE])})
    await reports.set_pref(db, VICE.id, R1, enabled=1)
    bot.dm_blocked = {VICE.id}
    await reports.run_digests(svc, bot)
    assert dms(bot, BOSS.id) and not [c for c in bot.named("send_message") if c[1] == R1]


# ── 하네스: 새 화면이 버튼으로 닿고 규칙을 지키는지 ─────────
@test
async def harness_reaches_new_screens():
    import harness
    from sodam.panels import members as members_panel
    try:
        rep, db, svc = await harness.crawl()
    finally:   # 크롤러가 누른 CSV 내보내기 간격(모듈 전역)이 뒤 테스트(test_fix_mod)의 같은 사용자 ID 를 막지 않게
        members_panel._csv_last.clear()
    datas = {s.data for s in rep.shots}
    assert f"m:rpme:{harness.CHAT}:1" in datas or f"m:rpme:{harness.CHAT}:0" in datas, sorted(datas)[:50]
    assert {"m:dgp", "m:dgp:h:9", "m:dgp:off", "m:dgp:h:d"} <= datas
    assert not [i for i in rep.issues if "rp" in i or "dgp" in i], rep.issues
    # 요약 메시지에만 있는 버튼(⏰ 새 메시지 · 🔕)도 하네스 규칙으로
    world_db, wsvc, wbot = await harness.make_world()
    for uid in (harness.OWNER, harness.MEMBER):
        for data in ("m:dgp:new", "m:dgp:stop"):
            wsvc.menu_limiter._hits.clear()
            q = await harness.press(wsvc, wbot, uid, data)
            assert len(q.answers) == 1, (uid, data, q.answers)
            for c in wbot.calls:
                if c[0] == "send_message":
                    assert not harness.html_errors(c[2]), c[2]


if __name__ == "__main__":
    import asyncio
    sys.exit(1 if asyncio.run(run_all()) else 0)
