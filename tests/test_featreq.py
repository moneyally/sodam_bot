"""💡 기능 요청 받기 (sodam/featreq.py · sodam/panels/featreq.py): python tests/run_all.py featreq

진짜 handlers(방 메시지·1:1·버튼 콜백) + 대본 LLM(도구 호출을 대본으로) 으로 돈다. 실제 OpenAI 호출 없음.
"""
import asyncio
import time
from types import SimpleNamespace

from fake_llm import Room, fast_timers, reply, restore_timers, tool_call
from fakes import FakeMsg, FakeQuery, fake_user, runner

from sodam import featreq, handlers, menu, tools
from sodam.menu import PanelCtx
from sodam.panels import featreq as fr_panel
from sodam.permissions import Role

test, run_all = runner()

BOSS, OWNER = fake_user(1, "방장", "boss"), fake_user(7, "오너", "owner")
A, B, C = fake_user(20, "민지 <b>사장</b>", "minji"), fake_user(21, "준호&", "junho"), fake_user(22, "수진", "sujin")
ASK = "소담아 이런 기능 있어? 입장할 때 규칙 퀴즈 내는 거"
QUIZ = {"summary": "입장할 때 규칙 퀴즈", "detail": "새로 들어온 사람에게 규칙 퀴즈를 내고 맞히면 말하기"}


async def room():
    r = await Room().open(admins={BOSS.id})
    r.svc.perms.owner_ids = {OWNER.id}
    for u in (BOSS, A, B, C):
        await r.join(u)
    return r


async def say(r, user, text, args, answer="운영자에게 전달했어요!"):
    """방에서 '소담아 …' → AI 가 feature_request 를 부르는 대본. 도구 결과(문자열)를 돌려준다."""
    r.llm.script = [tool_call("feature_request", args), reply(answer)]
    old = fast_timers()
    try:
        m = await r.say(user, text)
    finally:
        restore_timers(old)
    chats = r.llm.of("chat")
    assert any(t["function"]["name"] == "feature_request" for t in chats[0]["tools"]), "도구 목록에 있어야 AI 가 씀"
    results = [x["content"] for x in chats[-1]["messages"] if x["role"] == "tool"]
    r.llm.calls.clear()
    return results[-1], m


async def dm(r, user, text):
    msg = FakeMsg(user.id, user, text, message_id=int(time.time() * 1000) % 1_000_000)
    await handlers.on_private(SimpleNamespace(message=msg), r.ctx)
    await r.settle()
    return msg


async def press(r, user, data):
    q = FakeQuery(user.id, user, data)
    r.svc.menu_limiter._hits.clear()
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    return q


async def groups(r, statuses=featreq.OPEN + ("done", "wont")):
    return await featreq.list_groups(r.db, statuses, "n", 100)


def sent_to(r, chat_id):
    return [c for c in r.bot.named("send_message") if c[1] == chat_id]


# ── 1. 도구: 누구나·어디서나, 기록 읽은 답변(tainted)에선 안 됨, '못 해요' 대신 쓰라는 설명 ─────
@test
def tool_scope_and_description():
    for role in (Role.MEMBER, Role.ADMIN, Role.OWNER):
        for in_dm in (False, True):
            assert "feature_request" in {t.name for t in tools.available(role, {}, in_dm)}, (role, in_dm)
    assert "feature_request" not in tools.READ_ONLY
    desc = tools._BY_NAME["feature_request"].description
    assert "못 해요" in desc and "운영자" in desc and "약속하지" in desc


# ── 2. 방에서 멤버가 물음 → 저장 → 답 ─────────────────────
@test
async def member_in_room_request_is_stored_and_answered():
    r = await room()
    res, m = await say(r, A, ASK, QUIZ)
    assert "운영자에게" in res and "약속하지" in res, res
    [g] = await groups(r)
    assert g["summary"] == "입장할 때 규칙 퀴즈" and g["status"] == "new" and g["voters"] == 1
    [it] = await featreq.items(r.db, g["id"])
    assert it["user_id"] == A.id and it["user_name"] == "민지 <b>사장</b>" and it["chat_id"] == Room.CHAT
    assert "규칙 퀴즈를 내고" in it["text"] and it["count"] == 1
    assert m.replies == ["운영자에게 전달했어요!"], m.replies                  # AI 답이 요청 메시지에 답장으로


