"""'누구 얘기인지' 단서 모으기: python tests/test_addressee.py (실제 모델 평가는 tools/ai_eval_addressee.py)"""
import asyncio
import sys
import time
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam import addressee

test, run_all = runner()
CHAT = -1005550000021
HANA, SKY, KIM, NEW = (fake_user(201, "하나", "hana_k"), fake_user(202, "하늘코인 거래소", "sky_trade"),
                       fake_user(203, "김철수", "kimcs"), fake_user(204, "새내기", "newbie"))
ME = fake_user(1, "방장")


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    now = int(time.time())
    for u, joined in ((HANA, 40 * 86400), (SKY, 40 * 86400), (KIM, 40 * 86400), (NEW, 300), (ME, 90 * 86400)):
        await db.upsert_user(u)
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                        (CHAT, u.id, now - joined, now - 60))
    await db.log_message(CHAT, KIM.id, 9, "방금 한마디")
    return db, svc, FakeBot()


def msg(text, reply_to=None, entities=()):
    return SimpleNamespace(text=text, reply_to_message=reply_to, entities=entities)


@test
def core_strips_particles_and_titles():
    for w, want in (("철수형한테", "철수"), ("하늘대표님께", "하늘"), ("민지씨랑", "민지"), ("새내기를", "새내기"),
                    ("대표님께", "대표"), ("영희", "영희")):
        assert addressee._core(w) == want, (w, addressee._core(w))


@test
async def reply_tag_name_and_newcomer_become_candidates():
    db, svc, bot = await setup()
    lines = await addressee.collect(svc, bot, msg("소담아 대표님 인사드려", reply_to=SimpleNamespace(from_user=HANA)),
                                    CHAT, ME, "소담아 대표님 인사드려")
    text = "\n".join(lines)
    assert "★★★ 하나" in text and "답장" in text                     # 답장 대상
    assert "★★ 새내기" in text and "새로 들어옴" in text              # 방금 입장
    assert "김철수" not in text                                        # 방금 말했을 뿐인 사람은 후보 아님
    assert "방장" not in text                                          # 말한 본인은 제외
    lines = await addressee.collect(svc, bot, msg("소담아 하늘대표님께 인사"), CHAT, ME, "소담아 하늘대표님께 인사")
    assert any("★★ 하늘코인 거래소" in ln for ln in lines)
    lines = await addressee.collect(svc, bot, msg("소담아 @kimcs 인사"), CHAT, ME, "소담아 @kimcs 인사")
    assert any(ln.startswith("★★★ 김철수") for ln in lines)


@test
async def no_candidates_says_do_not_invent_and_style_ask_is_noted():
    db, svc, bot = await setup()
    await db._write("UPDATE members SET joined_at=0 WHERE user_id=?", (NEW.id,))
    lines = await addressee.collect(svc, bot, msg("소담아 대표님 인사드려 .말투 여친"), CHAT, ME, "소담아 대표님 인사드려 .말투 여친")
    assert any("후보 없음" in ln and "지어내지" in ln for ln in lines)
    assert any("말투 변경" in ln and "여친" in ln for ln in lines)
    assert await addressee.collect(svc, bot, msg("hi"), 5, ME, "hi") == []   # 1:1 은 없음


@test
async def name_only_call_answers_the_caller_not_the_newcomer():
    """일루왕 10/05: 루피가 '소담아' 만 불렀는데 40분 전 들어온 '맞링공 문의주세연' 이름으로 답함."""
    db, svc, bot = await setup()
    odd = fake_user(205, "️" * 6, "Jjmmm6")
    odd.last_name = "️️맞링공 문의주세연"
    await db.upsert_user(odd)
    await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                    (CHAT, odd.id, int(time.time()) - 40 * 60, int(time.time())))
    for req in ("(이름만 부름)", "뭐해", "오늘 날씨 어때"):
        text = "\n".join(await addressee.collect(svc, bot, msg("소담아"), CHAT, ME, req))
        assert "문의주세연" not in text and "새내기" not in text, (req, text)
    text = "\n".join(await addressee.collect(svc, bot, msg("소담아"), CHAT, ME, "(이름만 부름)"))
    assert "부른 사람 본인에게" in text
    text = "\n".join(await addressee.collect(svc, bot, msg("x"), CHAT, ME, "방금 들어온 분 환영해줘"))
    assert "문의주세연" in text and "새로 들어옴" in text


@test
async def disliked_nickname_is_forgotten_by_code():
    from sodam import memory
    db, svc, bot = await setup()
    for f in ("호칭: 팽부장", "영업대표함"):
        await db._write("INSERT INTO member_memory(chat_id, user_id, fact, ts) VALUES(?,?,?,?)", (CHAT, HANA.id, f, 1))
    await db.set_member_note(CHAT, HANA.id, "호칭", "하나사장")
    assert await memory.drop_disliked_nickname(db, CHAT, HANA.id, "팽부장 좋아") == []
    assert await memory.drop_disliked_nickname(db, CHAT, HANA.id, "김부장 떠오르게 하지마") == []   # 다른 이름
    assert await memory.drop_disliked_nickname(db, CHAT, HANA.id, "팽부장 떠오르게 하지마라 소담아 에바다") == ["팽부장"]
    assert [r["fact"] for r in await memory.get_facts(db, CHAT, HANA.id)] == ["영업대표함"]
    assert await memory.drop_disliked_nickname(db, CHAT, HANA.id, "그 호칭 쓰지마") == ["하나사장"]


@test
async def hints_reach_the_model_as_data():
    from sodam.prompt import build_messages
    from fakes import TZ
    msgs = build_messages(bot_name="소담", bot_id=999, style_key="polite", tz=TZ, caller=ME, role_label="admin",
                          notes={}, history=[], reply_to=None, request="인사드려", hints=["★★★ 테스트멤버XY (ID 201): 답장"])
    user = msgs[-1]["content"]
    assert "<addressee_hints" in user and "★★★ 테스트멤버XY" in user and "테스트멤버XY" not in msgs[0]["content"]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
