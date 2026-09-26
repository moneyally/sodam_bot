"""태그·답장 알림 점검: python tests/test_tagnotify.py"""
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, add_member, fake_user, make_db, make_svc, runner
from harness import html_errors

from sodam import handlers, menu, tagnotify
from sodam.permissions import Role

test, run_all = runner()
CHAT, OTHER, BASIC = -1001111111, -1002222222, -123456
SENDER, KIM, LEE = 10, 20, 30


async def setup():
    """SENDER·KIM 은 CHAT 멤버, LEE 는 OTHER 멤버만. KIM·LEE 는 봇과 1:1 을 시작한 상태."""
    db = await make_db()
    svc = await make_svc(db)
    for cid, title in ((CHAT, "대표님 소통방"), (OTHER, "다른 방"), (BASIC, "일반 그룹")):
        await db.ensure_chat(cid, title)
    await add_member(db, CHAT, fake_user(SENDER, "보낸이", "sender"))
    await add_member(db, CHAT, fake_user(KIM, "김대표", "kim"))
    await add_member(db, BASIC, fake_user(KIM, "김대표", "kim"))
    await add_member(db, OTHER, fake_user(LEE, "이대표", "lee"))
    for uid in (KIM, LEE):
        await tagnotify.mark_started(db, uid)
    t = [1000.0]
    tagnotify.state(svc).clock = lambda: t[0]
    return db, svc, FakeBot(), t


def gmsg(text, *, chat=CHAT, sender=SENDER, entities=(), reply_to=None, message_id=5, username=None,
         title="대표님 소통방", caption=False):
    m = FakeMsg(chat, fake_user(sender, "보낸이 <b>", "sender"), "" if caption else text,
                caption=text if caption else None, message_id=message_id, reply_to=reply_to)
    if caption:
        m.caption_entities = tuple(entities)
    else:
        m.entities = tuple(entities)
    m.chat = SimpleNamespace(id=chat, title=title, username=username)
    m.sender_chat = None
    return m


def ment(text, word):
    """text 안의 @word 멘션 entity (UTF-16 위치)."""
    i = text.index("@" + word)
    off = len(text[:i].encode("utf-16-le")) // 2
    return SimpleNamespace(type="mention", offset=off, length=len(word) + 1, user=None)


def dms(bot, uid=None):
    return [c for c in bot.named("send_message") if c[1] > 0 and (uid is None or c[1] == uid)]


async def notify(svc, bot, msg):
    await tagnotify.on_group_message(svc, bot, msg, Role.MEMBER)


def kb_buttons(call):
    return [b for row in call[3]["reply_markup"].inline_keyboard for b in row]


@test
async def mention_member_gets_dm_with_link_and_escape():
    db, svc, bot, _ = await setup()
    text = "🎉 @kim 내일 <회의> & 점심 " + "가" * 300
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")]))   # 이모지 뒤 멘션 = UTF-16 위치
    [call] = dms(bot, KIM)
    body = call[2]
    assert body.startswith("🔔 <b>태그되었어요</b>") and "대표님 소통방" in body
    assert "&lt;회의&gt; &amp; 점심" in body and "<회의>" not in body            # 내용 이스케이프
    assert "보낸이 &lt;b&gt;" in body and not html_errors(body)                 # 보낸 사람 이름도
    assert body.count("가") == 200 - len("🎉 @kim 내일 <회의> & 점심 ") and "…" in body  # 200자
    btns = kb_buttons(call)
    assert btns[0].url == "https://t.me/c/1111111/5"
    assert btns[1].callback_data == f"m:tn:0:{CHAT}:dm" and len(btns[1].callback_data.encode()) <= 64


@test
async def username_of_non_member_ignored_and_case_insensitive():
    db, svc, bot, _ = await setup()
    text = "@LEE @Kim 둘 다 봐줘"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "LEE"), ment(text, "Kim")]))
    assert [c[1] for c in dms(bot)] == [KIM]                                     # LEE 는 이 방 멤버가 아님


