"""버튼 메뉴 점검: python tests/test_menu.py"""
import asyncio
import sys
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from telegram.error import BadRequest

from sodam import commands, handlers, menu
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.security import normalize_domain

test, run_all = runner()
CHAT, OTHER = -1001111, -1002222
LONG = -100999999999999999  # 방 ID 가 길어도 콜백 64바이트 안에 들어가는지


async def setup(tg_admins=(1,), bot_admins=()):
    """uid 1 = CHAT·LONG 의 텔레그램 관리자. bot_admins = .봇관리자 로만 추가된 사람 (구독 화면 X)."""
    db = await make_db()
    svc = await make_svc(db, admins={1})
    for cid, title in ((CHAT, "내 방"), (OTHER, "남의 방"), (LONG, "긴 방")):
        await db.ensure_chat(cid, title)
    state = SimpleNamespace(tg=set(tg_admins), bot=set(bot_admins), forgets=0)
    svc.perms.is_admin = lambda bot, cid, uid: _async(cid in (CHAT, LONG) and uid in state.tg | state.bot)
    svc.perms.is_tg_admin = lambda bot, cid, uid: _async(cid in (CHAT, LONG) and uid in state.tg)

    def forget(cid):
        state.forgets += 1
    svc.perms.forget = forget
    return db, svc, FakeBot(), state


async def _async(v):
    return v


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


