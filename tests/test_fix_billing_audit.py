"""구독·결제 흐름 감사 (대표님 입장에서 끝까지, 가짜 TronGrid·가짜 텔레그램): python tests/run_all.py test_fix_billing_audit"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_billing as TB  # noqa: E402
from fakes import FakeBot, FakeJobQueue, FakeQuery, fake_user, runner  # noqa: E402

from sodam import billing as billing_mod  # noqa: E402
from sodam import handlers, menu, subscription  # noqa: E402
from sodam.billing import LATE_GRACE  # noqa: E402

test, run_all = runner()
CHAT, OTHER = TB.CHAT, -100777
DAY = 86400


def _member(status, bot):
    return SimpleNamespace(status=status, is_member=None, user=SimpleNamespace(id=bot.id))


async def _bot_event(svc, bot, adder, old, new, chat=CHAT):
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc, "chats": set()})
    upd = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=chat, type="supergroup", title="대표님 방"), from_user=adder,
        old_chat_member=_member(old, bot), new_chat_member=_member(new, bot)))
    await handlers.on_my_chat_member(upd, ctx)


def _fixed_random(values):
    """청구서 끝자리 난수를 정해진 순서로 (같은 금액 충돌 재현)."""
    seq = iter(values)
    orig = billing_mod.secrets.randbelow
    billing_mod.secrets.randbelow = lambda n: next(seq)
    return lambda: setattr(billing_mod.secrets, "randbelow", orig)


# ── 체험: 한 번만 ─────────────────────────────────────────
@test
async def trial_starts_once_and_cannot_be_restarted():
    db, svc, _ = await TB.setup(trial_days=3)
    svc.perms.admins |= {2}
    bot = FakeBot()
    owner, admin2 = fake_user(1, "대표님"), fake_user(2, "부방장")
    await _bot_event(svc, bot, owner, "left", "member")
    sub = await db.get_subscription(CHAT)
    assert abs(sub["trial_until"] - (time.time() + 3 * DAY)) < 5 and sub["added_by"] == 1
    await db.conn.execute("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (int(time.time()) - 60, CHAT))
    await db.conn.commit()
    await _bot_event(svc, bot, owner, "member", "left")           # 내보냈다가
    await _bot_event(svc, bot, admin2, "left", "member")          # 다른 관리자가 다시 초대
    await menu.group_panel(svc, bot, CHAT, 1)                      # 설정 화면 열기
    await subscription.panel(svc, CHAT)
    after = await db.get_subscription(CHAT)
    assert after["trial_until"] < time.time() and after["added_by"] == 1
    assert (await svc.billing.status(CHAT)).state == "expired"


# ── 청구서: 연타·동시 생성 ─────────────────────────────────
@test
async def double_press_pay_new_makes_one_invoice():
    db, svc, _ = await TB.setup()
    await svc.billing.ensure_trial(CHAT, 1)
    bot, admin = FakeBot(), fake_user(1, "대표님")
    qs = [FakeQuery(1, admin), FakeQuery(1, admin)]
    await asyncio.gather(*(subscription.on_callback(svc, bot, q, ["new", str(CHAT)]) for q in qs))
    rows = await db._all("SELECT id FROM invoices WHERE chat_id=?", (CHAT,))
    assert len(rows) == 1, rows                                    # 금액이 다른 청구서 두 장이 오면 헷갈림
    assert all(len(q.answers) == 1 for q in qs)


@test
async def repress_near_expiry_gets_fresh_invoice():
    """1분 남은 청구서를 다시 보여주면 대표님이 송금하는 사이 끝나 '늦은 입금'(수동 처리)이 됨."""
    db, svc, grid = await TB.setup(invoice_minutes=60)
    old = await svc.billing.create_invoice(CHAT, 1)
    await db.conn.execute("UPDATE invoices SET expires=? WHERE id=?", (int(time.time()) + 60, old["id"]))
    await db.conn.commit()
    new = await svc.billing.create_invoice(CHAT, 1)
    assert new["id"] != old["id"] and new["expires"] - time.time() > 50 * 60
    assert new["amount_units"] != old["amount_units"]
    assert (await svc.billing.create_invoice(CHAT, 1))["id"] == new["id"]   # 넉넉하면 그대로 재사용
    grid.transfer("tx-old", old["amount_units"])                  # 옛 청구서로 보낸 돈도 그대로 인정
    paid, _ = await svc.billing.check_pending()
    assert [p["id"] for p in paid] == [old["id"]]


@test
async def simultaneous_admins_never_share_amount():
    db, svc, grid = await TB.setup()
    await db.ensure_chat(OTHER, "다른 방")
    restore = _fixed_random([7, 7, 8])
    try:
        a, b = await asyncio.gather(svc.billing.create_invoice(CHAT, 1), svc.billing.create_invoice(OTHER, 2))
    finally:
        restore()
    assert a["amount_units"] != b["amount_units"]                  # 같으면 한쪽 입금이 다른 방을 연장할 수 있음


@test
async def paid_invoice_amount_not_reused_in_window():
    """A 방 대표님이 같은 금액을 두 번 보냄 → 두 번째 입금이 같은 끝자리를 받은 B 방을 연장하면 안 됨."""
    db, svc, grid = await TB.setup()
    await db.ensure_chat(OTHER, "다른 방")
    restore = _fixed_random([7, 7, 8])
    try:
        a = await svc.billing.create_invoice(CHAT, 1)
        grid.transfer("tx-a1", a["amount_units"])
        paid, _ = await svc.billing.check_pending()
        assert [p["tx_id"] for p in paid] == ["tx-a1"]
        b = await svc.billing.create_invoice(OTHER, 2)
    finally:
        restore()
    assert b["amount_units"] != a["amount_units"]
    grid.transfer("tx-a2", a["amount_units"])                      # 실수로 한 번 더
    paid, unmatched = await svc.billing.check_pending()
    assert not paid and [u["tx_id"] for u in unmatched] == ["tx-a2"]
    assert (await db.get_invoice(b["id"]))["status"] == "pending"


# ── 입금 판정 ─────────────────────────────────────────────
@test
async def duplicate_and_reused_tx_never_double_extend():
    db, svc, grid = await TB.setup()
    await db.ensure_chat(OTHER, "다른 방")
    a = await svc.billing.create_invoice(CHAT, 1)
    grid.transfer("tx-1", a["amount_units"])
    grid.transfers.append(dict(grid.transfers[0]))                 # TronGrid 가 같은 거래를 두 번 줌
    paid, unmatched = await svc.billing.check_pending()
    assert len(paid) == 1 and not unmatched
    until = (await db.get_subscription(CHAT))["paid_until"]
    b = await svc.billing.create_invoice(OTHER, 2)
    grid.transfers = [dict(grid.transfers[0], value=str(b["amount_units"]))]   # 같은 tx 로 다른 방 청구서
    paid, unmatched = await svc.billing.check_pending()
    assert not paid and not unmatched
    assert (await db.get_subscription(CHAT))["paid_until"] == until
    assert (await db.get_invoice(b["id"]))["status"] == "pending"


@test
async def late_payment_after_invoice_expiry_goes_to_owner():
    db, svc, grid = await TB.setup(invoice_minutes=10)
    await svc.billing.ensure_trial(CHAT, 1)
    inv = await svc.billing.create_invoice(CHAT, 1)
    grid.transfer("tx-late", inv["amount_units"], ts=inv["expires"] + 5)
    paid, unmatched = await svc.billing.check_pending()
    assert not paid and [u["tx_id"] for u in unmatched] == ["tx-late"]
    assert (await svc.billing.status(CHAT)).state == "trial"      # 자동 연장 안 함 (오너가 .구독부여)


# ── 연장 계산 ─────────────────────────────────────────────
@test
async def extension_math_early_late_and_during_trial():
    db, svc, grid = await TB.setup(sub_days=30, trial_days=3)
    await svc.billing.ensure_trial(CHAT, 1)
    trial_until = (await db.get_subscription(CHAT))["trial_until"]

    async def pay(tx):
        inv = await svc.billing.create_invoice(CHAT, 1)
        grid.transfer(tx, inv["amount_units"])
        paid, _ = await svc.billing.check_pending()
        return paid[0]["until"]

    assert await pay("tx-trial") == trial_until + 30 * DAY         # 체험 중 결제 → 체험 끝 뒤로
    assert await pay("tx-early") == trial_until + 60 * DAY         # 남은 기간 뒤로
    await db.conn.execute("UPDATE subscriptions SET paid_until=?, trial_until=? WHERE chat_id=?",
                          (int(time.time()) - 5 * DAY, int(time.time()) - 40 * DAY, CHAT))
    await db.conn.commit()
    assert abs(await pay("tx-after") - (time.time() + 30 * DAY)) < 5   # 끝난 뒤 → 지금부터


# ── 기록 직후 죽음 + 오래 멈춤 ─────────────────────────────
@test
async def crash_after_record_then_long_downtime_still_extends_once():
    db, svc, grid = await TB.setup(invoice_minutes=10)
    await svc.billing.ensure_trial(CHAT, 1)
    inv = await svc.billing.create_invoice(CHAT, 1)
    grid.transfer("tx-crash", inv["amount_units"])
    real = db.pay_invoice

    async def boom(*a, **kw):
        raise RuntimeError("죽음")
    db.pay_invoice = boom
    try:
        await svc.billing.check_pending()
        raise AssertionError("예외가 나야 함")
    except RuntimeError:
        pass
    db.pay_invoice = real
    # 컨테이너 회수로 1시간 넘게 멈췄다가 새 프로세스로 시작: 청구서는 유효시간+여유가 지나 만료 처리까지 됨
    # (옛 버전은 이 상태에서 거래를 '이미 본 것'으로 건너뛰고 청구서만 만료시켜 입금이 조용히 사라졌음)
    old = int(time.time()) - 2 * 3600
    await db.conn.execute("UPDATE invoices SET created=?, expires=?, status='expired' WHERE id=?",
                          (old, old + 600, inv["id"]))
    await db.conn.commit()
    grid.transfers[0]["block_timestamp"] = (old + 60) * 1000
    fresh = billing_mod.Billing(svc.cfg, db, svc.billing.http)
    paid, unmatched = await fresh.check_pending()
    assert [p["tx_id"] for p in paid] == ["tx-crash"] and not unmatched, (paid, unmatched)
    assert paid[0]["user_id"] == 1 and paid[0]["chat_id"] == CHAT  # 결제한 대표님에게 알림 갈 수 있게
    assert (await svc.billing.status(CHAT)).state == "paid"
    until = (await db.get_subscription(CHAT))["paid_until"]
    paid, unmatched = await fresh.check_pending()
    assert not paid and not unmatched and (await db.get_subscription(CHAT))["paid_until"] == until


@test
async def cancelled_after_paying_is_reported_not_recovered():
    """입금 후 [취소] 를 누른 경우: 자동 연장은 안 하고 오너에게 한 번 보고 (기록 뒤 복구 대상 아님)."""
    db, svc, grid = await TB.setup()
    inv = await svc.billing.create_invoice(CHAT, 1)
    grid.transfer("tx-c", inv["amount_units"])
    orig = db.pay_invoice

    async def cancel_then_pay(*a, **kw):
        await db.cancel_invoice(inv["id"])
        return await orig(*a, **kw)
    db.pay_invoice = cancel_then_pay
    paid, unmatched = await svc.billing.check_pending()
    db.pay_invoice = orig
    assert not paid and [u["tx_id"] for u in unmatched] == ["tx-c"]
    paid, unmatched = await svc.billing.check_pending()
    assert not paid and not unmatched


# ── 버튼 ──────────────────────────────────────────────────
@test
async def buttons_answer_once_and_fit_64_bytes():
    db, svc, grid = await TB.setup()
    await svc.billing.ensure_trial(-1009999999999, 1)
    await db.ensure_chat(-1009999999999, "긴 ID 방")
    bot, admin, member = FakeBot(), fake_user(1, "대표님"), fake_user(20, "멤버")
    _, kb = await subscription.panel(svc, -1009999999999)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    q = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q, ["new", "-1009999999999"])
    inv = await db.open_invoice(-1009999999999, int(time.time()), 1)
    datas += [b.callback_data for r in subscription.invoice_buttons(inv["id"] * 10**9).inline_keyboard for b in r]
    assert all(len(d.encode()) <= 64 for d in datas), datas
    cases = [(member, ["new", str(CHAT)]), (admin, ["check", "999999"]), (admin, ["x", "abc"]), (admin, []),
             (admin, ["check", str(inv["id"])]), (admin, ["check", str(inv["id"])]),   # 두 번째는 15초 제한
             (member, ["cancel", str(inv["id"])]), (admin, ["cancel", str(inv["id"])]),
             (admin, ["check", str(inv["id"])])]
    for user, parts in cases:
        q = FakeQuery(user.id, user)
        await subscription.on_callback(svc, bot, q, parts)
        assert len(q.answers) == 1, (parts, q.answers)
    assert (await db.get_invoice(inv["id"]))["status"] == "cancelled"


@test
async def demoted_admin_blocked_from_new_invoice_but_payment_still_counts():
    db, svc, grid = await TB.setup()
    await svc.billing.ensure_trial(CHAT, 1)
    bot, admin = FakeBot(), fake_user(1, "대표님")
    svc.perms.owners = lambda: asyncio.sleep(0, {99})
    q = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q, ["new", str(CHAT)])
    inv = await db.open_invoice(CHAT, int(time.time()), 1)
    svc.perms.admins.discard(1)                                    # 결제 도중 관리자 해제
    q = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q, ["new", str(CHAT)])
    assert "관리자만" in q.answers[0][0]
    grid.transfer("tx-d", inv["amount_units"])                     # 이미 보낸 돈은 방에 반영
    q = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q, ["check", str(inv["id"])])
    assert (await svc.billing.status(CHAT)).state == "paid"
    room = [c[2] for c in bot.named("send_message") if c[1] == CHAT]
    assert room and not any("USDT" in t or TB.PAY in t for t in room)   # 방엔 금액·주소 없음


# ── 만료 안내 ─────────────────────────────────────────────
@test
async def trial_last_day_report_once_and_money_only_in_dm():
    db, svc, _ = await TB.setup(trial_days=3)
    await svc.billing.ensure_trial(CHAT, 1)
    await db.conn.execute("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (int(time.time()) + 5 * 3600, CHAT))
    await db.conn.commit()
    bot = FakeBot(admins=[fake_user(1, "대표님"), fake_user(2, "부방장")])
    svc.perms.admins |= {2}
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    await handlers.job_sub_reminders(ctx)
    await handlers.job_sub_reminders(ctx)                         # 같은 날 두 번 돌아도
    sends = bot.named("send_message")
    room = [c[2] for c in sends if c[1] == CHAT]
    assert room and not any("USDT" in t or TB.PAY in t for t in room)
    dms = {uid: [c for c in sends if c[1] == uid and "USDT" in c[2]] for uid in (1, 2)}
    assert all(len(v) >= 1 for v in dms.values())
    assert sum("무료 체험 동안" in c[2] for c in sends) == 2       # 리포트는 관리자마다 1번


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
