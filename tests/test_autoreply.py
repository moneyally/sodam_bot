"""💬 자동 답글 (sodam/autoreply.py · panels/autoreply.py, 2026-10-09 NASA 방 '.reply 공지'): python tests/run_all.py autoreply
실제 handlers.on_group_message 로 (가짜 텔레그램)."""
import asyncio
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc, runner
from telegram.error import BadRequest

from sodam import autoreply as AR
from sodam import handlers, tools
from sodam.panels import autoreply as P
from sodam.permissions import Role

test, run_all = runner()
CHAT = -1003333
ADMIN, MEMBER, OTHER = 1, 7, 8


class Bot(FakeBot):
    copy_fails = False

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        if self.copy_fails:
            raise BadRequest("Message to copy not found")
        self.calls.append(("copy", chat_id, from_chat_id, message_id, kw))
        return SimpleNamespace(message_id=message_id + 100000)

    async def send_voice(self, chat_id, voice, caption=None, **kw):
        return await self._send_media("send_voice", chat_id, voice, caption, **kw)


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, "방")
    await db.set_setting(CHAT, "cas_enabled", False)
    AR._cache.clear()
    AR._user_last.clear()
    AR._daily.clear()
    bot = Bot()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "chats": set(), "cas_seen": set(), "tasks": set(), "joins": {}})
    return db, svc, bot, ctx


_ids = iter(range(500, 10_000))


async def say(ctx, uid, text, reply_to=None, **media):
    m = FakeMsg(CHAT, fake_user(uid, f"사람{uid}"), text, message_id=next(_ids), reply_to=reply_to)
    for k, v in media.items():
        setattr(m, k, v)
    m.chat = SimpleNamespace(id=CHAT, title="방", type="supergroup")
    m.sender_chat = None
    upd = SimpleNamespace(message=m, effective_chat=m.chat)
    await handlers.on_any_update(upd, ctx)
    await handlers.on_group_message(upd, ctx)
    await asyncio.gather(*ctx.bot_data["tasks"], return_exceptions=True)
    return m


def copies(bot):
    return bot.named("copy")


def photo_msg(mid=50):
    m = FakeMsg(CHAT, fake_user(ADMIN, "관리"), "", caption="📢 오늘 공지", message_id=mid,
                photo=(SimpleNamespace(file_id="PHOTO1"),))
    return m


@test
async def reply_saves_and_keyword_answers_with_the_original():
    db, svc, bot, ctx = await setup()
    src = photo_msg()
    cmd = await say(ctx, ADMIN, ".reply 공지", reply_to=src)
    assert "공지" in cmd.replies[-1] and "✅" in cmd.replies[-1], cmd.replies
    m = await say(ctx, MEMBER, "공지")
    assert len(copies(bot)) == 1, bot.calls
    _, chat, frm, mid, kw = copies(bot)[0]
    assert (chat, frm, mid) == (CHAT, CHAT, 50)
    assert kw["reply_parameters"].message_id == m.message_id          # 친 사람 글에 답장으로
    row = (await AR.rows(db, CHAT))[0]
    assert row["uses"] == 1 and '"PHOTO1"' in row["snap"]


@test
async def only_the_word_alone_triggers_with_loose_spacing_and_case():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply Notice", reply_to=photo_msg())
    await say(ctx, MEMBER, "notice 언제 올라와요")                     # 문장 속 낱말은 아님
    assert not copies(bot)
    await say(ctx, MEMBER, "  NOTICE ? ")
    assert len(copies(bot)) == 1


@test
async def flood_is_limited_per_word_and_per_person():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 공지", reply_to=photo_msg())
    for uid in (MEMBER, MEMBER, OTHER, OTHER):
        await say(ctx, uid, "공지")
    assert len(copies(bot)) == 1, "같은 낱말은 간격(기본 30초)에 한 번"
    await db._write("UPDATE auto_replies SET last_used=0")          # 간격이 지났다고 치고
    await say(ctx, MEMBER, "공지")
    assert len(copies(bot)) == 1, "같은 사람은 10초에 한 번"
    await say(ctx, 9, "공지")                                          # (OTHER 는 같은 글 반복이라 도배 검사에 걸림)
    assert len(copies(bot)) == 2


@test
async def same_person_waits_even_when_the_word_is_free():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 공지", reply_to=photo_msg())
    u = fake_user(20, "연타")
    assert await AR.maybe_reply(svc, bot, FakeMsg(CHAT, u, "공지", message_id=901), "공지")
    await db._write("UPDATE auto_replies SET last_used=0")
    assert await AR.maybe_reply(svc, bot, FakeMsg(CHAT, u, "공지", message_id=902), "공지")   # 조용히 넘어감
    assert len(copies(bot)) == 1
    assert not await AR.maybe_reply(svc, bot, FakeMsg(CHAT, u, "다른 말", message_id=903), "다른 말")


@test
async def deleted_original_falls_back_to_the_saved_copy():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 공지", reply_to=photo_msg())
    bot.copy_fails = True
    await say(ctx, MEMBER, "공지")
    sent = bot.named("send_photo")
    assert sent and sent[0][2] == "PHOTO1" and sent[0][3] == "📢 오늘 공지", bot.calls


