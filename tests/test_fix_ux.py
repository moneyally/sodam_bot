"""UX·오탐 수정 회귀 테스트 (도배 시각·무료 한도 안내·호출어·답장·체험 알림·도움말·./! 혼동·사용량·봇 강퇴·그림장 이전):
python tests/run_all.py fix_ux"""
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, TZ, add_member, fake_user, make_db, make_svc, runner
from fake_llm import Room, ScriptedLLM, reply
from test_billing import CHAT as BILL_CHAT, setup as billing_setup

from sodam import commands, handlers, menu, social
from sodam.casino import board
from sodam.commands import CmdCtx
from sodam.greet import Greeter
from sodam.handlers import addressed_to_bot
from sodam.llm import LLM, ROOM_TOKENS
from sodam.permissions import Role
from sodam.util import RateLimiter

test, run_all = runner()
CHAT = -100555
BOT = SimpleNamespace(id=999, username="sodambot")
NAMES = ("소담아", "소담이", "소담")
ALICE = fake_user(10, "김민지", "minji")


def _ctx(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set(),
                                     "limiter": RateLimiter()})


def _day(ts=None):
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _sent(bot, chat_id=None):
    return [c[2] for c in bot.named("send_message") if chat_id is None or c[1] == chat_id]


# ── 1. 재시작 뒤 밀린 메시지: 보낸 시각 기준으로 도배 판정 ─────
@test
async def backlog_messages_not_flood_muted():
    db = await make_db()
    svc = await make_svc(db)
    await db.set_setting(CHAT, "flood_count", 5)
    await db.set_setting(CHAT, "flood_seconds", 8)
    bot = FakeBot()
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i in range(8):  # 1분 간격으로 보낸 8개를 재시작 뒤 한꺼번에 처리
        m = FakeMsg(CHAT, ALICE, f"안녕하세요 {i}번째 얘기", message_id=i + 1)
        m.date = base + timedelta(minutes=i)
        assert await svc.mod.check_message(bot, m, m.text) is None, i
    assert not bot.named("restrict"), "밀린 정상 메시지를 도배로 뮤트함"
    # 실제로 1초 안에 몰아친 건 (늦게 받아도) 도배
    now = datetime.now(timezone.utc)
    notices = []
    for i in range(5):
        m = FakeMsg(CHAT, fake_user(11, "도배꾼"), f"광고 {i}", message_id=100 + i)
        m.date = now
        notices.append(await svc.mod.check_message(bot, m, m.text))
    assert notices[-1] and "채팅 금지" in notices[-1] and bot.named("restrict")


# ── 2. 무료 AI 한도 초과 안내: 방마다 하루 1번, 자동 삭제 ─────
@test
async def free_quota_notice_once_per_day_and_temp():
    db, svc, _ = await billing_setup(free_ai_per_day=1)
    await svc.billing.ensure_trial(BILL_CHAT, 1)
    await db.conn.execute("UPDATE subscriptions SET trial_until=?, paid_until=NULL WHERE chat_id=?",
                          (int(time.time()) - 10, BILL_CHAT))
    await db.conn.commit()
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    results = [await handlers._within_ai_quota(ctx, BILL_CHAT, 20, Role.MEMBER) for _ in range(5)]
    assert results == [False] * 5                                  # 끝난 방은 AI 없음, 안내만
    notices = [t for t in _sent(bot, BILL_CHAT) if "이용 기간이 끝나서" in t]
    assert len(notices) == 1, notices                               # 부를 때마다가 아니라 한 번만
    sent_id = bot._next_id
    assert any(data == (BILL_CHAT, sent_id) for _, _, data in ctx.job_queue.once)  # 자동 삭제 예약
    print("  실제 안내:", notices[0])


