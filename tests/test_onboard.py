"""🚀 빠른 설정 마법사: python tests/test_onboard.py (실제 menu.on_callback 으로, 가짜 텔레그램)"""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeQuery, cfg, fake_user, make_db, make_svc, runner

from sodam import handlers, menu, settings, subscription
from sodam.billing import Billing
from sodam.panels import onboard

test, run_all = runner()
CHAT = -1001234
OTHER = -1005555
ADMIN, MEMBER = 1, 20
PAY = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


async def setup(title="우리 <방> & 친구"):
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    await db.ensure_chat(CHAT, title)
    await db.ensure_chat(OTHER, "남의 방")
    return svc, FakeBot()


async def press(svc, bot, data, uid=ADMIN):
    svc.menu_limiter._hits.clear()
    q = FakeQuery(uid, fake_user(uid, "대표"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers                    # answer 정확히 1번
    for row in (q.kb.inline_keyboard if q.kb else ()):
        for b in row:
            assert b.callback_data is None or len(b.callback_data.encode()) <= 64, b.callback_data
    return q


def cbs(q):
    return [b.callback_data for row in q.kb.inline_keyboard for b in row] if q.kb else []


async def stored(svc, cid=CHAT):
    """캐시 말고 DB 에 저장된 설정."""
    row = await svc.db._one("SELECT settings FROM chats WHERE chat_id=?", (cid,))
    return {**settings.DEFAULTS, **json.loads(row["settings"] or "{}")}


async def count(svc, sql, *args):
    return (await svc.db._one(f"SELECT COUNT(*) AS n FROM {sql}", args))["n"]


async def to_preview(svc, bot, kind="trade", uid=ADMIN):
    await press(svc, bot, f"m:obt:{CHAT}:{kind}", uid)
    q = await press(svc, bot, f"m:obp:{CHAT}", uid)
    apply = next(d for d in cbs(q) if d.startswith("m:obx:"))
    return q, apply


# ── 프리셋 ────────────────────────────────────────────────
@test
def presets_use_only_existing_keys_with_valid_values():
    assert set(onboard.PRESETS) == {"chat", "trade", "game", "notice"}
    for kind, preset in onboard.PRESETS.items():
        for k, v in preset.items():
            assert k in settings.DEFAULTS and k in settings.LABELS, k
            assert type(v) is type(settings.DEFAULTS[k]), (kind, k, v)
            assert settings.coerce(k, onboard._raw(v)) == v, (kind, k, v)
        assert len(onboard.QUESTIONS[kind]) == 3
        for k, _, opts in onboard.QUESTIONS[kind]:
            assert k in preset and all(settings.coerce(k, onboard._raw(val)) == val for _, _, val in opts)
    # 보수적: 거래방은 사기 알림 켜고 스팸 방패는 기록만, 게임방은 도배 느슨·자동 뮤트 없음
    t, g = onboard.PRESETS["trade"], onboard.PRESETS["game"]
    assert t["scam_guard"] and t["scam_action"] == "ask" and t["spamshield_mode"] == "shadow" and not t["link_filter"]
    assert g["gt_action"] == "notify" and g["botlink_mode"] == "observe" and g["flood_count"] >= 10


@test
def validate_rejects_invented_key_and_bad_value():
    for bad in ({"no_such_setting": True}, {"style": "angry"}, {"digest_hour": 99}, {"captcha_enabled": "yes"}):
        saved = dict(onboard.PRESETS["chat"])
        onboard.PRESETS["chat"].update(bad)
        try:
            onboard._validate()
            raise AssertionError(f"통과하면 안 됨: {bad}")
        except ValueError:
            pass
        finally:
            onboard.PRESETS["chat"].clear()
            onboard.PRESETS["chat"].update(saved)
    onboard._validate()


# ── 진입점 ────────────────────────────────────────────────
@test
async def hub_first_row_and_hint_until_onboarded():
    svc, bot = await setup()
    q = await press(svc, bot, f"m:g:{CHAT}")
    assert q.kb.inline_keyboard[0][0].callback_data == f"m:ob:{CHAT}"
    assert onboard.HINT in q.edits[-1]
    text, kb = await menu.group_panel(svc, bot, CHAT, ADMIN)          # 딥링크·.설정 허브도
    assert onboard.HINT in text
    q = await press(svc, bot, f"m:f:{CHAT}")
    assert f"m:ob:{CHAT}" in cbs(q)                                  # 🧩 기능 화면에도
    _, apply = await to_preview(svc, bot)
    await press(svc, bot, apply)
    q = await press(svc, bot, f"m:g:{CHAT}")
    assert onboard.HINT not in q.edits[-1] and f"m:ob:{CHAT}" in cbs(q)
    svc.perms.admins.discard(ADMIN)
    svc.perms.is_admin = lambda b, c, u: asyncio.sleep(0, u == ADMIN)  # 봇관리자(ADMIN 이지만 TG 관리자 아님)
    q = await press(svc, bot, f"m:g:{CHAT}")
    assert f"m:ob:{CHAT}" not in cbs(q) and onboard.HINT not in q.edits[-1]
    q = await press(svc, bot, f"m:f:{CHAT}")
    assert f"m:ob:{CHAT}" not in cbs(q)


@test
async def entry_button_in_dm_after_bot_added():
    svc, bot = await setup()
    svc.cfg = cfg(svc.db.path, pay_address=PAY)
    svc.billing = Billing(svc.cfg, svc.db)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc, "chats": set()})
    new = -1007777

    def m(status):
        return SimpleNamespace(status=status, is_member=None, user=SimpleNamespace(id=bot.id))

    upd = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=new, type="supergroup", title="새 방"), from_user=fake_user(ADMIN, "대표"),
        old_chat_member=m("left"), new_chat_member=m("member")))
    await handlers.on_my_chat_member(upd, ctx)
    dm = [c for c in bot.named("send_message") if c[1] == ADMIN]
    assert dm, bot.calls
    data = [b.callback_data for row in dm[-1][3]["reply_markup"].inline_keyboard for b in row]
    assert f"m:ob:{new}" in data and any(d.startswith("pay:new:") for d in data), data
    room = [c for c in bot.named("send_message") if c[1] == new]
    assert room and "빠른" not in room[0][2]                        # 방엔 그대로 (1:1 에만 버튼)
    q = await press(svc, bot, f"m:ob:{new}")
    assert "방 종류" in q.edits[-1] and f"m:obt:{new}:chat" in cbs(q)
    # 이미 한 방이면 1:1 에 버튼 없음
    await press(svc, bot, f"m:obt:{new}:chat")
    q = await press(svc, bot, f"m:obp:{new}")
    await press(svc, bot, next(d for d in cbs(q) if d.startswith("m:obx:")))
    bot2 = FakeBot()
    await subscription.send_panel_dm(svc, bot2, new, ADMIN)
    kb = bot2.named("send_message")[-1][3]["reply_markup"]
    assert all(not (b.callback_data or "").startswith("m:ob") for row in kb.inline_keyboard for b in row)


