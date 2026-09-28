"""👤 멤버 타임라인 · 🔎 상황 분석 · 방 변화: python tests/run_all.py insight"""
import json
import time

from fakes import FakeBot, FakeQuery, fake_user, make_db, make_svc, runner
from harness import html_errors

from sodam import insight, memory, menu, tools
from sodam.insight import Facts, member_facts, suggest
from sodam.panels import members as M
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
CHAT, OTHER = -1005550000031, -1005550000032
ADMIN, CS, YH, SP, NEWBIE = 1, 2, 3, 4, 5
DAY = 86400


async def ins(db, sql, *p):
    await db._write(sql, p)


async def world():
    """철수(CS) 한 사람의 기록을 날짜까지 정해서 넣는다."""
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    bot = FakeBot(admins=[fake_user(ADMIN, "방장")])
    await db.ensure_chat(CHAT, "테스트방")
    await db.ensure_chat(OTHER, "다른방")
    now = int(time.time())
    for uid, first, un in ((ADMIN, "방장", "boss"), (CS, "<b>철수</b>&", "cs_new"), (YH, "영희", None), (SP, "스팸", None)):
        await db.upsert_user(fake_user(uid, first, un))
        await ins(db, "INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                  CHAT, uid, now - 50 * DAY, now - 60)
    await ins(db, "INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)", OTHER, YH, now - DAY, now)
    # 이름 기록: 처음 (철수, @cs_old) → 10일 전 아이디 바꿈 → 2일 전 이름 바꿈 = 변경 2회 (7일 안 1회)
    for first, un, ago in (("철수", "cs_old", 20), ("철수", "cs_new", 10), ("<b>철수</b>&", "cs_new", 2)):
        await ins(db, "INSERT INTO name_history(user_id, first_name, last_name, username, ts) VALUES(?,?,?,?,?)",
                  CS, first, None, un, now - ago * DAY)
    # 메시지: 60일 전 1 · 20일 전 3 · 5일 전 5 (링크 3) · 최근 2시간 25 (그중 답장)
    await db.log_message(CHAT, CS, None, "옛날 글", ts=now - 60 * DAY)
    for i in range(3):
        await db.log_message(CHAT, CS, None, f"20일 전 {i} http://old.xyz", ts=now - 20 * DAY)
    texts = ["사이트 https://spam.xyz", "t.me/joinme", "go to shop.com", "그냥 말", "점 하나. 끝"]
    for t in texts:
        await db.log_message(CHAT, CS, None, t, ts=now - 5 * DAY)
    replies = [YH, YH, ADMIN, SP] + [None] * 21
    for i, to in enumerate(replies):
        await db.log_message(CHAT, CS, None, f"최근 {i}", ts=now - 3600, reply_to_user=to)
    await db.log_message(CHAT, YH, None, "답장", ts=now - 1800, reply_to_user=CS)
    await db.log_message(CHAT, ADMIN, None, "관리자 답장", ts=now - 1800, reply_to_user=CS)
    await db.log_message(CHAT, CS, None, "나한테 답장", ts=now - 1800, reply_to_user=CS)   # 자기 답장은 안 셈
    # 경고: 7일 안 2회(자동) + 40일 전 1회 → 지금 누적 3
    for ago, reason in ((1, "금지어 사용"), (3, "같은 메시지 반복"), (40, "<옛날>&")):
        await ins(db, "INSERT INTO warnings(chat_id, user_id, by_id, reason, ts) VALUES(?,?,?,?,?)",
                  CHAT, CS, bot.id, reason, now - ago * DAY)
        await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                  CHAT, bot.id, CS, "warn", reason, now - ago * DAY)
    await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
              CHAT, ADMIN, CS, "mute", "1시간 / 관리자 판단", now - 2 * DAY)
    await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
              CHAT, ADMIN, SP, "ban", "스팸", now - DAY)                      # 스팸 = 제재받은 멤버
    await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
              OTHER, ADMIN, CS, "ban", "다른 방 기록", now - DAY)              # 다른 방 기록은 안 섞임
    # AI 기억 메모 + 본인 메모
    await memory.add_facts(db, CHAT, CS, ["커피 <좋아함> & 라떼"])
    await db.set_member_note(CHAT, CS, "호칭", "철수 대표")
    M._count_cache.clear()
    M._msg_cache.clear()
    return db, svc, bot, now