# ── 3. 호출어 오탐: 조사가 붙은 3인칭 언급은 호출 아님 ─────
@test
def call_name_third_person_not_addressed():
    msg = SimpleNamespace(reply_to_message=None)
    for t in ("소담이가 또 틀렸네", "소담이는 왜 저래", "우리 소담이 최고", "소담이랑 놀자", "소담이한테 물어봤는데"):
        assert addressed_to_bot(msg, t, NAMES, BOT)[0] is False, t
    assert addressed_to_bot(msg, "소담아 오늘 날씨?", NAMES, BOT) == (True, "오늘 날씨?")
    assert addressed_to_bot(msg, "소담이 오늘 일정 정리해줘", NAMES, BOT) == (True, "오늘 일정 정리해줘")
    assert addressed_to_bot(msg, "근데 소담이 이거 알려줘", NAMES, BOT)[0] is True     # 중간이어도 끝이 부탁
    assert addressed_to_bot(msg, "소담이야 뭐해?", NAMES, BOT)[0] is True
    assert addressed_to_bot(msg, "@sodambot 도와줘", NAMES, BOT) == (True, "도와줘")
    # '소담이도 …' + 부탁·권유로 끝나면 부른 것 (베베 2026-10-06 '소담이도 참여 ㄱㄱ' 에 답 없음)
    for t in ("소담이도 참여 ㄱㄱ", "소담도 해봐", "그럼 소담이도 같이 하자", "소담이도 와"):
        assert addressed_to_bot(msg, t, NAMES, BOT)[0] is True, t
    assert addressed_to_bot(msg, "소담이도 참여 ㄱㄱ", NAMES, BOT) == (True, "참여 ㄱㄱ")
    for t in ("소담이도 틀렸네", "소담이도 사람이냐", "우리 소담이도 귀엽다"):   # 끝이 부탁이 아니면 그대로 언급
        assert addressed_to_bot(msg, t, NAMES, BOT)[0] is False, t


# ── 4. 봇 메시지 답장: AI 답에 단 답장만 호출 ──────────────
@test
async def reply_to_non_ai_bot_message_not_addressed():
    r = await Room().open(admins={1})
    await r.join(ALICE)
    game_result = SimpleNamespace(from_user=r.bot_user(), text="🎲 홀! 김민지님 +1,950P", caption=None, message_id=5000)
    await r.say(ALICE, "와 이게 뭐야?", reply_to=game_result)          # 게임 결과에 답장 → AI 아님
    assert not r.llm.of("chat"), "AI 답이 아닌 봇 메시지 답장이 AI 를 불렀음"

    r.llm.script = [reply("모임은 3시예요."), reply("강남역이에요.")]
    first = await r.say(ALICE, "소담아 모임 몇 시야?")
    ai_msg = SimpleNamespace(from_user=r.bot_user(), text="모임은 3시예요.", caption=None,
                             message_id=first.message_id + 10_000)   # FakeMsg.reply_text 가 돌려준 id
    await r.say(ALICE, "장소는?", reply_to=ai_msg)                    # AI 답에 답장 → 호출
    assert len(r.llm.of("chat")) == 2 and r.llm.script == []


# ── 5. .기억 제목 조사 ─────────────────────────────────────
@test
async def memory_title_josa():
    db = await make_db()
    svc = await make_svc(db)
    await add_member(db, CHAT, ALICE)
    msg = FakeMsg(CHAT, ALICE, ".기억")
    cmd, args, argstr = commands.parse(".기억", "sodambot")
    await commands.dispatch(CmdCtx(svc, FakeBot(), msg, CHAT, ALICE, Role.MEMBER, args, argstr), cmd)
    assert "소담이 기억하는" in msg.replies[0] and "소담가" not in msg.replies[0], msg.replies[0]
    print("  실제 제목:", msg.replies[0].splitlines()[0])
    assert cmd.fn is social.c_memory


# ── 6. 체험 알림: 마지막 날 1번 + 끝난 날 1번, 자동 삭제, 사실대로 ─────
@test
async def trial_reminders_last_day_only():
    db, svc, _ = await billing_setup(free_ai_per_day=10)
    now = int(time.time())
    rooms = {-1001: ("trial", now + int(2.5 * 86400)),   # 체험 첫날 → 알림 없음
             -1002: ("trial", now + 10 * 3600),          # 체험 마지막 날
             -1003: ("trial", now - 3600),               # 체험 끝난 날
             -1004: ("paid", now + 2 * 86400)}           # 유료 2일 남음 (원래대로)
    for cid, (kind, until) in rooms.items():
        col = "trial_until" if kind == "trial" else "paid_until"
        await db.conn.execute(f"INSERT INTO subscriptions(chat_id, {col}, updated_at) VALUES(?,?,?)", (cid, until, now))
    await db.conn.commit()
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    await handlers.job_sub_reminders(ctx)
    assert not _sent(bot, -1001), "체험 첫날부터 알림"
    last, paid = _sent(bot, -1002), _sent(bot, -1004)
    assert not _sent(bot, -1003), "끝난 방 안내는 10시가 아니라 끝난 그 시각에 (_notify_ended)"
    await handlers._notify_ended(ctx)
    await handlers._notify_ended(ctx)                                # 두 번 돌아도 한 번
    ended = _sent(bot, -1003)
    assert len(last) == 1 and "무료 체험이" in last[0] and "끝나요" in last[0]
    assert len(ended) == 1 and "무료 체험이 끝났어요" in ended[0] and "AI 대화" in ended[0] and "계속 무료" in ended[0]
    assert "USDT" not in ended[0]
    assert len(paid) == 1 and "2일 남았어요" in paid[0]
    assert not _sent(bot, -1002)[1:] and not _sent(bot, -1004)[1:], "아직 안 끝난 방엔 끝남 안내 없음"
    assert len(ctx.job_queue.once) == 3 and all(when == handlers.REMINDER_TTL for _, when, _ in ctx.job_queue.once)
    for t in last + ended + paid:
        print("  실제 알림:", t)


