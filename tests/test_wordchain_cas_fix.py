"""실제 사례 회귀: python tests/run_all.py wordchain_cas_fix

① 끝말잇기: 게임이 '우산'으로 시작했는데 AI 가 따로 '제가 먼저: 사과' → 그 AI 답에 '과일' 답장 → AI 가 '일 🍓' 로 대신 진행.
② 스팸 명단(CAS·lols) 차단: 관리자가 방에서 바로 [↩️ 차단 풀기] [🔕 스팸 명단 차단 끄기].
"""
from types import SimpleNamespace

from fake_llm import Room, reply, tool_call
from fakes import FakeBot, FakeJobQueue, FakeQuery, fake_user, make_db, make_svc, runner
from test_sanction_multi import A, BOSS, room

from sodam import games, handlers, memory
from sodam.cas import ALLOW_KEY

test, run_all = runner()


@test
async def game_start_sends_only_the_game_message():
    r = await room()
    r.llm.script = [tool_call("start_game", {"game": "끝말잇기"}), reply("좋아요 시작됐어요! 제가 먼저 갈게요: 사과")]
    m = await r.say(BOSS, "소담아 끝말잇기 하자")
    game = r.svc.games.active[Room.CHAT]
    posts = [c[2] for c in r.bot.named("send_message") if c[1] == Room.CHAT]
    assert len(posts) == 1 and game.last in posts[0] and not m.replies, (posts, m.replies)


@test
async def reply_to_ai_during_game_goes_to_game_and_ai_gets_hint():
    r = await room()
    r.llm.script = [tool_call("start_game", {"game": "끝말잇기"}), reply("x")]
    await r.say(BOSS, "소담아 끝말잇기 하자")
    game = r.svc.games.active[Room.CHAT]
    await memory.record_turn(r.db, Room.CHAT, BOSS.id, "call", "안녕", "안녕하세요", 555)
    ai_msg = SimpleNamespace(message_id=555, from_user=r.bot_user(), text="안녕하세요", caption=None)
    agent_calls = lambda: [c for c in r.llm.of("chat") if c.get("purpose") != "wordchain"]  # noqa: E731
    chats = len(agent_calls())
    word = next(w for ch in games.starts_for(game.last) for w in games._COMMON.get(ch, []) if w not in game.used)
    await r.say(A, word, reply_to=ai_msg)                           # AI 답에 답장으로 단 단어
    assert len(agent_calls()) == chats, "게임이 받음 (대화 AI 가 대신 진행 안 함, 소담이 수는 wordchain)"
    assert any(f"{word} →" in c[2] for c in r.bot.named("send_message")), "사전으로 바로 판정·봇이 이음"
    r.llm.script = [reply("단어를 쳐주세요")]
    await r.say(A, "소담아 이거 어떻게 해")
    user = r.llm.of("chat")[-1]["messages"][-1]["content"]
    assert "끝말잇기" in user and "단어를 내지 말고" in user and game.last in user, user[-600:]


async def cas_room():
    db = await make_db()
    svc = await make_svc(db, admins={1}, cas_banned={66})
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})
    return db, svc, bot, ctx


async def press(svc, bot, ctx, uid, data):
    q = FakeQuery(Room.CHAT, fake_user(uid, "누름"), data)
    q.message.chat_id, q.message.text_html = Room.CHAT, "🛡️ 차단 안내"
    await handlers.on_callback(SimpleNamespace(callback_query=q), ctx)
    return q


@test
async def cas_notice_has_admin_buttons_unban_sticks_and_off_works():
    db, svc, bot, ctx = await cas_room()
    await handlers.handle_new_member(ctx, Room.CHAT, "방", fake_user(66, "스패머"))
    [notice] = [c for c in bot.named("send_message") if "스팸 계정 명단" in c[2]]
    data = [b.callback_data for row in notice[3]["reply_markup"].inline_keyboard for b in row]
    assert data == ["cas:66", "cas:off"], data
    q = await press(svc, bot, ctx, 20, "cas:66")                       # 관리자 아님
    assert q.answers[-1][1] is True and not bot.named("unban")
    await press(svc, bot, ctx, 1, "cas:66")
    assert bot.named("unban") and await db.get_state(Room.CHAT, ALLOW_KEY.format(66))
    bans = len(bot.named("ban"))
    svc.joins.clear()                                      # 중복 입장 무시 창 비우기 (진짜 재입장)
    await handlers.handle_new_member(ctx, Room.CHAT, "방", fake_user(66, "스패머"))
    assert len(bot.named("ban")) == bans, "풀어준 사람은 이 방에서 다시 안 막음"
    await press(svc, bot, ctx, 1, "cas:off")
    assert (await db.get_settings(Room.CHAT))["cas_enabled"] is False


@test
async def manual_unban_of_cas_ban_also_sticks():
    """.밴해제·오너 [밴 해제] 처럼 버튼 말고 다른 길로 풀어도 이 방에선 다시 안 막음 (실제: 풀었는데 또 차단됨)."""
    db, svc, bot, ctx = await cas_room()
    await handlers.handle_new_member(ctx, Room.CHAT, "방", fake_user(66, "스패머"))
    assert bot.named("ban") and not await db.get_state(Room.CHAT, ALLOW_KEY.format(66))
    await svc.mod.unban(bot, Room.CHAT, 66, 1)                         # 관리자가 직접 풂
    assert await db.get_state(Room.CHAT, ALLOW_KEY.format(66))
    bans = len(bot.named("ban"))
    svc.joins.clear()
    await handlers.handle_new_member(ctx, Room.CHAT, "방", fake_user(66, "스패머"))
    assert len(bot.named("ban")) == bans
    await svc.mod.ban(bot, Room.CHAT, 78, 1, "광고")                    # 스팸 명단 밴이 아니면 예외 안 만듦
    await svc.mod.unban(bot, Room.CHAT, 78, 1)
    assert not await db.get_state(Room.CHAT, ALLOW_KEY.format(78))
