"""🧠 스티커 실시간 학습 (sodam/stickerlearn.py): 기록·반응 점수·레시피 승격·취향 정렬·정리·기능 요청. python tests/run_all.py sticker_learn

AI·ffmpeg 호출 없음 (forge 는 가짜).
"""
import json
from types import SimpleNamespace

from fake_llm import Room, reply, tool_call
from fakes import runner
from test_sanction_multi import A, BOSS
from test_sticker import mascot

from sodam import featreq, stickerforge as SF, stickerlearn as L
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.stickerforge import recipes
from sodam.tools import ToolCtx
from sodam.vision import Attached

test, run_all = runner()
SPEC_A = {"mode": "cutout", "motion": [{"type": "jab"}], "fx": [{"type": "aura"}, {"type": "shockwave"}]}
SPEC_B = {"mode": "cutout", "motion": [{"type": "idle"}], "fx": [{"type": "sparkle"}]}


async def room():
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    for u in (BOSS, A):
        await r.join(u)
    return r


@test
async def log_marks_redo_within_ten_minutes_and_reactions_and_replies_score():
    r = await room()
    db = r.db
    i1 = await L.log(db, chat_id=r.CHAT, user_id=A.id, request="출근완료 강렬하게 " * 30, kind="glow", spec=SPEC_A, outcome="ok", msg_id=501, now=1000)
    row = await db._one("SELECT * FROM sticker_log WHERE id=?", (i1,))
    assert len(row["request"]) == L.REQUEST_CHARS and row["family"] == "impact" and row["score"] == 0
    i2 = await L.log(db, chat_id=r.CHAT, user_id=A.id, request="다시", kind="glow", spec=SPEC_B, outcome="ok", msg_id=502, now=1000 + 300)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i1,)))["score"] == -1        # 10분 안 재요청 = 별로
    i3 = await L.log(db, chat_id=r.CHAT, user_id=A.id, request="x", kind="glow", spec=SPEC_B, outcome="ok", msg_id=503, now=1000 + 300 + 700)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i2,)))["score"] == 0         # 10분 지나면 아님
    # 👍 반응
    mr = SimpleNamespace(chat=SimpleNamespace(id=r.CHAT), message_id=502, user=A,
                         new_reaction=[SimpleNamespace(emoji="👍")])
    await L.on_reaction(r.svc, r.bot, mr)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i2,)))["score"] == 1
    bad = SimpleNamespace(chat=SimpleNamespace(id=r.CHAT), message_id=503, user=A, new_reaction=[SimpleNamespace(emoji="💩")])
    await L.on_reaction(r.svc, r.bot, bad)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i3,)))["score"] == 0         # 나쁜 이모지는 안 셈
    # 답장 글
    sticker_msg = SimpleNamespace(message_id=503)
    await L.on_group_message(r.svc, r.bot, r.msg(BOSS, "이거 완벽하다 ㅋㅋㅋ", reply_to=sticker_msg), Role.ADMIN)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i3,)))["score"] == 1
    await L.on_group_message(r.svc, r.bot, r.msg(BOSS, "음 별로인데 다른 느낌으로", reply_to=sticker_msg), Role.ADMIN)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i3,)))["score"] == -1
    # 답장 없이도 만든 사람이 10분 안에 '별로' → 그 사람 마지막 것
    import time
    i4 = await L.log(db, chat_id=r.CHAT, user_id=A.id, request="x", kind="glow", spec=SPEC_A, outcome="ok", msg_id=504, now=int(time.time()))
    await L.on_group_message(r.svc, r.bot, r.msg(A, "아 별론데 ㅅㅂ"), Role.MEMBER)
    assert (await db._one("SELECT score FROM sticker_log WHERE id=?", (i4,)))["score"] == -1