# ── 7. 도움말: 새 기능 안내 · 관리자 전체 목록은 1:1 · 한글 이름 먼저 ─────
@test
async def help_new_features_admin_dm_korean_first():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    cmd, args, argstr = commands.parse(".명령어", "sodambot")   # 명령어 전체 (예전 .도움말, 지금 .도움말 = 말 예시)
    bot = FakeBot()
    member = FakeMsg(CHAT, ALICE, ".명령어")
    await commands.dispatch(CmdCtx(svc, bot, member, CHAT, ALICE, Role.MEMBER, args, argstr), cmd)
    text = member.replies[0]
    for want in ("소담아 이거 뭐야", "그려줘", "!도움", "!그림장", "!룰렛"):
        assert want in text, want

    admin = fake_user(1, "방장")
    msg = FakeMsg(CHAT, admin, ".명령어")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    room_text = msg.replies[0]
    assert room_text.count("\n") < 5 and "1:1" in room_text, room_text     # 방엔 짧게
    dm = [c[2] for c in bot.named("send_message") if c[1] == 1]
    assert dm and "<code>.경고</code>" in dm[0]                          # 전체는 관리자 1:1
    for ko, en in (("설정", "settings"), ("설정변경", "set"), ("AI대화", "ai"), ("스팸차단", "cas")):
        assert f"<code>.{ko}</code>" in dm[0] and f"<code>.{en}</code>" not in dm[0], ko
    assert commands._INDEX["settings"] is commands._INDEX["설정"]         # 영어 별칭은 그대로 동작
    assert commands._BACKGROUND, "방 도움말 자동 삭제 예약 안 됨"
    for t in list(commands._BACKGROUND):
        t.cancel()
    assert "!그림장" in menu.HELP and "그려줘" in menu.HELP
    print("  방(관리자):", room_text.replace("\n", " / "))
    print("  멤버 도움말 앞 3줄:", " / ".join(text.splitlines()[:3]))


# ── 8. . 과 ! 혼동 ────────────────────────────────────────
@test
async def dot_bang_mixup_hint():
    r = await Room().open(admins={1})
    await r.join(ALICE)
    await r.say(ALICE, ".가입")
    hints = [t for t in _sent(r.bot, r.CHAT) if "혹시" in t]
    assert hints and "!가입" in hints[-1], _sent(r.bot)
    assert r.ctx.job_queue.once and r.ctx.job_queue.once[-1][1] == 15
    await r.say(ALICE, "!설정")
    assert ".설정" in [t for t in _sent(r.bot, r.CHAT) if "혹시" in t][-1]
    m = await r.say(ALICE, "!도움말")
    assert m.replies and "포인트 게임" in m.replies[0]
    n = len(_sent(r.bot))
    await r.say(ALICE, "!ㅋㅋㅋ")                                  # 어느 쪽에도 없으면 조용히
    assert len(_sent(r.bot)) == n
    print("  실제 안내:", hints[-1])


