"""근거 있는 답: 예전 이름으로 사람 찾기 · 지시어('걔') · 숫자 검사 · 자료끼리 다름 · 근거 보여줘.
python tests/run_all.py agent_grounding (가짜 LLM, 네트워크 없음)"""
import time
from types import SimpleNamespace

from fake_llm import Room, ScriptedLLM, fast_timers, restore_timers, tool_call
from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam import addressee, knowledge, memory, namehist, tools
from sodam.agent import NUMBER_NOTE, unsupported_numbers
from sodam.panels.members import mark
from sodam.permissions import Role

test, run_all = runner()
BOSS, JUNHO, KIM = fake_user(1, "방장", "boss"), fake_user(20, "박준호", "junho"), fake_user(30, "김철수", "kimcs")
OLD_JUNHO = fake_user(20, "준호킹", "jh_old")
NUM_HEAD = NUMBER_NOTE.split("{")[0]


async def room(script, *extra):
    r = Room()
    r.llm = ScriptedLLM(script)
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, "ai_sanction_card": "all"})
    for u in (BOSS, JUNHO, KIM, *extra):
        await r.join(u)
    await namehist.record(r.db, OLD_JUNHO)     # 준호킹(@jh_old) → 박준호(@junho)
    await namehist.record(r.db, JUNHO)
    return r


def tool_text(msgs) -> str:
    return "\n".join(m["content"] for m in msgs if m["role"] == "tool")


def seen(llm, needle) -> bool:
    return any(needle in str(m.get("content")) for c in llm.of("chat") for m in c["messages"])


def num_notes(llm):
    return [c for c in llm.of("chat") if any(str(m.get("content", "")).startswith(NUM_HEAD) for m in c["messages"])]


# ── 1. 예전 이름 ───────────────────────────────────────────
@test
async def greet_by_former_name_or_username_finds_current_member():
    old = fast_timers()
    try:
        got = []
        r = await room([tool_call("greet_members", {"names": ["준호킹대표님"]}),
                        lambda m: got.append(tool_text(m)) or "반가워요"])
        m = await r.say(BOSS, "소담아 준호킹대표님께 인사해")
        assert "예전 이름 준호킹 → 지금 박준호 (ID 20)" in got[0] and "지금 이름으로 부를 것" in got[0], got
        assert m.replies and 'tg://user?id=20' in m.replies[0]              # 멘션은 지금 사람에게
        ctx = tools.ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT))
        row, err = await tools._resolve(ctx, "@jh_old")                    # 예전 @아이디
        assert row["user_id"] == 20 and "예전 이름 @jh_old" in ctx.name_notes[0]
        row, err = await tools._resolve(ctx, "박준호")                      # 지금 이름이면 설명 없음
        assert row["user_id"] == 20 and len(ctx.name_notes) == 1
    finally:
        restore_timers(old)


@test
async def former_name_only_for_current_members_of_this_chat():
    r = await room([])
    gone = fake_user(40, "이영희", "yh")
    await r.join(gone)
    await namehist.record(r.db, fake_user(40, "영희공주", None))
    await mark(r.db, r.CHAT, 40, left=True)                               # 나간 사람은 예전 이름으로 안 찾음
    other = fake_user(50, "최민수", "ms")
    await r.db.upsert_user(other)
    await namehist.record(r.db, fake_user(50, "민수짱", None))              # 다른 방 사람
    await r.db.touch_member(-100999, 50)
    ctx = tools.ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT))
    for name in ("영희공주", "민수짱"):
        row, err = await tools._resolve(ctx, name)
        assert row is None and "찾을 수 없어요" in err, name
    await mark(r.db, r.CHAT, 40, left=False)                              # 다시 들어오면 찾음
    row, _ = await tools._resolve(ctx, "영희공주")
    assert row["user_id"] == 40


