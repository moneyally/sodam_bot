"""🧭 이상징후 자동 감지: python tests/run_all.py anomaly

본코드 경로(handlers.handle_new_member → hooks → anomaly.on_member_join, handlers.on_group_message → 관리 검사 →
hooks → anomaly.on_message)와 버튼(menu.on_callback → m:anm / m:anmx / m:anmu)을 가짜 텔레그램으로 돈다.
"""
import asyncio
import logging
import time

import harness
from fake_llm import Room
from fakes import FakeQuery, fake_user, runner

from sodam import anomaly, free, handlers, menu, raid
from sodam.panels import log as log_panel

test, run_all = runner()
ADMIN, WEAK, THIRD = 5, 6, 7          # 관리자(차단 권한) · 관리자(차단 권한 없음) · 관리자(차단 권한, 1:1 막힘 시험용)
OLD, NEW = 100_000_000, 9_000_000_000  # 2015년 계정 · 기준 데이터보다 새 계정
CHAT = Room.CHAT


async def room(admins=(ADMIN, WEAK), **settings):
    for t in list(anomaly._tasks):
        t.cancel()
    anomaly._rooms.clear()
    anomaly._due.clear()
    anomaly.EVAL_GAP, anomaly.FOLLOW_UP = 0, 3600
    r = await Room().open(admins=admins, settings={"captcha_enabled": False, "greet_enabled": False,
                                                   "raid_guard": False, **settings})
    r.bot.admins = [fake_user(a, {ADMIN: "방장", WEAK: "부방장", THIRD: "셋째"}[a]) for a in admins]
    perms = r.svc.perms
    r.rights = {a for a in admins if a != WEAK}

    async def can(bot, cid, uid, right="restrict"):
        return uid in r.rights
    perms.can = can
    return r


async def join(r, uid, name="입장", username=None):
    await handlers.handle_new_member(r.ctx, CHAT, "방", fake_user(uid, name, username))
    await r.settle()


async def joins(r, n, *, base=OLD, name=lambda i: f"사람{chr(0xAC00 + i * 7)}", username=lambda i: None):
    for i in range(n):
        await join(r, base + i, name(i), username(i))


def dms(r, uid=None):
    return [c for c in r.bot.named("send_message") if c[1] > 0 and (uid is None or c[1] == uid)]


def alerts(r):
    return [c for c in dms(r) if "방 이상징후" in c[2]]


def buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


async def press(r, uid, data):
    r.svc.menu_limiter._hits.clear()
    q = FakeQuery(uid, fake_user(uid, "관리자"), data)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, (data, q.answers)   # answer 는 정확히 1번
    return q


async def rows(r):
    return await r.db._all("SELECT * FROM anomaly_alerts WHERE chat_id=? ORDER BY id", (CHAT,))


async def modlog(r, action):
    return await r.db._all("SELECT * FROM mod_log WHERE chat_id=? AND action=?", (CHAT, action))


# ── 정규화·링크 ───────────────────────────────────────────
@test
def normalize_names_and_links():
    assert anomaly.name_key("Crypto 💰 Bot 12") == anomaly.name_key("crypto_bot 99!") == "cryptobot"
    assert anomaly.name_key("코인 1") == anomaly.name_key("코 인🚀7") == "코인"
    assert anomaly.name_key("7 🚀") == "" and anomaly.name_key("A1") == ""
    assert anomaly.username_stem("airdrop_bot_123") == "airdrop_bot" and anomaly.username_stem("ab12") == ""
    assert anomaly.extract_links("오세요 https://www.Evil.XYZ/a?b=1 그리고 evil.xyz/c") == ["evil.xyz"]
    assert anomaly.extract_links("t.me/+AbCd 와 https://t.me/joinchat/Zz9 와 T.me/SomeChan") == \
        ["t.me/+AbCd", "t.me/joinchat/Zz9", "t.me/somechan"]
    assert anomaly.extract_links("메일 me@gmail.com · Node.js · report.pdf") == []
    ent = type("E", (), {"type": "text_link", "url": "https://hidden.top/x"})()
    assert anomaly.extract_links("여기 눌러", [ent]) == ["hidden.top"]
    assert anomaly.defang("t.me/+Ab") == "t[.]me/+Ab" and anomaly.defang("tg://x") == "tg[:]//x"


