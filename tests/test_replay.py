"""🎞️ 사건 재현 · 🔬 설정 시뮬레이터: python tests/run_all.py replay

실제 흐름(handlers.on_group_message → ai_reply → run_agent → 도구 → 답장)으로: 답장한 메시지가 기준이 되는지, 멤버는 못 쓰는지,
다른 방 기록이 안 섞이는지, 읽은 뒤 제재 도구가 막히는지(tainted), 기준 메시지 없음·봇 글, 큰 방 한도, 글자 처리,
시뮬레이션 뒤 설정이 그대로인지, 그리고 moderation 판정과 replay() 판정이 같은지(어긋나면 FAIL).
"""
import json
import time
from datetime import datetime, timezone

from fake_llm import Room, reply, tool_call
from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner

from sodam import replay, tools
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
BOSS = fake_user(1, "방장", "boss")
CS = fake_user(20, "<b>철수</b>", "chulsoo")      # 이름에 꺾쇠 (글자 처리 확인)
YH = fake_user(21, "영희", "younghee")
MJ = fake_user(22, "민지", "minji")               # 관계없는 사람
OTHER = -100888
DAY, HOUR = 86400, 3600


async def room(**settings):
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False, "greet_enabled": False, **settings})
    for u in (BOSS, CS, YH, MJ):
        await r.join(u)
    return r


def tool_results(r) -> list[str]:
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


async def fight(r):
    """철수·영희 말다툼 + 민지 딴 얘기 + 철수 링크(자동 삭제). 기준이 될 철수 글을 돌려준다."""
    a = await r.say(CS, "어제 거래 먹튀한 거 너지?")
    b = await r.say(YH, "무슨 소리야 증거 있어?", reply_to=a)
    await r.say(MJ, "오늘 점심 뭐 먹지 (관계없는 얘기)")
    anchor = await r.say(CS, "증거 여기 @younghee 봐라 ㅋㅋ", reply_to=b)
    await r.say(YH, "그만해 진짜", reply_to=anchor)
    await r.say(CS, "여기 봐 https://evil.xyz/proof")          # link_filter 기본 켜짐 → 지워지고 link_del 기록
    await r.say(CS, "왜 지워")                                  # 같은 초의 다음 글 — 자동 조치는 링크 글에만 붙어야 함
    return anchor


# ── 🎞️ 사건 재현: 실제 에이전트 흐름 ──────────────────────
@test
async def incident_uses_replied_message_and_taints():
    r = await room()
    anchor = await fight(r)
    # 다른 방: 같은 텔레그램 메시지 ID·같은 사람의 글과 관리 기록 → 절대 섞이면 안 됨 (나중에 넣어 id 가 더 큼)
    await r.db.log_message(OTHER, CS.id, anchor.message_id, "다른방 비밀 글", ts=int(time.time()))
    await r.db.log_mod(OTHER, BOSS.id, CS.id, "ban", "다른방 밴 사유")
    r.llm.script = [tool_call("build_incident_case", {}),
                    tool_call("warn_member", {"names": ["영희"], "reason": "x"}, "c2"),   # 읽은 뒤 제재 → 막혀야 함
                    reply("<b>철수</b> 님이 먼저 링크를 올렸어요. 추정: 거래 다툼.")]
    m = await r.say(BOSS, "소담아 이거 무슨 일이야", reply_to=anchor)
    res = tool_results(r)
    case, blocked = res[0], res[1]
    assert "사건 재현:" in case and "관련된 사람 2명" in case, case[:300]
    assert "증거 여기" in case.split("\n▶ ")[1].split("\n")[0], case     # 기준 = 답장한 메시지
    assert "영희 ↩ ‹b›철수‹/b›" in case and "‹b›철수‹/b› ↩ 영희" in case, case        # 답장 화살표
    assert "점심" not in case and "민지" not in case                                # 관계없는 사람 글은 없음
    assert "다른방" not in case                                                     # 다른 방 기록 안 섞임
    assert "evil[.]xyz" in case and "evil.xyz" not in case                          # 링크는 안 눌리게
    line = lambda t: next(x for x in case.split("\n") if t in x)  # noqa: E731
    assert "[직후 자동: 링크 지움]" in line("evil[.]xyz/proof") and "직후" not in line("왜 지워") and "링크 지움 (자동) → ‹b›철수‹/b›" in case, case
    assert "<b>" not in case and "추정" in case                                      # 꺾쇠는 ‹›, 해석 규칙
    assert "보안" in blocked and not r.svc.pending                                   # tainted → 제재 도구 막힘
    assert m.replies and "&lt;b&gt;" in m.replies[-1] and "<b>철수" not in m.replies[-1]   # 방에 가는 답은 esc


