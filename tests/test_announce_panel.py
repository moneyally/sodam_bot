"""🗓️ 예약공지 버튼 패널 + 1:1 마법사 점검: python tests/test_announce_panel.py"""
import asyncio
import sys
from types import SimpleNamespace

import harness
from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import announce, handlers, menu

test, run_all = runner()
CHAT, OTHER = -1001111, -1002222
ADMIN = 1


# ── 하네스 시드: 켜진 것·꺼진 것 2개 (제목 이스케이프·미디어까지) ─────
async def seed_schedules(svc):
    db, cid = svc.db, harness.CHAT
    await db.add_schedule(cid, kind="daily", at_time="09:00", interval_min=None, title="<아침> 공지 & 규칙",
                          text="좋은 아침이에요!\n{규칙}", media_type=None, media_id=None, pin=True, created_by=harness.TG)
    sid = await db.add_schedule(cid, kind="interval", at_time=None, interval_min=180, title="",
                                text="이벤트 안내 사진입니다. 참여는 관리자에게 문의", media_type="photo",
                                media_id="PHOTO_ID", pin=False, created_by=harness.TG)
    await db.set_schedule_enabled(cid, sid, False)


if not any(getattr(f, "__name__", "") == "seed_schedules" for f in harness.SEEDERS):  # 두 번 import 돼도 1번만
    harness.SEEDERS.append(seed_schedules)


# ── 도우미 ────────────────────────────────────────────────
async def _async(v):
    return v


async def setup():
    """uid 1 = CHAT 관리자 (OTHER 는 관리 안 함). state.admins 에서 빼면 강등."""
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    for cid, title in ((CHAT, "내 방"), (OTHER, "남의 방")):
        await db.ensure_chat(cid, title)
    state = SimpleNamespace(admins={ADMIN}, forgets=0)
    svc.perms.is_admin = lambda bot, cid, uid: _async(cid == CHAT and uid in state.admins)
    svc.perms.is_tg_admin = svc.perms.is_admin

    def forget(cid):
        state.forgets += 1
    svc.perms.forget = forget
    return db, svc, FakeBot(), state


async def add(db, cid, title, **kw):
    base = dict(kind="daily", at_time="09:00", interval_min=None, text="본문", media_type=None, media_id=None,
                pin=False, created_by=ADMIN)
    base.update(kw)
    return await db.add_schedule(cid, title=title, **base)


async def press(svc, bot, data, uid=ADMIN):
    q = FakeQuery(uid, fake_user(uid, "방장"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    assert len(q.answers) == 1, q.answers
    return q


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row] if kb else []


def dm_ctx(svc, bot):
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data={"svc": svc})

    async def dm(text="", uid=ADMIN, **kw):
        msg = FakeMsg(uid, fake_user(uid, "방장"), text, **kw)
        await handlers.on_private(SimpleNamespace(message=msg), ctx)
        return msg
    return dm


def sent_to(bot, chat_id):
    return [c for c in bot.calls if c[0].startswith("send_") and c[1] == chat_id]


async def mod_log_count(db, action="schedule"):
    return len(await db._all("SELECT * FROM mod_log WHERE action=?", (action,)))