@test
async def sanction_by_former_name_needs_exact_unique_match_and_card_shows_current():
    old = fast_timers()
    try:
        got = []
        r = await room([tool_call("mute_member", {"names": ["준호킹"], "minutes": 30, "reason": "도배"}),
                        lambda m: got.append(tool_text(m)) or "버튼 눌러주세요"])
        await r.say(BOSS, "소담아 준호킹 30분 뮤트")
        [card] = [c for c in r.bot.named("send_message") if "할까요" in c[2]]
        assert "박준호" in card[2] and "<code>20</code>" in card[2] and "준호킹" not in card[2]
        assert "예전 이름 준호킹 → 지금 박준호" in got[0]
        ctx = tools.ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT))
        row, err = await tools._resolve(ctx, "준호킹님", for_sanction=True)     # 제재는 호칭 붙은 예전 이름 X
        assert row is None and err
        # 예전 이름이 둘 다 '철수' → 제재 카드 없이 되묻기
        a, b = fake_user(61, "가나다", "ga"), fake_user(62, "라마바", "ra")
        for u, oldname in ((a, "철수"), (b, "철수")):
            await r.join(u)
            await namehist.record(r.db, fake_user(u.id, oldname, None))
            await namehist.record(r.db, u)
        before = len(r.bot.named("send_message"))
        ctx = tools.ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT))
        out = await tools.execute("ban_member", '{"names": ["철수"], "reason": "x"}', ctx)
        assert "여러 명" in out and "예전 이름 기준" in out and "가나다(61)" in out and "라마바(62)" in out, out
        assert len(r.bot.named("send_message")) == before and not ctx.sanctioned
    finally:
        restore_timers(old)


# ── 지시어 ('걔', '그 사람') ─────────────────────────────────
@test
async def pronoun_points_to_reply_author_in_prompt():
    old = fast_timers()
    try:
        r = await room(["누구를 말씀하시는지 확인할게요"])
        target = r.msg(JUNHO, "도배 도배")
        await r.say(BOSS, "소담아 걔 30분 뮤트해", reply_to=target)
        assert seen(r.llm, "요청의 '걔' = 답장한 메시지의 작성자 박준호 (ID 20)")
    finally:
        restore_timers(old)


@test
async def pronoun_uses_last_named_person_in_recent_turns_else_ask_back():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    chat, now = -1005550000077, int(time.time())
    for u in (BOSS, JUNHO, KIM):
        await db.upsert_user(u)
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                        (chat, u.id, now - 40 * 86400, now - 60))
    msg = SimpleNamespace(text="", reply_to_message=None, entities=())
    lines = await addressee.collect(svc, FakeBot(), msg, chat, BOSS, "소담아 그 사람 경고 줘")
    assert any("되물을 것" in ln for ln in lines)                        # 단서 없음 → 되묻기
    await memory.record_turn(db, chat, BOSS.id, "call", "박준호 요즘 어때?", "박준호 님은 어제 김철수 님과 얘기했어요", 5)
    await db._write("UPDATE ai_turns SET ts=?", (now - 120,))
    await memory.record_turn(db, chat, BOSS.id, "call", "김철수 메시지 몇 개야?", "김철수 님은 12개 보냈어요. 박준호 님도 봤어요", 6)
    lines = await addressee.collect(svc, FakeBot(), msg, chat, BOSS, "소담아 그 사람한테 경고 줘")
    text = "\n".join(lines)
    assert "★★★ 박준호 (ID 20)" in text and "'그 사람' = 박준호 (ID 20)" in text, text   # 최신 대화의 마지막 사람
    await memory.record_turn(db, chat, KIM.id, "call", "김철수 얘기", "김철수 님 반가워요", 7)   # 다른 사람 대화는 안 봄
    lines = await addressee.collect(svc, FakeBot(), msg, chat, BOSS, "쟤 누구야")
    assert any("'쟤' = 박준호" in ln for ln in lines)
    await db._write("UPDATE ai_turns SET ts=?", (now - 3600,))            # 30분 지난 대화는 안 봄
    lines = await addressee.collect(svc, FakeBot(), msg, chat, BOSS, "걔 누구야")
    assert any("되물을 것" in ln for ln in lines)
    assert not addressee._PRONOUN.search("그사람들이랑 저분야 이분법 걔속")   # 다른 낱말 속은 아님


