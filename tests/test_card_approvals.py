"""🗳 AI 확인 카드: 누른 결과가 AI 맥락에 · 카드당 한 번 · '오늘은 확인 생략' (낮은 위험만, 제재는 절대 없음).
python tests/run_all.py card_approvals"""
import asyncio
import time

from fake_llm import Room, fast_timers, reply, restore_timers, tool_call
from fakes import runner
from test_botlink import DICE, blroom, press_room, trust
from test_sanction_multi import A, B, BOSS, ask, press, room

from sodam import cards, menu, opsdesk
from sodam.permissions import Role

test, run_all = runner()


def cards_sent(r, word):
    return [c for c in r.bot.named("send_message") if word in c[2]]


def rows(card):
    return card[3]["reply_markup"].inline_keyboard


async def schedule(r, who=BOSS, text="회의", role=Role.ADMIN):
    return (await ask(r, who, [tool_call("schedule_task", {"when": "매일 09:00", "action": "remind", "text": text})],
                      role=role))[0]


async def log_lines(r, chat=Room.CHAT):
    return [x["text"] for x in await r.db._all("SELECT text FROM ai_card_log WHERE chat_id=? ORDER BY id", (chat,))]


@test
async def sanction_press_is_recorded_and_grounds_next_answer():
    old = fast_timers()
    try:
        r = await room()
        await ask(r, BOSS, [tool_call("mute_member", {"names": ["캎이바라요"], "minutes": 10, "reason": "도배"})])
        card = cards_sent(r, "할까요")[-1]
        assert not any("확인 생략" in b.text for row in rows(card) for b in row)        # 제재 카드엔 절대 없음
        await press(r, BOSS, next(iter(r.svc.pending)), "y")
        [line] = await log_lines(r)
        assert line.startswith("✅ 뮤트 10분 실행됨") and "캎이바라요" in line and "처리: 방장" in line, line
        await ask(r, BOSS, [tool_call("warn_member", {"names": ["조이킨"], "reason": "x"})])
        await press(r, BOSS, next(iter(r.svc.pending)), "n")
        assert (await log_lines(r))[-1].startswith("❌ 경고 취소 — 조이킨")
        r.llm.script = [reply("아까 10분으로 걸린 걸로 나와요.")]
        await r.say(BOSS, "소담아 아까 뮤트 됐어?")
        user = r.llm.of("chat")[-1]["messages"][-1]["content"]
        block = user.split("<card_results", 1)[1].split("</card_results", 1)[0]
        assert "뮤트 10분 실행됨" in block and "경고 취소" in block and 'id="' in block, block   # nonce 태그 안 데이터
        assert user.index("<card_results") < user.index("<chat_log")
        system = r.llm.of("chat")[-1]["messages"][0]["content"]
        assert "<card_results>" in system and "취소된 일은" in system
    finally:
        restore_timers(old)


@test
async def owner_dm_sanction_outcome_goes_to_owner_dm():
    from test_sanction_multi import OWNER
    r = await room()
    r.svc.perms.owner_ids = {OWNER.id}
    res = await ask(r, OWNER, [tool_call("owner_sanction", {"room": str(Room.CHAT), "action": "ban", "names": ["조이킨"],
                                                           "reason": "사기"})], chat_id=OWNER.id, role=Role.OWNER)
    assert "확인 버튼을 보냈음" in res[0], res
    await press(r, OWNER, next(iter(r.svc.pending)), "n")
    assert (await log_lines(r, OWNER.id))[-1].startswith("❌ 내보내기 취소 — 조이킨") and not await log_lines(r)


