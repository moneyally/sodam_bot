"""하루 예산(달러)·방 한도(오너 요금제 × 방 관리자 %): python tests/run_all.py budget"""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, make_db, make_svc, runner
from harness import HQuery
from test_fix_billing_ai import cfg

from sodam import costs, menu
from sodam.llm import LLM, ROOM_TOKENS, BudgetExceeded
from sodam.settings import coerce

test, run_all = runner()
ROOT = Path(__file__).resolve().parent.parent
CHAT, OTHER = -1001000000001, -1001000000002


def usage(prompt=1000, cached=600, total=1100):
    return SimpleNamespace(total_tokens=total, prompt_tokens=prompt,
                           prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


def fake_client(calls):
    async def create(**kw):
        calls.append(kw)
        return SimpleNamespace(usage=usage(), choices=[SimpleNamespace(message=SimpleNamespace(content="{}", tool_calls=None))])

    async def resp(**kw):
        calls.append(kw)
        return SimpleNamespace(usage=usage(), output_text="요약")
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                           responses=SimpleNamespace(create=resp))


async def blocked(llm, chat_id=None) -> str | None:
    try:
        await llm._check_budget(chat_id)
    except BudgetExceeded as e:
        return e.args[0] if e.args else "?"
    return None


async def setup():
    db = await make_db()
    llm = LLM(cfg(db.path), db)
    return db, llm


@test
async def usd_recorded_globally_and_per_room():
    db, llm = await setup()
    calls = []
    llm.client = fake_client(calls)
    await llm.chat([{"role": "user", "content": "x"}], model="gpt-5.4", chat_id=CHAT)
    day = llm._today()
    # gpt-5.4: 400×2.5 + 600×0.25 + 100×15 = 2650 마이크로달러
    assert await db.counter(day, 0, costs.USD) == 2650
    assert await db.counter(day, CHAT, costs.ROOM_USD) == 2650 and await llm.usd_today(CHAT) == 2650
    await llm.web_search("뉴스", CHAT)          # mini 토큰 요금 + 검색 1번 $0.01
    mini = costs.usd_micro("gpt-5.4-mini", 1000, 600, 100)
    assert await db.counter(day, CHAT, costs.ROOM_USD) == 2650 + mini + 10_000
    assert await llm.usd_today() == 2650 + mini + 10_000
    await llm.json("s", "u")                   # 방 지정 없는 호출은 전체에만
    assert await llm.usd_today() == 2650 + 2 * mini + 10_000
    assert await db.counter(day, CHAT, costs.ROOM_USD) == 2650 + mini + 10_000


@test
async def unknown_model_counted_at_main_price():
    db, llm = await setup()
    await llm._record(usage(0, 0, 1000), CHAT, "chat", "gpt-9")          # 대화 모델: 요금표 없음 → 기본 모델 값
    assert await db.counter(llm._today(), 0, costs.USD) == costs.usd_micro("gpt-5.4", 0, 0, 1000) == 15_000
    assert costs.usd_micro("모름", 0, 0, 1000, "모름2") == 15_000       # 기본 모델도 모르면 가장 비싼 요금
    assert costs.usd_micro("gpt-5.4", 0, 0, 0) == 0


@test
async def global_usd_budget_blocks_before_calling_openai():
    db, llm = await setup()
    calls = []
    llm.client = fake_client(calls)
    llm.usd_budget = 1.0
    assert await blocked(llm) is None
    await db.bump(llm._today(), 0, costs.USD, 999_999)
    assert await blocked(llm) is None
    await db.bump(llm._today(), 0, costs.USD, 1)
    assert await blocked(llm) == "usd" and await blocked(llm, CHAT) == "usd"
    try:
        await llm.chat([{"role": "user", "content": "x"}])
        raise AssertionError("예산을 넘었는데 통과")
    except BudgetExceeded:
        pass
    assert not calls
    llm.usd_budget = 0          # 0 = 달러 예산 끔
    assert await blocked(llm) is None


