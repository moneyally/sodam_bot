"""📢 채널 구독 필수 (sodam/subgate.py). 2026-10-07 백악관 요청. python tests/run_all.py subgate"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import FakeQuery, fake_user, runner  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from sodam import handlers, hooks, subgate  # noqa: E402

test, run_all = runner()
BOSS = fake_user(10, "대표", "boss")
NEW = fake_user(30, "신입", "newbie")
SUBS = {"@wh_channel": set()}


async def room(**settings):
    subgate._ok.clear()
    subgate._nag.clear()
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False, "recent_account_captcha": False,
                                                      "greet_enabled": False, "subgate_mode": "on",
                                                      "subgate_channel": "@wh_channel", **settings})
    real = r.bot.get_chat_member

    async def gcm(chat_id, user_id):
        if chat_id == "@wh_channel":
            if user_id == r.bot.id:
                return SimpleNamespace(status="administrator")
            if user_id in SUBS["@wh_channel"]:
                return SimpleNamespace(status="member")
            raise BadRequest("User not found")
        return await real(chat_id, user_id)
    r.bot.get_chat_member = gcm
    return r


async def join(r, user):
    m = SimpleNamespace(chat_id=r.CHAT, chat=SimpleNamespace(id=r.CHAT, title="방", type="supergroup"),
                        new_chat_members=[user], from_user=user)

    async def delete():
        return True
    m.delete = delete
    await handlers.on_join(SimpleNamespace(message=m), r.ctx)


async def press(r, user, target):
    q = FakeQuery(user.id, user, f"sg:{target}")
    q.message = SimpleNamespace(chat_id=r.CHAT, message_id=999, delete=_noop)
    await hooks.CALLBACK_HANDLERS["sg"](r.svc, r.bot, q, [str(target)])
    return q


async def _noop():
    return True


@test
async def new_member_blocked_until_subscribed_then_released_by_own_button():
    SUBS["@wh_channel"] = set()
    r = await room()
    await join(r, NEW)
    restrict = [c for c in r.bot.calls if c[0] == "restrict" and c[2] == NEW.id]
    assert restrict and not restrict[-1][3].can_send_messages, r.bot.calls
    prompt = [c for c in r.bot.named("send_message") if "채널 구독" in c[2]]
    assert prompt, r.bot.calls
    kb = prompt[-1][3]["reply_markup"].inline_keyboard
    assert kb[0][0].url == "https://t.me/wh_channel" and kb[1][0].callback_data == f"sg:{NEW.id}"
    rows = await r.db._all("SELECT action, detail FROM mod_log WHERE target_id=? ORDER BY id", (NEW.id,))
    assert [x["action"] for x in rows][:2] == ["join_info", "subgate"], [dict(x) for x in rows]   # 누가·어떻게 + 구독 안 함
    assert "직접 입장" in rows[0]["detail"] and "@newbie" in rows[0]["detail"]
    other = await press(r, BOSS, NEW.id)                       # 남이 누르면 안 됨
    assert "본인만" in str(other.answers)
    q = await press(r, NEW, NEW.id)                            # 아직 구독 안 함
    assert "아직" in str(q.answers)
    SUBS["@wh_channel"].add(NEW.id)
    q = await press(r, NEW, NEW.id)
    assert "확인됐어요" in str(q.answers)
    assert any(c[0] == "restrict" and c[2] == NEW.id and c[3].can_send_messages for c in r.bot.calls), "채팅 금지 풀림"


@test
async def existing_member_message_deleted_and_off_mode_or_subscriber_passes():
    SUBS["@wh_channel"] = set()
    r = await room()
    old = fake_user(40, "기존", "old")
    await r.join(old)
    await r.say(old, "안녕하세요")
    assert any("채널 구독" in c[2] for c in r.bot.named("send_message"))
    assert any(c[0] == "restrict" and c[2] == old.id for c in r.bot.calls)
    st = await r.db.get_settings(r.CHAT)                       # 구독 안 한 사람은 캐시돼도 계속 '아님'
    assert await subgate.check(r.svc, r.bot, r.CHAT, old, st) is False
    assert await subgate.check(r.svc, r.bot, r.CHAT, old, st) is False
    SUBS["@wh_channel"].add(old.id)                            # 구독자는 그대로
    r2 = await room()
    await join(r2, old)                                        # 입장 때 이미 구독 → subgate_pass 기록
    assert (await r2.db._one("SELECT action FROM mod_log WHERE target_id=? AND action LIKE 'subgate%'", (old.id,)))["action"] == "subgate_pass"
    await r2.say(old, "안녕하세요")
    assert not any(c[0] == "restrict" and c[2] == old.id for c in r2.bot.calls)
    r3 = await room(subgate_mode="off")                         # 꺼져 있으면 아무 일 없음
    await r3.join(NEW)
    await join(r3, NEW)
    assert not any(c[0] == "restrict" for c in r3.bot.calls)


@test
async def blind_when_bot_not_channel_admin_does_not_block():
    r = await room(subgate_channel="@other_ch")
    async def gcm(chat_id, user_id):
        raise BadRequest("Member list is inaccessible")
    r.bot.get_chat_member = gcm
    await join(r, NEW)
    assert not any(c[0] == "restrict" for c in r.bot.calls)


@test
def channel_value_validated():
    assert subgate._channel("https://t.me/abcd_ch") == "@abcd_ch" and subgate._channel("abcd_ch") == "@abcd_ch"
    assert subgate._channel("-1001234567890") == "-1001234567890"
    try:
        subgate._channel("http://evil.com/x")
        raise AssertionError("should fail")
    except ValueError:
        pass


if __name__ == "__main__":
    run_all()