@test
async def two_goods_promote_a_named_recipe_and_prefs_reorder_catalog():
    r = await room()
    db = r.db
    ids = [await L.log(db, chat_id=r.CHAT, user_id=A.id, request="출근완료 강렬하게", kind="glow", spec=SPEC_A, outcome="ok",
                       msg_id=600 + i, now=100 + i * 1000) for i in range(3)]
    await L.mark(db, ids[0], 1)
    assert not await db._all("SELECT * FROM sticker_recipes")                                    # 한 번은 아직
    await L.mark(db, ids[1], 1)
    [rec] = await db._all("SELECT * FROM sticker_recipes")
    assert rec["name"] == "출근완료_강렬하게_impact" and rec["signature"] == "jab+aura+shockwave" and rec["good"] == 1
    await L.mark(db, ids[2], 1)
    [rec] = await db._all("SELECT * FROM sticker_recipes")
    assert rec["good"] == 2                                                                        # 같은 조합은 한 줄로
    assert await L.learned_recipe(db, r.CHAT, rec["name"]) == SPEC_A
    # 별로였던 조합은 뒤로, 최근 계열은 미룸
    b = await L.log(db, chat_id=r.CHAT, user_id=A.id, request="잘자요", kind="cutout", spec=SPEC_B, outcome="ok", msg_id=700, now=99999)
    await L.mark(db, b, -1)
    prefs = await L.preferences(db, r.CHAT, A.id)
    assert prefs["liked"][0]["name"] == rec["name"] and "idle+sparkle" in prefs["disliked_sigs"]
    assert prefs["recent_families"][0] == "calm"
    ordered = L.order_candidates(list(recipes.RECIPES), prefs)
    assert L.signature(ordered[-1]) in prefs["disliked_sigs"] or recipes.family(ordered[-1]) == "calm"
    assert recipes.family(ordered[0]) != "calm"                                                 # 최근 계열(calm)은 뒤로
    liked_static = [r for r in recipes.RECIPES if L.signature(r) == "punch+scan+sparkle"]
    prefs["user_liked_sigs"].add("punch+scan+sparkle")
    assert L.order_candidates(list(recipes.RECIPES), prefs)[0] is liked_static[0]                # 이 사람이 좋아한 조합이 맨 앞
    from sodam.panels.sticker import catalog_text
    txt = catalog_text("아무거나", "glow", prefs)
    assert txt.splitlines()[1].startswith(f"- {rec['name']}") and "학습 레시피" in txt.splitlines()[1]


@test
async def recipes_are_capped_per_room_and_expire():
    r = await room()
    db = r.db

    def seed(c):
        for i in range(60):
            c.execute("INSERT INTO sticker_recipes(chat_id, name, product, family, signature, spec, good, bad, last_used) "
                      "VALUES(?,?,?,?,?,?,?,0,?)", (r.CHAT, f"r{i}", "sticker", "calm", f"sig{i}", "{}", i, 5000 + i))
        L._trim(c, r.CHAT, 10000)
    await db.atomic(seed)
    rows = await db._all("SELECT name, good FROM sticker_recipes ORDER BY good DESC")
    assert len(rows) == L.MAX_PER_CHAT and rows[-1]["good"] == 10                                  # 점수 낮은 것부터 지움
    await L.cleanup(db, now=5030 + L.EXPIRE_DAYS * 86400)
    assert len(await db._all("SELECT 1 FROM sticker_recipes")) == 30                              # last_used 5030 이전은 삭제


async def ask(r, caller, script, image=None):
    r.llm.script = [*script, reply("짠!")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, caller, Role.MEMBER, await r.db.get_settings(r.CHAT), image=image)
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="스티커", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


@test
async def tool_logs_sent_message_uses_learned_recipe_and_files_feature_request():
    r = await room()
    seen = []

    async def fake_forge(image, spec, icon=False):
        seen.append(spec)
        return SF.Result(True, b"\x1aE\xdf\xa3webm", b"", b"", [("size", "200KB / 256KB", True)], "glow")
    orig, SF.forge = SF.forge, fake_forge
    try:
        img = Attached(mascot(), "image/png", A.id)
        res = await ask(r, A, [tool_call("make_sticker", {"spec": SPEC_A, "request": "출근완료 강렬하게", "wanted": "눈에서 레이저"})], image=img)
        assert "보냈음" in res[0] and "레이저" in res[0] and "기능 요청" in res[0], res
        [row] = await r.db._all("SELECT * FROM sticker_log")
        assert row["msg_id"] == r.bot.named("send_sticker") and row["msg_id"] > 0 or row["msg_id"] > 0
        assert row["request"] == "출근완료 강렬하게" and row["kind"] == "glow" and json.loads(row["spec"])["motion"][0]["type"] == "jab"
        groups = await featreq.list_groups(r.db, chat_id=r.CHAT)
        assert groups and "레이저" in groups[0]["summary"], groups
        # 학습 레시피 이름으로 부르기
        def promote(c):
            c.execute("INSERT INTO sticker_recipes(chat_id, name, product, family, signature, spec, good, last_used) VALUES(?,?,?,?,?,?,3,1)",
                      (r.CHAT, "출근완료_강렬_impact", "sticker", "impact", "jab+aura+shockwave", json.dumps(SPEC_A), ))
        await r.db.atomic(promote)
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"recipe": "출근완료_강렬_impact", "caption": "출근완료"}})], image=img)
        assert "보냈음" in res[0] and seen[-1]["motion"][0]["type"] == "jab" and seen[-1]["caption"]["text"] == "출근완료", res
        assert (await r.db._one("SELECT uses FROM sticker_recipes"))["uses"] == 1
        res = await ask(r, A, [tool_call("make_sticker", {"spec": {"recipe": "없는것"}})], image=img)
        assert "spec 오류" in res[0] and "없음" in res[0], res
    finally:
        SF.forge = orig
