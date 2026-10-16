"""🤖 로봇 인증 (가입 신청 숫자 입력, 2026-10-16 뉴월드 '입요 넣기 전에 로봇이 아닙니다 인증'): python tests/run_all.py robotcheck

join_verify_kind=number → 1:1 에 비뚤게 그린 숫자 그림 + 숫자 버튼 판. 버튼으로 누르거나 1:1 채팅에 숫자를 보내면 확인 →
맞으면 승인, 틀리면 새 숫자, 3번 틀리면 거절, 시간 지나면 거절. 관리자 '🧪 시험'은 승인 안 함. 허브 🤖 화면은 권한·승인제 점검.
"""
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeMsg, FakeQuery, fake_user, runner

from sodam import handlers, joinreq, menu

test, run_all = runner()

ADMIN = fake_user(1, "방장")
U = fake_user(100_000_000, "신청자")


async def room(**settings):
    return await Room().open(admins={1}, settings={"captcha_enabled": False, "greet_enabled": False, "join_verify": True,
                                                    "join_verify_kind": "number", **settings})


async def apply(r, user=U):
    req = SimpleNamespace(chat=SimpleNamespace(id=Room.CHAT, type="supergroup"), from_user=user, user_chat_id=user.id)
    await handlers.on_join_request(SimpleNamespace(chat_join_request=req), r.ctx)


async def key(r, k, user=U):
    q = FakeQuery(user.id, user, f"jr:{Room.CHAT}:k:{k}")
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    return q


async def code(r, uid=U.id):
    return str((await joinreq._row(r.svc, Room.CHAT, uid))["answer"])


async def dm(r, text, user=U):
    m = FakeMsg(user.id, user, text, message_id=7)
    await handlers.on_private(SimpleNamespace(message=m), r.ctx)
    return m


@test
async def number_dm_has_picture_and_keypad_and_buttons_approve():
    r = await room()
    await apply(r)
    [photo] = r.bot.named("send_photo")
    assert photo[1] == U.id and "로봇 인증" in photo[3] and "4자리" in photo[3]
    data = [b.callback_data for row in photo[4]["reply_markup"].inline_keyboard for b in row]
    assert all(d.startswith(f"jr:{Room.CHAT}:k:") and len(d.encode()) <= 64 for d in data) and len(data) == 12
    c = await code(r)
    assert len(c) == 4 and c[0] != "0"
    for ch in c[:-1]:
        await key(r, ch)
    assert not r.bot.named("approve") and (await joinreq._row(r.svc, Room.CHAT, U.id))["typed"] == c[:-1]
    await key(r, c[-1])
    assert r.bot.named("approve") == [("approve", Room.CHAT, U.id)]
    assert "확인됐어요" in r.bot.named("edit_caption")[-1][3]
    q = await key(r, "1")                                    # 끝난 뒤 또 누름 = 승인 한 번
    assert q.answers[-1][0] == "이미 끝난 확인이에요." and len(r.bot.named("approve")) == 1


@test
async def backspace_and_new_code_do_not_cost_attempts():
    r = await room()
    await apply(r)
    await key(r, "9")
    await key(r, "b")
    assert (await joinreq._row(r.svc, Room.CHAT, U.id))["typed"] == ""
    old = await code(r)
    await key(r, "n")
    row = await joinreq._row(r.svc, Room.CHAT, U.id)
    assert row["attempts"] == 0 and r.bot.named("edit_media"), "새 숫자 = 그림 바꿈, 기회 그대로"
    assert len(str(row["answer"])) == len(old)


@test
async def typed_digits_in_dm_approve_even_fullwidth_spaced():
    r = await room()
    await apply(r)
    c = await code(r)
    wide = " ".join(chr(ord(ch) + 0xFEE0) for ch in c)      # '４ ２ ７ ５' 처럼 보내도
    m = await dm(r, wide)
    assert r.bot.named("approve") == [("approve", Room.CHAT, U.id)] and "확인됐어요" in m.replies[-1]


@test
async def wrong_gives_new_code_then_three_wrong_declines():
    r = await room()
    await apply(r)
    first = await code(r)
    bad = "1111" if first != "1111" else "2222"
    m = await dm(r, bad)
    row = await joinreq._row(r.svc, Room.CHAT, U.id)
    assert row["attempts"] == 1 and "남은 기회 2번" in m.replies[-1] and r.bot.named("edit_media")
    assert not r.bot.named("decline") and not r.bot.named("approve")
    for _ in range(2):
        c = await code(r)
        await dm(r, "1111" if c != "1111" else "2222")
    assert ("decline", Room.CHAT, U.id) in r.bot.calls and not r.bot.named("approve")
    assert not await joinreq._row(r.svc, Room.CHAT, U.id)


@test
async def digits_without_pending_check_go_on_to_normal_dm():
    r = await room()
    assert not await joinreq.on_dm_text(r.svc, r.bot, FakeMsg(U.id, U, "1234"))
    await apply(r)
    assert not await joinreq.on_dm_text(r.svc, r.bot, FakeMsg(U.id, U, "안녕 1234")), "숫자만일 때만"


@test
async def timeout_declines_and_marks_message():
    r = await room()
    await apply(r)
    await r.db._write("UPDATE join_requests SET expires=0 WHERE user_id=?", (U.id,))
    await handlers.job_tick(r.ctx)
    assert ("decline", Room.CHAT, U.id) in r.bot.calls
    assert "시간이 지나" in r.bot.named("edit_caption")[-1][3]


@test
async def puzzle_rooms_unchanged_and_buttons_do_not_cross():
    r = await room(join_verify_kind="puzzle")
    await apply(r)
    assert r.bot.named("send_message") and not r.bot.named("send_photo")
    q = await key(r, "1")                                    # 그림 방식 신청에 숫자 버튼 = 무시
    assert q.answers[-1][0] == "이미 끝난 확인이에요." and not r.bot.named("approve")


@test
async def admin_test_never_approves():
    r = await room()
    q = FakeQuery(1, ADMIN, "")
    await menu.on_callback(r.svc, r.bot, q, ["jvt", str(Room.CHAT)])
    [photo] = r.bot.named("send_photo")
    assert photo[1] == ADMIN.id and "시험" in photo[3]
    m = await dm(r, await code(r, ADMIN.id), ADMIN)
    assert "시험 통과" in m.replies[-1] and not r.bot.named("approve")
    assert not await joinreq._row(r.svc, Room.CHAT, ADMIN.id)


@test
async def hub_screen_warns_missing_invite_right_and_members_cannot_open():
    r = await room()
    r.bot.can_moderate = False                               # 소담에게 관리 권한(사용자 초대 포함) 없음
    q = FakeQuery(1, ADMIN, "")
    await menu.on_callback(r.svc, r.bot, q, ["jv", str(Room.CHAT)])
    screen = q.edits[-1]
    assert "로봇 인증" in screen and "사용자 초대" in screen and "안 와요" in screen
    assert "가입 승인" in screen                              # 가짜 방은 승인제가 아님 → 안내
    [item] = [i for i in menu.HUB_ITEMS if i.code == "jv"]          # 그룹 허브 버튼
    assert item.label({"join_verify": True}) == "🤖 로봇 인증 ✅"
    stranger = FakeQuery(77, fake_user(77, "남"), "")
    await menu.on_callback(r.svc, r.bot, stranger, ["jvt", str(Room.CHAT)])
    assert not [c for c in r.bot.named("send_photo") if c[1] == 77]


@test
async def code_image_is_png():
    data = joinreq.code_image("4275")
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 200_000


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
