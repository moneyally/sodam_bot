"""관리·명령 버그 수정 회귀 테스트 (캡차 뮤트 우회·결제 노출·CAS·잠금해제·CSV 등): python tests/run_all.py fix_mod"""
import asyncio
import io
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from telegram import Chat, ChatPermissions, Message, User
from telegram.error import BadRequest, NetworkError

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, add_member, fake_user, make_db, make_svc, runner
from fake_llm import ChatBot, Room, ScriptedLLM
from test_billing import make_address

from sodam import __main__ as sodam_main
from sodam import commands, handlers, security
from sodam.billing import Billing
from sodam.commands import CmdCtx
from sodam.menu import PanelCtx
from sodam.panels import members as members_panel
from sodam.permissions import Permissions, Role
from sodam.util import RateLimiter, parse_duration

test, run_all = runner()
CHAT = -1001234567
PAY = make_address(b"our-wallet")


def _ctx(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set(),
                                     "limiter": RateLimiter()})


class MemberBot(FakeBot):
    """get_chat_member 결과를 사람별로 지정 (restricted 등)."""
    def __init__(self, members=None, fail=False, **kw):
        super().__init__(**kw)
        self.members, self.fail = members or {}, fail

    async def get_chat_member(self, chat_id, user_id):
        if user_id in self.members:
            self.calls.append(("get_chat_member", chat_id, user_id))
            if self.fail:
                raise NetworkError("timeout")
            return self.members[user_id]
        return await super().get_chat_member(chat_id, user_id)


def _restricted(can_send):
    return SimpleNamespace(status="restricted", is_member=True, can_send_messages=can_send, user=SimpleNamespace(id=20))


# ── 1. 뮤트된 사람이 재입장해도 캡차가 뮤트를 풀지 않음 ──────
@test
async def muted_user_rejoin_keeps_mute():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = MemberBot({20: _restricted(False)})
    await handlers.handle_new_member(_ctx(svc, bot), CHAT, "방", fake_user(20, "뮤트된사람"))
    assert not await svc.captcha.pending(CHAT, 20), "뮤트된 사람에게 캡차를 걸면 통과 시 뮤트가 풀림"
    assert not bot.named("restrict") and not bot.named("send_message") and not svc.greeter.queued


@test
async def muted_check_failure_falls_back_to_captcha():
    """조회 실패면 원래대로 캡차 (새 입장자를 검사 없이 들이지 않게)."""
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = MemberBot({20: _restricted(False)}, fail=True)
    await handlers.handle_new_member(_ctx(svc, bot), CHAT, "방", fake_user(20, "신입"))
    assert await svc.captcha.pending(CHAT, 20)


@test
async def captcha_leave_lifts_so_rejoin_gets_captcha_again():
    """캡차 도중 나가면 캡차 제한을 풀어둔다 → 재입장 때 '뮤트된 사람'으로 오인해 영영 막히지 않게."""
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = MemberBot()
    ctx = _ctx(svc, bot)
    await handlers.handle_new_member(ctx, CHAT, "방", fake_user(20, "신입"))
    assert await svc.captcha.pending(CHAT, 20)
    await svc.captcha.cancel(bot, CHAT, 20)
    last = bot.named("restrict")[-1]
    assert last[3] == ChatPermissions.all_permissions(), last


# ── 2·3. .구독 결제 패널은 텔레그램 관리자에게만, 1:1 은 후보 방만 조회 ──
async def _billing_setup():
    db = await make_db()
    svc = await make_svc(db, pay_address=PAY, trongrid_api_key="k", owner_ids=frozenset())
    svc.perms = Permissions(svc.cfg, db)  # 실제 권한 판단 (봇관리자 ≠ 텔레그램 관리자)
    svc.billing = Billing(svc.cfg, db, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))))
    await db.ensure_chat(CHAT, "오케이 소통방")
    await svc.billing.ensure_trial(CHAT, 1)
    return db, svc


class CountBot(FakeBot):
    async def get_chat_administrators(self, chat_id):
        self.calls.append(("get_admins", chat_id))
        return await super().get_chat_administrators(chat_id)


async def _sub(svc, bot, chat_id, user, role):
    cmd, args, argstr = commands.parse(".구독", "sodambot")
    msg = FakeMsg(chat_id, user, ".구독")
    await commands.dispatch(CmdCtx(svc, bot, msg, chat_id, user, role, args, argstr), cmd)
    return msg


@test
async def bot_admin_gets_no_payment_panel():
    db, svc = await _billing_setup()
    bot = CountBot(admins=[fake_user(1, "방장")])
    bot_admin = fake_user(5, "봇관리자")
    await db.set_bot_admin(CHAT, 5, True)
    assert await svc.perms.is_admin(bot, CHAT, 5) and not await svc.perms.is_tg_admin(bot, CHAT, 5)
    await _sub(svc, bot, CHAT, bot_admin, Role.ADMIN)
    assert not any(c[1] == 5 for c in bot.named("send_message")), "봇관리자 1:1 로 결제 패널(금액)이 감"
    msg = await _sub(svc, bot, 5, bot_admin, Role.MEMBER)                    # 1:1 .구독 도 마찬가지
    assert not any("USDT" in r for r in msg.replies), msg.replies
    await _sub(svc, bot, CHAT, fake_user(1, "방장"), Role.ADMIN)              # 텔레그램 관리자는 받음
    assert any(c[1] == 1 and "USDT" in c[2] for c in bot.named("send_message"))


