"""🧹 멤버 정리 (sodam/cleanup.py · panels/cleanup.py · commands.c_cleanup · diag /v1/cleanup): python tests/run_all.py member_cleanup

계기(2026-09-30): 세컨드방 되살리기 — 오너 '가라·탈퇴 계정·2주/1달 미접속 한꺼번에 내보내기'. 설계 docs/MEMBER_CLEANUP.md.
가짜 Telethon(seed_mtproto.FakeClient)에 실제 User.status 타입(telethon.tl.types)을 넣어 돌린다. 네트워크 없음.
검사: 분류 · 보호(14일 글·관리자·오너·제외·자유) · '모름'≠잠수 · LastMonth≠잠수30 · 스캔 10분·partial · 확인 카드(만든 사람만·한 번만)
· 실행 전 재확인(나감·관리자·새 제외) · 방마다 작업 1개 · 중지·이어서·재시작 이어하기 · RetryAfter · 권한 빠짐 · 서비스 메시지 삭제
· 작별 인사 건너뜀 · 방에 명단 안 뿌림 · 이름 esc · 원격 점검 숫자만.
"""
import asyncio
import dataclasses
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from seed_mtproto import factory
from telegram.error import BadRequest, RetryAfter, TimedOut
from telethon.errors import FloodWaitError
from telethon.tl.types import UserStatusEmpty, UserStatusLastMonth, UserStatusOffline, UserStatusOnline, UserStatusRecently

from sodam import cleanup, commands, handlers, menu, mtproto, tools
from sodam.commands import CmdCtx
from sodam.panels import cleanup as panel  # noqa: F401  화면·토큰 등록

test, run_all = runner()

CH = -1003962672437
OWNER, BOSS, SUB = fake_user(1, "오너"), fake_user(2, "방장"), fake_user(3, "부방장")
DAY = 86400
mtproto.MIN_GAP = mtproto.PAGE_GAP = 0
cleanup.KICK_GAP = 0
SLEEPS: list = []


async def _no_sleep(s):
    SLEEPS.append(s)


cleanup._sleep = _no_sleep


def off(days: float):
    return UserStatusOffline(was_online=datetime.fromtimestamp(time.time() - days * DAY, timezone.utc))


PHOTO = SimpleNamespace(photo_id=1)   # UserProfilePhoto 흉내 (이름이 Empty 가 아니면 '있음')

# (id, 이름, 아이디[, 봇]) + 속성
PEOPLE = {
    10: ((10, None, None), {"deleted": True}),                                   # 🪦 탈퇴
    11: ((11, "다른봇", "other_bot", True), {"status": None}),                    # 🤖
    12: ((12, "잠수40", "idle40"), {"status": off(40), "photo": PHOTO}),        # 잠수 14·30
    13: ((13, "잠수20", "idle20"), {"status": off(20), "photo": PHOTO}),        # 잠수 14 만
    14: ((14, "모름", "unknown14"), {"status": UserStatusEmpty(), "photo": PHOTO}),  # ❔ (잠수 아님!)
    15: ((15, "한달안", "month15"), {"status": UserStatusLastMonth(), "photo": PHOTO}),  # 대략 — 잠수30 아님
    16: ((16, "최근글", "wrote16"), {"status": off(40), "photo": PHOTO}),       # 3일 전 글 → 보호
    17: ((17, "제외", "excl17"), {"status": off(40), "photo": PHOTO}),          # 제외 명단 → 보호
    18: ((18, "자유", "free18"), {"status": off(40), "photo": PHOTO}),          # 자유 멤버 → 보호
    19: ((19, "활동", "act19"), {"status": off(40), "photo": PHOTO}),           # 소담 기록상 10일 전 활동 → 잠수 아님
    2: ((2, "방장", "boss"), {"status": off(40)}),                               # 관리자 → 보호
    1: ((1, "오너", "owner"), {"status": off(40)}),                              # 오너 → 보호
    999: ((999, "소담", "sodambot", True), {}),                                  # 소담 자신 → 보호
    8_600_000_001: ((8_600_000_001, "가라", None), {"status": UserStatusRecently()}),   # 👻 신호 5개
    21: ((21, "<b>나쁜</b>&이름이아주아주아주아주길어요정말로요", "online21"), {"status": UserStatusOnline(expires=0), "photo": PHOTO}),
}


async def world(people=PEOPLE, **kw):
    cleanup._TASKS.clear()
    cleanup._leaving.clear()
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id, SUB.id}, telegram_token="999:AA", mtproto_api_id=1, mtproto_api_hash="h", **kw)
    svc.perms.owner_ids = {OWNER.id}
    await db.ensure_chat(CH, "𝕊𝔼ℂ𝕆ℕ𝔻 세컨드방")
    mt = mtproto.MTProto(svc.cfg, db, factory=factory(members=[m for m, _ in people.values()],
                                                      extra={uid: attrs for uid, (_, attrs) in people.items()}))
    await mt.start()
    svc.mtproto = mt
    bot = FakeBot(admins=[BOSS, SUB])
    now = time.time()
    await db.log_message(CH, 16, 1, "안녕", ts=int(now - 3 * DAY))        # 최근 14일 글
    await db.log_message(CH, 12, 2, "옛날 글", ts=int(now - 60 * DAY))    # 기록 시작 = 60일 전
    await db.upsert_user(fake_user(19, "활동"))
    await db.touch_member(CH, 19)
    await db._write("UPDATE members SET last_seen=? WHERE chat_id=? AND user_id=19", (int(now - 10 * DAY), CH))
    await cleanup.exclude(db, CH, 17, BOSS.id, True)
    from sodam import free
    await free.add(db, CH, 18, BOSS.id)
    return svc, mt, bot


def ids(rows):
    return {r["user_id"] for r in rows}


async def wait_job(svc):
    t = cleanup._TASKS.get((svc.db.path, CH))
    if t is not None:
        await asyncio.wait_for(asyncio.shield(t), 10)


