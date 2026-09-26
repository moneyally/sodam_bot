"""하네스 데이터: 오너 메뉴(👑)가 깊은 화면까지 눌리도록 방 여러 개·구독·결제를 넣는다.

harness 가 tests/seed_*.py 를 자동으로 불러온다. 테스트(test_owner_menu)도 같은 데이터를 쓴다.
"""
import time
from datetime import datetime

from telegram.error import Forbidden

import harness
from sodam.billing import UNIT

DAY = 86400
PAYER = "TPayerWalletAddr0000000000000000001"
STRANGER = "TStrangerWalletAddr00000000000000002"
# 봇이 나간 방들: 결제·구독 기록만 남아 있고 텔레그램 권한 조회는 거절된다
# → 그룹 목록·허브에는 안 나오고(다른 역할 크롤 수가 안 늘어남) 오너 현황에만 나온다.
LEFT = {-1005550000000 - i: f"옛 소통방 {i + 1}" for i in range(10)}
PAID_TX, LAST_TX, ODD_TX = "a1" * 32, "b2" * 32, "c3" * 32
PAID_UNITS, LAST_UNITS, ODD_UNITS = 30 * UNIT + 1370, 30 * UNIT + 5500, 29 * UNIT + 500_000


def month_start(tz, back: int = 0) -> int:
    now = datetime.now(tz)
    y, m = now.year, now.month - back
    if m < 1:
        y, m = y - 1, m + 12
    return int(now.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


async def paid_payment(db, cid, tx, units, ts, uid=harness.TG):
    inv = await db.add_invoice(cid, uid, units, ts - 300, ts + 3000)
    assert await db.mark_invoice_paid(inv, tx)
    assert await db.record_payment(tx, inv, cid, units, PAYER, ts)


async def seed(svc):
    db, now = svc.db, int(time.time())
    tz = svc.cfg.tz
    # 메인 방: 구독 중 + 이번 달·지난 달 결제 1건씩
    await db.extend_paid(harness.CHAT, 30 * DAY, now)
    await paid_payment(db, harness.CHAT, PAID_TX, PAID_UNITS, max(now - 3600, month_start(tz)))  # 월초에도 이번 달
    await paid_payment(db, harness.CHAT, LAST_TX, LAST_UNITS, month_start(tz, 1) + 2 * DAY)
    # 청구서와 안 맞는 입금 (금액을 틀리게 보낸 경우)
    await db.record_payment(ODD_TX, None, None, ODD_UNITS, STRANGER, now - 7200)
    for n, (cid, title) in enumerate(LEFT.items()):
        await db.ensure_chat(cid, title)
        await db.start_subscription(cid, now + (n - 5) * DAY + DAY // 2, None)  # 앞 5개는 만료, 뒤 5개는 체험 중

    perms = svc.perms
    orig_admin, orig_tg = perms.is_admin, perms.is_tg_admin

    async def is_admin(bot, cid, uid):
        if cid in LEFT:
            raise Forbidden("bot was kicked from the supergroup chat")
        return await orig_admin(bot, cid, uid)

    async def is_tg_admin(bot, cid, uid):
        if cid in LEFT:
            raise Forbidden("bot was kicked from the supergroup chat")
        return await orig_tg(bot, cid, uid)

    perms.is_admin, perms.is_tg_admin = is_admin, is_tg_admin


harness.SEEDERS.append(seed)
