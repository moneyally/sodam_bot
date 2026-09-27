"""🎓 다른 봇 명령 프로필 (sodam/botskills.py · panels/botlink.py 🎓): python tests/run_all.py botskills

실제 사례: 방에 멜론 음악봇·유튜브 봇이 있고 관리자가 "소담아 멜론에 밤편지 신청해줘 / 유튜브로 틀어줘" →
소담이 그 봇의 명령(/play@봇 …)을 알아야 함. 가짜 텔레그램·가짜 LLM·가짜 Telethon 만 씀.
"""
import time
from types import SimpleNamespace

from fake_llm import Room, fast_timers, reply, restore_timers, tool_call
from fakes import FakeMsg, FakeQuery, fake_user, runner
from test_botlink import blroom, bot_says, card_buttons, press_room, room_sends, trust
from test_sanction_multi import A, BOSS, ask

from sodam import botlink, botskills, menu, mtproto

test, run_all = runner()
MELON = fake_user(770011, "멜론봇", "melon_bot", is_bot=True)
YT = fake_user(770012, "유튜브뮤직", "ytmusic_player_bot", is_bot=True)
DICE = fake_user(770001, "주사위봇", "dice_bot", is_bot=True)
LINK = "https://youtu.be/dQw4w9WgXcQ"


async def dm(r, user, data):
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers                             # 버튼은 answer 한 번
    for row in (q.kb.inline_keyboard if q.kb else ()):
        for b in row:
            assert len(b.callback_data.encode()) <= 64, b.callback_data
    return q


async def typed(r, user, text):
    msg = FakeMsg(user.id, user, text)
    assert await menu.handle_input(r.svc, r.bot, msg)
    return msg


def sk_map(rows):
    return {x["command"]: (x["source"], x["intent"], x["args_hint"]) for x in rows}


def sent_cmds(r):
    return [c[2] for c in r.bot.named("send_message") if c[2].startswith("/")]


@test
async def preset_apply_badges_and_panel_permissions():
    r = await blroom("interact")
    await trust(r, MELON)
    cid, bid = Room.CHAT, MELON.id
    q = await dm(r, BOSS, f"m:blkb:{cid}:{bid}")
    assert f"m:bsk:{cid}:{bid}" in [b.callback_data for row in q.kb.inline_keyboard for b in row]
    q = await dm(r, BOSS, f"m:bsk:{cid}:{bid}")
    assert "아직 없음" in q.edits[-1]
    q = await dm(r, BOSS, f"m:bskp:{cid}:{bid}:mu")
    got = sk_map(await botskills.skills(r.db, cid, bid))
    assert got == {"/play": ("preset", "play", "{곡}"), "/skip": ("preset", "skip", ""), "/pause": ("preset", "pause", ""),
                   "/resume": ("preset", "resume", ""), "/stop": ("preset", "stop", ""),
                   "/queue": ("preset", "queue", "")}, got
    assert "📌직접" in q.edits[-1] and "6개" in q.answers[-1][0]
    await dm(r, BOSS, f"m:bskp:{cid}:{bid}:yt")                         # 같은 명령은 새 묶음 값으로 (newest wins)
    assert sk_map(await botskills.skills(r.db, cid, bid))["/play"][2] == "{검색어 또는 링크}"
    q = await dm(r, A, f"m:bskp:{cid}:{bid}:gm")                       # 멤버는 못 함
    assert q.answers[-1][1] and "/dice" not in sk_map(await botskills.skills(r.db, cid, bid))
    q = await dm(r, BOSS, f"m:bskp:{cid}:{bid}:zz")                    # 없는 묶음
    assert q.answers[-1][1]
    assert not room_sends(r)


