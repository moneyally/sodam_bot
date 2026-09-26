"""관리자 권한 세분화 + 공동 차단 명단: python tests/run_all.py fedban_perms

실제 Permissions(텔레그램 관리자 목록 + 권한) 로 본코드 경로를 돈다.
방장(1)·차단 권한 있는 관리자(2)·차단 권한 없는 관리자(3)·오너(7)·멤버(20)·대상(21).
"""
import asyncio
import sys
from types import SimpleNamespace

import httpx
from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import commands, fedban, handlers, menu, tools
from sodam.billing import Billing
from sodam.commands import CmdCtx
from sodam.permissions import Permissions, Role
from sodam.services import PendingAction

test, run_all = runner()
A, B = -1001000000001, -1001000000002          # A: 올리는 방, B: 다른 방
CREATOR, STRONG, WEAK, OWNER, MEMBER, TARGET, BADM = 1, 2, 3, 7, 20, 21, 30
NO_RIGHT = "'사용자 차단' 권한이 있는 관리자만"


class RightsBot(FakeBot):
    """getChatAdministrators 가 실제처럼 권한 필드를 돌려준다."""

    def __init__(self, fail=False):
        super().__init__()
        self.fetches: list[int] = []
        self.fail = fail
        self.rights = {CREATOR: ("creator", True), STRONG: ("administrator", True), WEAK: ("administrator", False)}

    async def get_chat_administrators(self, chat_id):
        self.fetches.append(chat_id)
        if self.fail:
            from telegram.error import TimedOut
            raise TimedOut()
        return [SimpleNamespace(user=fake_user(uid, f"관리자{uid}"), status=st, can_restrict_members=r,
                                can_delete_messages=r) for uid, (st, r) in self.rights.items()]


