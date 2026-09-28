"""🔎 한국어 전문 검색: python tests/run_all.py search

2음절 낱말·부분 일치·여러 낱말 관련도 순·방 구분·검색어 안전·보관 정리와 색인 동기·예전 기록 색인 채우기·
방 자료(knowledge) 검색·.검색 명령 실제 경로·2만 개에서의 속도.
"""
import time

from fake_llm import Room
from fakes import fake_user, make_db, runner

from sodam import knowledge
from sodam.db import DB
from sodam.search import grams, match_query

test, run_all = runner()
A, B = -1001, -1002


async def seed(db, chat, texts, ts=None):
    for i, t in enumerate(texts):
        await db.log_message(chat, 10 + i, i, t, ts=ts)


async def found(db, chat, q, since=0):
    return [r["text"] for r in await db.search_messages(chat, q, since, 10)]


@test
async def grams_and_safe_query():
    assert grams("원두값 올랐어요!") == ["원두", "두값", "올랐", "랐어", "어요"]
    assert grams("돈 USDT") == ["돈", "us", "sd", "dt"]
    assert grams("ｕｓｄｔ") == ["us", "sd", "dt"]                        # 전각 → 반각 (NFKC)
    q, n = match_query('원두 "x" OR a:* -- 돈')
    assert q == '"원두" OR "or"' and n == 2, q                         # 따옴표·연산자·1글자는 사라짐
    assert match_query("가 ! ?") == (None, 0)


@test
async def two_syllable_substring_ranked_and_scoped():
    db = await make_db()
    await seed(db, A, ["원두값 올랐어요", "커피콩 도매가", "원두 도매 어디서 사요", "환불 문의요"])
    await seed(db, B, ["원두 다른방"])
    assert await found(db, A, "원두") == ["원두 도매 어디서 사요", "원두값 올랐어요"]   # 2음절, 최신순, 다른 방 제외
    assert await found(db, A, "두값") == ["원두값 올랐어요"]                          # 낱말 중간
    assert (await found(db, A, "원두 도매"))[0] == "원두 도매 어디서 사요"             # 둘 다 맞는 게 먼저
    assert set(await found(db, A, "원두 도매")) == {"원두 도매 어디서 사요", "원두값 올랐어요", "커피콩 도매가"}
    assert await found(db, A, "원") == [] and await found(db, A, "콩도매") == []       # 1글자·붙여쓰기 다른 말
    await db.flag_message(A, 3)                                                       # 인젝션 판정 메시지 제외
    assert "환불 문의요" not in await found(db, A, "환불")


@test
async def since_filter_and_prune_keeps_index_in_sync():
    db = await make_db()
    now = int(time.time())
    await seed(db, A, ["옛날 원두 얘기"], ts=now - 100 * 86400)
    await seed(db, A, ["요즘 원두 얘기"], ts=now - 86400)
    assert await found(db, A, "원두", since=now - 7 * 86400) == ["요즘 원두 얘기"]
    await db.prune(now)
    n_msg = (await db._one("SELECT COUNT(*) AS n FROM messages WHERE is_bot=0"))["n"]
    n_fts = (await db._one("SELECT COUNT(*) AS n FROM messages_fts"))["n"]
    assert n_msg == n_fts == 1, (n_msg, n_fts)


@test
async def old_db_backfilled_on_open():
    db = await make_db()
    await db._write("INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged) "
                    "VALUES(?, 1, 1, '예전 버전 원두 기록', ?, 0, 0)", (A, int(time.time())))   # 색인 없던 시절 기록
    await db.close()
    db2 = DB(db.path)
    await db2.open()
    try:
        assert await found(db2, A, "원두") == ["예전 버전 원두 기록"]
        await db2.close()
        db3 = DB(db.path)
        await db3.open()                                                           # 두 번 열어도 중복 색인 없음
        assert (await db3._one("SELECT COUNT(*) AS n FROM messages_fts"))["n"] == 1
        await db3.close()
    finally:
        if db2.conn:
            await db2.close()


@test
async def knowledge_search_uses_index_and_delete_cleans():
    db = await make_db()
    doc, _, _ = await knowledge.add_document(db, A, "운영 안내", "환불 규정: 구매 7일 이내면 전액 돌려드려요.\n"
                                                              "모임은 매주 토요일 오후 3시.", "test", 1)
    res = await knowledge.search(db, A, "환불은 어떻게 해요?")
    assert res and "7일 이내" in res[0]["content"], res
    assert not await knowledge.search(db, B, "환불"), "다른 방 자료는 안 보임"
    await db.delete_knowledge(A, doc)
    assert not await knowledge.search(db, A, "환불")
    assert (await db._one("SELECT COUNT(*) AS n FROM knowledge_fts"))["n"] == 0


@test
async def search_command_end_to_end():
    r = await Room().open(admins={1}, settings={"injection_guard": False})
    await r.say(fake_user(20, "김대표"), "원두 도매처 좋은 곳 있나요")
    await r.say(fake_user(21, "박대표"), "오늘 날씨 좋네요")
    m = await r.say(fake_user(22, "이대표"), ".검색 원두")
    assert "원두 도매처" in m.replies[-1] and "날씨" not in m.replies[-1], m.replies


@test
async def fast_on_20k_messages():
    db = await make_db()
    words = ["원두", "도매", "환불", "모임", "테더", "시세", "배송", "가격", "문의", "입금"]
    rows = [(A if i % 3 else B, i % 50, i, f"{words[i % 10]} 관련 얘기 {i} {words[(i * 7) % 10]}", 1_700_000_000 + i)
            for i in range(20000)]

    def bulk(c):
        from sodam.search import index_text
        for r in rows:
            mid = c.execute("INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged) "
                            "VALUES(?,?,?,?,?,0,0)", r).lastrowid
            c.execute("INSERT INTO messages_fts(rowid, body) VALUES(?, ?)", (mid, index_text(r[3])))
    await db.atomic(bulk)
    t = time.perf_counter()
    for q in ["원두", "환불 배송", "테더 시세", "가격"]:
        assert await db.search_messages(A, q, 0, 10)
    per = (time.perf_counter() - t) / 4
    print(f"    2만 개 중 검색 1번 평균 {per * 1000:.1f}ms")
    assert per < 0.5


if __name__ == "__main__":
    run_all()
