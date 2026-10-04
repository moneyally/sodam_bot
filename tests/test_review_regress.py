"""Adversarial review repros. Each test asserts the CORRECT behaviour, so a FAIL = confirmed bug."""
import asyncio
import re
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, add_member, fake_user, make_db, make_svc, runner

from sodam import handlers, menu, tagnotify
from sodam.permissions import Role

test, run_all = runner()
CHAT = -1001111111
SECRET = -1009876543  # a private group the attacker is NOT in
ADMIN, ATTACKER = 1, 666
HUGE = "-99999999999999999999"   # passes CID_RE (-\d{5,20}) but > int64


async def _async(v):
    return v


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, "내 방")
    await db.ensure_chat(SECRET, "비밀 VIP 시그널방")
    svc.perms.is_admin = lambda bot, cid, uid: _async(cid == CHAT and uid == ADMIN)
    svc.perms.is_tg_admin = svc.perms.is_admin
    return db, svc, FakeBot()


async def press(svc, bot, data, uid=ADMIN):
    q = FakeQuery(uid, fake_user(uid, "x"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


# 1. double-press ✅ 저장 in the announce wizard → two schedules
@test
async def announce_save_double_press_creates_one_schedule():
    db, svc, bot = await setup()
    reason = await svc.announcer.start_dm(bot, ADMIN, CHAT)
    assert reason is None
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    for t in ("제목", "본문", "없음", "매일 09:00"):
        await handlers.on_private(SimpleNamespace(message=FakeMsg(ADMIN, fake_user(ADMIN), t)), ctx)
    d = svc.announcer.drafts[(ADMIN, ADMIN)]
    q0 = FakeQuery(ADMIN, fake_user(ADMIN), "")
    await svc.announcer.on_callback(bot, q0, [d.token, "pin0"])
    q1, q2 = FakeQuery(ADMIN, fake_user(ADMIN), ""), FakeQuery(ADMIN, fake_user(ADMIN), "")
    await asyncio.gather(svc.announcer.on_callback(bot, q1, [d.token, "save"]),
                         svc.announcer.on_callback(bot, q2, [d.token, "save"]))
    n = await db.count_schedules(CHAT)
    assert n == 1, f"double press saved {n} schedules"


# 2. any user can learn titles of arbitrary groups via m:tn
@test
async def tagnotify_toggle_leaks_title_of_foreign_group():
    db, svc, bot = await setup()
    q = await press(svc, bot, f"m:tn:0:{SECRET}:dm", uid=ATTACKER)
    toast = q.answers[0][0] or ""
    assert "비밀 VIP" not in toast, f"title leaked to non-member: {toast!r}"


# 3a. 20-digit chat id passes CID_RE and crashes the router (no q.answer)
@test
async def huge_chat_id_callback_does_not_crash():
    db, svc, bot = await setup()
    q = await press(svc, bot, f"m:g:{HUGE}", uid=ATTACKER)
    assert len(q.answers) == 1


# 3b. same via m:tn (PUBLIC)
@test
async def huge_chat_id_tagnotify_does_not_crash():
    db, svc, bot = await setup()
    q = await press(svc, bot, f"m:tn:1:{HUGE}", uid=ATTACKER)
    assert len(q.answers) == 1


# 3c. 20-digit doc id in kbv
@test
async def huge_doc_id_does_not_crash():
    db, svc, bot = await setup()
    q = await press(svc, bot, f"m:kbv:{CHAT}:99999999999999999999")
    assert len(q.answers) == 1


# 3d. deep link with huge id (real Permissions path would query sqlite; FakePerms here → check has_chat path)
@test
async def huge_chat_id_deeplink_does_not_crash():
    from sodam.permissions import Permissions
    db, svc, bot = await setup()
    svc.perms = Permissions(svc.cfg, db)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    msg = FakeMsg(ATTACKER, fake_user(ATTACKER), f"/start cfg_{HUGE}")
    await handlers.on_private(SimpleNamespace(message=msg), ctx)
    assert msg.replies


# 4. owner menu: m:ol with no page crashes (int(""))
@test
async def owner_rooms_without_page_does_not_crash():
    db, svc, bot = await setup()
    async def owners():
        return {ADMIN}
    svc.perms.owners = owners
    q = await press(svc, bot, "m:ol")
    assert len(q.answers) == 1


# 5. 1:1 wizard ignores a photo-only body (text-less media)
@test
async def dm_wizard_accepts_photo_without_caption():
    db, svc, bot = await setup()
    await svc.announcer.start_dm(bot, ADMIN, CHAT)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})
    await handlers.on_private(SimpleNamespace(message=FakeMsg(ADMIN, fake_user(ADMIN), "제목")), ctx)
    photo = FakeMsg(ADMIN, fake_user(ADMIN), "", photo=(SimpleNamespace(file_id="PH"),))
    await handlers.on_private(SimpleNamespace(message=photo), ctx)
    d = svc.announcer.drafts[(ADMIN, ADMIN)]
    assert d.step == "when" and d.media_id == "PH", f"step={d.step} media={d.media_id}"


