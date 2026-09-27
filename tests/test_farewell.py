"""👋 퇴장 인사: python tests/test_farewell.py (실제 handlers.on_left / on_chat_member 로, 가짜 텔레그램)"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from telegram.error import BadRequest

from sodam import farewell, handlers, menu, raid

test, run_all = runner()
CHAT = -1001234
LONG = -100999999999999999
ADMIN = 1


def ctx_of(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})


async def setup(mode="on", style="polite", **settings):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, "방")
    await db.set_setting(CHAT, "style", style)
    if mode:
        await db.set_setting(CHAT, "farewell_mode", mode)
    for k, v in settings.items():
        await db.set_setting(CHAT, k, v)
    bot = FakeBot()
    return svc, bot, ctx_of(svc, bot)


async def service_left(ctx, user, by=None):
    """'OO님이 나갔습니다' 서비스 메시지. by = 한 사람 (스스로 나가면 본인)."""
    msg = SimpleNamespace(chat_id=CHAT, left_chat_member=user, from_user=by or user)
    await handlers.on_left(SimpleNamespace(message=msg), ctx)


async def member_left(ctx, user, by=None, status="left"):
    """멤버 상태 변경(chat_member) — 서비스 메시지를 숨긴 방에서도 온다."""
    def m(st):
        return SimpleNamespace(status=st, is_member=None, user=user)
    upd = SimpleNamespace(chat_member=SimpleNamespace(chat=SimpleNamespace(id=CHAT, type="supergroup", title="방"),
                                                      old_chat_member=m("member"), new_chat_member=m(status),
                                                      from_user=by or user))
    await handlers.on_chat_member(upd, ctx)


def farewells(bot, chat=CHAT):
    return [c for c in bot.named("send_message") if c[1] == chat and not c[2].startswith("🚨")]   # 🚨 = 대량 입장 안내


@test
async def off_by_default():
    svc, bot, ctx = await setup(mode=None)
    assert (await svc.db.get_settings(CHAT))["farewell_mode"] == "off"
    u = fake_user(50, "떠난이", "bye50")
    await service_left(ctx, u)
    await member_left(ctx, fake_user(51, "또떠남"))
    assert farewells(bot) == []


@test
async def voluntary_service_message_polite_default():
    svc, bot, ctx = await setup(style="polite")
    await service_left(ctx, fake_user(50, "떠난이", "bye50"))
    (call,) = farewells(bot)
    text = call[2]
    assert "떠난이 대표님이 방을 나가셨습니다" in text and "감사했습니다" in text, text
    assert "🆔 <code>50</code> · @bye50" in text, text
    assert "tg://user" not in text and "<a " not in text          # 나간 사람은 멘션 아님
    assert call[3]["parse_mode"] == "HTML" and call[3]["reply_markup"] is None
    assert ("get_chat_member_count", CHAT) not in bot.calls        # {count} 없으면 조회 안 함


@test
async def free_style_is_banmal():
    svc, bot, ctx = await setup(style="free")
    await member_left(ctx, fake_user(60, "철수"))
    (call,) = farewells(bot)
    assert call[2].startswith("철수 나갔네. 잘 가~"), call[2]
    assert "대표님" not in call[2] and "요" not in call[2] and "니다" not in call[2]
    assert "🆔 <code>60</code> · 아이디 없음" in call[2]


@test
async def every_style_has_default_and_voice():
    for key in farewell.DEFAULTS:
        assert "{name}" in farewell.DEFAULTS[key]
    from sodam.styles import STYLES
    assert set(STYLES) <= set(farewell.DEFAULTS)
    for key in ("polite", "friendly", "brief", "secretary", "tsundere"):
        assert any(e in farewell.DEFAULTS[key] for e in ("요", "니다")), key
    assert farewell.template_of({"style": "없는말투", "farewell_template": ""}) == farewell.DEFAULTS["polite"]


@test
async def kicked_or_banned_by_admin_no_farewell():
    svc, bot, ctx = await setup()
    admin = fake_user(ADMIN, "관리자")
    await service_left(ctx, fake_user(70, "킥당함"), by=admin)               # 관리자가 내보냄 (서비스 메시지)
    await member_left(ctx, fake_user(71, "킥당함2"), by=admin)               # kick = left, 한 사람이 관리자
    await member_left(ctx, fake_user(72, "밴당함"), by=admin, status="kicked")
    await member_left(ctx, fake_user(73, "밴당함2"), status="kicked")       # 밴이면 한 사람이 누구든 없음
    assert farewells(bot) == []


@test
async def auto_moderation_kick_no_farewell():
    """소담 자동 관리(스팸·CAS·대량 입장 kick·캡차 시간 초과·공동 차단)는 봇이 한 사람."""
    svc, bot, ctx = await setup()
    me = fake_user(bot.id, "소담", "sodambot", is_bot=True)
    await service_left(ctx, fake_user(80, "스패머"), by=me)
    await member_left(ctx, fake_user(81, "스패머2"), by=me, status="kicked")
    await member_left(ctx, fake_user(82, "캡차실패"), by=me)
    assert farewells(bot) == []


@test
async def bot_leave_ignored():
    svc, bot, ctx = await setup()
    b = fake_user(90, "게임봇", "gamebot", is_bot=True)
    await service_left(ctx, b)
    await member_left(ctx, b)
    assert farewells(bot) == []


@test
async def captcha_pending_no_farewell():
    svc, bot, ctx = await setup()
    await svc.db.add_captcha(CHAT, 95, 5, "x", 2_000_000_000)
    await member_left(ctx, fake_user(95, "캡차대기"))
    assert farewells(bot) == []


@test
async def both_paths_one_farewell():
    """서비스 메시지·멤버 상태 변경이 둘 다 와도(순서 무관·동시) 인사 1번, 하나만 와도 인사."""
    old, farewell.WINDOW = farewell.WINDOW, 0.1
    try:
        for order in ("service_first", "member_first", "together"):
            svc, bot, ctx = await setup()
            u = fake_user(100, "둘다")
            if order == "service_first":
                await service_left(ctx, u)
                await member_left(ctx, u)
            elif order == "member_first":
                await member_left(ctx, u)
                await service_left(ctx, u)
            else:
                await asyncio.gather(service_left(ctx, u), member_left(ctx, u))
            await asyncio.sleep(0.25)                        # 다음 창이 지나도 두 번째 인사 없음
            assert len(farewells(bot)) == 1, (order, farewells(bot))
        svc, bot, ctx = await setup()
        await member_left(ctx, fake_user(102, "상태만"))
        await service_left(ctx, fake_user(103, "메시지만"))
        await asyncio.sleep(0.25)
        texts = [c[2] for c in farewells(bot)]
        assert len(texts) == 2 and "상태만" in texts[0] and "메시지만" in texts[1], texts
    finally:
        farewell.WINDOW = old


@test
async def placeholders_escaped_and_count():
    svc, bot, ctx = await setup(farewell_template="잘가요 <b>{name}</b> & {username} / {id}\n현재 {count}명")
    bot.member_count = 41
    await service_left(ctx, fake_user(110, "<b>해커</b>&co", "h_ck"))
    text = farewells(bot)[0][2]
    assert "잘가요 &lt;b&gt;&lt;b&gt;해커&lt;/b&gt;&amp;co&lt;/b&gt; &amp; @h_ck / <code>110</code>" in text, text
    assert text.endswith("\n현재 41명") and "🆔" not in text        # {id} 를 직접 쓰면 🆔 줄은 안 붙임
    assert "<b>" not in text
    assert ("get_chat_member_count", CHAT) in bot.calls


@test
async def count_unavailable_drops_line_and_id_line_added():
    svc, bot, ctx = await setup(farewell_template="안녕히 가세요\n남은 인원 {count}명")

    async def boom(chat_id):
        raise BadRequest("no")
    bot.get_chat_member_count = boom
    await service_left(ctx, fake_user(120, "누구"))
    text = farewells(bot)[0][2]
    assert "{count}" not in text and "남은 인원" not in text
    assert text.startswith("누구 안녕히 가세요") and text.endswith("🆔 <code>120</code> · 아이디 없음"), text


@test
async def rate_limit_merges_next_one():
    old = farewell.WINDOW
    farewell.WINDOW = 0.3
    try:
        svc, bot, ctx = await setup(style="brief")
        await service_left(ctx, fake_user(130, "첫째", "one"))
        assert len(farewells(bot)) == 1
        for uid, name in ((131, "둘째"), (132, "셋째"), (133, "넷째")):
            await member_left(ctx, fake_user(uid, name, None if uid == 132 else f"u{uid}"))
        assert len(farewells(bot)) == 1                      # 창 안에서는 안 보냄
        await asyncio.sleep(0.45)
        calls = farewells(bot)
        assert len(calls) == 2, calls
        text = calls[1][2]
        assert text.startswith("둘째 외 2명님이 나갔습니다."), text
        assert "<code>131</code>, <code>132</code>, <code>133</code>" in text and "@u131, 아이디 없음, @u133" in text
    finally:
        farewell.WINDOW = old


@test
async def raid_defense_suppresses():
    svc, bot, ctx = await setup()
    await raid.start(svc, bot, CHAT, 30, actor_id=ADMIN)
    await service_left(ctx, fake_user(140, "방어중"))
    await member_left(ctx, fake_user(141, "방어중2"))
    assert farewells(bot) == []
    await raid.stop(svc, bot, CHAT, ADMIN)
    await service_left(ctx, fake_user(142, "끝난뒤"))
    assert len(farewells(bot)) == 1


@test
async def auto_delete_scheduled():
    svc, bot, ctx = await setup(farewell_delete_after=600)
    await service_left(ctx, fake_user(150, "지울것"))
    (job,) = ctx.job_queue.once
    cb, when, data = job
    assert cb is handlers._delete_job and when == 600
    assert data == (CHAT, bot._next_id)
    svc, bot, ctx = await setup()                           # 기본 = 안 지움
    await service_left(ctx, fake_user(151, "남길것"))
    assert farewells(bot) and ctx.job_queue.once == []


@test
async def buttons_attached_and_bad_ones_dropped():
    svc, bot, ctx = await setup(farewell_buttons=[["규칙", "https://example.com/r"], ["나쁨", "javascript:alert(1)"]])
    await service_left(ctx, fake_user(160, "버튼"))
    kb = farewells(bot)[0][3]["reply_markup"]
    urls = [b.url for row in kb.inline_keyboard for b in row]
    assert urls == ["https://example.com/r"]


@test
async def send_failure_does_not_break_leave():
    svc, bot, ctx = await setup()

    async def fail(*a, **kw):
        raise BadRequest("Chat_write_forbidden")
    bot.send_message = fail
    await service_left(ctx, fake_user(170, "실패"))          # 예외 없이 끝나야 함
    bot.send_message = FakeBot.send_message.__get__(bot)
    await member_left(ctx, fake_user(171, "다음"))            # 창(20초) 안 → 합쳐서 나중에, 지금은 조용
    assert farewells(bot) == []


# ── 화면 ─────────────────────────────────────────────────
async def press(svc, bot, uid, data):
    q = FakeQuery(uid, fake_user(uid, "방장"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row] if kb else []


def find(kb, text):
    return next(b for b in buttons(kb) if text in b.text)


@test
async def panel_flow():
    svc, bot, ctx = await setup(mode=None, style="free")
    await svc.db.ensure_chat(LONG, "긴 방")
    q = await press(svc, bot, ADMIN, f"m:g:{CHAT}")
    assert find(q.kb, "퇴장 인사").callback_data == f"m:fw:{CHAT}"
    q = await press(svc, bot, ADMIN, f"m:fw:{CHAT}")
    assert "❌ 꺼짐" in q.edits[-1] and "나갔네. 잘 가~" in q.edits[-1] and "자유분방" in q.edits[-1]
    q = await press(svc, bot, ADMIN, find(q.kb, "✅ 켜기").callback_data)
    assert (await svc.db.get_settings(CHAT))["farewell_mode"] == "on" and "✅ 켜짐" in q.edits[-1]
    q = await press(svc, bot, ADMIN, find(q.kb, "10분").callback_data)
    assert (await svc.db.get_settings(CHAT))["farewell_delete_after"] == 600
    # 멤버는 화면을 못 연다
    q = await press(svc, bot, 20, f"m:fw:{CHAT}")
    assert not q.edits and q.answers[0][1]
    # 문구 입력 (글자 입력 엔진)
    q = await press(svc, bot, ADMIN, f"m:in:{CHAT}:fwt")
    assert "{name}" in q.edits[-1]
    msg = FakeMsg(ADMIN, fake_user(ADMIN, "방장"), "잘 가요 {name} <3")
    await handlers.on_private(SimpleNamespace(message=msg), ctx)
    assert "저장했어요" in msg.replies[-1] and "🆔 줄은 맨 아래" in msg.replies[-1]
    assert (await svc.db.get_settings(CHAT))["farewell_template"] == "잘 가요 {name} <3"
    # URL 버튼 (입장 인사와 같은 형식·검사)
    await press(svc, bot, ADMIN, f"m:in:{CHAT}:fwb")
    msg = FakeMsg(ADMIN, fake_user(ADMIN, "방장"), "공지 - http://bad.example")
    await handlers.on_private(SimpleNamespace(message=msg), ctx)
    assert "https://" in msg.replies[-1] and (await svc.db.get_settings(CHAT))["farewell_buttons"] == []
    msg = FakeMsg(ADMIN, fake_user(ADMIN, "방장"), "공지 - https://t.me/notice")
    await handlers.on_private(SimpleNamespace(message=msg), ctx)
    assert (await svc.db.get_settings(CHAT))["farewell_buttons"] == [["공지", "https://t.me/notice"]]
    # 미리보기 = 1:1 로, 관리자 본인 이름·ID + 닫기
    await svc.db.upsert_user(fake_user(ADMIN, "방장<b>", "boss"))   # 1:1 대화가 이름을 덮었으니 다시
    q = await press(svc, bot, ADMIN, f"m:fwv:{CHAT}")
    sent = [c for c in bot.named("send_message") if c[1] == ADMIN][-1]
    assert "잘 가요 방장&lt;b&gt; &lt;3" in sent[2] and "🆔 <code>1</code> · @boss" in sent[2], sent[2]
    kb = sent[3]["reply_markup"]
    assert [b.url for b in buttons(kb) if b.url] == ["https://t.me/notice"]
    await press(svc, bot, ADMIN, find(kb, "닫기").callback_data)
    assert ("delete", ADMIN, bot._next_id) in bot.calls
    # 삭제 (확인 → 토큰)
    q = await press(svc, bot, ADMIN, f"m:fwd:{CHAT}:t")
    q = await press(svc, bot, ADMIN, find(q.kb, "삭제").callback_data)
    assert (await svc.db.get_settings(CHAT))["farewell_template"] == "" and "삭제했어요" in q.answers[-1][0]
    details = [r["detail"] for r in await svc.db.recent_mod_log(CHAT, 50)]
    assert any(d.startswith("farewell_template=잘 가요") for d in details) and "farewell_mode=on" in details
    # 긴 방 ID 에서도 64바이트 이하
    svc.perms.admins.add(ADMIN)
    q = await press(svc, bot, ADMIN, f"m:fw:{LONG}")
    q = await press(svc, bot, ADMIN, f"m:in:{LONG}:fwb")
    q = await press(svc, bot, ADMIN, f"m:fw:{LONG}")
    for b in buttons(q.kb):
        assert len(b.callback_data.encode()) <= 64, b.callback_data


@test
async def set_command_values_validated():
    from sodam.settings import coerce, render
    assert coerce("farewell_mode", "켜기") == "on" and coerce("farewell_mode", "off") == "off"
    for bad in ("maybe",):
        try:
            coerce("farewell_mode", bad)
            raise AssertionError("accepted")
        except ValueError:
            pass
    try:
        coerce("farewell_delete_after", "99999")
        raise AssertionError("accepted")
    except ValueError:
        pass
    assert render("farewell_mode", "on") == "켜짐"


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