# ── 목록 · 항목 ───────────────────────────────────────────
@test
async def hub_has_announce_button_and_list_is_scoped():
    db, svc, bot, _ = await setup()
    mine = await add(db, CHAT, "내 공지")
    off = await add(db, CHAT, "꺼둔 공지", kind="interval", at_time=None, interval_min=120)
    await db.set_schedule_enabled(CHAT, off, False)
    theirs = await add(db, OTHER, "남의 비밀 공지")

    hub = await press(svc, bot, f"m:g:{CHAT}")
    assert any(b.callback_data == f"m:sc:{CHAT}" and "예약공지" in b.text for b in buttons(hub.kb))

    q = await press(svc, bot, f"m:sc:{CHAT}")
    text, labels = q.edits[-1], [b.text for b in buttons(q.kb)]
    assert "남의 비밀" not in text + "".join(labels) and "(2/20)" in text
    assert "🟢 매일 09:00 · 내 공지" in labels and "⏸ 2시간마다 · 꺼둔 공지" in labels
    assert not any(str(theirs) == b.callback_data.split(":")[-1] for b in buttons(q.kb) if "sci" in b.callback_data)

    q = await press(svc, bot, f"m:sci:{CHAT}:{mine}")
    assert "예약공지 #%d" % mine in q.edits[-1] and "🟢 켜짐" in q.edits[-1]
    assert {"👀 미리보기", "✏️ 수정", "⏸ 끄기", "🗑 삭제", "⬅️ 목록"} <= {b.text for b in buttons(q.kb)}

    # 남의 방 공지 번호를 내 방 버튼에 넣어 위조 → 못 봄·못 바꿈·못 지움·못 고침·미리보기 안 됨
    q = await press(svc, bot, f"m:sci:{CHAT}:{theirs}")
    assert "남의 비밀" not in q.edits[-1] and "없는 예약공지" in q.answers[0][0]
    await press(svc, bot, f"m:sct:{CHAT}:{theirs}:0")
    assert (await db.get_schedule(OTHER, theirs))["enabled"] == 1
    tokens_before = len(svc.menu_tokens)
    q = await press(svc, bot, f"m:scd:{CHAT}:{theirs}")
    assert len(svc.menu_tokens) == tokens_before and not any("m:k:" in b.callback_data for b in buttons(q.kb))
    await press(svc, bot, f"m:sce:{CHAT}:{theirs}")
    assert not svc.announcer.drafts
    await press(svc, bot, f"m:scp:{CHAT}:{theirs}")
    assert not sent_to(bot, ADMIN)
    for bad in ("abc", "-1", "", "99999999999999999999"):                       # 이상한 번호도 조용히 목록으로
        q = await press(svc, bot, f"m:sci:{CHAT}:{bad}")
        assert "없는 예약공지" in q.answers[0][0]

    # 관리하지 않는 방의 목록 자체도 거절
    q = await press(svc, bot, f"m:sc:{OTHER}")
    assert not q.edits and q.answers[0][1]
    # 일반 멤버는 버튼을 그대로 눌러도 거절
    q = await press(svc, bot, f"m:sci:{CHAT}:{mine}", uid=20)
    assert not q.edits and "관리자만" in q.answers[0][0]
    await db.close()


@test
async def empty_list_invites_to_create():
    db, svc, bot, _ = await setup()
    q = await press(svc, bot, f"m:sc:{CHAT}")
    assert "새로 만들기" in q.edits[-1] and "(0/20)" in q.edits[-1]
    assert [b.callback_data for b in buttons(q.kb)] == [f"m:scn:{CHAT}", f"m:g:{CHAT}"]
    await db.close()


@test
async def preview_is_new_dm_message_with_close():
    db, svc, bot, _ = await setup()
    await db.set_setting(CHAT, "rules", "서로 존중")
    sid = await add(db, CHAT, "규칙 안내", text="{규칙}")
    photo = await add(db, CHAT, "사진", text="설명", media_type="photo", media_id="PID")
    q = await press(svc, bot, f"m:scp:{CHAT}:{sid}")
    assert not q.edits and "미리보기" in q.answers[0][0]                   # 패널은 그대로
    call = sent_to(bot, ADMIN)[-1]
    assert call[0] == "send_message" and "서로 존중" in call[2]             # 1:1 로, 그 방의 규칙으로
    assert buttons(call[3]["reply_markup"])[0].callback_data == "an:x"
    assert not sent_to(bot, CHAT)                                          # 방에는 안 올라감
    await press(svc, bot, f"m:scp:{CHAT}:{photo}")
    assert sent_to(bot, ADMIN)[-1][:3] == ("send_photo", ADMIN, "PID")

    close = FakeQuery(ADMIN, fake_user(ADMIN))
    close.message.message_id = 4321
    await svc.announcer.on_callback(bot, close, ["x"])
    assert ("delete", ADMIN, 4321) in bot.calls and len(close.answers) == 1
    await db.close()