# ── 흐름 ──────────────────────────────────────────────────
@test
async def full_flow_preview_diff_and_apply():
    svc, bot = await setup()
    q = await press(svc, bot, f"m:ob:{CHAT}")
    assert "우리 &lt;방&gt; &amp; 친구" in q.edits[-1]                 # 제목 HTML 이스케이프
    assert [d for d in cbs(q) if d.startswith("m:obt:")] == [f"m:obt:{CHAT}:{k}" for k in onboard.TYPES]
    assert f"m:g:{CHAT}" in cbs(q)                                  # ⚙️ 직접 할게요
    q = await press(svc, bot, f"m:obt:{CHAT}:trade")
    assert "2/3" in q.edits[-1] and f"m:oba:{CHAT}:2:1" in cbs(q)
    q = await press(svc, bot, f"m:oba:{CHAT}:0:0")                  # 캡차 아니오
    q = await press(svc, bot, f"m:oba:{CHAT}:9:1")                  # 없는 질문 → 무시
    q = await press(svc, bot, f"m:oba:{CHAT}:0:x")                  # 없는 값 → 무시
    q = await press(svc, bot, f"m:obp:{CHAT}")
    text = q.edits[-1]
    assert "입장 캡차: 켜짐 → <b>꺼짐</b>" in text, text
    assert "사기 의심 검사: 꺼짐 → <b>켜짐</b>" in text
    assert "스팸 방패: 끔 → <b>기록만 (관리자 알림 없음)</b>" in text
    assert "신규 링크 금지(시간): 24 → <b>72</b>" in text
    assert "스팸 명단" not in text                                   # 이미 같은 값은 목록에 없음
    before = await stored(svc)
    target = {**onboard.PRESETS["trade"], "captcha_enabled": False}
    expected = onboard.diff(before, target)
    assert f"({len(expected)}개" in text
    assert (await stored(svc)) == before                            # 미리보기는 안 바꿈
    q = await press(svc, bot, next(d for d in cbs(q) if d.startswith("m:obx:")))
    assert "빠른 설정 완료" in q.edits[-1] and q.answers[0][0] == "✅ 적용했어요"
    after = await stored(svc)
    for k, v in target.items():
        assert after[k] == v, k
    assert {k for k in after if after[k] != before[k]} == {k for k, _, _ in expected}
    assert (await svc.db.get_settings(CHAT))["scam_guard"] is True   # 캐시도 새 값
    log = await svc.db._one("SELECT * FROM mod_log WHERE chat_id=? AND action='onboard:trade'", (CHAT,))
    assert log and log["actor_id"] == ADMIN
    assert {f"m:rules:{CHAT}", f"m:sc:{CHAT}"} <= set(cbs(q)) and any(d.startswith("m:obu:") for d in cbs(q))
    assert await count(svc, "onboard_state") == 0


