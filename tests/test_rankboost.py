"""🚀 채팅 집계 부스트 구간 (rankboost.py · db.count_filter). 2026-10-31 뉴월드 NASA:
평소 5글자인데 잠깐 2글자 부스트 → 되돌려도 그 기간 친 짧은 글은 순위에 영구 보존.
python tests/run_all.py rankboost"""
import time

from fakes import fake_user, make_db, make_svc, runner

from sodam import rankboost, stats
from sodam.panels import rankboost as P

test, run_all = runner()
CHAT = -1007777
A = 1


async def setup(base_min=5):
    rankboost._CACHE.clear()   # 모듈 전역 캐시가 테스트 간 샘 → 매번 비움
    db = await make_db()
    await db.ensure_chat(CHAT, "뉴월드")
    await db.upsert_user(fake_user(A, "가나"))
    await db.set_setting(CHAT, "chat_min_chars", base_min)
    return db


async def counts(db, uid=A):
    """지금 설정·부스트 기준으로 이 사람 집계 수."""
    rule = await stats.min_chars(db, CHAT)
    rows = await db.top_chatters(CHAT, 0, 10, None, rule)
    return next((r["n"] for r in rows if r["user_id"] == uid), 0)


@test
async def boost_window_keeps_short_msgs_outside_uses_base():
    """부스트 구간 안 글은 그 낮은 기준(2글자), 밖 글은 방 기본(5글자)."""
    db = await setup(base_min=5)
    # 2글자 글 2개(구간 안 ts 1000·1100) · 2글자 1개(구간 밖 ts 3000) · 6글자 1개(구간 밖 ts 3000)
    await db.log_message(CHAT, A, 1, "안녕", ts=1000)
    await db.log_message(CHAT, A, 2, "반가", ts=1100)
    await db.log_message(CHAT, A, 3, "하이", ts=3000)
    await db.log_message(CHAT, A, 4, "오늘날씨좋다", ts=3000)
    # 부스트 없을 때: 기본 5글자 → 6글자 글 1개만
    assert await counts(db) == 1, "부스트 없으면 base 5글자 — 6글자만"
    # 부스트 구간 [900, 2000) min=2 삽입
    await db._write("INSERT INTO rank_boost(chat_id, from_ts, to_ts, min_chars, created_by, created_at) "
                    "VALUES(?,?,?,?,?,?)", (CHAT, 900, 2000, 2, A, 900))
    rankboost._CACHE.clear()
    # 이제: 구간 안 2글자 2개 + 구간 밖 6글자 1개 = 3개 (구간 밖 2글자 '하이'는 base 5 라 빠짐)
    assert await counts(db) == 3, "구간 안 짧은 글 2개 + 밖 긴 글 1개"
    await db.close()


@test
async def boost_on_then_off_preserves_window():
    """NASA 시나리오: 부스트 켜고(열린 구간) 2글자 침 → 끄면(구간 닫힘) 이후 2글자는 안 세지만 켜 둔 동안 친 건 남음.
    (start/stop 이 실제 시각이라 같은 초면 빈 구간 → from_ts 를 넉넉히 앞으로 당겨 검증)."""
    db = await setup(base_min=5)
    ok, _ = await rankboost.start(db, CHAT, 2, A)
    assert ok
    cur = await rankboost.current(db, CHAT)
    assert cur and cur["to_ts"] is None and cur["min_chars"] == 2
    # 켜진 구간 시작을 1000 으로 당겨, 구간 안(ts 1001) 2글자 글
    await db._write("UPDATE rank_boost SET from_ts=1000 WHERE chat_id=?", (CHAT,))
    rankboost._CACHE.clear()
    await db.log_message(CHAT, A, 1, "안녕", ts=1001)
    assert await counts(db) == 1, "부스트 켜진 동안(열린 구간) 2글자 집계됨"
    # 끄기 → 구간 닫힘(to_ts=2000 로 고정)
    ok, _ = await rankboost.stop(db, CHAT)
    assert ok and (await rankboost.current(db, CHAT)) is None
    await db._write("UPDATE rank_boost SET to_ts=2000 WHERE chat_id=?", (CHAT,))
    rankboost._CACHE.clear()
    # 끈 뒤 친 2글자 글(구간 밖 ts 3000) → base 5 라 안 세짐, 켜 둔 동안 친 '안녕'은 남음
    await db.log_message(CHAT, A, 2, "하이", ts=3000)
    assert await counts(db) == 1, "켜 둔 동안 친 '안녕'은 남고, 끈 뒤 '하이'는 안 세짐"
    await db.close()


@test
async def no_boost_room_is_unchanged_fast_path():
    """부스트 구간이 없는 방은 CountRule.boosted=False → 기존 SQL·동작 그대로 (회귀 0)."""
    db = await setup(base_min=3)
    rule = await stats.min_chars(db, CHAT)
    assert rule.boosted is False
    from sodam.db import count_filter
    assert "rank_boost" not in count_filter("messages", rule)   # 부스트 없는 방은 서브쿼리 안 탐
    await db.log_message(CHAT, A, 1, "ㅋㅋ", ts=1000)       # 2글자 < 3 → 안 셈
    await db.log_message(CHAT, A, 2, "안녕하", ts=1000)     # 3글자 → 셈
    assert await counts(db) == 1
    await db.close()


@test
async def start_replaces_open_interval_and_caps():
    """부스트 다시 켜면 진행 중 구간을 닫고 새로. 상한 넘으면 거부."""
    db = await setup()
    await rankboost.start(db, CHAT, 2, A)
    await rankboost.start(db, CHAT, 1, A)   # 다시 켜면 앞 구간 닫고 새로
    opens = await db._all("SELECT * FROM rank_boost WHERE chat_id=? AND to_ts IS NULL", (CHAT,))
    assert len(opens) == 1 and opens[0]["min_chars"] == 1, "진행 중 구간은 하나뿐·최신"
    assert await rankboost.has_boost(db, CHAT) is True
    rankboost._CACHE.clear()
    # 상한
    old = rankboost.MAX_PER_CHAT
    rankboost.MAX_PER_CHAT = 2
    try:
        ok, msg = await rankboost.start(db, CHAT, 3, A)
        assert not ok and "너무 많" in msg
    finally:
        rankboost.MAX_PER_CHAT = old
    await db.close()


@test
async def ai_tool_and_command():
    """AI 도구 chat_boost 와 .채팅부스트 명령이 켜고/끈다."""
    db = await setup()
    svc = await make_svc(db)

    class Ctx:
        pass
    c = Ctx()
    c.svc, c.chat_id, c.caller = svc, CHAT, fake_user(A, "가나")
    out = await P.t_chat_boost(c, {"on": True, "min_chars": 2})
    assert "부스트" in out and (await rankboost.current(db, CHAT)) is not None
    out = await P.t_chat_boost(c, {"on": False})
    assert (await rankboost.current(db, CHAT)) is None

    # 명령: .채팅부스트 2 → 켜기, .채팅부스트 끄기 → 끄기
    replies = []

    class CmdCtx:
        pass
    cc = CmdCtx()
    cc.svc, cc.chat_id, cc.user, cc.argstr = svc, CHAT, fake_user(A, "가나"), "2"
    cc.reply = lambda t, **k: replies.append(t) or _noop()

    async def _noop():
        return None
    await P.c_boost(cc)
    assert (await rankboost.current(db, CHAT)) is not None
    cc.argstr = "끄기"
    await P.c_boost(cc)
    assert (await rankboost.current(db, CHAT)) is None
    await db.close()


if __name__ == "__main__":
    import sys
    sys.exit(1 if __import__("asyncio").run(run_all()) else 0)