@test
async def admin_add_edit_delete_via_input():
    r = await blroom("interact")
    await trust(r, MELON)
    cid, bid = Room.CHAT, MELON.id
    await dm(r, BOSS, f"m:in:{cid}:bsk:{bid}")
    for bad in ("play https://evil.xyz", "p!ay x", "p " + "가" * 31, "p @someone"):
        msg = await typed(r, BOSS, bad)
        assert "다시 보내" in msg.replies[-1], (bad, msg.replies)
    assert not await botskills.skills(r.db, cid, bid)
    msg = await typed(r, BOSS, "p {곡} 재생")
    assert "저장" in msg.replies[-1] and bid not in r.svc.inputs
    assert sk_map(await botskills.skills(r.db, cid, bid)) == {"/p": ("manual", "play", "{곡} 재생")}
    q = await dm(r, BOSS, f"m:bsk:{cid}:{bid}")
    datas = [b.callback_data for row in q.kb.inline_keyboard for b in row]
    edit = next(d for d in datas if d.startswith("m:bske:"))
    delete = next(d for d in datas if d.startswith("m:bskd:"))
    q = await dm(r, BOSS, edit)
    assert "지금" in q.edits[-1] and "p {곡}" in q.edits[-1]
    assert not await menu.handle_input(r.svc, r.bot, FakeMsg(BOSS.id, BOSS, "/request {곡}")), \
        "'/' 로 시작하면 1:1 명령으로 넘어감 → 안내문이 '/ 빼고' 라고 함"
    await typed(r, BOSS, "request {곡}")
    assert sk_map(await botskills.skills(r.db, cid, bid)) == {"/request": ("manual", "play", "{곡}")}, "고치기 = 원래 줄 교체"
    q = await dm(r, BOSS, delete)                                       # 옛 버튼(확인값 다름) → 거절
    assert q.answers[-1][1] and await botskills.skills(r.db, cid, bid)
    q = await dm(r, BOSS, f"m:bsk:{cid}:{bid}")
    delete = next(b.callback_data for row in q.kb.inline_keyboard for b in row if b.callback_data.startswith("m:bskd:"))
    q = await dm(r, A, delete)                                          # 멤버는 못 지움
    assert q.answers[-1][1] and await botskills.skills(r.db, cid, bid)
    q = await dm(r, BOSS, delete)
    assert not await botskills.skills(r.db, cid, bid) and "지웠어요" in q.answers[-1][0]
    assert {x["action"] for x in await r.db._all("SELECT action FROM mod_log")} >= {"botlink_skill"}


@test
async def input_handler_rejects_slash_prefixed_passthrough_and_cap():
    # 1:1 입력 엔진은 '/' 로 시작하는 글을 명령으로 넘김 → parse 는 '/' 있어도 없어도 같게, 안내문은 '/' 빼라고
    assert botskills.parse_input("/play {곡}", "melon_bot") == ("/play", "{곡}")
    assert botskills.parse_input("play {곡}", None) == ("/play", "{곡}")
    assert isinstance(botskills.parse_input("play <b>", "x"), str)
    r = await blroom("interact")
    await trust(r, MELON)
    for i in range(botskills.MAX_SKILLS):
        assert await botskills.set_manual(r.db, Room.CHAT, MELON.id, f"/c{i}", "")
    assert not await botskills.set_manual(r.db, Room.CHAT, MELON.id, "/extra", "")
    assert await botskills.set_manual(r.db, Room.CHAT, MELON.id, "/c0", "다시"), "있는 명령 고치기는 상한과 무관"