# ── ① 입장 몰림 ───────────────────────────────────────────
@test
async def join_surge_alone_needs_to_be_big():
    r = await room()
    await joins(r, 20)                                   # 보통: 15명↑ = 35점 → 혼자선 알림 안 함
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert f.score == anomaly.PTS_SURGE and not alerts(r), (f.score, f.reasons)
    await joins(r, 10, base=OLD + 100)                   # 30명(2배) = 50점 → 알림
    [a] = alerts(r)
    assert a[1] == ADMIN and "입장 +30" in a[2] and "입장 몰림: 10분에 30명" in a[2], a[2]
    assert "자동 제재는 하지 않았어요" in a[2]
    data = buttons(a[3]["reply_markup"])
    assert [d.rsplit(":", 1)[1] for d in data] == ["d", "h", "i"] and all(len(d.encode()) <= 64 for d in data), data
    assert not r.bot.named("ban") and not r.bot.named("restrict"), "자동 제재 금지"
    [log] = await modlog(r, "anomaly")
    assert log["actor_id"] is None and "입장 +30" in log["detail"]


@test
async def surge_is_relative_to_room_normal_rate():
    r = await room()
    now = int(time.time())
    uids = range(500_000, 500_000 + 3 * 7 * 24 * 6 // 2)   # 지난 7일 동안 10분에 평균 1.5명씩 들어오던 방
    await r.db.conn.executemany("INSERT OR IGNORE INTO users(user_id, first_name, updated_at) VALUES(?, '옛날', 0)",
                                [(u,) for u in uids])
    await r.db.conn.executemany("INSERT OR IGNORE INTO members(chat_id, user_id, joined_at) VALUES(?, ?, ?)",
                                [(CHAT, u, now - 86400 - i * 300) for i, u in enumerate(uids)])
    await r.db.conn.commit()
    await r.db.set_setting(CHAT, "anomaly_level", "sensitive")   # 8명↑ · 평소의 3배↑
    await joins(r, 4)
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert not f.reasons, f.reasons   # 8명 미만
    await joins(r, 4, base=OLD + 50)  # 8명 = 평소(1.5명)의 3배(4.5명) 넘음
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert any("입장 몰림" in x and "평균 1.5명" in x for x in f.reasons), f.reasons
    anomaly._rooms[CHAT].baseline = (time.time(), 3.0)   # 평소 3명이면 3배 = 9명 > 8명 → 몰림 아님
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert not any("입장 몰림" in x for x in f.reasons), f.reasons


# ── ② 비슷한 이름 ─────────────────────────────────────────
@test
async def similar_names_and_username_prefix():
    r = await room()
    await joins(r, 16, name=lambda i: f"Airdrop 💰 {i}")    # 몰림 35 + 비슷한 이름 30 → 15번째 입장에서 알림
    [a] = alerts(r)
    assert "비슷한 이름 15명" in a[2] and "비슷한 이름: 15명" in a[2], a[2]
    r = await room()
    await joins(r, 16, name=lambda i: f"사람{chr(0xAC00 + i * 13)}", username=lambda i: f"promo_bot_{i}")
    [a] = alerts(r)
    assert "비슷한 @아이디: 15명" in a[2], a[2]
    r = await room()
    await joins(r, 16)   # 이름이 다 다름 → 몰림만 35점
    assert not alerts(r)


# ── ③ 같은 링크 (링크 규칙에 지워진 시도도 셈) ───────────
@test
async def same_link_counts_blocked_attempts_after_joins():
    r = await room()
    await joins(r, 3)
    st = anomaly._rooms[CHAT]
    assert st.pending, "입장이 몰린 방은 다시 평가를 예약해야 함 (지워진 링크는 훅이 안 불림)"
    msgs = []
    for k in range(4):
        for i in range(3):
            msgs.append(await r.say(fake_user(OLD + i, "입장"), f"수익 인증방 https://evil.xyz/r?{k}"))
    assert all(m.deleted for m in msgs), "신규 링크는 링크 규칙이 지움 → 그룹 메시지 훅은 안 불림"
    assert not alerts(r)
    for t in list(anomaly._tasks):
        t.cancel()
    await anomaly._later(r.svc, r.bot, CHAT, st, 0)   # 예약된 다시 평가
    [a] = alerts(r)
    assert "같은 링크 12회" in a[2] and "evil[.]xyz 12회 · 3명" in a[2] and "evil.xyz" not in a[2], a[2]


@test
async def same_link_excludes_admins_free_members_and_whitelist():
    r = await room(admins=(ADMIN, WEAK), link_filter=False, newbie_link_hours=0,
                   whitelist_domains=["youtube.com"])
    await free.add(r.db, CHAT, 60, ADMIN)
    for i in range(8):
        await r.say(fake_user(ADMIN, "방장"), "공지 https://notice.xyz/a")
        await r.say(fake_user(60, "자유"), "https://notice.xyz/b")
        await r.say(fake_user(61 + i % 4, "멤버"), "https://youtube.com/watch?v=1")
    for i in range(12):   # 한 사람이 혼자 반복한 링크는 '여러 명이 같은 링크' 가 아님 (도배 규칙 몫)
        await r.say(fake_user(70, "혼자"), f"https://solo.xyz/{i}")
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert not f.reasons and f.score == 0, f.reasons
    for i in range(12):   # 같은 글 반복이라 뒤 4개는 반복 규칙이 지움 → 훅 없음 → 예약된 다시 평가가 셈
        await r.say(fake_user(61 + i % 4, "멤버"), "https://t.me/+Spam&123")
    st = anomaly._rooms[CHAT]
    assert not alerts(r) and st.pending
    for t in list(anomaly._tasks):
        t.cancel()
    await anomaly._later(r.svc, r.bot, CHAT, st, 0)
    [a] = alerts(r)
    assert "t[.]me/+Spam&amp;123 12회 · 4명" in a[2] and not harness.html_errors(a[2]), a[2]


# ── ④ 새 계정 비율 · ⑤ 신규 멤버 도배 ─────────────────────
@test
async def recent_accounts_only_support():
    r = await room()
    await joins(r, 4, base=NEW + 500)     # 5명 미만이면 비율을 안 봄
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert not f.reasons, f.reasons
    r = await room()
    await joins(r, 10, base=NEW)          # 새 계정 100% 지만 입장 10명 → 20점만
    f = await anomaly.collect(r.svc, r.bot, CHAT)
    assert f.score == anomaly.PTS_RECENT and not alerts(r), (f.score, f.reasons)
    await joins(r, 6, base=NEW + 100)     # 16명: 몰림 35 + 새 계정 20
    [a] = alerts(r)
    assert "새 계정 100%" in a[2] and "새 계정 비율" in a[2], a[2]


@test
async def newbie_message_flood():
    r = await room(anomaly_level="sensitive")
    await joins(r, 4, name=lambda i: f"Lucky{i}")      # 비슷한 이름 4명 = 30점 (민감)
    assert not alerts(r)
    await r.say(fake_user(ADMIN, "방장"), "안녕하세요")    # 관리자 말은 안 셈
    for k in range(2):
        for i in range(4):
            await r.say(fake_user(OLD + i, f"Lucky{i}"), f"안녕 {k}")
    [a] = alerts(r)
    assert "신규 메시지 8개" in a[2] and "4명이 8개" in a[2], a[2]


# ── 대량 입장 방어와 같이 ─────────────────────────────────
@test
async def raid_active_no_double_alert_but_mentioned():
    r = await room(raid_guard=True, raid_count=10, raid_seconds=60)
    await joins(r, 30)
    assert await raid.active(r.svc, CHAT)
    assert any("방어 모드" in c[2] for c in dms(r, ADMIN)), "raid 가 알림"
    assert not alerts(r), "입장 신호는 raid 가 처리 중 → 이상징후 알림 안 함"
    for i in range(12):   # 입장과 다른 신호(같은 링크)는 따로 알림, 방어 중이라고 적음
        await r.say(fake_user(OLD + 900 + i % 3, "멤버"), f"https://evil.xyz/join{i}")
    await anomaly.evaluate(r.svc, r.bot, CHAT)
    [a] = alerts(r)
    assert "대량 입장 방어가 이미 켜져" in a[2] and "같은 링크" in a[2], a[2]


# ── 쿨다운 · 하루 상한 ────────────────────────────────────
@test
async def cooldown_and_daily_cap():
    r = await room()
    await joins(r, 30)
    assert len(alerts(r)) == 1
    await joins(r, 30, base=OLD + 1000)
    assert len(alerts(r)) == 1, "30분 쿨다운"
    await r.db._write("UPDATE anomaly_alerts SET ts=ts-? WHERE chat_id=?", (anomaly.COOLDOWN + 1, CHAT))
    await join(r, OLD + 5000)
    assert len(alerts(r)) == 2, "쿨다운 지나면 다시"
    f = anomaly.Finding(score=99, summary="x")
    later = time.time() + anomaly.COOLDOWN * 2
    day = anomaly._day(r.svc, later)
    have = (await r.db._one("SELECT COUNT(*) AS n FROM anomaly_alerts WHERE day=?", (day,)))["n"]
    for _ in range(anomaly.DAILY_CAP - have):
        await r.db._write("INSERT INTO anomaly_alerts(chat_id, ts, day, score, summary, detail) VALUES(?,?,?,?,?,?)",
                          (CHAT, 1, day, 1, "old", "{}"))
    assert await anomaly.claim(r.svc, CHAT, f, later) is None, "하루 상한"
    await r.db._write("DELETE FROM anomaly_alerts WHERE summary='old'")
    assert await anomaly.claim(r.svc, CHAT, f, later)


@test
async def concurrent_evaluations_alert_once():
    r = await room()
    await joins(r, 30)
    await r.db._write("DELETE FROM anomaly_alerts")
    r.bot.calls.clear()
    await asyncio.gather(*[anomaly.evaluate(r.svc, r.bot, CHAT) for _ in range(5)])
    assert len(alerts(r)) == 1 and len(await rows(r)) == 1


# ── 받는 사람 ─────────────────────────────────────────────
@test
async def recipients_need_ban_right_and_blocked_dm_is_skipped():
    r = await room(admins=(ADMIN, WEAK, THIRD))
    r.bot.dm_blocked = {ADMIN}
    await joins(r, 30)
    assert [c[1] for c in alerts(r)] == [THIRD], [c[1] for c in dms(r)]   # WEAK = 권한 없음, ADMIN = 1:1 막힘
    [row] = await rows(r)
    assert row["sent"] == 1


# ── 버튼: 상세 · 권한 · 무시 · 보안 강화 ─────────────────
async def alerted(r):
    anomaly.SCORE_MIN = 10 ** 6   # 신호를 다 모은 뒤 한 번에 알림
    try:
        await joins(r, 16, name=lambda i: f"<b>코인</b>&{i}", username=lambda i: f"coin_{i}")
        for i in range(12):
            await r.say(fake_user(OLD + i % 3, "x"), f"https://evil.xyz/a{i}")
    finally:
        anomaly.SCORE_MIN = 50
    await anomaly.evaluate(r.svc, r.bot, CHAT)
    a = alerts(r)[0]   # 받는 관리자마다 같은 알림 1통
    return buttons(a[3]["reply_markup"])


@test
async def detail_screen_escapes_and_defangs():
    r = await room()
    d, h, i = await alerted(r)
    q = await press(r, ADMIN, d)
    text = q.edits[-1]
    assert not harness.html_errors(text), harness.html_errors(text)
    assert "&lt;b&gt;코인&lt;/b&gt;&amp;0" in text and "<b>코인</b>" not in text, text
    assert f'<a href="tg://user?id={OLD}">' in text and "@coin_0" in text
    assert "evil[.]xyz" in text and "evil.xyz" not in text and "https://" not in text, text
    assert "시간대별" in text
    assert [b.rsplit(":", 1)[1] for b in buttons(q.kb)][:2] == ["h", "i"]


@test
async def buttons_recheck_ban_right_on_press():
    r = await room()
    d, h, i = await alerted(r)
    for data in (h, h[:-1] + "H", i):
        q = await press(r, WEAK, data)
        assert q.answers[0][1] and "사용자 차단" in q.answers[0][0] and not q.edits, (data, q.answers)
    q = await press(r, ADMIN, h)                  # 확인 화면
    assert "보안 강화" in q.edits[-1]
    r.rights.discard(ADMIN)                       # 그 사이 권한을 잃음
    q = await press(r, ADMIN, h[:-1] + "H")
    assert q.answers[0][1] and not await raid.active(r.svc, CHAT)
    [row] = await rows(r)
    assert row["status"] is None
    q = await press(r, 20, d)                     # 관리자 아닌 사람
    assert q.answers[0][1] and not q.edits


@test
async def ignore_marks_handled_and_logs():
    r = await room()
    d, h, i = await alerted(r)
    q = await press(r, ADMIN, i)
    assert "무시했어요" in q.answers[0][0] and "🙈 무시함" in q.edits[-1]
    assert not [b for b in buttons(q.kb) if b.endswith((":h", ":i"))], "처리 뒤엔 버튼 없음"
    [row] = await rows(r)
    assert row["status"] == "ignored" and row["by_id"] == ADMIN
    recent = await r.db.recent_mod_log(CHAT, 50)
    assert any(x["action"] == "anomaly_ignore" and x["actor_id"] == ADMIN for x in recent), "관리 기록에 보여야 함"
    assert log_panel.ACTIONS["anomaly_ignore"] == "🙈 이상징후 무시"
    q = await press(r, ADMIN, h[:-1] + "H")
    assert "이미 처리된" in q.answers[0][0] and not await raid.active(r.svc, CHAT)


@test
async def two_admins_pressing_at_once_handle_once():
    r = await room(admins=(ADMIN, WEAK, THIRD))
    d, h, i = await alerted(r)
    await asyncio.gather(press(r, ADMIN, i), press(r, THIRD, i))
    assert len(await modlog(r, "anomaly_ignore")) == 1


@test
async def harden_applies_and_auto_reverts_after_restart():
    r = await room(newbie_link_hours=0, forward_filter="off", anomaly_harden_hours=3)
    d, h, i = await alerted(r)
    q = await press(r, ADMIN, h)
    assert "신규 입장자 링크 금지: 24시간" in q.edits[-1] and "전달(포워드) 막기" in q.edits[-1]
    q = await press(r, ADMIN, h[:-1] + "H")
    assert not harness.html_errors(q.edits[-1])
    s = await r.db.get_settings(CHAT)
    assert s["newbie_link_hours"] == 24 and s["forward_filter"] == "newbie"
    until = await raid.until(r.svc, CHAT)
    assert abs(until - (time.time() + 3 * 3600)) < 120, until
    st = await r.db.get_state(CHAT, anomaly.HARDEN_KEY)
    assert st["restore"] == {"newbie_link_hours": 0, "forward_filter": "off"} and st["raid"], st
    assert (await rows(r))[0]["status"] == "hardened"
    assert await modlog(r, "anomaly_harden")
    q = await press(r, ADMIN, h[:-1] + "H")
    assert "이미 처리된" in q.answers[0][0]
    await r.db.set_setting(CHAT, "forward_filter", "all")   # 그 사이 관리자가 직접 바꾼 값은 그대로 둬야 함

    # 재시작: 메모리 비움 + 시간이 지남 → 이 방의 다음 메시지에서 되돌림
    for t in list(anomaly._tasks):
        t.cancel()
    anomaly._rooms.clear()
    anomaly._due.clear()
    st["until"] = int(time.time()) - 1
    await r.db.set_state(CHAT, anomaly.HARDEN_KEY, st)
    await r.say(fake_user(OLD + 777, "멤버"), "안녕")
    s = await r.db.get_settings(CHAT)
    assert s["newbie_link_hours"] == 0 and s["forward_filter"] == "all", s
    assert await r.db.get_state(CHAT, anomaly.HARDEN_KEY) is None
    [off] = await modlog(r, "anomaly_harden_off")
    assert "시간 끝남" in off["detail"]


@test
async def tick_reverts_quiet_room():
    r = await room(newbie_link_hours=0)
    await anomaly.harden(r.svc, r.bot, CHAT, ADMIN, 1)
    assert (await r.db.get_settings(CHAT))["newbie_link_hours"] == 24
    await anomaly.tick(r.svc, r.bot)
    assert (await r.db.get_settings(CHAT))["newbie_link_hours"] == 24, "아직 시간 전"
    st = await r.db.get_state(CHAT, anomaly.HARDEN_KEY)
    st["until"] = int(time.time()) - 1   # (메모리의 _due 는 아직 1시간 뒤 — tick 은 DB 를 다시 봐야 함)
    await r.db.set_state(CHAT, anomaly.HARDEN_KEY, st)
    await anomaly.tick(r.svc, r.bot)
    assert (await r.db.get_settings(CHAT))["newbie_link_hours"] == 0
    for t in list(anomaly._tasks):
        t.cancel()


@test
async def harden_extends_running_raid_and_unharden_button():
    r = await room(newbie_link_hours=0)
    await raid.start(r.svc, r.bot, CHAT, 10, actor_id=ADMIN)
    await anomaly.harden(r.svc, r.bot, CHAT, ADMIN, 2)
    assert await raid.until(r.svc, CHAT) > time.time() + 7000, "이미 방어 중이면 끝나는 시각만 늦춤"
    q = await press(r, ADMIN, f"m:anm:{CHAT}")
    assert "지금 보안 강화 중" in q.edits[-1] and f"m:anmu:{CHAT}:0" in buttons(q.kb)
    q = await press(r, WEAK, f"m:anmu:{CHAT}:1")
    assert q.answers[0][1] and await anomaly.hardened_until(r.svc, CHAT)
    q = await press(r, ADMIN, f"m:anmu:{CHAT}:0")
    assert f"m:anmu:{CHAT}:1" in buttons(q.kb)
    q = await press(r, ADMIN, f"m:anmu:{CHAT}:1")
    assert "껐어요" in q.answers[0][0] and not await anomaly.hardened_until(r.svc, CHAT)
    assert (await r.db.get_settings(CHAT))["newbie_link_hours"] == 0
    assert await raid.active(r.svc, CHAT), "이미 켜져 있던 방어 모드는 우리가 켠 게 아니라서 둠"
    for t in list(anomaly._tasks):
        t.cancel()


# ── 설정 화면 · 끄기 ──────────────────────────────────────
@test
async def settings_screen_presets_and_off():
    r = await room()
    q = await press(r, ADMIN, f"m:anm:{CHAT}")
    data = buttons(q.kb)
    assert f"m:n:{CHAT}:anomaly_mode:off" in data and f"m:n:{CHAT}:anomaly_level:sensitive" in data
    assert f"m:n:{CHAT}:anomaly_harden_hours:6" in data and not harness.html_errors(q.edits[-1])
    await press(r, ADMIN, f"m:n:{CHAT}:anomaly_mode:off")
    assert (await r.db.get_settings(CHAT))["anomaly_mode"] == "off"
    await joins(r, 40, name=lambda i: "같은이름")
    assert not alerts(r) and CHAT not in anomaly._rooms, "꺼지면 세지도 않음"
    assert menu.HUB_ITEMS and any(i.code == "anm" for i in menu.HUB_ITEMS)


@test
def callback_data_fits_64_bytes():
    kb = anomaly.alert_kb(-1009999999999999999 // 10, 10 ** 12)
    assert all(len(d.encode()) <= 64 for d in buttons(kb)), buttons(kb)


# ── 안전 · 비용 ───────────────────────────────────────────
@test
async def failures_never_break_message_or_join_handling():
    r = await room()
    old = anomaly.collect

    async def boom(*a, **k):
        raise RuntimeError("감지 고장")
    anomaly.collect = boom
    logging.disable(logging.ERROR)   # 일부러 낸 예외 로그는 숨김
    try:
        await joins(r, 3)
        assert len(await r.db._all("SELECT 1 FROM messages WHERE chat_id=? AND is_bot=1", (CHAT,))) == 3, "입장 기록 계속"
        m = await r.say(fake_user(OLD, "입장"), "https://evil.xyz")
        assert m.deleted, "관리 검사 계속"
        assert await anomaly.on_member_join(r.svc, r.bot, CHAT, fake_user(OLD + 9, "x")) is False
    finally:
        logging.disable(logging.NOTSET)
        anomaly.collect = old


@test
async def evaluation_is_rate_limited_and_memory_bounded():
    r = await room()
    anomaly.EVAL_GAP = 5
    calls = []
    old = anomaly.evaluate

    async def count(*a, **k):
        calls.append(1)
    anomaly.evaluate = count
    try:
        for i in range(anomaly.MAXQ * 2):
            await anomaly.on_member_join(r.svc, r.bot, CHAT, fake_user(OLD + i, f"n{i}"))
        st = anomaly._rooms[CHAT]
        assert len(calls) == 1 and st.pending and len(anomaly._tasks) == 1, (len(calls), len(anomaly._tasks))
        assert len(st.joins) == anomaly.MAXQ and len(st.newbies) == anomaly.MAXQ
        m = r.msg(fake_user(ADMIN + 10**6, "멤버"), "그냥 잡담")
        await anomaly.on_message(r.svc, r.bot, m, 0)
        assert len(calls) == 1, "링크도 신규도 아닌 메시지는 평가·DB 없이 끝"
    finally:
        anomaly.evaluate = old
        anomaly.EVAL_GAP = 0
        for t in list(anomaly._tasks):
            t.cancel()


# ── 하네스 ────────────────────────────────────────────────
@test
async def harness_reaches_anomaly_screens():
    rep, db, svc = await harness.crawl()
    seen = {s.data.split(":")[1] + ":" + s.data.rsplit(":", 1)[-1] for s in rep.shots if ":anm" in s.data}
    assert {"anmx:d", "anmx:h", "anmu:0"} <= seen, seen
    assert not [x for x in rep.issues if "anm" in x], rep.issues