def dms(bot, uid):
    return [c for c in bot.named("send_message") if c[1] == uid]


def last_dm(bot, uid):
    """관리자 1:1 에 마지막으로 보이는 글 (새로 보냄 또는 진행 메시지 고침) → (글, 버튼)."""
    got = [(c[2], c[3].get("reply_markup")) for c in bot.calls
           if c[0] in ("send_message", "edit_text") and c[1] == uid]
    return got[-1]


def room_texts(bot):
    return " ".join(c[2] for c in bot.named("send_message") if c[1] == CH)


async def run_cmd(svc, bot, user, text, chat=CH, reply=None):
    msg = FakeMsg(chat, user, text, reply_to=reply)
    cmd, args, argstr = commands.parse(text, bot.username)
    role = await svc.perms.role(bot, chat, user.id)
    await commands.dispatch(CmdCtx(svc, bot, msg, chat, user, role, args, argstr), cmd)
    return msg


async def press(svc, bot, user, data):
    q = FakeQuery(user.id, user, data)
    svc.menu_limiter._hits.clear()
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


def buttons(kb):
    return {b.text: b.callback_data for row in (kb.inline_keyboard if kb else ()) for b in row}


# ── 1. 분류 · 보호 ────────────────────────────────────────
@test
async def classify_and_protect():
    svc, mt, bot = await world()
    s, err = await cleanup.scan(svc, bot, CH, BOSS.id)
    assert s and not err and not s["reused"] and s["total"] == len(PEOPLE) and not s["partial"], (s, err)
    now = time.time()
    got = {t: ids(await cleanup.selection(svc, CH, [t], now)) for t in ("d", "b", "i14", "i30", "u", "f")}
    assert got["d"] == {10} and got["b"] == {11}, got
    assert got["i30"] == {12}, got
    assert got["i14"] == {12, 13}, got
    assert got["u"] == {14}, got
    assert 8_600_000_001 in got["f"] and 12 not in got["f"], got
    everyone = set().union(*got.values())
    for uid in (16, 17, 18, 2, 1, 999):
        assert uid not in everyone, f"보호 대상 {uid} 가 후보에 들어감"
    assert 19 not in everyone, "스티커·사진 같은 활동(members.last_seen)도 14일 보호"
    assert s["counts"] == {"d": 1, "b": 1, "i14": 2, "i30": 1, "u": 1, "f": len(got["f"])}, s["counts"]
    assert s["prot"]["최근 14일 활동"] == 2 and s["prot"]["제외 명단"] == 1 and s["prot"]["자유 멤버"] == 1, s["prot"]
    assert s["prot"]["관리자·오너·봇"] == 3, s["prot"]
    assert s["dist"]["offline"] == 8 and s["dist"]["unknown"] >= 3 and s["dist"]["last_month"] == 1, s["dist"]
    assert s["rec_since"] and s["rec_since"] < now - 59 * DAY
    # 스캔 뒤 제외·자유 멤버로 바뀐 사람은 미리보기에서도 빠짐
    await cleanup.exclude(svc.db, CH, 12, BOSS.id, True)
    assert 12 not in ids(await cleanup.selection(svc, CH, ["i30", "i14"]))


@test
def unknown_and_rough_status_are_never_idle():
    now = time.time()
    base = {"deleted": 0, "is_bot": 0, "msgs": 0, "photo": 0, "username": "", "recent": 0, "burst": 0, "last_act": None}
    for kind in ("unknown", "recently", "last_week", "last_month", "online"):
        r = {**base, "status": kind, "was_online": None}
        assert not any(cleanup.idle(r, d, now) for d in (1, 14, 30, 365)), kind
        assert cleanup.cats_of(r, ["i1", "i30"], now) == [], kind
    r = {**base, "status": "offline", "was_online": int(now - 31 * DAY)}
    assert cleanup.idle(r, 30, now) and not cleanup.idle({**r, "last_act": int(now - 2 * DAY)}, 30, now)
    assert not cleanup.idle({**r, "was_online": None}, 1, now), "정확한 시각이 없으면 잠수 아님"
    assert cleanup.cats_of({**r, "status": "unknown", "was_online": None}, ["u", "i1"], now) == ["u"]
    # 탈퇴·봇은 그 분류만 (잠수·가라로 두 번 안 셈)
    assert cleanup.cats_of({**r, "deleted": 1}, ["d", "i30", "f"], now) == ["d"]
    assert cleanup.cats_of({**r, "is_bot": 1}, ["i30"], now) == []
    # 가라 의심 = 신호 3개 이상
    assert len(cleanup.fake_signals({**base, "status": "offline", "photo": 1, "username": "a"})) == 1
    assert cleanup.cats_of({**base, "status": "recently", "was_online": None}, ["f"], now) == ["f"]
    # 분류 문자열: 모르는 조각·범위 밖 잠수 버림, 잠수는 하나만
    assert cleanup.parse_sel("d.x.i0.i400.i30.i14.b.d") == ["d", "i30", "b"]
    assert cleanup.parse_sel("") == [] and cleanup.parse_sel("-") == []


# ── 2. 스캔: 10분 1번 · partial · MTProto 없음·실패 ────────
@test
async def scan_rate_limit_partial_and_failures():
    svc, mt, bot = await world()
    s1, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    pages = len([c for c in mt.bot.client.calls if c[0] == "page"])
    s2, _ = await cleanup.scan(svc, bot, CH, BOSS.id, force=True)
    assert s2["reused"] and len([c for c in mt.bot.client.calls if c[0] == "page"]) == pages, "10분 안엔 다시 안 물어봄"
    await svc.db._write("UPDATE cleanup_scans SET ts=ts-? WHERE chat_id=?", (cleanup.SCAN_GAP + 1, CH))
    old = cleanup.SCAN_LIMIT
    cleanup.SCAN_LIMIT = 5
    try:
        s3, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    finally:
        cleanup.SCAN_LIMIT = old
    assert s3["partial"] and s3["total"] == 5 and not s3["reused"]
    screen = await panel.summary(svc, bot, CH)
    assert "1만 명까지만" in screen.text
    # FloodWait 길면 스캔 실패 (봇은 안 죽음, 오류 글)
    await svc.db._write("DELETE FROM cleanup_scans")
    mt.bot.client.raises.append(FloodWaitError(None, capture=mtproto.MAX_FLOOD_SLEEP + 5))
    s4, err = await cleanup.scan(svc, bot, CH, BOSS.id)
    assert s4 is None and "못 받았어요" in err
    # MTProto 꺼짐
    svc.mtproto = None
    s5, err = await cleanup.scan(svc, bot, CH, BOSS.id)
    assert s5 is None and "MTProto" in err
    await mt.stop()