@test
async def auto_learn_from_member_command_and_bot_reply():
    old = fast_timers()
    try:
        r = await blroom("observe")
        r.llm.script = []                                               # AI 가 불리면 실패
        await bot_says(r, MELON, "🎵 멜론봇 대기 중")
        await bot_says(r, DICE, "🎲 주사위봇")
        cid = Room.CHAT
        m = await r.say(A, "/play@melon_bot 아이유 밤편지")
        await bot_says(r, MELON, "▶️ 아이유 - 밤편지 재생", reply_to=m)
        rows = await botskills.skills(r.db, cid, MELON.id)
        assert sk_map(rows) == {"/play": ("seen", "play", botskills.SEEN_HINT)} and rows[0]["count"] == 1
        assert "밤편지" not in str([dict(x) for x in await r.db._all("SELECT * FROM botlink_skills")]), "인자 글자 저장 안 함"
        # 봇 글에 답장으로 '/skip' (@ 없이)
        bm = await bot_says(r, MELON, "지금 곡: 밤편지")
        m = await r.say(A, "/skip", reply_to=bm)
        await bot_says(r, MELON, "⏭ 건너뜀", reply_to=m)
        # 또 쓰면 횟수만
        m = await r.say(A, "/play@melon_bot 라일락")
        await bot_says(r, MELON, "▶️ 라일락", reply_to=m)
        got = {x["command"]: (x["source"], x["count"], x["args_hint"]) for x in await botskills.skills(r.db, cid, MELON.id)}
        assert got == {"/play": ("seen", 2, botskills.SEEN_HINT), "/skip": ("seen", 1, "")}, got
        # ✗ 봇이 답하지 않은 명령 (다른 글에 답함)
        m = await r.say(A, "/queue@melon_bot")
        await bot_says(r, MELON, "아무 말", reply_to=await r.say(A, "딴 얘기"))
        await bot_says(r, MELON, "아무 말2")
        # ✗ 10초 넘어 답함
        m2 = await r.say(A, "/pause@melon_bot")
        st = botskills.state(r.svc)
        k = (cid, m2.message_id)
        st.pending[k] = st.pending[k][:3] + (time.time() - botskills.LEARN_WINDOW - 1,)
        await bot_says(r, MELON, "일시정지", reply_to=m2)
        # ✗ 다른 봇 앞으로 보낸 명령에 이 봇이 답함
        m3 = await r.say(A, "/stop@dice_bot")
        await bot_says(r, MELON, "정지", reply_to=m3)
        # ✗ 봇끼리 (주사위봇이 멜론봇에게 명령 → 멜론봇 답)
        bm = await bot_says(r, DICE, "/resume@melon_bot")
        await bot_says(r, MELON, "다시 재생", reply_to=bm)
        # ✗ 등록 안 된 봇 · 소담 앞 명령
        m4 = await r.say(A, "/play@unknown_bot 곡")
        await bot_says(r, fake_user(770099, "새봇", "unknown_bot", is_bot=True), "재생", reply_to=m4)
        await r.say(A, "/help@sodambot")
        assert set(sk_map(await botskills.skills(r.db, cid, MELON.id))) == {"/play", "/skip"}
        assert not await botskills.skills(r.db, cid, 770099) and not await botskills.skills(r.db, cid, DICE.id)
        assert not r.llm.calls
        # 무시하면 배운 것도 지움 · 꺼진 방은 안 배움
        await botlink.set_status(r.db, cid, MELON.id, "ignored")
        assert not await botskills.skills(r.db, cid, MELON.id)
        await r.db.set_setting(cid, "botlink_mode", "off")
        await botlink.set_status(r.db, cid, MELON.id, "seen")
        m = await r.say(A, "/play@melon_bot 곡")
        assert not botskills.state(r.svc).pending.get((cid, m.message_id))
    finally:
        restore_timers(old)


@test
async def learned_commands_capped_per_bot():
    r = await blroom("observe")
    await bot_says(r, MELON, "hi")
    await botskills.set_manual(r.db, Room.CHAT, MELON.id, "/go", "{곡} 재생")
    await botskills.record_seen(r.db, Room.CHAT, MELON.id, "/go", True)       # 본 것은 관리자 설명·intent 를 안 덮음
    [row] = await botskills.skills(r.db, Room.CHAT, MELON.id)
    assert (row["source"], row["intent"], row["args_hint"], row["count"]) == ("manual", "play", "{곡} 재생", 1), dict(row)
    for i in range(botskills.MAX_SKILLS + 5):
        await botskills.record_seen(r.db, Room.CHAT, MELON.id, f"/x{i}", False)
    assert len(await botskills.skills(r.db, Room.CHAT, MELON.id)) == botskills.MAX_SKILLS


@test
async def intent_play_and_skip_map_to_bot_commands_with_first_use_card():
    r = await blroom("interact")
    await trust(r, MELON)
    await botskills.apply_preset(r.db, Room.CHAT, MELON.id, "mu")
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "멜론", "intent": "play", "query": "아이유 밤편지"})])
    assert "확인 버튼" in res[0] and not sent_cmds(r), res
    card = [c for c in r.bot.named("send_message") if "보낼까요" in c[2]][-1]
    assert "/play@melon_bot 아이유 밤편지" in card[2]
    ok, _ = card_buttons(r)
    await press_room(r, BOSS, ok)
    await r.settle()
    assert sent_cmds(r) == ["/play@melon_bot 아이유 밤편지"]
    botlink.WAIT_SECONDS, old = 0.01, botlink.WAIT_SECONDS
    try:
        res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "melon", "intent": "play", "query": "라일락"})])
        assert "보냈음" in res[0] and sent_cmds(r)[-1] == "/play@melon_bot 라일락", res     # 승인된 명령은 바로
        res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "멜론봇", "intent": "skip"})])
        assert "확인 버튼" in res[0], "새 명령(/skip)은 다시 확인 카드"
        assert "/skip@melon_bot" in [c for c in r.bot.named("send_message") if "보낼까요" in c[2]][-1][2]
        # 같은 intent 가 둘이면 가장 최근 것 (newest wins)
        await r.db._write("UPDATE botlink_skills SET updated=updated-100")
        await botskills.set_manual(r.db, Room.CHAT, MELON.id, "/next", "다음 곡")
        assert (await botskills.for_intent(r.db, Room.CHAT, MELON.id, "skip"))["command"] == "/next"
    finally:
        botlink.WAIT_SECONDS = old