@test
async def cancel_and_back_change_nothing():
    svc, bot = await setup()
    before = await stored(svc)
    await to_preview(svc, bot, "game")
    q = await press(svc, bot, f"m:obq:{CHAT}")                      # ↩️ 뒤로 → 2단계
    assert "2/3" in q.edits[-1]
    q = await press(svc, bot, f"m:obc:{CHAT}")
    assert "취소" in q.answers[0][0] and await count(svc, "onboard_state") == 0
    assert (await stored(svc)) == before and await count(svc, "mod_log") == 0


@test
async def apply_once_even_when_pressed_twice_at_once():
    svc, bot = await setup()
    _, apply = await to_preview(svc, bot, "game")
    q1, q2 = FakeQuery(ADMIN, fake_user(ADMIN), apply), FakeQuery(ADMIN, fake_user(ADMIN), apply)
    await asyncio.gather(menu.on_callback(svc, bot, q1, apply.split(":")[1:]),
                         menu.on_callback(svc, bot, q2, apply.split(":")[1:]))
    assert await count(svc, "onboard_log") == 1
    assert await count(svc, "mod_log WHERE action LIKE 'onboard:%'") == 1
    texts = q1.edits + q2.edits
    assert sum("빠른 설정 완료" in t for t in texts) == 1 and sum("이미 적용" in t for t in texts) == 1
    q = await press(svc, bot, apply)                                # 나중에 또 눌러도
    assert "이미 적용" in q.edits[-1] and await count(svc, "onboard_log") == 1


@test
async def apply_is_atomic():
    svc, bot = await setup()
    before = await stored(svc)
    _, apply = await to_preview(svc, bot, "notice")
    await svc.db._write("ALTER TABLE mod_log RENAME TO mod_log_off")   # 기록 쓰기가 실패하게
    q = await press(svc, bot, apply)                                 # 예외는 메뉴가 잡아 토스트로 (로딩만 돌지 않게)
    assert "잠시 후" in q.answers[-1][0], q.answers
    await svc.db._write("ALTER TABLE mod_log_off RENAME TO mod_log")
    assert (await stored(svc)) == before                            # 설정 하나도 안 바뀜
    assert await count(svc, "onboard_log") == 0 and await count(svc, "onboard_state") == 1   # 다시 누를 수 있음
    q = await press(svc, bot, apply)
    assert "빠른 설정 완료" in q.edits[-1] and (await stored(svc))["greet_enabled"] is False