# ── 3. 명령 → 1:1 미리보기 · 방엔 명단 없음 · 이름 esc ──────
@test
async def command_sends_everything_to_dm_only():
    svc, mt, bot = await world()
    msg = await run_cmd(svc, bot, BOSS, ".멤버정리 잠수 30")
    assert msg.deleted, "방 명령은 지움"
    dm = dms(bot, BOSS.id)
    assert dm and "미리보기" in dm[-1][2] and "잠수 30일" in dm[-1][2] and "잠수40" in dm[-1][2], dm
    assert "잠수40" not in room_texts(bot) and "1:1" in room_texts(bot), "방엔 '1:1 확인'만"
    assert "방 인원 약" in dm[-1][2]
    await run_cmd(svc, bot, BOSS, ".멤버정리 모름")
    txt = dms(bot, BOSS.id)[-1][2]
    assert "모름" in txt and "unknown14" in txt, txt
    # 요약: 숫자만 + 분류 버튼
    await run_cmd(svc, bot, BOSS, ".멤버정리")
    txt, kb = dms(bot, BOSS.id)[-1][2], dms(bot, BOSS.id)[-1][3]["reply_markup"]
    assert "10분 안에 스캔한" in txt and "접속 상태: 정확" in txt and "보호" in txt and "잠수40" not in txt, txt
    assert "60" not in txt or "부터" in txt
    assert any(v.startswith(f"m:mcp:{CH}:") for v in buttons(kb).values())
    # 이름은 esc + 길이 자름 (남이 정한 글)
    q = await press(svc, bot, BOSS, f"m:mcp:{CH}:i1.u")   # 21 은 접속 중이라 안 걸림 → 가라로 보기
    await svc.db._write("UPDATE cleanup_cands SET status='unknown' WHERE chat_id=? AND user_id=21", (CH,))
    q = await press(svc, bot, BOSS, f"m:mcp:{CH}:u")
    body = q.edits[-1]
    assert "&lt;b&gt;나쁜&lt;/b&gt;&amp;" in body and "<b>나쁜" not in body and "정말로요" not in body, body
    # 일반 멤버·권한 없는 사람은 명령·화면 못 씀
    m2 = await run_cmd(svc, bot, fake_user(50, "멤버"), ".멤버정리")
    assert m2.replies and "관리자만" in m2.replies[-1]
    svc.perms.admins.discard(SUB.id)
    q = await press(svc, bot, SUB, f"m:mc:{CH}")
    assert not q.edits and q.answers[-1][1]
    await mt.stop()


# ── 4. 확인 카드: 만든 사람만 · 한 번만 · 실행 전 재확인 ────
async def card_tokens(svc, bot, user, sel="d.b.i30"):
    q = await press(svc, bot, user, f"m:mck:{CH}:{sel}")
    card = dms(bot, user.id)[-1]
    assert "정말 내보낼까요" in card[2] and "방 인원 약" in card[2] and "10분" in card[2], (q.answers, card[2])
    b = buttons(card[3]["reply_markup"])
    ok = next(v for k, v in b.items() if "내보내기" in k)
    no = next(v for k, v in b.items() if "취소" in k)
    return ok, no


@test
async def card_only_creator_once_and_rechecks_before_each_kick():
    svc, mt, bot = await world()
    await cleanup.scan(svc, bot, CH, BOSS.id)
    ok, no = await card_tokens(svc, bot, BOSS, "d.b.i14")
    assert not [c for c in bot.named("send_message") if c[1] == CH], "카드는 방에 안 올림"
    # 남이 누르면 거절 · 토큰은 그대로 (주인이 누를 수 있게)
    for other in (SUB, OWNER):
        q = await press(svc, bot, other, ok)
        assert "요청한 사람만" in q.answers[-1][0] and not q.edits, q.answers
    assert not bot.named("ban")
    # 미리보기 뒤 실행 전 명단이 바뀜: 12 는 이미 나감, 11(봇)은 관리자가 됨, 10 은 제외 명단에, 13 은 방금 글을 씀
    bot.member_status = {(CH, 12): "left", (CH, 11): "administrator"}
    await svc.db.log_message(CH, 13, 9, "저 살아 있어요")
    await cleanup.exclude(svc.db, CH, 10, BOSS.id, True)
    await svc.db._write("DELETE FROM cleanup_excl WHERE user_id=10")   # 스캔 뒤 명단 → 실행 땐 다시 넣음
    q = await press(svc, bot, BOSS, ok)
    assert q.edits and "시작했어요" in q.edits[-1], (q.answers, q.edits)
    await cleanup.exclude(svc.db, CH, 10, BOSS.id, True)
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "done" and job["total"] == 4, job
    assert job["gone"] == 1 and job["skipped"] == 3 and job["kicked"] == 0, job
    assert not bot.named("ban"), "나감·관리자·새 제외는 안 내보냄"
    # 다시 누르기 = 만료 · 취소 버튼도 이미 처리됨
    q = await press(svc, bot, BOSS, ok)
    assert q.answers[-1][1] and not q.edits
    q = await press(svc, bot, BOSS, no)
    assert q.answers[-1][1] and not q.edits
    rep = last_dm(bot, BOSS.id)[0]
    assert "멤버 정리 끝" in rep and "이미 나감 1" in rep and "건너뜀(보호·관리자) 3" in rep, rep
    await mt.stop()


