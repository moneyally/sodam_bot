"""스팸 우회 구멍·종류별 잠금·대량 입장 방어: python tests/run_all.py spam_locks_raid

모두 실제 핸들러(handlers.on_group_message / on_group_edit / handle_new_member / job_tick)로 돈다.
"""
import asyncio
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, Update, User
from telegram.ext import MessageHandler

from fake_llm import ChatBot, ScriptedLLM
from fakes import FakeJobQueue, FakeMsg, FakeQuery, add_member, fake_user, make_db, make_svc, runner

from sodam import handlers, menu, raid
from sodam.util import RateLimiter

test, run_all = runner()
CHAT = -1003334445
ADMIN, OLD, NEW = 1, 20, 30


async def _world(settings=None, *, admins=(ADMIN,), admin_users=None, can_moderate=True):
    db = await make_db()
    svc = await make_svc(db, admins=set(admins))
    svc.llm = ScriptedLLM()
    bot = ChatBot(admins=admin_users if admin_users is not None else [fake_user(a, "방장", "boss_admin") for a in admins])
    bot.can_moderate = can_moderate
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "limiter": RateLimiter(), "chats": {CHAT}, "cas_seen": set(),
                                    "tasks": set(), "joins": {}})
    await db.ensure_chat(CHAT, "업자 소통방")
    for k, v in {"cas_enabled": False, "flood_count": 100, **(settings or {})}.items():
        await db.set_setting(CHAT, k, v)
    await add_member(db, CHAT, fake_user(OLD, "기존멤버", "old_member"))              # 오래된 멤버 (입장 시각 없음)
    await add_member(db, CHAT, fake_user(NEW, "신입", "new_guy"), joined=True)         # 방금 들어온 사람
    bot.public_chats = {"spam_channel_kr": "channel", "some_channel": "channel", "other_group": "supergroup"}
    raid._joins.clear()
    return db, svc, bot, ctx


_mid = [500]


def _msg(uid, text="", name="멤버", username=None, **kw):
    _mid[0] += 1
    extra = {k: kw.pop(k) for k in list(kw) if k not in ("caption", "photo", "video", "animation", "document")}
    m = FakeMsg(CHAT, fake_user(uid, name, username), text, message_id=_mid[0], **kw)
    m.chat = SimpleNamespace(id=CHAT, title="업자 소통방", type="supergroup")
    m.sender_chat = None
    for k, v in extra.items():
        setattr(m, k, v)
    return m


async def _say(ctx, m):
    await handlers.on_group_message(SimpleNamespace(message=m), ctx)
    await asyncio.gather(*list(ctx.bot_data["tasks"]), return_exceptions=True)
    return m


async def _edit(ctx, m):
    await handlers.on_group_edit(SimpleNamespace(edited_message=m), ctx)
    return m


def _room_texts(bot):
    return [c[2] for c in bot.named("send_message") if c[1] == CHAT]


# ── 1a. 수정된 메시지 재검사 ──────────────────────────────
@test
async def edited_message_with_link_is_deleted():
    db, svc, bot, ctx = await _world()
    m = await _say(ctx, _msg(OLD, "안녕하세요"))
    assert not m.deleted
    m.text = "안녕하세요 https://spam-casino.xyz 로 오세요"  # 나중에 고쳐서 광고 넣기
    await _edit(ctx, m)
    assert m.deleted, "수정해서 넣은 링크가 안 지워짐"
    assert any("링크는 관리자만" in t for t in _room_texts(bot)), _room_texts(bot)


@test
async def edited_caption_banned_word_warns():
    db, svc, bot, ctx = await _world()
    await db.set_banned_word(CHAT, "도박", True)
    m = _msg(OLD, "", caption="사진 올려요", photo=[SimpleNamespace(file_id="p")])
    await _say(ctx, m)
    assert not m.deleted
    m.caption = "도박 사이트 추천"
    await _edit(ctx, m)
    assert m.deleted
    assert any("경고 1회" in t for t in _room_texts(bot)), _room_texts(bot)


@test
async def edited_message_skips_flood_ai_and_admins():
    db, svc, bot, ctx = await _world({"flood_count": 2})
    m = await _say(ctx, _msg(OLD, "처음 글"))
    for i in range(5):  # 여러 번 고쳐도 도배로 안 셈, AI 도 안 부름
        m.text = f"소담아 고친 글 {i}"
        await _edit(ctx, m)
    assert not m.deleted and not bot.named("restrict") and not svc.llm.calls
    a = await _say(ctx, _msg(ADMIN, "공지"))
    a.text = "공지 https://partner.example.com"
    await _edit(ctx, a)
    assert not a.deleted, "관리자 글은 검사 면제"


