"""📓 소담이 일기 (sodam/diary.py · panels/diary.py). python tests/run_all.py diary"""
from datetime import datetime
from types import SimpleNamespace

from fake_llm import Room, reply
from fakes import FakeQuery, fake_user, runner

from sodam import diary, handlers
from sodam.menu import PanelCtx
from sodam.panels import diary as P

test, run_all = runner()

OWNER, A, B = fake_user(7, "오너", "owner"), fake_user(20, "민지", "minji"), fake_user(21, "준호", "junho")
CH = -1004455399205
DIARY = ("오늘은 대표님들 소통방에서 이야기가 많았어요 😊\n민지님이 https://evil.xyz 를 보냈고 TSyV5aaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
         "새로 온 분들도 반가웠어요.\n내일도 만나요 🌙\n\n— 소담")


async def world(mode="auto", time="21:00"):
    r = await Room().open()
    r.svc.perms.owner_ids = {OWNER.id}
    for u in (A, B):
        await r.join(u)
    for i, (u, t) in enumerate([(A, "비밀이야기 첫째"), (B, "반가워 둘째"), (A, "내 계좌번호 셋째")]):
        await r.db.log_message(r.CHAT, u.id, 100 + i, t)
    await r.db._write("INSERT INTO channels(chat_id, title, active, can_post) VALUES(?,?,1,1)", (CH, "소담이의 메모장"))
    for k, v in (("diary_mode", mode), ("diary_time", time), ("diary_channel", CH), ("diary_no", 1)):
        await r.db.set_state(0, k, v)
    return r


@test
async def facts_are_numbers_only_and_ai_never_sees_names():
    r = await world()
    f = await diary.facts(r.db, r.svc.cfg.tz, datetime.now(r.svc.cfg.tz))
    assert f["대화 수"] == 3 and f["말한 사람 수"] == 2 and f["대화가 있던 방 수"] == 1, f
    r.llm.script = [reply(DIARY)]
    text = await diary.write(r.svc, datetime.now(r.svc.cfg.tz))
    sent = str(r.llm.of("chat")[-1]["messages"])
    for name in ("대표님들 소통방", "민지", "준호", "비밀이야기", "계좌번호"):     # 방·사람 이름·대화 글은 AI 에 안 감
        assert name not in sent, name
    # 보내기 전 코드 검사: 방 이름 → '어떤 방', 링크·지갑 제거
    assert "대표님들 소통방" not in text and "어떤 방" in text, text
    assert "evil.xyz" not in text and "TSyV5" not in text and text.endswith("— 소담"), text


@test
async def auto_posts_once_a_day_with_number_and_tells_owner():
    r = await world("auto", "21:00")
    r.llm.script = [reply("오늘은 조용한 하루였어요.\n그래도 몇 분이 인사해 주셔서 좋았어요 😊\n\n— 소담")] * 2
    orig = diary.datetime

    class Late(orig):
        @classmethod
        def now(cls, tz=None):
            return orig.now(tz).replace(hour=23, minute=40)
    diary.datetime = Late
    try:
        await diary.tick(r.svc, r.bot)
        await diary.tick(r.svc, r.bot)                                   # 같은 날 두 번째 = 안 올림 (claim)
    finally:
        diary.datetime = orig
    posts = [c for c in r.bot.named("send_message") if c[1] == CH]
    assert len(posts) == 1 and posts[0][2].startswith("📓 소담이의 메모장 #2\n\n"), posts
    assert await r.db.get_state(0, "diary_no") == 2
    assert any(c[1] == OWNER.id and "#2" in c[2] for c in r.bot.named("send_message"))
    assert len(await r.db.get_state(0, "diary_prev")) == 1


@test
async def off_or_before_time_does_nothing():
    for mode, hour in (("off", 23), ("auto", 20)):
        r = await world(mode, "21:00")
        orig = diary.datetime

        class At(orig):
            @classmethod
            def now(cls, tz=None, _h=hour):
                return orig.now(tz).replace(hour=_h, minute=0)
        diary.datetime = At
        try:
            await diary.tick(r.svc, r.bot)
        finally:
            diary.datetime = orig
        assert not r.bot.named("send_message") and not r.llm.of("chat"), mode


async def press(r, user, data):
    q = FakeQuery(user.id, user, data)
    r.svc.menu_limiter._hits.clear()
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    return q


@test
async def preview_then_post_or_discard_owner_only():
    r = await world("preview")
    r.llm.script = [reply(DIARY), reply("두 번째 초안이에요.\n오늘도 고마웠어요.\n\n— 소담")]
    q = await press(r, A, "m:dyw")                                        # 오너가 아니면 막힘
    assert not r.llm.of("chat") and "오너" in q.answers[0][0]
    await press(r, OWNER, "m:dyw")
    [draft] = [c for c in r.bot.named("send_message") if c[1] == OWNER.id]
    assert "초안" in draft[2] and "m:dyg" in str(draft[3]["reply_markup"])
    assert not [c for c in r.bot.named("send_message") if c[1] == CH], "초안은 아직 안 올림"
    q = await press(r, OWNER, "m:dyg")
    assert [c for c in r.bot.named("send_message") if c[1] == CH] and "#2" in q.answers[0][0]
    q = await press(r, OWNER, "m:dyg")                                    # 이미 올림 → 없음
    assert "없어요" in q.answers[0][0] and len([c for c in r.bot.named("send_message") if c[1] == CH]) == 1
    await press(r, OWNER, "m:dyr")
    await press(r, OWNER, "m:dyx")
    assert await r.db.get_state(0, "diary_draft") is None


@test
async def settings_screen_and_whitelists():
    r = await world("off")
    q = await press(r, OWNER, "m:dy")
    assert "소담이 일기" in q.edits[-1] and "소담이의 메모장" in q.edits[-1]
    for data in ("m:dym:auto", "m:dyt:2200"):
        await press(r, OWNER, data)
    assert await r.db.get_state(0, "diary_mode") == "auto" and await r.db.get_state(0, "diary_time") == "22:00"
    for data in ("m:dym:evil", "m:dyt:0300", "m:dyc:-100123"):              # 목록에 없는 값은 안 바뀜
        await press(r, OWNER, data)
    assert await r.db.get_state(0, "diary_mode") == "auto" and await r.db.get_state(0, "diary_time") == "22:00"
    assert await r.db.get_state(0, "diary_channel") == CH
    s = await P.s_diary(PanelCtx(r.svc, r.bot, A.id, None, []))
    assert s.text is None and "오너" in s.toast


@test
def clean_strips_titles_links_and_length():
    long = "가" * 3000
    out = diary.clean(long + "\n끝", [])
    assert len(out) <= diary.MAX_CHARS
    assert diary.clean("우리 OTC 방 최고 @evilbot t.me/+abc", ["OTC 방"]) == "우리 어떤 방 최고 evilbot [링크 생략]"


@test
async def defaults_pick_the_only_channel_and_auto():
    r = await world()
    for k in ("diary_mode", "diary_channel"):
        await r.db.set_state(0, k, None)
    st = await diary.settings(r.db)
    assert st["diary_mode"] == "auto" and st["diary_channel"] == CH
    await r.db._write("INSERT INTO channels(chat_id, title, active, can_post) VALUES(?,?,1,1)", (-100999, "다른 채널"))
    assert (await diary.settings(r.db))["diary_channel"] is None, "채널이 둘이면 고르게"