@test
async def sticker_and_voice_copies_work_too():
    db, svc, bot, ctx = await setup()
    stk = FakeMsg(CHAT, fake_user(ADMIN, "관리"), "", message_id=60)
    stk.sticker = SimpleNamespace(file_id="STK1")
    voice = FakeMsg(CHAT, fake_user(ADMIN, "관리"), "", message_id=61)
    voice.voice = SimpleNamespace(file_id="VOICE1")
    await say(ctx, ADMIN, ".reply 안녕", reply_to=stk)
    await say(ctx, ADMIN, ".답글 노래", reply_to=voice)
    bot.copy_fails = True
    await say(ctx, MEMBER, "안녕")
    await say(ctx, OTHER, "노래")
    assert bot.named("send_sticker")[0][2] == "STK1" and bot.named("send_voice")[0][2] == "VOICE1", bot.calls


@test
async def text_form_and_cancel_words():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 입금 입금은 관리자 1:1 로 문의")
    await say(ctx, MEMBER, "입금")
    assert any(c[1] == CHAT and c[2] == "입금은 관리자 1:1 로 문의" for c in bot.named("send_message")), bot.calls
    out = await say(ctx, ADMIN, ".reply 취소 입금")
    assert "지웠" in out.replies[-1]
    n = len(bot.named("send_message"))
    await say(ctx, OTHER, "입금")
    assert len(bot.named("send_message")) == n
    await say(ctx, ADMIN, ".리플 a 첫째")
    out = await say(ctx, ADMIN, ".답글취소 a")
    assert "지웠" in out.replies[-1] and not await AR.rows(db, CHAT)


@test
async def members_cannot_register_and_commands_are_refused():
    db, svc, bot, ctx = await setup()
    out = await say(ctx, MEMBER, ".reply 공지", reply_to=photo_msg())
    assert "관리자" in out.replies[-1] and not await AR.rows(db, CHAT)
    out = await say(ctx, ADMIN, ".reply .공지", reply_to=photo_msg())
    assert "명령어" in out.replies[-1] and not await AR.rows(db, CHAT)
    assert AR.check_word("!!!") and AR.check_word("가" * 31) and AR.check_word("공지") is None


@test
async def list_shows_words():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 공지, 규칙안내", reply_to=photo_msg())
    out = await say(ctx, ADMIN, ".reply 목록")
    assert "공지" in out.replies[-1] and "규칙안내" in out.replies[-1] and "[사진]" in out.replies[-1]


@test
async def menu_two_step_flow_saves_any_message():
    db, svc, bot, ctx = await setup()
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN, args=[], arg=lambda i: c.args[i] if len(c.args) > i else "")
    ok, _ = await P.in_word(c, FakeMsg(ADMIN, fake_user(ADMIN, "관리"), "/공지"))
    assert not ok                                                      # 명령어 모양은 거절
    ok, _ = await P.in_word(c, FakeMsg(ADMIN, fake_user(ADMIN, "관리"), "환영"))
    assert ok
    screen = await P.s_ask_body(c)
    pend = svc.inputs[ADMIN]
    assert pend.kind == "arb" and pend.args == ["환영"] and "글을 보내" in screen.text
    body = FakeMsg(ADMIN, fake_user(ADMIN, "관리"), "", message_id=77)
    body.sticker = SimpleNamespace(file_id="STK9")
    c.args = list(pend.args)
    ok, text = await P.in_body(c, body)
    assert ok, text
    await say(ctx, MEMBER, "환영")
    assert copies(bot)[0][2:4] == (ADMIN, 77)                         # 1:1 에 보낸 원본을 복사
    screen = await P.s_ar(c)
    assert "환영" in str(screen.kb.inline_keyboard)


@test
async def ai_tool_saves_the_replied_message():
    db, svc, bot, ctx = await setup()
    req = FakeMsg(CHAT, fake_user(ADMIN, "관리"), "소담아 공지 치면 이거 나오게 해줘", reply_to=photo_msg(88))
    tctx = tools.ToolCtx(svc, bot, CHAT, fake_user(ADMIN, "관리"), Role.ADMIN, {}, request_msg=req)
    out = await P.t_auto_reply(tctx, {"op": "add", "word": "공지"})
    assert "저장" in out
    await say(ctx, MEMBER, "공지")
    assert copies(bot)[0][3] == 88
    assert "공지" in await P.t_auto_reply(tctx, {"op": "list"})
    assert "지움" in await P.t_auto_reply(tctx, {"op": "remove", "word": "공지"})
    nothing = tools.ToolCtx(svc, bot, CHAT, fake_user(ADMIN, "관리"), Role.ADMIN, {},
                            request_msg=FakeMsg(CHAT, fake_user(ADMIN, "관리"), "공지 자동답글 만들어"))
    assert "답장" in await P.t_auto_reply(nothing, {"op": "add", "word": "공지"})


@test
def animated_emoji_fallback_keeps_the_plain_emoji():
    assert AR._no_emoji('안녕 <tg-emoji emoji-id="5">🔥</tg-emoji>!') == "안녕 🔥!"


if __name__ == "__main__":
    run_all()
