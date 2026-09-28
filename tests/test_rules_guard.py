"""🔔 알림 규칙 폭주 방지 · 지난 7일 미리 보기 (sodam/rules.py): python tests/run_all.py rules_guard

본코드 경로: 멤버 말(handlers.on_group_message → 훅 → rules.on_message → fire) · 버튼(menu.on_callback, 1:1) ·
AI 도구 alert_rule 확인 카드.
"""
import time
from datetime import datetime

from fake_llm import Room, tool_call
from fakes import FakeQuery, fake_user, runner
from test_sanction_multi import A, B, BOSS, ask, room

from sodam import menu, rules

test, run_all = runner()
ADM2 = fake_user(3, "부방장")


async def press(r, user, data):
    r.svc.menu_limiter._hits.clear()
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers
    return q


def dms(r, uid=BOSS.id):
    return [c for c in r.bot.named("send_message") if c[1] == uid]


def buttons(call):
    kb = call[3]["reply_markup"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def pauses(r):
    return await r.db._all("SELECT * FROM alert_rule_pauses ORDER BY id")


async def runaway_world():
    r = await room()
    r.svc.perms.admins.add(ADM2.id)
    rid = await rules.add(r.svc, Room.CHAT, BOSS.id, {"trig": "keyword", "arg": "입금", "action": "dm"})
    return r, rid


# ── 폭주 → 1시간 멈춤 + 만든 사람 1:1 한 번 ───────────────
@test
async def runaway_rule_pauses_once_and_dms_creator():
    r, rid = await runaway_world()
    for i in range(rules.RUNAWAY_MIN):
        await r.say(A if i % 2 else B, f"입금 {i}")
    assert not await pauses(r), "10번까지는 폭주 아님"
    assert len(dms(r)) == 1, "쿨다운 10분 → 알림은 1번"
    await r.say(A, "입금 11")
    [p] = await pauses(r)
    assert p["hits"] == 11 and p["until"] >= time.time() + rules.PAUSE_SECONDS - 5 and p["status"] is None
    [_, warn] = dms(r)
    assert "⚠️ 규칙 '" in warn[2] and "10분에 11번 → 1시간 멈춤" in warn[2], warn[2]
    data = buttons(warn)
    assert [d.split(":")[1] for d in data] == ["rlp", "rlp", "rli"] and all(len(d.encode()) <= 64 for d in data)
    texts = [b.text for row in warn[3]["reply_markup"].inline_keyboard for b in row]
    assert texts == ["▶️ 계속 실행", "⏸ 오늘 중지", "✏️ 규칙 보기"], texts
    for i in range(5):                                  # 멈춘 동안: 더 안 알리고 멈춤도 한 번
        await r.say(B, f"입금 또 {i}")
    assert len(dms(r)) == 2 and len(await pauses(r)) == 1
    await r.db._write("UPDATE alert_rules SET last_fired=0 WHERE id=?", (rid,))   # 쿨다운이 지나도
    await r.say(B, "입금 쿨다운 지남")
    assert len(dms(r)) == 2, "멈춤 중엔 안 울림 (_claim)"
    q = await press(r, BOSS, data[2])                   # ✏️ 규칙 보기 = 항목 화면 (멈춤 표시 + 버튼)
    assert "너무 자주 걸려서 멈춤" in q.edits[-1] and "10분에 11번" in q.edits[-1]
    assert f"m:rlp:{Room.CHAT}:{p['id']}:r" in [b.callback_data for row in q.kb.inline_keyboard for b in row]


@test
async def pause_buttons_creator_only_fresh_admin_once():
    r, rid = await runaway_world()
    for i in range(rules.RUNAWAY_MIN + 1):
        await r.say(A, f"입금 {i}")
    resume, today, _ = buttons(dms(r)[-1])
    q = await press(r, ADM2, resume)                    # 다른 관리자
    assert q.answers[-1] == ("규칙을 만든 분만 누를 수 있어요.", True) and (await pauses(r))[0]["status"] is None
    q = await press(r, A, resume)                       # 관리자 아님
    assert q.answers[-1][1] is True and (await pauses(r))[0]["status"] is None
    r.svc.perms.admins.discard(BOSS.id)                 # 만든 사람이 관리자에서 빠짐 → 누를 때 다시 확인
    q = await press(r, BOSS, resume)
    assert q.answers[-1][1] is True and (await pauses(r))[0]["status"] is None
    r.svc.perms.admins.add(BOSS.id)
    q = await press(r, BOSS, resume)
    [p] = await pauses(r)
    assert p["status"] == "resume" and p["until"] == 0 and p["by_id"] == BOSS.id and "다시 울려요" in q.answers[-1][0]
    q = await press(r, BOSS, today)                     # 두 번째 누름 = 이미 처리
    assert "이미 처리" in q.answers[-1][0] and (await pauses(r))[0]["status"] == "resume"
    await r.db._write("UPDATE alert_rules SET last_fired=0 WHERE id=?", (rid,))
    await r.say(B, "입금 다시")
    assert "입금 다시" in dms(r)[-1][2], "계속 실행 → 다시 울림"
    for i in range(rules.RUNAWAY_MIN + 5):              # 계속 실행 뒤 1시간은 다시 안 멈춤
        await r.say(A, f"입금 더 {i}")
    assert len(await pauses(r)) == 1
    assert [x for x in await r.db._all("SELECT detail FROM mod_log WHERE action='setting'") if "계속 실행" in x["detail"]]


@test
async def today_stop_holds_until_midnight():
    r, rid = await runaway_world()
    for i in range(rules.RUNAWAY_MIN + 1):
        await r.say(A, f"입금 {i}")
    _, today, _ = buttons(dms(r)[-1])
    await press(r, BOSS, today)
    [p] = await pauses(r)
    now = int(time.time())
    assert p["status"] == "today" and p["until"] == rules.midnight(r.svc, now) > now
    assert datetime.fromtimestamp(p["until"], r.svc.cfg.tz).strftime("%H:%M") == "00:00"
    n = len(dms(r))
    await r.db._write("UPDATE alert_rules SET last_fired=0 WHERE id=?", (rid,))
    await r.say(B, "입금 오늘")
    assert len(dms(r)) == n, "오늘은 안 울림"
    await r.db._write("UPDATE alert_rule_pauses SET until=? WHERE id=?", (now - 1, p["id"]))   # 자정 지남
    await r.db._write("UPDATE alert_rule_hits SET minute=minute-600 WHERE rule_id=?", (rid,))  # 그 몰림은 옛일
    await r.say(B, "입금 내일")
    assert "입금 내일" in dms(r)[-1][2]


@test
async def busy_rule_threshold_scales_with_its_own_average():
    r, rid = await runaway_world()
    minute = int(time.time()) // 60
    # 지난 7일 시간당 평균 4번 → 기준 max(10, 20) = 20
    await r.db.atomic(lambda c: c.executemany(
        "INSERT INTO alert_rule_hits(rule_id, minute, n) VALUES(?, ?, 4)",
        [(rid, minute - 60 * h - 30) for h in range(1, 7 * 24)] + [(rid, minute - 30)]))
    for i in range(20):
        await r.say(A, f"입금 {i}")
    assert not await pauses(r), "평소에도 자주 걸리는 규칙은 20번까지 괜찮음"
    await r.say(A, "입금 21")
    assert len(await pauses(r)) == 1 and (await pauses(r))[0]["hits"] == 21


# ── 지난 7일 미리 보기 ────────────────────────────────────
async def log(r, uid, text, ts, is_bot=False):
    await r.db.log_message(Room.CHAT, uid, None, text, is_bot=is_bot, ts=int(ts))


@test
async def replay_keyword_cooldown_creator_and_daily_cap():
    r = await room()
    now = int(time.time())
    base = now - 3 * 86400
    for dt in (0, 60, 120, 700, 1300):                  # 쿨다운 10분 → 0·700·1300 = 3번
        await log(r, A.id, "입 금 됐나요", base + dt)
    await log(r, BOSS.id, "입금 확인", base + 2000)       # 만든 사람 말은 안 셈
    await log(r, B.id, "그냥 대화", base + 2100)
    await log(r, A.id, "입금", now - 8 * 86400)          # 7일보다 전
    spec = {"trig": "keyword", "arg": "입금", "action": "dm", "cooldown": 10, "created_by": BOSS.id}
    rp = await rules.replay(r.svc, Room.CHAT, spec, now=now)
    assert (rp.total, rp.max_day) == (3, 3), rp
    noon = datetime.fromtimestamp(now - 2 * 86400, r.svc.cfg.tz).replace(hour=12, minute=0, second=0)
    busy = int(noon.timestamp())
    for i in range(40):                                  # 그날 낮 2분 간격 40번, 쿨다운 1분 → 하루 상한 30
        await log(r, B.id, "입금 입금", busy + i * 120)
    rp = await rules.replay(r.svc, Room.CHAT, {**spec, "cooldown": 1}, now=now)
    assert rp.per_day[noon.strftime("%Y-%m-%d")] == rules.DAILY_CAP and rp.max_day == rules.DAILY_CAP, rp
    assert rp.total == rules.DAILY_CAP + 5, rp            # 3일 전 5번(쿨다운 1분 → 전부) + 30
    text = await rules.preview_text(r.svc, Room.CHAT, {**spec, "cooldown": 1})
    assert f"지난 7일이면 {rp.total}번 · 하루 최대 {rp.max_day}번" in text and "⚠️ 하루 평균" not in text, text


@test
async def replay_newbie_filter_join_user_and_quiet():
    r = await room()
    now = int(time.time())
    j = now - 4 * 86400
    await r.db._write("UPDATE members SET joined_at=? WHERE chat_id=? AND user_id=?", (j, Room.CHAT, A.id))
    await log(r, A.id, "코인 사요", j + 3600)             # 들어온 지 1시간 → 신규
    await log(r, A.id, "코인 또", j + 2 * 86400)          # 2일 뒤 → 신규 아님
    await r.db._write("UPDATE members SET joined_at=NULL WHERE chat_id=? AND user_id=?", (Room.CHAT, B.id))
    await log(r, B.id, "조이킨(21) 님이 방에 들어옴", now - 86400, is_bot=True)   # 캡차 통과 뒤 입장 기록
    await log(r, B.id, "코인 문의", now - 86400 + 600)
    await log(r, B.id, "코인 문의 2", now - 3600)          # 입장 23시간 뒤라 아직 신규 (쿨다운 지남)
    spec = {"trig": "keyword", "arg": "코인", "who": "newbie", "cooldown": 10, "created_by": BOSS.id}
    assert (await rules.replay(r.svc, Room.CHAT, spec, now=now)).total == 3
    assert (await rules.replay(r.svc, Room.CHAT, {**spec, "who": "all"}, now=now)).total == 4
    rp = await rules.replay(r.svc, Room.CHAT, {"trig": "user", "arg": str(B.id), "cooldown": 10}, now=now)
    assert rp.total == 2, rp
    joins = await rules.replay(r.svc, Room.CHAT, {"trig": "join", "cooldown": 1}, now=now)
    assert joins.total == 3, joins                       # A(members.joined_at) · B(입장 기록만) · BOSS(방금)
    # 조용함: 마지막 말 뒤 3시간 = 한 번씩 (A 코인 사요 → 2일 뒤 코인 또 사이 1번, …)
    await r.db._write("DELETE FROM messages WHERE chat_id=?", (Room.CHAT,))
    for ts in (now - 10 * 3600, now - 9 * 3600, now - 5 * 3600, now - 1800):
        await log(r, A.id, "말", ts)
    rp = await rules.replay(r.svc, Room.CHAT, {"trig": "quiet", "arg": "3", "cooldown": 10}, now=now)
    assert rp.total == 2, rp                             # 9→5시간 전 (4시간)·5시간→30분 전 (4.5시간) 한 번씩


@test
async def ai_card_and_panel_show_preview_and_warn_when_busy():
    r = await room()
    now = int(time.time())
    for i in range(7 * 12):                              # 하루 12번쯤 → 경고
        await r.db.log_message(Room.CHAT, A.id, None, "입금 됐어요", ts=now - i * 7200 - 60)
    await ask(r, BOSS, [tool_call("alert_rule", {"trigger": "keyword", "value": "입금", "cooldown_min": 10})])
    [card] = [c for c in r.bot.named("send_message") if "규칙을 만들까요" in c[2]]
    assert "🔎 지난 7일이면" in card[2] and "하루 최대 12번" in card[2] and "⚠️ 하루 평균 12번" in card[2], card[2]
    rid = await rules.add(r.svc, Room.CHAT, BOSS.id, {"trig": "keyword", "arg": "입금", "action": "dm"})
    q = await press(r, BOSS, f"m:rli:{Room.CHAT}:{rid}")
    data = [b.callback_data for row in q.kb.inline_keyboard for b in row]
    assert f"m:rlv:{Room.CHAT}:{rid}" in data and all(len(d.encode()) <= 64 for d in data)
    q = await press(r, BOSS, f"m:rlv:{Room.CHAT}:{rid}")
    assert "미리 보기" in q.edits[-1] and "지난 7일이면" in q.edits[-1] and "⚠️" in q.edits[-1], q.edits[-1]
    q = await press(r, A, f"m:rlv:{Room.CHAT}:{rid}")    # 멤버는 못 봄
    assert q.answers[-1][1] is True and not q.edits
    await press(r, BOSS, f"m:rld:{Room.CHAT}:{rid}:1")   # 지우면 걸린 수·멈춤 기록도
    assert not await r.db._all("SELECT 1 FROM alert_rule_hits WHERE rule_id=?", (rid,))


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