@test
async def edited_message_skipped_without_bot_rights():
    db, svc, bot, ctx = await _world(can_moderate=False)
    m = _msg(OLD, "https://spam.xyz")
    await _edit(ctx, m)
    assert not m.deleted and not _room_texts(bot)


@test
async def edited_message_handler_is_registered():
    """실제 앱 등록: 그룹의 edited_message 업데이트가 on_group_edit 로 간다."""
    added = []
    app = SimpleNamespace(add_handler=lambda h, group=0: added.append(h), add_error_handler=lambda fn: None,
                          job_queue=SimpleNamespace(run_repeating=lambda *a, **k: None, run_daily=lambda *a, **k: None))
    handlers.register(app, timezone.utc)
    tg_msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(CHAT, "supergroup"),
                     from_user=User(OLD, "기존멤버", False), text="https://spam.xyz")
    upd = Update(update_id=1, edited_message=tg_msg)
    hit = [h for h in added if isinstance(h, MessageHandler) and h.check_update(upd)]
    assert hit and hit[0].callback is handlers.on_group_edit, [getattr(h, "callback", h) for h in hit]


# ── 1b. 미디어 캡션도 텍스트와 똑같이 ─────────────────────
@test
async def media_caption_link_and_banned_word_checked():
    db, svc, bot, ctx = await _world()
    await db.set_banned_word(CHAT, "도박", True)
    for kind in ("photo", "video", "document"):
        media = [SimpleNamespace(file_id="x")] if kind == "photo" else SimpleNamespace(file_id="x")
        m = await _say(ctx, _msg(OLD, "", caption="문의 t.me/spam_shop", **{kind: media}))
        assert m.deleted, f"{kind} 캡션 링크가 안 지워짐"
    v = await _say(ctx, _msg(OLD, "", caption="도박 하실 분", voice=SimpleNamespace(file_id="v")))
    assert v.deleted, "음성 캡션 금지어가 안 지워짐"
    ok = await _say(ctx, _msg(OLD, "", caption="오늘 물건 사진", photo=[SimpleNamespace(file_id="p")]))
    assert not ok.deleted


# ── 1c. 전달(포워드) 메시지 ────────────────────────────────
FWD = SimpleNamespace(type="channel", chat=SimpleNamespace(id=-100999, title="광고채널"))


@test
async def forward_blocked_for_newbies_by_default():
    db, svc, bot, ctx = await _world()
    m = await _say(ctx, _msg(NEW, "좋은 정보", forward_origin=FWD))
    assert m.deleted, "신규 입장자 전달이 안 지워짐 (기본 '신규 입장자만')"
    assert any("전달(포워드)" in t and "24시간" in t for t in _room_texts(bot)), _room_texts(bot)
    old = await _say(ctx, _msg(OLD, "좋은 정보", forward_origin=FWD))
    assert not old.deleted, "기존 멤버 전달은 기본값에서 허용"
    auto = await _say(ctx, _msg(NEW, "채널 글", forward_origin=FWD, is_automatic_forward=True))
    assert not auto.deleted, "연결된 채널 자동 전달은 제외"


@test
async def forward_filter_all_and_off():
    db, svc, bot, ctx = await _world({"forward_filter": "all"})
    m = await _say(ctx, _msg(OLD, "", forward_origin=FWD, photo=[SimpleNamespace(file_id="p")]))
    assert m.deleted
    a = await _say(ctx, _msg(ADMIN, "", forward_origin=FWD))
    assert not a.deleted, "관리자 면제"
    await db.set_setting(CHAT, "forward_filter", "off")
    n = await _say(ctx, _msg(NEW, "", forward_origin=FWD))
    assert not n.deleted


# ── 1d. 이 방 멤버가 아닌 @아이디 홍보 ─────────────────────
@test
async def outside_mention_treated_like_link():
    db, svc, bot, ctx = await _world({"ai_enabled": False})  # '@sodambot' 은 봇 호출이라 AI 는 끔
    m = await _say(ctx, _msg(OLD, "문의는 @spam_channel_kr 로 주세요"))
    assert m.deleted, "외부 @아이디 홍보가 안 지워짐"
    assert any("@아이디" in t for t in _room_texts(bot)), _room_texts(bot)
    for text in ("@new_guy 님 반가워요", "@sodambot 도움말", "@BOSS_ADMIN 확인 부탁요", "사내메일 kim@company 로",
                 "@silent_member 님 오랜만이에요"):                  # 말 안 해서 DB 에 없는 사람 계정도 허용
        ok = await _say(ctx, _msg(OLD, text))
        assert not ok.deleted, f"오탐: {text}"
    for text in ("가입은 @other_group", "자동매매 @coin_signal_bot"):  # 다른 그룹·봇 홍보는 막음
        assert (await _say(ctx, _msg(OLD, text))).deleted, text


