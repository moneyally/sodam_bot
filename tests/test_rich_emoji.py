"""움직이는 이모지(프리미엄 커스텀 이모지)·서식이 예약공지·알람·.공지 에서 사라지던 버그: python tests/run_all.py rich_emoji

실제 사례 2026-09-30: 예약공지 제목·내용에 움직이는 이모지를 넣어도 방엔 보통 이모지로만 올라감.
원인: 마법사가 msg.text(글자만)를 저장 → custom_emoji 엔티티가 버려짐, 발송 때 전부 escape.
고침: 서식이 있으면 텔레그램 HTML(<tg-emoji emoji-id=…>)로 보관 (fmt='html'), 거절되면 글자로 다시 보냄.
"""
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from telegram import MessageEntity
from telegram.error import BadRequest, NetworkError

from sodam import cron
from sodam.panels import announce as panel

test, run_all = runner()
CHAT, ADMIN = -1001111, 1
EMO = '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji>'


def rich_msg(chat_id, text, html, uid=ADMIN, mid=50):
    """텔레그램이 주는 것처럼: 글자(text) + 엔티티 + PTB text_html."""
    m = FakeMsg(chat_id, fake_user(uid, "방장"), text, message_id=mid)
    m.entities = (MessageEntity("custom_emoji", text.index("🔥"), 2, custom_emoji_id="5368324170671202286"),)
    m.text_html = html
    return m


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, "내 방")

    async def yes(*a):
        return True
    svc.perms.is_admin = yes
    svc.perms.forget = lambda cid: None
    return db, svc, FakeBot()


async def wizard(svc, bot, title_msg, body_msg):
    for m in (title_msg, body_msg, FakeMsg(ADMIN, fake_user(ADMIN), "없음", message_id=59),
              FakeMsg(ADMIN, fake_user(ADMIN), "매일 09:00", message_id=60)):
        assert await svc.announcer.handle_message(bot, m), m.text
    draft = svc.announcer.drafts[(ADMIN, ADMIN)]
    q = FakeQuery(ADMIN, fake_user(ADMIN))
    await svc.announcer.on_callback(bot, q, [draft.token, "pin0"])
    if draft.step == "keep":                                   # 반복 공지 → 지난 회차 쌓기 물음 한 단계
        await svc.announcer.on_callback(bot, q, [draft.token, "keep1"])
    if draft.step == "limits":                                 # 기간·시간대 제한 (선택) → 제한 없음
        await svc.announcer.on_callback(bot, q, [draft.token, "nolimit"])
    return draft, q


@test
async def scheduled_post_keeps_animated_emoji_and_plain_title_is_escaped_once():
    db, svc, bot = await setup()
    from sodam import menu   # 1:1 [➕ 새로 만들기] 로 시작 (실제 경로)
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"), f"m:scn:{CHAT}")
    await menu.on_callback(svc, bot, q, q.data.split(":")[1:])
    title = FakeMsg(ADMIN, fake_user(ADMIN), "<이벤트> & 공지", message_id=51)
    body = rich_msg(ADMIN, "오늘 🔥 이벤트", f"오늘 {EMO} 이벤트", mid=52)
    draft, q = await wizard(svc, bot, title, body)
    assert draft.fmt == "html" and draft.title == "&lt;이벤트&gt; &amp; 공지"
    preview = [c for c in bot.calls if c[0] == "send_message" and c[1] == ADMIN and "오늘" in c[2]][-1]
    assert EMO in preview[2] and preview[3]["parse_mode"] == "HTML", preview      # 미리보기에도 움직이는 이모지
    await svc.announcer.on_callback(bot, q, [draft.token, "save"])
    [row] = await db.schedules(CHAT)
    assert row["fmt"] == "html" and EMO in row["text"]
    await svc.announcer.publish(bot, row)
    sent = [c for c in bot.calls if c[0] == "send_message" and c[1] == CHAT][-1][2]
    assert sent == f"📢 <b>&lt;이벤트&gt; &amp; 공지</b>\n\n오늘 {EMO} 이벤트", sent   # 두 번 escape 안 됨
    s = await panel.s_list(SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN, args=[]))
    if True:   # 목록 버튼엔 태그 없이 보이는 글자
        labels = " ".join(b.text for row_ in s.kb.inline_keyboard for b in row_)
        assert "<이벤트>" in labels and "tg-emoji" not in labels and "&lt;" not in labels, labels
    await db.close()


