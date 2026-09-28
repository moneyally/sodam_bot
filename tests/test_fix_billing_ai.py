"""결제·AI 비용·제재 버그 수정 회귀 테스트 (네트워크 없이): python tests/run_all.py test_fix_billing_ai"""
import asyncio
import sqlite3
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_billing as TB  # noqa: E402
from fake_llm import Room, reply, tool_call  # noqa: E402
from fakes import FakeBot, FakeQuery, add_member, cfg, fake_user, make_db, make_svc, runner  # noqa: E402

from sodam import billing as billing_mod  # noqa: E402
from sodam import handlers, subscription, tools  # noqa: E402
from sodam.agent import run_agent  # noqa: E402
from sodam.ai_settings import ROOM_TOKENS_MAX  # noqa: E402
from sodam.llm import LLM, ROOM_TOKENS, BudgetExceeded  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.settings import coerce  # noqa: E402
from sodam.tools import ToolCtx, execute  # noqa: E402

test, run_all = runner()
CHAT = TB.CHAT


# ── 1. 결제 기록 뒤 죽어도 다음 주기에 연장 ─────────────────
@test
async def payment_retried_after_crash_between_record_and_extend():
    db, svc, grid = await TB.setup()
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
    paid, unmatched = await svc.billing.check_pending()        # 다음 주기
    assert [p["tx_id"] for p in paid] == ["tx-crash"] and not unmatched, (paid, unmatched)
    assert (await db.get_invoice(inv["id"]))["status"] == "paid"
    assert (await svc.billing.status(CHAT)).state == "paid"
    paid, unmatched = await svc.billing.check_pending()        # 같은 거래로 두 번 연장은 안 됨
    assert not paid and not unmatched


@test
async def invoice_paid_and_extend_are_one_transaction():
    db, svc, grid = await TB.setup()
    inv = await svc.billing.create_invoice(CHAT, 1)
    grid.transfer("tx-half", inv["amount_units"])
    # 구독 연장 SQL 이 DB 안에서 실패 (청구서 UPDATE 는 이미 실행된 뒤)
    for ev in ("INSERT", "UPDATE"):
        await db.conn.execute(f"CREATE TEMP TRIGGER boom_{ev} BEFORE {ev} ON subscriptions "
                              "BEGIN SELECT RAISE(ABORT, '연장 중 죽음'); END")
    try:
        await svc.billing.check_pending()
        raise AssertionError("예외가 나야 함")
    except sqlite3.IntegrityError:
        pass
    for ev in ("INSERT", "UPDATE"):
        await db.conn.execute(f"DROP TRIGGER boom_{ev}")
    assert (await db.get_invoice(inv["id"]))["status"] == "pending"   # 반쯤 처리된 채로 남지 않음
    paid, _ = await svc.billing.check_pending()
    assert [p["tx_id"] for p in paid] == ["tx-half"]
    assert (await svc.billing.status(CHAT)).state == "paid"


# ── 2. 취소한 청구서 금액은 유효시간 안에 다른 방에 안 줌 ───────
@test
async def cancelled_invoice_amount_not_reused():
    db, svc, grid = await TB.setup()
    other = -100999
    await db.ensure_chat(other, "다른 방")
    a = await svc.billing.create_invoice(CHAT, 1)
    await db.cancel_invoice(a["id"])
    same = (a["amount_units"] - svc.billing.price_units - 100) // 100
    seq = iter([same, same, (same + 1) % 999])
    orig = billing_mod.secrets.randbelow
    billing_mod.secrets.randbelow = lambda n: next(seq)
    try:
        b = await svc.billing.create_invoice(other, 2)
    finally:
        billing_mod.secrets.randbelow = orig
    assert b["amount_units"] != a["amount_units"]
    grid.transfer("tx-a", a["amount_units"])                     # A 방 관리자가 취소 뒤 그래도 보냄
    paid, unmatched = await svc.billing.check_pending()
    assert not paid and [u["tx_id"] for u in unmatched] == ["tx-a"]   # 다른 방에 잘못 연장 안 됨, 오너 보고


