"""👑 오너 메뉴 점검 (방 현황·기간 부여·매출): python tests/test_owner_menu.py"""
import asyncio
import sys
import time

import harness
import seed_owner as seed
from fakes import runner
from harness import BOTADM, CHAT, MEMBER, OWNER, TG, make_world, press

from sodam import menu
from sodam.billing import UNIT, fmt_usdt
from sodam.panels import owner

test, run_all = runner()
DAY = 86400
OWNER_CODES = ["m:o", "m:ol:0", "m:ol:1", f"m:oc:{CHAT}:0", f"m:og:{CHAT}:30:0", "m:os"]
OWNER_ROUTES = {"o", "ol", "oc", "og", "os"}
SECRETS = [fmt_usdt(u) for u in (seed.PAID_UNITS, seed.LAST_UNITS, seed.ODD_UNITS)] + ["매출", "오너 메뉴"]


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row] if kb else []


def find(kb, text):
    return next(b for b in buttons(kb) if text in b.text)


async def world():
    db, svc, bot = await make_world()
    return db, svc, bot


async def paid_until(db, cid):
    return (await db.get_subscription(cid))["paid_until"]


@test
async def main_menu_item_only_for_owner():
    db, svc, bot = await world()
    _, kb = await menu.main_menu(svc, bot, OWNER)
    assert any(b.callback_data == "m:o" for b in buttons(kb))
    for uid in (TG, BOTADM, MEMBER):
        _, kb = await menu.main_menu(svc, bot, uid)
        assert not any((b.callback_data or "").startswith("m:o") for b in buttons(kb)), uid


@test
async def forged_owner_callbacks_rejected():
    db, svc, bot = await world()
    before = await paid_until(db, CHAT)
    for uid in (TG, BOTADM, MEMBER):
        for data in OWNER_CODES:
            svc.menu_limiter._hits.clear()
            q = await press(svc, bot, uid, data)
            assert not q.edits and len(q.answers) == 1 and q.answers[0][1], (uid, data, q.answers)
            assert "오너" in q.answers[0][0]
    assert not [c for c in bot.calls if c[0] == "send_message"]
    assert await paid_until(db, CHAT) == before


@test
async def grant_token_is_owner_only_and_rechecked():
    db, svc, bot = await world()
    before = await paid_until(db, CHAT)
    q = await press(svc, bot, OWNER, f"m:og:{CHAT}:30:0")
    tok = find(q.edits[-1][1], "30일 부여").callback_data
    # 다른 사람이 그 토큰을 눌러도 안 됨 (토큰은 만든 사람 것)
    q = await press(svc, bot, TG, tok)
    assert not q.edits and q.answers[0][1]
    assert await paid_until(db, CHAT) == before
    # 오너가 확인 화면을 띄운 뒤 오너에서 빠지면 토큰도 무효 (새로 권한 확인)
    q = await press(svc, bot, OWNER, f"m:og:{CHAT}:30:0")
    tok = find(q.edits[-1][1], "30일 부여").callback_data

    async def nobody():
        return set()
    svc.perms.owners = nobody
    q = await press(svc, bot, OWNER, tok)
    assert not q.edits and q.answers[0][1]
    assert await paid_until(db, CHAT) == before


@test
async def room_list_pagination_bounds():
    db, svc, bot = await world()
    rooms = 1 + len(seed.LEFT)  # 11개 → 10 + 1
    q = await press(svc, bot, OWNER, "m:ol:0")
    kb = q.edits[-1][1]
    assert len([b for b in buttons(kb) if b.callback_data.startswith("m:oc:")]) == owner.PAGE_SIZE
    assert find(kb, "다음").callback_data == "m:ol:1"
    assert not any("이전" in b.text for b in buttons(kb))
    assert f"{rooms}개 · 1/2쪽" in q.edits[-1][0]
    q = await press(svc, bot, OWNER, "m:ol:1")
    kb = q.edits[-1][1]
    assert len([b for b in buttons(kb) if b.callback_data.startswith("m:oc:")]) == rooms - owner.PAGE_SIZE
    assert find(kb, "이전").callback_data == "m:ol:0" and not any("다음" in b.text for b in buttons(kb))
    # 없는 쪽은 마지막 쪽으로
    q = await press(svc, bot, OWNER, "m:ol:999")
    assert "2/2쪽" in q.edits[-1][0]
    # 쪽 번호 위조: 숫자 아님·음수·너무 큼·유니코드 숫자 → 아무 변화 없음 (answer 는 1번)
    for bad in ("m:ol:abc", "m:ol:-1", "m:ol:1000", "m:ol:²", "m:ol:1e3", f"m:oc:{CHAT}:x",
                f"m:oc:{CHAT}:99999", "m:oc:-1:0", "m:oc:abc:0", "m:oc:-1009999999:0",
                f"m:og:{CHAT}:31:0", f"m:og:{CHAT}:-30:0", f"m:og:{CHAT}:30:x"):
        svc.menu_limiter._hits.clear()
        q = await press(svc, bot, OWNER, bad)
        assert not q.edits and len(q.answers) == 1, (bad, q.answers)