@test
async def stale_preview_is_refused():
    svc, bot = await setup()
    _, apply = await to_preview(svc, bot, "chat")
    await press(svc, bot, f"m:oba:{CHAT}:2:free")                   # 미리보기 뒤 말투 바꿈
    q = await press(svc, bot, apply)
    assert q.answers[0][1] and "바뀌었어요" in q.answers[0][0]
    assert await count(svc, "onboard_log") == 0 and (await stored(svc))["style"] == "polite"
    assert "기본 말투: 정중 → <b>자유분방</b>" in q.edits[-1]          # 새 미리보기
    q = await press(svc, bot, next(d for d in cbs(q) if d.startswith("m:obx:")))
    assert (await stored(svc))["style"] == "free"


@test
async def undo_restores_exactly_once():
    svc, bot = await setup()
    await svc.db.set_setting(CHAT, "style", "brief")                # 원래 값이 기본값이 아닌 것도
    before = await stored(svc)
    _, apply = await to_preview(svc, bot, "chat")
    q = await press(svc, bot, apply)
    undo = next(d for d in cbs(q) if d.startswith("m:obu:"))
    assert (await stored(svc)) != before
    q = await press(svc, bot, undo)
    assert "되돌렸어요" in q.edits[-1] and (await stored(svc)) == before
    assert (await svc.db.get_settings(CHAT))["style"] == "brief"
    assert await count(svc, "mod_log WHERE action='onboard_undo'") == 1
    q = await press(svc, bot, undo)                                 # 두 번째는 안 됨
    assert "이미 되돌렸" in q.edits[-1] and await count(svc, "mod_log WHERE action='onboard_undo'") == 1
    assert (await stored(svc)) == before
    q = await press(svc, bot, f"m:g:{CHAT}")                        # 되돌리면 다시 '처음' 안내
    assert onboard.HINT in q.edits[-1]


@test
async def undo_keeps_manual_changes_and_expires():
    svc, bot = await setup()
    _, apply = await to_preview(svc, bot, "chat")
    q = await press(svc, bot, apply)
    undo = next(d for d in cbs(q) if d.startswith("m:obu:"))
    await press(svc, bot, f"m:s:{CHAT}:secretary")                  # 그 사이 직접 바꿈
    q = await press(svc, bot, undo)
    s = await stored(svc)
    assert s["style"] == "secretary" and s["farewell_mode"] == "off" and "직접 바꾼 1개" in q.edits[-1]
    # 10분 지난 되돌리기
    _, apply = await to_preview(svc, bot, "game")
    q = await press(svc, bot, apply)
    undo = next(d for d in cbs(q) if d.startswith("m:obu:"))
    await svc.db._write("UPDATE onboard_log SET ts=ts-? WHERE id=?", (onboard.UNDO_TTL + 1, int(undo.split(":")[3])))
    q = await press(svc, bot, undo)
    assert "지났거나" in q.edits[-1] and (await stored(svc))["gt_enabled"] is True