@test
async def kicks_are_ban_then_unban_and_logged():
    svc, mt, bot = await world()
    await cleanup.scan(svc, bot, CH, BOSS.id)
    ok, _ = await card_tokens(svc, bot, BOSS, "d.i14")
    await press(svc, bot, BOSS, ok)
    await wait_job(svc)
    kicked = [c[2] for c in bot.named("ban")]
    assert sorted(kicked) == [10, 12, 13] and sorted(c[2] for c in bot.named("unban")) == [10, 12, 13], bot.calls
    order = [c[0] for c in bot.calls if c[0] in ("ban", "unban", "get_chat_member") and c[2] == 12]
    assert order == ["get_chat_member", "ban", "unban"], order
    logs = await svc.db._all("SELECT target_id, action, detail FROM mod_log WHERE chat_id=? AND action='kick'", (CH,))
    assert {r["target_id"] for r in logs} == {10, 12, 13} and all(r["detail"].startswith("멤버 정리:") for r in logs)
    assert any("탈퇴 계정" in r["detail"] for r in logs) and any("잠수 14일" in r["detail"] for r in logs)
    summary = await svc.db._all("SELECT detail FROM mod_log WHERE chat_id=? AND action='member_cleanup'", (CH,))
    assert any(r["detail"].startswith("끝: 내보냄 3") for r in summary), summary
    assert not await svc.db._all("SELECT 1 FROM cleanup_cands WHERE chat_id=? AND user_id IN (10,12,13)", (CH,)), \
        "내보낸 사람은 미리보기 후보에서 빠짐"
    assert "잠수40" not in room_texts(bot), "방엔 명단을 안 뿌림"
    await mt.stop()


@test
async def start_job_rechecks_presser_bot_and_scan_age():
    svc, mt, bot = await world()
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    svc.perms.admins.discard(SUB.id)
    ok, text = await cleanup.start_job(svc, bot, CH, SUB.id, ["d"], s["ts"])
    assert not ok and "권한" in text, "누른 사람이 '사용자 차단' 권한을 잃으면 거절"
    bot.can_moderate = False
    ok, text = await cleanup.start_job(svc, bot, CH, BOSS.id, ["d"], s["ts"])
    assert not ok and "소담에게" in text
    bot.can_moderate = True
    ok, text = await cleanup.start_job(svc, bot, CH, BOSS.id, ["d"], s["ts"] - 1)
    assert not ok and "스캔" in text, "카드를 만든 뒤 다시 스캔했으면 그 카드는 무효"
    await svc.db._write("UPDATE cleanup_scans SET ts=ts-? WHERE chat_id=?", (cleanup.SCAN_VALID + 1, CH))
    ok, text = await cleanup.start_job(svc, bot, CH, BOSS.id, ["d"], s["ts"] - cleanup.SCAN_VALID - 1)
    assert not ok and "30분" in text
    assert not bot.named("ban")
    await mt.stop()


@test
async def only_one_job_per_room():
    svc, mt, bot = await world()
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    ok1, _ = await cleanup.start_job(svc, bot, CH, BOSS.id, ["d", "i14"], s["ts"])
    ok2, text = await cleanup.start_job(svc, bot, CH, SUB.id, ["b"], s["ts"])   # 다른 관리자가 동시에
    assert ok1 and not ok2 and "이미" in text, text
    sent, why = await panel.send_card(svc, bot, CH, SUB.id, ["b"])
    assert not sent and "진행 중" in why
    await wait_job(svc)
    assert sorted(c[2] for c in bot.named("ban")) == [10, 12, 13], "두 번째 작업의 봇(11)은 안 내보냄"
    await mt.stop()


@test
async def many_people_progress_edits_are_throttled():
    people = {1000 + i: ((1000 + i, None, None), {"deleted": True}) for i in range(55)}
    svc, mt, bot = await world(people)
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    assert s["counts"]["d"] == 55
    old = cleanup.PROGRESS_MIN_SEC
    cleanup.PROGRESS_MIN_SEC = 0
    try:
        await cleanup.start_job(svc, bot, CH, BOSS.id, ["d"], s["ts"])
        await wait_job(svc)
    finally:
        cleanup.PROGRESS_MIN_SEC = old
    edits = [c for c in bot.calls if c[0] == "edit_text" and c[1] == BOSS.id]
    assert len(bot.named("ban")) == 55 and len(edits) == 55 // cleanup.PROGRESS_EVERY + 1, len(edits)   # 20명마다 + 끝 보고
    assert len(dms(bot, BOSS.id)) == 1, "진행은 한 메시지를 고침 (새 메시지 폭탄 없음)"
    checks = [c for c in bot.calls if c[0] == "get_chat_member" and c[2] == bot.id]
    assert len(checks) >= 55 // cleanup.RIGHTS_EVERY + 1, "봇 권한은 20명마다 다시 확인"
    await mt.stop()