@test
async def incident_member_cannot_see_or_call():
    r = await room()
    anchor = await fight(r)
    r.llm.script = [tool_call("build_incident_case", {}), reply("그건 관리자만 볼 수 있어요.")]
    await r.say(MJ, "소담아 이거 무슨 일이야", reply_to=anchor)
    call = r.llm.of("chat")[0]
    assert "build_incident_case" not in json.dumps(call["tools"], ensure_ascii=False)
    assert "simulate_setting_change" not in json.dumps(call["tools"], ensure_ascii=False)
    assert "사용할 수 없음" in tool_results(r)[0] and "증거" not in tool_results(r)[0]
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, MJ, Role.MEMBER, await r.db.get_settings(Room.CHAT), reply_msg_id=anchor.message_id)
    out = await tools.execute("build_incident_case", "{}", ctx)
    assert "권한 없음" in out and not ctx.tainted
    ctx = ToolCtx(r.svc, r.bot, BOSS.id, BOSS, Role.OWNER, {}, reply_msg_id=anchor.message_id)   # 1:1 에선 안 보임
    assert "권한 없음" in await tools.execute("build_incident_case", "{}", ctx)
    assert "권한 없음" in await tools.execute("simulate_setting_change", '{"change": "link_filter", "value": "on"}', ctx)


@test
async def incident_anchor_missing_bot_or_none():
    r = await room()
    await fight(r)
    ghost = r.msg(CS, "기록 안 된 글 (사진만 있던 글 같은 것)")                 # 봇이 못 본 메시지에 답장
    r.llm.script = [tool_call("build_incident_case", {}), reply("그 메시지는 기록이 없어요.")]
    await r.say(BOSS, "소담아 이거 무슨 일이야", reply_to=ghost)
    assert "기록에 없음" in tool_results(r)[0]
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, {}, reply_msg_id=ghost.message_id)
    assert "기록에 없음" in await tools.execute("build_incident_case", "{}", ctx) and not ctx.tainted
    await r.db.log_message(Room.CHAT, r.bot.id, 5555, "소담 답", is_bot=True)
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, {}, reply_msg_id=5555)
    assert "봇" in await tools.execute("build_incident_case", "{}", ctx) and not ctx.tainted
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, {})                   # 답장도 시각도 없음
    assert await tools.execute("build_incident_case", "{}", ctx) == replay.NO_ANCHOR and not ctx.tainted
    assert "못 읽었음" in await tools.execute("build_incident_case", '{"at": "아까"}', ctx)


