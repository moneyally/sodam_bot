"""움직이는 이모지(프리미엄 커스텀 이모지)·서식이 예약공지·알람·.공지 에서 사라지던 버그: python tests/run_all.py rich_emoji

실제 사례 2026-09-30: 예약공지 제목·내용에 움직이는 이모지를 넣어도 방엔 보통 이모지로만 올라감.
원인: 마법사가 msg.text(글자만)를 저장 → custom_emoji 엔티티가 버려짐, 발송 때 전부 escape.
고침: 서식이 있으면 텔레그램 HTML(<tg-emoji emoji-id=…>)로 보관 (fmt='html'), 거절되면 글자로 다시 보냄.
"""
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from telegram import MessageEntity
from telegram.error import BadRequest

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


if __name__ == "__main__":
    run_all()