@test
async def animated_emoji_in_title_and_plain_body_escaped_once():
    db, svc, bot = await setup()
    from sodam import menu
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"), f"m:scn:{CHAT}")
    await menu.on_callback(svc, bot, q, q.data.split(":")[1:])
    draft, q = await wizard(svc, bot, rich_msg(ADMIN, "🔥 필독", f"{EMO} 필독", mid=51),
                            FakeMsg(ADMIN, fake_user(ADMIN), "A&B <필독>", message_id=52))
    await svc.announcer.on_callback(bot, q, [draft.token, "save"])
    [row] = await db.schedules(CHAT)
    await svc.announcer.publish(bot, row)
    sent = [c for c in bot.calls if c[0] == "send_message" and c[1] == CHAT][-1][2]
    assert sent == f"📢 <b>{EMO} 필독</b>\n\nA&amp;B &lt;필독&gt;", sent
    await db.close()


@test
async def refused_rich_send_falls_back_to_plain_text():
    """봇 주인이 프리미엄이 아니면 텔레그램이 거절할 수 있음 → 공지는 글자로라도 올라가야."""
    db, svc, bot = await setup()
    sid = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title="", text=f"오늘 {EMO} 이벤트",
                                media_type=None, media_id=None, pin=False, created_by=ADMIN, fmt="html")
    real = bot.send_message

    async def picky(chat_id, text, **kw):
        if "tg-emoji" in text:
            raise BadRequest("Can't parse entities: custom emoji not allowed")
        return await real(chat_id, text, **kw)
    bot.send_message = picky
    await svc.announcer.publish(bot, await db.get_schedule(CHAT, sid))
    sent = [c for c in bot.calls if c[0] == "send_message" and c[1] == CHAT]
    assert len(sent) == 1 and sent[0][2] == "📢 오늘 🔥 이벤트", sent
    await db.close()


@test
async def html_plain_drops_abc_lettering_emoji():
    """봇 주인이 프리미엄이 아니면 '글자' 커스텀 이모지(🔤 등)가 못 떠서 'abc' 로 깨짐 → 글자 변환 때 통째로 뺀다
    (보통 이모지 💰💗😀 는 그 기본 이모지로 그대로). 2026-10-27 New world AMEX 광고."""
    from sodam.util import html_plain
    ABC = '<tg-emoji emoji-id="5900093657955967434">🔤</tg-emoji>'
    MONEY = '<tg-emoji emoji-id="4983234121556820510">💰</tg-emoji>'
    assert html_plain(ABC + ABC + ABC + MONEY + "안녕") == "💰안녕"
    assert html_plain(MONEY + "값 10%") == "💰값 10%"
    for box in "🔠🔡🔢🔣🔤":                                 # U+1F520~U+1F524 네모 글자 이모지 전부
        assert html_plain(f'<tg-emoji emoji-id="1">{box}</tg-emoji>X') == "X", box


@test
async def refused_send_drops_abc_lettering_emoji():
    """봇이 커스텀 이모지를 못 보내 글자로 폴백할 때 '글자' 이모지는 'abc' 로 안 뜨고 빠진다 (New world AMEX 공지)."""
    db, svc, bot = await setup()
    ABC = '<tg-emoji emoji-id="5900093657955967434">🔤</tg-emoji>'
    MONEY = '<tg-emoji emoji-id="4983234121556820510">💰</tg-emoji>'
    sid = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title="",
                                text=ABC + ABC + MONEY + "안녕하세요 아멕스" + MONEY,
                                media_type=None, media_id=None, pin=False, created_by=ADMIN, fmt="html")
    real = bot.send_message

    async def picky(chat_id, text, **kw):
        if "tg-emoji" in text:
            raise BadRequest("Document_invalid")
        return await real(chat_id, text, **kw)
    bot.send_message = picky
    await svc.announcer.publish(bot, await db.get_schedule(CHAT, sid))
    sent = [c for c in bot.calls if c[0] == "send_message" and c[1] == CHAT]
    assert len(sent) == 1 and "🔤" not in sent[0][2] and "💰안녕하세요 아멕스💰" in sent[0][2], sent
    await db.close()