async def setup(paid=(A, B)):
    db = await make_db()
    svc = await make_svc(db, owner_ids=frozenset({OWNER}), pay_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    svc.perms = Permissions(svc.cfg, db)
    svc.mod.perms = svc.perms
    svc.billing = Billing(svc.cfg, db, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    for cid in (A, B):
        await db.ensure_chat(cid, f"방{cid % 10}")
        await db.set_setting(cid, "cas_enabled", False)
        if cid in paid:
            await svc.billing.ensure_trial(cid, CREATOR)
        for uid in (CREATOR, STRONG, WEAK, MEMBER, TARGET, BADM):
            await db.upsert_user(fake_user(uid, f"사람{uid}", username=f"user{uid}"))
            await db.touch_member(cid, uid)
    return db, svc, RightsBot()


async def cmd(svc, bot, chat_id, uid, text, reply_to=None):
    user = fake_user(uid, f"사람{uid}")
    parsed = commands.parse(text, bot.username)
    msg = FakeMsg(chat_id, user, text, reply_to=reply_to)
    role = await svc.perms.role(bot, chat_id, uid)
    await commands.dispatch(CmdCtx(svc, bot, msg, chat_id, user, role, parsed[1], parsed[2]), parsed[0])
    return msg.replies


def bans(bot, chat_id=None):
    return [c for c in bot.named("ban") if chat_id is None or c[1] == chat_id]


def _drain():
    for t in list(fedban._BG):
        t.cancel()


# ── A. 권한 세분화 ────────────────────────────────────────
@test
async def ban_command_needs_restrict_right():
    db, svc, bot = await setup()
    r = await cmd(svc, bot, A, WEAK, f".밴 @user{TARGET} 도배")
    assert NO_RIGHT in r[0] and not bans(bot), r                       # 차단 권한 없는 TG 관리자 → 막힘
    r = await cmd(svc, bot, A, WEAK, f".뮤트 @user{TARGET} 1h")
    assert NO_RIGHT in r[0] and not bot.named("restrict"), r
    r = await cmd(svc, bot, A, WEAK, f".경고 @user{TARGET} 도배")
    assert NO_RIGHT in r[0] and not await db.warning_count(A, TARGET), r
    r = await cmd(svc, bot, A, WEAK, ".잠금")
    assert NO_RIGHT in r[0] and not bot.named("set_perms"), r
    r = await cmd(svc, bot, A, WEAK, f".경고목록 @user{TARGET}")        # 조회는 그대로
    assert NO_RIGHT not in r[0]
    await cmd(svc, bot, A, STRONG, f".밴 @user{TARGET} 도배")           # 권한 있는 관리자 → 됨
    assert len(bans(bot)) == 1
    await cmd(svc, bot, A, CREATOR, f".킥 @user{MEMBER}")               # 방장은 전부
    await cmd(svc, bot, A, OWNER, f".밴 @user{MEMBER}")                 # 오너는 항상
    assert len(bans(bot)) == 3, bot.named("ban")
    print("  문구:", (await cmd(svc, bot, A, WEAK, f".밴 @user{TARGET}"))[0].replace("\n", " / "))


@test
async def ai_sanction_needs_restrict_right():
    db, svc, bot = await setup()
    s = await db.get_settings(A)

    def ctx(uid):
        return tools.ToolCtx(svc, bot, A, fake_user(uid), Role.ADMIN, s)
    out = await tools.execute("ban_member", f'{{"name": "@user{TARGET}", "reason": "사기"}}', ctx(WEAK))
    assert out == tools.NO_RIGHT and not svc.pending and not bot.named("send_message"), out
    out = await tools.execute("unmute_member", f'{{"name": "@user{TARGET}"}}', ctx(WEAK))
    assert out == tools.NO_RIGHT and not bot.named("restrict"), out
    out = await tools.execute("ban_member", f'{{"name": "@user{TARGET}", "reason": "사기"}}', ctx(STRONG))
    assert len(svc.pending) == 1 and "확인 버튼" in out, out
    out = await tools.execute("warn_member", f'{{"name": "@user{TARGET}", "reason": "도배"}}', ctx(OWNER))
    assert "확인 버튼" in out


@test
async def confirm_button_checks_presser_right():
    db, svc, bot = await setup()
    key = svc.add_pending(PendingAction(A, "ban", TARGET, "대상", "사기", STRONG))
    q = FakeQuery(A, fake_user(WEAK), f"act:{key}:y")
    await handlers._confirm_action(svc, bot, q, [key, "y"])             # 누른 사람 기준 (요청자는 권한 있어도)
    assert q.answers and NO_RIGHT in q.answers[0][0] and q.answers[0][1] and not bans(bot), q.answers
    print("  문구:", q.answers[0][0].replace("\n", " / "))
    q = FakeQuery(A, fake_user(STRONG), f"act:{key}:y")
    await handlers._confirm_action(svc, bot, q, [key, "y"])
    assert len(bans(bot)) == 1, q.answers
    key = svc.add_pending(PendingAction(A, "mute", TARGET, "대상", "도배", STRONG, minutes=10))
    await handlers._confirm_action(svc, bot, FakeQuery(A, fake_user(OWNER)), [key, "y"])
    assert bot.named("restrict")


@test
async def captcha_admin_buttons_need_restrict_right():
    db, svc, bot = await setup()
    newbie = fake_user(40, "신입")
    await db.upsert_user(newbie)
    assert await svc.captcha.start(bot, A, newbie)
    for choice in ("ok", "no"):
        q = FakeQuery(A, fake_user(WEAK))
        await svc.captcha.on_callback(bot, q, ["40", choice])
        assert NO_RIGHT in q.answers[0][0] and await svc.captcha.pending(A, 40), q.answers
    q = FakeQuery(A, fake_user(STRONG))
    await svc.captcha.on_callback(bot, q, ["40", "ok"])
    assert not await svc.captcha.pending(A, 40), q.answers                # 권한 있으면 승인됨
    await db.upsert_user(fake_user(41, "신입2"))
    assert await svc.captcha.start(bot, A, fake_user(41, "신입2"))
    await svc.captcha.on_callback(bot, FakeQuery(A, fake_user(OWNER)), ["41", "no"])
    assert not await svc.captcha.pending(A, 41)                             # 오너는 됨


@test
async def old_cache_format_refetches_rights_and_network_failure_is_safe():
    db, svc, bot = await setup()
    import time
    now = int(time.time())
    for uid in (STRONG, WEAK):                                               # 예전 버전이 남긴 캐시 (권한 없음)
        await db._write("INSERT INTO chat_admins(chat_id, user_id) VALUES(?, ?)", (A, uid))
    await db._write("INSERT INTO chat_admins_fetched(chat_id, ts) VALUES(?, ?)", (A, now))
    p = Permissions(svc.cfg, db)                                             # 재시작 직후
    assert await p.can_restrict(bot, A, STRONG) and bot.fetches == [A]      # 옛 형식 → 다시 조회
    assert not await p.can_restrict(bot, A, WEAK)
    p2 = Permissions(svc.cfg, db)                                            # 새 형식 캐시는 조회 없이
    assert await p2.can_restrict(bot, A, STRONG) and bot.fetches == [A]
    # 연결 실패 + 옛 형식 캐시만 있는 방: 목록은 쓰되 권한은 모르니 막는다 (터지지 않음)
    for uid in (STRONG,):
        await db._write("INSERT INTO chat_admins(chat_id, user_id) VALUES(?, ?)", (B, uid))
    await db._write("INSERT INTO chat_admins_fetched(chat_id, ts) VALUES(?, ?)", (B, now - 99999))
    down = RightsBot(fail=True)
    p3 = Permissions(svc.cfg, db)
    assert await p3.is_admin(down, B, STRONG)                                # 기존 stale 원칙 그대로
    assert not await p3.can_restrict(down, B, STRONG)
    # 연결 실패 + 새 형식 캐시: 저장된 권한 사용
    p4 = Permissions(svc.cfg, db)
    p4.forget(A)
    assert await p4.can_restrict(down, A, STRONG) and not await p4.can_restrict(down, A, WEAK)


@test
async def bot_admin_follows_who_appointed_them():
    db, svc, bot = await setup()
    await db.set_bot_admin(A, BADM, True)
    assert await svc.perms.can_restrict(bot, A, BADM)                        # 지정 기록 없음(오너 명령 옛 기록) → 허용
    await db.log_mod(A, WEAK, BADM, "bot_admin", "추가")                     # 차단 권한 없는 관리자가 지정
    assert not await svc.perms.can_restrict(bot, A, BADM)
    r = await cmd(svc, bot, A, BADM, f".밴 @user{TARGET}")
    assert NO_RIGHT in r[0] and not bans(bot)
    await db.log_mod(A, STRONG, BADM, "bot_admin", "추가")                   # 권한 있는 관리자가 다시 지정
    assert await svc.perms.can_restrict(bot, A, BADM)
    await cmd(svc, bot, A, OWNER, f".봇관리자 추가 @user{MEMBER}")          # 오너 명령도 기록이 남음
    assert await svc.perms.can_restrict(bot, A, MEMBER)
    row = await db._one("SELECT actor_id FROM mod_log WHERE target_id=? AND action='bot_admin'", (MEMBER,))
    assert row["actor_id"] == OWNER


# ── B. 공동 차단 명단 ─────────────────────────────────────
def join_ctx(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "chats": set(), "cas_seen": set(), "tasks": set(), "joins": {}})


async def listed(svc, bot, reason="코인 사기 DM"):
    r = await cmd(svc, bot, A, STRONG, f".공동차단 @user{TARGET} {reason}")
    assert "공동 차단 명단" in r[0] and bans(bot, A), r
    return r


@test
async def fedban_add_then_join_other_room_alerts_admins():
    db, svc, bot = await setup()
    r = await listed(svc, bot)
    print("  올리기 문구:", r[0].replace("\n", " / "))
    await handlers.handle_new_member(join_ctx(svc, bot), B, "방2", fake_user(TARGET, "사람21"))
    dms = [c for c in bot.named("send_message") if c[1] in (CREATOR, STRONG, WEAK)]
    assert len(dms) == 3 and "공동 차단 명단 계정 입장" in dms[0][2] and "올린 방: 1곳" in dms[0][2], dms
    kb = dms[0][3]["reply_markup"].inline_keyboard[0]
    assert kb[0].callback_data == f"m:fbx:{B}:{TARGET}:b" and not bans(bot, B)
    print("  알림 문구:", dms[0][2].replace("\n", " / "))
    # [밴] 버튼: 차단 권한 없는 관리자 → 막힘, 있는 관리자 → 밴
    q = FakeQuery(WEAK, fake_user(WEAK))
    await menu.on_callback(svc, bot, q, kb[0].callback_data.split(":")[1:])
    assert NO_RIGHT in q.answers[0][0] and not bans(bot, B), q.answers
    q = FakeQuery(STRONG, fake_user(STRONG))
    await menu.on_callback(svc, bot, q, kb[0].callback_data.split(":")[1:])
    assert bans(bot, B) and "내보냈어요" in q.edits[0], (q.answers, q.edits)
    # 두 번째 방도 올리면 신뢰도(올린 방 수) ↑
    await cmd(svc, bot, B, STRONG, f".공동차단 {TARGET} 사칭 사기")
    assert (await fedban.lookup(db, TARGET))["rooms"] == 2
    _drain()


@test
async def fedban_first_message_alerts_once():
    db, svc, bot = await setup()
    await listed(svc, bot)
    msg = FakeMsg(B, fake_user(TARGET, "사람21"), "안녕하세요")
    await fedban.on_message(svc, bot, msg, Role.MEMBER)
    await fedban.on_message(svc, bot, msg, Role.MEMBER)
    dms = [c for c in bot.named("send_message") if "말을 했어요" in c[2]]
    assert len(dms) == 3, dms                                                # 관리자 3명에게 1번씩만


@test
async def fedban_modes_off_and_autoban():
    db, svc, bot = await setup()
    await listed(svc, bot)
    await db.set_setting(B, "fedban_mode", "off")
    await handlers.handle_new_member(join_ctx(svc, bot), B, "방2", fake_user(TARGET, "사람21"))
    assert not bans(bot, B) and not any("공동 차단" in c[2] for c in bot.named("send_message") if c[1] > 0)
    await svc.captcha.cancel(bot, B, TARGET)                                 # 끔 → 평소처럼 캡차가 걸렸던 것 정리
    await db.set_setting(B, "fedban_mode", "ban")
    svc.joins.clear()                                                        # 같은 사람 재입장 (중복 입장 방지 기록 비움)
    handlers_ctx = join_ctx(svc, bot)
    await handlers.handle_new_member(handlers_ctx, B, "방2", fake_user(TARGET, "사람21"))
    assert bans(bot, B) and not await svc.captcha.pending(B, TARGET), bot.calls[-3:]
    await asyncio.sleep(0)
    notice = [c for c in bot.named("send_message") if c[1] == B]
    assert notice and "내보냈어요" in notice[-1][2], notice
    print("  자동 밴 문구:", notice[-1][2])
    _drain()


@test
async def fedban_unpaid_room_limits():
    db, svc, bot = await setup(paid=(A,))
    import time
    await db.start_subscription(B, int(time.time()) - 60, None)            # B 는 이용 기간 끝남
    r = await cmd(svc, bot, B, STRONG, f".공동차단 @user{MEMBER} 스팸")
    assert "이용 기간" in r[0] and not await fedban.lookup(db, MEMBER), r   # 올리기 불가
    await listed(svc, bot)
    await db.set_setting(B, "fedban_mode", "ban")                            # 자동 밴 설정이어도
    await handlers.handle_new_member(join_ctx(svc, bot), B, "방2", fake_user(TARGET, "사람21"))
    assert not bans(bot, B)                                                  # 밴 안 하고
    assert any("공동 차단 명단 계정 입장" in c[2] for c in bot.named("send_message"))   # 알림은 감
    _drain()


@test
async def fedban_rules_limit_reason_protected():
    db, svc, bot = await setup()
    r = await cmd(svc, bot, A, STRONG, f".공동차단 @user{TARGET}")
    assert "사유" in r[0] and not await fedban.lookup(db, TARGET)
    r = await cmd(svc, bot, A, STRONG, f".공동차단 @user{WEAK} 사기")         # 관리자
    assert not await fedban.lookup(db, WEAK), r
    r = await cmd(svc, bot, A, STRONG, f".공동차단 {OWNER} 사기")             # 오너 (방에 없는 숫자 ID)
    assert "올릴 수 없어요" in r[0] and not await fedban.lookup(db, OWNER), r
    r = await cmd(svc, bot, A, WEAK, f".공동차단 @user{TARGET} 사기")          # 차단 권한 없는 관리자
    assert NO_RIGHT in r[0] and not await fedban.lookup(db, TARGET)
    for i in range(fedban.DAILY_LIMIT):
        await cmd(svc, bot, A, STRONG, f".공동차단 {5000 + i} 스팸 {i}")
    r = await cmd(svc, bot, A, STRONG, f".공동차단 {6000} 스팸")
    assert "하루 20명" in r[0] and not await fedban.lookup(db, 6000), r
    assert await fedban.lookup(db, 5019)
    print("  한도 문구:", r[0])


@test
async def fedban_remove_room_mark_and_owner_delete():
    db, svc, bot = await setup()
    await listed(svc, bot)
    await cmd(svc, bot, B, STRONG, f".공동차단 {TARGET} 사칭")
    r = await cmd(svc, bot, A, STRONG, f".공동차단 해제 {TARGET}")
    info = await fedban.lookup(db, TARGET)
    assert info and info["rooms"] == 1 and info["chats"] == [B], r          # A 표시만 빠짐
    r = await cmd(svc, bot, A, STRONG, f".공동차단 해제 {TARGET}")
    assert "이 방이 올린 기록이 없어요" in r[0] and await fedban.lookup(db, TARGET)
    r = await cmd(svc, bot, A, OWNER, f".공동차단 해제 {TARGET}")             # 오너 → 완전 삭제
    assert "완전히" in r[0] and not await fedban.lookup(db, TARGET), r
    await listed(svc, bot)
    await cmd(svc, bot, A, STRONG, f".공동차단 해제 {TARGET}")               # 올린 방이 하나뿐 → 명단에서 사라짐
    assert not await fedban.lookup(db, TARGET)
    _drain()


@test
async def fedban_panel_screens():
    db, svc, bot = await setup()
    await listed(svc, bot)
    q = FakeQuery(STRONG, fake_user(STRONG))
    await menu.on_callback(svc, bot, q, ["fb", str(A)])
    assert "공동 차단 명단" in q.edits[0] and any(b.callback_data == f"m:n:{A}:fedban_mode:ban"
                                                for row in q.kb.inline_keyboard for b in row)
    await menu.on_callback(svc, bot, FakeQuery(STRONG, fake_user(STRONG)), ["n", str(A), "fedban_mode", "ban"])
    assert (await db.get_settings(A))["fedban_mode"] == "ban"
    q = FakeQuery(STRONG, fake_user(STRONG))
    await menu.on_callback(svc, bot, q, ["fbl", str(A)])
    assert str(TARGET) in q.edits[0] and "이 방이 올림" in q.edits[0]
    unmark = [b.callback_data for row in q.kb.inline_keyboard for b in row if "표시 빼기" in b.text]
    qw = FakeQuery(WEAK, fake_user(WEAK))                                   # 차단 권한 없는 관리자가 자기 목록에서 누름
    await menu.on_callback(svc, bot, qw, ["fbl", str(A)])
    mine = [b.callback_data for row in qw.kb.inline_keyboard for b in row if "표시 빼기" in b.text]
    q2 = FakeQuery(WEAK, fake_user(WEAK))
    await menu.on_callback(svc, bot, q2, mine[0].split(":")[1:])
    assert NO_RIGHT in q2.answers[0][0] and await fedban.lookup(db, TARGET), q2.answers
    q3 = FakeQuery(STRONG, fake_user(STRONG))
    await menu.on_callback(svc, bot, q3, unmark[0].split(":")[1:])
    assert not await fedban.lookup(db, TARGET), q3.answers
    _drain()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
