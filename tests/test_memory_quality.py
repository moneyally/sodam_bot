"""멤버 기억 품질 (OpenAI Codex memories 방식 참고, 오프라인 가짜 LLM):
- add_facts 는 한 번의 db.atomic (예전: 삭제·추가를 _write 로 따로 → 중간 실패 시 반쯤 된 기억)
- 쓰인 기억 표시(mark_used, 코드 휴리스틱·비용 0) → 넘치면 덜 쓰인 것 → 오래 안 쓰인 것부터 지움 · 60일 안 쓰이면 만료
- 추출 프롬프트: 명시/추정 태그 · 한 번 한 말 일반화 금지 · 정정은 그 줄을 고침(replaces) · 추정은 '(추정)' 으로 표시
- 예전 DB(컬럼 없는 member_memory)는 열 때 컬럼 추가
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import OPEN_DBS, fake_user, make_db, runner  # noqa: E402

from sodam import memory  # noqa: E402
from sodam.db import DB  # noqa: E402

test, run_all = runner()

CHAT = -1001
U = 10
DAY = 86400
ALICE = fake_user(10, "김민지", "minji")


async def facts(db, chat=CHAT, uid=U) -> list[str]:
    return [r["fact"] for r in await memory.get_facts(db, chat, uid)]


async def room() -> Room:
    r = await Room().open(admins={1})
    await r.join(ALICE)
    return r


# ── 1. 한 번의 atomic ──────────────────────────────────────
@test
async def add_facts_is_all_or_nothing():
    db = await make_db()
    await memory.add_facts(db, CHAT, U, ["부산에서 카페 운영", "등산 좋아함"])
    # 두 번째 새 기억을 넣을 때 DB 가 거절 → 앞의 교체(옛 줄 삭제 + 새 줄)·삭제까지 전부 되돌려야 함
    await db._write("CREATE TRIGGER boom BEFORE INSERT ON member_memory WHEN NEW.fact LIKE '%폭탄%' "
                    "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    first_id = (await memory.get_facts(db, CHAT, U))[1]["id"]
    try:
        await memory.add_facts(db, CHAT, U, ["부산에서 카페 운영 (3년째)", "폭탄 문장"], remove_ids=[first_id])
        raise AssertionError("실패해야 함")
    except sqlite3.IntegrityError:
        pass
    assert await facts(db) == ["부산에서 카페 운영", "등산 좋아함"], await facts(db)
    # 연결이 멀쩡히 다음 쓰기를 함 (SAVEPOINT 정리됨)
    await db._write("DROP TRIGGER boom")
    assert await memory.add_facts(db, CHAT, U, ["요가 배우는 중"]) == ["요가 배우는 중"]


@test
async def clear_facts_still_works():
    db = await make_db()
    await memory.add_facts(db, CHAT, U, ["카페 운영", "요가 좋아함", "고양이 키움"])
    assert await memory.clear_facts(db, CHAT, U, "요가") == 1
    assert await facts(db) == ["카페 운영", "고양이 키움"]
    assert await memory.clear_facts(db, CHAT, U) == 2 and await facts(db) == []
    assert await memory.clear_facts(db, CHAT, U) == 0


# ── 2. 쓰인 기억 표시 · 밀려나는 순서 · 만료 ───────────────
@test
def fact_used_heuristic():
    f = memory.fact_used
    assert f("부산 서면에서 카페 운영함", "부산에서 카페 하신다고 하셨죠? 그럼 뱅쇼 어떠세요")     # 부산·카페
    assert f("카페 운영 중", "카페 운영하시는 분들은 겨울에 바쁘시죠")                             # 운영중? → '운영'
    assert f("부산에서 카페 운영", "부산 카페 좋죠")                                              # '부산에서' → '부산'
    assert not f("부산 서면에서 카페 운영함", "카페 좋죠")                                        # 낱말 1개뿐
    assert not f("카페 운영", "오늘 날씨 춥네요")
    assert f("호칭: 김사장", "김사장님 안녕하세요")                                               # '호칭' 은 핵심어 아님 → 1개면 1개
    assert not f("", "아무 말") and not f("카페 운영", "")


@test
async def mark_used_counts_and_record_turn_calls_it():
    db = await make_db()
    await memory.add_facts(db, CHAT, U, ["부산 서면에서 카페 운영", "등산 좋아함"])
    assert await memory.mark_used(db, CHAT, U, "부산 카페라면 따뜻한 음료가 좋아요") == 1
    rows = {r["fact"]: r for r in await memory.get_facts(db, CHAT, U)}
    assert rows["부산 서면에서 카페 운영"]["used_n"] == 1 and rows["부산 서면에서 카페 운영"]["used_ts"]
    assert rows["등산 좋아함"]["used_n"] == 0 and rows["등산 좋아함"]["used_ts"] is None
    assert await memory.mark_used(db, CHAT, 99, "부산 카페") == 0                       # 기억 없는 사람
    assert await memory.mark_used(db, -2, U, "부산 카페") == 0                          # 다른 방은 따로
    # AI 답 기록(record_turn) 때 자동으로
    await memory.record_turn(db, CHAT, U, "call", "주말에 뭐 할까", "등산 좋아하시니 북한산 어떠세요?", 5)
    rows = {r["fact"]: r for r in await memory.get_facts(db, CHAT, U)}
    assert rows["등산 좋아함"]["used_n"] == 1, dict(rows["등산 좋아함"])


@test
async def eviction_least_used_then_oldest_and_new_fact_survives():
    db = await make_db()
    n = memory.MAX_FACTS
    await memory.add_facts(db, CHAT, U, [f"관심사 {i}번 항목" for i in range(n)])
    now = int(time.time())
    for r in await memory.get_facts(db, CHAT, U):   # 0번이 가장 오래됨
        await db._write("UPDATE member_memory SET ts=? WHERE id=?", (now - 1000 + int(r["fact"].split()[1][:-1]), r["id"]))
    await memory.mark_used(db, CHAT, U, "관심사 0번 항목 얘기")
    await memory.mark_used(db, CHAT, U, "관심사 0번 항목 얘기")
    await memory.mark_used(db, CHAT, U, "관심사 1번 항목 얘기")
    await memory.add_facts(db, CHAT, U, ["새 취미 수영"])
    left = await facts(db)
    assert len(left) == n and "관심사 2번 항목" not in left, left                    # 안 쓰인 것 중 가장 오래된 것
    assert "관심사 0번 항목" in left and "관심사 1번 항목" in left and "새 취미 수영" in left
    await memory.add_facts(db, CHAT, U, ["새 취미 테니스"])
    assert "관심사 3번 항목" not in await facts(db)
    # 전부 한 번 이상 쓰였어도 방금 넣은 기억은 곧바로 밀려나지 않음 → 덜 쓰인 1번(1회)이 0번(2회)보다 먼저
    await db._write("UPDATE member_memory SET used_n=MAX(used_n, 1), used_ts=? WHERE used_ts IS NULL", (now,))
    await memory.add_facts(db, CHAT, U, ["새 취미 골프"])
    left = await facts(db)
    assert "새 취미 골프" in left and "관심사 0번 항목" in left and "관심사 1번 항목" not in left, left
    assert len(left) == n and sum(1 for x in left if x.startswith("관심사")) == n - 3


@test
async def unused_facts_expire_after_max_unused_days():
    db = await make_db()
    await memory.add_facts(db, CHAT, U, ["오래된 근황 이사 준비", "오래됐지만 자주 씀 카페", "최근 기억 요가", "59일 된 기억 등산"])
    now = int(time.time())
    rows = {r["fact"]: r["id"] for r in await memory.get_facts(db, CHAT, U)}
    old = now - (memory.MAX_UNUSED_DAYS + 1) * DAY
    await db._write("UPDATE member_memory SET ts=? WHERE id=?", (old, rows["오래된 근황 이사 준비"]))
    await db._write("UPDATE member_memory SET ts=?, used_n=3, used_ts=? WHERE id=?",
                    (old, now - DAY, rows["오래됐지만 자주 씀 카페"]))
    await db._write("UPDATE member_memory SET ts=? WHERE id=?", (now - 59 * DAY, rows["59일 된 기억 등산"]))
    await memory.add_facts(db, -2, U, ["다른 방 기억"])                            # 다른 방 정리는 이 사람 기억 안 건드림
    assert len(await facts(db)) == 4
    await memory.mark_used(db, CHAT, U, "아무 상관 없는 답")                         # 답마다 정리
    assert await facts(db) == ["오래됐지만 자주 씀 카페", "최근 기억 요가", "59일 된 기억 등산"], await facts(db)
    # add_facts 도 같은 atomic 안에서 정리
    await db._write("UPDATE member_memory SET used_ts=? WHERE id=?", (old, rows["오래됐지만 자주 씀 카페"]))
    await memory.add_facts(db, CHAT, U, ["새 기억 수영"])
    assert "오래됐지만 자주 씀 카페" not in await facts(db)


# ── 3. 추출 프롬프트: 명시/추정 · 정정은 교체 ────────────────
@test
def extract_prompt_has_codex_style_rules():
    s = memory.EXTRACT_SYSTEM
    for part in ("명시", "추정", "일반화", "replaces", "나중 말이 이긴다", "증거가 아니다"):
        assert part in s, part


@test
async def correction_replaces_instead_of_appending():
    r = await room()
    await memory.add_facts(r.db, r.CHAT, U, ["부산에서 카페 운영", "등산 좋아함"])
    first = await memory.get_facts(r.db, r.CHAT, U)
    await r.db._write("UPDATE member_memory SET used_n=4 WHERE id=?", (first[0]["id"],))
    await r.db.log_message(r.CHAT, U, 50, "저 사실 이제 대구로 이사해서 식당 하고 있어요")
    r.llm.json_script["memory"] = [{"facts": [
        {"text": "대구에서 식당 운영", "tag": "명시", "replaces": 1},
        {"text": "대구로 이사함", "tag": "명시", "replaces": None},
    ], "remove": []}]
    added = await memory.extract(r.svc, r.CHAT, U)
    assert added == ["대구에서 식당 운영", "대구로 이사함"], added
    rows = await memory.get_facts(r.db, r.CHAT, U)
    assert [x["fact"] for x in rows] == ["대구에서 식당 운영", "등산 좋아함", "대구로 이사함"]
    assert rows[0]["id"] == first[0]["id"] and rows[0]["used_n"] == 4 and rows[0]["inferred"] == 0  # 같은 줄을 고침
    known = r.llm.of("json", "memory")[0]["user"]
    assert "1. 부산에서 카페 운영" in known


@test
async def bad_replace_numbers_are_ignored_and_replace_dedupes():
    r = await room()
    await memory.add_facts(r.db, r.CHAT, U, ["서울에서 꽃집 운영", "꽃집 운영 5년째"])
    await r.db.log_message(r.CHAT, U, 51, "저는 이제 꽃집 접고 서울에서 빵집 해요")
    r.llm.json_script["memory"] = [{"facts": [
        {"text": "서울에서 빵집 운영", "tag": "명시", "replaces": 1},
        {"text": "빵 굽는 것 좋아함", "tag": "추정", "replaces": 99},     # 없는 번호 → 새 줄
        {"text": "서울에서 빵집 운영", "tag": "명시", "replaces": True},  # bool → 번호 아님 → 중복이라 건너뜀
    ], "remove": [2]}]
    await memory.extract(r.svc, r.CHAT, U)
    assert await facts(r.db, r.CHAT) == ["서울에서 빵집 운영", "빵 굽는 것 좋아함"], await facts(r.db, r.CHAT)


# ── 태그 저장·표시 ─────────────────────────────────────────
@test
async def inferred_tag_is_stored_and_rendered():
    r = await room()
    await r.db.log_message(r.CHAT, U, 60, "저는 요즘 라떼 아트 연습 중이에요. 민지 사장이라고 불러주세요")
    r.llm.json_script["memory"] = [{"facts": [
        {"text": "카페 운영", "tag": "추정"},
        {"text": "호칭: 민지 사장", "tag": "명시"},
        "라떼 아트 연습 중",                                               # 예전 형식(문장만) → 출처 모름 = 추정
    ], "remove": []}]
    await memory.extract(r.svc, r.CHAT, U)
    rows = {x["fact"]: x["inferred"] for x in await memory.get_facts(r.db, r.CHAT, U)}
    assert rows == {"카페 운영": 1, "호칭: 민지 사장": 0, "라떼 아트 연습 중": 1}, rows
    out = await memory.context_for(r.svc, r.CHAT, U, {}, [])
    mem = out["user_memory"]
    assert mem[0].startswith("카페 운영 (추정) (") and mem[1].startswith("호칭: 민지 사장 (") \
        and "추정" not in mem[1], mem
    assert mem[2].startswith("라떼 아트 연습 중 (추정)")
    # 나중에 본인이 직접 말하면(명시) 추정 → 명시로
    await memory.add_facts(r.db, r.CHAT, U, ["카페 운영"])
    assert {x["fact"]: x["inferred"] for x in await memory.get_facts(r.db, r.CHAT, U)}["카페 운영"] == 0
    # 다음 정리 때 AI 에 보이는 <known> 에도 (추정) 표시
    await r.db.log_message(r.CHAT, U, 61, "저는 주말엔 등산 다녀요")
    await r.db._write("DELETE FROM memory_state")
    r.llm.json_script["memory"] = [{"facts": [], "remove": []}]
    await memory.extract(r.svc, r.CHAT, U)
    assert "라떼 아트 연습 중 (추정)" in r.llm.of("json", "memory")[-1]["user"]
    # .기억 목록(social) 도 같은 표시
    assert memory.fact_line({"fact": "카페 운영", "inferred": 1}) == "카페 운영 (추정)"


# ── 예전 DB 이전 ───────────────────────────────────────────
@test
async def old_schema_gets_new_columns():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE member_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "user_id INTEGER NOT NULL, fact TEXT NOT NULL, ts INTEGER NOT NULL)")
    con.execute("INSERT INTO member_memory(chat_id, user_id, fact, ts) VALUES(?,?,?,?)",
                (CHAT, U, "부산에서 카페 운영", int(time.time())))
    con.commit()
    con.close()
    db = DB(path)
    await db.open()
    OPEN_DBS.append(db)
    cols = {r["name"] for r in await db._all("PRAGMA table_info(member_memory)")}
    assert {"used_n", "used_ts", "inferred"} <= cols, cols
    row = (await memory.get_facts(db, CHAT, U))[0]
    assert row["fact"] == "부산에서 카페 운영" and row["used_n"] == 0 and row["used_ts"] is None and row["inferred"] == 0
    assert await memory.mark_used(db, CHAT, U, "부산 카페 좋죠") == 1
    assert await memory.add_facts(db, CHAT, U, [("요가 좋아함", True)]) == ["요가 좋아함"]
    await db.close()
    OPEN_DBS.remove(db)
    db2 = DB(path)                                                                  # 두 번 열어도 괜찮음
    await db2.open()
    OPEN_DBS.append(db2)
    assert len(await memory.get_facts(db2, CHAT, U)) == 2


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