@test
async def schedule_card_day_button_then_skips_same_tool_same_requester():
    r = await room()
    res = await schedule(r)
    assert "확인 버튼" in res
    card = cards_sent(r, "예약할까요")[-1]
    (ok, no), (day,) = rows(card)
    assert day.text == cards.DAY_LABEL and all(len(b.callback_data.encode()) <= 64 for b in (ok, no, day))
    q = await press_room(r, A, day.callback_data)                       # 요청한 사람만
    assert "요청한 사람만" in q.answers[-1][0] and not await r.db.schedules(Room.CHAT)
    q = await press_room(r, BOSS, day.callback_data)
    assert len(q.answers) == 1 and "예약했어요" in q.edits[-1] and "확인 없이" in q.edits[-1]
    assert len(await r.db.schedules(Room.CHAT)) == 1 and await cards.approved(r.svc, Room.CHAT, BOSS.id, "schedule_task")
    q = await press_room(r, BOSS, ok.callback_data)                     # 같은 카드의 다른 버튼 = 이미 처리
    assert len(q.answers) == 1 and len(await r.db.schedules(Room.CHAT)) == 1
    assert "예약 저장됨" in (await log_lines(r))[-1]
    before = len(cards_sent(r, "예약할까요"))
    res = await schedule(r, text="점심")                                  # 오늘은 카드 없이 바로
    assert "확인 생략" in res and "예약했어요" in res and "확인 버튼" not in res, res
    assert len(cards_sent(r, "예약할까요")) == before and len(await r.db.schedules(Room.CHAT)) == 2
    assert (await log_lines(r))[-1].endswith("· 확인 생략 (방장)")
    audit = [x["action"] for x in await r.db._all("SELECT action FROM mod_log WHERE action LIKE 'approval%'")]
    assert audit == ["approval_day", "approval_skip"], audit
    res = (await ask(r, BOSS, [tool_call("alert_rule", {"trigger": "keyword", "value": "입금"})]))[0]
    assert "확인 버튼" in res                                              # 다른 도구는 여전히 카드
    r.svc.perms.admins.add(B.id)
    assert "확인 버튼" in await schedule(r, who=B)                        # 다른 관리자도 카드


@test
async def approval_ends_at_midnight_and_rechecks_admin():
    r = await room()
    assert await cards.approve_day(r.svc, Room.CHAT, BOSS.id, "schedule_task")
    row = await r.db._one("SELECT until FROM ai_approvals")
    assert 0 < row["until"] - time.time() <= 86400 and time.localtime(row["until"]).tm_min == 0
    await r.db._write("UPDATE ai_approvals SET until=?", (int(time.time()) - 1,))
    assert "확인 버튼" in await schedule(r)                                # 지난 승인 = 다시 카드
    await cards.approve_day(r.svc, Room.CHAT, BOSS.id, "schedule_task")
    r.svc.perms.admins.discard(BOSS.id)                                 # 그 사이 관리자에서 내려옴
    n = len(await r.db.schedules(Room.CHAT))
    assert "확인 버튼" in await schedule(r) and len(await r.db.schedules(Room.CHAT)) == n   # 바로 실행 안 함


@test
async def sanctions_can_never_be_skipped():
    r = await room()
    for tool in ("mute_member", "warn_member", "ban_member", "owner_sanction", "mute", "ban", "warn"):
        assert not await cards.approve_day(r.svc, Room.CHAT, BOSS.id, tool)
        await r.db._write("INSERT OR REPLACE INTO ai_approvals(chat_id, user_id, tool, until) VALUES(?,?,?,?)",
                          (Room.CHAT, BOSS.id, tool, int(time.time()) + 3600))   # 억지로 넣어도
        assert not await cards.approved(r.svc, Room.CHAT, BOSS.id, tool)
    assert not set(cards.LOW_RISK) & cards.NEVER
    res = await ask(r, BOSS, [tool_call("mute_member", {"names": ["캎이바라요"], "minutes": 10})])
    assert "확인 버튼을 보냈음" in res[0] and r.svc.pending


@test
async def card_buttons_race_only_one_wins_and_inbox_clears():
    r = await room()
    await schedule(r)
    (ok, no), (day,) = rows(cards_sent(r, "예약할까요")[-1])
    items = await opsdesk._record_items(r.svc, Room.CHAT, BOSS.id, int(time.time()))
    assert len([i for i in items if i.kind == "card"]) == 1
    qs = await asyncio.gather(*(press_room(r, BOSS, b.callback_data) for b in (ok, no, day)))
    assert all(len(q.answers) == 1 for q in qs)
    assert len([q for q in qs if q.edits]) == 1, [q.edits for q in qs]    # 한 버튼만 처리
    assert len(await r.db.schedules(Room.CHAT)) <= 1 and len(await log_lines(r)) == 1
    items = await opsdesk._record_items(r.svc, Room.CHAT, BOSS.id, int(time.time()))
    assert not [i for i in items if i.kind == "card"]                    # 남은 토큰도 지워 '안 누른 카드' 아님