# ── 5. 중지 · 이어서 · 재시작 이어하기 ───────────────────────
@test
async def stop_resume_and_restart_continue():
    svc, mt, bot = await world()
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    ok, _ = await cleanup.start_job(svc, bot, CH, BOSS.id, ["d", "b", "i14"], s["ts"])
    assert await cleanup.stop_job(svc, CH, BOSS.id)          # 작업이 첫 사람 전에 멈춤
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "stopped" and await cleanup.remaining(svc.db, CH) == 4 and not bot.named("ban"), job
    assert "멈춤" in last_dm(bot, BOSS.id)[0] and "m:mcg:" in str(last_dm(bot, BOSS.id)[1])
    assert not await cleanup.stop_job(svc, CH, BOSS.id), "이미 멈춘 건 또 못 멈춤"
    # [▶️ 이어서] (권한 다시 확인)
    q = await press(svc, bot, BOSS, f"m:mcg:{CH}")
    assert "이어서" in q.answers[-1][0], q.answers
    # 첫 사람 뒤 '재시작' 흉내: 작업 태스크를 죽임 → DB 는 running 그대로
    task = cleanup._TASKS[(svc.db.path, CH)]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    cleanup._TASKS.clear()
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "running" and await cleanup.remaining(svc.db, CH) >= 1
    # 틱: 방금 소식이 있었으면(다른 프로세스가 돌 수도) 안 건드림 → LEASE 지나면 이어 받음
    assert await cleanup.resume_all(svc, bot, stale_only=True) == 0
    await svc.db._write("UPDATE cleanup_jobs SET beat=0 WHERE chat_id=?", (CH,))
    assert await cleanup.resume_all(svc, bot, stale_only=True) == 1
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "done" and sorted({c[2] for c in bot.named("ban")}) == [10, 11, 12, 13], (job, bot.named("ban"))
    # 시작 때 이어하기(post_init): 진행 중 작업은 바로 (딜러 봇은 안 함)
    await svc.db._write("UPDATE cleanup_jobs SET state='running' WHERE chat_id=?", (CH,))
    await svc.db._write("INSERT INTO cleanup_queue(chat_id, user_id, pos, reason) VALUES(?,?,?,?)", (CH, 15, 0, "잠수 30일"))
    dealer = dataclasses.replace(svc.cfg, bot_role="dealer")
    real = svc.cfg
    svc.cfg = dealer
    assert await cleanup.resume_all(svc, bot) == 0
    svc.cfg = real
    assert await cleanup.resume_all(svc, bot) == 1
    await wait_job(svc)
    assert 15 in [c[2] for c in bot.named("ban")]
    await mt.stop()


# ── 6. RetryAfter · 권한 빠짐 · 알 수 없는 실패 ─────────────
class SlowBot(FakeBot):
    def __init__(self, *a, waits=(), ban_error=None, **kw):
        super().__init__(*a, **kw)
        self.waits, self.ban_error = list(waits), ban_error

    async def ban_chat_member(self, chat_id, user_id, **kw):
        if self.waits:
            raise RetryAfter(self.waits.pop(0))
        if self.ban_error:
            raise self.ban_error
        await super().ban_chat_member(chat_id, user_id, **kw)


@test
async def retry_after_waits_then_halts_when_too_long():
    svc, mt, _ = await world()
    bot = SlowBot(admins=[BOSS, SUB], waits=[7])
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    SLEEPS.clear()
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["d"], s["ts"])
    await wait_job(svc)
    assert 7.5 in SLEEPS and [c[2] for c in bot.named("ban")] == [10], (SLEEPS, bot.calls)
    # 너무 긴 대기 → 멈춤 (무한 재시도 없음), 그 사람은 남은 명단에 그대로
    await svc.db._write("UPDATE cleanup_scans SET ts=ts-1 WHERE chat_id=?", (CH,))
    s = await cleanup.last_scan(svc.db, CH)
    bot.waits = [cleanup.RETRY_MAX_WAIT + 100]
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["b"], s["ts"])
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "halted" and "속도 제한" in job["why"] and await cleanup.remaining(svc.db, CH) == 1, job
    assert "멈춤" in last_dm(bot, BOSS.id)[0]
    # 같은 사람에게 RetryAfter 가 계속 → RETRY_MAX 번 뒤 멈춤
    bot.waits = [1] * (cleanup.RETRY_MAX + 1)
    ok, _ = await cleanup.resume_job(svc, bot, CH, BOSS.id)
    await wait_job(svc)
    assert ok and (await cleanup.get_job(svc.db, CH))["state"] == "halted"
    await mt.stop()


@test
async def bot_losing_ban_right_halts_and_notifies():
    svc, mt, _ = await world()
    bot = SlowBot(admins=[BOSS, SUB], ban_error=BadRequest("Not enough rights to restrict/ban chat member"))
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["d", "i14"], s["ts"])
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "halted" and "권한" in job["why"] and await cleanup.remaining(svc.db, CH) == 3, job
    assert "m:mcg:" in str(last_dm(bot, BOSS.id)[1]), "[▶️ 이어서] 버튼"
    # 도중에 봇 권한이 빠진 걸 확인 (RIGHTS_EVERY 마다 get_chat_member 로)
    bot.ban_error, bot.can_moderate = None, False
    ok, text = await cleanup.resume_job(svc, bot, CH, BOSS.id)
    assert not ok and "권한" in text, "권한 없으면 이어서도 안 됨"
    await svc.db._write("UPDATE cleanup_jobs SET state='running' WHERE chat_id=?", (CH,))
    cleanup.launch(svc, bot, CH)
    await wait_job(svc)
    assert (await cleanup.get_job(svc.db, CH))["state"] == "halted" and not bot.named("ban")
    # 다른 실패(사람 한 명)는 세고 계속
    bot.can_moderate, bot.ban_error = True, BadRequest("User_not_participant")
    ok, _ = await cleanup.resume_job(svc, bot, CH, BOSS.id)
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert ok and job["state"] == "done" and job["failed"] == 3 and job["fails"] == {"내보내기 실패": 3}, job
    assert sorted(job["fail_ids"]) == [10, 12, 13], "실패한 사람 ID 는 이유와 따로 목록으로"
    assert not await svc.db._all("SELECT 1 FROM cleanup_banning"), "밴이 거절되면 '밴 중' 표시도 지움"
    assert not any(cleanup._leaving), "밴이 안 됐으면 작업 퇴장 기록도 없음 (서비스 메시지 지우기 대상 아님)"
    await mt.stop()