@test
async def incident_by_time_picks_reply_pair_and_name_changes():
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id})
    bot = FakeBot(admins=[BOSS])
    await db.ensure_chat(Room.CHAT, "방")
    for u in (BOSS, CS, YH, MJ):
        await add_member(db, Room.CHAT, u)
    tz = svc.cfg.tz
    center = int(datetime.now(tz).replace(second=0, microsecond=0).timestamp()) - 3 * HOUR
    for i in range(4):
        await db.log_message(Room.CHAT, CS.id, 10 + i, f"철수 {i}", ts=center - 60 + i, reply_to_user=YH.id)
        await db.log_message(Room.CHAT, YH.id, 20 + i, f"영희 {i}", ts=center - 30 + i, reply_to_user=CS.id)
    for i in range(6):
        await db.log_message(Room.CHAT, MJ.id, 30 + i, f"민지 혼잣말 {i}", ts=center + i)
    await db.log_message(Room.CHAT, MJ.id, 40, "민지 먼 글", ts=center + 40 * 60)               # 범위 밖
    await db._write("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                    (Room.CHAT, BOSS.id, CS.id, "mute", "1시간 / 싸움", center + 120))
    await db._write("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                    (Room.CHAT, BOSS.id, CS.id, "ask_mute", "확인 카드", center + 100))         # 오너 감사 기록은 안 보임
    now = int(time.time())
    for first, un, ago in (("철수", "cs_old", 3 * DAY), ("<b>철수</b>", "chulsoo", DAY)):
        await db._write("INSERT INTO name_history(user_id, first_name, last_name, username, ts) VALUES(?,?,?,?,?)",
                        (CS.id, first, None, un, now - ago))
    ctx = ToolCtx(svc, bot, Room.CHAT, BOSS, Role.ADMIN, {})
    at = datetime.fromtimestamp(center, tz).strftime("%H:%M")
    out = await tools.execute("build_incident_case", json.dumps({"at": at, "minutes": 10}), ctx)
    assert ctx.tainted and "관련된 사람 2명" in out and "민지" not in out, out
    assert "답장한 메시지 없음 → 시각 기준" in out and "‹b›철수‹/b› ↩ 영희 4" in out, out
    assert "채팅 금지 (관리자 방장) → ‹b›철수‹/b›" in out and "확인 카드" not in out, out
    assert "이름 철수 → ‹b›철수‹/b›" in out and "아이디 @cs_old → @chulsoo" in out, out
    ctx = ToolCtx(svc, bot, Room.CHAT, BOSS, Role.ADMIN, {})
    out = await tools.execute("build_incident_case", json.dumps({"at": at, "minutes": 10, "name": "민지"}), ctx)
    assert "민지 혼잣말 0" in out and "관련된 사람 1명" in out and "먼 글" not in out, out
    ctx = ToolCtx(svc, bot, Room.CHAT, BOSS, Role.ADMIN, {})
    out = await tools.execute("build_incident_case", json.dumps({"at": at, "name": "없는사람"}), ctx)
    assert "찾을 수 없어요" in out and not ctx.tainted
    assert replay._parse_at("어제 23:10", tz, now) < now - 60 and replay._parse_at("25:00", tz, now) is None
    assert replay._parse_at("오후 3시", tz, now) is not None


@test
async def incident_huge_room_stays_within_limits():
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id})
    bot = FakeBot(admins=[BOSS])
    await db.ensure_chat(Room.CHAT, "큰방")
    await add_member(db, Room.CHAT, CS)
    center = int(time.time()) - HOUR
    rows = []
    for i in range(6000):   # ±60분에 6천 개: 철수와 200명이 답장 폭탄
        uid = CS.id if i % 2 else 1000 + i % 200
        rows.append((Room.CHAT, uid, 100 + i, "가" * 300 + f" {i}", center - 3000 + i, 0, 0, None,
                     YH.id if uid == CS.id else CS.id))
    await db.atomic(lambda c: c.executemany(
        "INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged, reply_to_msg_id, reply_to_user) "
        "VALUES(?,?,?,?,?,?,?,?,?)", rows))
    ctx = ToolCtx(svc, bot, Room.CHAT, BOSS, Role.ADMIN, {}, reply_msg_id=100 + 3001)
    t0 = time.time()
    out = await tools.execute("build_incident_case", '{"minutes": 999}', ctx)
    assert time.time() - t0 < 5 and len(out) <= replay.BUDGET + 400, (time.time() - t0, len(out))
    assert "±60분" in out and "생략" in out and "관련된 사람 6명" in out and "▶ " in out, out[:400]
    assert out.count("\n") < 60


# ── 🔬 설정 시뮬레이터 ────────────────────────────────────
async def sim_world(**settings):
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id})
    bot = FakeBot(admins=[BOSS])
    await db.ensure_chat(Room.CHAT, "방")
    await db.ensure_chat(OTHER, "다른방")
    for k, v in settings.items():
        await db.set_setting(Room.CHAT, k, v)
    now = int(time.time())
    for u, joined in ((BOSS, now - 90 * DAY), (CS, now - 90 * DAY), (YH, now - 2 * HOUR), (MJ, now - 50 * HOUR)):
        await add_member(db, Room.CHAT, u)
        await db._write("UPDATE members SET joined_at=? WHERE chat_id=? AND user_id=?", (joined, Room.CHAT, u.id))
    return db, svc, bot, now