@test
async def youtube_link_only_for_play_and_search():
    assert botlink.build(f"/play {LINK}", "yt_bot", wide=True) == ("/play", f"/play@yt_bot {LINK}")
    assert botlink.build("/search 아이유 https://www.youtube.com/watch?v=dQw4w9WgXcQ", "yt_bot", wide=True)
    for bad in ("https://evil.xyz/x", "https://m.youtube.com/watch?v=dQw4w9WgXcQ", LINK + "&x=1",
                "https://youtu.be/dQw4w9WgXcQ?list=1", "http://youtu.be/dQw4w9WgXcQ", "@yt " + LINK, "t.me/+abc"):
        assert botlink.build(f"/play {bad}", "yt_bot", wide=True) is None, bad
    assert botlink.build(f"/play {LINK}", "yt_bot") is None, "wide 아니면 링크 안 됨"
    assert botlink.build("/play " + "가" * 90, "b", wide=True) and not botlink.build("/play " + "가" * 90, "b")
    assert not botlink.build("/play " + "가" * 101, "b", wide=True)
    r = await blroom("interact")
    await trust(r, YT)
    await trust(r, DICE)
    await botskills.apply_preset(r.db, Room.CHAT, YT.id, "yt")
    await botskills.apply_preset(r.db, Room.CHAT, DICE.id, "gm")
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "유튜브", "intent": "play", "query": LINK})])
    assert "확인 버튼" in res[0], res
    assert f"/play@ytmusic_player_bot {LINK}" in [c for c in r.bot.named("send_message") if "보낼까요" in c[2]][-1][2]
    ok, _ = card_buttons(r)
    await press_room(r, BOSS, ok)                                       # 누를 때도 wide 로 다시 검사 → 보냄
    await r.settle()
    assert sent_cmds(r) == [f"/play@ytmusic_player_bot {LINK}"]
    for a in ({"bot": "유튜브", "intent": "play", "query": "https://evil.xyz/x"},
              {"bot": "유튜브", "intent": "skip", "query": LINK},
              {"bot": "dice_bot", "command": f"/bet {LINK}"},
              {"bot": "dice_bot", "intent": "bet", "query": "1" * 70}):
        res = await ask(r, BOSS, [tool_call("bot_command", a)])
        assert "형식" in res[0], (a, res)
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "유튜브", "command": f"/play {LINK}"})])
    assert "형식" not in res[0], "직접 준 /play 도 그 명령이 재생이면 링크 허용"
    assert sent_cmds(r) == [f"/play@ytmusic_player_bot {LINK}"] * 2


@test
async def unknown_intent_returns_known_list_and_how_to_add():
    r = await blroom("interact")
    await trust(r, DICE)
    await botskills.apply_preset(r.db, Room.CHAT, DICE.id, "gm")
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "dice_bot", "intent": "play", "query": "밤편지"})])
    assert "표시돼 있지 않음" in res[0] and "/dice" in res[0] and "/bet {금액}(bet)" in res[0] and "🎓 명령 배우기" in res[0], res
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "dice_bot", "intent": "fly"})])
    assert "intent 는" in res[0]
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "dice_bot"})])
    assert "필요" in res[0]
    assert not room_sends(r)


