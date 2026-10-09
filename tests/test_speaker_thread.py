"""바쁜 방 '기억상실' (서버 실측 2026-10-09): 최근 30줄이 중앙 5.5분 치뿐이라, 같은 사람이 30분 안에 다시 부른 973번 중
126번은 앞 대화가 30줄 밖으로 밀려나 있었음 → 그 앞(2시간 안) 이 사람 쪽 대화를 <speaker_thread> 로 따로 싣는다."""
import time

from fake_llm import Room
from fakes import fake_user, runner

from sodam import memory

test, run_all = runner()
ME, OTHER, THIRD = fake_user(501, "영희"), fake_user(502, "철수"), fake_user(503, "민수")


def user_text(llm) -> str:
    call = [c for c in llm.calls if c["kind"] == "chat"][-1]
    body = call["messages"][-1]["content"]
    return body if isinstance(body, str) else body[0]["text"]


def block(text: str, tag: str) -> str:
    start = text.find(f"<{tag} ")
    return text[start:text.find(f"</{tag} ", start)] if start >= 0 else ""


async def busy_room(bot_reply: bool = False):
    r = await Room().open(settings={"ai_memory": False})   # 기억 정리(90초 뒤)는 이 시험과 무관
    for u in (ME, OTHER, THIRD):
        await r.join(u)
    now = int(time.time())
    db, C = r.db, r.CHAT
    await db.log_message(C, ME.id, 10, "내일 3시에 홍대 원두 가게 가기로 했어", ts=now - 1800)
    await db.log_message(C, OTHER.id, 11, "영희 그 가게 이름 블루보틀이지?", ts=now - 1790, reply_to_msg_id=10, reply_to_user=ME.id)
    await db.log_message(C, THIRD.id, 12, "나는 내일 출장이야", ts=now - 1785)                 # 남의 말 (영희와 상관없음)
    await db.log_message(C, ME.id, 13, "응 맞아", ts=now - 1780, reply_to_msg_id=12, reply_to_user=THIRD.id)
    await db.log_message(C, ME.id, 14, "옛날 얘기", ts=now - 3 * 3600)                    # 2시간 밖
    if bot_reply:                                                                         # 소담이 영희에게 한 답 (기록 순서 = 시간 순서)
        await db.log_message(C, r.bot.id, 15, "블루보틀 좋지!", is_bot=True, ts=now - 1700, reply_to_msg_id=10,
                             reply_to_user=ME.id)
    for i in range(40):                                                                   # 그 뒤로 남들 잡담 40줄 (30줄 창을 밀어냄)
        await db.log_message(C, OTHER.id if i % 2 else THIRD.id, 100 + i, f"잡담 {i}", ts=now - 600 + i * 10)
    return r


@test
async def busy_room_keeps_the_callers_earlier_talk():
    r = await busy_room()
    r.llm.script = ["블루보틀 3시 약속이었지!"]
    await r.say(ME, "소담아 아까 내가 내일 뭐 한다고 했지?")
    text = user_text(r.llm)
    chat = block(text, "chat_log")
    assert "원두 가게" not in chat, "30줄 창 밖 (이 시험의 전제)"
    th = block(text, "speaker_thread")
    assert "내일 3시에 홍대 원두 가게" in th, th                       # 그 사람이 한 말
    assert "블루보틀" in th, th                                          # 그 사람에게 답장한 말
    assert "출장" in th, th                                              # 그 사람이 답장한 글
    assert "옛날 얘기" not in th, "2시간 밖은 안 넣음"
    assert "잡담" not in th, "남들끼리 한 말은 안 넣음"
    assert th.index("원두") < th.index("블루보틀"), "시간 순서"


@test
async def quiet_room_or_dm_adds_nothing_and_past_turns_are_not_doubled():
    r = await Room().open(settings={"ai_memory": False})   # 기억 정리(90초 뒤)는 이 시험과 무관
    await r.join(ME)
    await r.db.log_message(r.CHAT, ME.id, 10, "조용한 방 첫 말", ts=int(time.time()) - 60)
    r.llm.script = ["응!"]
    await r.say(ME, "소담아 안녕")
    assert "<speaker_thread" not in user_text(r.llm), "30줄 안에 다 보이면 따로 안 넣음"

    r = await busy_room(bot_reply=True)
    now = int(time.time())
    await r.db._write("INSERT INTO ai_turns(chat_id, user_id, ts, via, request, answer, bot_msg_id) VALUES(?,?,?,?,?,?,?)",
                      (r.CHAT, ME.id, now - 1700, "call", "블루보틀 어때", "블루보틀 좋지!", 15))
    out = await memory.context_for(r.svc, r.CHAT, ME.id, {}, await r.db.recent_messages(r.CHAT, 30))
    assert any("블루보틀 좋지" in r_["text"] for r_ in out["speaker_thread"]), "소담 답도 이 사람 쪽 대화"
    assert not any("블루보틀 좋지" in t for t in out["past_turns"]), "같은 답을 past_turns 에 또 넣지 않음"
    out = await memory.context_for(r.svc, ME.id, ME.id, {}, [])
    assert "speaker_thread" not in out, "1:1 은 대화 자체가 그 사람 것"


if __name__ == "__main__":
    run_all()