# 6. an:x deletes ANY bot message it is attached to, including in groups, pressed by anyone
@test
async def an_x_only_closes_in_private():
    db, svc, bot = await setup()
    q = FakeQuery(CHAT, fake_user(ATTACKER), "an:x")
    q.message.message_id = 42          # e.g. the group's captcha / settings / greeting message
    await handlers.on_callback(SimpleNamespace(callback_query=q), SimpleNamespace(bot=bot, bot_data={"svc": svc}))
    assert ("delete", CHAT, 42) not in bot.calls, "member deleted a bot message in the group via forged an:x"


# 7. tag-notify rate-limit state grows forever (one key per sender/recipient)
@test
async def tagnotify_state_is_pruned():
    db, svc, bot = await setup()
    st = tagnotify.state(svc)
    t = [0.0]
    st.clock = lambda: t[0]
    for sid in range(10_000):
        st.sender_ok(sid)
        st.recipient_ok(sid + 1_000_000, CHAT)
    t[0] += 7200
    st.sender_ok(1)
    assert len(st.senders) + len(st.sent) < 1000, f"{len(st.senders)} sender keys, {len(st.sent)} recipient keys kept"


# 8. menu tokens: one admin can evict everyone else's pending confirmations (global MAX_TOKENS FIFO)
@test
async def token_flood_evicts_other_users_tokens():
    db, svc, bot = await setup()
    victim_tok = menu.token(svc, 7, CHAT, "del_bw", "x")
    for i in range(menu.MAX_TOKENS):
        menu.token(svc, ADMIN, CHAT, "ask_bw", str(i), menu.LIST_TOKEN_TTL)
    assert victim_tok in svc.menu_tokens, "victim's confirm button evicted by another user's list renders"


# 9. banned-words screen can exceed Telegram's 4096-char limit (200 words x 50 chars allowed)
@test
async def banned_words_screen_fits_4096():
    import html as H
    db, svc, bot = await setup()
    for i in range(menu.MAX_WORDS):
        await db.set_banned_word(CHAT, f"{i:03d}" + "가" * 47, True)
    q = await press(svc, bot, f"m:bw:{CHAT}")
    plain = H.unescape(re.sub(r"<[^>]+>", "", q.edits[-1]))
    assert len(plain) <= 4096, f"screen is {len(plain)} chars -> BadRequest 'message is too long'"


# 10. user who left/was banned keeps getting tag previews for up to MEMBER_TTL
@test
async def tag_notify_stops_after_leave():
    db, svc, bot = await setup()
    await add_member(db, CHAT, fake_user(10, "보낸이", "sender"))
    await add_member(db, CHAT, fake_user(20, "김", "kim"))
    await tagnotify.mark_started(db, 20)
    st = tagnotify.state(svc); t = [1000.0]; st.clock = lambda: t[0]
    def gmsg(mid):
        m = FakeMsg(CHAT, fake_user(10, "보낸이", "sender"), "답장입니다 비밀내용", message_id=mid,
                    reply_to=SimpleNamespace(from_user=fake_user(20, "김"), message_id=1, forum_topic_created=None))
        m.chat = SimpleNamespace(id=CHAT, title="방", username=None); m.sender_chat = None
        return m
    await tagnotify.on_group_message(svc, bot, gmsg(5), Role.MEMBER)
    bot.member_status = {(CHAT, 20): "kicked"}         # banned right after
    t[0] += 61
    before = len([c for c in bot.named("send_message") if c[1] == 20])
    await tagnotify.on_group_message(svc, bot, gmsg(6), Role.MEMBER)
    after = len([c for c in bot.named("send_message") if c[1] == 20])
    assert after == before, "banned user still received group content (member cache not invalidated on leave)"


# 11. bot-admin input with 20-digit numeric id crashes handle_input
@test
async def badm_huge_id_does_not_crash():
    db, svc, bot = await setup()
    await press(svc, bot, f"m:in:{CHAT}:badm")
    msg = FakeMsg(ADMIN, fake_user(ADMIN), "99999999999999999999")
    await menu.handle_input(svc, bot, msg)
    assert msg.replies



