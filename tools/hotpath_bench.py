"""그룹 메시지 hot path 부하 측정 (오프라인, 가짜 텔레그램 · AI 없음).

실제 handlers.on_group_message + 모든 그룹 메시지 훅(스팸 방패 지문·이상징후·태그 알림·알림 규칙·게임 시간·사기 의심·
공동 차단·social)을 10개 방 × 여러 멤버의 메시지로 돌리고, 메시지당 SQL 문 수·DB 왕복 수·초당 처리 수를 잰다.
권한은 실제 Permissions(관리자 목록 캐시 포함), 결제 켜짐(체험 중), 방 절반은 스팸 방패 기록만 모드.

사용: python tools/hotpath_bench.py [메시지 수=5000] [방 수=10]
"""
import asyncio
import collections
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc  # noqa: E402

from sodam import handlers  # noqa: E402
from sodam.billing import Billing  # noqa: E402
from sodam.moderation import Moderator  # noqa: E402
from sodam.permissions import Permissions  # noqa: E402
from sodam.util import RateLimiter  # noqa: E402

ADMIN = 1
TEXTS = ["안녕하세요", "오늘 시세 어때요?", "USDT 1000개 삽니다 1390원", "ㅋㅋㅋ 그렇네요", "점심 뭐 먹지",
         "유튜브 봤어요 https://youtube.com/watch?v=abc", "@member_{u} 확인 부탁드려요", "네 알겠습니다",
         "거래 완료했습니다 감사합니다", "내일 회의 몇 시죠?", "!출석", "다른 얘기 해요"]


async def build(rooms: int, members: int):
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot(admins=[fake_user(ADMIN, "방장", "boss")])
    svc.perms = Permissions(svc.cfg, db)
    svc.mod = Moderator(svc.cfg, db, svc.perms)
    svc.billing = Billing(svc.cfg, db)
    svc.llm = SimpleNamespace(enabled=False)   # AI 는 부르지 않음 (스팸 방패 판별도 'off')
    chats = [-100_900_000 - i for i in range(rooms)]
    now = int(time.time())
    for n, cid in enumerate(chats):
        await db.ensure_chat(cid, f"방 {n}")
        await db.start_subscription(cid, now + 3 * 86400, ADMIN)
        await db.set_setting(cid, "cas_enabled", False)
        await db.set_setting(cid, "flood_count", 1000)
        if n % 2:
            await db.set_setting(cid, "spamshield_mode", "shadow")
        for u in range(members):
            uid = 1000 + u
            await db.upsert_user(fake_user(uid, f"멤버{u}", f"member_{u}"))
            await db.touch_member(cid, uid, joined=(u % 10 == 0))   # 10명 중 1명은 방금 들어온 사람
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "limiter": RateLimiter(), "chats": set(), "cas_seen": set(),
                                    "tasks": set(), "joins": {}})
    return db, svc, bot, ctx, chats


class Counter:
    """SQL 문(trace callback)·DB 스레드 왕복(aiosqlite _execute) 수."""

    def __init__(self, db):
        self.sql = 0
        self.trips = 0
        self.kinds: collections.Counter = collections.Counter()
        conn = db.conn
        orig = conn._execute

        async def counted(fn, *a, **kw):
            self.trips += 1
            return await orig(fn, *a, **kw)
        conn._execute = counted
        self._raw = conn._conn

    async def start(self, db):
        def trace(stmt):
            self.sql += 1
            self.kinds[" ".join(stmt.split()[:4])[:70]] += 1
        await db.conn._execute(self._raw.set_trace_callback, trace)


async def run(n_msgs: int = 5000, rooms: int = 10, members: int = 40, seed: int = 7, top: int = 0):
    rnd = random.Random(seed)
    db, svc, bot, ctx, chats = await build(rooms, members)
    cnt = Counter(db)
    await cnt.start(db)
    cnt.sql = cnt.trips = 0
    cnt.kinds.clear()
    t0 = time.perf_counter()
    for i in range(n_msgs):
        cid = rnd.choice(chats)
        u = rnd.randrange(members)
        text = rnd.choice(TEXTS).format(u=rnd.randrange(members))
        m = FakeMsg(cid, fake_user(1000 + u, f"멤버{u}", f"member_{u}"), text, message_id=10_000 + i)
        m.chat = SimpleNamespace(id=cid, title="방", type="supergroup")
        m.sender_chat = None
        m.date = datetime.now(timezone.utc)
        await handlers.on_group_message(SimpleNamespace(message=m), ctx)
        await asyncio.gather(*list(ctx.bot_data["tasks"]), return_exceptions=True)
    dt = time.perf_counter() - t0
    out = {"msgs": n_msgs, "sec": dt, "msg_per_sec": n_msgs / dt, "sql_per_msg": cnt.sql / n_msgs,
           "trips_per_msg": cnt.trips / n_msgs, "kinds": cnt.kinds.most_common(top)}
    await db.close()
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    r = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    res = asyncio.run(run(n, r, top=25))
    print(f"{res['msgs']} msgs in {res['sec']:.1f}s → {res['msg_per_sec']:.0f} msg/s · "
          f"SQL {res['sql_per_msg']:.1f}/msg · DB 왕복 {res['trips_per_msg']:.1f}/msg")
    for k, v in res["kinds"]:
        print(f"{v / res['msgs']:6.2f}/msg  {k}")