@test
async def cached_heavy_day_not_blocked_by_tokens_unless_env_set():
    """2M 토큰(대부분 캐시) = 실제 몇 달러 → 토큰 예산은 .env 에 적은 경우만 본다."""
    db, llm = await setup()
    await db.bump(llm._today(), 0, "tokens", 5_000_000)
    old = os.environ.pop("DAILY_TOKEN_BUDGET", None)
    try:
        assert LLM(cfg(db.path), db).token_budget == 0
        assert await blocked(LLM(cfg(db.path), db)) is None
        os.environ["DAILY_TOKEN_BUDGET"] = "4000000"      # 예전처럼 적어 두면 그대로 지킴
        strict = LLM(cfg(db.path, daily_token_budget=4_000_000), db)
        assert strict.token_budget == 4_000_000 and await blocked(strict) == "tokens"
        os.environ["DAILY_USD_BUDGET"] = "2.5"
        assert LLM(cfg(db.path), db).usd_budget == 2.5
        os.environ["DAILY_USD_BUDGET"] = "abc"
        assert LLM(cfg(db.path), db).usd_budget == costs.DEFAULT_USD_BUDGET
    finally:
        os.environ.pop("DAILY_TOKEN_BUDGET", None)
        os.environ.pop("DAILY_USD_BUDGET", None)
        if old is not None:
            os.environ["DAILY_TOKEN_BUDGET"] = old


@test
async def room_usd_quota_blocks_that_room_only():
    db, llm = await setup()
    day = llm._today()
    await db.bump(day, CHAT, costs.ROOM_USD, 1_499_999)            # 기본 요금제 $1.50
    assert await blocked(llm, CHAT) is None
    await db.bump(day, CHAT, costs.ROOM_USD, 1)
    assert await blocked(llm, CHAT) == "room_usd"
    assert await blocked(llm, OTHER) is None and await blocked(llm) is None
    await db.set_state(CHAT, costs.PLAN_KEY, 300)                  # 오너가 $3 요금제로 올림
    assert await blocked(llm, CHAT) is None
    await db.set_setting(CHAT, costs.PCT_KEY, 50)                  # 방 관리자가 50% 로 낮춤 → $1.50
    assert await costs.room_cap_micro(db, CHAT) == 1_500_000 and await blocked(llm, CHAT) == "room_usd"
    await db.set_state(CHAT, costs.PLAN_KEY, 99_999)               # 정해진 값이 아니면 기본 요금제
    assert await costs.room_plan_cents(db, CHAT) == costs.DEFAULT_PLAN_CENTS


@test
async def room_token_cap_still_works():
    db, llm = await setup()
    await db.bump(llm._today(), CHAT, ROOM_TOKENS, 600_000)
    assert await blocked(llm, CHAT) == "room_tokens"


@test
async def owner_dm_has_no_room_usd_cap_but_others_do():
    db, llm = await setup()                                        # cfg owner_ids = {1}
    day = llm._today()
    for uid in (1, 55):
        await db.bump(day, uid, costs.ROOM_USD, 5_000_000)
    assert await blocked(llm, 1) is None
    assert await blocked(llm, 55) == "room_usd"
    await db.add_owner(55)                                         # /owner 로 등록된 오너도
    assert await blocked(llm, 55) is None


@test
async def room_admin_can_only_lower():
    for bad in ("101", "500", "0", "9"):
        try:
            coerce(costs.PCT_KEY, bad)
            raise AssertionError(f"{bad} 가 통과")
        except ValueError:
            pass
    assert coerce(costs.PCT_KEY, "30") == 30
    try:                                   # 요금제는 방 설정이 아니라서 .설정변경·AI 도구로 못 바꿈
        coerce(costs.PLAN_KEY, "1000")
        raise AssertionError("요금제가 방 설정으로 바뀜")
    except ValueError:
        pass
    db, llm = await setup()
    await db.set_setting(CHAT, costs.PCT_KEY, 900)                 # 예전·위조 저장값도 100% 로 자름
    assert await costs.room_cap_micro(db, CHAT) == costs.DEFAULT_PLAN_CENTS * 10_000