# ── 숫자 검사 ──────────────────────────────────────────────
@test
def unsupported_numbers_normalizes():
    assert unsupported_numbers("회원 1,234명 · 3.50%", ["회원 1234 명, 비율 3.5"]) == []
    assert unsupported_numbers("오늘 17건, 0명", ["오늘 15건"]) == ["17"]
    assert unsupported_numbers("2024년 5월", []) == []                   # 단위 없는 숫자는 안 봄


async def kb_room(script):
    r = await room(script)
    await knowledge.add_document(r.db, r.CHAT, "모임 안내", "정기 모임 참가 인원: 1,234명\n회비는 2만원", "메시지", 1)
    return r


@test
async def number_not_in_tool_results_rechecks_once():
    old = fast_timers()
    try:
        r = await kb_room([tool_call("search_knowledge", {"query": "모임 인원"}), "참가 인원 1,500명이에요",
                           "참가 인원 1,234명이에요"])
        m = await r.say(BOSS, "소담아 모임 몇 명 와?")
        assert len(num_notes(r.llm)) == 1 and m.replies[-1].endswith("1,234명이에요") and not r.llm.script
        assert seen(r.llm, "숫자 1500 가")
        r = await kb_room([tool_call("search_knowledge", {"query": "모임"}), "999명이요", "888명이요"])
        await r.say(BOSS, "소담아 모임 몇 명 와?")                        # 또 틀려도 한 번만
        assert len(r.llm.of("chat")) == 3 and not r.llm.script
    finally:
        restore_timers(old)


@test
async def matching_number_or_no_tool_or_action_tool_only_no_recheck():
    old = fast_timers()
    try:
        r = await kb_room([tool_call("search_knowledge", {"query": "모임 인원"}), "1234명 와요!"])
        await r.say(BOSS, "소담아 모임 몇 명 와?")
        assert not num_notes(r.llm) and not r.llm.script
        r = await kb_room(["아마 50명쯤요"])                                # 도구 안 씀 → 검사 비용 0
        await r.say(BOSS, "소담아 모임 몇 명 와?")
        assert len(r.llm.of("chat")) == 1 and not r.llm.script
        r = await kb_room([tool_call("set_my_style", {"style": "친근"}), "말투 바꿨어요, 3번째 부탁이네요"])
        await r.say(BOSS, "소담아 말투 친근하게")                           # 읽기 도구가 아니면 숫자 검사 안 함
        assert not num_notes(r.llm) and not r.llm.script
    finally:
        restore_timers(old)


# ── 자료끼리 다름 ──────────────────────────────────────────
@test
async def knowledge_conflict_marks_newest_and_older():
    db = await make_db()
    chat = -1005550000088
    old_id, _, _ = await knowledge.add_document(db, chat, "가격표 8월", "수수료: 3%\n영업 시간: 10시~20시", "메시지", 1)
    new_id, _, _ = await knowledge.add_document(db, chat, "가격표 9월", "- 수수료: 5%\n영업 시간: 10시~20시", "메시지", 1)
    await db._write("UPDATE knowledge_docs SET updated_at=? WHERE id=?", (1_756_000_000, old_id))   # 2025-08
    await db._write("UPDATE knowledge_docs SET updated_at=? WHERE id=?", (1_758_900_000, new_id))   # 2025-09
    res = await knowledge.search(db, chat, "수수료 얼마")
    out = knowledge.format_results(res, None)
    assert "⚠️ 자료끼리 다름 [수수료]: 최신(2025-09-" in out and f"#{new_id}) 5% / 이전(2025-08-" in out, out
    assert f"#{old_id}) 3% — 최신 기준으로 답하고 다름을 짧게 알릴 것" in out
    assert "[영업시간]" not in out                                        # 숫자가 같으면 다름 아님
    await db._write("UPDATE knowledge_docs SET updated_at=? WHERE id=?", (1_760_000_000, old_id))   # 순서 바뀌면 최신도 바뀜
    out = knowledge.format_results(await knowledge.search(db, chat, "수수료 얼마"), None)
    assert f"#{old_id}) 3% / 이전(" in out
    filler = "\n".join(f"안내 문장 {i}번째 줄입니다 참고하세요" for i in range(60))   # 조각 여러 개인 한 문서
    await knowledge.add_document(db, -5, "한 문서", f"수수료: 3%\n{filler}\n수수료: 4%", "메시지", 1)
    res = await knowledge.search(db, -5, "수수료")
    assert len(res) >= 2 and "자료끼리 다름" not in knowledge.format_results(res, None)   # 같은 문서 안은 비교 안 함