async def press(svc, bot, uid, data, name="방장"):
    """콜백 버튼 누르기. data 는 'm:...' 전체."""
    q = FakeQuery(uid, fake_user(uid, name), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


def find(kb, text):
    return next(b for b in buttons(kb) if text in b.text)


def with_billing(svc, db):
    from fakes import cfg
    from sodam.billing import Billing
    svc.cfg = cfg(db.path, pay_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    svc.billing = Billing(svc.cfg, db)


@test
async def main_menu_like_grouphelp():
    db, svc, bot, _ = await setup()
    text, kb = await menu.main_menu(svc, bot, 1)
    add = buttons(kb)[0]
    assert "추가" in add.text and "startgroup=true" in add.url and "restrict_members" in add.url  # 권한 미리 체크
    assert not any("오너" in b.text for b in buttons(kb))                # 일반 사용자 메뉴엔 오너 등록 없음
    text, kb = await menu.groups_menu(svc, bot, 1)
    labels = [b.text for b in buttons(kb)]
    assert any("내 방" in t for t in labels) and not any("남의 방" in t for t in labels)  # 관리하는 방만


@test
async def many_rooms_page_and_newest_first():
    """2026-10-10: 방 27개 오너에게 앞 20개만 보여서 오늘 들어온 '파멸' 방이 목록에 없었음 → 쪽 넘기기 + 최근 대화 순."""
    db = await make_db()
    svc = await make_svc(db)
    ids = [-1009000 - i for i in range(27)]
    for i, cid in enumerate(ids):
        await db.ensure_chat(cid, f"방{i:02d}")
    await db.log_message(ids[26], 5, 1, "오늘 첫 대화", ts=int(__import__("time").time()))   # 맨 마지막에 들어온 방
    svc.perms.candidate_chats = lambda uid: _async(ids)
    svc.perms.is_admin = lambda bot, cid, uid: _async(True)
    bot = FakeBot()
    text, kb = await menu.groups_menu(svc, bot, 1)
    labels = [b.text for b in buttons(kb)]
    assert labels[0] == "💬 방26", "최근 대화가 있는 새 방이 맨 위"
    assert "27개" in text and sum(t.startswith("💬") for t in labels) == menu.GROUPS_PAGE
    nxt = next(b for b in buttons(kb) if "다음" in b.text)
    page2 = [b.text for b in buttons((await menu.groups_menu(svc, bot, 1, 1))[1])]
    assert nxt.callback_data == "m:groups:1" and sum(t.startswith("💬") for t in page2) == 27 - menu.GROUPS_PAGE
    shown = {t for t in labels + page2 if t.startswith("💬")}
    assert len(shown) == 27, "모든 방이 어느 쪽엔가 보임"


@test
async def toggles_and_presets_admin_only():
    db, svc, bot, _ = await setup()
    assert (await db.get_settings(CHAT))["captcha_enabled"] is True

    q = await press(svc, bot, 1, f"m:j:{CHAT}")                         # 입장 화면
    cap_btn = next(b for b in buttons(q.kb) if "캡차" in b.text)
    assert cap_btn.callback_data == f"m:t:{CHAT}:captcha_enabled:0"      # 목표값(끄기)을 담음
    for _ in range(2):                                                   # 두 번 눌려도(재전송) 결과 같음
        q = await press(svc, bot, 1, cap_btn.callback_data)
    assert (await db.get_settings(CHAT))["captcha_enabled"] is False and "꺼짐" in q.answers[0][0]
    assert len(q.answers) == 1 and q.edits                               # 답은 정확히 1번, 패널은 새 상태로
    assert len(await db.recent_mod_log(CHAT)) == 1                       # 재전송은 기록도 한 번만

    q = await press(svc, bot, 20, f"m:t:{CHAT}:captcha_enabled")         # 관리자 아님
    assert "관리자만" in q.answers[0][0] and (await db.get_settings(CHAT))["captcha_enabled"] is False

    q = await press(svc, bot, 1, f"m:t:{OTHER}:ai_enabled:0")            # 남의 방 ID 로 위조
    assert "관리자만" in q.answers[0][0] and (await db.get_settings(OTHER))["ai_enabled"] is True

    q = FakeQuery(CHAT, fake_user(1, "방장"))                            # 그룹에서 누른 버튼
    await menu.on_callback(svc, bot, q, ["t", str(CHAT), "ai_enabled"])
    assert "1:1" in q.answers[0][0]

    await press(svc, bot, 1, f"m:t:{CHAT}:rules:1")                      # 목록에 없는 설정 키
    assert (await db.get_settings(CHAT))["rules"] == ""

    await press(svc, bot, 1, f"m:s:{CHAT}:free")
    assert (await db.get_settings(CHAT))["style"] == "free"
    q = await press(svc, bot, 1, f"m:fl:{CHAT}:strict")
    s = await db.get_settings(CHAT)
    assert (s["flood_count"], s["flood_mute_minutes"]) == (4, 60)
    assert any(b.text.startswith("● 도배 엄격") for b in buttons(q.kb))


@test
async def presets_whitelist_and_idempotent():
    db, svc, bot, _ = await setup()
    for _ in range(2):
        q = await press(svc, bot, 1, f"m:n:{CHAT}:captcha_minutes:10")
    assert (await db.get_settings(CHAT))["captcha_minutes"] == 10 and "10" in q.answers[0][0]
    assert find(q.kb, "10분").text.startswith("●")
    await press(svc, bot, 1, f"m:n:{CHAT}:captcha_action:ban")
    assert (await db.get_settings(CHAT))["captcha_action"] == "ban"

    # 화이트리스트 밖의 값·키는 무시 (범위 안이어도 버튼에 없는 값은 X)
    for data in (f"m:n:{CHAT}:captcha_minutes:7", f"m:n:{CHAT}:captcha_minutes:99999",
                 f"m:n:{CHAT}:captcha_action:nuke", f"m:n:{CHAT}:rules:hi", f"m:n:{CHAT}:flood_count:2"):
        q = await press(svc, bot, 1, data)
        assert len(q.answers) == 1 and not q.edits, data
    s = await db.get_settings(CHAT)
    assert (s["captcha_minutes"], s["captcha_action"], s["rules"], s["flood_count"]) == (10, "ban", "", 6)

    # 경고 단계: 밴 기준이 뮤트 기준 이하면 경고 문구
    await press(svc, bot, 1, f"m:n:{CHAT}:warn_mute_at:5")
    q = await press(svc, bot, 1, f"m:n:{CHAT}:warn_ban_at:3")
    s = await db.get_settings(CHAT)
    assert (s["warn_mute_at"], s["warn_ban_at"]) == (5, 3) and "바로 밴" in q.edits[-1]
    q = await press(svc, bot, 1, f"m:n:{CHAT}:warn_mute_minutes:1440")
    assert (await db.get_settings(CHAT))["warn_mute_minutes"] == 1440 and "1일" in q.edits[-1]


@test
async def strict_parsing_rejects_forged_callbacks():
    db, svc, bot, _ = await setup()
    for data in ("m:g:-1009999", "m:g:abc", "m:g:-1", "m:g:1001111", "m:g:", "m:g:--1001111", "m:g:-1001111²"):
        q = await press(svc, bot, 1, data)                              # 없는 방·형식 틀림
        assert "없는 그룹" in q.answers[0][0] and not q.edits, data
    q = await press(svc, bot, 1, f"m:zz:{CHAT}")                         # 모르는 코드
    assert q.answers == [(None, False)] and not q.edits
    q = await press(svc, bot, 1, "m:k:nosuchtoken")                      # 없는 토큰
    assert "만료" in q.answers[0][0]


@test
async def all_callbacks_fit_64_bytes():
    db, svc, bot, _ = await setup()
    await db.set_banned_word(LONG, "가" * 50, True)                      # 긴 금지어도 토큰이라 짧음
    await db.set_setting(LONG, "whitelist_domains", ["a" * 60 + ".com"])
    seen = 0
    for code in ("g", "f", "j", "sec", "wl", "bw", "dom", "st", "in:bw", "in:dom"):
        code, _, extra = code.partition(":")
        q = await press(svc, bot, 1, f"m:{code}:{LONG}" + (f":{extra}" if extra else ""))
        assert q.edits, code
        for b in buttons(q.kb):
            if b.callback_data:
                seen += 1
                assert len(b.callback_data.encode()) <= 64, b.callback_data
    assert seen > 40


@test
async def demoted_admin_blocked_immediately():
    db, svc, bot, state = await setup()
    q = await press(svc, bot, 1, f"m:sec:{CHAT}")
    link_btn = find(q.kb, "링크 차단")
    state.tg.discard(1)                                                  # 관리자에서 내려옴
    q = await press(svc, bot, 1, link_btn.callback_data)
    assert "관리자만" in q.answers[0][0] and (await db.get_settings(CHAT))["link_filter"] is True
    q = await press(svc, bot, 1, f"m:g:{CHAT}")
    assert "관리자만" in q.answers[0][0] and not q.edits


@test
async def subscription_hidden_from_non_tg_admins():
    db, svc, bot, state = await setup(bot_admins={30})
    with_billing(svc, db)
    await svc.billing.ensure_trial(CHAT, 1)
    q = await press(svc, bot, 1, f"m:g:{CHAT}")                          # 텔레그램 관리자: 💳 보임
    assert any("구독" in b.text for b in buttons(q.kb))
    q = await press(svc, bot, 30, f"m:g:{CHAT}")                         # 봇관리자(.봇관리자): 설정은 되지만 💳 없음
    assert q.edits and not any("구독" in b.text or "💳" in b.text for b in buttons(q.kb))
    assert "USDT" not in q.edits[-1] and "체험" not in q.edits[-1]
    q = await press(svc, bot, 30, f"m:sub:{CHAT}")                       # 직접 눌러도 거부
    assert "관리자만" in q.answers[0][0] and not q.edits
    before = state.forgets
    q = await press(svc, bot, 1, f"m:sub:{CHAT}")                        # 결제 화면은 캐시 무시하고 확인
    assert "USDT" in q.edits[-1] and state.forgets == before + 1
    # 결제 꺼져 있으면 아무도 안 보임
    db2, svc2, bot2, _ = await setup()
    q = await press(svc2, bot2, 1, f"m:g:{CHAT}")
    assert not any("구독" in b.text for b in buttons(q.kb))


@test
async def banned_word_delete_uses_one_time_token():
    db, svc, bot, state = await setup()
    await db.set_banned_word(CHAT, "나쁜말", True)
    q = await press(svc, bot, 1, f"m:bw:{CHAT}")
    ask = find(q.kb, "나쁜말")
    assert ask.callback_data.startswith("m:k:") and "나쁜말" not in ask.callback_data
    q = await press(svc, bot, 99, ask.callback_data)                     # 다른 사람이 토큰을 써도
    assert "요청한 사람만" in q.answers[0][0]                            # (토큰 주인만)
    q = await press(svc, bot, 1, f"m:bw:{CHAT}")
    ask = find(q.kb, "나쁜말")
    q = await press(svc, bot, 1, ask.callback_data)                      # 확인 화면
    confirm = find(q.kb, "삭제")
    assert "삭제할까요" in q.edits[-1]
    before = state.forgets
    q = await press(svc, bot, 1, confirm.callback_data)
    assert await db.banned_words(CHAT) == [] and "삭제" in q.answers[0][0]
    assert state.forgets == before + 1                                   # 삭제는 권한을 새로 확인
    q = await press(svc, bot, 1, confirm.callback_data)                  # 재전송
    assert "만료" in q.answers[0][0]

    # 확인 화면이 떠 있는 사이 강등되면 삭제 안 됨
    await db.set_banned_word(CHAT, "욕", True)
    q = await press(svc, bot, 1, f"m:bw:{CHAT}")
    q = await press(svc, bot, 1, find(q.kb, "욕").callback_data)
    confirm = find(q.kb, "삭제")
    state.tg.discard(1)
    q = await press(svc, bot, 1, confirm.callback_data)
    assert "관리자만" in q.answers[0][0] and await db.banned_words(CHAT) == ["욕"]

    # 만료된 토큰
    state.tg.add(1)
    q = await press(svc, bot, 1, f"m:bw:{CHAT}")
    tok = find(q.kb, "욕").callback_data.split(":")[2]
    svc.menu_tokens[tok].expires = time.time() - 1
    assert "만료" in (await press(svc, bot, 1, f"m:k:{tok}")).answers[0][0]


@test
async def text_input_flow_for_words_and_domains():
    db, svc, bot, state = await setup()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    admin = fake_user(1, "방장")
    ai_calls = []

    async def fake_ai(*a, **kw):
        ai_calls.append(a)
    orig_ai, handlers.ai_reply = handlers.ai_reply, fake_ai

    async def dm(text="", **kw):
        msg = FakeMsg(1, admin, text, **kw)
        await handlers.on_private(SimpleNamespace(message=msg), ctx)
        return msg.replies

    try:
        q = await press(svc, bot, 1, f"m:in:{CHAT}:bw")
        assert "금지어" in q.edits[-1] and 1 in svc.inputs
        assert "추가했어요" in (await dm("스팸, 광고\n도박, 스팸"))[0]    # 쉼표·줄바꿈, 중복 제거
        assert sorted(await db.banned_words(CHAT)) == ["광고", "도박", "스팸"] and 1 not in svc.inputs
        assert not ai_calls                                              # AI 로 새지 않음

        await press(svc, bot, 1, f"m:in:{CHAT}:dom")
        assert "형식" in (await dm("<b>not a domain"))[0] and 1 in svc.inputs  # 틀리면 다시 받음
        assert "글자로" in (await dm(photo=[SimpleNamespace(file_id="p")]))[0]  # 사진만 온 경우
        replies = await dm(".도움말")                                     # 명령어는 통과
        assert 1 in svc.inputs and replies and "추가했어요" not in replies[0]
        await dm("https://www.YouTube.com/watch?v=1, bad!!")
        assert (await db.get_settings(CHAT))["whitelist_domains"] == ["youtube.com"]

        await press(svc, bot, 1, f"m:in:{CHAT}:bw")                      # 취소
        assert "취소" in (await dm("취소"))[0] and 1 not in svc.inputs
        await press(svc, bot, 1, f"m:in:{CHAT}:bw")                      # 다른 버튼 누르면 입력 대기 취소
        await press(svc, bot, 1, f"m:sec:{CHAT}")
        assert 1 not in svc.inputs

        await press(svc, bot, 1, f"m:in:{CHAT}:bw")                      # 시간 초과 (방금 지남 → 안내)
        svc.inputs[1].expires = time.time() - 1
        assert "시간" in (await dm("늦은단어"))[0] and "늦은단어" not in await db.banned_words(CHAT)
        await press(svc, bot, 1, f"m:in:{CHAT}:bw")                      # 한참 지남 → 평범한 대화로
        svc.inputs[1].expires = time.time() - 3600
        await dm("안녕")
        assert ai_calls and 1 not in svc.inputs

        await press(svc, bot, 1, f"m:in:{CHAT}:bw")                      # 입력 대기 중 강등
        state.tg.discard(1)
        assert "관리자만" in (await dm("몰래추가"))[0] and "몰래추가" not in await db.banned_words(CHAT)
    finally:
        handlers.ai_reply = orig_ai


@test
async def input_and_announce_wizard_cancel_each_other():
    from sodam.announce import Draft
    db, svc, bot, _ = await setup()
    svc.announcer.drafts[(1, 1)] = Draft(1, 1)
    await press(svc, bot, 1, f"m:in:{CHAT}:bw")
    assert (1, 1) not in svc.announcer.drafts and 1 in svc.inputs
    await svc.announcer.start(bot, FakeMsg(1, fake_user(1, "방장"), ".예약공지"))
    assert 1 not in svc.inputs and (1, 1) in svc.announcer.drafts


@test
async def edit_failures_fall_back_and_answer_once():
    db, svc, bot, _ = await setup()

    class Q(FakeQuery):
        def __init__(self, err):
            super().__init__(1, fake_user(1, "방장"))
            self.err = err

        async def edit_message_text(self, text, **kw):
            raise BadRequest(self.err)

    q = Q("Message is not modified: specified new message content ...")
    await menu.on_callback(svc, bot, q, ["g", str(CHAT)])
    assert len(q.answers) == 1 and not bot.named("send_message")
    q = Q("Message to edit not found")
    await menu.on_callback(svc, bot, q, ["g", str(CHAT)])
    assert len(q.answers) == 1 and bot.named("send_message")[-1][1] == 1  # 새 메시지로


@test
async def callback_rate_limit():
    db, svc, bot, _ = await setup()
    answers = [(await press(svc, bot, 1, "m:home")).answers[0][0] for _ in range(menu.CALLBACK_PER_MIN + 1)]
    assert answers[-1] and "빨리" in answers[-1] and answers[0] is None


@test
async def private_start_and_deep_link():
    db, svc, bot, state = await setup()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})

    async def dm(user, text):
        msg = FakeMsg(user.id, user, text)
        await handlers.on_private(SimpleNamespace(message=msg), ctx)
        return msg.replies

    admin, member = fake_user(1, "방장"), fake_user(20, "멤버")
    welcome = (await dm(member, "/start"))[0]
    assert "시작하는 방법" in welcome and "1️⃣" in welcome and "USDT" not in welcome  # 첫 안내 문구
    assert "내 방</b> 관리" in (await dm(admin, f"/start cfg_{CHAT}"))[0]
    reply = (await dm(member, f"/start cfg_{CHAT}"))[0]                 # 멤버: 거절 대신 일반 메뉴
    assert "관리자용" in reply and "USDT" not in reply and "내 방" not in reply
    assert "관리자용" in (await dm(admin, f"/start sub_{OTHER}"))[0]     # 관리하지 않는 방
    # 회귀: 딥링크 연타가 매번 관리자 캐시를 지우지 않음 (getChatAdministrators 과다 호출 방지)
    before = state.forgets
    for _ in range(5):
        await dm(admin, f"/start sub_{CHAT}")
    assert state.forgets == before