class TwoRoomPerms:
    """오너 7 · CHAT 관리자 1 · OTHER 관리자 2 · 멤버 20."""

    async def owners(self):
        return {7}

    async def is_admin(self, bot, cid, uid):
        return uid == 7 or (cid, uid) in {(CHAT, 1), (OTHER, 2)}

    is_tg_admin = is_admin

    def forget(self, cid):
        pass


async def panel_world():
    db = await make_db()
    svc = await make_svc(db)
    svc.perms = TwoRoomPerms()
    for cid in (CHAT, OTHER):
        await db.ensure_chat(cid, f"방 {cid}")
    return db, svc, FakeBot()


async def press(svc, bot, uid, data) -> HQuery:
    svc.menu_limiter._hits.clear()
    q = HQuery(uid, data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers
    return q


@test
async def only_owner_sets_room_plan():
    db, svc, bot = await panel_world()
    for uid in (1, 20):                                            # 방 관리자·멤버는 요금제 못 바꿈
        q = await press(svc, bot, uid, f"m:alqs:{CHAT}:500")
        assert q.answers[0][1] and not q.edits
    assert await db.get_state(CHAT, costs.PLAN_KEY) is None
    q = await press(svc, bot, 7, f"m:alqs:{CHAT}:99999")          # 위조 값
    assert q.answers[0][1] and await db.get_state(CHAT, costs.PLAN_KEY) is None
    q = await press(svc, bot, 7, f"m:alqs:{CHAT}:500")
    assert await db.get_state(CHAT, costs.PLAN_KEY) == 500 and "$5.00" in q.edits[0][0]
    assert await costs.room_cap_micro(db, CHAT) == 5_000_000
    # 방 관리자는 % 로 줄이기만: 프리셋 목록에 없는 값(위조 콜백)은 무시
    await press(svc, bot, 1, f"m:n:{CHAT}:{costs.PCT_KEY}:500")
    assert (await db.get_settings(CHAT))[costs.PCT_KEY] == 100
    await press(svc, bot, 1, f"m:n:{CHAT}:{costs.PCT_KEY}:30")
    assert (await db.get_settings(CHAT))[costs.PCT_KEY] == 30
    await press(svc, bot, 2, f"m:n:{CHAT}:{costs.PCT_KEY}:100")    # 다른 방 관리자는 못 바꿈
    assert (await db.get_settings(CHAT))[costs.PCT_KEY] == 30


@test
async def owner_overview_shows_usd_and_top_rooms():
    db, svc, bot = await panel_world()
    svc.llm = SimpleNamespace(usd_budget=8.0, token_budget=0)
    day = LLM(svc.cfg, db)._today()
    await db.bump(day, 0, costs.USD, 4_000_000)
    await db.bump(day, CHAT, costs.ROOM_USD, 1_200_000)
    q = await press(svc, bot, 7, "m:al")
    text = q.edits[0][0]
    assert "$4.00" in text and "$8.00 (50%)" in text and "$1.20 / $1.50 (80%)" in text, text
    for uid in (1, 20):
        q = await press(svc, bot, uid, "m:al")
        assert q.answers[0][1] and not q.edits


@test
async def usage_report_shows_room_usd():
    db = await make_db()
    await db.ensure_chat(CHAT, "OTC 방")
    await db.bump("2026-09-28", 0, costs.USD, 3_000_000)
    await db.bump("2026-09-28", CHAT, costs.ROOM_USD, 1_400_000)
    await db.set_state(CHAT, costs.PLAN_KEY, 300)
    await db.conn.commit()
    env = {k: v for k, v in os.environ.items() if k != "DAILY_TOKEN_BUDGET"}
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "usage_report.py"), "--day", "2026-09-28", "--db", db.path],
                         capture_output=True, text=True, timeout=60,
                         env={**env, "DAILY_USD_BUDGET": "4", "DAILY_TOKEN_BUDGET": ""})
    assert out.returncode == 0, out.stderr
    assert "기록된 요금 $3.000 / 하루 예산 $4.00 (75%)" in out.stdout, out.stdout
    assert "OTC 방" in out.stdout and "$1.400 / $3.00 (46%)" in out.stdout, out.stdout


if __name__ == "__main__":
    import asyncio
    sys.exit(1 if asyncio.run(run_all()) else 0)