async def snapshot(db):
    raw = await db._one("SELECT settings FROM chats WHERE chat_id=?", (Room.CHAT,))
    words = await db._all("SELECT word FROM banned_words WHERE chat_id=? ORDER BY word", (Room.CHAT,))
    return raw["settings"], [w["word"] for w in words], dict(await db.get_settings(Room.CHAT))


async def sim(svc, bot, args, uid=BOSS.id, role=Role.ADMIN):
    ctx = ToolCtx(svc, bot, Room.CHAT, fake_user(uid, "방장"), role, await svc.db.get_settings(Room.CHAT))
    return await tools.execute("simulate_setting_change", json.dumps(args, ensure_ascii=False), ctx), ctx


@test
async def simulate_link_filter_counts_and_never_changes_settings():
    db, svc, bot, now = await sim_world(link_filter=False, newbie_link_hours=0, whitelist_domains=["youtube.com"])
    await db.set_banned_word(Room.CHAT, "욕설", True)
    msgs = [(CS, "https://evil.xyz/a <script>"), (CS, "youtube.com/watch?v=1"), (YH, "t.me/joinme 들어와"),
            (MJ, "그냥 대화"), (BOSS, "관리자 링크 https://boss.com"), (YH, "욕설 섞인 글 evil.xyz")]
    for i, (u, t) in enumerate(msgs):
        await db.log_message(Room.CHAT, u.id, 100 + i, t, ts=now - 600 + i * 30)
    await db.log_message(OTHER, CS.id, 1, "다른방 링크 https://x.xyz", ts=now - 60)     # 다른 방은 안 셈
    await db.log_message(Room.CHAT, CS.id, 2, "옛날 링크 https://old.xyz", ts=now - 30 * HOUR)   # 기간 밖
    before = await snapshot(db)
    out, ctx = await sim(svc, bot, {"change": "link_filter", "value": "on"})
    assert await snapshot(db) == before and before[2]["link_filter"] is False     # DB·캐시 설정 그대로
    assert out.startswith("시뮬레이션(실제 설정은 안 바뀜): 링크 차단 꺼짐 → 켜짐 · 최근 24시간 메시지 5개 확인 → "
                          "새 설정이면 3개·2명 걸림 (지금 설정 1개 → 새로 걸림 2·덜 걸림 0)"), out
    assert "관리자·자유 멤버 글 1개" in out and "금지어(삭제+경고) 1" in out and "링크(삭제) 2" in out, out
    assert "evil[.]xyz" in out and "t[.]me/joinme" in out and "‹script›" in out and "<script>" not in out
    assert "evil.xyz" not in out and "boss" not in out and "old" not in out and "다른방" not in out
    assert ctx.tainted and "주의:" in out
    out, ctx = await sim(svc, bot, {"change": "link_filter", "value": "on"}, uid=CS.id, role=Role.MEMBER)
    assert "권한 없음" in out and not ctx.tainted


