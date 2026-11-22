"""💬 자동 답글 (sodam/autoreply.py · panels/autoreply.py, 2026-10-09 NASA 방 '.reply 공지'): python tests/run_all.py autoreply
실제 handlers.on_group_message 로 (가짜 텔레그램)."""
import asyncio
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc, runner
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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

    async def _post(self, endpoint, data=None, **kw):       # 네이티브 표(sendRichMessage) raw 호출 기록
        self.calls.append((endpoint, data))
        return SimpleNamespace(message_id=90000)


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, "방")
    await db.set_setting(CHAT, "cas_enabled", False)
    AR._cache.clear()
    AR._user_hits.clear()
    AR._daily.clear()
    AR._icons_off[0] = False          # 프로세스 전역 플래그 — 테스트 간 오염 방지
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


def promo_msg(mid=60):
    """URL 버튼(색·콜백 섞임) 달린 제휴 글 — URL 버튼만 유지되어야 한다."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("앙벳", url="https://t.me/a", style="success", icon_custom_emoji_id="5431"),
         InlineKeyboardButton("띵벳", url="https://t.me/b")],
        [InlineKeyboardButton("코드 받기", callback_data="x")],   # 콜백은 버려야 (다른 방에서 안 먹힘)
    ])
    m = FakeMsg(CHAT, fake_user(ADMIN, "관리"), "📢 제휴업체", message_id=mid)
    m.reply_markup = kb
    return m


@test
async def url_buttons_are_kept_on_copy_and_fallback():
    import json
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 제휴", reply_to=promo_msg())
    snap = json.loads((await AR.rows(db, CHAT))[0]["snap"])
    assert snap.get("markup") and "https://t.me/a" in snap["markup"]
    assert "callback_data" not in snap["markup"]                      # 콜백 버튼은 안 저장

    # 원본 복사 경로: copy_message 에 버튼이 붙어 나간다 (뮤테이션: reply_markup 빼면 실패)
    await say(ctx, MEMBER, "제휴")
    mk = copies(bot)[0][4]["reply_markup"]
    assert [b.url for r in mk.inline_keyboard for b in r] == ["https://t.me/a", "https://t.me/b"]
    assert mk.inline_keyboard[0][0].style == "success"               # 색 유지
    assert mk.inline_keyboard[0][0].icon_custom_emoji_id == "5431"   # 색아이콘(다이소 제휴리스트 꼴) 유지

    # 사본 경로(원본 복사 실패)에서도 버튼 유지
    await db._write("UPDATE auto_replies SET last_used=0")
    bot.copy_fails = True
    await say(ctx, OTHER, "제휴")
    kw = bot.named("send_message")[-1][3]
    assert kw.get("reply_markup") is not None
    fb = kw["reply_markup"].inline_keyboard
    assert [b.url for r in fb for b in r] == ["https://t.me/a", "https://t.me/b"]
    assert fb[0][0].style == "success" and fb[0][0].icon_custom_emoji_id == "5431"   # 사본에서도 색·아이콘 유지


@test
async def premium_icon_rejected_retries_without_icon_not_dropped():
    # 봇 주인이 프리미엄 아니면 버튼 앞 커스텀 이모지 아이콘 발송이 거절됨 (#309/310/311).
    # 예전엔 같은 아이콘 마크업으로 재시도해서 자동답글이 통째로 사라졌음 → 이제 아이콘만 떼고 다시 보내 답글은 뜬다.
    db, svc, bot, ctx = await setup()
    bot.copy_fails = True                       # 원본 복사 실패 → 사본(send_snap→_send_text) 경로
    orig_send = bot.send_message

    async def picky(chat_id, text, **kw):       # 아이콘 달린 마크업이면 텔레그램이 거절
        mk = kw.get("reply_markup")
        if mk and any(getattr(b, "icon_custom_emoji_id", None) for r in mk.inline_keyboard for b in r):
            raise BadRequest("Bad Request: PREMIUM_ACCOUNT_REQUIRED custom_emoji")
        return await orig_send(chat_id, text, **kw)
    bot.send_message = picky

    await say(ctx, ADMIN, ".reply 제휴", reply_to=promo_msg())
    await say(ctx, MEMBER, "제휴")
    replies = [c for c in bot.named("send_message") if c[3].get("reply_markup")]
    assert replies, "자동답글이 떠야 함 (아이콘 거절돼도 안 버려짐)"
    final = replies[-1][3]["reply_markup"]
    icons = [getattr(b, "icon_custom_emoji_id", None) for r in final.inline_keyboard for b in r]
    assert not any(icons), f"아이콘은 떼고 재전송: {icons}"
    assert [b.url for r in final.inline_keyboard for b in r] == ["https://t.me/a", "https://t.me/b"], "URL·색은 유지"


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
async def gap_default_fast_and_user_max_raised():
    # 2026-11-22 오너 '전체 3초로 바로바로' — 같은 낱말 간격 기본 3초(예전 30)·1초까지 허용, 한 사람 1분 10번(예전 5)
    from sodam.settings import DEFAULTS, coerce
    assert DEFAULTS["autoreply_gap"] == 3, DEFAULTS["autoreply_gap"]
    assert coerce("autoreply_gap", "1") == 1 and coerce("autoreply_gap", "3") == 3, "1초까지 허용"
    try:
        coerce("autoreply_gap", "0")
        assert False, "0초는 범위 밖"
    except ValueError:
        pass
    assert (AR.USER_MAX, AR.USER_WINDOW) == (10, 60)


@test
async def flood_is_limited_per_word_and_per_person():
    db, svc, bot, ctx = await setup()
    await say(ctx, ADMIN, ".reply 공지", reply_to=photo_msg())
    for uid in (MEMBER, MEMBER, OTHER, OTHER):
        await say(ctx, uid, "공지")
    assert len(copies(bot)) == 1, "같은 낱말은 간격(기본 3초)에 한 번 — 빠르게 연달아 치면 1번"
    await db._write("UPDATE auto_replies SET last_used=0")          # 간격이 지났다고 치고
    await say(ctx, 9, "공지")                                          # (MEMBER·OTHER 는 같은 글 반복이라 도배 검사에 걸림)
    assert len(copies(bot)) == 2


@test
async def one_person_different_words_answer_at_once_but_flood_stops():
    """2026-10-10 뉴월드: NASA 님이 '인사'(00:58:29) → '공지'(5초 뒤) → '이벤트'(6초 뒤) — 한 사람 10초 규칙에 씹혔음."""
    db, svc, bot, ctx = await setup()
    words = ["인사", "공지", "이벤트", "규칙", "제휴", "가격", "문의", "안내", "결제", "충전", "환전"]
    for w in words:
        await say(ctx, ADMIN, f".reply {w}", reply_to=photo_msg())
    u = fake_user(20, "연타")
    for i, w in enumerate(words[:10]):
        assert await AR.maybe_reply(svc, bot, FakeMsg(CHAT, u, w, message_id=900 + i), w)
    assert len(copies(bot)) == 10, "다른 낱말은 바로바로 (한 사람 1분 10번까지)"
    assert await AR.maybe_reply(svc, bot, FakeMsg(CHAT, u, "환전", message_id=910), "환전")   # 1분에 11번째 → 조용히
    assert len(copies(bot)) == 10
    assert not await AR.maybe_reply(svc, bot, FakeMsg(CHAT, u, "다른 말", message_id=911), "다른 말")


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
    out = await say(ctx, ADMIN, ".unreply a")             # 다른 봇에서 쓰던 말 (얼라이드 실제 사례)
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


def _tiles(n):
    """커스텀 이모지 조각 n 개짜리 배너(표·텔레모션) HTML."""
    return "".join(f'<tg-emoji emoji-id="{500 + i}">🟦</tg-emoji>' for i in range(n))


@test
async def banner_in_fallback_gets_a_full_width_spacer():
    # 표(분할 커스텀 이모지)를 사본(텍스트)으로 보낼 때 전체폭 스페이서가 붙어 계단으로 안 깨진다 (예약공지와 같은 방식)
    from sodam.util import BANNER_SPACER
    db, svc, bot, ctx = await setup()
    await AR.send_snap(bot, db, CHAT, {"html": _tiles(4)})         # 미디어 없는 글 = 사본 텍스트 경로
    body = bot.named("send_message")[-1][2]
    assert body.rstrip().endswith(BANNER_SPACER), (              # 뮤테이션: send_snap 의 banner_spacer 줄 제거하면 실패
        "표면 스페이서가 붙어야 함", body[-50:])


def _table_blocks():
    return [{"type": "table", "cells": [
        [{"text": "제휴", "is_header": True}, {"text": "바로가기", "is_header": True}],
        [{"text": "토지노"}, {"text": {"type": "url", "text": "열기", "url": "https://t.me/a"}}]]}]


@test
def snapshot_captures_a_native_table():
    # 전달된 네이티브 표(Bot API 10.1 리치 메시지)는 PTB 가 모르니 api_kwargs 에서 blocks 를 챙긴다
    # ⚠️ PTB 는 api_kwargs 를 MappingProxyType 로 주므로 생성자로 넘겨 진짜 경로를 타게 (2026-11-21 실측 함정: dict 로 덮어쓰면 뮤테이션 못 잡음)
    m = FakeMsg(CHAT, fake_user(ADMIN, "관리"), "", api_kwargs={"rich_message": {"blocks": _table_blocks()}})
    snap = AR.snapshot(m)
    assert snap["rich"] and snap["rich"]["blocks"][0]["type"] == "table"
    assert "표" in AR.describe(snap)                                  # 목록에 [📊 표]


@test
async def table_message_is_saved_and_resent_as_rich():
    # 표(글·미디어 없음)도 자동답글로 저장되고(빈 글 거절 X), 사본은 sendRichMessage(blocks)로 재현
    db, svc, bot, ctx = await setup()
    blocks = _table_blocks()
    snap = {"html": "", "kind": None, "file_id": None, "markup": None, "rich": {"blocks": blocks}}
    ok, _ = await AR.save(db, CHAT, "제휴표", by=ADMIN, src=(CHAT, 777), snap=snap)
    assert ok, "표도 저장돼야 함 (빈 글로 거절되면 안 됨)"
    await AR.send_snap(bot, db, CHAT, snap, reply_to=5)
    posts = bot.named("sendRichMessage")
    assert posts, "표는 sendRichMessage 로 보내야 함 (뮤테이션: send_snap 의 rich 분기 제거하면 실패)"
    sent = posts[-1][1]
    assert sent["rich_message"]["blocks"] == blocks                  # 받은 표 그대로 재전송
    assert sent["reply_parameters"]["message_id"] == 5              # 친 사람 글에 답장


@test
async def ordinary_text_is_left_alone():
    # 이모지 한두 개짜리 보통 글은 스페이서를 안 붙인다 (조각 4개 이상 '배너'만)
    from sodam.util import BANNER_SPACER
    db, svc, bot, ctx = await setup()
    await AR.send_snap(bot, db, CHAT, {"html": "안녕 " + _tiles(1)})
    body = bot.named("send_message")[-1][2]
    assert BANNER_SPACER not in body, body


if __name__ == "__main__":
    run_all()
