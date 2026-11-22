"""📢 채널 구독 필수 (sodam/subgate.py). 2026-10-07 백악관 요청. python tests/run_all.py subgate"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import FakeQuery, fake_user, runner  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from sodam import handlers, hooks, menu, subgate  # noqa: E402
from sodam.panels import subgate as subgate_panel  # noqa: E402

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
async def prompt_comes_after_the_join_greeting():
    """2026-10-10 뉴월드: 인사는 5초 모아서 나가는데 구독 안내가 바로 떠서 안내가 인사 위에 깔렸음 → 인사 먼저."""
    from sodam import greet
    SUBS["@wh_channel"] = set()
    r = await room(greet_enabled=True, greet_template="환영합니다 {names}")
    r.svc.greeter = greet.Greeter(r.svc)                       # 진짜 인사기 (5초 모아서 보냄)
    old_wait = greet.WAIT_SECONDS
    greet.WAIT_SECONDS = 0.05
    try:
        await join(r, NEW)
        assert any(c[0] == "restrict" and c[2] == NEW.id for c in r.bot.calls), "채팅 금지는 바로"
        assert not any("채널 구독" in c[2] for c in r.bot.named("send_message")), "안내는 인사 뒤에"
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any("채널 구독" in c[2] for c in r.bot.named("send_message")):
                break
    finally:
        greet.WAIT_SECONDS = old_wait
    texts = [c[2] for c in r.bot.named("send_message") if c[1] == r.CHAT]
    gi = next(i for i, t in enumerate(texts) if "환영합니다" in t)
    pi = next(i for i, t in enumerate(texts) if "채널 구독" in t)
    assert gi < pi, texts


@test
def channel_value_validated():
    assert subgate._channel("https://t.me/abcd_ch") == "@abcd_ch" and subgate._channel("abcd_ch") == "@abcd_ch"
    assert subgate._channel("-1001234567890") == "-1001234567890"
    try:
        subgate._channel("http://evil.com/x")
        raise AssertionError("should fail")
    except ValueError:
        pass


@test
async def turning_off_releases_everyone_blocked():
    # 2026-11-22 오너(수리남 소통방): 채널 구독 필수를 끄면 그걸로 막은 사람을 자동 복구.
    # 예전엔 끄면 '새로 안 막음'만 하고 이미 막은 사람은 그대로 남아 "off 인데 막힘" 상태였음.
    SUBS["@wh_channel"] = set()
    r = await room()
    await join(r, NEW)
    assert any(c[0] == "restrict" and c[2] == NEW.id and not c[3].can_send_messages for c in r.bot.calls)
    assert await r.db._one("SELECT 1 AS x FROM subgate_blocked WHERE chat_id=? AND user_id=?", (r.CHAT, NEW.id)), "막은 사람 기록"
    # 켜져 있는 동안 틱 → 안 풂 (구독으로 풀어야)
    await subgate.release_tick(r.svc, r.bot)
    assert await r.db._one("SELECT 1 AS x FROM subgate_blocked WHERE user_id=?", (NEW.id,)), "켜진 방은 그대로"
    # 끄고 틱 → 자동 복구
    await r.db.set_setting(r.CHAT, "subgate_mode", "off")
    before = sum(1 for c in r.bot.calls if c[0] == "restrict" and c[2] == NEW.id and c[3].can_send_messages)
    await subgate.release_tick(r.svc, r.bot)
    after = sum(1 for c in r.bot.calls if c[0] == "restrict" and c[2] == NEW.id and c[3].can_send_messages)
    assert after > before, "꺼지면 채팅 금지 풀림(all_permissions)"
    assert not await r.db._one("SELECT 1 AS x FROM subgate_blocked WHERE user_id=?", (NEW.id,)), "테이블 비움"
    # 다시 틱 해도 또 안 건드림 (한 번만)
    n = sum(1 for c in r.bot.calls if c[0] == "restrict" and c[2] == NEW.id)
    await subgate.release_tick(r.svc, r.bot)
    assert sum(1 for c in r.bot.calls if c[0] == "restrict" and c[2] == NEW.id) == n, "한 번만 복구"


@test
async def subscribing_removes_from_blocked_list():
    # 구독 확인으로 풀면 subgate_blocked 에서도 빠져서, 나중에 꺼도 틱이 또 안 건드림
    SUBS["@wh_channel"] = set()
    r = await room()
    await join(r, NEW)
    assert await r.db._one("SELECT 1 AS x FROM subgate_blocked WHERE user_id=?", (NEW.id,))
    SUBS["@wh_channel"].add(NEW.id)
    await press(r, NEW, NEW.id)   # 구독 확인
    assert not await r.db._one("SELECT 1 AS x FROM subgate_blocked WHERE user_id=?", (NEW.id,)), "구독하면 목록에서 빠짐"


@test
async def clear_channel_button_removes_channel_and_releases_blocked():
    # 2026-11-22 오너(클린뱅크방): 채널값을 잘못 넣었는데 지우는 버튼이 없었음 → 🗑 채널 지우기 추가.
    SUBS["@wh_channel"] = set()
    r = await room()
    await join(r, NEW)                                        # 켜진 상태라 막힘
    assert await r.db._one("SELECT 1 AS x FROM subgate_blocked WHERE user_id=?", (NEW.id,))
    c = menu.PanelCtx(r.svc, r.bot, BOSS.id, r.CHAT, [])
    sc = await subgate_panel.s_sgt(c)                         # 채널 있으면 지우기 버튼이 보임
    data = [b.callback_data for row in sc.kb.inline_keyboard for b in row]
    assert f"m:sgcx:{r.CHAT}" in data, data
    await subgate_panel.s_sgcx(c)                             # 🗑 채널 지우기
    s = await r.db.get_settings(r.CHAT)
    assert not s["subgate_channel"] and s["subgate_mode"] == "off", s
    sc2 = await subgate_panel.s_sgt(c)                        # 지운 뒤엔 버튼 사라짐
    assert f"m:sgcx:{r.CHAT}" not in [b.callback_data for row in sc2.kb.inline_keyboard for b in row]
    await subgate.release_tick(r.svc, r.bot)                  # 끄면 막던 사람 자동 복구
    assert any(x[0] == "restrict" and x[2] == NEW.id and x[3].can_send_messages for x in r.bot.calls), r.bot.calls


if __name__ == "__main__":
    run_all()
