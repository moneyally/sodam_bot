"""오너 보고 바로가기 버튼 · 봇 권한 없는 방 알림: python tests/run_all.py owner_ops"""
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeQuery, fake_user, runner
from telegram import ChatPermissions

from sodam import handlers

test, run_all = runner()
OWNER, ADMIN, SPAM = fake_user(7, "오너"), fake_user(1, "방장"), fake_user(40, "도배맨")


async def room():
    r = await Room().open(admins={ADMIN.id}, settings={"flood_count": 4, "flood_seconds": 60, "injection_guard": False})
    r.svc.perms.owner_ids = {OWNER.id}
    reports = []

    async def report(bot, text, kb=None):
        reports.append((text, kb))
    r.svc.mod.report = report

    async def incident(bot, chat_id, kind, text, kb=None, **kw):   # 방에서 저절로 생긴 일 = 사건 보고 (sodam/incidents.py)
        reports.append((text, kb))
    r.svc.mod.incident = incident
    return r, reports


async def press(r, user, data):
    q = FakeQuery(user.id, user, data)
    q.message.text_html = "📣 보고"
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    return q


def buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


@test
async def flood_report_has_owner_buttons_only_owner_can_press():
    r, reports = await room()
    for i in range(4):
        await r.say(SPAM, f"광고 {i}")
    [(text, kb)] = [x for x in reports if "도배 뮤트" in x[0]]
    data = buttons(kb)
    assert [d.rsplit(":", 1)[1] for d in data] == ["u", "x", "b"] and all(len(d.encode()) <= 64 for d in data)
    q = await press(r, ADMIN, data[0])                       # 방 관리자라도 오너 보고 버튼은 못 누름
    assert q.answers[-1][1] is True
    assert not [c for c in r.bot.named("restrict") if c[3] == ChatPermissions.all_permissions()]
    q = await press(r, OWNER, data[0])
    assert [c for c in r.bot.named("restrict") if c[2] == SPAM.id and c[3] == ChatPermissions.all_permissions()]
    assert "풀었어요" in q.edits[-1]
    await press(r, OWNER, data[1])                           # 1일로 연장
    assert r.bot.named("restrict")[-1][4] is not None
    await press(r, OWNER, data[2])
    assert ("ban", Room.CHAT, SPAM.id) in r.bot.calls


@test
async def cas_ban_report_has_unban_button():
    r, reports = await room()
    await r.db.set_setting(Room.CHAT, "cas_enabled", True)
    r.svc.cas.banned = {SPAM.id}
    await handlers.handle_new_member(r.ctx, Room.CHAT, "방", SPAM)
    [(_, kb)] = [x for x in reports if "[CAS]" in x[0]]
    await press(r, OWNER, buttons(kb)[0])
    assert r.bot.named("unban") and r.bot.named("unban")[-1][2] == SPAM.id


@test
async def rooms_without_bot_rights_reported_once_a_day():
    r, reports = await room()
    r.bot.can_moderate = False
    ctx = SimpleNamespace(bot=r.bot, bot_data=r.ctx.bot_data, job_queue=r.ctx.job_queue)
    await handlers.job_rights(ctx)
    await handlers.job_rights(ctx)
    alerts = [t for t, _ in reports if "관리 권한이 없는 방" in t]
    assert len(alerts) == 1 and "사용자 차단" in alerts[0], alerts
    r2, reports2 = await room()
    await handlers.job_rights(SimpleNamespace(bot=r2.bot, bot_data=r2.ctx.bot_data, job_queue=r2.ctx.job_queue))
    assert not [t for t, _ in reports2 if "관리 권한이 없는 방" in t], "권한 있으면 알림 없음"


if __name__ == "__main__":
    run_all()