# ── 3·5. 인젝션 판별·웹검색도 방 토큰으로 센다 ──────────────
def fake_openai(total=500):
    usage = SimpleNamespace(total_tokens=total, prompt_tokens=total - 50, prompt_tokens_details=None)
    calls = []

    async def chat_create(**kw):
        calls.append(("chat", kw))
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"injection": False, "reason": ""}), tool_calls=None))])

    async def resp_create(**kw):
        calls.append(("responses", kw))
        return SimpleNamespace(usage=usage, output_text="검색 요약")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
                             responses=SimpleNamespace(create=resp_create))
    return client, calls


@test
async def classify_injection_counts_room_tokens():
    db = await make_db()
    llm = LLM(cfg(db.path), db)
    llm.client, calls = fake_openai(700)
    assert await llm.classify_injection("긴 메시지", chat_id=CHAT) == (False, "")
    assert calls and await db.counter(llm._today(), CHAT, ROOM_TOKENS) == 700
    await db.set_setting(CHAT, "ai_room_daily_tokens", 10_000)
    await db.bump(llm._today(), CHAT, ROOM_TOKENS, 10_000)
    n = len(calls)
    assert await llm.classify_injection("또", chat_id=CHAT) == (False, "")   # 방 한도 넘으면 OpenAI 안 부름
    assert len(calls) == n


@test
async def web_search_tool_counts_and_respects_room_tokens():
    db = await make_db()
    svc = await make_svc(db)
    svc.llm = LLM(svc.cfg, db)
    svc.llm.client, calls = fake_openai(900)
    ctx = ToolCtx(svc, FakeBot(), CHAT, fake_user(10), Role.MEMBER, await db.get_settings(CHAT))
    assert await execute("web_search", json.dumps({"query": "오늘 뉴스"}), ctx) == "검색 요약"
    assert await db.counter(svc.llm._today(), CHAT, ROOM_TOKENS) == 900
    await db.bump(svc.llm._today(), CHAT, ROOM_TOKENS, ROOM_TOKENS_MAX)
    n = len(calls)
    assert "한도" in await execute("web_search", json.dumps({"query": "또 검색"}), ctx)
    assert len(calls) == n                                        # 방 한도 넘으면 검색 호출 안 함
    try:
        await svc.llm.web_search("직접", CHAT)
        raise AssertionError("방 한도 넘었는데 통과")
    except BudgetExceeded:
        pass


# ── 4. 방 토큰 한도는 방 관리자가 풀 수 없음 ─────────────────
@test
async def room_cost_settings_have_safe_ranges():
    # .set 경로(commands.c_set)와 버튼 입력이 쓰는 coerce 에서 막힘
    for key, bad in (("ai_room_daily_tokens", "0"), ("ai_room_daily_tokens", str(ROOM_TOKENS_MAX + 1)),
                     ("web_search_daily", "101"), ("room_rate_per_min", "61")):
        try:
            coerce(key, bad)
            raise AssertionError(f"{key}={bad} 가 통과함")
        except ValueError:
            pass
    assert coerce("ai_room_daily_tokens", "10000") == 10_000 and coerce("web_search_daily", "0") == 0


@test
async def stored_unlimited_room_cap_is_clamped():
    db = await make_db()
    llm = LLM(cfg(db.path), db)
    await db.set_setting(CHAT, "ai_room_daily_tokens", 0)             # 예전에 저장된 '무제한'
    await db.bump(llm._today(), CHAT, ROOM_TOKENS, ROOM_TOKENS_MAX)
    try:
        await llm._check_budget(CHAT)
        raise AssertionError("상한을 넘었는데 통과")
    except BudgetExceeded:
        pass


