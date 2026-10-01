"""🚨 사건 묶기 (sodam/incidents.py): python tests/run_all.py incidents

본코드 경로로 돈다: 입장(handlers.handle_new_member) → 캡차 → 시간 초과(handlers.job_tick → captcha.expire → 보고) ·
틱(job_tick → hooks.tick → incidents.tick) · 스팸 방패·사기 의심 검사(handlers.on_group_message → 훅).
가짜 텔레그램은 고친 메시지 ID 까지 기록하고, 원하면 고치기를 실패시킨다.
"""
import asyncio
import json
import time
from types import SimpleNamespace

from fake_llm import ChatBot, Room
from fakes import FakeJobQueue, fake_user, make_svc, runner
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from sodam import handlers, incidents
from sodam.db import DB
from sodam.util import RateLimiter

test, run_all = runner()
ADMIN, WEAK, OWNER, NEW = 5, 6, 7, 30
CHAT = Room.CHAT
WALLET = "TQrZ9wBzVh9Yr4Uc3r1jWZgLvX5mRj8kAb"


class Bot(ChatBot):
    """edit_message_text 가 메시지 ID 를 남김 · fail_edit 에 있는 ID 는 '고칠 메시지 없음'."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fail_edit: set[int] = set()

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        if message_id in self.fail_edit:
            raise BadRequest("Message to edit not found")
        if chat_id in getattr(self, "dm_blocked", ()):
            raise Forbidden("Forbidden: bot was blocked by the user")
        self.calls.append(("edit_text", chat_id, text, {**kw, "message_id": message_id}))


class Clock:
    """incidents._now 대신: 진짜 시각 + 테스트가 더한 만큼."""
    def __init__(self):
        self.shift = 0.0

    @property
    def t(self):
        return time.time() + self.shift

    @t.setter
    def t(self, value):
        self.shift = value - time.time()

    def __call__(self):
        return self.t


def ctx_for(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "limiter": RateLimiter(), "chats": set(), "cas_seen": set(),
                                     "tasks": set(), "joins": {}})


async def world(**settings):
    r = await Room().open(admins=(ADMIN, WEAK), settings={"captcha_enabled": True, "greet_enabled": False,
                                                          "raid_guard": False, "anomaly_mode": "off", **settings})
    r.bot = Bot(admins=[fake_user(ADMIN, "방장"), fake_user(WEAK, "부방장")])
    r.ctx = ctx_for(r.svc, r.bot)
    r.svc.perms.owner_ids = {OWNER}
    r.clock = Clock()
    incidents._now = r.clock
    return r


def sends(r, uid):
    return [c for c in r.bot.named("send_message") if c[1] == uid]


def edits(r, uid):
    return [c for c in r.bot.named("edit_text") if c[1] == uid]


async def captcha_fail(r, uid, ctx=None):
    """입장 → 캡차 → 시간 초과 (job_tick 이 처리하고 오너에게 보고)."""
    ctx = ctx or r.ctx
    svc = ctx.bot_data["svc"]
    await handlers.handle_new_member(ctx, CHAT, "방", fake_user(uid, f"입장{uid}"))
    assert await svc.captcha.pending(CHAT, uid)
    await svc.db._write("UPDATE captcha SET expires_at=0 WHERE chat_id=? AND user_id=?", (CHAT, uid))
    await handlers.job_tick(ctx)


async def row(db):
    return await db._one("SELECT * FROM incidents ORDER BY id DESC LIMIT 1")


@test
async def report_names_rooms_and_people_and_escapes():
    r = await world()
    await r.db.ensure_chat(-100555, "백악관 <since>")
    await r.db.upsert_user(fake_user(8591039149, "팬<b>텀", "ss5011004"), commit=True)
    out = await r.svc.mod.named("[캡차] chat -100555 / user 8591039149: 시간 초과 · chat -1009 / user 42")
    assert out == ("[캡차] 백악관 &lt;since&gt;(-100555) / 팬&lt;b&gt;텀 @ss5011004(8591039149): 시간 초과 · "
                   "chat -1009 / user 42"), out                          # 모르는 번호는 그대로
    await r.svc.mod.report(r.bot, "[인젝션 차단] chat -100555 / x")
    assert "백악관 &lt;since&gt;(-100555)" in sends(r, OWNER)[-1][2]


# ── 캡차 실패 몰림 ────────────────────────────────────────
@test
async def five_captcha_failures_one_send_four_edits_and_states():
    r = await world()
    for i in range(5):
        await captcha_fail(r, 100 + i)
        r.clock.t += 30
    [first] = sends(r, OWNER)
    assert "🚨 감지" in first[2] and "캡차 실패" in first[2] and "입장100(100)" in first[2], first[2]
    title = (await r.db._one("SELECT title FROM chats WHERE chat_id=?", (CHAT,)))["title"]
    assert f"{title}({CHAT})" in first[2] and f"chat {CHAT}" not in first[2], first[2]   # 어느 방인지 이름으로
    ed = edits(r, OWNER)
    assert len(ed) == 4, len(ed)
    mid = (await row(r.db))["msgs_json"]
    assert {e[3]["message_id"] for e in ed} == {json.loads(mid)[str(OWNER)]}, "같은 메시지를 고침"
    assert "🚨 감지" in ed[0][2] and "+1건" in ed[0][2], ed[0][2]                       # 2건: 아직 감지
    assert "🔴 지속" in ed[1][2] and "+2건" in ed[1][2], ed[1][2]                       # 3건 → 지속
    assert "+4건 · 마지막" in ed[-1][2] and "입장104(104)" in ed[-1][2] and "입장100(100)" in ed[-1][2], "최신 + 이전 한 줄"
    assert not [c for c in r.bot.named("send_message") if c[1] in (ADMIN, WEAK)], "보고는 오너(·로그방)만"
    # 조용해지면 틱이 🟢 로 한 번 고치고 닫음
    r.clock.t += incidents.WINDOW - 1 - 30              # 마지막 사건 뒤 WINDOW-1초
    await handlers.job_tick(r.ctx)
    assert len(edits(r, OWNER)) == 4, "창 안이면 아직"
    r.clock.t += 2
    await handlers.job_tick(r.ctx)
    await handlers.job_tick(r.ctx)
    ed = edits(r, OWNER)
    assert len(ed) == 5 and "🟢 잠잠해짐" in ed[-1][2] and "+4건" in ed[-1][2], ed[-1][2]
    assert (await row(r.db))["state"] == "calm"
    # 닫힌 뒤 새 실패 = 새 메시지
    await captcha_fail(r, 200)
    assert len(sends(r, OWNER)) == 2 and "🚨 감지" in sends(r, OWNER)[-1][2] and "+" not in sends(r, OWNER)[-1][2].split("\n")[0]


@test
async def window_slides_and_older_than_five_minutes_is_ongoing():
    r = await world()
    await captcha_fail(r, 100)
    r.clock.t += incidents.ONGOING_AFTER + 1
    await captcha_fail(r, 101)                          # 2건이지만 5분 넘음 → 지속
    assert "🔴 지속" in edits(r, OWNER)[-1][2]
    r.clock.t += incidents.WINDOW - 10                  # 처음부터 10분이 넘었어도 마지막 뒤 10분 안 → 같은 사건
    await captcha_fail(r, 102)
    assert len(sends(r, OWNER)) == 1 and "+2건" in edits(r, OWNER)[-1][2]
    r.clock.t += incidents.WINDOW + 1                   # 틱 없이 창이 지남(재시작 등) → 새 사건
    await captcha_fail(r, 103)
    assert len(sends(r, OWNER)) == 2
    assert len(await r.db._all("SELECT id FROM incidents")) == 2


@test
async def restart_mid_incident_keeps_editing_same_message():
    r = await world()
    await captcha_fail(r, 100)
    await captcha_fail(r, 101)
    path, bot = r.db.path, r.bot
    await r.db.close()
    db2 = DB(path)                                       # 재시작: 새 서비스, 같은 DB
    await db2.open()
    try:
        svc2 = await make_svc(db2, admins=(ADMIN, WEAK))
        svc2.perms.owner_ids = {OWNER}
        ctx2 = ctx_for(svc2, bot)
        await captcha_fail(r, 102, ctx2)
        assert len(sends(r, OWNER)) == 1, "재시작 뒤에도 새로 안 보냄"
        [mid] = {e[3]["message_id"] for e in edits(r, OWNER)}
        assert mid == json.loads((await row(db2))["msgs_json"])[str(OWNER)] and "+2건" in edits(r, OWNER)[-1][2]
        r.clock.t += incidents.WINDOW + 1
        await handlers.job_tick(ctx2)
        assert "🟢 잠잠해짐" in edits(r, OWNER)[-1][2] and edits(r, OWNER)[-1][3]["message_id"] == mid
    finally:
        await db2.close()


@test
async def edit_failure_sends_new_and_replaces_id():
    r = await world()
    await captcha_fail(r, 100)
    old = json.loads((await row(r.db))["msgs_json"])[str(OWNER)]
    r.bot.fail_edit.add(old)                             # 오너가 알림을 지웠음 등
    await captcha_fail(r, 101)
    assert len(sends(r, OWNER)) == 2 and "+1건" in sends(r, OWNER)[-1][2]
    new = json.loads((await row(r.db))["msgs_json"])[str(OWNER)]
    assert new != old
    await captcha_fail(r, 102)
    assert len(sends(r, OWNER)) == 2 and edits(r, OWNER)[-1][3]["message_id"] == new
    # 1:1 을 막은 사람은 빼 두고, 틱의 🟢 고치기는 새로 보내지 않음
    r.bot.dm_blocked = {OWNER}
    await captcha_fail(r, 103)
    assert str(OWNER) not in json.loads((await row(r.db))["msgs_json"])
    r.clock.t += incidents.WINDOW + 1
    await handlers.job_tick(r.ctx)
    assert len(sends(r, OWNER)) == 2


@test
async def concurrent_events_open_once():
    r = await world()
    await asyncio.gather(*[incidents.open_or_bump(r.svc, r.bot, CHAT, "captcha", "room", f"[캡차] {i}", None, [OWNER])
                           for i in range(6)])
    assert len(sends(r, OWNER)) == 1, "동시에 와도 새 메시지는 한 번"
    x = await row(r.db)
    assert x["count"] == 6 and x["sending"] == 0
    assert "+5건" in (edits(r, OWNER) or sends(r, OWNER))[-1][2], "여는 쪽이 늦게 온 것까지 따라잡음"


@test
async def per_person_buttons_are_kept_and_labeled():
    r = await world()
    kb = lambda uid, a: InlineKeyboardMarkup([[InlineKeyboardButton(a, callback_data=f"ow:{CHAT}:{uid}:u")]])  # noqa: E731
    await incidents.open_or_bump(r.svc, r.bot, CHAT, "flood", "room", "도배 1", kb(40, "🔊 풀기"), [OWNER],
                                 sub=40, label="도배맨<1>")
    await incidents.open_or_bump(r.svc, r.bot, CHAT, "flood", "room", "도배 2", kb(41, "🔊 풀기"), [OWNER],
                                 sub=41, label="둘째")
    await incidents.open_or_bump(r.svc, r.bot, CHAT, "flood", "room", "도배 1 또", kb(40, "🔊 다시 풀기"), [OWNER],
                                 sub=40, label="도배맨<1>")
    markup = edits(r, OWNER)[-1][3]["reply_markup"]
    btns = [(b.text, b.callback_data) for row_ in markup.inline_keyboard for b in row_]
    assert btns == [("둘째 · 🔊 풀기", f"ow:{CHAT}:41:u"), ("도배맨<1> · 🔊 다시 풀기", f"ow:{CHAT}:40:u")], btns
    await incidents.open_or_bump(r.svc, r.bot, CHAT, "flood", "room", "버튼 없는 일", None, [OWNER])
    assert len(edits(r, OWNER)[-1][3]["reply_markup"].inline_keyboard) == 2, "버튼 없는 사건이 버튼을 지우지 않음"


# ── 실제 보고 경로: 도배 뮤트·사칭 ─────────────────────────
@test
async def flood_mutes_of_two_people_share_one_report_with_both_buttons():
    r = await world(flood_count=3, flood_seconds=60, captcha_enabled=False)
    for uid, name in ((40, "도배맨"), (41, "도배둘")):
        for i in range(3):
            await r.say(fake_user(uid, name), f"광고 {i}")
    [s] = sends(r, OWNER)
    assert "도배 뮤트" in s[2]
    markup = edits(r, OWNER)[-1][3]["reply_markup"]
    data = [b.callback_data for row_ in markup.inline_keyboard for b in row_]
    assert {d.split(":")[2] for d in data} == {"40", "41"} and all(len(d.encode()) <= 64 for d in data), data
    assert markup.inline_keyboard[0][0].text.startswith("도배맨 · ")


# ── 스팸 방패 + 사기 의심 검사 = 같은 사람 한 통 ───────────
@test
async def spamshield_and_scamguard_same_user_one_dm():
    r = await world(captcha_enabled=False, scam_guard=True, scam_action="ask", scam_ai=True,
                    spamshield_mode="alert", link_filter=False, newbie_link_hours=0)

    async def can(bot, cid, uid, right="restrict"):
        return uid == ADMIN
    r.svc.perms.can = can
    r.llm.json_script["scam"] = [{"scam": True, "confidence": 0.95, "reason": "리딩방 모집"}] * 3
    r.llm.json_script["spamshield"] = [{"scam_prob": 0.95, "ad_prob": 0.1, "reasons": ["리딩방"]}] * 3
    await r.join(fake_user(NEW, "신규"))
    await r.say(fake_user(NEW, "신규"), f"무료 리딩방 여기로 {WALLET}")
    assert r.llm.of("json", "scam") and r.llm.of("json", "spamshield"), "둘 다 켜져서 둘 다 판정"
    assert len(sends(r, ADMIN)) == 1, [c[2][:40] for c in sends(r, ADMIN)]
    assert len(sends(r, WEAK)) <= 1
    body = (edits(r, ADMIN) or sends(r, ADMIN))[-1]
    assert "+1건" in body[2] and "사기·스팸 의심" in body[2], body[2]
    data = [b.callback_data for row_ in body[3]["reply_markup"].inline_keyboard for b in row_]
    assert data and all(len(d.encode()) <= 64 for d in data)
    x = await row(r.db)
    assert (x["kind"], x["key"], x["count"]) == ("scam", str(NEW), 2)
    # 다른 사람은 다른 사건
    await r.join(fake_user(NEW + 1, "신규2"))
    await r.say(fake_user(NEW + 1, "신규2"), f"무료 리딩방 {WALLET}")
    assert len(sends(r, ADMIN)) == 2


@test
def restore_clock():
    incidents._now = time.time   # 다음 테스트 모듈은 진짜 시각으로


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