# ── 3. 같은 사람 24시간 안 = 한 번 더 셈 · 다른 사람 비슷한 요청 = 묶음 👍 · 다른 요청 = 새 묶음 ─
@test
async def dedupe_same_person_and_group_different_people():
    r = await room()
    await say(r, A, ASK, QUIZ)
    res, _ = await say(r, A, "소담아 기능 요청: 입장 때 규칙퀴즈", {"summary": "입장 때 규칙퀴즈 기능"})
    assert "이미 운영자에게 전달" in res, res
    [g] = await groups(r)
    [it] = await featreq.items(r.db, g["id"])
    assert it["count"] == 2, "같은 사람은 새 줄 대신 +1"
    res, _ = await say(r, B, "소담아 들어올 때 규칙 퀴즈 되면 좋겠다", {"summary": "입장할때 규칙 퀴즈 내기"})
    assert "묶었음" in res and "2명" in res, res
    await say(r, BOSS, "소담아 출석 체크 기능 있어?", {"summary": "출석 체크"})
    rows = await groups(r)
    assert len(rows) == 2, [dict(x) for x in rows]
    quiz = next(x for x in rows if "퀴즈" in x["summary"])
    assert quiz["voters"] == 2 and quiz["asks"] == 3
    # 24시간 지난 뒤 같은 사람이 또 말하면 같은 묶음에 새 줄(👍 는 그대로 2명)
    await r.db._write("UPDATE featreq_items SET last_ts=last_ts-90000 WHERE user_id=?", (A.id,))
    await featreq.submit(r.db, A.id, "민지", Room.CHAT, "입장할 때 규칙 퀴즈")
    assert len(await featreq.items(r.db, quiz["id"])) == 3 and (await featreq.get_group(r.db, quiz["id"]))["voters"] == 2
    res, _ = await say(r, C, ASK, {"summary": "입장할 때 규칙 퀴즈"})
    assert "3명" in res, "👍 = 서로 다른 사람 수 (같은 사람 두 줄은 1명)"
    # 비슷함 판정
    n = featreq.normalize
    assert featreq.similar(n("입장 때 규칙 퀴즈"), n("입장할 때 규칙 퀴즈 내는 기능"))
    assert featreq.similar(n("출석 체크"), n("매일 출석체크 해줘"))
    assert not featreq.similar(n("음성 메시지 요약"), n("음성 메시지 번역"))
    assert not featreq.similar(n("공지 번역"), n("공지 예약"))
    assert not featreq.similar(n("입장 규칙 퀴즈"), n("입장 인사"))


# ── 4. 하루 한도 · 너무 짧은 요청 · 글은 500자·제어 글자 제거 ─────
@test
async def rate_limit_short_and_clean_text():
    r = await room()
    topics = ["출석 체크", "투표 만들기", "환율 알림", "생일 축하", "음성 메시지 요약"]
    for t in topics:
        res, _ = await say(r, C, f"소담아 기능 요청: {t}", {"summary": t})
        assert "전달했음" in res, res
    res, _ = await say(r, C, "소담아 기능 요청: 공지 번역", {"summary": "공지 번역"}, "오늘은 더 못 보내요")
    assert "다 썼음" in res, res
    assert len(await groups(r)) == 5, "한도 넘은 요청은 저장 안 됨"
    res, _ = await say(r, B, "소담아 기능 요청: ㅋ", {"summary": "ㅋ"})
    assert "너무 짧아" in res, res
    long = "‮<b>굵게</b>\x07 " + "가" * 900
    g = await featreq.submit(r.db, B.id, "준호", Room.CHAT, "긴 요청 테스트", long)
    [it] = await featreq.items(r.db, g.group_id)
    assert len(it["text"]) <= featreq.MAX_TEXT and "‮" not in it["text"] and "\x07" not in it["text"]
    assert "<b>굵게</b>" in it["text"], "저장은 원문 그대로(데이터), 화면에서 esc"


# ── 5. 1:1 에서도 · 방 없음(NULL) ──────────────────────────
@test
async def request_from_dm():
    r = await room()
    r.llm.script = [tool_call("feature_request", {"summary": "음성 메시지 요약"}), reply("운영자에게 전달했어요.")]
    old = fast_timers()
    try:
        msg = await dm(r, B, "기능 요청: 음성 메시지 요약해 주는 거")
    finally:
        restore_timers(old)
    assert not r.llm.script, "대본대로 도구를 부름"
    [g] = await groups(r)
    [it] = await featreq.items(r.db, g["id"])
    assert it["chat_id"] is None and it["user_id"] == B.id
    assert msg.replies == ["운영자에게 전달했어요."], msg.replies


