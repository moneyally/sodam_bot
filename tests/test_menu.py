"""버튼 메뉴 점검: python tests/test_menu.py"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import commands, handlers, menu
from sodam.commands import CmdCtx
from sodam.permissions import Role

test, run_all = runner()
CHAT, OTHER = -1001111, -1002222


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={1})
    await db.ensure_chat(CHAT, "내 방")
    await db.ensure_chat(OTHER, "남의 방")
    svc.perms.is_admin = lambda bot, chat_id, uid: _async(uid == 1 and chat_id == CHAT)
    return db, svc, FakeBot()


async def _async(v):
    return v


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


@test
async def main_menu_like_grouphelp():
    db, svc, bot = await setup()
    text, kb = await menu.main_menu(svc, bot, 1)
    add = buttons(kb)[0]
    assert "추가" in add.text and "startgroup=true" in add.url and "restrict_members" in add.url  # 권한 미리 체크
    assert not any("오너" in b.text for b in buttons(kb))                # 일반 사용자 메뉴엔 오너 등록 없음
    text, kb = await menu.groups_menu(svc, bot, 1)
    labels = [b.text for b in buttons(kb)]
    assert any("내 방" in t for t in labels) and not any("남의 방" in t for t in labels)  # 관리하는 방만


@test
async def toggles_and_presets_admin_only():
    db, svc, bot = await setup()
    admin, member = fake_user(1, "방장"), fake_user(20, "멤버")
    assert (await db.get_settings(CHAT))["captcha_enabled"] is True

    text, kb = await menu.group_panel(svc, CHAT)
    cap_btn = next(b for b in buttons(kb) if "캡차" in b.text and "시간" not in b.text)
    assert cap_btn.callback_data == f"m:t:{CHAT}:captcha_enabled:0"      # 목표값(끄기)을 담음
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons(kb) if b.callback_data)  # 텔레그램 64바이트 제한
    for _ in range(2):                                                   # 두 번 눌려도(재전송) 결과 같음
        q = FakeQuery(1, admin)
        await menu.on_callback(svc, bot, q, cap_btn.callback_data.split(":")[1:])
    assert (await db.get_settings(CHAT))["captcha_enabled"] is False and "꺼짐" in q.answers[0][0]
    assert q.edits                                                       # 패널이 새 상태로 갱신

    q = FakeQuery(20, member)                                            # 관리자 아님
    await menu.on_callback(svc, bot, q, ["t", str(CHAT), "captcha_enabled"])
    assert "관리자만" in q.answers[0][0] and (await db.get_settings(CHAT))["captcha_enabled"] is False

    q = FakeQuery(1, admin)                                              # 남의 방 ID 로 위조
    await menu.on_callback(svc, bot, q, ["t", str(OTHER), "ai_enabled"])
    assert "관리자만" in q.answers[0][0]

    q = FakeQuery(CHAT, admin)                                           # 그룹에서 누른 버튼
    await menu.on_callback(svc, bot, q, ["t", str(CHAT), "ai_enabled"])
    assert "1:1" in q.answers[0][0]

    q = FakeQuery(1, admin)                                              # 목록에 없는 설정 키
    await menu.on_callback(svc, bot, q, ["t", str(CHAT), "rules"])
    assert (await db.get_settings(CHAT))["rules"] == ""

    await menu.on_callback(svc, bot, FakeQuery(1, admin), ["s", str(CHAT), "free"])
    assert (await db.get_settings(CHAT))["style"] == "free"
    await menu.on_callback(svc, bot, FakeQuery(1, admin), ["fl", str(CHAT), "strict"])
    s = await db.get_settings(CHAT)
    assert (s["flood_count"], s["flood_mute_minutes"]) == (4, 60)
    text, kb = await menu.group_panel(svc, CHAT)
    assert any(b.text.startswith("● 도배 엄격") for b in buttons(kb))
    assert not any("구독" in b.text for b in buttons(kb))                # 결제 꺼져 있으면 구독 버튼 없음


@test
async def private_start_and_deep_link():
    db, svc, bot = await setup()
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})

    async def dm(user, text):
        msg = FakeMsg(user.id, user, text)
        await handlers.on_private(SimpleNamespace(message=msg), ctx)
        return msg.replies

    admin, member = fake_user(1, "방장"), fake_user(20, "멤버")
    welcome = (await dm(member, "/start"))[0]
    assert "시작하는 방법" in welcome and "1️⃣" in welcome and "USDT" not in welcome  # 첫 안내 문구
    assert "설정" in (await dm(admin, f"/start cfg_{CHAT}"))[0]
    reply = (await dm(member, f"/start cfg_{CHAT}"))[0]                 # 멤버: 거절 대신 일반 메뉴
    assert "관리자용" in reply and "USDT" not in reply and "설정</b>" not in reply
    assert "관리자용" in (await dm(admin, f"/start sub_{OTHER}"))[0]     # 관리하지 않는 방


@test
async def non_admin_inviter_never_gets_price():
    db, svc, bot = await setup()
    from sodam.billing import Billing
    from fakes import cfg
    svc.cfg = cfg(db.path, pay_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
    svc.billing = Billing(svc.cfg, db)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc, "chats": set()})

    def m(status):
        return SimpleNamespace(status=status, is_member=None, user=SimpleNamespace(id=bot.id))

    for adder_id, expect_dm in ((20, False), (1, True)):                # 20=일반 멤버, 1=관리자
        bot.calls.clear()
        upd = SimpleNamespace(my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT, type="supergroup", title="내 방"), from_user=fake_user(adder_id, "초대자"),
            old_chat_member=m("left"), new_chat_member=m("member")))
        await handlers.on_my_chat_member(upd, ctx)
        dms = [c for c in bot.named("send_message") if c[1] == adder_id]
        assert bool(dms) is expect_dm, adder_id
        assert not any("USDT" in c[2] for c in bot.named("send_message") if c[1] == CHAT)


@test
async def settings_command_sends_panel_privately():
    db, svc, bot = await setup()
    admin = fake_user(1, "방장")
    cmd, args, argstr = commands.parse(".설정", "sodambot")
    msg = FakeMsg(CHAT, admin, ".설정")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    dm = next(c for c in bot.named("send_message") if c[1] == 1)
    assert dm[3]["reply_markup"] is not None                            # 패널은 관리자 개인 채팅으로
    assert msg.deleted and "1:1" in [c for c in bot.named("send_message") if c[1] == CHAT][-1][2]
    cmd, args, argstr = commands.parse(".설정 전체", "sodambot")
    msg = FakeMsg(CHAT, admin, ".설정 전체")
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, admin, Role.ADMIN, args, argstr), cmd)
    assert "flood_count" in msg.replies[0]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