@test
async def text_mention_and_caption():
    db, svc, bot, _ = await setup()
    await tagnotify.mark_started(db, 40)
    text = "박대표님 사진 확인요"
    e = SimpleNamespace(type="text_mention", offset=0, length=3, user=fake_user(40, "박대표"))
    await notify(svc, bot, gmsg(text, entities=[e], caption=True))
    assert len(dms(bot, 40)) == 1
    bot_e = SimpleNamespace(type="text_mention", offset=0, length=3, user=fake_user(41, "봇", is_bot=True))
    await tagnotify.mark_started(db, 41)
    await notify(svc, bot, gmsg(text, entities=[bot_e], message_id=6))
    assert not dms(bot, 41)                                                      # 봇은 제외


@test
async def reply_notifies_author_but_not_self_bot_or_topic():
    db, svc, bot, t = await setup()
    orig = FakeMsg(CHAT, fake_user(KIM, "김대표", "kim"), "원래 글", message_id=3)
    await notify(svc, bot, gmsg("동의해요", reply_to=orig))
    [call] = dms(bot, KIM)
    assert call[2].startswith("↩️ <b>답장이 왔어요</b>")
    t[0] += 120
    await notify(svc, bot, gmsg("내 글에 답장", sender=KIM, reply_to=orig, message_id=7))   # 자기 글에 답장
    botmsg = FakeMsg(CHAT, fake_user(999, "소담", is_bot=True), "봇 글", message_id=4)
    await notify(svc, bot, gmsg("봇에게 답장", reply_to=botmsg, message_id=8))
    topic = FakeMsg(CHAT, fake_user(KIM, "김대표", "kim"), "", message_id=2)
    topic.forum_topic_created = SimpleNamespace(name="공지")                       # 포럼 토픽 안의 글
    await notify(svc, bot, gmsg("토픽 글", reply_to=topic, message_id=9))
    assert len(dms(bot)) == 1
    # 태그와 답장이 같은 사람이면 한 번만 (태그 우선)
    t[0] += 120
    text = "@kim 이거요"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], reply_to=orig, message_id=10))
    assert len(dms(bot, KIM)) == 2 and dms(bot, KIM)[-1][2].startswith("🔔")


@test
async def sender_self_mention_and_commands_skipped():
    db, svc, bot, _ = await setup()
    text = "@sender 나야"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "sender")]))
    text = ".경고 @kim 도배"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")]))
    assert not dms(bot)


@test
async def left_member_skipped_with_cache():
    db, svc, bot, t = await setup()
    bot.member_status = {(CHAT, KIM): "left"}
    text = "@kim 있어?"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")]))
    assert not dms(bot) and len(bot.named("get_chat_member")) == 1
    bot.member_status = {}
    t[0] += 120
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=6))
    assert not dms(bot) and len(bot.named("get_chat_member")) == 1              # 10분 캐시
    t[0] += 600
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=7))
    assert len(dms(bot)) == 1                                                   # 캐시 만료 → 다시 확인
    bot.member_status = {(CHAT, 40): "kicked"}
    await tagnotify.mark_started(db, 40)
    e = SimpleNamespace(type="text_mention", offset=0, length=2, user=fake_user(40, "강퇴"))
    await notify(svc, bot, gmsg("강퇴 씨", entities=[e], message_id=8))
    assert not dms(bot, 40)


@test
async def not_started_dm_or_room_off_no_notification():
    db, svc, bot, _ = await setup()
    await add_member(db, CHAT, fake_user(50, "처음", "newbie"))              # 봇과 1:1 안 함
    text = "@newbie 안녕"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "newbie")]))
    assert not dms(bot)
    await db.touch_member(50, 50)                                            # 1:1 로 말 걸면(on_private) 받을 수 있음
    await notify(svc, bot, gmsg(text, entities=[ment(text, "newbie")], message_id=6))
    assert len(dms(bot, 50)) == 1
    await db.set_setting(CHAT, "tag_notify", False)                          # 방 설정으로 끔
    text = "@kim 안녕"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=7))
    assert not dms(bot, KIM)