@test
async def grant_30_extends_after_remaining_and_token_single_use():
    db, svc, bot = await world()
    before = await paid_until(db, CHAT)
    assert before > time.time() + 29 * DAY  # 시드: 구독 30일 남음
    q = await press(svc, bot, OWNER, f"m:og:{CHAT}:30:0")
    assert "30일" in q.edits[-1][0] and "늘릴까요" in q.edits[-1][0]
    assert await paid_until(db, CHAT) == before  # 확인 화면만으로는 안 바뀜
    tok = find(q.edits[-1][1], "30일 부여").callback_data
    q = await press(svc, bot, OWNER, tok)
    assert await paid_until(db, CHAT) == before + 30 * DAY  # 남은 기간 뒤로 이어서
    assert "방 상세" in q.edits[-1][0] and "부여" in q.answers[0][0]
    log = await db.recent_mod_log(CHAT, 1)
    assert log[0]["action"] == "sub_grant" and log[0]["actor_id"] == OWNER and "30일" in log[0]["detail"]
    assert any(c[0] == "send_message" and c[1] == OWNER and "[기간 부여]" in c[2] for c in bot.calls)
    # 같은 토큰 다시 → 만료 안내, 기간 그대로
    q = await press(svc, bot, OWNER, tok)
    assert q.answers[0][1] and not q.edits
    assert await paid_until(db, CHAT) == before + 30 * DAY
    expired, trial = max(seed.LEFT), min(seed.LEFT)  # 시드: 첫 방은 만료, 마지막 방은 체험 중
    now = int(time.time())
    for cid in (expired, trial):
        q = await press(svc, bot, OWNER, f"m:og:{cid}:7:0")
        await press(svc, bot, OWNER, find(q.edits[-1][1], "7일 부여").callback_data)
    assert now + 7 * DAY - 5 <= await paid_until(db, expired) <= now + 7 * DAY + 5  # 만료된 방은 지금부터
    trial_until = (await db.get_subscription(trial))["trial_until"]
    assert trial_until > now and await paid_until(db, trial) == trial_until + 7 * DAY  # 체험 끝난 뒤로


@test
async def month_totals_and_unmatched():
    db, svc, bot = await world()
    now = int(time.time())
    # 청구서 경합으로 반영 못 한 입금(청구서 번호는 있지만 청구서가 이 tx 로 결제 안 됨)도 '안 맞는 입금'
    inv = await db.add_invoice(CHAT, TG, 30 * UNIT + 777, now - 600, now + 600)
    await db.record_payment("d4" * 32, inv, CHAT, 30 * UNIT + 777, seed.PAYER, max(now - 60, seed.month_start(svc.cfg.tz)))
    # 두 달 전 결제는 어느 달에도 안 들어감
    await seed.paid_payment(db, CHAT, "e5" * 32, 99 * UNIT, seed.month_start(svc.cfg.tz, 2) + DAY)
    c = menu.PanelCtx(svc, bot, OWNER, None, [])
    this, _ = owner._month_start(c)
    last, _ = owner._month_start(c, 1)
    assert await owner.payments_summary(c, this) == (seed.PAID_UNITS, 1)
    assert await owner.payments_summary(c, last, this) == (seed.LAST_UNITS, 1)
    rows, n, total = await owner.unmatched_payments(c)
    assert n == 2 and total == seed.ODD_UNITS + 30 * UNIT + 777
    q = await press(svc, bot, OWNER, "m:os")
    text = q.edits[-1][0]
    assert fmt_usdt(seed.PAID_UNITS) in text and fmt_usdt(seed.LAST_UNITS) in text
    assert "청구서와 안 맞는 입금</b> 2건" in text
    q = await press(svc, bot, OWNER, "m:o")
    assert f"{fmt_usdt(seed.PAID_UNITS)} USDT</b> (1건)" in q.edits[-1][0] and "2건" in q.edits[-1][0]


@test
async def amounts_never_shown_to_non_owners():
    rep = harness.Report()
    for uid in (TG, BOTADM, MEMBER):
        db, svc, bot = await make_world()
        await harness.crawl_as(svc, bot, uid, rep, ["m:home", "m:groups", f"m:g:{CHAT}"] + OWNER_CODES)
    assert not rep.issues, rep.issues[:5]
    assert rep.shots
    for s in rep.shots:
        assert s.data.split(":")[1] not in OWNER_ROUTES, (s.persona, s.data)  # 오너 화면이 한 번도 안 열림
        for secret in SECRETS:
            assert secret not in (s.text or ""), (s.persona, s.data, secret)
        for b in buttons(s.kb):
            assert (b.callback_data or "m:x").split(":")[1] not in OWNER_ROUTES, (s.persona, s.data, b.callback_data)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