# ── 7. 작업이 낸 퇴장: 서비스 메시지 삭제 · 작별 인사 건너뜀 ──
@test
async def job_leave_deletes_service_message_and_skips_farewell():
    svc, mt, bot = await world()
    await svc.db.set_setting(CH, "farewell_mode", "on")
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["i14"], s["ts"])
    await wait_job(svc)
    botuser = fake_user(bot.id, "소담", "sodambot", is_bot=True)
    deleted = []

    class Svc(SimpleNamespace):
        async def delete(self):
            deleted.append(self.left_chat_member.id)
    for uid in (12, 13):
        await handlers.on_left(SimpleNamespace(message=Svc(chat_id=CH, left_chat_member=fake_user(uid, "x"),
                                                           from_user=botuser)), ctx)
    assert deleted == [12, 13], deleted
    # 상태 변경(chat_member) 경로도 인사 없음
    def m(st, uid):
        return SimpleNamespace(status=st, is_member=None, user=fake_user(uid, "x"))
    upd = SimpleNamespace(chat_member=SimpleNamespace(chat=SimpleNamespace(id=CH, type="supergroup", title="방"),
                                                      old_chat_member=m("member", 12), new_chat_member=m("left", 12),
                                                      from_user=fake_user(12, "x")))   # 한 사람 = 본인처럼 보여도
    await handlers.on_chat_member(upd, ctx)
    await asyncio.sleep(0)
    assert not [c for c in bot.named("send_message") if c[1] == CH], "작업이 낸 퇴장엔 작별 인사 없음"
    # 작업과 상관없이 스스로 나간 사람은 그대로 인사 + 서비스 메시지 안 지움
    cleanup._leaving.clear()
    await svc.db._write("UPDATE cleanup_jobs SET state='done'")
    u = fake_user(70, "떠난이")
    await handlers.on_left(SimpleNamespace(message=Svc(chat_id=CH, left_chat_member=u, from_user=u)), ctx)
    for _ in range(3):
        await asyncio.sleep(0)
    assert deleted == [12, 13] and [c for c in bot.named("send_message") if c[1] == CH], "보통 퇴장은 인사"
    # 재시작 직후(기록 없음): 소담이 내보냈고 그 사람에게 '밴 중' 표시가 있을 때만 작업 퇴장
    await svc.db._write("UPDATE cleanup_jobs SET state='running'")
    assert not await cleanup.job_leave(svc, CH, 71, bot.id, bot.id), "작업 중이라도 작업이 안 내보낸 사람(스팸 등)은 아님"
    await svc.db._write("INSERT INTO cleanup_banning(chat_id, user_id, stage, ts) VALUES(?,?,?,?)", (CH, 71, "banning", 1))
    assert await cleanup.job_leave(svc, CH, 71, bot.id, bot.id)
    assert not await cleanup.job_leave(svc, CH, 71, 71, bot.id), "스스로 나간 건 아님"
    await svc.db._write("UPDATE cleanup_jobs SET state='done'")
    await mt.stop()


# ── 8. 오너 1:1 (다른 방) · 제외 명단 명령 · CSV ────────────
@test
async def owner_dm_room_argument_exclusions_and_csv():
    svc, mt, bot = await world()
    await svc.db.ensure_chat(-100555, "첫째방")
    m = await run_cmd(svc, bot, OWNER, ".멤버정리 탈퇴 세컨드", chat=OWNER.id)
    assert not m.replies and "미리보기" in dms(bot, OWNER.id)[-1][2] and "탈퇴 계정" in dms(bot, OWNER.id)[-1][2]
    m = await run_cmd(svc, bot, OWNER, ".멤버정리 없는방", chat=OWNER.id)
    assert "못 찾았어요" in m.replies[-1]
    m = await run_cmd(svc, bot, OWNER, ".멤버정리", chat=OWNER.id)
    assert "방 이름" in m.replies[-1]
    m = await run_cmd(svc, bot, BOSS, ".멤버정리 세컨드", chat=BOSS.id)
    assert "관리자만" in m.replies[-1], "오너가 아닌 사람은 1:1 에서 못 씀"
    # 제외 명단 (방)
    await run_cmd(svc, bot, BOSS, ".멤버정리 제외 13")
    assert 13 in await cleanup.excluded_ids(svc.db, CH)
    reply = FakeMsg(CH, fake_user(12, "잠수40", "idle40"), "글")
    await run_cmd(svc, bot, BOSS, ".멤버정리 제외", reply=reply)
    assert 12 in await cleanup.excluded_ids(svc.db, CH)
    await run_cmd(svc, bot, BOSS, ".멤버정리 제외해제 13")
    assert 13 not in await cleanup.excluded_ids(svc.db, CH)
    await run_cmd(svc, bot, BOSS, ".멤버정리 제외목록")
    assert "제외 목록" in dms(bot, BOSS.id)[-1][2] and "잠수40" in dms(bot, BOSS.id)[-1][2]
    tok = next(v for k, v in buttons(dms(bot, BOSS.id)[-1][3]["reply_markup"]).items() if k.startswith("❌"))
    q = await press(svc, bot, BOSS, tok)
    assert "뺐어요" in q.answers[-1][0]
    # CSV: 1:1 로만, 1분 1번, 수식 주입 막기
    await svc.db._write("UPDATE cleanup_cands SET name='=cmd()' WHERE user_id=12")
    q = await press(svc, bot, BOSS, f"m:mcx:{CH}:i14")
    docs = bot.named("send_document")
    assert docs and docs[-1][1] == BOSS.id, "CSV 는 누른 관리자 1:1"
    q = await press(svc, bot, BOSS, f"m:mcx:{CH}:i14")
    assert "1분" in q.answers[-1][0]
    assert not [c for c in bot.named("send_document") if c[1] == CH]
    # 중지 명령
    m = await run_cmd(svc, bot, BOSS, ".멤버정리 중지")
    assert "진행 중인" in room_texts(bot)
    await mt.stop()