@test
async def opt_out_button_idempotent_and_menu():
    db, svc, bot, t = await setup()
    text = "@kim 안녕"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")]))
    data = kb_buttons(dms(bot, KIM)[0])[1].callback_data
    for _ in range(2):                                                       # 두 번 눌러도 같은 결과
        q = FakeQuery(KIM, fake_user(KIM, "김대표"), data)
        await menu.on_callback(svc, bot, q, data.split(":")[1:])
        assert q.answers[0][1] and "껐어요" in q.answers[0][0] and not q.edits  # 알림 글은 그대로, 토스트만
    assert len(await db._all("SELECT * FROM tag_optout")) == 1
    t[0] += 120
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=6))
    assert len(dms(bot, KIM)) == 1
    # 메뉴에서 다시 켜기
    q = FakeQuery(KIM, fake_user(KIM, "김대표"), "m:tn")
    await menu.on_callback(svc, bot, q, ["tn"])
    labels = [b.text for row in q.kb.inline_keyboard for b in row]
    assert "❌ 대표님 소통방" in labels and "✅ 일반 그룹" in labels
    on = next(b.callback_data for row in q.kb.inline_keyboard for b in row if b.text == "❌ 대표님 소통방")
    assert on == f"m:tn:1:{CHAT}"
    for _ in range(2):
        q = FakeQuery(KIM, fake_user(KIM, "김대표"), on)
        await menu.on_callback(svc, bot, q, on.split(":")[1:])
    assert "✅ 대표님 소통방" in [b.text for row in q.kb.inline_keyboard for b in row]
    assert not await db._all("SELECT * FROM tag_optout")
    t[0] += 120
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=7))
    assert len(dms(bot, KIM)) == 2
    # 위조: 없는 방 / 이상한 값 → 거절, 저장 안 함
    for bad in ("m:tn:0:-1009999", "m:tn:2:" + str(CHAT), "m:tn:0:abc", "m:tn:0:" + str(KIM)):
        q = FakeQuery(KIM, fake_user(KIM, "김대표"), bad)
        await menu.on_callback(svc, bot, q, bad.split(":")[1:])
        assert q.answers[0][1] and not q.edits, bad
    assert not await db._all("SELECT * FROM tag_optout")
    # 다른 사람 설정은 그대로 (자기 것만 바뀜)
    await tagnotify.set_opt_out(db, LEE, CHAT, True)
    q = FakeQuery(KIM, fake_user(KIM, "김대표"), f"m:tn:1:{CHAT}")
    await menu.on_callback(svc, bot, q, ["tn", "1", str(CHAT)])
    assert await tagnotify.opted_out(db, LEE, CHAT)


@test
async def rate_limits():
    db, svc, bot, t = await setup()
    uids = list(range(100, 112))
    for u in uids:
        await add_member(db, CHAT, fake_user(u, f"멤버{u}", f"m{u}"))
        await tagnotify.mark_started(db, u)
    # 메시지 하나에 7명 태그 → 5명만
    text = " ".join(f"@m{u}" for u in uids[:7])
    await notify(svc, bot, gmsg(text, entities=[ment(text, f"m{u}") for u in uids[:7]]))
    assert [c[1] for c in dms(bot)] == uids[:5]
    # 같은 수신자·같은 방 60초 1회
    t[0] += 30
    text = "@kim 1"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=6))
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=7))
    assert len(dms(bot, KIM)) == 1
    # 보낸 사람 분당 10회: 이미 6번 보냄 → 4명 더만
    text = " ".join(f"@m{u}" for u in uids[5:10])
    await notify(svc, bot, gmsg(text, entities=[ment(text, f"m{u}") for u in uids[5:10]], message_id=8))
    text = "@m110 @m111"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "m110"), ment(text, "m111")], message_id=9))
    assert len(dms(bot)) == 10
    # 1분 지나면 다시 가능
    t[0] += 61
    await notify(svc, bot, gmsg(text, entities=[ment(text, "m110"), ment(text, "m111")], message_id=10))
    assert len(dms(bot)) == 12


@test
async def recipient_hourly_cap():
    db, svc, bot, t = await setup()
    senders = list(range(200, 225))
    for s in senders:
        await add_member(db, CHAT, fake_user(s, f"보낸{s}"))
    text = "@kim 확인"
    for i, s in enumerate(senders):
        t[0] += 61
        await notify(svc, bot, gmsg(text, sender=s, entities=[ment(text, "kim")], message_id=100 + i))
    assert len(dms(bot, KIM)) == 20                                          # 시간당 20회
    t[0] += 3600
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=999))
    assert len(dms(bot, KIM)) == 21


