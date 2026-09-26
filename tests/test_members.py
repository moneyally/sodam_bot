"""👥 멤버 목록: python tests/test_members.py"""
import asyncio
import sys
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from harness import html_errors
from telegram.error import TimedOut

from sodam import commands, handlers, menu
from sodam.commands import CmdCtx
from sodam.panels import members as M
from sodam.permissions import Role

test, run_all = runner()
CHAT = -1005550000009


async def setup(n=40):
    db = await make_db()
    svc = await make_svc(db, admins={1})
    await db.ensure_chat(CHAT, "테스트방")
    now = int(time.time())
    for i in range(n):
        u = fake_user(1000 + i, f"멤버{i:03d}", f"m{i:03d}")
        await db.upsert_user(u)
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                        (CHAT, u.id, now - 86400 * (n - i), now - 60 * i))
    for i in range(5):                                                  # 1005 번이 제일 수다쟁이
        await db.log_message(CHAT, 1005, None, f"안녕{i}")
    await db.log_message(CHAT, 1007, None, "hi")
    M._count_cache.clear()
    M._msg_cache.clear()
    M._csv_last.clear()
    bot = FakeBot(admins=[fake_user(1, "방장")])
    return db, svc, bot


async def press(svc, bot, data, uid=1):
    svc.menu_limiter._hits.clear()
    q = FakeQuery(uid, fake_user(uid, "방장"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


@test
async def paging_sorting_and_bounds():
    db, svc, bot = await setup(40)
    q = await press(svc, bot, f"m:mb:{CHAT}")
    t = q.edits[-1]
    assert "1/3쪽" in t and "텔레그램 기준 <b>100명</b>" in t and "소담이 본 사람 <b>40명</b>" in t
    assert t.index("멤버000") < t.index("멤버001") and not html_errors(t)   # 최근 활동순
    q = await press(svc, bot, f"m:mb:{CHAT}:seen:2")
    assert "3/3쪽" in q.edits[-1] and "멤버039" in q.edits[-1]
    for bad in ("99999", "-3", "x", "²", "9" * 30):                      # 이상한 쪽 번호 → 범위 안으로
        q = await press(svc, bot, f"m:mb:{CHAT}:seen:{bad}")
        assert q.edits and "쪽)" in q.edits[-1], bad
    q = await press(svc, bot, f"m:mb:{CHAT}:msg:0")                      # 메시지 많은 순
    lines = [ln for ln in q.edits[-1].splitlines() if ln[:2].rstrip(".").isdigit()]
    assert "멤버005" in lines[0] and "💬5" in q.edits[-1]
    q = await press(svc, bot, f"m:mb:{CHAT}:join:0")                     # 입장순 (오래된 사람 먼저)
    assert q.edits[-1].index("멤버000") < q.edits[-1].index("멤버010")
    kb = [b for row in q.kb.inline_keyboard for b in row]
    assert all(len(b.callback_data.encode()) <= 64 for b in kb)


@test
async def left_members_are_hidden_and_rejoin_restores():
    db, svc, bot = await setup(5)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc, "chats": set(), "joins": {}})
    m = FakeMsg(CHAT, fake_user(1002, "멤버002"), "")
    m.left_chat_member = fake_user(1002, "멤버002")
    await handlers.on_left(SimpleNamespace(message=m), ctx)
    q = await press(svc, bot, f"m:mb:{CHAT}")
    assert "멤버002" not in q.edits[-1] and "소담이 본 사람 <b>4명</b>" in q.edits[-1]
    await db.set_setting(CHAT, "captcha_enabled", False)
    await handlers.handle_new_member(ctx, CHAT, "테스트방", fake_user(1002, "멤버002"))
    q = await press(svc, bot, f"m:mb:{CHAT}")
    assert "멤버002" in q.edits[-1]


@test
async def only_admins_can_open():
    db, svc, bot = await setup(3)
    q = await press(svc, bot, f"m:mb:{CHAT}", uid=1000)
    assert "관리자만" in q.answers[0][0] and not q.edits
    admin, member = fake_user(1, "방장"), fake_user(1000, "멤버")
    for u, role in ((member, Role.MEMBER), (admin, Role.ADMIN)):
        c, args, argstr = commands.parse(".멤버", "sodambot")
        msg = FakeMsg(CHAT, u, ".멤버")
        await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, u, role, args, argstr), c)
    dms = [x for x in bot.named("send_message") if x[1] == 1]
    assert dms and "멤버 목록" in dms[-1][2]                             # 명단은 방이 아니라 관리자 1:1 로
    assert not [x for x in bot.named("send_message") if x[1] == CHAT and "멤버000" in x[2]]