@test
async def ambiguous_music_bots_return_candidates():
    r = await blroom("interact")
    for b in (MELON, YT, DICE):
        await trust(r, b)
    await botskills.apply_preset(r.db, Room.CHAT, MELON.id, "mu")
    await botskills.apply_preset(r.db, Room.CHAT, YT.id, "yt")
    await botskills.apply_preset(r.db, Room.CHAT, DICE.id, "gm")
    res = await ask(r, BOSS, [tool_call("bot_command", {"intent": "play", "query": "밤편지"})])
    assert "후보" in res[0] and "@melon_bot" in res[0] and "@ytmusic_player_bot" in res[0] and "dice" not in res[0], res
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "벅스", "intent": "play", "query": "밤편지"})])
    assert "후보" in res[0], "아무 봇 이름과도 안 맞으면 되묻기"
    assert not [c for c in r.bot.named("send_message") if "보낼까요" in c[2]]
    for q, want in (("멜론", "@melon_bot"), ("유튜브", "@ytmusic_player_bot"), ("youtube", "@ytmusic_player_bot")):
        res = await ask(r, BOSS, [tool_call("bot_command", {"bot": q, "intent": "play", "query": "밤편지"})])
        assert "확인 버튼" in res[0], (q, res)
        assert f"/play{want} 밤편지" in [c for c in r.bot.named("send_message") if "보낼까요" in c[2]][-1][2]
    await botlink.set_status(r.db, Room.CHAT, YT.id, "ignored")          # 음악봇이 하나만 남으면 이름 없이도
    res = await ask(r, BOSS, [tool_call("bot_command", {"intent": "play", "query": "밤편지"})])
    assert "확인 버튼" in res[0] and "/play@melon_bot 밤편지" in [
        c for c in r.bot.named("send_message") if "보낼까요" in c[2]][-1][2]
    assert not sent_cmds(r)


class HelperClient:
    """Telethon 흉내: get_input_entity(아이디) + users.getFullUser → bot_info.commands."""

    def __init__(self, commands=(), error=None):
        self.cmds, self.error, self.calls = list(commands), error, []

    def is_connected(self):
        return True

    async def get_input_entity(self, who):
        self.calls.append(("entity", who))
        if self.error:
            raise self.error
        return SimpleNamespace(user=who)

    async def __call__(self, req):
        self.calls.append(type(req).__name__)
        return SimpleNamespace(full_user=SimpleNamespace(bot_info=SimpleNamespace(
            commands=[SimpleNamespace(command=c, description=d) for c, d in self.cmds])))


def with_helper(r, client):
    mt = mtproto.MTProto(r.svc.cfg, r.db)
    mt.bot.client = client
    r.svc.mtproto = mt
    return mt


@test
async def helper_fetches_bot_commands_without_overwriting_admin_rows():
    old_gap = mtproto.MIN_GAP
    mtproto.MIN_GAP = 0
    try:
        r = await blroom("interact")
        await trust(r, MELON)
        await botskills.set_manual(r.db, Room.CHAT, MELON.id, "/play", "{곡} 신청")
        client = HelperClient([("play", "노래 재생 https://x.y"), ("skip", "다음 곡으로"), ("Bad-Cmd", "x"),
                               ("queue", "대기열 <b>보기</b>")])
        with_helper(r, client)
        n = await botskills.helper_fetch(r.svc, Room.CHAT, MELON.id, "melon_bot")
        assert n == 2 and ("entity", "melon_bot") in client.calls and "GetFullUserRequest" in client.calls
        got = sk_map(await botskills.skills(r.db, Room.CHAT, MELON.id))
        assert got == {"/play": ("manual", "play", "{곡} 신청"), "/skip": ("helper", "skip", "다음 곡으로"),
                       "/queue": ("helper", "queue", "")}, got
        q = await dm(r, BOSS, f"m:bsk:{Room.CHAT}:{MELON.id}")
        assert "🔧헬퍼" in q.edits[-1] and "📌직접" in q.edits[-1]
        assert any(b.callback_data.startswith("m:bskh:") for row in q.kb.inline_keyboard for b in row)
        q = await dm(r, BOSS, f"m:bskh:{Room.CHAT}:{MELON.id}")
        assert "불러왔어요" in q.answers[-1][0]
        q = await dm(r, BOSS, f"m:bskh:{Room.CHAT}:{MELON.id}")          # 연타
        assert "지금은" in q.answers[-1][0]
        # 오류는 무시 (예외 안 남, 기존 줄 그대로)
        with_helper(r, HelperClient(error=RuntimeError("USER_ID_INVALID")))
        assert await botskills.helper_fetch(r.svc, Room.CHAT, MELON.id, "melon_bot") is None
        assert len(await botskills.skills(r.db, Room.CHAT, MELON.id)) == 3
        # 봇이 말하면 하루 1번 뒤에서 자동 (한 번만)
        r2 = await blroom("observe")
        c2 = HelperClient([("dice", "주사위 굴리기")])
        with_helper(r2, c2)
        await bot_says(r2, DICE, "🎲")
        await bot_says(r2, DICE, "🎲🎲")
        for t in list(botskills.state(r2.svc).tasks):
            await t
        assert c2.calls.count("GetFullUserRequest") == 1
        assert sk_map(await botskills.skills(r2.db, Room.CHAT, DICE.id)) == {"/dice": ("helper", "dice", "주사위 굴리기")}
        r2.svc.mtproto = None                                          # 헬퍼 없음 → 조용히 None
        assert await botskills.helper_fetch(r2.svc, Room.CHAT, DICE.id, "dice_bot") is None
    finally:
        mtproto.MIN_GAP = old_gap