@test
async def toggle_uses_target_value():
    db, svc, bot, _ = await setup()
    sid = await add(db, CHAT, "공지")
    for _ in range(2):                                                      # 두 번 눌려도 꺼진 상태 그대로
        q = await press(svc, bot, f"m:sct:{CHAT}:{sid}:0")
        assert not (await db.get_schedule(CHAT, sid))["enabled"] and "껐어요" in q.answers[0][0]
    assert "⏸ 꺼짐" in q.edits[-1] and any(b.text == "▶️ 켜기" for b in buttons(q.kb))
    assert await mod_log_count(db) == 1                                    # 바뀐 경우에만 기록
    await press(svc, bot, f"m:sct:{CHAT}:{sid}:1")
    await press(svc, bot, f"m:sct:{CHAT}:{sid}:1")
    assert (await db.get_schedule(CHAT, sid))["enabled"] and await mod_log_count(db) == 2
    await press(svc, bot, f"m:sct:{CHAT}:{sid}:x")                          # 이상한 목표값은 무시
    assert (await db.get_schedule(CHAT, sid))["enabled"]
    await db.close()


@test
async def delete_needs_single_use_token():
    db, svc, bot, state = await setup()
    sid = await add(db, CHAT, "지울 공지")
    keep = await add(db, CHAT, "남길 공지")
    q = await press(svc, bot, f"m:scd:{CHAT}:{sid}")
    assert "삭제할까요" in q.edits[-1] and await db.get_schedule(CHAT, sid)  # 확인 전엔 안 지움
    ok = next(b.callback_data for b in buttons(q.kb) if b.text == "🗑 삭제")

    stranger = await press(svc, bot, ok, uid=20)                           # 남이 토큰을 눌러도 안 됨 (+토큰 소모)
    assert stranger.answers[0][1] and await db.get_schedule(CHAT, sid)

    q = await press(svc, bot, f"m:scd:{CHAT}:{sid}")
    ok = next(b.callback_data for b in buttons(q.kb) if b.text == "🗑 삭제")
    forgets = state.forgets
    q = await press(svc, bot, ok)
    assert "삭제했어요" in q.answers[0][0] and not await db.get_schedule(CHAT, sid)
    assert await db.get_schedule(CHAT, keep) and state.forgets > forgets  # 권한은 새로 확인
    labels = " ".join(b.text for b in buttons(q.kb))
    assert "지울 공지" not in labels and "남길 공지" in labels and "(1/20)" in q.edits[-1]
    again = await press(svc, bot, ok)                                       # 1회용
    assert "만료" in again.answers[0][0] and not again.edits

    q = await press(svc, bot, f"m:scd:{CHAT}:{keep}")                       # 확인 화면에서 강등되면 못 지움
    ok = next(b.callback_data for b in buttons(q.kb) if b.text == "🗑 삭제")
    state.admins.clear()
    q = await press(svc, bot, ok)
    assert "관리자만" in q.answers[0][0] and await db.get_schedule(CHAT, keep)
    await db.close()


# ── 1:1 마법사 ────────────────────────────────────────────
async def _run_wizard(svc, bot, dm, answers=("공지 제목", "공지 내용", "매일 21:30"), pin="pin1", save=True):
    for a in answers:
        msg = await dm(a)
        assert not msg.replies, msg.replies                                # 마법사가 받음 (AI 로 새지 않음)
    draft = svc.announcer.drafts[(ADMIN, ADMIN)]
    q = FakeQuery(ADMIN, fake_user(ADMIN))
    await svc.announcer.on_callback(bot, q, [draft.token, pin])
    if save:
        await svc.announcer.on_callback(bot, q, [draft.token, "save"])
    return draft, q