def ctx_for(svc, bot, chat_id=CHAT, uid=ADMIN, role=Role.ADMIN):
    return ToolCtx(svc=svc, bot=bot, chat_id=chat_id, caller=fake_user(uid, "방장"), role=role, settings={})


async def press(svc, bot, data, uid=ADMIN):
    svc.menu_limiter._hits.clear()
    q = FakeQuery(uid, fake_user(uid, "누구"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


# ── 타임라인 사실 ─────────────────────────────────────────
@test
async def timeline_facts_are_counted_from_records():
    db, svc, bot, now = await world()
    f = await member_facts(svc, bot, CHAT, CS, days=7)
    assert (f.msgs_total, f.msgs_30d, f.msgs_7d, f.msgs_24h) == (35, 34, 31, 26), (f.msgs_total, f.msgs_30d, f.msgs_7d, f.msgs_24h)
    assert abs(f.prev7_avg - 5 / 7) < 1e-9 and f.spike                 # 24시간 26개 vs 평소 하루 0.7개
    assert f.first_seen == now - 60 * DAY and f.joined_at == now - 50 * DAY
    assert f.name_changes() == 2 and f.name_changes(f.since) == 1       # 처음 본 값은 변경이 아님
    assert f.names[0][0] == "<b>철수</b>&" and f.usernames[0][0] == "cs_new"
    assert (f.replies_sent, f.replies_recv) == (4, 2), (f.replies_sent, f.replies_recv)   # 자기 답장 제외
    assert f.admin_replies == 2 and f.sanctioned_replies == 1
    assert dict((p[0], (p[1], p[2])) for p in f.partners)["영희"] == (2, 1)
    assert f.link_msgs == 3, f.link_msgs                                # 7일 안 링크 글 (20일 전 것 제외)
    assert f.warnings_active == 3 and f.count("warn") == 2 and len(f.events) == 4   # 다른 방 ban 안 섞임
    assert f.admin_actions == 1 and f.count("mute") == 1 and f.auto_count("mute") == 0
    assert [e[0] for e in f.events] == sorted((e[0] for e in f.events), reverse=True)   # 최신 먼저 (넣은 순서 무관)
    assert not f.is_admin and (await member_facts(svc, bot, CHAT, ADMIN)).is_admin
    assert await member_facts(svc, bot, OTHER, CS) is None               # 그 방 멤버가 아니면 없음
    f30 = await member_facts(svc, bot, CHAT, CS, days=99)
    assert f30.days == 30 and f30.link_msgs == 6                         # 기간 상한 30일


@test
async def timeline_separates_facts_from_ai_memo():
    db, svc, bot, now = await world()
    f = await member_facts(svc, bot, CHAT, CS, days=30)
    m = await insight.memo(db, CHAT, CS)
    t = insight.timeline_text(f, svc.cfg.tz, m)
    memo_at = t.index("[AI 기억 메모")
    assert t.index("[기록된 사실]") < memo_at and "확인 안 됨" in t[memo_at:]
    assert t.index("커피") > memo_at and t.index("철수 대표") > memo_at   # 메모는 메모 칸에만
    assert "커피" not in insight.timeline_text(f, svc.cfg.tz, None)
    assert "위험" not in t
    h = insight.timeline_html(f, svc.cfg.tz, m)
    assert not html_errors(h), html_errors(h)
    facts_part, memo_part = h.split("🧠 AI 기억 메모")
    assert "📋 기록된 사실" in facts_part and "커피" in memo_part and "커피" not in facts_part
    assert "&lt;b&gt;철수&lt;/b&gt;&amp;" in h and "<b>철수</b>&" not in h   # 이름·메모 esc
    assert "&lt;좋아함&gt; &amp; 라떼" in h


# ── AI 도구: 권한·등록·결과 ───────────────────────────────
@test
async def tools_registered_by_role_and_where():
    names = {"member_timeline", "analyze_member", "room_changes"}
    room_admin = {t.name for t in tools.available(Role.ADMIN, {}, False)}
    assert names <= room_admin and "owner_room_insight" not in room_admin
    assert not names & {t.name for t in tools.available(Role.MEMBER, {}, False)}
    assert not names & {t.name for t in tools.available(Role.ADMIN, {}, True)}      # 1:1 엔 방 도구 없음
    assert "owner_room_insight" in {t.name for t in tools.available(Role.OWNER, {}, True)}
    assert "owner_room_insight" not in {t.name for t in tools.available(Role.ADMIN, {}, True)}
    assert names | {"owner_room_insight"} <= tools.READ_ONLY
    for t in insight.INSIGHT_TOOLS:
        json.dumps(t.schema())


@test
async def member_cannot_call_and_other_room_admin_cannot_see():
    db, svc, bot, now = await world()
    for name in ("member_timeline", "analyze_member", "room_changes"):
        r = await tools.execute(name, json.dumps({"name": "철수"}), ctx_for(svc, bot, uid=YH, role=Role.MEMBER))
        assert "권한 없음" in r, r
    # 다른 방(OTHER)의 관리자: 그 방엔 철수가 없음 → 찾을 수 없음, 이 방 숫자 안 나옴
    r = await tools.execute("member_timeline", '{"name": "철수"}', ctx_for(svc, bot, chat_id=OTHER))
    assert "찾을 수 없어요" in r and "메시지" not in r, r
    r = await tools.execute("member_timeline", '{"name": "%d"}' % CS, ctx_for(svc, bot, chat_id=OTHER))
    assert "찾을 수 없어요" in r, r


@test
async def timeline_tool_output_and_taint():
    db, svc, bot, now = await world()
    ctx = ctx_for(svc, bot)
    r = await tools.execute("member_timeline", '{"name": "cs_new"}', ctx)
    assert r.startswith("<b>철수</b>&(2) 메시지 7일 31·30일 34·보관 전체 35 · 경고 3회"), r[:120]
    assert "[AI 기억 메모" in r and ctx.tainted                          # 멤버 글에서 뽑은 메모 → 이후 읽기 도구만
    assert "이름·아이디 변경 2회" in r and "답장 보냄 4·받음 2" in r
    r2 = await tools.execute("analyze_member", '{"name": "철수"}', ctx)    # 읽기 전용이라 tainted 여도 됨
    assert "추천" in r2
    r3 = await tools.execute("mute_member", '{"names": ["철수"], "minutes": 60}', ctx)
    assert "보안" in r3                                                   # 제재는 tainted 답변에서 막힘
    ctx2 = ctx_for(svc, bot)
    await db._write("DELETE FROM member_memory")
    await db.set_member_note(CHAT, CS, "호칭", "")
    r = await tools.execute("member_timeline", '{"name": "철수"}', ctx2)
    assert not ctx2.tainted and "없음" in r.split("[AI 기억 메모")[1]


@test
async def analyze_member_suggests_by_rules_not_executes():
    db, svc, bot, now = await world()
    ctx = ctx_for(svc, bot)
    r = await tools.execute("analyze_member", '{"name": "철수", "days": 7}', ctx)
    assert "경고 2회(지금 누적 3)" in r and "링크 글 3개" in r and "이름·아이디 변경 1번" in r, r
    assert "채팅 금지 1일 (mute_member minutes=1440)" in r                # 뮤트 뒤에도 경고 → 1일
    assert insight.TOOL_NOTE in r and not ctx.tainted and not ctx.sanctioned
    assert not bot.named("send_message")                                  # 확인 카드도 안 보냄 (추천만)
    r = await tools.execute("analyze_member", '{"name": "방장"}', ctx)
    assert "관리자라 제재 대상 아님" in r


def _f(**kw):
    f = Facts(CHAT, CS, "철수", None, 7, int(time.time()))
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def _ev(action, n, ago=DAY, human=False):
    return [(int(time.time()) - ago, action, "", human)] * n


@test
def suggestion_thresholds():
    assert suggest(_f()) == ["특별한 조치 필요 없음 (주의만)"]
    assert suggest(_f(events=_ev("warn", 1)))[0].startswith("경고 1회 있음")
    assert suggest(_f(events=_ev("warn", 2)))[0].startswith("채팅 금지 1시간 (mute_member minutes=60)")
    assert suggest(_f(events=_ev("warn", 3)))[0].startswith("채팅 금지 1일 (mute_member minutes=1440)")
    assert suggest(_f(events=_ev("warn", 5, ago=10 * DAY)))[0].startswith("특별한")   # 기간 밖 경고는 안 셈
    assert suggest(_f(link_msgs=3))[0].startswith("채팅 금지 1시간") and "링크 반복" in suggest(_f(link_msgs=3))[0]
    assert suggest(_f(link_msgs=2))[0].startswith("링크 규칙 안내")
    assert suggest(_f(link_blocks=2))[0].startswith("채팅 금지 1시간")
    assert suggest(_f(events=_ev("scam_hide", 2)))[0].startswith("내보내기 검토 (ban_member)")
    assert suggest(_f(events=_ev("scam_alert", 1)))[0].startswith("사기 의심 기록 1번")
    assert suggest(_f(msgs_24h=30, prev7_avg=5))[0].startswith("대화량 급증")
    assert suggest(_f(msgs_24h=12, prev7_avg=1))[0].startswith("특별한")                # 20개 미만은 급증 아님
    assert suggest(_f(is_admin=True, events=_ev("warn", 9))) == ["관리자라 제재 대상 아님 (조치 추천 없음)"]
    both = suggest(_f(events=_ev("warn", 3) + _ev("mute", 1), link_msgs=4))
    assert both[0].startswith("채팅 금지 1일") and len([s for s in both if s.startswith("채팅 금지 1일")]) == 1


# ── 방 변화 ───────────────────────────────────────────────
async def room_world():
    db, svc, bot, now = await world()
    for uid in range(10, 16):
        await db.upsert_user(fake_user(uid, f"손님{uid}"))
    # 5시간 전 2명 입장, 3시간 전 3명 나감(1명은 방금 들어온 사람, 1명은 킥 기록), 3일 전 1명 나감(비교 기준)
    for uid in (10, 11):
        await ins(db, "INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                  CHAT, uid, now - 5 * 3600, now - 5 * 3600)
    for uid in (12, 13):
        await ins(db, "INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                  CHAT, uid, now - 40 * DAY, now - 4 * 3600)
    for uid, ago in ((10, 3 * 3600), (12, 3 * 3600), (13, 3 * 3600), (14, 3 * DAY)):
        await ins(db, "INSERT INTO member_left(chat_id, user_id, ts) VALUES(?,?,?)", CHAT, uid, now - ago)
    await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
              CHAT, ADMIN, 13, "kick", "관리자 판단", now - 3 * 3600)
    await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
              CHAT, None, None, "raid", "30분 / 60초 10명", now - 3 * 3600)
    await ins(db, "INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
              CHAT, ADMIN, None, "ask_mute", "감사 기록은 안 보임", now - 3 * 3600)
    for i in range(40):
        await db.log_message(CHAT, YH, None, f"폭주 {i}", ts=now - 3 * 3600)
    await db.log_message(CHAT, bot.id, None, "봇 공지", is_bot=True, ts=now - 3 * 3600)
    await ins(db, "INSERT INTO schedules(chat_id, kind, at_time, title, text, last_sent) VALUES(?,?,?,?,?,?)",
              CHAT, "daily", "09:00", "<공지>&", "본문", now - 3 * 3600)
    return db, svc, bot, now


@test
async def room_changes_counts_leaves_and_buckets():
    db, svc, bot, now = await room_world()
    rc = await insight.room_changes(svc, bot, CHAT, "24h")
    assert (rc.joins, rc.leaves) == (2, 3), (rc.joins, rc.leaves)
    assert rc.kicked_leavers == 1 and rc.quick_leavers == 1
    assert abs(rc.base_leaves - 1 / 7) < 1e-9
    hot = max(rc.buckets, key=lambda b: b.human)
    assert hot.leaves == 3 and hot.human == 40 and hot.bot == 1
    assert hot.events.get("대량 입장 방어 발동") == 1 and hot.events.get("관리자 내보내기") == 1
    assert hot.events.get("예약 공지") == 1 and not any("ask" in k for b in rc.buckets for k in b.events)
    assert rc.human == 40 + 1 + 26 + 1 and rc.admin == 1, (rc.human, rc.admin)   # 영희 41 · 철수 26 · 관리자 1
    t = insight.changes_text(rc, svc.cfg.tz)
    assert t.startswith("최근 24시간 방 변화: 입장 2 · 나감 3 (순 -1)"), t[:80]
    assert "봇·관리자가 내보낸 기록 1" in t and "들어왔다 나감 1" in t and "급증" in t and "추정" in t
    assert t.index("가장 바쁜 시간: " + hot.label + " 40") > 0
    week = await insight.room_changes(svc, bot, CHAT, "7d")
    assert week.leaves == 4 and all("(" in b.label for b in week.buckets)       # 7일 = 날짜(요일) 버킷
    assert (await insight.room_changes(svc, bot, CHAT, "이상한값")).period == "today"


@test
async def room_changes_tool_admin_room_only():
    db, svc, bot, now = await room_world()
    r = await tools.execute("room_changes", '{"period": "24h"}', ctx_for(svc, bot))
    assert "나감 3" in r and len(r) < 2500
    r = await tools.execute("room_changes", '{"period": "24h"}', ctx_for(svc, bot, uid=YH, role=Role.MEMBER))
    assert "권한 없음" in r


@test
async def owner_dm_variant_resolves_room_and_taints():
    db, svc, bot, now = await room_world()
    ctx = ctx_for(svc, bot, chat_id=ADMIN, role=Role.OWNER)
    r = await tools.execute("owner_room_insight", '{"room": "테스트방", "kind": "changes", "period": "24h"}', ctx)
    assert r.startswith("[테스트방]") and "나감 3" in r and ctx.tainted, r[:100]
    ctx = ctx_for(svc, bot, chat_id=ADMIN, role=Role.OWNER)
    r = await tools.execute("owner_room_insight", '{"room": "테스트방", "kind": "analyze", "name": "철수"}', ctx)
    assert "추천" in r
    r = await tools.execute("owner_room_insight", '{"room": "없는방", "kind": "changes"}', ctx)
    assert "못 찾음" in r
    r = await tools.execute("owner_room_insight", '{"room": "테스트방", "kind": "changes"}', ctx_for(svc, bot))
    assert "권한 없음" in r                                               # 방 관리자·그룹방에선 안 보임


# ── 버튼 화면 ─────────────────────────────────────────────
@test
async def members_list_links_to_timeline_screen():
    db, svc, bot, now = await world()
    q = await press(svc, bot, f"m:mb:{CHAT}")
    btns = [b for row in q.kb.inline_keyboard for b in row if b.callback_data.startswith("m:mbt:")]
    assert btns and all(len(b.callback_data.encode()) <= 64 for b in btns)
    data = next(b.callback_data for b in btns if f":{CS}:" in b.callback_data)
    q = await press(svc, bot, data)
    t = q.edits[-1]
    assert "📋 기록된 사실" in t and "🧠 AI 기억 메모" in t and not html_errors(t), html_errors(t)
    assert "💬 메시지 7일 <b>31</b>" in t
    back = [b.callback_data for row in q.kb.inline_keyboard for b in row]
    assert f"m:mb:{CHAT}:seen:0" in back
    big = f"m:mbt:-1009999999999999:{2 ** 62}:join"
    assert len(big.encode()) <= 64


@test
async def timeline_screen_permissions():
    db, svc, bot, now = await world()
    q = await press(svc, bot, f"m:mbt:{CHAT}:{CS}", uid=YH)                # 일반 멤버
    assert not q.edits and q.answers[0][1]
    real = svc.perms.is_admin

    async def only_other(bot_, cid, uid):                                   # OTHER 방만 관리자인 사람
        return uid == 77 and cid == OTHER or await real(bot_, cid, uid)
    svc.perms.is_admin = only_other
    q = await press(svc, bot, f"m:mbt:{CHAT}:{CS}", uid=77)
    assert not q.edits and q.answers[0][1]
    q = await press(svc, bot, f"m:mbt:{OTHER}:{CS}", uid=77)               # 자기 방이어도 그 방 멤버가 아니면
    assert not q.edits and "본 적 없는" in q.answers[0][0]
    for bad in ("x", "-5", "", "²"):
        q = await press(svc, bot, f"m:mbt:{CHAT}:{bad}")
        assert not q.edits and q.answers[0][1], bad


@test
async def harness_reaches_timeline():
    import harness
    db, svc, bot = await harness.make_world()
    rep = harness.Report()
    await harness.crawl_as(svc, bot, harness.TG, rep, [f"m:mb:{harness.CHAT}"])
    shots = [s for s in rep.shots if s.data.startswith("m:mbt:")]
    assert shots and any("🧠 AI 기억 메모" in s.text and "커피" in s.text for s in shots)
    assert not rep.issues, rep.issues[:5]


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