@test
async def echo_bot_with_intent_still_one_send():
    """따라 말하는 봇 + intent 경로: 관리자 한 번 → 명령 1번, 봇의 답(명령 섞인)으로는 배우지도 보내지도 않음."""
    old = fast_timers()
    try:
        r = await blroom("interact", echo=True, ai_chime_in=True)
        await trust(r, DICE)
        await botskills.apply_preset(r.db, Room.CHAT, DICE.id, "gm")
        await botlink.approve(r.db, Room.CHAT, DICE.id, "/dice", BOSS.id)
        r.llm.script = [tool_call("bot_command", {"bot": "주사위", "intent": "dice"}), reply("4 나왔어요!")]
        m = await r.say(BOSS, "소담아 주사위봇 굴려줘")
        for _ in range(5):
            await r.settle()
        assert m.replies == ["4 나왔어요!"], m.replies
        ai = SimpleNamespace(message_id=m.message_id + 10_000, from_user=r.bot_user())
        for i in range(10):
            await bot_says(r, DICE, f"/dice@sodambot {i}", reply_to=ai)
        assert sent_cmds(r) == ["/dice@dice_bot"] and r.bot.echoes == 1
        assert {x["source"] for x in await botskills.skills(r.db, Room.CHAT, DICE.id)} == {"preset"}
    finally:
        restore_timers(old)


if __name__ == "__main__":
    run_all()


@test
async def learns_commands_from_bot_help_text_and_defaults_play():
    """실제 사례: 멜론봇이 /help 로 /play·/skip… 을 다 보여줬는데 소담은 /help 만 배워서 '재생 명령 모름'."""
    r = await blroom("interact")
    await trust(r, MELON)
    help_text = ("🍈 멜론뮤직 사용법\n▶️ /play 곡명 또는 링크(유튜브·사운드클라우드) — 노래 재생 / 대기열 추가\n"
                 "⏭ /skip — 다음 곡으로\n⏸ /pause · ▶️ /resume — 일시정지 / 다시재생\n📜 /queue — 대기열 보기\n"
                 "🙋 안 들어오면 /userbotjoin")
    await bot_says(r, MELON, help_text)
    got = sk_map(await botskills.skills(r.db, Room.CHAT, MELON.id))
    assert got["/play"][:2] == ("help", "play") and got["/skip"][1] == "skip" and got["/resume"][1] == "resume", got
    await bot_says(r, MELON, "재생 시작 /play 로 신청됨")                   # 명령 하나뿐인 답 = 사용법 아님
    assert len(await botskills.skills(r.db, Room.CHAT, MELON.id)) == len(got)
    res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "멜론", "intent": "play", "query": "먼데이키즈 발자국"})])
    assert "확인 버튼" in res[0], res
    assert "/play@melon_bot 먼데이키즈 발자국" in [c for c in r.bot.named("send_message") if "보낼까요" in c[2]][-1][2]
    r2 = await blroom("interact")
    await trust(r2, YT)                                                   # 배운 게 없으면 하드코딩 기본값 없이 목록·안내
    res = await ask(r2, BOSS, [tool_call("bot_command", {"bot": "유튜브", "intent": "play", "query": "밤편지"})])
    assert "따로 표시돼 있지 않음" in res[0] and not [c for c in r2.bot.named("send_message") if "보낼까요" in c[2]], res
    await r2.db._write("INSERT INTO botlink_msgs(chat_id, bot_id, msg_id, ts, text, to_user, to_us) VALUES(?,?,?,?,?,?,?)",
                       (Room.CHAT, YT.id, 999, int(time.time()), "사용법\n/yplay 검색어 — 재생\n/next — 다음 곡", None, 0))
    assert await botskills.learn_from_history(r2.db, Room.CHAT, YT.id)    # 기능 전에 기록된 사용법 글도 DB 로 배움
    got = sk_map(await botskills.skills(r2.db, Room.CHAT, YT.id))
    assert got["/yplay"][0] == "help" and "/next" in got, got
