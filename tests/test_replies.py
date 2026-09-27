"""↩ 답장 관계 저장: python tests/run_all.py replies

예전 DB 에 컬럼 추가(행·검색 색인 그대로) · 그룹/1:1/봇 답 기록 · 포럼 토픽·채널 글 답장 제외 ·
AI chat_log '↩이름' · 방 흐름 요약 입력 · reply_stats(누가 누구에게 몇 번).
"""
import os
import sqlite3
import tempfile
from datetime import timezone
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeMsg, fake_user, make_db, runner

from sodam import handlers, memory
from sodam.db import DB
from sodam.prompt import _line, reply_mark

test, run_all = runner()
ALICE = fake_user(10, "김민지", "minji")
BOB = fake_user(20, "박준호", "junho")
CAROL = fake_user(30, "이수진", "sujin")
C = -100777


def user_msg(call) -> str:
    return [m for m in call["messages"] if m["role"] == "user"][-1]["content"]


@test
async def migrate_old_schema_keeps_rows_and_index():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    con = sqlite3.connect(path)   # 답장 컬럼이 없던 예전 messages
    con.executescript("""
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, msg_id INTEGER, text TEXT NOT NULL, ts INTEGER NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0);
        INSERT INTO messages(chat_id, user_id, msg_id, text, ts) VALUES(-1, 10, 5, '원두 가격 올랐어요', 1000);
    """)
    con.commit()
    con.close()
    for _ in range(2):   # 두 번 열어도 (이미 있는 컬럼) 괜찮아야 함
        db = DB(path)
        await db.open()
        cols = {r["name"] for r in await db._all("PRAGMA table_info(messages)")}
        assert {"reply_to_msg_id", "reply_to_user"} <= cols, cols
        rows = await db.recent_messages(-1, 10)
        assert rows[0]["text"] == "원두 가격 올랐어요"
        assert rows[0]["reply_to_user"] is None and reply_mark(rows[0]) == ""
        assert "원두 가격 올랐어요" in [r["text"] for r in await db.search_messages(-1, "원두", 0)]
        await db.log_message(-1, 20, 6, "원두 어디서 사요?", reply_to_msg_id=5, reply_to_user=10)
        await db.close()
    db = DB(path)
    await db.open()
    rows = await db.recent_messages(-1, 10)
    assert [(r["reply_to_msg_id"], r["reply_to_user"]) for r in rows] == [(None, None), (5, 10), (5, 10)]
    assert len(await db.search_messages(-1, "원두", 0)) == 3          # 새 글도 색인 (rowid = messages.id)
    await db.close()


@test
async def group_reply_recorded_and_rendered_in_chat_log():
    r = await Room().open(admins={1})
    for u in (ALICE, BOB, CAROL):
        await r.join(u)
    a = await r.say(ALICE, "오늘 원두 들어왔어요")
    b = await r.say(BOB, "얼마에 받으셨어요?", reply_to=a)
    r.llm.script = ["네 알려드릴게요"]
    c = await r.say(CAROL, "소담아 둘이 무슨 얘기야?", reply_to=b)
    rows = {x["msg_id"]: x for x in await r.db.recent_messages(C, 20)}
    assert rows[a.message_id]["reply_to_user"] is None
    assert (rows[b.message_id]["reply_to_msg_id"], rows[b.message_id]["reply_to_user"]) == (a.message_id, ALICE.id)
    assert rows[c.message_id]["reply_to_user"] == BOB.id
    bot_row = next(x for x in rows.values() if x["user_id"] == r.bot.id)          # 봇 답 = 부른 사람에게 한 답장
    assert (bot_row["reply_to_msg_id"], bot_row["reply_to_user"]) == (c.message_id, CAROL.id)
    log = user_msg(r.llm.of("chat")[0])
    assert "박준호(20) ↩김민지: 얼마에 받으셨어요?" in log, log
    assert "김민지(10): 오늘 원두" in log                                          # 답장 아니면 표시 없음
    r.llm.script = ["네"]
    await r.say(ALICE, "소담아 고마워")
    log = user_msg(r.llm.of("chat")[-1])
    assert "소담(봇) ↩이수진: 네 알려드릴게요" in log, log


@test
async def reply_to_bot_and_unknown_user_names():
    r = await Room().open(admins={1})
    await r.join(ALICE)
    botmsg = FakeMsg(C, r.bot_user(), "안녕하세요", message_id=50)
    stranger = FakeMsg(C, fake_user(77, "처음본사람"), "저 왔어요", message_id=51)   # 말한 적 없는 사람
    await r.say(ALICE, "반가워요", reply_to=botmsg)
    await r.say(ALICE, "누구세요?", reply_to=stranger)
    rows = await r.db.recent_messages(C, 10)
    assert [reply_mark(x, r.bot.id, "소담") for x in rows] == [" ↩소담", " ↩처음본사람"]
    assert reply_mark(rows[0]) == " ↩소담"          # bot_id 를 몰라도(방 요약) 이름이 users 에 있음