# ── 9. .사용량: 방 관리자는 그 방만, 오너는 전체 ─────────────
@test
async def usage_room_only_for_admin():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    svc.llm = LLM(svc.cfg, db)
    await db.bump(_day(), CHAT, ROOM_TOKENS, 12_345)
    await db.bump(_day(), 0, "tokens", 987_654)
    cmd, args, argstr = commands.parse(".사용량", "sodambot")
    admin = fake_user(1, "방장")
    msg = FakeMsg(CHAT, admin, ".사용량")
    await commands.dispatch(CmdCtx(svc, FakeBot(), msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    assert "12,345" in msg.replies[0] and "987,654" not in msg.replies[0] and "1,000,000" not in msg.replies[0]
    msg2 = FakeMsg(CHAT, admin, ".사용량")
    await commands.dispatch(CmdCtx(svc, FakeBot(), msg2, CHAT, admin, Role.OWNER, args, argstr), cmd)
    assert "12,345" in msg2.replies[0] and "987,654" in msg2.replies[0]
    print("  관리자:", msg.replies[0])


# ── 10. 봇 강퇴 → 예약공지 끔 + 오너 알림 / 관리자 지정 → 권한 안내 ─────
def _cm(status, **rights):
    return SimpleNamespace(status=status, user=SimpleNamespace(id=999), is_member=status != "left", **rights)


def _my_update(old, new):
    chat = SimpleNamespace(id=CHAT, title="대표님 방", type="supergroup")
    return SimpleNamespace(my_chat_member=SimpleNamespace(chat=chat, from_user=fake_user(1, "방장"),
                                                          old_chat_member=old, new_chat_member=new))


@test
async def bot_kicked_disables_schedules_and_reports():
    db = await make_db()
    svc = await make_svc(db, admins={1}, log_chat_id=-777)
    await db.ensure_chat(CHAT, "대표님 방")
    await db.add_schedule(CHAT, kind="interval", at_time=None, interval_min=30, title="t", text="x",
                          media_type=None, media_id=None, pin=False, created_by=1)
    bot = FakeBot()
    ctx = _ctx(svc, bot)
    await handlers.on_my_chat_member(_my_update(_cm("member"), _cm("kicked")), ctx)
    assert not await db.schedules()                                # enabled=1 인 예약공지 없음
    report = _sent(bot, -777)
    assert report and "봇 강퇴" in report[0] and "예약공지 1개" in report[0], report
    print("  오너 알림:", report[0])


@test
async def bot_promoted_rights_notice():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot()
    ctx = _ctx(svc, bot)
    await handlers.on_my_chat_member(
        _my_update(_cm("member"), _cm("administrator", can_delete_messages=True, can_restrict_members=False)), ctx)
    t = _sent(bot, CHAT)
    assert len(t) == 1 and "관리자 권한 확인" in t[0] and "사용자 차단" in t[0] and "메시지 삭제" not in t[0], t
    assert ctx.job_queue.once                                        # 자동 삭제
    await handlers.on_my_chat_member(
        _my_update(_cm("administrator", can_delete_messages=True, can_restrict_members=False),
                   _cm("administrator", can_delete_messages=True, can_restrict_members=True)), ctx)
    t = _sent(bot, CHAT)
    assert len(t) == 2 and t[1].startswith("✅ 관리자 권한 확인!"), t
    await handlers.on_my_chat_member(                                # 관리와 무관한 권한만 바뀜 → 조용히
        _my_update(_cm("administrator", can_delete_messages=True, can_restrict_members=True),
                   _cm("administrator", can_delete_messages=True, can_restrict_members=True, can_pin_messages=True)), ctx)
    assert len(_sent(bot, CHAT)) == 2
    print("  빠진 권한:", _sent(bot, CHAT)[0])
    print("  다 있음:", _sent(bot, CHAT)[1])


# ── 11. 입장 인사 AI 호출도 방 토큰으로 ──────────────────
@test
async def greet_llm_counts_room_tokens():
    db = await make_db()
    svc = await make_svc(db)
    svc.llm = ScriptedLLM([reply("{names}님 어서 오세요!")])
    tpl = await Greeter(svc)._template(CHAT, 1)
    assert "{names}" in tpl
    assert svc.llm.calls[-1].get("chat_id") == CHAT


# ── 12. 슈퍼그룹 전환 때 그림장 기록 유지 ─────────────────
@test
async def board_results_survive_migration():
    db = await make_db()
    await board.record(db, CHAT, "홀짝", "홀")
    await board.record(db, CHAT, "홀짝", "짝")
    new = -1009999
    await db.migrate_chat(CHAT, new)
    assert await board.recent(db, new, "홀짝") == ["홀", "짝"] or await board.recent(db, new, "홀짝") == ["짝", "홀"]
    assert not await board.recent(db, CHAT, "홀짝")


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