# ── 6. 한 번의 AI 답변에서 제재는 한 번만 ─────────────────
@test
async def agent_sanctions_once_per_run():
    r = await Room().open(admins={1})
    bob = fake_user(20, "박준호", "junho")
    boss = fake_user(1, "방장", "boss")
    for u in (bob, boss):
        await r.join(u)
    r.llm.script = [tool_call("warn_member", {"name": "@junho", "reason": "a"}, "c1"),
                    tool_call("warn_member", {"name": "@junho", "reason": "b"}, "c2"),
                    tool_call("mute_member", {"name": "@junho", "minutes": 60, "reason": "c"}, "c3"),
                    tool_call("ban_member", {"name": "@junho", "reason": "d"}, "c4"),
                    reply("처리했어요.")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, boss, Role.ADMIN, await r.db.get_settings(r.CHAT))
    out = await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None,
                          request="준호 경고 여러 번 주고 뮤트하고 내보내", extras={})
    assert out == "처리했어요."
    tool_results = [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]
    assert sum("한 번만" in t for t in tool_results) == 3, tool_results
    # 제재는 바로 안 되고 확인 버튼 1개만 (대화에 숨은 지시로 제재되지 않게)
    assert await r.db.warning_count(r.CHAT, bob.id) == 0 and not r.bot.named("restrict")
    assert [a.kind for a in r.svc.pending.values()] == ["warn"]
    key = next(iter(r.svc.pending))
    await handlers._confirm_action(r.svc, r.bot, FakeQuery(1, boss, f"act:{key}:y"), [key, "y"])
    assert await r.db.warning_count(r.CHAT, bob.id) == 1 and not r.svc.pending


@test
async def mute_via_ai_waits_for_admin_button():
    r = await Room().open(admins={1})
    bob, boss = fake_user(20, "박준호", "junho"), fake_user(1, "방장", "boss")
    for u in (bob, boss):
        await r.join(u)
    r.llm.script = [tool_call("mute_member", {"name": "@junho", "minutes": 60, "reason": "도배"}), reply("버튼 눌러주세요.")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, boss, Role.ADMIN, await r.db.get_settings(r.CHAT))
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="준호 1시간 뮤트", extras={})
    assert not r.bot.named("restrict")
    key = next(iter(r.svc.pending))
    q = FakeQuery(bob.id, bob, f"act:{key}:y")                      # 일반 멤버는 못 누름
    await handlers._confirm_action(r.svc, r.bot, q, [key, "y"])
    assert not r.bot.named("restrict") and "관리자만" in q.answers[-1][0]
    q = FakeQuery(1, boss, f"act:{key}:y")
    await handlers._confirm_action(r.svc, r.bot, q, [key, "y"])
    assert r.bot.named("restrict") and "1시간" in q.edits[-1]


# ── 7. 청구서 못 만들면 버튼에 안내 ────────────────────────
@test
async def invoice_error_answers_callback():
    db, svc, grid = await TB.setup()
    await svc.billing.create_invoice(CHAT, 1)
    other = -100999
    await db.ensure_chat(other, "다른 방")
    taken = next(iter(await db.pending_amounts(0)))
    orig = billing_mod.secrets.randbelow
    billing_mod.secrets.randbelow = lambda n: (taken - svc.billing.price_units - 100) // 100   # 계속 겹침
    svc.perms.admins.add(1)
    q = FakeQuery(1, fake_user(1))
    try:
        await subscription.on_callback(svc, FakeBot(), q, ["new", str(other)])
    finally:
        billing_mod.secrets.randbelow = orig
    assert len(q.answers) == 1 and q.answers[0][1] is True and "청구서" in q.answers[0][0], q.answers


# ── 8. 멤버 전달은 그 방 관리자에게도 1:1 로 ─────────────────
@test
async def report_to_admin_reaches_room_admins():
    db = await make_db()
    svc = await make_svc(db, admins={2, 3}, log_chat_id=-100555)
    admin2, admin3, helper = fake_user(2, "관리자A"), fake_user(3, "관리자B"), fake_user(77, "다른봇", is_bot=True)
    bot = FakeBot(admins=[admin2, admin3, helper])
    bot.dm_blocked = {3}                                            # 봇과 대화 안 시작한 관리자 → 조용히 건너뜀
    member = fake_user(10, "김민지")
    await add_member(db, CHAT, member)
    ctx = ToolCtx(svc, bot, CHAT, member, Role.MEMBER, await db.get_settings(CHAT))
    r = await execute("report_to_admin", json.dumps({"message": "도배하는 사람 있어요"}), ctx)
    targets = [c[1] for c in bot.named("send_message")]
    assert "전달함" in r and 2 in targets and -100555 in targets, (r, targets)
    assert 77 not in targets and 3 not in targets
    assert all("도배하는 사람" in c[2] for c in bot.named("send_message"))


if __name__ == "__main__":
    sys.exit(asyncio.run(run_all()))
