"""구독 결제 점검 (가짜 TronGrid 로 네트워크 없이): python tests/test_billing.py"""
import asyncio
import hashlib
import os
import sys
import time
from types import SimpleNamespace

import httpx

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, add_member, cfg, fake_user, make_db, make_svc, runner

from sodam import commands, config, handlers, subscription
from sodam.billing import UNIT, Billing, fmt_usdt
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.tron import USDT_CONTRACT, valid_tron_address

test, run_all = runner()
CHAT = -1001234567
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def make_address(seed: bytes) -> str:
    """테스트용 올바른 트론 주소 (0x41 + 20바이트 + 체크섬)."""
    payload = b"\x41" + hashlib.sha256(seed).digest()[:20]
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return out


PAY = make_address(b"our-wallet")
OTHER = make_address(b"someone-else")
PAYER = make_address(b"payer")
FAKE_TOKEN = make_address(b"fake-usdt")


class FakeTronGrid:
    def __init__(self):
        self.transfers: list[dict] = []
        self.requests: list[httpx.Request] = []

    def transfer(self, tx, amount_units, *, to=PAY, contract=USDT_CONTRACT, ts=None, decimals=6):
        self.transfers.append({
            "transaction_id": tx, "from": PAYER, "to": to, "type": "Transfer", "value": str(amount_units),
            "block_timestamp": int((ts or time.time()) * 1000),
            "token_info": {"symbol": "USDT", "address": contract, "decimals": decimals, "name": "Tether USD"}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        items = [t for t in self.transfers if t["block_timestamp"] >= int(request.url.params.get("min_timestamp", 0))]
        if request.url.params.get("fingerprint") is None and len(items) > 2:  # 페이지 나눔 흉내
            return httpx.Response(200, json={"data": items[:2], "meta": {"fingerprint": "page2"}})
        if request.url.params.get("fingerprint") == "page2":
            return httpx.Response(200, json={"data": items[2:], "meta": {}})
        return httpx.Response(200, json={"data": items, "meta": {}})


async def setup(**kw):
    db = await make_db()
    grid = FakeTronGrid()
    svc = await make_svc(db, admins={1}, pay_address=PAY, trongrid_api_key="k-123", **kw)
    svc.billing = Billing(svc.cfg, db, httpx.AsyncClient(transport=httpx.MockTransport(grid.handler)))
    await db.ensure_chat(CHAT, "오케이 소통방")
    return db, svc, grid


# ── 주소·설정 검증 ───────────────────────────────────────
@test
def tron_address_validation():
    assert valid_tron_address(USDT_CONTRACT)                    # 실제 공식 주소로 구현 검증
    assert valid_tron_address(PAY)
    typo = USDT_CONTRACT[:-1] + ("s" if USDT_CONTRACT[-1] != "s" else "t")
    assert not valid_tron_address(typo)                           # 한 글자 오타도 거름
    for bad in ("", "0x" + "a" * 40, USDT_CONTRACT[:-1], "T" + "0" * 33):
        assert not valid_tron_address(bad), bad


@test
def config_rejects_bad_payment_settings():
    assert config._pay_address("") == "" and config._pay_address(f" {PAY} ") == PAY
    for bad in ("TXYZ", USDT_CONTRACT[:-1] + "x"):
        try:
            config._pay_address(bad)
            raise AssertionError(bad)
        except SystemExit:
            pass
    assert config._price("30") == "30" and config._price("19.99") == "19.99"
    for bad in ("0", "abc", "1.234", "-5"):
        try:
            config._price(bad)
            raise AssertionError(bad)
        except SystemExit:
            pass


# ── 구독 상태 ─────────────────────────────────────────────
@test
async def status_trial_paid_expired():
    db = await make_db()
    off = Billing(cfg(db.path), db)
    assert (await off.status(CHAT)).state == "free" and await off.active(CHAT)   # 결제 꺼짐 = 무료

    db, svc, _ = await setup(trial_days=3, sub_days=30)
    b = svc.billing
    st = await b.status(CHAT)
    assert st.state == "trial" and st.active and st.until > time.time() + 2 * 86400
    await db.conn.execute("UPDATE subscriptions SET trial_until=? WHERE chat_id=?", (int(time.time()) - 10, CHAT))
    await db.conn.commit()
    assert (await b.status(CHAT)).state == "expired" and not await b.active(CHAT)
    until = await b.extend(CHAT, 30)
    assert (await b.status(CHAT)).state == "paid" and abs(until - (time.time() + 30 * 86400)) < 5
    until2 = await b.extend(CHAT, 30)                               # 남은 기간 뒤로 이어서 연장
    assert abs(until2 - until - 30 * 86400) < 2


# ── 청구서 ────────────────────────────────────────────────
@test
async def invoice_amounts_unique():
    db, svc, _ = await setup(sub_price_usdt="30")
    b = svc.billing
    inv = await b.create_invoice(CHAT, 1)
    assert 30 * UNIT < inv["amount_units"] < 30.1 * UNIT and inv["amount_units"] % 100 == 0
    assert (await b.create_invoice(CHAT, 1))["id"] == inv["id"]    # 버튼 연타해도 같은 청구서
    amounts = {inv["amount_units"]}
    for i in range(30):
        amounts.add((await b.create_invoice(CHAT - 1 - i, 1))["amount_units"])
    assert len(amounts) == 31                                      # 방마다 금액 끝자리가 다름
    assert fmt_usdt(30_013_700) == "30.0137"


# ── 블록체인 대조 ─────────────────────────────────────────
@test
async def payment_matching_is_strict():
    db, svc, grid = await setup(sub_days=30)
    b = svc.billing
    await b.ensure_trial(CHAT, 1)
    inv = await b.create_invoice(CHAT, 1)
    amt = inv["amount_units"]
    grid.transfer("tx-fake-token", amt, contract=FAKE_TOKEN)      # 가짜 USDT
    grid.transfer("tx-wrong-to", amt, to=OTHER)                    # 다른 주소로
    grid.transfer("tx-wrong-dec", amt, decimals=18)                # 소수점이 다른 토큰
    grid.transfer("tx-short", amt - 100)                           # 금액 부족
    paid, unmatched = await b.check_pending()
    assert paid == [] and {u["tx_id"] for u in unmatched} == {"tx-short"}  # 진짜 USDT 인데 금액만 틀린 건 '확인 필요'로 보고
    assert (await db.get_invoice(inv["id"]))["status"] == "pending"

    grid.transfer("tx-good", amt)
    paid, _ = await b.check_pending()
    assert [p["tx_id"] for p in paid] == ["tx-good"]
    assert (await db.get_invoice(inv["id"]))["status"] == "paid"
    assert (await b.status(CHAT)).state == "paid"

    req = grid.requests[-1]
    assert req.url.path == f"/v1/accounts/{PAY}/transactions/trc20"
    assert req.url.params["only_confirmed"] == "true" and req.url.params["only_to"] == "true"
    assert req.url.params["contract_address"] == USDT_CONTRACT and req.headers["TRON-PRO-API-KEY"] == "k-123"
    assert any(r.url.params.get("fingerprint") == "page2" for r in grid.requests)  # 다음 페이지도 읽음

    # 같은 거래를 다시 보여줘도 두 번 연장되지 않음
    until = (await db.get_subscription(CHAT))["paid_until"]
    inv2 = await b.create_invoice(CHAT, 1)
    grid.transfers = [t for t in grid.transfers if t["transaction_id"] == "tx-good"]
    grid.transfers[0]["value"] = str(inv2["amount_units"])         # 금액을 바꿔 재사용 시도
    paid, _ = await b.check_pending()
    assert paid == [] and (await db.get_subscription(CHAT))["paid_until"] == until


@test
async def late_or_early_transfer_not_matched():
    db, svc, grid = await setup(invoice_minutes=10)
    b = svc.billing
    inv = await b.create_invoice(CHAT, 1)
    grid.transfer("tx-early", inv["amount_units"], ts=inv["created"] - 3600)   # 청구서 전에 보낸 돈
    grid.transfer("tx-late", inv["amount_units"], ts=inv["expires"] + 60)      # 유효시간 지나서
    paid, unmatched = await b.check_pending()
    assert paid == [] and {u["tx_id"] for u in unmatched} == {"tx-late"}      # 너무 이른 건 조회 범위 밖


# ── 기능 제한 (구독 안 한 방) ─────────────────────────────
async def _expire(db):
    await db.conn.execute("UPDATE subscriptions SET trial_until=?, paid_until=NULL WHERE chat_id=?",
                          (int(time.time()) - 10, CHAT))
    await db.conn.commit()


@test
async def expired_room_gating():
    db, svc, _ = await setup(free_ai_per_day=2)
    await svc.billing.ensure_trial(CHAT, 1)
    await _expire(db)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    results = [await handlers._within_ai_quota(ctx, CHAT, 20, Role.MEMBER) for _ in range(3)]
    assert results == [True, True, False]                          # 무료 2회까지
    notice = bot.named("send_message")[-1]
    assert "USDT" not in notice[2] and PAY not in notice[2]         # 방에는 금액·주소 노출 안 함
    assert "start=sub_" in notice[3]["reply_markup"].inline_keyboard[0][0].url
    assert await handlers._within_ai_quota(ctx, CHAT, 1, Role.OWNER)            # 오너는 제한 없음

    assert "이용 기간" in await svc.games.start(bot, CHAT, 20, "업다운")
    await db.add_schedule(CHAT, kind="interval", at_time=None, interval_min=30, title="t", text="x",
                          media_type=None, media_id=None, pin=False, created_by=1)
    await db.conn.execute("UPDATE schedules SET last_sent=0")
    await db.conn.commit()
    await svc.announcer.run_due(bot)
    assert not [c for c in bot.named("send_message") if c[1] == CHAT and "📢" in c[2]]  # 예약공지도 멈춤

    await svc.billing.extend(CHAT, 30)
    assert await handlers._within_ai_quota(ctx, CHAT, 20, Role.MEMBER)
    assert "시작" in await svc.games.start(bot, CHAT, 20, "업다운")


# ── 결제 화면 (1:1 전용) ──────────────────────────────────
@test
async def payment_flow_private_only():
    db, svc, grid = await setup(sub_price_usdt="30")
    await svc.billing.ensure_trial(CHAT, 1)
    bot = FakeBot()
    admin, member = fake_user(1, "방장"), fake_user(20, "멤버")

    # 딥링크: 관리자만 설정 화면
    m = FakeMsg(20, member, f"/start sub_{CHAT}")
    await subscription.on_deep_link(svc, bot, m, CHAT)
    assert "관리자만" in m.replies[0]
    m = FakeMsg(1, admin, f"/start sub_{CHAT}")
    await subscription.on_deep_link(svc, bot, m, CHAT)
    assert "30.0000 USDT" in m.replies[0] or "30 USDT" in m.replies[0] or "USDT" in m.replies[0]

    # 방에서 누른 결제 버튼은 거부
    q = FakeQuery(CHAT, admin)
    await subscription.on_callback(svc, bot, q, ["new", str(CHAT)])
    assert "1:1" in q.answers[0][0] and not await db.open_invoice(CHAT, int(time.time()))
    # 1:1 에서도 관리자가 아니면 거부
    q = FakeQuery(20, member)
    await subscription.on_callback(svc, bot, q, ["new", str(CHAT)])
    assert "관리자만" in q.answers[0][0]
    # 관리자 1:1 → 청구서는 관리자 개인 채팅으로만
    q = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q, ["new", str(CHAT)])
    inv_msg = bot.named("send_message")[-1]
    assert inv_msg[1] == 1 and PAY in inv_msg[2] and "TRC20" in inv_msg[2]
    inv = await db.open_invoice(CHAT, int(time.time()))
    assert fmt_usdt(inv["amount_units"]) in inv_msg[2]

    # 남의 청구서 확인 버튼은 거부
    q = FakeQuery(20, member)
    await subscription.on_callback(svc, bot, q, ["check", str(inv["id"])])
    assert "찾을 수 없" in q.answers[0][0]

    # 입금 → 확인 버튼 → 결제자·방·오너에게 알림 (방 알림엔 금액 없음)
    svc.perms.owners = lambda: _async({99})
    grid.transfer("tx-ok", inv["amount_units"])
    q = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q, ["check", str(inv["id"])])
    sent = {c[1]: c[2] for c in bot.named("send_message")}
    assert "결제 확인" in sent[1]
    assert "활성화" in sent[CHAT] and "USDT" not in sent[CHAT] and PAY not in sent[CHAT]
    assert "[결제]" in sent[99] and "tx-ok" in sent[99]
    q2 = FakeQuery(1, admin)
    await subscription.on_callback(svc, bot, q2, ["check", str(inv["id"])])
    assert "이미" in q2.answers[0][0]