@test
async def non_admin_and_other_room_refused():
    svc, bot = await setup()
    before = await stored(svc)
    for data in (f"m:ob:{CHAT}", f"m:obt:{CHAT}:trade", f"m:obp:{CHAT}", f"m:obx:{CHAT}:1", f"m:obu:{CHAT}:1"):
        q = await press(svc, bot, data, MEMBER)
        assert q.answers[0][1] and not q.edits, data
    _, apply = await to_preview(svc, bot, "trade")
    # 다른 방: 그 방 관리자가 아니면 거절, 내 초안을 다른 방 번호로 눌러도 안 됨
    svc.perms.is_tg_admin = lambda b, cid, uid: asyncio.sleep(0, uid == ADMIN and cid == CHAT)
    q = await press(svc, bot, apply.replace(str(CHAT), str(OTHER)))
    assert q.answers[0][1] and not q.edits
    # 미리보기 뒤 관리자에서 내려옴 → 적용 거절
    svc.perms.is_tg_admin = lambda b, cid, uid: asyncio.sleep(0, False)
    q = await press(svc, bot, apply)
    assert q.answers[0][1] and not q.edits
    assert (await stored(svc)) == before and (await stored(svc, OTHER))["scam_guard"] is False
    # 되돌리기도 누를 때 다시 확인
    svc.perms.is_tg_admin = lambda b, cid, uid: asyncio.sleep(0, uid == ADMIN)
    q = await press(svc, bot, apply)
    undo = next(d for d in cbs(q) if d.startswith("m:obu:"))
    assert (await press(svc, bot, undo, MEMBER)).edits == []
    svc.perms.is_tg_admin = lambda b, cid, uid: asyncio.sleep(0, False)   # 봇관리자처럼 (ADMIN 이지만 TG 관리자 아님)
    q = await press(svc, bot, undo)
    assert q.answers[0][1] and not q.edits
    svc.perms.is_tg_admin = lambda b, cid, uid: asyncio.sleep(0, uid == ADMIN)
    assert await count(svc, "onboard_log WHERE undone=1") == 0
    # 다른 방 번호로 이 방 기록 되돌리기 → 안 됨
    q = await press(svc, bot, undo.replace(str(CHAT), str(OTHER)))
    assert "지났거나" in q.edits[-1] and await count(svc, "onboard_log WHERE undone=1") == 0


@test
async def expired_state_is_graceful():
    svc, bot = await setup()
    _, apply = await to_preview(svc, bot, "trade")
    await svc.db._write("UPDATE onboard_state SET expires=?", (int(time.time()) - 1,))
    for data in (f"m:obq:{CHAT}", f"m:obp:{CHAT}", f"m:oba:{CHAT}:0:1"):
        q = await press(svc, bot, data)
        assert "시간(30분)이 지났" in q.edits[-1] and f"m:ob:{CHAT}" in cbs(q), data
    q = await press(svc, bot, apply)
    assert "시간이 지난" in q.edits[-1] and await count(svc, "onboard_log") == 0
    q = await press(svc, bot, f"m:ob:{CHAT}")                       # 이어서 하기 버튼 없음
    assert f"m:obq:{CHAT}" not in cbs(q)


@test
async def state_survives_restart():
    svc, bot = await setup()
    await press(svc, bot, f"m:obt:{CHAT}:game")
    await press(svc, bot, f"m:oba:{CHAT}:0:0")
    svc.menu_tokens.clear()
    svc.db._settings_cache.clear()                                  # 메모리 없이 DB 만으로
    q = await press(svc, bot, f"m:ob:{CHAT}")
    assert f"m:obq:{CHAT}" in cbs(q) and "게임" in q.kb.inline_keyboard[0][0].text
    q = await press(svc, bot, f"m:obp:{CHAT}")
    assert "포인트 게임(! 명령): 켜짐 → <b>꺼짐</b>" in q.edits[-1], q.edits[-1]
    await press(svc, bot, next(d for d in cbs(q) if d.startswith("m:obx:")))
    assert (await stored(svc))["casino_enabled"] is False and (await stored(svc))["gt_enabled"] is True


@test
async def callbacks_fit_64_bytes_with_longest_chat_id():
    svc, bot = await setup()
    big = -100999999999999999
    await svc.db.ensure_chat(big, "긴 방")
    for kind in onboard.TYPES:
        await press(svc, bot, f"m:obt:{big}:{kind}")                # press() 가 64바이트 검사
        q = await press(svc, bot, f"m:obp:{big}")
        q = await press(svc, bot, next(d for d in cbs(q) if d.startswith("m:obx:")))
        assert any(d.startswith("m:obu:") for d in cbs(q))


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