@test
async def dm_subscribe_queries_only_candidate_chats():
    db, svc = await _billing_setup()
    bot = CountBot(admins=[fake_user(1, "방장")])
    await svc.perms.telegram_admins(bot, CHAT)                              # 1 은 CHAT 관리자로 기록됨
    others = [-1009000000 - i for i in range(5)]
    for cid in others:                                                      # 1 이 관리자가 아닌 방들 (1시간 전 목록)
        await db.ensure_chat(cid, f"남의 방 {cid}")
        await db._write("INSERT INTO chat_admins_fetched(chat_id, ts) VALUES(?, ?)", (cid, int(time.time()) - 3600))
    bot.calls.clear()
    msg = await _sub(svc, bot, 1, fake_user(1, "방장"), Role.MEMBER)
    asked = {c[1] for c in bot.named("get_admins")}
    assert not asked & set(others), f"관계없는 방에도 관리자 조회: {sorted(asked)}"
    assert msg.replies and "오케이 소통방" in msg.replies[0]


# ── 4. CAS 조회 기록은 방마다 ────────────────────────────────
@test
async def cas_checked_per_chat():
    db = await make_db()
    svc = await make_svc(db, admins={1}, cas_banned={66})
    svc.llm = ScriptedLLM()
    bot = ChatBot()
    ctx = _ctx(svc, bot)
    spammer = fake_user(66, "스패머")
    other = -1005555555
    for cid in (CHAT, other):
        await db.ensure_chat(cid, "방")
        await db.set_setting(cid, "cas_enabled", True)
        m = FakeMsg(cid, spammer, "ㅎㅇ", message_id=7)
        m.chat, m.sender_chat = SimpleNamespace(id=cid, title="방", type="supergroup"), None
        await handlers.on_group_message(SimpleNamespace(message=m), ctx)
        await asyncio.gather(*list(ctx.bot_data["tasks"]), return_exceptions=True)
    banned = {c[1] for c in bot.named("ban")}
    assert banned == {CHAT, other}, f"CAS 밴 된 방: {banned}"


# ── 5. 만료 목록을 뽑은 뒤 정답을 누른 사람은 강퇴하지 않음 ──
@test
async def expire_does_not_kick_user_who_just_passed():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot()
    u = fake_user(20, "신입")
    assert await svc.captcha.start(bot, CHAT, u)
    await db._write("UPDATE captcha SET expires_at=0")
    real = db.expired_captchas

    async def racy(now_ts):  # 목록을 뽑은 직후 사용자가 정답을 누름
        rows = await real(now_ts)
        await svc.captcha.approve(bot, CHAT, 20, "신입", None)
        return rows
    db.expired_captchas = racy
    await svc.captcha.expire(bot)
    assert not bot.named("ban"), "정답 누른 사람을 시간 초과로 강퇴함"
    assert svc.greeter.queued == [(CHAT, 20, "신입")]


# ── 6. 잠금해제: 저장된 권한에 모르는 필드가 있어도 ─────────
@test
async def unlock_with_unknown_saved_field():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot()
    assert await svc.mod.lock(bot, CHAT, 1)
    saved = await db.get_state(CHAT, "saved_permissions")
    await db.set_state(CHAT, "saved_permissions", {**saved, "can_send_future_thing": True})  # 새 API 필드
    await svc.mod.unlock(bot, CHAT, 1)
    perms = bot.named("set_perms")[-1][2]
    assert perms.can_send_messages is True and perms.can_send_photos is True, perms
    assert not await db.get_state(CHAT, "locked")


# ── 7. 멤버 CSV 수식 주입 ─────────────────────────────────────
@test
async def members_csv_escapes_formulas():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot(admins=[fake_user(1, "방장")])
    await db.ensure_chat(CHAT, "방")
    bad = ['=HYPERLINK("http://x","클릭")', "+1+1", "-2+3", "@SUM(A1)"]  # 앞 탭·CR 은 display_name 이 strip
    for i, name in enumerate(bad):
        await add_member(db, CHAT, fake_user(100 + i, name), joined=True)
    await add_member(db, CHAT, fake_user(200, "보통이름"), joined=True)
    await members_panel.r_export(PanelCtx(svc, bot, 1, CHAT, []))
    doc = bot.named("send_document")[-1][2]
    text = doc.input_file_content.decode("utf-8-sig")
    rows = list(__import__("csv").reader(io.StringIO(text)))
    names = {r[1] for r in rows[1:]}
    for name in bad:
        assert "'" + name in names, (name, names)
    assert "보통이름" in names
    assert members_panel._cell("\t=1") == "'\t=1" and members_panel._cell("\r=1") == "'\r=1"