@test
async def non_admin_inviter_never_gets_price():
    db, svc, bot, state = await setup()
    with_billing(svc, db)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc, "chats": set()})

    def m(status):
        return SimpleNamespace(status=status, is_member=None, user=SimpleNamespace(id=bot.id))

    state.bot.add(30)
    for adder_id, expect_dm in ((20, False), (30, False), (1, True)):   # 20=멤버, 30=봇관리자, 1=텔레그램 관리자
        bot.calls.clear()
        before = state.forgets
        upd = SimpleNamespace(my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT, type="supergroup", title="내 방"), from_user=fake_user(adder_id, "초대자"),
            old_chat_member=m("left"), new_chat_member=m("member")))
        await handlers.on_my_chat_member(upd, ctx)
        dms = [c for c in bot.named("send_message") if c[1] == adder_id]
        assert bool(dms) is expect_dm, adder_id
        assert state.forgets == before + 1                              # 결제 노출 판단은 새로 확인
        assert not any("USDT" in c[2] for c in bot.named("send_message") if c[1] == CHAT)


@test
async def settings_command_sends_panel_privately():
    db, svc, bot, _ = await setup()
    admin = fake_user(1, "방장")
    cmd, args, argstr = commands.parse(".설정", "sodambot")
    msg = FakeMsg(CHAT, admin, ".설정")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    dm = next(c for c in bot.named("send_message") if c[1] == 1)
    assert dm[3]["reply_markup"] is not None and "🛡️ 보안" in [b.text for b in buttons(dm[3]["reply_markup"])]
    assert msg.deleted and "1:1" in [c for c in bot.named("send_message") if c[1] == CHAT][-1][2]
    cmd, args, argstr = commands.parse(".설정 전체", "sodambot")
    msg = FakeMsg(CHAT, admin, ".설정 전체")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    assert "flood_count" in msg.replies[0]