# ── 6. 오너 화면: 오너만 · 목록·자세히(esc) · 상태 한 번만 ─────
@test
async def owner_screens_permission_and_once():
    r = await room()
    await say(r, A, ASK, QUIZ)
    await say(r, B, ASK, {"summary": "입장할 때 규칙 퀴즈", "detail": "<script>x</script> & 퀴즈"})
    [g] = await groups(r)
    gid = g["id"]
    text, kb = await menu.main_menu(r.svc, r.bot, OWNER.id)
    assert "m:fr" in [b.callback_data for row in kb.inline_keyboard for b in row]
    _, kb = await menu.main_menu(r.svc, r.bot, BOSS.id)
    assert "m:fr" not in [b.callback_data for row in kb.inline_keyboard for b in row], "방 관리자 메인엔 없음"
    for who in (BOSS, A):
        for data in ("m:fr", f"m:frv:{gid}", f"m:frs:{gid}:doing", f"m:frd:{gid}", f"m:frdn:{gid}", f"m:frxy:{gid}"):
            q = await press(r, who, data)
            assert not q.edits and q.answers[0][1], (who.id, data, q.answers)
    assert (await featreq.get_group(r.db, gid))["status"] == "new" and who.id not in r.svc.inputs
    q = await press(r, OWNER, "m:fr")
    assert "입장할 때 규칙 퀴즈" in q.edits[-1] and "👍 2명" in q.edits[-1]
    q = await press(r, OWNER, f"m:frv:{gid}")
    body = q.edits[-1]
    assert "민지 &lt;b&gt;사장&lt;/b&gt;" in body and "&lt;script&gt;" in body and "<script>" not in body, body
    assert "대표님들 소통방" in body and f"<code>{A.id}</code>" in body
    labels = [b.text for row in q.kb.inline_keyboard for b in row]
    assert {"🛠 진행 중", "✅ 완료 → 알림", "🙅 안 함", "🗑 삭제"} <= set(labels), labels
    q = await press(r, OWNER, f"m:frs:{gid}:doing")
    assert "진행 중" in q.edits[-1] and (await featreq.get_group(r.db, gid))["status"] == "doing"
    q = await press(r, OWNER, f"m:frs:{gid}:doing")                      # 두 번째 = 이미 처리
    assert not q.edits and "이미 처리" in q.answers[0][0]
    # 진행 중이면 새 요청이 묶일 때 AI 에게 '살펴보는 중' 으로
    res, _ = await say(r, C, ASK, {"summary": "입장할 때 규칙 퀴즈"})
    assert "살펴보는 중" in res, res
    # 권한 재확인: 오너에서 빠지면 바로 막힘
    r.svc.perms.owner_ids = set()
    q = await press(r, OWNER, f"m:frs:{gid}:wont")
    assert not q.edits and (await featreq.get_group(r.db, gid))["status"] == "doing"


@test
async def owner_check_inside_handlers_too():
    """라우터(OWNER)와 별개로 화면 함수·메모 입력도 스스로 오너를 확인 (라우트 등록이 실수로 바뀌어도 막힘)."""
    r = await room()
    await say(r, A, ASK, QUIZ)
    [g] = await groups(r)
    gid = str(g["id"])
    for fn, args in ((fr_panel.s_list, []), (fr_panel.s_detail, [gid]), (fr_panel.r_status, [gid, "wont"]),
                     (fr_panel.r_done_ask, [gid]), (fr_panel.r_done_now, [gid]), (fr_panel.r_delete, [gid])):
        s = await fn(PanelCtx(r.svc, r.bot, BOSS.id, None, args))
        assert s.text is None and "오너" in s.toast, fn.__name__
    ok, text = await fr_panel.i_note(PanelCtx(r.svc, r.bot, BOSS.id, 0, [gid]), FakeMsg(BOSS.id, BOSS, "메모"))
    assert "오너만" in text and (await featreq.get_group(r.db, g["id"]))["status"] == "new" and BOSS.id not in r.svc.inputs
    assert all(menu.ROUTES[c].need == menu.OWNER and not menu.ROUTES[c].scoped
               for c in ("fr", "frn", "frv", "frs", "frd", "frdn", "frx", "frxy"))
    assert menu.INPUT_OPTS["frn"][1] == menu.OWNER