@test
async def simulate_banned_word_whitelist_newbie_dup_flood():
    db, svc, bot, now = await sim_world(link_filter=True, newbie_link_hours=24)
    rows = [(CS, "먹튀 사이트 조심"), (YH, "여기 먹튀 아님"), (CS, "youtube.com/watch 영상"), (YH, "youtube.com 링크"),
            (MJ, "shop.com 구경"), (MJ, "shop.com 구경")]
    for i, (u, t) in enumerate(rows):
        await db.log_message(Room.CHAT, u.id, 100 + i, t, ts=now - 3000 + i * 60)
    await db.set_banned_word(Room.CHAT, "기존", True)
    before = await snapshot(db)
    out, _ = await sim(svc, bot, {"change": "banned_word_add", "value": "먹튀, 먹튀"})
    assert "금지어 추가: 먹튀 · " in out and "새로 걸림 2" in out and "금지어(삭제+경고) 2" in out, out
    out, _ = await sim(svc, bot, {"change": "whitelist_add", "value": "https://www.YouTube.com/x"})
    assert "허용 도메인에 youtube.com 추가" in out and "덜 걸림 2" in out and "예시 덜 걸리는 글 2개" in out, out
    out, _ = await sim(svc, bot, {"change": "whitelist_remove", "value": "nope.com"})
    assert "지금 목록에 없음" in out and "새로 걸림 0·덜 걸림 0" in out, out
    await db.set_setting(Room.CHAT, "link_filter", False)
    before = await snapshot(db)
    # 신규 72시간: 영희(2시간 전 입장)는 24시간에도 신규, 민지(50시간 전)는 72시간이면 신규 → 2개 새로 걸림
    out, _ = await sim(svc, bot, {"change": "newbie_link_hours", "value": "72"})
    assert "신규 링크 금지 24시간 → 72시간" in out and "지금 설정 1개 → 새로 걸림 2" in out, out
    # 반복 한도 2: 민지 같은 말 2번 → 걸림
    out, _ = await sim(svc, bot, {"change": "dup_limit", "value": "엄격"})
    assert "같은 말 반복 한도 3회 → 2회" in out and "같은 말 반복(삭제+경고) 1" in out, out
    assert await snapshot(db) == before
    for bad in ({"change": "newbie_link_hours", "value": "9999"}, {"change": "flood", "value": "아주"},
                {"change": "whitelist_add", "value": "<b>"}, {"change": "nope", "value": "1"},
                {"change": "banned_word_add", "value": ""}):
        out, ctx = await sim(svc, bot, bad)
        assert "시뮬레이션" not in out and not ctx.tainted, (bad, out)
    out, ctx = await sim(svc, bot, {"change": "lock", "value": "사진"})
    assert "기록에 메시지 종류가 남지 않아서" in out and not ctx.tainted


@test
async def simulate_flood_preset_and_mute_window():
    db, svc, bot, now = await sim_world()
    t0 = now - 1000
    for i in range(5):                       # 5초에 5개 → 보통(6개/8초)은 통과, 엄격(4개/8초)은 4번째에 뮤트
        await db.log_message(Room.CHAT, CS.id, 100 + i, f"말 {i}", ts=t0 + i)
    await db.log_message(Room.CHAT, CS.id, 200, "뮤트 중 글", ts=t0 + 60)
    await db.log_message(Room.CHAT, CS.id, 201, "뮤트 끝난 뒤", ts=t0 + 61 * 60 + 5)
    for i in range(4):
        await db.log_message(Room.CHAT, YH.id, 300 + i, "!홀짝 1000 홀", ts=t0 + i)   # 게임 명령은 도배 수에서 뺌
    out, _ = await sim(svc, bot, {"change": "flood", "value": "엄격"})
    assert "도배 기준 8초에 6개 → 8초에 4개 (뮤트 60분)" in out and "도배(뮤트) 1" in out, out
    assert "도배 뮤트 중이라 못 보냈을 글 2" in out and "3개·1명" in out and "뮤트 끝난 뒤" not in out, out
    out, _ = await sim(svc, bot, {"change": "flood", "value": "3/8"})
    assert "8초에 3개" in out and "도배(뮤트) 1" in out
    out, _ = await sim(svc, bot, {"change": "flood", "value": "느슨"})
    assert "새 설정이면 0개·0명" in out and "예시" not in out


@test
async def simulate_huge_room_scans_recent_only():
    db, svc, bot, now = await sim_world(link_filter=False)
    old = replay.MAX_SCAN
    replay.MAX_SCAN = 1000
    try:
        rows = [(Room.CHAT, 5000 + i % 300, i, f"글 {i} spam{i % 7}.xyz", now - 5000 + i, 0, 0) for i in range(1500)]
        await db.atomic(lambda c: c.executemany(
            "INSERT INTO messages(chat_id, user_id, msg_id, text, ts, is_bot, flagged) VALUES(?,?,?,?,?,?,?)", rows))
        out, _ = await sim(svc, bot, {"change": "link_filter", "value": "on", "hours": 9999})
        assert "최근 168시간 메시지 1000개 확인" in out and "최근 1,000개만 봄" in out and "1000개·300명 걸림" in out, out
        assert out.count("\n- ") == replay.EXAMPLES and len(out) < 3000
    finally:
        replay.MAX_SCAN = old