@test
async def outside_mention_follows_link_rules():
    db, svc, bot, ctx = await _world({"link_filter": False})
    old = await _say(ctx, _msg(OLD, "@some_channel 구독"))
    assert not old.deleted, "링크 차단이 꺼져 있으면 기존 멤버는 허용"
    new = await _say(ctx, _msg(NEW, "@some_channel 구독"))
    assert new.deleted, "신규 입장자 링크 금지 시간엔 막혀야 함"
    assert any("신규 입장 후엔" in t for t in _room_texts(bot)), _room_texts(bot)


# ── 2. 종류별 잠금 ─────────────────────────────────────────
@test
async def locked_kind_deleted_notice_once_per_10min():
    db, svc, bot, ctx = await _world({"lock_photo": True})
    svc.llm = ScriptedLLM(["안녕하세요"])
    p1 = await _say(ctx, _msg(OLD, "", caption="소담아 안녕", photo=[SimpleNamespace(file_id="p")]))
    p2 = await _say(ctx, _msg(OLD, "", caption="소담아 안녕", photo=[SimpleNamespace(file_id="p")]))
    assert p1.deleted and p2.deleted
    notes = [t for t in _room_texts(bot) if "사진 금지" in t]
    assert len(notes) == 1, notes
    assert not svc.llm.calls, "지운 메시지(안내 생략)에 AI 가 답함"
    other = await _say(ctx, _msg(NEW, "", photo=[SimpleNamespace(file_id="p")]))
    assert other.deleted and len([t for t in _room_texts(bot) if "사진 금지" in t]) == 2, "다른 사람은 따로 안내"
    txt = await _say(ctx, _msg(OLD, "글은 괜찮아요"))
    assert not txt.deleted


@test
async def each_lock_kind_detected():
    cases = {"lock_video": {"video": SimpleNamespace(file_id="v")},
             "lock_sticker": {"sticker": SimpleNamespace(file_id="s")},
             "lock_gif": {"animation": SimpleNamespace(file_id="g"), "document": SimpleNamespace(file_id="g")},
             "lock_voice": {"voice": SimpleNamespace(file_id="o")},
             "lock_document": {"document": SimpleNamespace(file_id="d")},
             "lock_poll": {"poll": SimpleNamespace(id="1")},
             "lock_contact": {"contact": SimpleNamespace(phone_number="010")},
             "lock_location": {"location": SimpleNamespace(latitude=1, longitude=2)},
             "lock_inline": {"via_bot": SimpleNamespace(id=5, username="gif")}}
    for key, attrs in cases.items():
        db, svc, bot, ctx = await _world({key: True})
        m = await _say(ctx, _msg(OLD, "", **attrs))
        assert m.deleted, f"{key} 잠금인데 안 지워짐"
        a = await _say(ctx, _msg(ADMIN, "", **attrs))
        assert not a.deleted, f"{key}: 관리자 면제"
    # GIF 는 document 도 채워져 오지만 '파일' 잠금에 걸리면 안 됨 / 잠금 기본은 전부 허용
    db, svc, bot, ctx = await _world({"lock_document": True})
    g = await _say(ctx, _msg(OLD, "", animation=SimpleNamespace(file_id="g"), document=SimpleNamespace(file_id="g")))
    assert not g.deleted
    db, svc, bot, ctx = await _world()
    s = await _say(ctx, _msg(OLD, "", sticker=SimpleNamespace(file_id="s")))
    assert not s.deleted


@test
async def locks_skipped_without_bot_rights():
    db, svc, bot, ctx = await _world({"lock_sticker": True, "forward_filter": "all"}, can_moderate=False)
    m = await _say(ctx, _msg(OLD, "", sticker=SimpleNamespace(file_id="s"), forward_origin=FWD))
    assert not m.deleted and not _room_texts(bot)


@test
async def lock_menu_toggle_changes_setting():
    db, svc, bot, ctx = await _world()
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"))
    await menu.on_callback(svc, bot, q, ["t", str(CHAT), "lock_sticker", "1"])
    assert (await db.get_settings(CHAT))["lock_sticker"] is True
    assert "🔒 스티커" in str(q.kb)
    q = FakeQuery(OLD, fake_user(OLD, "기존멤버"))
    await menu.on_callback(svc, bot, q, ["t", str(CHAT), "lock_sticker", "0"])
    assert (await db.get_settings(CHAT))["lock_sticker"] is True, "일반 멤버가 잠금을 바꿈"


# ── 3. 대량 입장 방어 ─────────────────────────────────────
async def _join(ctx, uid):
    await handlers.handle_new_member(ctx, CHAT, "업자 소통방", fake_user(uid, f"입장{uid}"))


def _dm_blocked_admins():
    return [fake_user(ADMIN, "방장", "boss_admin"), fake_user(2, "부방장"), fake_user(3, "차단한관리자"),
            fake_user(4, "관리봇", is_bot=True)]