@test
async def plain_schedules_and_long_rich_text_still_work():
    db, svc, bot = await setup()
    from sodam.announce import render
    import datetime
    tz = datetime.timezone.utc
    assert render("<a>", "1 < 2", has_media=False, rules="", tz=tz) == "📢 <b>&lt;a&gt;</b>\n\n1 &lt; 2"     # 예전 그대로
    long = "가" * 1100 + EMO
    out = render("", long, has_media=True, rules="", tz=tz, fmt="html")                   # 사진 설명 1024자 넘침
    assert "tg-emoji" not in out and len(out) <= 1024 and out.endswith("…"), len(out)
    assert "&lt;광고 금지&gt;" in render("", "{규칙} " + EMO, has_media=False, rules="<광고 금지>", tz=tz, fmt="html")
    await db.close()


@test
async def alarm_and_notice_keep_animated_emoji():
    db, svc, bot = await setup()
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN)
    msg = rich_msg(ADMIN, "30분 뒤 | 회의 🔥 시작", f"30분 뒤 | 회의 {EMO} 시작")
    ok, _ = await panel._input("remind", None)(c, msg)
    [row] = await db.schedules(CHAT)
    assert ok and row["fmt"] == "html" and row["text"] == f"회의 {EMO} 시작", dict(row)
    assert await cron.fire(svc, bot, row)
    body = [x for x in bot.calls if x[0] == "send_message" and x[1] == CHAT][-1][2]
    assert f"회의 {EMO} 시작" in body, body
    await db.close()

    r = await Room().open(admins={1})
    m = r.msg(fake_user(1, "방장"), ".공지 오늘 🔥 필독")
    m.entities = (MessageEntity("custom_emoji", 8, 2, custom_emoji_id="5368324170671202286"),)
    m.text_html = f".공지 오늘 {EMO} 필독"
    from sodam import handlers
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)
    await r.settle()
    sent = [x for x in r.bot.named("send_message") if "공지" in x[2]][-1][2]
    assert sent == f"📢 <b>공지</b>\n오늘 {EMO} 필독", sent


@test
async def alarm_all_bold_is_not_cut_mid_tag_and_lists_hide_tags():
    """감사 2026-09-30: 글 전체가 굵게면 '|' 가 <b> 안에 있어 자르면 </b> 만 남음 → 그땐 글자만 저장.
    목록·운영 인박스·기록엔 태그 대신 보이는 글자."""
    db, svc, bot = await setup()
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN)
    m = rich_msg(ADMIN, "30분 뒤 | 회의 🔥", f"<b>30분 뒤 | 회의 {EMO}</b>")
    ok, _ = await panel._input("remind", None)(c, m)
    [row] = await db.schedules(CHAT)
    assert ok and row["fmt"] == "" and row["text"] == "회의 🔥", dict(row)
    from sodam.util import html_balanced
    assert html_balanced(f"<b>a</b>{EMO}") and not html_balanced("a</b>") and not html_balanced("<b>a")
    sid = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title=f"{EMO} 필독",
                                text="x", media_type=None, media_id=None, pin=False, created_by=ADMIN, fmt="html")
    from sodam import opsdesk
    import time as _t
    await db._write("INSERT INTO ops_events(chat_id, kind, ref, ts) VALUES(?, 'sched_send', ?, ?)", (CHAT, sid, int(_t.time())))
    items = await opsdesk._record_items(svc, CHAT, ADMIN, int(_t.time()))
    texts = [i.text for i in items if i.kind == "sched"]
    assert texts and all("tg-emoji" not in t and "🔥 필독" in t for t in texts), texts
    await db.close()


@test
async def schedule_detail_screen_shows_animated_emoji_not_squares():
    """실제 2026-10-05 일루왕 #11: 글자 모양 움직이는 이모지 99개 → 공지 화면에선 기본 이모지 ⬜️ 로 바뀌어 '다 깨짐'.
    화면 HTML 에 움직이는 이모지를 그대로 넣고, 길면 태그 안 깨지게 자름."""
    from sodam.util import html_balanced, html_truncate
    db, svc, bot = await setup()
    letter = '<tg-emoji emoji-id="6104937634897860055">⬜️</tg-emoji>'
    text = f'{letter}{letter} 회원 모집 <b>신규 가입머니</b>\n' + ("가" * 400) + f"<b>{letter}끝</b>"
    sid = await db.add_schedule(CHAT, kind="interval", at_time=None, interval_min=120, title=f"{letter} 공지",
                                text=text, media_type=None, media_id=None, pin=False, created_by=ADMIN, fmt="html")
    c = SimpleNamespace(svc=svc, bot=bot, cid=CHAT, uid=ADMIN, arg=lambda i: str(sid) if i == 0 else "")
    screen = await panel.s_item(c)
    assert screen.text.count(letter) == 3 and html_balanced(screen.text), screen.text   # 제목 1 + 내용 앞 2 (뒤는 잘림)
    assert "…" in screen.text and "<b>신규 가입머니</b>" in screen.text
    cut = html_truncate(f"<b>{letter}가나다</b>", 2)
    assert cut == f"<b>{letter}가…</b>", cut                                          # 이모지 반쪽 태그 없음
    assert html_truncate("<b>짝 안 맞음", 3) == "짝 안…"                               # 짝 안 맞으면 글자만
    plain_sid = await db.add_schedule(CHAT, kind="daily", at_time="09:00", interval_min=None, title="<i>그냥</i>",
                                      text="a < b", media_type=None, media_id=None, pin=False, created_by=ADMIN)
    c.arg = lambda i: str(plain_sid) if i == 0 else ""
    screen = await panel.s_item(c)
    assert "&lt;i&gt;그냥&lt;/i&gt;" in screen.text and "a &lt; b" in screen.text   # 서식 없는 글은 그대로 이스케이프
    await db.close()