@test
async def forbidden_marks_dm_blocked():
    db, svc, bot, t = await setup()
    bot.dm_blocked = {KIM}
    text = "@kim 안녕"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")]))
    assert not await tagnotify.dm_ok(db, KIM)
    bot.dm_blocked = set()
    t[0] += 120
    members_checked = len(bot.named("get_chat_member"))
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=6))
    assert not dms(bot) and len(bot.named("get_chat_member")) == members_checked  # 더 시도하지 않음
    q = FakeQuery(KIM, fake_user(KIM, "김대표"), "m:tn")                       # 다시 1:1 메뉴를 열면 풀림
    await menu.on_callback(svc, bot, q, ["tn"])
    assert await tagnotify.dm_ok(db, KIM)
    t[0] += 120
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], message_id=7))
    assert len(dms(bot, KIM)) == 1


@test
async def link_formats():
    assert tagnotify.message_link(-1001234567890, None, 42) == "https://t.me/c/1234567890/42"
    assert tagnotify.message_link(-1001234567890, "public_room", 42) == "https://t.me/public_room/42"
    assert tagnotify.message_link(-123456, None, 42) is None                  # 일반 그룹은 링크 없음
    db, svc, bot, _ = await setup()
    text = "@kim 공개방"
    await notify(svc, bot, gmsg(text, entities=[ment(text, "kim")], username="public_room"))
    assert kb_buttons(dms(bot, KIM)[0])[0].url == "https://t.me/public_room/5"
    await notify(svc, bot, gmsg(text, chat=BASIC, entities=[ment(text, "kim")], message_id=6, title="일반 그룹"))
    btns = kb_buttons(dms(bot, KIM)[1])
    assert [b.text for b in btns] == ["🔕 이 그룹 알림 끄기"]                   # 이동 버튼 없음


@test
async def feature_screen_room_toggle():
    db, svc, bot, _ = await setup()
    svc.perms.admins = {1}
    q = FakeQuery(1, fake_user(1, "방장"), f"m:f:{CHAT}")
    await menu.on_callback(svc, bot, q, ["f", str(CHAT)])
    btn = next(b for row in q.kb.inline_keyboard for b in row if "태그" in b.text)
    assert btn.text == "✅ 태그·답장 알림" and btn.callback_data == f"m:t:{CHAT}:tag_notify:0"
    q = FakeQuery(1, fake_user(1, "방장"), btn.callback_data)
    await menu.on_callback(svc, bot, q, btn.callback_data.split(":")[1:])
    assert (await db.get_settings(CHAT))["tag_notify"] is False
    await db.ensure_chat(CHAT, "대표님 <소통방> & 친구")
    q = FakeQuery(KIM, fake_user(KIM, "김대표"), "m:tn")                       # 멤버 화면에 '방에서 꺼짐' 안내
    await menu.on_callback(svc, bot, q, ["tn"])
    assert "꺼둬서" in q.edits[0] and "대표님 &lt;소통방&gt; &amp; 친구" in q.edits[0]
    assert not html_errors(q.edits[0])
    text, kb = await menu.main_menu(svc, bot, KIM)
    assert any(b.callback_data == "m:tn" for row in kb.inline_keyboard for b in row)


@test
async def hook_runs_after_moderation_in_handlers():
    assert tagnotify.on_group_message in handlers.GROUP_MESSAGE_HOOKS
    db, svc, bot, _ = await setup()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})
    text = "@kim 여기 봐주세요"
    await handlers.on_group_message(SimpleNamespace(message=gmsg(text, entities=[ment(text, "kim")])), ctx)
    await asyncio.gather(*ctx.bot_data["tasks"])
    assert len(dms(bot, KIM)) == 1
    # 금지어에 걸린 메시지는 지워지고 알림도 안 감
    await db.set_banned_word(CHAT, "도박", True)
    text = "@kim 도박 ㄱ"
    await handlers.on_group_message(SimpleNamespace(message=gmsg(text, entities=[ment(text, "kim")],
                                                                 message_id=6)), ctx)
    await asyncio.gather(*ctx.bot_data["tasks"])
    assert len(dms(bot, KIM)) == 1


@test
async def hook_registered_in_real_startup_order():
    # python -m sodam 은 handlers 를 먼저 import → handlers → menu → panels 순환. 그래도 훅이 들어가야 함
    code = ("import sodam.__main__, sodam.handlers as h, sodam.tagnotify as t;"
            "assert t.on_group_message in h.GROUP_MESSAGE_HOOKS, h.GROUP_MESSAGE_HOOKS")
    r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