async def _async(v):
    return v


@test
async def bot_added_starts_trial_without_showing_price():
    db, svc, _ = await setup(trial_days=3)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc, "chats": set()})
    adder = fake_user(1, "방장")

    def m(status):
        return SimpleNamespace(status=status, is_member=None, user=SimpleNamespace(id=bot.id))

    upd = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup", title="새 방"), from_user=adder,
        old_chat_member=m("left"), new_chat_member=m("member")))
    await handlers.on_my_chat_member(upd, ctx)
    assert (await svc.billing.status(CHAT)).state == "trial"
    group_msg = next(c for c in bot.named("send_message") if c[1] == CHAT)
    assert "3일" in group_msg[2] and "USDT" not in group_msg[2] and "결제" not in group_msg[2]
    assert group_msg[3]["reply_markup"].inline_keyboard[0][0].text == "⚙️ 봇 설정 (관리자)"
    dm = [c for c in bot.named("send_message") if c[1] == 1]
    assert dm and "USDT" in dm[0][2]                                # 가격은 초대한 관리자 1:1 로만


@test
async def subscribe_command_in_group_hides_details():
    db, svc, _ = await setup()
    await svc.billing.ensure_trial(CHAT, 1)
    bot = FakeBot()
    admin = fake_user(1, "방장")
    cmd, args, argstr = commands.parse(".구독", "sodambot")
    msg = FakeMsg(CHAT, admin, ".구독")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    group_notes = [c[2] for c in bot.named("send_message") if c[1] == CHAT]
    assert msg.deleted and group_notes and "1:1" in group_notes[-1]      # 명령은 지우고 짧은 안내만
    assert not any("USDT" in n for n in group_notes)
    assert any(c[1] == 1 and "USDT" in c[2] for c in bot.named("send_message"))

    member = fake_user(20, "멤버")
    await add_member(db, CHAT, member)
    msg = FakeMsg(CHAT, member, ".구독")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, member, Role.MEMBER, args, argstr), cmd)
    assert "관리자만" in msg.replies[0]

    # 실사용 버그: 오너 등록 전인 방 관리자가 1:1 에서 .구독 → 막히면 안 되고 자기 방 목록이 나와야 함
    msg = FakeMsg(1, admin, ".구독")
    await commands.dispatch(CmdCtx(svc, bot, msg, 1, admin, Role.MEMBER, args, argstr), cmd)
    assert msg.replies and "USDT" in msg.replies[0] and "오케이 소통방" in msg.replies[0]
    msg = FakeMsg(20, member, ".구독")                              # 관리하는 방이 없는 사람
    await commands.dispatch(CmdCtx(svc, bot, msg, 20, member, Role.MEMBER, args, argstr), cmd)
    assert "관리 중인 방이 없" in msg.replies[0]

    # 방의 일반 멤버 도움말엔 .구독 이 안 보임, 관리자에겐 보임
    hcmd, hargs, hargstr = commands.parse(".도움말", "sodambot")
    for role, visible in ((Role.MEMBER, False), (Role.ADMIN, True)):
        msg = FakeMsg(CHAT, member, ".도움말")
        await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, member, role, hargs, hargstr), hcmd)
        assert (".구독" in msg.replies[0]) is visible, role


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(1 if asyncio.run(run_all()) else 0)