@test
async def dead_emoji_screen_falls_back_to_plain_not_crash():
    """실제 2026-10-27 New world: 죽은 💎 커스텀 이모지(Document_invalid)가 든 예약공지의 수정 화면 버튼이 '씹혔음'.
    _show 가 HTML 거절 때 raise 하면 콜백이 터져 버튼 무반응 → 글자로라도 화면·버튼을 띄운다."""
    from sodam import menu
    from sodam.menu import Screen

    class DeadQuery(FakeQuery):
        async def edit_message_text(self, text, **kw):
            if kw.get("parse_mode") == "HTML" and "tg-emoji" in text:
                raise BadRequest("Document_invalid")   # 죽은 커스텀 이모지 → 전체 거절
            self.edits.append(text)
            self.kb = kw.get("reply_markup")

    DEAD = '<tg-emoji emoji-id="5388803751559586023">💎</tg-emoji>'
    q = DeadQuery(ADMIN, fake_user(ADMIN, "방장"))
    q.message = SimpleNamespace(message_id=50)
    screen = Screen(text=f"{DEAD} 아멕스 공지 수정", kb=None)
    await menu._show(None, q, ADMIN, screen)
    assert q.edits == ["💎 아멕스 공지 수정"], q.edits     # 글자로 떴고(= 버튼 살아있음), 안 터짐

    # 서식 문제가 아닌 다른 BadRequest 는 그대로 올라간다 (숨기면 진짜 버그를 놓침)
    class OtherBad(FakeQuery):
        async def edit_message_text(self, text, **kw):
            raise BadRequest("CHAT_WRITE_FORBIDDEN")

    q2 = OtherBad(ADMIN, fake_user(ADMIN, "방장"))
    q2.message = SimpleNamespace(message_id=51)
    try:
        await menu._show(None, q2, ADMIN, Screen(text="평범한 글", kb=None))
        assert False, "서식 무관 오류는 올라와야"
    except BadRequest:
        pass


@test
async def badrequest_not_misreported_as_network_error():
    """PTB 에선 BadRequest 가 NetworkError 의 하위 클래스라, on_error 가 먼저 안 걸러내면
    Document_invalid(깨진 이모지) 같은 내용 오류가 '연결 일시 오류'로 묻혀 버튼이 조용히 씹힌다
    (2026-10-27 NASA·New world 공지). 콘텐츠 오류와 진짜 네트워크 오류를 구분해야 함."""
    from sodam import handlers
    assert issubclass(BadRequest, NetworkError)        # 이 전제가 버그의 뿌리
    seen = []
    real = handlers.log.warning
    handlers.log.warning = lambda msg, *a: seen.append(msg % a if a else msg)
    try:
        await handlers.on_error(None, SimpleNamespace(error=BadRequest("Document_invalid")))
        await handlers.on_error(None, SimpleNamespace(error=NetworkError("Connection reset")))
        await handlers.on_error(None, SimpleNamespace(error=BadRequest("Message is not modified")))
    finally:
        handlers.log.warning = real
    assert any("요청 거절" in s and "Document_invalid" in s for s in seen), seen       # 내용 오류로 드러남
    assert any("연결 일시 오류" in s and "Connection reset" in s for s in seen), seen   # 진짜 네트워크
    assert not any("Message is not modified" in s for s in seen), seen                  # 흔한 무해 → 조용히
    assert not any("연결 일시 오류" in s and "Document_invalid" in s for s in seen), seen  # 오분류 X


if __name__ == "__main__":
    run_all()