@test
async def pressing_one_button_retires_the_others():
    """[✅ + 오늘은 확인 생략] 만 눌러도 ✅·❌ 토큰까지 지움 → 📥 에 '안 누른 카드'로 안 남고, 옛 버튼은 만료."""
    r = await room()
    await schedule(r)
    (ok, no), (day,) = rows(cards_sent(r, "예약할까요")[-1])
    await press_room(r, BOSS, day.callback_data)
    items = await opsdesk._record_items(r.svc, Room.CHAT, BOSS.id, int(time.time()))
    assert not [i for i in items if i.kind == "card"]
    assert not await r.db._all("SELECT tok FROM menu_tokens WHERE action IN ('cron_save', 'cron_no')")
    q = await press_room(r, BOSS, no.callback_data)
    assert "만료" in q.answers[-1][0] and (await log_lines(r))[-1].startswith("✅")


@test
async def cancel_is_recorded_and_restart_keeps_card_and_approval():
    r = await room()
    await schedule(r)
    (ok, no), _ = rows(cards_sent(r, "예약할까요")[-1])
    await press_room(r, BOSS, no.callback_data)
    assert (await log_lines(r))[-1] == "❌ 예약 취소 (저장 안 함) (방장)"
    assert not await r.db.schedules(Room.CHAT)
    await schedule(r)
    _, (day,) = rows(cards_sent(r, "예약할까요")[-1])
    r.svc.menu_tokens.clear()                                            # 재시작
    q = await press_room(r, BOSS, day.callback_data)
    assert "예약했어요" in q.edits[-1]
    r.svc.menu_tokens.clear()
    assert "확인 생략" in await schedule(r, text="저녁")                   # 승인도 DB


@test
async def room_rule_card_has_no_skip_and_records_outcome():
    r = await room()
    await ask(r, BOSS, [tool_call("save_room_rule", {"text": "광고는 관리자에게 먼저 말하고 올려야 해요", "title": "광고"})])
    card = cards_sent(r, "저장할까요")[-1]
    assert len(rows(card)) == 1                                           # 방 자료는 확인 생략 없음
    await press_room(r, BOSS, rows(card)[0][0].callback_data)
    assert (await log_lines(r))[-1].startswith("✅ 방 자료 #")


@test
async def alert_rule_day_button_then_skip():
    r = await room()
    await ask(r, BOSS, [tool_call("alert_rule", {"trigger": "keyword", "value": "입금"})])
    _, (day,) = rows(cards_sent(r, "알림 규칙을 만들까요")[-1])
    await press_room(r, BOSS, day.callback_data)
    assert (await log_lines(r))[-1].startswith("✅ 알림 규칙 #")
    res = (await ask(r, BOSS, [tool_call("alert_rule", {"trigger": "keyword", "value": "환불"})]))[0]
    assert "확인 생략" in res and len(await r.db._all("SELECT id FROM alert_rules")) == 2, res


@test
async def bot_command_day_button_then_sends_directly():
    old = fast_timers()
    try:
        r = await blroom("interact")
        await trust(r)
        await ask(r, BOSS, [tool_call("bot_command", {"bot": "dice_bot", "command": "/dice"})])
        _, (day,) = rows(cards_sent(r, "보낼까요")[-1])
        q = await press_room(r, BOSS, day.callback_data)
        assert "보냈어요" in q.edits[-1] and (await log_lines(r))[-1].startswith("✅ @dice_bot 에 /dice@dice_bot 보냄")
        res = await ask(r, BOSS, [tool_call("bot_command", {"bot": "dice_bot", "command": "/roll"})])   # 처음 쓰는 명령도
        assert "확인 버튼" not in res[0] and len(cards_sent(r, "보낼까요")) == 1, res
        assert [c[2] for c in r.bot.named("send_message") if c[2].startswith("/")] == ["/dice@dice_bot", "/roll@dice_bot"]
        assert (await log_lines(r))[-1].endswith("· 확인 생략 (방장)")
    finally:
        restore_timers(old)


_ = (DICE, menu)