@test
async def raid_triggers_captcha_notice_dm_and_report():
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "greet_enabled": False},
                                     admins=(ADMIN, 2, 3, 4), admin_users=_dm_blocked_admins())
    bot.dm_blocked = {3}

    async def owners():
        return {7}
    svc.perms.owners = owners
    for uid in range(100, 109):  # 9명: 아직 평소 (캡차 꺼짐 → 캡차 없음)
        await _join(ctx, uid)
    assert not await raid.active(svc, CHAT)
    assert not await svc.captcha.pending(CHAT, 108)
    await _join(ctx, 109)       # 10명째 → 방어 모드
    assert await raid.active(svc, CHAT)
    await _join(ctx, 110)
    assert await svc.captcha.pending(CHAT, 109) and await svc.captcha.pending(CHAT, 110), \
        "방어 모드인데 캡차 설정(꺼짐)을 따라 캡차가 안 걸림"
    room = [t for t in _room_texts(bot) if "방어 모드" in t]
    assert len(room) == 1, room
    dms = {c[1] for c in bot.named("send_message") if "방어 모드를" in c[2]}
    assert dms == {ADMIN, 2}, f"관리자 1:1 알림 대상: {dms} (봇·DM 막은 관리자 제외)"
    reports = [c for c in bot.named("send_message") if c[1] == 7 and "[대량 입장]" in c[2]]
    assert len(reports) == 1, reports
    for uid in range(111, 125):  # 방어 중 계속 들어와도 안내·보고는 한 번
        await _join(ctx, uid)
    assert len([t for t in _room_texts(bot) if "방어 모드" in t]) == 1
    assert len([c for c in bot.named("send_message") if c[1] == 7 and "[대량 입장]" in c[2]]) == 1


@test
async def raid_concurrent_joins_trigger_once():
    """봇은 업데이트를 동시에 처리(concurrent_updates) → 한꺼번에 몰려도 방어 모드·안내는 한 번."""
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "greet_enabled": False, "raid_count": 5})
    await asyncio.gather(*[_join(ctx, uid) for uid in range(500, 520)])
    assert await raid.active(svc, CHAT)
    room = [t for t in _room_texts(bot) if "방어 모드" in t]
    assert len(room) == 1, f"방 안내 {len(room)}번"


@test
async def raid_ends_automatically_on_tick():
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "greet_enabled": False, "raid_count": 3})
    for uid in (200, 201, 202):
        await _join(ctx, uid)
    st = await db.get_state(CHAT, raid.STATE)
    assert st and st["notice"]
    await db.set_state(CHAT, raid.STATE, {**st, "until": int(time.time()) - 1})  # 30분 지난 것으로
    await handlers.job_tick(SimpleNamespace(bot=bot, bot_data={"svc": svc}))
    assert await db.get_state(CHAT, raid.STATE) is None, "시간이 지났는데 방어 모드 기록이 남음"
    assert ("delete", CHAT, st["notice"]) in bot.calls, "방 안내가 안 지워짐"
    await _join(ctx, 203)
    assert not await svc.captcha.pending(CHAT, 203), "해제 뒤엔 원래 설정(캡차 꺼짐)대로"


@test
async def raid_guard_off_and_admins_not_counted():
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "raid_count": 3, "raid_guard": False})
    for uid in (300, 301, 302, 303):
        await _join(ctx, uid)
    assert not await raid.active(svc, CHAT), "자동 방어 꺼짐인데 켜짐"
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "raid_count": 3}, admins=(ADMIN, 2, 3, 4))
    for uid in (ADMIN, 2, 3, 4):
        await _join(ctx, uid)
    assert not await raid.active(svc, CHAT), "관리자 입장까지 셈"
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "raid_count": 3}, can_moderate=False)
    for uid in (310, 311, 312):
        await _join(ctx, uid)
    assert not await raid.active(svc, CHAT) and not _room_texts(bot), "권한 없는 방에서 방어 모드"


@test
async def raid_manual_on_off_from_menu():
    db, svc, bot, ctx = await _world({"captcha_enabled": False, "greet_enabled": False})
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"))
    await menu.on_callback(svc, bot, q, ["rdm", str(CHAT), "1"])
    assert await raid.active(svc, CHAT) and "방어 모드 끄기" in str(q.kb)
    await _join(ctx, 400)
    assert await svc.captcha.pending(CHAT, 400)
    q = FakeQuery(OLD, fake_user(OLD, "기존멤버"))
    await menu.on_callback(svc, bot, q, ["rdm", str(CHAT), "0"])
    assert await raid.active(svc, CHAT), "일반 멤버가 방어 모드를 끔"
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"))
    await menu.on_callback(svc, bot, q, ["rdm", str(CHAT), "0"])
    assert not await raid.active(svc, CHAT)


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