@test
async def telegram_timeouts_do_not_break_screen_and_count_is_cached():
    db, svc, bot = await setup(3)

    async def slow_count(chat_id):
        bot.calls.append(("count",))
        raise TimedOut()
    bot.get_chat_member_count = slow_count
    q = await press(svc, bot, f"m:mb:{CHAT}")
    assert "텔레그램 기준 <b>?명</b>" in q.edits[-1] and "멤버000" in q.edits[-1]
    bot2 = FakeBot()
    await press(svc, bot2, f"m:mb:{CHAT}")
    await press(svc, bot2, f"m:mb:{CHAT}:seen:0")
    assert len(bot2.named("get_chat_member_count")) == 1                 # 10분 캐시
    q = await press(svc, bot2, f"m:mbr:{CHAT}:seen")                     # 🔄 새로고침은 캐시 비움
    assert len(bot2.named("get_chat_member_count")) == 2 and "새로" in q.answers[0][0]


@test
async def search_by_name_username_and_id():
    db, svc, bot = await setup(20)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    for text, expect in (("멤버01", "멤버010"), ("@m007", "멤버007"), ("1003", "멤버003"), ("없는사람", "없어요")):
        await press(svc, bot, f"m:in:{CHAT}:mbq")
        msg = FakeMsg(1, fake_user(1, "방장"), text)
        await handlers.on_private(SimpleNamespace(message=msg), ctx)
        assert expect in msg.replies[-1], (text, msg.replies[-1][:80])
    await press(svc, bot, f"m:in:{CHAT}:mbq")
    msg = FakeMsg(1, fake_user(1, "방장"), "<b>&")
    await handlers.on_private(SimpleNamespace(message=msg), ctx)
    assert not html_errors(msg.replies[-1])


@test
async def csv_export_to_dm_with_rate_limit():
    db, svc, bot = await setup(12)
    q = await press(svc, bot, f"m:mbx:{CHAT}")
    doc = bot.named("send_document")[-1]
    assert doc[1] == 1 and "12명" in doc[3] and "1:1" in q.answers[0][0]
    data = doc[2].input_file_content.decode("utf-8-sig")
    assert data.splitlines()[0].startswith("user_id,name,username") and "멤버005" in data and len(data.splitlines()) == 13
    q = await press(svc, bot, f"m:mbx:{CHAT}")
    assert "1분" in q.answers[0][0] and len(bot.named("send_document")) == 1
    assert (await db.recent_mod_log(CHAT))[0]["action"] == "export"


@test
async def big_room_stays_fast():
    """2만 명 방에서도 한 쪽 조회가 빠르게 (인덱스·LIMIT)."""
    db = await make_db()
    svc = await make_svc(db, admins={1})
    await db.ensure_chat(CHAT, "큰방")
    now = int(time.time())
    await db.conn.executemany("INSERT INTO users(user_id, first_name, is_bot, updated_at) VALUES(?,?,0,?)",
                              [(10_000 + i, f"u{i}", now) for i in range(20_000)])
    await db.conn.executemany("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                              [(CHAT, 10_000 + i, now - i, now - i) for i in range(20_000)])
    await db.conn.executemany("INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged) VALUES(?,?,?,?,?,0,0)",
                              [(CHAT, 10_000 + (i % 20_000), i, "x", now - i) for i in range(60_000)])
    await db.conn.commit()
    bot = FakeBot(admins=[fake_user(1, "방장")])
    M._msg_cache.clear()
    for data in (f"m:mb:{CHAT}", f"m:mb:{CHAT}:seen:1000", f"m:mb:{CHAT}:msg:0", f"m:mb:{CHAT}:msg:5"):
        t0 = time.perf_counter()
        q = await press(svc, bot, data)
        took = time.perf_counter() - t0
        assert q.edits and took < 2.0, (data, took)
    assert "1334쪽" in q.edits[-1]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