# ── moderation 과 replay 판정이 같은지 (드리프트 방지) ───────
@test
async def replay_matches_moderation_decisions():
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id})
    bot = FakeBot(admins=[BOSS])
    s = {"flood_count": 4, "flood_seconds": 8, "dup_limit": 3, "link_filter": False, "newbie_link_hours": 24,
         "whitelist_domains": ["youtube.com"], "warn_mute_at": 100, "warn_ban_at": 100}
    for k, v in s.items():
        await db.set_setting(Room.CHAT, k, v)
    await db.set_banned_word(Room.CHAT, "먹튀", True)
    now = int(time.time())
    OLD, NEW, FL = fake_user(40, "오래된"), fake_user(41, "신입"), fake_user(42, "도배꾼")
    for u, joined in ((OLD, now - 50 * DAY), (NEW, now - HOUR), (FL, now - 50 * DAY)):
        await add_member(db, Room.CHAT, u)
        await db._write("UPDATE members SET joined_at=? WHERE chat_id=? AND user_id=?", (joined, Room.CHAT, u.id))
    script = [(OLD, "안녕하세요", 0), (OLD, "https://evil.xyz 보세요", 2), (NEW, "https://evil.xyz", 3),
              (NEW, "youtube.com/watch?v=1", 4), (NEW, "YOUTUBE.com 과 evil.xyz 같이", 5), (OLD, "먹튀 조심", 6),
              (OLD, "같은말", 20), (OLD, "같은말", 23), (OLD, "같은  말", 26), (OLD, "같은말", 29), (OLD, "같은말", 32),
              (OLD, "같은말", 35), (NEW, "\u200b", 36), (NEW, "!홀짝 1000 홀", 37), (NEW, "!홀짝 1000 홀", 37),
              (NEW, "!홀짝 1000 홀", 38), (NEW, "t.me/joinme", 40)]
    script += [(FL, f"도배 {i}", 60 + i) for i in range(5)] + [(FL, "먹튀 도배 끝", 75), (FL, "조용", 90)]
    got, rows = {}, []
    for i, (u, text, dt) in enumerate(script):
        ts = now - 200 + dt
        m = FakeMsg(Room.CHAT, u, text, message_id=100 + i)
        m.date = datetime.fromtimestamp(ts, timezone.utc)
        n0 = (await db._one("SELECT COUNT(*) AS n FROM mod_log"))["n"]
        await svc.mod.check_message(bot, m, text)
        new = await db._all("SELECT action, detail FROM mod_log ORDER BY id LIMIT -1 OFFSET ?", (n0,))
        if new:
            a, d = new[0]["action"], new[0]["detail"] or ""
            got[i] = {"mute": "flood" if d.endswith("도배") else "?", "link_del": "link",
                      "warn": {"금지어 사용": "banned", "같은 메시지 반복": "dup"}.get(d, "?")}.get(a, "?")
        rows.append({"user_id": u.id, "ts": ts, "text": text})
    joined = {r["user_id"]: r["joined_at"] for r in await db._all("SELECT user_id, joined_at FROM members")}
    want = replay.replay(rows, await db.get_settings(Room.CHAT), await db.banned_words(Room.CHAT), joined,
                         mute_blocks=False)
    assert got == want, (got, want)
    assert sorted(set(got.values())) == ["banned", "dup", "flood", "link"], got   # 네 가지 판정이 다 나옴
    # 다시 들어온 사람: 옛 글(입장 전)은 신규 규칙으로 안 셈
    again = replay.replay([{"user_id": 9, "ts": now - DAY, "text": "evil.xyz"}], {**s, "link_filter": False},
                          [], {9: now - HOUR})
    assert again == {}


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