# ── 8. .뮤트 시간 상한 ────────────────────────────────────────
@test
async def mute_huge_duration_is_rejected():
    assert parse_duration("527040") == 527040 and parse_duration("366d") == 527040
    assert parse_duration("527041") is None and parse_duration("367일") is None
    db = await make_db()
    svc = await make_svc(db, admins={1})
    bot = FakeBot()
    target = fake_user(20, "대상")
    await add_member(db, CHAT, target)
    for dur in ("99999999999d", "400일"):
        cmd, args, argstr = commands.parse(f".뮤트 {dur} 도배", "sodambot")
        msg = FakeMsg(CHAT, fake_user(1, "관리자"), f".뮤트 {dur}", reply_to=FakeMsg(CHAT, target, "스팸"))
        await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, msg.from_user, Role.ADMIN, args, argstr), cmd)
        assert "366일" in msg.replies[-1], msg.replies
    assert not bot.named("restrict")
    cmd, args, argstr = commands.parse(".뮤트 2시간 도배", "sodambot")    # 정상 값은 그대로
    msg = FakeMsg(CHAT, fake_user(1, "관리자"), ".뮤트", reply_to=FakeMsg(CHAT, target, "스팸"))
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, msg.from_user, Role.ADMIN, args, argstr), cmd)
    assert bot.named("restrict") and "2시간" in msg.replies[-1]


# ── 9. 텔레그램 오류 처리 ─────────────────────────────────────
class GoneQuery(FakeQuery):
    async def edit_message_reply_markup(self, markup=None):
        raise BadRequest("Message to edit not found")


@test
async def expired_confirm_button_survives_bad_request():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    q = GoneQuery(CHAT, fake_user(1), "act:nokey:y")
    await handlers._confirm_action(svc, FakeBot(), q, ["nokey", "y"])
    assert q.answers == [("만료된 요청이에요.", False)]


class ReplyCheckBot(ChatBot):
    """실제 텔레그램처럼: 원본이 지워졌는데 allow_sending_without_reply 없이 답장하면 BadRequest."""
    async def send_message(self, chat_id, text, **kw):
        rp = kw.get("reply_parameters")
        if rp is not None and not rp.allow_sending_without_reply:
            raise BadRequest("Message to be replied not found")
        return await super().send_message(chat_id, text, **kw)


@test
async def ai_answer_sent_even_if_original_deleted():
    r = await Room().open()
    r.bot = ReplyCheckBot()
    r.ctx.bot = r.bot
    r.llm.script = ["안녕하세요 대표님"]
    tg_bot = SimpleNamespace(defaults=None, send_message=r.bot.send_message)
    m = Message(55, datetime.now(timezone.utc), Chat(r.CHAT, "supergroup", title="대표님들 소통방"),
                from_user=User(10, "앨리스", False), text="소담아 안녕")
    m.set_bot(tg_bot)  # 진짜 PTB Message.reply_text 경로 (do_quote·reply_parameters 처리)
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)
    sent = [c for c in r.bot.named("send_message") if c[1] == r.CHAT]
    assert sent and "안녕하세요 대표님" in sent[-1][2], r.bot.calls
    assert sent[-1][3]["reply_parameters"].message_id == 55   # 원본이 있으면 여전히 답장으로


class NoNetBot(FakeBot):
    async def set_my_commands(self, *a, **kw):
        raise NetworkError("connection reset")

    async def set_my_short_description(self, *a, **kw):
        raise NetworkError("connection reset")

    async def set_my_description(self, *a, **kw):
        raise NetworkError("connection reset")


@test
async def startup_survives_set_my_commands_network_error():
    await sodam_main.set_profile(NoNetBot(), dealer=False)   # 예외가 밖으로 나오면 post_init → 봇 시작 실패


# ── 10. 1:1 무료 한도를 넘으면 AI 인젝션 판별(유료)도 안 부름 ──
class CountLLM(ScriptedLLM):
    async def classify_injection(self, text, chat_id=None):
        self.calls.append({"kind": "classify", "text": text})
        return False, ""


@test
async def dm_over_quota_skips_injection_classifier():
    db = await make_db()
    svc = await make_svc(db, free_ai_per_day=0)
    svc.llm = CountLLM(script=["답"])
    bot = ChatBot()
    ctx = _ctx(svc, bot)
    user = fake_user(50, "외부인")
    request = "가" * 200                                                       # 150자 초과 → 2층 판별 대상
    msg = FakeMsg(50, user, request)
    await handlers.ai_reply(ctx, msg, Role.MEMBER, request, security.scan(request))
    assert not svc.llm.of("classify"), "한도를 넘은 1:1 메시지에도 AI 판별 호출"
    assert not svc.llm.of("chat")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(1 if asyncio.run(run_all()) else 0)