# ── 회귀: 이상한 숫자 입력 ('²' 는 isdigit() 이 True 인데 int() 가 터짐) ──
@test
async def weird_digits_do_not_crash():
    db, svc, bot, _ = await setup()
    admin = fake_user(1, "방장")
    for text in (".밴해제 ²", ".cas 확인 ²", ".예약공지 수정 ²", ".밴해제 -5"):
        cmd, args, argstr = commands.parse(text, "sodambot")
        msg = FakeMsg(CHAT, admin, text)
        await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
        assert msg.replies, text                                        # 예외 없이 안내
    assert not bot.named("unban")
    assert await db.find_members(CHAT, "²") == []
    await svc.games.start(bot, CHAT, 1, "끝말잇기")                       # 업다운 진행 중에 '²' 입력
    assert svc.games.is_active(CHAT)
    assert not await svc.games.on_text(FakeMsg(CHAT, admin, "²"), "²")
    svc.games.active.pop(CHAT).cancel_timer()
    assert normalize_domain("https://www.YouTube.com/x") == "youtube.com" and normalize_domain("<b>") is None


@test
async def only_one_live_panel_per_user():
    db, svc, bot, _ = await setup()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})

    async def dm(text, mid):
        msg = FakeMsg(1, fake_user(1, "방장"), text, message_id=mid)
        await handlers.on_private(SimpleNamespace(message=msg), ctx)

    await dm("/start", 1)                                                # 메뉴 A (id 10001)
    assert not bot.named("edit_markup")
    await dm(f"/start cfg_{CHAT}", 2)                                    # 메뉴 B → A 의 버튼 제거
    assert bot.named("edit_markup") == [("edit_markup", 1, 10001, None)]
    q = FakeQuery(1, fake_user(1, "방장"))
    q.message.message_id = 10002                                         # B 에서 버튼 누름 → B 가 살아있는 메뉴
    await menu.on_callback(svc, bot, q, ["sec", str(CHAT)])
    await press(svc, bot, 1, f"m:in:{CHAT}:bw")
    await dm("새단어", 3)                                                 # 입력 결과로 새 메뉴 → B 버튼 제거
    assert bot.named("edit_markup")[-1][2] == 10002


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