@test
async def topic_root_and_channel_post_are_not_person_replies():
    root = SimpleNamespace(message_id=7, from_user=fake_user(5, "만든이"), sender_chat=None, forum_topic_created=None)
    m = FakeMsg(C, ALICE, "토픽 안 글", reply_to=root)
    m.is_topic_message, m.message_thread_id = True, 7
    assert handlers.reply_ref(m) == (None, None)
    m.message_thread_id = 3                                         # 토픽 안에서 진짜 답장
    assert handlers.reply_ref(m) == (7, 5)
    chan = SimpleNamespace(message_id=8, from_user=fake_user(136817688, "Channel", is_bot=True),
                           sender_chat=SimpleNamespace(id=-100555))
    assert handlers.reply_ref(FakeMsg(C, ALICE, "채널 글에 답", reply_to=chan)) == (8, None)
    assert handlers.reply_ref(FakeMsg(C, ALICE, "그냥 글")) == (None, None)


@test
async def dm_message_records_reply():
    r = await Room().open()
    botmsg = FakeMsg(ALICE.id, r.bot_user(), "무엇을 도와드릴까요?", message_id=300)
    m = FakeMsg(ALICE.id, ALICE, "이거 다시 설명해줘", message_id=301, reply_to=botmsg)
    m.chat = SimpleNamespace(id=ALICE.id, type="private", title=None)
    m.sender_chat, m.forward_origin = None, None
    r.llm.script = ["네"]
    await handlers.on_private(SimpleNamespace(message=m), r.ctx)
    row = await r.db._one("SELECT reply_to_msg_id, reply_to_user FROM messages WHERE msg_id=301")
    assert (row["reply_to_msg_id"], row["reply_to_user"]) == (300, r.bot.id)


@test
def line_marker_is_short_and_safe():
    row = {"ts": 0, "is_bot": 0, "user_id": 10, "first_name": "김민지", "username": None, "text": "응",
           "reply_to_user": 20, "reply_first": "아주아주아주긴이름\n줄바꿈있는사람이름입니다", "reply_username": None}
    line = _line(row, timezone.utc, 999, "소담")
    assert line.endswith("김민지(10) ↩아주아주아주긴이름 줄바꿈있는사람이름입: 응"), line   # 20자로 자름
    assert reply_mark({**row, "reply_first": None, "reply_username": "jh"}) == " ↩@jh"
    assert reply_mark({**row, "reply_first": None}) == " ↩20"
    assert reply_mark({**row, "reply_to_user": 999}, 999, "소담") == " ↩소담"
    old = {k: v for k, v in row.items() if not k.startswith("reply")}  # 예전 조회 결과(컬럼 없음)
    assert _line(old, timezone.utc, 999, "소담").endswith("김민지(10): 응")


@test
async def room_memory_input_has_reply_marks():
    r = await Room().open()
    for u in (ALICE, BOB):
        await r.join(u)
    a = await r.say(ALICE, "내일 모임 몇 시예요?")
    await r.say(BOB, "7시요", reply_to=a)
    seen = []
    r.llm.json_script = {"room_memory": [lambda user: seen.append(user) or {"summary": "모임 시간 얘기"}]}
    assert await memory.refresh_room(r.svc, C, force=True)
    assert "박준호(20) ↩김민지: 7시요" in seen[0], seen[0]
    assert "김민지(10): 내일 모임" in seen[0]


@test
async def reply_stats_counts_pairs():
    db = await make_db()
    for u in (ALICE, BOB, CAROL):
        await db.upsert_user(u)
    log = [(10, 20), (10, 20), (20, 10), (30, 10), (10, 10), (10, None)]   # (보낸이, 답장받은이)
    for i, (frm, to) in enumerate(log):
        await db.log_message(C, frm, i, "글", ts=2000, reply_to_msg_id=i - 1 if to else None, reply_to_user=to)
    await db.log_message(C, 999, 99, "봇 답", is_bot=True, ts=2000, reply_to_msg_id=1, reply_to_user=10)
    await db.log_message(C, 20, 100, "예전 답", ts=100, reply_to_msg_id=1, reply_to_user=10)
    await db.log_message(-5, 20, 1, "다른 방", ts=2000, reply_to_msg_id=1, reply_to_user=10)
    rows = await db.reply_stats(C, since=1000)
    got = [(x["from_id"], x["to_id"], x["n"]) for x in rows]
    assert got == [(10, 20, 2), (20, 10, 1), (30, 10, 1)], got               # 봇·자기 답장·기간 밖·다른 방 제외
    assert (rows[0]["from_first"], rows[0]["to_first"]) == ("김민지", "박준호")
    mine = [(x["from_id"], x["to_id"], x["n"]) for x in await db.reply_stats(C, 1000, user_id=30)]
    assert mine == [(30, 10, 1)]
    assert len(await db.reply_stats(C, 0, user_id=20)) == 2                   # 보낸·받은 둘 다
    assert await db.reply_stats(C, 0, limit=1) and len(await db.reply_stats(C, 0, limit=1)) == 1


if __name__ == "__main__":
    run_all()