# ── 7. ✅ 완료 + 메모(글자 입력) → 요청자마다 1:1 한 통 · 막힌 사람은 방에 한 번 · 다시 눌러도 안 감 ─
@test
async def complete_with_note_notifies_each_requester_once():
    r = await room()
    await say(r, A, ASK, QUIZ)
    await say(r, A, ASK, {"summary": "입장 때 규칙 퀴즈"})                       # 같은 사람 한 번 더 (1통만)
    await say(r, B, ASK, {"summary": "입장할 때 규칙 퀴즈"})
    await say(r, C, ASK, {"summary": "입장할 때 규칙 퀴즈 내기"})
    await featreq.submit(r.db, 555, "외부", 555, "입장할 때 규칙 퀴즈")          # 1:1 에서 요청한 사람
    await r.db._write("UPDATE featreq_items SET last_ts=last_ts-90000 WHERE user_id=?", (A.id,))
    await featreq.submit(r.db, A.id, "민지", Room.CHAT, "입장할 때 규칙 퀴즈")    # 하루 지나 또 → A 는 두 줄 (알림은 1통)
    [g] = await groups(r)
    gid = g["id"]
    r.bot.dm_blocked = {B.id, C.id, 555}                                       # 1:1 막힘
    q = await press(r, OWNER, f"m:frd:{gid}")
    assert "메모" in q.edits[-1] and r.svc.inputs[OWNER.id].kind == "frn"
    assert (await featreq.get_group(r.db, gid))["status"] == "new", "메모 받기 전엔 그대로"
    before = len(r.bot.named("send_message"))
    msg = await dm(r, OWNER, "⚙️ 설정 → 🧩 에서 <켜요> & 끝")
    assert "1명에게 1:1" in msg.replies[0] and "방 1곳" in msg.replies[0], msg.replies
    g = await featreq.get_group(r.db, gid)
    assert g["status"] == "done" and g["note"] == "⚙️ 설정 → 🧩 에서 <켜요> & 끝"
    new = r.bot.named("send_message")[before:]
    to_a = [c for c in new if c[1] == A.id]
    assert len(to_a) == 1 and "요청하신 '<b>입장할 때 규칙 퀴즈</b>' 기능이 추가됐어요" in to_a[0][2]
    assert "&lt;켜요&gt; &amp; 끝" in to_a[0][2], to_a[0][2]
    room_notes = [c for c in new if c[1] == Room.CHAT]
    assert len(room_notes) == 1, "막힌 사람들은 방에 한 줄로 같이"
    assert f"tg://user?id={B.id}" in room_notes[0][2] and f"tg://user?id={C.id}" in room_notes[0][2]
    assert "준호&amp;" in room_notes[0][2]
    assert not [c for c in new if c[1] == 555], "1:1 에서 요청하고 막힌 사람은 보낼 곳 없음"
    # 다시 눌러도·메모 없이 완료를 눌러도 한 번 더 안 감
    before = len(r.bot.named("send_message"))
    q = await press(r, OWNER, f"m:frdn:{gid}")
    assert not q.edits and "이미 처리" in q.answers[0][0]
    q = await press(r, OWNER, f"m:frd:{gid}")
    assert not q.edits and OWNER.id not in r.svc.inputs
    assert await featreq.notify_done(r.db, r.bot, gid) == (0, 0), "알림은 항목마다 한 번만"
    assert {it["notified"] for it in await featreq.items(r.db, gid)} == {"dm", "room", "skip"}
    assert len(r.bot.named("send_message")) == before
    # 끝난 묶음엔 새 요청이 안 붙음 → 새 묶음
    await featreq.submit(r.db, B.id, "준호", Room.CHAT, "입장할 때 규칙 퀴즈", now=int(time.time()) + 90000)
    assert len(await groups(r)) == 2


@test
async def complete_without_note_and_permission_recheck_on_input():
    r = await room()
    await say(r, A, ASK, QUIZ)
    [g] = await groups(r)
    gid = g["id"]
    await press(r, OWNER, f"m:frd:{gid}")
    r.svc.perms.owner_ids = set()                                     # 입력 기다리는 사이 오너에서 빠짐
    msg = await dm(r, OWNER, "메모")
    assert (await featreq.get_group(r.db, gid))["status"] == "new" and not sent_to(r, A.id), msg.replies
    r.svc.perms.owner_ids = {OWNER.id}
    q = await press(r, OWNER, f"m:frdn:{gid}")
    assert "1명에게" in q.edits[-1] and (await featreq.get_group(r.db, gid))["status"] == "done"
    [dm_a] = sent_to(r, A.id)
    assert "기능이 추가됐어요" in dm_a[2] and "📝" not in dm_a[2]
    assert "✅ 완료 → 알림" not in [b.text for row in q.kb.inline_keyboard for b in row]