# ── 9. AI 도구: 읽기 전용 요약 · 내보내기는 카드까지만 ────────
@test
async def ai_tools_read_only_and_card_only():
    svc, mt, bot = await world()
    assert {"member_profile", "member_cleanup_status"} <= tools.READ_ONLY and "member_cleanup" not in tools.READ_ONLY
    ctx = tools.ToolCtx(svc, bot, CH, BOSS, await svc.perms.role(bot, CH, BOSS.id), await svc.db.get_settings(CH))
    out = await tools.execute("member_cleanup_status", "{}", ctx)
    assert "스캔 기록 없음" in out
    out = await tools.execute("member_cleanup", json.dumps({"categories": ["deleted", "idle"], "idle_days": 14}), ctx)
    assert "확인 카드" in out and "아직 실행된 게 아님" in out, out
    assert not bot.named("ban") and "정말 내보낼까요" in dms(bot, BOSS.id)[-1][2]
    out = await tools.execute("member_cleanup_status", "{}", ctx)
    assert "탈퇴 1" in out and "잠수14일 2" in out and "잠수40" not in out, out
    # 기록을 읽은(tainted) 답변에선 내보내기 카드도 못 띄움
    ctx.tainted = True
    out = await tools.execute("member_cleanup", json.dumps({"categories": ["deleted"]}), ctx)
    assert "못 씀" in out
    # 멤버는 도구 자체가 없음
    mctx = tools.ToolCtx(svc, bot, CH, fake_user(50, "멤버"), await svc.perms.role(bot, CH, 50), await svc.db.get_settings(CH))
    assert "권한 없음" in await tools.execute("member_cleanup", json.dumps({"categories": ["deleted"]}), mctx)
    await mt.stop()


# ── 10. 원격 점검: 숫자만 ────────────────────────────────────
@test
async def diag_route_numbers_only():
    from sodam import diag
    svc, mt, bot = await world()
    await cleanup.scan(svc, bot, CH, BOSS.id)
    d = diag.Diag(svc.db.path)
    code, body = d.handle("/v1/cleanup", {"chat": str(CH)})
    assert code == 200 and body["scan"]["total"] == len(PEOPLE) and body["scan"]["counts"]["d"] == 1, body
    assert body["scan"]["dist"]["offline"] == 8
    raw = json.dumps(body, ensure_ascii=False)
    assert "잠수40" not in raw and "idle40" not in raw and "user_id" not in raw, "이름·ID 목록은 안 나감"
    code, body = d.handle("/v1/cleanup", {})
    assert code == 200 and body["scans"][0]["chat_id"] == CH
    await mt.stop()


# ── 11. 리뷰 수정 (2026-09-30, 203019a 코드 리뷰) ─────────────
async def _kill_task(svc):
    t = cleanup._TASKS.get((svc.db.path, CH))
    if t is not None:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    cleanup._TASKS.clear()


@test
async def activity_protects_but_join_alone_does_not():
    """14일 보호가 messages 만 보던 것: 스티커·사진만 올리는 사람(members.last_seen)도 보호. 입장만 한 건 활동 아님."""
    svc, mt, bot = await world()
    now = time.time()
    await svc.db.upsert_user(fake_user(13, "잠수20", "idle20"))
    await svc.db.touch_member(CH, 13)                                   # 그룹에 스티커 (글 기록 없음)
    await svc.db._write("UPDATE members SET last_seen=? WHERE chat_id=? AND user_id=13", (int(now - 3 * DAY), CH))
    await svc.db.upsert_user(fake_user(12, "잠수40", "idle40"))
    await svc.db.touch_member(CH, 12, joined=True)                      # 방금 입장만 (last_seen = joined_at)
    assert await cleanup.recent_writer(svc.db, CH, 13) and not await cleanup.recent_writer(svc.db, CH, 12)
    await cleanup.scan(svc, bot, CH, BOSS.id)
    got = ids(await cleanup.selection(svc, CH, ["i14"]))
    assert 13 not in got and 12 in got, got
    base = {"deleted": 0, "is_bot": 0, "msgs": 0, "photo": 0, "username": "", "recent": 0, "burst": 0,
            "status": "offline", "was_online": 1}
    assert "활동 0(소담 기록)" in cleanup.fake_signals({**base, "last_act": None})
    assert "활동 0(소담 기록)" not in cleanup.fake_signals({**base, "last_act": int(now)}), "스티커만 올려도 '활동 0' 아님"
    await mt.stop()


@test
async def crash_between_ban_and_unban_is_undone_on_resume():
    """ban 뒤 unban 전에 죽음(배포·크래시) → 이어 할 때 'kicked + 밴 중 표시' = 우리가 밴한 사람 → 먼저 풂 (영구 밴 X).
    표시 없는 kicked(다른 이유로 밴)는 안 풂."""
    svc, mt, bot = await world()
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["d", "b"], s["ts"])   # 10, 11
    await _kill_task(svc)
    await svc.db._write("INSERT INTO cleanup_banning(chat_id, user_id, stage, ts, reason, by_id) VALUES(?,?,?,?,?,?)",
                        (CH, 10, "banning", int(time.time()), "탈퇴 계정", BOSS.id))
    bot.member_status = {(CH, 10): "kicked", (CH, 11): "kicked"}
    assert await cleanup.resume_all(svc, bot) == 1
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert ("unban", CH, 10) in bot.calls and ("unban", CH, 11) not in bot.calls and not bot.named("ban"), bot.calls
    assert job["kicked"] == 1 and job["gone"] == 1 and job["state"] == "done", job
    assert not await svc.db._all("SELECT 1 FROM cleanup_banning")
    # 틱: 작업이 사라진 채 오래된 '밴 중' 표시도 정리 (밴돼 있으면 풂)
    await svc.db._write("INSERT INTO cleanup_banning(chat_id, user_id, stage, ts) VALUES(?,?,?,?)", (CH, 13, "banning", 1))
    bot.member_status[(CH, 13)] = "kicked"
    assert await cleanup.retry_unbans(svc, bot) == 1 and ("unban", CH, 13) in bot.calls
    await mt.stop()