@test
async def knowledge_updated_at_migration_for_old_db():
    db = await make_db()
    await db._write("ALTER TABLE knowledge_docs DROP COLUMN updated_at")
    await db._migrate()
    cols = {r["name"] for r in await db._all("PRAGMA table_info(knowledge_docs)")}
    assert "updated_at" in cols
    await db._write("INSERT INTO knowledge_docs(chat_id, title, source, added_by, chars, ts) VALUES(-7,'t','m',1,5,111)")
    [row] = await db._all("SELECT COALESCE(updated_at, ts) AS t FROM knowledge_docs WHERE chat_id=-7")
    assert row["t"] == 111


# ── 근거 보여줘 ────────────────────────────────────────────
@test
async def answer_sources_shows_categories_of_callers_previous_answer_only():
    old = fast_timers()
    try:
        got = []
        grab = lambda m: got.append(tool_text(m)) or "자료 보고 말했어요"   # noqa: E731
        r = await kb_room([tool_call("search_knowledge", {"query": "모임 인원"}), "1,234명 와요",
                           tool_call("answer_sources"), grab,
                           tool_call("answer_sources"), grab,                  # 연달아 물어도 진짜 답의 근거
                           tool_call("answer_sources"), grab,                  # 다른 사람은 내 답 근거 못 봄
                           "안녕하세요!", tool_call("answer_sources"), grab])
        await r.say(BOSS, "소담아 모임 몇 명 와?")
        await r.say(BOSS, "소담아 왜 그렇게 말했어?")
        assert "📚 자료 · search_knowledge(query=모임 인원)" in got[0] and "근거 종류: 📚 자료" in got[0], got[0]
        assert "· 요청: 모임 몇 명 와?\n" in got[0], got[0]
        await r.say(BOSS, "소담아 근거 보여줘")
        assert "· 요청: 모임 몇 명 와?\n" in got[1] and "answer_sources(" not in got[1], got[1]
        await r.say(KIM, "소담아 근거 보여줘")
        assert "기록이 없음" in got[2]
        await r.say(BOSS, "소담아 안녕")                                   # 도구 없이 한 답
        await r.say(BOSS, "소담아 그건 어디서 봤어?")
        assert "없음(대화만)" in got[3]
        await r.db._write("INSERT INTO agent_runs(chat_id, user_id, ts, steps, status) VALUES(?,?,?,?,?)",
                          (-100999, BOSS.id, int(time.time()), '[{"tool": "web_search", "args": "", "result": "x"}]',
                           "answered"))
        ctx = tools.ToolCtx(r.svc, r.bot, r.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(r.CHAT))
        out = await tools.execute("answer_sources", "{}", ctx)
        assert "🌐 웹" not in out and "answer_sources" not in out.split("근거")[0]   # 다른 방 기록은 안 봄
    finally:
        restore_timers(old)


@test
def source_kinds():
    assert tools.source_kind("search_knowledge") == "📚 자료" and tools.source_kind("web_search") == "🌐 웹"
    assert tools.source_kind("read_chat") == "📜 기록" and tools.source_kind("chat_stats") == "🗄️ DB"
    assert tools.source_kind("mute_member") == "🛠️ 실행" and "answer_sources" in tools.READ_ONLY


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