@test
async def dm_wizard_creates_schedule_in_group_not_dm():
    db, svc, bot, _ = await setup()
    dm = dm_ctx(svc, bot)
    q = await press(svc, bot, f"m:scn:{CHAT}")
    assert not q.edits and "안내" in q.answers[0][0]
    draft = svc.announcer.drafts[(ADMIN, ADMIN)]                            # 1:1 키 (menu.r_input 과 같은 약속)
    assert (draft.ui_chat_id, draft.chat_id, draft.user_id) == (ADMIN, CHAT, ADMIN)
    first = sent_to(bot, ADMIN)[-1][2]
    assert "1/4 제목" in first and "내 방" in first                          # 어느 방에 올릴지 보여줌

    await _run_wizard(svc, bot, dm)
    rows = await db.schedules(CHAT)
    assert len(rows) == 1 and (rows[0]["title"], rows[0]["at_time"], rows[0]["pin"]) == ("공지 제목", "21:30", 1)
    assert rows[0]["created_by"] == ADMIN
    assert not await db.schedules(ADMIN)                                    # 1:1 채팅에 만들어지지 않음
    assert not sent_to(bot, CHAT)                                          # 만드는 동안 방엔 아무것도 안 감
    assert any(c[0] == "delete_many" and c[1] == ADMIN for c in bot.calls)  # 1:1 에서 오간 메시지 정리
    done = sent_to(bot, ADMIN)[-1]
    assert "저장" in done[2] and buttons(done[3]["reply_markup"])[0].callback_data == f"m:sc:{CHAT}"
    assert not svc.announcer.drafts
    assert not await svc.announcer.handle_message(bot, FakeMsg(ADMIN, fake_user(ADMIN), "그냥 대화"))  # 끝나면 안 가로챔
    await db.close()


@test
async def dm_wizard_edits_existing_schedule():
    db, svc, bot, _ = await setup()
    dm = dm_ctx(svc, bot)
    sid = await add(db, CHAT, "원래 제목", text="원래 내용", kind="interval", at_time=None, interval_min=120, pin=True)
    await press(svc, bot, f"m:sce:{CHAT}:{sid}")
    assert "#%d 수정" % sid in sent_to(bot, ADMIN)[-1][2]
    await _run_wizard(svc, bot, dm, answers=("새 제목", "그대로", "그대로"), pin="pin0")
    r = await db.get_schedule(CHAT, sid)
    assert (r["title"], r["text"], r["interval_min"], r["pin"]) == ("새 제목", "원래 내용", 120, 0)
    assert len(await db.schedules(CHAT)) == 1
    await db.close()


@test
async def dm_wizard_edit_of_deleted_schedule_is_not_resurrected():
    db, svc, bot, _ = await setup()
    dm = dm_ctx(svc, bot)
    sid = await add(db, CHAT, "곧 삭제")
    await press(svc, bot, f"m:sce:{CHAT}:{sid}")
    await db.delete_schedule(CHAT, sid)                                     # 수정하는 사이 다른 관리자가 삭제
    await _run_wizard(svc, bot, dm, answers=("그대로", "그대로", "그대로"))
    assert not await db.schedules(CHAT) and "삭제됐어요" in sent_to(bot, ADMIN)[-1][2]
    await db.close()


@test
async def group_wizard_still_runs_in_group():
    db, svc, bot, _ = await setup()
    admin = fake_user(ADMIN, "방장")
    await svc.announcer.start(bot, FakeMsg(CHAT, admin, ".예약공지 만들기", message_id=77))
    draft = svc.announcer.drafts[(CHAT, ADMIN)]
    assert draft.ui_chat_id == draft.chat_id == CHAT and not draft.in_dm
    for i, a in enumerate(("그룹 공지", "내용", "반복 60")):
        assert await svc.announcer.handle_message(bot, FakeMsg(CHAT, admin, a, message_id=100 + i))
    assert not await svc.announcer.handle_message(bot, FakeMsg(ADMIN, admin, "1:1 메시지"))  # 1:1 은 별개
    q = FakeQuery(CHAT, admin)
    await svc.announcer.on_callback(bot, q, [draft.token, "pin0"])
    assert sent_to(bot, CHAT)[-1][0] == "send_message" and "그룹 공지" in sent_to(bot, CHAT)[-1][2]  # 미리보기도 방에
    await svc.announcer.on_callback(bot, q, [draft.token, "save"])
    assert [r["title"] for r in await db.schedules(CHAT)] == ["그룹 공지"]
    done = sent_to(bot, CHAT)[-1]
    assert "저장" in done[2] and done[3]["reply_markup"] is None           # 방에는 메뉴 버튼 안 붙임
    assert not sent_to(bot, ADMIN)
    cleaned = next(c for c in bot.calls if c[0] == "delete_many")
    assert cleaned[1] == CHAT and 77 in cleaned[2]
    await db.close()