@test
def no_duplicate_top_level_definitions():
    """같은 모듈에 같은 이름의 함수·클래스가 두 번 있으면 뒤의 것만 쓰여서 고친 코드가 조용히 무시됨."""
    import ast
    from pathlib import Path
    dup = []
    for f in Path(__file__).resolve().parents[1].joinpath("sodam").rglob("*.py"):
        seen = set()
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    dup.append(f"{f.name}:{node.lineno} {node.name}")
                seen.add(node.name)
    assert not dup, dup

@test
async def member_join_hook_can_block_and_failures_do_not_break_join():
    from types import SimpleNamespace

    from fakes import FakeBot, FakeJobQueue, fake_user, make_db, make_svc

    from sodam import handlers, hooks
    db = await make_db()
    svc = await make_svc(db, admins={1})
    await db.set_setting(-100901, "captcha_enabled", False)
    ctx = SimpleNamespace(bot=FakeBot(), job_queue=FakeJobQueue(), bot_data={"svc": svc, "joins": {}, "chats": set()})
    seen = []

    async def boom(svc_, bot, chat_id, user):
        raise RuntimeError("훅 고장")

    async def block(svc_, bot, chat_id, user):
        seen.append(user.id)
        return user.id == 666
    for fn in (boom, block):
        hooks.add_member_join_hook(fn)
    try:
        await handlers.handle_new_member(ctx, -100901, "방", fake_user(666, "막힐사람"))
        await handlers.handle_new_member(ctx, -100901, "방", fake_user(777, "통과"))
    finally:  # 이 테스트가 넣은 것만 뺀다 (다른 모듈이 등록한 대량 입장 방어·공동 차단 훅은 그대로)
        for fn in (boom, block):
            hooks.MEMBER_JOIN_HOOKS.remove(fn)
    assert seen == [666, 777]
    assert svc.greeter.queued and all(q[1] == 777 for q in svc.greeter.queued)      # 막힌 사람은 인사 없음


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(run_all()))


@test
async def leave_event_clears_member_cache_immediately():
    from sodam import handlers
    db, svc, bot = await setup()
    st = tagnotify.state(svc)
    st.members[(CHAT, 20)] = (True, st.clock())
    ctx = SimpleNamespace(bot=bot, job_queue=None, bot_data={"svc": svc})

    def m(status):
        return SimpleNamespace(status=status, is_member=None, user=fake_user(20, "김"))
    upd = SimpleNamespace(chat_member=SimpleNamespace(chat=SimpleNamespace(id=CHAT, type="supergroup", title="방"),
                                                      old_chat_member=m("member"), new_chat_member=m("left")))
    await handlers.on_chat_member(upd, ctx)
    assert (CHAT, 20) not in st.members


@test
async def greet_tg_links_only_navigation():
    from sodam import greet
    assert greet.normalize_url("tg://resolve?domain=abc") and greet.normalize_url("tg://join?invite=x")
    for bad in ("tg://proxy?server=a&port=1&secret=b", "tg://socks?server=a", "tg://setlanguage?lang=x"):
        assert greet.normalize_url(bad) is None, bad


@test
async def migrate_moves_ai_memory():
    from sodam import memory  # noqa: F401  (테이블 등록)
    db, svc, bot = await setup()
    await db._write("INSERT INTO member_memory(chat_id, user_id, fact, ts) VALUES(?,?,?,?)", (CHAT, 5, "카페 운영", 1))
    await db._write("INSERT INTO room_memory(chat_id, summary, upto_id, updated_at) VALUES(?,?,?,?)", (CHAT, "요약", 1, 1))
    await db.migrate_chat(CHAT, -1007777777)
    assert await db._one("SELECT 1 FROM member_memory WHERE chat_id=?", (-1007777777,))
    assert await db._one("SELECT 1 FROM room_memory WHERE chat_id=?", (-1007777777,))
    assert not await db._one("SELECT 1 FROM room_memory WHERE chat_id=?", (CHAT,))


@test
async def greet_finds_member_by_nickname_but_sanctions_need_exact_name():
    from sodam import tools
    from sodam.permissions import Role
    db, svc, bot = await setup()
    woo = fake_user(5550001001, "하늘코인 거래소", "sky_trade")
    await db.upsert_user(woo)
    await db.touch_member(CHAT, woo.id)
    ctx = tools.ToolCtx(svc, bot, CHAT, fake_user(ADMIN), Role.ADMIN, await db.get_settings(CHAT))
    r = await tools.t_greet(ctx, {"names": ["하늘대표님"]})
    assert ctx.mentions == [(woo.id, "하늘코인 거래소")] and "원래 있던 멤버" in r
    row, err = await tools._resolve(ctx, "하늘대표님", for_sanction=True)   # 제재는 부분 이름으로 안 됨
    assert row is None and err
    row, err = await tools._resolve(ctx, "님")                              # 핵심이 너무 짧으면 안 찾음
    assert row is None