@test
async def concurrent_notify_sends_once():
    """완료 알림이 (어떤 이유로든) 동시에 두 번 돌아도 요청자마다 한 통 (items.notified 를 조건 UPDATE 로 차지)."""
    r = await room()
    for u in (A, B, C):
        await featreq.submit(r.db, u.id, u.first_name, Room.CHAT, "출석 체크")
    [g] = await groups(r)
    assert await featreq.set_status(r.db, g["id"], "done", OWNER.id)
    res = await asyncio.gather(featreq.notify_done(r.db, r.bot, g["id"]), featreq.notify_done(r.db, r.bot, g["id"]))
    assert sum(s for s, _ in res) == 3 and len(r.bot.named("send_message")) == 3, res


# ── 8. 🙅 안 함 · 🗑 삭제(확인 → 한 번만) · 목록 쪽·정렬 ───────
@test
async def wont_delete_and_paging():
    r = await room()
    users = [fake_user(3000 + i, f"사람{i}") for i in range(9)]
    topics = ["출석 체크", "투표 만들기", "환율 알림", "생일 축하", "음성 메시지 요약", "공지 번역", "유튜브 링크 요약",
              "끝말잇기 랭킹", "날씨 알림"]
    for u, t in zip(users, topics):
        await featreq.submit(r.db, u.id, u.first_name, Room.CHAT, t)
    await featreq.submit(r.db, A.id, "민지", Room.CHAT, "날씨 알림")
    q = await press(r, OWNER, "m:fr")
    assert "열린 요청 9개" in q.edits[-1] and "1. 🆕 <b>날씨 알림</b> · 👍 2명" in q.edits[-1], q.edits[-1]
    datas = [b.callback_data for row in q.kb.inline_keyboard for b in row]
    assert "m:fr:v:o:1" in datas and "m:fr:n:o:0" in datas
    q = await press(r, OWNER, "m:fr:v:o:1")
    assert "7." in q.edits[-1] and "1." not in q.edits[-1].split("\n")[1]
    [wid] = [x["id"] for x in await groups(r) if x["summary"] == "공지 번역"]
    q = await press(r, OWNER, f"m:frs:{wid}:wont")
    assert "안 함" in q.edits[-1] and (await featreq.get_group(r.db, wid))["status"] == "wont"
    assert not await featreq.set_status(r.db, wid, "done", OWNER.id), "안 함 → 완료로 못 바뀜"
    q = await press(r, OWNER, "m:fr:v:w:0")
    assert "공지 번역" in q.edits[-1]
    q = await press(r, OWNER, f"m:frx:{wid}")
    assert "지울까요" in q.edits[-1] and await featreq.get_group(r.db, wid)
    q = await press(r, OWNER, f"m:frxy:{wid}")
    assert q.answers[0][0] == "지웠어요" and not await featreq.get_group(r.db, wid) and not await featreq.items(r.db, wid)
    q = await press(r, OWNER, f"m:frxy:{wid}")
    assert not q.edits and "이미" in q.answers[0][0]
    q = await press(r, OWNER, f"m:frv:{wid}")
    assert "지워졌어요" in q.edits[-1]
    for bad in ("m:frv:abc", "m:frv:-5", "m:frs:1:done", "m:frd:0"):
        q = await press(r, OWNER, bad)
        assert len(q.answers) == 1 and not q.edits, bad


# ── 9. 방 관리자 허브: 우리 방 요청만, 읽기 전용 ──────────────
@test
async def room_admin_hub_read_only():
    r = await room()
    other = -100888
    await r.db.ensure_chat(other, "다른 방")
    await say(r, A, ASK, QUIZ)
    await featreq.submit(r.db, B.id, "준호", other, "다른 방 요청 <x>")
    [gid] = [x["id"] for x in await groups(r) if "퀴즈" in x["summary"]]
    await featreq.set_status(r.db, gid, "doing", OWNER.id)
    q = await press(r, BOSS, f"m:g:{Room.CHAT}")
    assert f"m:frr:{Room.CHAT}" in [b.callback_data for row in q.kb.inline_keyboard for b in row]
    q = await press(r, BOSS, f"m:frr:{Room.CHAT}")
    body = q.edits[-1]
    assert "입장할 때 규칙 퀴즈" in body and "🛠 진행 중" in body and "다른 방 요청" not in body, body
    assert "민지" not in body, "요청자 이름은 오너 화면에만"
    datas = [b.callback_data for row in q.kb.inline_keyboard for b in row]
    assert not [d for d in datas if d.startswith(("m:frs", "m:frd", "m:frx"))], "읽기 전용"
    q = await press(r, A, f"m:frr:{Room.CHAT}")
    assert not q.edits and q.answers[0][1], "멤버는 못 봄"


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