class GateBot(FakeBot):
    """ban 이 끝나기 전에 종료가 오는 상황."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = asyncio.Event()

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.calls.append(("ban", chat_id, user_id))
        await self.gate.wait()


@test
async def shutdown_finishes_ban_unban_before_stopping():
    svc, mt, _ = await world()
    bot = GateBot(admins=[BOSS, SUB])
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["d", "i14"], s["ts"])
    for _ in range(500):   # DB 는 스레드라 sleep(0) 로는 안 넘어감
        if bot.named("ban"):
            break
        await asyncio.sleep(0.01)
    assert bot.named("ban"), "ban 에서 멈춰 있어야"
    sd = asyncio.create_task(cleanup.shutdown(svc))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not bot.named("unban")
    bot.gate.set()
    await sd
    assert [c[2] for c in bot.named("unban")] == [10], "밴한 사람은 풀고 나서 멈춤"
    job = await cleanup.get_job(svc.db, CH)
    assert job["state"] == "running" and job["kicked"] == 1 and await cleanup.remaining(svc.db, CH) == 2, job
    assert not await svc.db._all("SELECT 1 FROM cleanup_banning")
    await mt.stop()


class FlakyBot(FakeBot):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ban_timeouts, self.unban_fail = 1, True

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.calls.append(("ban", chat_id, user_id))
        if self.ban_timeouts:
            self.ban_timeouts -= 1
            raise TimedOut()

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False, **kw):
        self.calls.append(("unban", chat_id, user_id))
        if self.unban_fail:
            raise RetryAfter(cleanup.RETRY_MAX_WAIT + 100)


@test
async def ban_timeout_and_failed_unban_go_to_retry_queue():
    """ban 이 시간 초과(밴 됐는지 모름) → 풀기 시도 · 풀기가 RetryAfter 로 실패 → DB 대기열 → 틱이 나중에 풂 (영구 밴 X)."""
    svc, mt, _ = await world()
    bot = FlakyBot(admins=[BOSS, SUB])
    s, _ = await cleanup.scan(svc, bot, CH, BOSS.id)
    await cleanup.start_job(svc, bot, CH, BOSS.id, ["d", "b"], s["ts"])
    await wait_job(svc)
    job = await cleanup.get_job(svc.db, CH)
    assert ("unban", CH, 10) in bot.calls, "시간 초과여도 풀기 시도 (only_if_banned)"
    assert job["failed"] == 2 and job["fails"] == {cleanup.UNBAN_WAIT: 2} and sorted(job["fail_ids"]) == [10, 11], job
    assert not any(ch.isdigit() for k in job["fails"] for ch in k), "실패 이유 키엔 사람 ID 없음"
    rows = await svc.db._all("SELECT user_id, stage FROM cleanup_banning ORDER BY user_id")
    assert [(r["user_id"], r["stage"]) for r in rows] == [(10, "unban"), (11, "unban")]
    assert list(cleanup._leaving) == [(svc.db.path, CH, 11)], "밴 성공이 확실한 사람만 작업 퇴장 기록 (10 은 시간 초과)"
    # 원격 점검: 숫자만 (fail_ids·사람 ID 없음)
    from sodam import diag
    code, body = diag.Diag(svc.db.path).handle("/v1/cleanup", {"chat": str(CH)})
    raw = json.dumps(body, ensure_ascii=False)
    assert code == 200 and body["scan"]["unban_wait"] == 2 and "fail_ids" not in raw, raw
    # 아직 시간 안 됨 → 안 건드림 · 시간 되면 틱이 풂 (재시작해도 DB 라 남음)
    bot.unban_fail = False
    assert await cleanup.retry_unbans(svc, bot) == 0
    await svc.db._write("UPDATE cleanup_banning SET next_ts=0")
    assert await cleanup.retry_unbans(svc, bot) == 2
    assert not await svc.db._all("SELECT 1 FROM cleanup_banning")
    logs = await svc.db._all("SELECT detail FROM mod_log WHERE action='kick' AND detail LIKE '%늦게 풂%'")
    assert len(logs) == 2
    await mt.stop()


@test
async def basic_group_is_blocked():
    """일반 그룹(-100 없는 ID)은 unbanChatMember 가 안 돼 내보내면 영구 밴 → 시작 전에 막음."""
    svc, mt, bot = await world()
    await svc.db.ensure_chat(-4242, "일반방")
    s, err = await cleanup.scan(svc, bot, -4242, BOSS.id)
    assert s is None and "슈퍼그룹" in err
    ok, text = await cleanup.start_job(svc, bot, -4242, BOSS.id, ["d"], 0)
    assert not ok and "슈퍼그룹" in text and not bot.named("ban")
    assert not cleanup.is_supergroup(-4242) and cleanup.is_supergroup(CH)
    await mt.stop()


@test
async def sodam_family_bots_are_protected():
    """같은 DB 를 쓰는 딜러 봇(다른 ID)·소담 계열 계정(@Sodam_bot2 음성 도우미 등)은 절대 안 내보냄."""
    people = {**PEOPLE, 5555: ((5555, "딜러", "casino_dealer_x", True), {}),
              6666: ((6666, "도우미", "Sodam_bot2"), {"status": off(40)})}
    svc, mt, bot = await world(people)
    await cleanup.note_bot(svc, SimpleNamespace(id=5555))   # 딜러 봇 프로세스가 시작 때 적음
    await cleanup.scan(svc, bot, CH, BOSS.id)
    got = ids(await cleanup.selection(svc, CH, ["d", "b", "i14", "u", "f"]))
    assert 5555 not in got and 6666 not in got and 11 in got, got
    assert await cleanup.still_protected(svc, bot, CH, 5555) == "소담"
    import os
    os.environ["SODAM_BOT_IDS"] = "11"
    try:
        assert await cleanup.still_protected(svc, bot, CH, 11) == "소담"
    finally:
        os.environ.pop("SODAM_BOT_IDS")
    await mt.stop()


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