@test
async def demoted_admin_cannot_save():
    db, svc, bot, state = await setup()
    dm = dm_ctx(svc, bot)
    await press(svc, bot, f"m:scn:{CHAT}")
    draft, _ = await _run_wizard(svc, bot, dm, save=False)
    state.admins.clear()                                                    # 미리보기 보는 사이 강등
    forgets = state.forgets
    q = FakeQuery(ADMIN, fake_user(ADMIN))
    await svc.announcer.on_callback(bot, q, [draft.token, "save"])
    assert not await db.schedules(CHAT) and state.forgets > forgets         # 캐시 말고 새로 확인
    assert "관리자만" in sent_to(bot, ADMIN)[-1][2] and not svc.announcer.drafts

    # 방 안 마법사도 마찬가지
    state.admins.add(ADMIN)
    admin = fake_user(ADMIN, "방장")
    await svc.announcer.start(bot, FakeMsg(CHAT, admin, ".예약공지 만들기"))
    for a in ("t", "b", "매일 10:00"):
        await svc.announcer.handle_message(bot, FakeMsg(CHAT, admin, a))
    draft = svc.announcer.drafts[(CHAT, ADMIN)]
    await svc.announcer.on_callback(bot, q, [draft.token, "pin0"])
    state.admins.clear()
    await svc.announcer.on_callback(bot, q, [draft.token, "save"])
    assert not await db.schedules(CHAT)
    await db.close()


@test
async def dm_wizard_and_menu_input_cancel_each_other():
    db, svc, bot, _ = await setup()
    dm = dm_ctx(svc, bot)
    await press(svc, bot, f"m:scn:{CHAT}")
    assert (ADMIN, ADMIN) in svc.announcer.drafts
    await press(svc, bot, f"m:in:{CHAT}:bw")                                # 메뉴 글자 입력 → 마법사 취소
    assert (ADMIN, ADMIN) not in svc.announcer.drafts and ADMIN in svc.inputs
    msg = await dm("스팸")
    assert "추가" in msg.replies[0] and "스팸" in await db.banned_words(CHAT)

    await press(svc, bot, f"m:in:{CHAT}:bw")
    await press(svc, bot, f"m:scn:{CHAT}")                                  # 마법사 시작 → 글자 입력 취소
    assert ADMIN not in svc.inputs and (ADMIN, ADMIN) in svc.announcer.drafts
    msg = await dm("광고")                                                  # 이제 이 글은 공지 제목
    assert not msg.replies and "광고" not in await db.banned_words(CHAT)
    assert svc.announcer.drafts[(ADMIN, ADMIN)].title == "광고"
    msg = await dm("취소")
    assert not svc.announcer.drafts and "취소" in sent_to(bot, ADMIN)[-1][2]
    await db.close()


@test
async def new_respects_max_per_chat():
    db, svc, bot, _ = await setup()
    for i in range(announce.MAX_PER_CHAT):
        await add(db, CHAT, f"공지{i}")
    q = await press(svc, bot, f"m:scn:{CHAT}")
    assert q.answers[0][1] and "20개까지" in q.answers[0][0] and not svc.announcer.drafts
    sid = (await db.schedules(CHAT))[0]["id"]
    await press(svc, bot, f"m:sce:{CHAT}:{sid}")                            # 꽉 차도 수정은 됨
    assert (ADMIN, ADMIN) in svc.announcer.drafts
    await db.close()


@test
async def harness_seeds_schedules():
    db, svc, bot = await harness.make_world()
    rows = await db.schedules(harness.CHAT)
    assert len(rows) == 2 and [bool(r["enabled"]) for r in rows] == [True, False]
    await db.close()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
