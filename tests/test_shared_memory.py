"""🧠 방 넘어 기억 공유 (ChatGPT 쓰레드처럼, 2026-10-26 오너): 한 사람의 본인 사실·본인 Q&A 를 같은 봇의
방들끼리 공유 (A방에서 알게 되면 B방에서도). 방 분위기 요약·남 얘기는 공유 안 함. 끈 방은 주지도 받지도 않음(대칭)."""
from fake_llm import Room
from fakes import fake_user, runner

from sodam import memory

test, run_all = runner()
U = fake_user(701, "민지")
A, B = -1000111, -1000222   # 같은 봇의 다른 두 방


async def svc():
    r = await Room().open()
    return r


def mem_texts(out) -> list[str]:
    return [m.split(" (")[0] for m in out["user_memory"]]


@test
async def fact_from_room_a_shows_in_room_b():
    r = await svc()
    await memory.add_facts(r.db, A, U.id, ["부산에서 카페 운영"])        # A방에서 알게 된 본인 사실 (기본 shared=1)
    await memory.add_facts(r.db, B, U.id, ["등산 좋아함"])
    out = await memory.context_for(r.svc, B, U.id, {}, [])              # B방 (기본 ON)
    assert "부산에서 카페 운영" in mem_texts(out), out["user_memory"]    # 딴 방 사실이 넘어옴
    assert "등산 좋아함" in mem_texts(out)


@test
async def sharing_off_keeps_only_this_room():
    r = await svc()
    await memory.add_facts(r.db, A, U.id, ["부산에서 카페 운영"])
    await memory.add_facts(r.db, B, U.id, ["등산 좋아함"])
    out = await memory.context_for(r.svc, B, U.id, {"ai_memory_shared": False}, [])
    assert mem_texts(out) == ["등산 좋아함"], out["user_memory"]        # 끄면 이 방 것만 (지금 동작 그대로)


@test
async def room_with_sharing_off_does_not_contribute():
    # 대칭: 공유 끈 방(A)의 기억은 shared=0 → 켠 방(B)이 못 봄
    r = await svc()
    await memory.add_facts(r.db, A, U.id, ["비밀 가게 준비중"], shared=0)
    await memory.add_facts(r.db, B, U.id, ["등산 좋아함"])
    out = await memory.context_for(r.svc, B, U.id, {}, [])
    assert "비밀 가게 준비중" not in mem_texts(out), out["user_memory"]
    assert "등산 좋아함" in mem_texts(out)
    # 그래도 A방 안에서는 자기 기억을 봄
    out_a = await memory.context_for(r.svc, A, U.id, {}, [])
    assert "비밀 가게 준비중" in mem_texts(out_a)


@test
async def same_fact_is_deduped_across_rooms():
    r = await svc()
    await memory.add_facts(r.db, A, U.id, ["카페 운영중"])
    await memory.add_facts(r.db, B, U.id, ["카페 운영중"])
    out = await memory.context_for(r.svc, B, U.id, {}, [])
    assert mem_texts(out).count("카페 운영중") == 1, out["user_memory"]  # 같은 사실은 하나만


@test
async def room_memory_is_not_shared():
    # 방 분위기 요약은 절대 방 넘어가면 안 됨 (누출)
    r = await svc()
    await r.db._write("INSERT INTO room_memory(chat_id, summary, upto_id, updated_at) VALUES(?,?,?,?)",
                      (A, "A방은 원두 OTC 거래가 활발함", 0, memory._now()))
    out = await memory.context_for(r.svc, B, U.id, {}, [])
    assert out["room_memory"] == "", out["room_memory"]                 # B방엔 A방 요약 안 옴


@test
async def past_turn_from_room_a_shows_in_room_b_when_shared():
    r = await svc()
    await memory.record_turn(r.db, A, U.id, "call", "홍대 맛집 추천해줘", "○○ 파스타 가봐", 1, shared=True)
    out = await memory.context_for(r.svc, B, U.id, {}, [])
    assert any("홍대 맛집" in t for t in out["past_turns"]), out["past_turns"]


@test
async def past_turn_from_off_room_does_not_leak():
    r = await svc()
    await memory.record_turn(r.db, A, U.id, "call", "비밀 질문", "비밀 답", 1, shared=False)
    out = await memory.context_for(r.svc, B, U.id, {}, [])
    assert not any("비밀" in t for t in out["past_turns"]), out["past_turns"]


@test
async def extract_writes_shared_from_room_setting():
    # 끝에서 끝까지: 방 설정 ai_memory_shared 가 꺼져 있으면 정리된 기억 행의 shared=0
    r = await Room().open(settings={"ai_memory_shared": False})
    await r.db.log_message(r.CHAT, U.id, 70, "저는 대구에서 꽃집 해요")
    r.llm.json_script["memory"] = [{"facts": [{"text": "대구에서 꽃집 운영", "tag": "명시"}], "remove": []}]
    await memory.extract(r.svc, r.CHAT, U.id)
    row = await r.db._one("SELECT shared FROM member_memory WHERE chat_id=? AND user_id=?", (r.CHAT, U.id))
    assert row and row["shared"] == 0, row


@test
async def extract_defaults_shared_on():
    r = await Room().open()   # 기본 ON
    await r.db.log_message(r.CHAT, U.id, 71, "저는 서울에서 빵집 해요")
    r.llm.json_script["memory"] = [{"facts": [{"text": "서울에서 빵집 운영", "tag": "명시"}], "remove": []}]
    await memory.extract(r.svc, r.CHAT, U.id)
    row = await r.db._one("SELECT shared FROM member_memory WHERE chat_id=? AND user_id=?", (r.CHAT, U.id))
    assert row and row["shared"] == 1, row


if __name__ == "__main__":
    run_all()
