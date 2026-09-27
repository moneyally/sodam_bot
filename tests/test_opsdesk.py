"""📥 운영 인박스 · 🧭 오너 운영센터 · 💸 비용 예측 (sodam/opsdesk.py · panels/opsdesk.py): python tests/run_all.py opsdesk

전부 실제 라우팅(menu.on_callback)·실제 도구 실행(tools.execute)으로: 역할별 권한 누출(멤버·A 방 관리자가 B 방·봇관리자·오너),
위조·오래된 콜백, 두 번 누름, answer 1번, 64바이트, HTML 이스케이프, 빈 상태, 긴 목록(쪽 나눔·4096자), 금액 노출.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from telegram.error import BadRequest

from fakes import FakeBot, fake_user, make_db, make_svc, runner
from harness import HQuery, html_errors

from sodam import announce, costs, cron, menu, opsdesk, tools
from sodam.ai_settings import ROOM_TOKENS_MAX
from sodam.billing import Billing
from sodam.llm import ROOM_TOKENS
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()

CHAT, OTHER = -1001000000011, -1001000000022
OWNER, A_ADMIN, B_ADMIN, BOTADM, MEMBER = 7, 1, 2, 30, 20
TITLE = "<b>A</b> & 방"


class Perms:
    """오너 7 · CHAT 텔레그램 관리자 1 · OTHER 텔레그램 관리자 2 · CHAT 봇관리자 30 · 멤버 20."""

    def __init__(self):
        self.tg = {CHAT: {A_ADMIN}, OTHER: {B_ADMIN}}
        self.botadm = {CHAT: {BOTADM}}
        self.no_rights: set[int] = set()

    async def owners(self):
        return {OWNER}

    async def candidate_chats(self, uid):
        return [CHAT, OTHER]

    async def is_admin(self, bot, cid, uid):
        return uid == OWNER or uid in self.tg.get(cid, set()) | self.botadm.get(cid, set())

    async def is_tg_admin(self, bot, cid, uid):
        return uid == OWNER or (cid < 0 and uid in self.tg.get(cid, set()))

    async def role(self, bot, cid, uid):
        if uid == OWNER:
            return Role.OWNER
        return Role.ADMIN if cid < 0 and await self.is_admin(bot, cid, uid) else Role.MEMBER

    async def can(self, bot, cid, uid, right="restrict"):
        return await self.is_admin(bot, cid, uid)

    async def protected(self, bot, cid, uid):
        return await self.is_admin(bot, cid, uid)

    def forget(self, cid):
        pass

    def forget_bot(self, cid):
        pass

    async def bot_can_moderate(self, bot, cid):
        return cid not in self.no_rights

    async def admin_users(self, bot, cid):
        return [fake_user(u, "관리자") for u in self.tg.get(cid, ())]


async def world(billing=False):
    db = await make_db()
    kw = {"pay_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"} if billing else {}
    svc = await make_svc(db, **kw)
    svc.perms = Perms()
    svc.mod.perms = svc.perms
    if billing:
        svc.billing = Billing(svc.cfg, db)
    await db.ensure_chat(CHAT, TITLE)
    await db.ensure_chat(OTHER, "B 방")
    for uid, name in ((OWNER, "오너"), (A_ADMIN, "에이 <i>&"), (B_ADMIN, "비"), (BOTADM, "봇관"), (MEMBER, "멤버")):
        await db.upsert_user(fake_user(uid, name))
    return db, svc, FakeBot()


async def press(svc, bot, uid, data, chat=None):
    svc.menu_limiter._hits.clear()
    q = HQuery(uid, data)
    if chat is not None:
        q.message = SimpleNamespace(chat_id=chat)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, (data, q.answers)
    for text, kb in q.edits:
        assert not html_errors(text), (data, html_errors(text))
        for row in getattr(kb, "inline_keyboard", ()):
            for b in row:
                assert b.callback_data is None or len(b.callback_data.encode()) <= 64, b.callback_data
                if b.callback_data and b.callback_data.startswith("m:"):
                    assert b.callback_data.split(":")[1] in menu.ROUTES, b.callback_data
    return q


def text_of(q) -> str:
    return q.edits[-1][0] if q.edits else ""


def cbs(q) -> list[str]:
    kb = q.edits[-1][1] if q.edits else None
    return [b.callback_data for row in getattr(kb, "inline_keyboard", ()) for b in row if b.callback_data]


def denied(q) -> bool:
    return not q.edits and q.answers[0][1]


async def scam(db, cid, name="<b>사기</b> & 꾼", ago=60, done=None):
    return await db._write("INSERT INTO scam_alerts(chat_id, user_id, msg_id, name, reason, deleted, ts, done) "
                           "VALUES(?,?,?,?,?,?,?,?)", (cid, 5555, 1, name, "지갑 <a>", 0, int(time.time()) - ago, done))


async def anomaly_alert(db, cid, status=None, ago=60):
    return await db._write("INSERT INTO anomaly_alerts(chat_id, ts, day, score, summary, detail, sent, status) "
                           "VALUES(?,?,?,?,?,?,?,?)", (cid, int(time.time()) - ago, time.strftime("%Y-%m-%d"), 70,
                                                         "입장 +9 <x>", json.dumps({"reasons": []}), 1, status))


def ctx_for(svc, bot, chat_id, uid, role):
    return ToolCtx(svc, bot, chat_id, fake_user(uid, "누구"), role, {})


# ── 📥 사실 모으기 ────────────────────────────────────────
@test
async def inbox_collects_open_facts_only_in_time_order():
    db, svc, bot = await world()
    a1 = await anomaly_alert(db, CHAT, ago=300)
    await anomaly_alert(db, CHAT, status="ignored", ago=200)            # 처리됨 → 안 보임
    await anomaly_alert(db, CHAT, ago=8 * 86400)                        # 7일 지난 알림 → 안 보임
    s1 = await scam(db, CHAT, ago=100)
    await scam(db, CHAT, done="t", ago=50)                              # 괜찮음 처리 → 안 보임
    await scam(db, OTHER, ago=10)                                       # 다른 방
    items = await opsdesk.room_items(svc, bot, CHAT, A_ADMIN)
    assert [i.key for i in items] == [f"a{a1}", f"s{s1}"], [i.key for i in items]
    got = opsdesk.ordered(items)
    assert [i.key for i in got] == [f"s{s1}", f"a{a1}"]                   # 기록은 최신순
    svc.perms.no_rights.add(CHAT)                                       # 지금 상태(봇 권한 없음)는 맨 앞
    got = opsdesk.ordered(await opsdesk.room_items(svc, bot, CHAT, A_ADMIN))
    assert got[0].kind == "rights" and got[1].key == f"s{s1}"


@test
async def pending_room_card_only_while_both_buttons_unpressed():
    db, svc, bot = await world()
    spec = {"when": ["daily", "09:00", None, None], "action": "remind", "skill": None, "text": "x", "title": "",
            "deliver": "room"}
    ok = await menu.lasting_token(svc, A_ADMIN, CHAT, "cron_save", spec, 1800)
    no = await menu.lasting_token(svc, A_ADMIN, CHAT, "cron_no", None, 1800)
    [card] = [i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.kind == "card"]
    assert "내가 요청" in card.text and card.open == f"m:sc:{CHAT}"
    [card_b] = [i for i in await opsdesk.room_items(svc, bot, CHAT, OWNER) if i.kind == "card"]
    assert "에이 <i>&님이" in card_b.text                                  # 다른 관리자에겐 요청자 이름 (화면에서 esc)
    q = await press(svc, bot, MEMBER, f"m:k:{ok}", chat=CHAT)            # 남이 누르면 거절, 카드 그대로
    assert q.answers[0][1] and [i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.kind == "card"]
    await press(svc, bot, A_ADMIN, f"m:k:{no}", chat=CHAT)               # 방에서 ❌ → 확인 토큰은 남지만 '안 누름' 아님
    assert await db._one("SELECT 1 FROM menu_tokens WHERE tok=?", (ok,))
    assert not [i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.kind == "card"]
    # 만료된 카드도 안 보임
    ok2 = await menu.lasting_token(svc, A_ADMIN, CHAT, "rule_save", {}, 1800)
    await menu.lasting_token(svc, A_ADMIN, CHAT, "rule_no", None, 1800)
    assert [i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.kind == "card"]
    await db._write("UPDATE menu_tokens SET expires=? WHERE action IN ('rule_save','rule_no')", (time.time() - 1,))
    assert not [i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.kind == "card"], ok2


@test
async def schedule_failures_recorded_through_real_cron_and_announce():
    db, svc, bot = await world()
    svc.perms.tg[CHAT] = {A_ADMIN}
    sid = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title="<b>알람</b>", text="t",
                                media_type=None, media_id=None, pin=False, created_by=MEMBER, action="remind")
    row = await db.get_schedule(CHAT, sid)
    assert await cron.fire(svc, bot, row) is False                     # 만든 사람이 관리자가 아님 → 꺼짐 + 기록
    sid2 = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title="", text="t",
                                 media_type=None, media_id=None, pin=False, created_by=A_ADMIN, action="remind")

    class Broken(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            raise BadRequest("Chat not found")
    assert await cron.fire(svc, Broken(), await db.get_schedule(CHAT, sid2)) is False
    sid3 = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title="공지", text="t",
                                 media_type=None, media_id=None, pin=False, created_by=A_ADMIN)
    await svc.announcer.publish(Broken(), await db.get_schedule(CHAT, sid3))
    sid4 = await db.add_schedule(CHAT, kind="once", at_time="09-01 09:00", interval_min=None, title="", text="t",
                                 media_type=None, media_id=None, pin=False, created_by=A_ADMIN,
                                 at_ts=int(time.time()) - announce.ONCE_GRACE - 60)
    await svc.announcer.run_due(bot)
    ev = [(r["ref"], r["kind"]) for r in await db._all("SELECT ref, kind FROM ops_events ORDER BY id")]
    assert ev == [(sid, "sched_creator"), (sid2, "sched_send"), (sid3, "sched_send"), (sid4, "sched_missed")], ev
    items = {i.key[0] + str(i.open): i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.kind == "sched"}
    assert len(items) == 4
    first = items[f"fm:sci:{CHAT}:{sid}"]
    assert "만든 관리자가 더는 관리자가 아니라" in first.text and "<b>알람</b>" in first.text
    # 같은 예약이 또 실패하면 한 줄로 합치고 횟수
    await cron.fire(svc, Broken(), await db.get_schedule(CHAT, sid2))
    [two] = [i for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN) if i.open == f"m:sci:{CHAT}:{sid2}"]
    assert "최근 7일 2번" in two.text


@test
async def record_failure_is_swallowed():
    db, svc, bot = await world()
    await db._write("DROP TABLE ops_events")
    await opsdesk.schedule_failed(svc, {"chat_id": CHAT, "id": 1}, "send")   # 표가 없어도 예외 없음


@test
async def budget_sub_rights_digest_state_items_without_amounts():
    db, svc, bot = await world(billing=True)
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    await db.bump(day, CHAT, ROOM_TOKENS, ROOM_TOKENS_MAX * 85 // 100)
    await svc.billing.ensure_trial(CHAT, A_ADMIN)
    until = int(time.time()) + 2 * 86400
    await db._write("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (until, CHAT))
    svc.perms.no_rights.add(CHAT)
    await db._write("INSERT INTO digest_log(user_id, day, rooms, status, ts) VALUES(?,?,?,?,?)",
                    (A_ADMIN, day, "", "forbidden", int(time.time())))
    kinds = {i.kind: i for i in await opsdesk.room_items(svc, bot, CHAT, OWNER)}
    assert set(kinds) == {"budget", "sub", "rights", "digest"}, kinds
    assert "85%" in kinds["budget"].text and kinds["sub"].key == f"u{until}" and "2일" in kinds["sub"].text
    assert "등록한 에이" in kinds["digest"].text
    q = await press(svc, bot, A_ADMIN, f"m:ibr:{CHAT}")
    t = text_of(q)
    for bad in ("$", "USDT", "요금"):                                   # 금액·요금 없음
        assert bad not in t, (bad, t)
    assert f"m:sub:{CHAT}" in cbs(q) and f"m:alg:{CHAT}" in cbs(q)
    # 체험 끝난 지 7일 넘으면 안 보임, 7일 안이면 '끝났어요'
    await db._write("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (int(time.time()) - 86400, CHAT))
    assert "끝났어요" in [i for i in await opsdesk.room_items(svc, bot, CHAT, OWNER) if i.kind == "sub"][0].text
    await db._write("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (int(time.time()) - 9 * 86400, CHAT))
    assert not [i for i in await opsdesk.room_items(svc, bot, CHAT, OWNER) if i.kind == "sub"]


# ── 📥 화면 · 권한 · 숨기기 ───────────────────────────────
@test
async def inbox_screens_permissions_across_roles():
    db, svc, bot = await world()
    await scam(db, CHAT, ago=30)
    await scam(db, OTHER, name="비방 사기", ago=20)
    q = await press(svc, bot, A_ADMIN, "m:ib")                         # 1:1 전체 = 내가 TG 관리자인 방만
    assert "&lt;b&gt;사기&lt;/b&gt; &amp; 꾼" in text_of(q) and "비방 사기" not in text_of(q)
    assert "&lt;b&gt;A&lt;/b&gt; &amp; 방" in text_of(q)
    q = await press(svc, bot, OWNER, "m:ib")                           # 오너 = 모든 방
    assert "비방 사기" in text_of(q) and "사기&lt;/b&gt;" in text_of(q)
    for uid in (MEMBER, BOTADM):                                       # 멤버·봇관리자는 빈 화면
        q = await press(svc, bot, uid, "m:ib")
        assert "텔레그램 관리자인 방이 없어요" in text_of(q) and "사기" not in text_of(q)
    for uid in (MEMBER, BOTADM, B_ADMIN):                              # A 방 화면은 A 방 TG 관리자·오너만
        assert denied(await press(svc, bot, uid, f"m:ibr:{CHAT}")), uid
    assert "사기" in text_of(await press(svc, bot, A_ADMIN, f"m:ibr:{CHAT}"))
    q = await press(svc, bot, A_ADMIN, f"m:g:{CHAT}")                  # 허브에 📥 (TG 관리자) / 봇관리자 허브엔 없음
    assert f"m:ibr:{CHAT}" in cbs(q) and f"m:fcr:{CHAT}" in cbs(q)
    q = await press(svc, bot, BOTADM, f"m:g:{CHAT}")
    assert f"m:ibr:{CHAT}" not in cbs(q) and f"m:fcr:{CHAT}" in cbs(q)
    q = await press(svc, bot, MEMBER, "m:home")
    assert "m:ib" in cbs(q) and "m:opc" not in cbs(q) and "m:opf" not in cbs(q)
    q = await press(svc, bot, OWNER, "m:home")
    assert {"m:ib", "m:opc", "m:opf"} <= set(cbs(q))


@test
async def hide_once_forged_foreign_and_double_press():
    db, svc, bot = await world()
    s1 = await scam(db, CHAT, ago=30)
    s_other = await scam(db, OTHER, ago=20)
    q = await press(svc, bot, A_ADMIN, f"m:ibr:{CHAT}")
    hide = f"m:ibh:{CHAT}:s{s1}:r:0"
    assert hide in cbs(q)
    for uid in (MEMBER, BOTADM, B_ADMIN):                              # 남의 방·권한 없는 사람
        assert denied(await press(svc, bot, uid, hide)), uid
    assert denied(await press(svc, bot, A_ADMIN, f"m:ibh:{OTHER}:s{s_other}:a:0"))   # 다른 방 cid 로 바꿔 누름
    q = await press(svc, bot, A_ADMIN, f"m:ibh:{CHAT}:s{s1}:z:0")    # 모르는 돌아갈 화면 → 거절
    assert denied(q) and "만료된 버튼" in q.answers[0][0]
    assert not await db._all("SELECT * FROM ops_hidden")
    q = await press(svc, bot, A_ADMIN, hide)
    assert q.answers[0][0] == "✓ 숨겼어요." and "처리할 일이 없어요" in text_of(q)
    q = await press(svc, bot, A_ADMIN, hide)                           # 두 번 눌림 (옛 화면의 같은 버튼)
    assert q.answers[0][0] == "이미 숨긴 항목이에요." and q.edits
    assert len(await db._all("SELECT * FROM ops_hidden")) == 1
    for forged in (f"m:ibh:{CHAT}:s999:r:0", f"m:ibh:{CHAT}:s{s_other}:r:0", f"m:ibh:{CHAT}:x:r:0",
                   f"m:ibh:{CHAT}:s{s1}:z:0", f"m:ibh:{CHAT}:S1:r:0", f"m:ibh:{CHAT}:s²:r:0", f"m:ibh:{CHAT}"):
        q = await press(svc, bot, A_ADMIN, forged)
        assert q.answers[0][0] and not q.answers[0][0].startswith("✓"), forged
    assert len(await db._all("SELECT * FROM ops_hidden")) == 1          # 위조 키로는 아무것도 안 씀
    # 숨김은 사람마다: 오너에겐 그대로
    assert "사기&lt;/b&gt;" in text_of(await press(svc, bot, OWNER, f"m:ibr:{CHAT}"))
    # 새 일은 다시 보임
    s2 = await scam(db, CHAT, name="새 사기", ago=5)
    q = await press(svc, bot, A_ADMIN, "m:ib")
    assert "새 사기" in text_of(q) and f"m:ibh:{CHAT}:s{s2}:a:0" in cbs(q)


@test
async def new_failure_of_hidden_schedule_reappears():
    db, svc, bot = await world()
    row = {"chat_id": CHAT, "id": 77}
    await opsdesk.schedule_failed(svc, row, "send")
    [it] = await opsdesk.inbox(svc, bot, A_ADMIN, [(CHAT, TITLE)])
    await press(svc, bot, A_ADMIN, f"m:ibh:{CHAT}:{it.key}:a:0")
    assert not await opsdesk.inbox(svc, bot, A_ADMIN, [(CHAT, TITLE)])
    await opsdesk.schedule_failed(svc, row, "send")
    [again] = await opsdesk.inbox(svc, bot, A_ADMIN, [(CHAT, TITLE)])
    assert again.key != it.key and "2번" in again.text


@test
async def long_inbox_pages_under_limits():
    db, svc, bot = await world()
    for i in range(40):
        await scam(db, CHAT, name=("<b>&" * 20) + str(i), ago=i + 1)
    q = await press(svc, bot, A_ADMIN, "m:ib")
    t = text_of(q)
    assert "(1/7쪽 · 40개)" in t and "m:ib:1" in cbs(q) and len(t) < 4096
    q = await press(svc, bot, A_ADMIN, "m:ib:6")
    assert "(7/7쪽" in text_of(q) and "m:ib:5" in cbs(q) and "m:ib:7" not in cbs(q)
    q = await press(svc, bot, A_ADMIN, "m:ib:999")                     # 너무 큰 쪽 → 마지막 쪽
    assert "(7/7쪽" in text_of(q)
    q = await press(svc, bot, A_ADMIN, "m:ib:-3")
    assert "(1/7쪽" in text_of(q)
    hide = next(c for c in cbs(await press(svc, bot, A_ADMIN, "m:ib:3")) if c.startswith("m:ibh:"))
    q = await press(svc, bot, A_ADMIN, hide)                           # 숨긴 뒤 같은 쪽으로
    assert "(4/7쪽 · 39개)" in text_of(q)


@test
async def empty_room_inbox():
    db, svc, bot = await world()
    q = await press(svc, bot, A_ADMIN, f"m:ibr:{CHAT}")
    assert "처리할 일이 없어요" in text_of(q) and f"m:g:{CHAT}" in cbs(q)


# ── 🧭 운영센터 ──────────────────────────────────────────
@test
async def command_center_classifies_and_filters():
    db, svc, bot = await world()
    now = int(time.time())
    third = -1001000000033
    await db.ensure_chat(third, "C 방 <조용>")
    await anomaly_alert(db, CHAT, status="ignored", ago=10)             # 오늘 이상징후 (처리됨) → 보안 이벤트만
    await db.log_mod(OTHER, None, 5, "ban", "CAS 스팸 명단")
    await db.log_mod(OTHER, None, 6, "ban", "관리자 판단")               # CAS 아님
    await scam(db, OTHER, ago=9 * 86400)                               # 오래된 → 확인 필요 아님
    svc.perms.no_rights.add(third)
    statuses, tot = await opsdesk.command_center(svc, bot, OWNER, now)
    by = {s.chat_id: s for s in statuses}
    assert by[CHAT].security == ["이상징후 알림 1번"] and not by[CHAT].attention and by[CHAT].label == "🚨 보안 이벤트"
    assert by[OTHER].security == ["CAS 스팸 계정 차단 1명"], by[OTHER].security
    assert by[third].attention and "봇에게 관리 권한" in by[third].attention[0] and not by[third].security
    assert tot["rooms"] == 3 and not tot["billing"]
    assert [s.chat_id for s in opsdesk.narrow(statuses, "attention")] == [third]
    assert {s.chat_id for s in opsdesk.narrow(statuses, "security")} == {CHAT, OTHER}
    # 버튼: 필터·방 상세·권한
    q = await press(svc, bot, OWNER, "m:opc:n:0")
    assert "C 방 &lt;조용&gt;" in text_of(q) and "B 방" not in text_of(q) and f"m:opcr:{third}:n:0" in cbs(q)
    q = await press(svc, bot, OWNER, "m:opc:s")
    assert "B 방" in text_of(q) and "C 방" not in text_of(q)
    q = await press(svc, bot, OWNER, "m:opc:zz:abc")                   # 모르는 필터·쪽 → 전체 첫 쪽
    assert "● 전체 3" in json.dumps([b.text for row in q.edits[-1][1].inline_keyboard for b in row], ensure_ascii=False)
    q = await press(svc, bot, OWNER, f"m:opcr:{third}:n:0")
    assert "봇에게 관리 권한" in text_of(q) and "m:opc:n:0" in cbs(q) and f"m:ibr:{third}" in cbs(q)
    for bad in ("m:opcr:-1009999999999:a:0", "m:opcr:x:a:0", "m:opcr"):
        assert denied(await press(svc, bot, OWNER, bad)), bad
    for uid in (A_ADMIN, B_ADMIN, BOTADM, MEMBER):
        for data in ("m:opc", "m:opc:n:0", f"m:opcr:{CHAT}:a:0", "m:opf"):
            assert denied(await press(svc, bot, uid, data)), (uid, data)


@test
async def command_center_reads_subscriptions_without_starting_trials():
    db, svc, bot = await world(billing=True)
    now = int(time.time())
    await svc.billing.ensure_trial(CHAT, A_ADMIN)
    await db.set_paid_until(CHAT, now + 20 * 86400)
    statuses, tot = await opsdesk.command_center(svc, bot, OWNER, now)
    assert (tot["paid"], tot["trial"], tot["billing"]) == (1, 0, True)
    assert {s.chat_id: s.state for s in statuses} == {CHAT: "paid", OTHER: ""}
    assert not await db.get_subscription(OTHER)                         # 보기만 했는데 체험이 시작되면 안 됨
    await opsdesk.inbox(svc, bot, OWNER, [(OTHER, "B 방")])
    assert not await db.get_subscription(OTHER)
    q = await press(svc, bot, OWNER, "m:opc")
    assert "구독 1 · 체험 0" in text_of(q)


@test
async def command_center_spikes_and_many_rooms_paged():
    db, svc, bot = await world()
    now = int(time.time())
    for i in range(40):                                                # 오늘 메시지 40개, 지난 7일 0 → 급증
        await db._write("INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot) VALUES(?,?,?,?,?,0)",
                        (CHAT, 900 + i % 5, i, "hi", now - 100 - i))
    s = await opsdesk.room_status(svc, bot, CHAT, TITLE, OWNER, now)
    assert any(x.startswith("메시지 급증: 24시간 40개") for x in s.attention), s.attention
    for i in range(25):
        await db.ensure_chat(-1002000000000 - i, f"방 {i:02d} <&>")
    q = await press(svc, bot, OWNER, "m:opc")
    assert "(1/4쪽)" in text_of(q) and "m:opc:a:1" in cbs(q) and len(text_of(q)) < 4096
    q = await press(svc, bot, OWNER, "m:opc:a:3")
    assert "(4/4쪽)" in text_of(q)


@test
async def command_center_empty():
    db = await make_db()
    svc = await make_svc(db)
    svc.perms = Perms()
    q = await press(svc, FakeBot(), OWNER, "m:opc")
    assert "봇이 들어간 그룹이 아직 없어요" in text_of(q)


# ── 💸 비용 예측 ─────────────────────────────────────────
def _ts(svc, y, m, d, h=12) -> int:
    return int(datetime(y, m, d, h, tzinfo=svc.cfg.tz).timestamp())


@test
async def forecast_exact_days_average_of_last_seven_recorded_and_projection():
    db, svc, bot = await world()
    now = _ts(svc, 2026, 10, 10)
    for d in range(1, 10):                                             # 10/1~10/9: d 달러
        await db.bump(f"2026-10-{d:02d}", 0, costs.USD, d * 1_000_000)
    await db.bump("2026-10-10", 0, costs.USD, 500_000)                 # 오늘 지금까지
    await db.bump("2026-09-29", 0, costs.USD, 100 * 1_000_000)         # 지난달 (평균엔 안 들어감: 최근 7일이 이미 있음)
    f = await opsdesk.forecast(svc, now)
    assert f.mtd == (45_500_000,) * 2 and f.today == (500_000,) * 2
    assert f.avg_days == 7 and f.avg == ((3 + 4 + 5 + 6 + 7 + 8 + 9) * 1_000_000 // 7,) * 2   # 10/3~10/9
    assert f.projected[0] == 45_500_000 + f.avg[0] * 21 and f.days_in_month == 31
    assert f.budget_month == int(costs.DEFAULT_USD_BUDGET * costs.MICRO) * 31 and f.range_days == 0
    lines = "\n".join(opsdesk.forecast_lines(f))
    assert "$45.50" in lines and "남은 21일" in lines and "범위" not in lines


@test
async def forecast_before_usd_counters_is_explicit_range():
    db, svc, bot = await world()
    now = _ts(svc, 2026, 9, 30)
    await db.bump("2026-09-27", 0, "tokens", 1_100_000)                 # 달러 기록 전 → 토큰 범위
    await db.bump("2026-09-27", 0, "prompt_tokens", 1_000_000)
    await db.bump("2026-09-27", 0, "cached_tokens", 600_000)
    await db.bump("2026-09-29", 0, costs.USD, 2_000_000)
    f = await opsdesk.forecast(svc, now)
    lo, hi = costs.total_range(1_000_000, 600_000, 1_100_000, svc.cfg.model, svc.cfg.guard_model)
    assert lo < hi and f.range_days == 1
    assert f.mtd == (round(lo * costs.MICRO) + 2_000_000, round(hi * costs.MICRO) + 2_000_000), f.mtd
    lines = "\n".join(opsdesk.forecast_lines(f))
    assert "~" in lines and opsdesk.USD_SINCE in lines and "범위" in lines
    # 2026-09-28 이후 날은 토큰만 있어도 범위로 추정하지 않음 (달러 기록이 정확한 값)
    await db.bump("2026-09-30", 0, "tokens", 5_000_000)
    f2 = await opsdesk.forecast(svc, now)
    assert f2.range_days == 1 and f2.today == (0, 0)


@test
async def forecast_no_history_uses_today():
    db, svc, bot = await world()
    now = _ts(svc, 2026, 11, 2)
    await db.bump("2026-11-02", 0, costs.USD, 1_000_000)
    f = await opsdesk.forecast(svc, now)
    assert f.avg_days == 0 and f.avg == (1_000_000,) * 2 and f.projected[0] == 1_000_000 * 29
    assert "오늘 쓴 만큼" in "\n".join(opsdesk.forecast_lines(f))


@test
async def room_forecast_percent_only_for_room_admins():
    db, svc, bot = await world()
    now = int(time.time())
    local = datetime.fromtimestamp(now, svc.cfg.tz)
    today = local.strftime("%Y-%m-%d")
    await db.bump(today, CHAT, costs.ROOM_USD, 3_000_000)
    await db.bump(today, OTHER, costs.ROOM_USD, 1_000_000)
    await db.bump(today, 0, costs.USD, 4_000_000)
    [rf] = await opsdesk.room_forecasts(svc, now, only=CHAT)
    dim = (local.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    assert rf.cap_month == costs.DEFAULT_PLAN_CENTS * 10_000 * dim.day and rf.mtd == 3_000_000
    for uid in (A_ADMIN, BOTADM, OWNER):                               # 방 화면 = % 만 (오너도 이 화면에선)
        q = await press(svc, bot, uid, f"m:fcr:{CHAT}")
        assert "%" in text_of(q) and "$" not in text_of(q), text_of(q)
    for uid in (MEMBER, B_ADMIN):
        assert denied(await press(svc, bot, uid, f"m:fcr:{CHAT}"))
    q = await press(svc, bot, OWNER, "m:opf")                          # 오너 화면 = 금액 + 방별 상위
    assert "$" in text_of(q) and "&lt;b&gt;A&lt;/b&gt; &amp; 방" in text_of(q) and f"m:alq:{CHAT}" in cbs(q)
    assert text_of(q).index("A&lt;/b&gt;") < text_of(q).index("B 방")    # 많이 쓸 곳 순


@test
async def forecast_screens_empty():
    db, svc, bot = await world()
    q = await press(svc, bot, OWNER, "m:opf")
    assert "아직 AI 요금 기록이 없어요" in text_of(q)
    q = await press(svc, bot, A_ADMIN, f"m:fcr:{CHAT}")
    assert "<b>0%</b>" in text_of(q) and "지난 기록이 없어서" in text_of(q)


# ── AI 도구 ──────────────────────────────────────────────
@test
async def tools_registered_where_and_read_only():
    names = {"ops_inbox", "owner_command_center", "cost_forecast"}
    assert names <= tools.READ_ONLY
    by = {t.name: t for t in tools.TOOLS}
    assert by["owner_command_center"].where == "owner_dm" and by["owner_command_center"].min_role == Role.OWNER
    member_dm = {t.name for t in tools.available(Role.MEMBER, {}, True)}
    assert {"ops_inbox", "cost_forecast"} <= member_dm and "owner_command_center" not in member_dm
    assert "owner_command_center" in {t.name for t in tools.available(Role.OWNER, {}, True)}
    assert "owner_command_center" not in {t.name for t in tools.available(Role.OWNER, {}, False)}


@test
async def ops_inbox_tool_by_role_and_place():
    db, svc, bot = await world()
    await scam(db, CHAT, name="에이방사기", ago=30)
    await scam(db, OTHER, name="비방사기", ago=20)
    for uid, role in ((MEMBER, Role.MEMBER), (BOTADM, Role.ADMIN)):   # 방에서: 멤버·봇관리자 거절
        out = await tools.execute("ops_inbox", "{}", ctx_for(svc, bot, CHAT, uid, role))
        assert "텔레그램 관리자만" in out and "사기" not in out, out
    c = ctx_for(svc, bot, CHAT, A_ADMIN, Role.ADMIN)
    out = await tools.execute("ops_inbox", '{"room": "B 방"}', c)     # 방에선 room 무시 → 그 방만
    assert "에이방사기" in out and "비방사기" not in out and c.tainted
    c = ctx_for(svc, bot, A_ADMIN, A_ADMIN, Role.MEMBER)               # 1:1: 내가 TG 관리자인 방만
    out = await tools.execute("ops_inbox", "{}", c)
    assert "에이방사기" in out and "비방사기" not in out and "$" not in out
    out = await tools.execute("ops_inbox", "{}", ctx_for(svc, bot, BOTADM, BOTADM, Role.MEMBER))
    assert "관리자인 방이 없음" in out
    out = await tools.execute("ops_inbox", '{"room": "B 방"}', ctx_for(svc, bot, OWNER, OWNER, Role.OWNER))
    assert "비방사기" in out and "에이방사기" not in out
    out = await tools.execute("ops_inbox", '{"room": "방"}', ctx_for(svc, bot, OWNER, OWNER, Role.OWNER))
    assert "여러 개" in out
    await press(svc, bot, A_ADMIN, f"m:ibh:{CHAT}:" + next(
        i.key for i in await opsdesk.room_items(svc, bot, CHAT, A_ADMIN)) + ":a:0")
    out = await tools.execute("ops_inbox", "{}", ctx_for(svc, bot, A_ADMIN, A_ADMIN, Role.MEMBER))
    assert "처리할 일 없음" in out                                     # 숨긴 건 AI 도 안 봄


@test
async def owner_center_tool_filters():
    db, svc, bot = await world()
    svc.perms.no_rights.add(OTHER)
    c = ctx_for(svc, bot, OWNER, OWNER, Role.OWNER)
    out = await tools.execute("owner_command_center", '{"filter": "attention"}', c)
    assert "B 방" in out and "<b>A</b>" not in out and "보기: 확인 필요 1방" in out and c.tainted
    out = await tools.execute("owner_command_center", '{"filter": "all"}', c)
    assert "전체 2방" in out and "✅ 정상" in out and "오늘 AI 요금 $0.00" in out
    out = await tools.execute("owner_command_center", '{"filter": "B 방"}', c)
    assert "B 방" in out and "보기: B 방 1방" in out
    out = await tools.execute("owner_command_center", '{"filter": "없는방"}', c)
    assert "못 찾음" in out
    out = await tools.execute("owner_command_center", "{}", ctx_for(svc, bot, A_ADMIN, A_ADMIN, Role.MEMBER))
    assert "사용할 수 없음" in out


@test
async def cost_tool_amounts_only_for_owner_in_dm():
    db, svc, bot = await world()
    today = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    await db.bump(today, CHAT, costs.ROOM_USD, 3_000_000)
    await db.bump(today, 0, costs.USD, 3_000_000)
    out = await tools.execute("cost_forecast", "{}", ctx_for(svc, bot, OWNER, OWNER, Role.OWNER))
    assert "$3.00" in out and "<b>A</b> & 방" in out
    out = await tools.execute("cost_forecast", '{"room": "A"}', ctx_for(svc, bot, OWNER, OWNER, Role.OWNER))
    assert "$3.00" in out and "지금까지" in out
    for ctx in (ctx_for(svc, bot, CHAT, OWNER, Role.OWNER),            # 방에선 오너도 % (멤버가 봄)
                ctx_for(svc, bot, CHAT, A_ADMIN, Role.ADMIN),
                ctx_for(svc, bot, A_ADMIN, A_ADMIN, Role.MEMBER)):     # 방 관리자 1:1
        out = await tools.execute("cost_forecast", "{}", ctx)
        assert "%" in out and "$" not in out, out
    out = await tools.execute("cost_forecast", "{}", ctx_for(svc, bot, CHAT, MEMBER, Role.MEMBER))
    assert "방 관리자만" in out and "%" not in out
    out = await tools.execute("cost_forecast", "{}", ctx_for(svc, bot, MEMBER, MEMBER, Role.MEMBER))
    assert "관리 중인 방이 없음" in out


# ── 등록 · 하네스 ─────────────────────────────────────────
@test
async def routes_do_not_clobber_other_panels():
    from sodam.panels import owner
    assert menu.ROUTES["oc"].handler.__module__ == owner.__name__      # 오너 메뉴의 m:oc(방 상세)는 그대로
    for code in ("ib", "ibr", "ibh", "opc", "opcr", "opf", "fcr"):
        assert menu.ROUTES[code].handler.__module__ == "sodam.panels.opsdesk", code
    big = f"m:ibh:-1009999999999999999:k{2 ** 63 - 1}:a:999"
    assert len(big.encode()) <= 64 and opsdesk.KEY_RE.fullmatch(f"k{2 ** 63 - 1}")


@test
async def harness_reaches_all_new_screens():
    import harness
    rep, db, svc = await harness.crawl()
    assert not rep.issues, rep.issues[:5]
    datas = {s.data.split(":")[1] for s in rep.shots}
    assert {"ib", "ibr", "ibh", "opc", "opcr", "opf", "fcr"} <= datas, datas
    for s in rep.shots:
        if s.persona != harness.OWNER and s.data.split(":")[1] in ("ib", "ibr", "ibh", "fcr"):
            assert "$" not in (s.text or "") and "USDT" not in (s.text or ""), (s.persona, s.data)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
