"""🔎 의미 검색 (sodam/semsearch.py): FTS5 + sqlite-vec RRF. 가짜 임베딩(뜻 묶음 → 같은 방향). python tests/run_all.py semsearch"""
import math
import zlib

from fake_llm import Room, tool_call
from fakes import fake_user, runner
from test_sanction_multi import BOSS, ask

from sodam import costs, persist, semsearch, stats

test, run_all = runner()
U = fake_user(40, "민수", "ms")
CONCEPTS = {0: ("먹튀", "잠수", "입금했는데", "연락 끊"), 1: ("짜장면", "점심", "밥")}


def fake_vec(text: str) -> list[float]:
    v = [0.0] * semsearch.DIM
    hit = [d for d, words in CONCEPTS.items() if any(w in text for w in words)]
    for d in hit or [10 + zlib.crc32(text.encode()) % 200]:   # 뜻 묶음이 없으면 글마다 다른 방향
        v[d] = 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


async def world(vec=True):
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    await r.join(U)
    calls = []

    async def embed(texts, *, dims, model, purpose="embed"):
        calls.append((purpose, list(texts)))
        assert dims == semsearch.DIM and model == semsearch.MODEL
        return [fake_vec(t) for t in texts]
    r.llm.embed = embed
    r.db.vec = r.db.vec and vec
    r.calls = calls
    return r


async def say(r, text, chat=None, user=U, **kw):
    await r.db.upsert_user(user)
    await r.db.log_message(chat or r.CHAT, user.id, None, text, **kw)


async def index_all(r):
    await r.db._write("DELETE FROM claims")
    return await semsearch.index_new(r.svc)


@test
async def indexes_new_human_messages_once_in_batches():
    r = await world()
    assert r.db.vec, "sqlite-vec 이 설치돼 있어야 함 (requirements.txt)"
    await say(r, "어제 그 사람한테 입금했는데 잠수탔어요")
    await say(r, "ㅋㅋ")                                                         # 너무 짧음
    await say(r, "봇이 쓴 긴 안내 문구입니다 여러분", is_bot=True)
    await say(r, "숨긴 글인데 먹튀 얘기 하는 거", flagged=True)
    assert await index_all(r) == 1 and r.calls[-1][1] == ["어제 그 사람한테 입금했는데 잠수탔어요"]
    assert await index_all(r) == 0                                               # 커서 → 다시 안 넣음
    await say(r, "새 글도 색인되나요 궁금합니다")
    assert await semsearch.index_new(r.svc) == 0                                 # 25초 차지: 두 프로세스가 같이 안 돌림
    assert await index_all(r) == 1


@test
async def keyword_plus_meaning_rrf_same_room_only():
    r = await world()
    await say(r, "그 업체 먹튀 확정이래요")                                       # 단어로 맞음
    await say(r, "어제 입금했는데 연락 끊겼어요 조심하세요")                        # 뜻으로만 맞음
    await say(r, "오늘 점심 짜장면 먹었어요 맛있네요")                              # 무관
    await say(r, "저쪽 방도 먹튀 당했대요 입금했는데", chat=-100999)               # 다른 방
    await index_all(r)
    res = await ask(r, BOSS, [tool_call("search_chat", {"keyword": "먹튀", "days": 7})])
    out = res[0]
    assert "먹튀 확정" in out and "≈" in out and "연락 끊겼" in out, out
    assert "짜장면" not in out and "저쪽 방" not in out, out
    assert out.index("먹튀 확정") < out.index("연락 끊겼")                         # 둘 다 맞는 글이 없으면 단어 결과가 앞 (RRF 동점 → 단어 먼저)
    assert r.calls[-1] == ("embed_query", ["먹튀"])


@test
async def falls_back_to_keywords_without_vec_or_on_error():
    r = await world(vec=False)
    await say(r, "그 업체 먹튀 확정이래요")
    await say(r, "어제 입금했는데 연락 끊겼어요")
    assert await index_all(r) == 0 and not r.calls
    out = await stats.search_text(r.db, r.CHAT, r.svc.cfg.tz, "먹튀", 7, svc=r.svc)
    assert "먹튀 확정" in out and "≈" not in out
    r2 = await world()
    await say(r2, "그 업체 먹튀 확정이래요")
    await index_all(r2)

    async def boom(*a, **k):
        raise RuntimeError("budget")
    r2.llm.embed = boom
    out = await stats.search_text(r2.db, r2.CHAT, r2.svc.cfg.tz, "먹튀", 7, svc=r2.svc)
    assert "먹튀 확정" in out                                                     # 뜻 검색이 실패해도 단어 결과는


@test
async def pruned_messages_lose_vectors_and_embedding_is_priced():
    r = await world()
    await say(r, "어제 입금했는데 연락 끊겼어요 조심")
    await index_all(r)
    assert (await r.db._one("SELECT COUNT(*) AS n FROM msg_vec"))["n"] == 1
    await r.db._write("DELETE FROM messages")
    await say(r, "새로 온 글 하나 더 있어요 여러분")
    await index_all(r)
    assert (await r.db._one("SELECT COUNT(*) AS n FROM msg_vec"))["n"] == 1        # 지운 글 벡터는 정리, 새 글만
    assert costs.usd_micro(semsearch.MODEL, 1_000_000, 0, 0) == 20_000             # $0.02 / 1M


_ = persist
