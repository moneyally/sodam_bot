"""제재 확인 버튼 (여러 명·봇 권한·오너 1:1) · 인사 중복·말투: python tests/run_all.py sanction_multi

실제 사례: 싸운 두 명 3분 뮤트 → 버튼이 한 명만 나오고, 봇이 그 방 관리자가 아니라 눌러도 실패.
"""
from types import SimpleNamespace

from fake_llm import Room, reply, tool_call
from fakes import FakeQuery, add_member, fake_user, runner

from sodam import handlers
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.tools import ToolCtx, available, execute

test, run_all = runner()
BOSS, OWNER = fake_user(1, "방장", "boss"), fake_user(7, "오너", "owner")
A, B = fake_user(20, "캎이바라요", "bqw"), fake_user(21, "조이킨", "jangha")
MUTE_PERM = lambda r, uid: [c for c in r.bot.named("restrict") if c[2] == uid]  # noqa: E731


async def room(**kw):
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False, "ai_sanction_card": "all", **kw})
    for u in (BOSS, A, B):
        await r.join(u)
    return r


async def ask(r, caller, script, chat_id=None, role=Role.ADMIN):
    r.llm.script = [*script, reply("알겠어요.")]
    ctx = ToolCtx(r.svc, r.bot, chat_id or Room.CHAT, caller, role, await r.db.get_settings(Room.CHAT))
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="뮤트", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


async def press(r, user, key, yn):
    q = FakeQuery(user.id, user, f"act:{key}:{yn}")
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    return q


@test
async def two_people_one_card_one_press():
    r = await room()
    res = await ask(r, BOSS, [tool_call("mute_member", {"names": ["캎이바라요", "조이킨"], "minutes": 3, "reason": "욕설"})])
    [action] = r.svc.pending.values()
    assert [t[0] for t in action.targets] == [A.id, B.id] and "2명" in res[0], res
    card = [c for c in r.bot.named("send_message") if "할까요" in c[2]][-1]
    assert "캎이바라요" in card[2] and "조이킨" in card[2] and "2명" in str(card[3]["reply_markup"].inline_keyboard[0])
    q = await press(r, BOSS, next(iter(r.svc.pending)), "y")
    assert MUTE_PERM(r, A.id) and MUTE_PERM(r, B.id), "버튼 한 번에 두 명 다"
    assert "캎이바라요" in q.edits[-1] and "조이킨" in q.edits[-1] and "3분" in q.edits[-1]


@test
async def bot_not_admin_no_button_and_honest_reason():
    r = await room()
    r.bot.can_moderate = False
    res = await ask(r, BOSS, [tool_call("mute_member", {"names": ["캎이바라요"], "minutes": 3, "reason": "욕설"})])
    assert not r.svc.pending and "봇이 '사용자 차단' 권한이 있는 관리자가 아니라서" in res[0], res
    assert not [c for c in r.bot.named("send_message") if "할까요" in c[2]]


@test
async def unknown_name_sends_nothing():
    r = await room()
    res = await ask(r, BOSS, [tool_call("mute_member", {"names": ["캎이바라요", "없는사람"], "minutes": 3})])
    assert not r.svc.pending and "없는사람" in res[0] and "확인 버튼을 보내지 않았음" in res[0], res


@test
async def old_single_name_arg_still_works_and_limit_five():
    r = await room()
    await ask(r, BOSS, [tool_call("warn_member", {"name": "조이킨", "reason": "a"})])
    assert [t[0] for t in next(iter(r.svc.pending.values())).targets] == [B.id]
    r2 = await room()
    res = await ask(r2, BOSS, [tool_call("mute_member", {"names": [f"p{i}" for i in range(6)], "minutes": 3})])
    assert "최대 5명" in res[0] and not r2.svc.pending


@test
async def tools_scoped_by_chat_kind_and_role():
    s = {}
    room_admin = {t.name for t in available(Role.ADMIN, s, in_dm=False)}
    dm_admin = {t.name for t in available(Role.ADMIN, s, in_dm=True)}
    dm_owner = {t.name for t in available(Role.OWNER, s, in_dm=True)}
    assert "mute_member" in room_admin and "owner_sanction" not in room_admin
    assert "mute_member" not in dm_admin and "owner_sanction" not in dm_admin, "1:1 에선 방 도구·오너 도구 없음"
    assert "owner_sanction" in dm_owner and "mute_member" not in dm_owner
    r = await room()
    ctx = ToolCtx(r.svc, r.bot, OWNER.id, OWNER, Role.OWNER, {})
    assert "사용할 수 없음" in await execute("mute_member", '{"names": ["조이킨"], "minutes": 3}', ctx)


@test
async def owner_dm_sanction_card_in_dm_then_room_notice():
    r = await room()
    r.svc.perms.owner_ids = {OWNER.id}
    await r.db._write("UPDATE chats SET title='𝐅𝐈𝐑𝐒𝐓' WHERE chat_id=?", (Room.CHAT,))
    res = await ask(r, OWNER, [tool_call("owner_sanction", {"room": "First그룹방", "action": "mute", "names": ["조이킨"],
                                                            "minutes": 30, "reason": "도배"})],
                    chat_id=OWNER.id, role=Role.OWNER)
    [card] = [c for c in r.bot.named("send_message") if "할까요" in c[2]]
    assert card[1] == OWNER.id and "𝐅𝐈𝐑𝐒𝐓" in card[2] and "확인 버튼을 보냈음" in res[0], (card, res)
    key = next(iter(r.svc.pending))
    q = await press(r, BOSS, key, "p")                      # 방 관리자라도 오너 1:1 카드는 못 누름
    assert q.answers[-1][1] is True and not MUTE_PERM(r, B.id)
    await press(r, OWNER, key, "p")
    assert MUTE_PERM(r, B.id), "오너가 누르면 그 방에서 뮤트"
    notice = [c for c in r.bot.named("send_message") if c[1] == Room.CHAT and "관리자 조치" in c[2]]
    assert notice and "조이킨" in notice[0][2] and "도배" in notice[0][2], notice


@test
async def owner_sanction_unknown_room_lists_rooms():
    r = await room()
    r.svc.perms.owner_ids = {OWNER.id}
    res = await ask(r, OWNER, [tool_call("owner_sanction", {"room": "없는방", "action": "mute", "names": ["조이킨"],
                                                            "reason": "x"})], chat_id=OWNER.id, role=Role.OWNER)
    assert "못 찾음" in res[0] and not r.svc.pending, res   # 여러 방 후보는 test_review2


@test
async def ai_greet_skips_person_just_auto_greeted_and_uses_room_style():
    r = await room(greet_enabled=True, style="secretary")
    newbie = fake_user(30, "김희중")
    await handlers.handle_new_member(r.ctx, Room.CHAT, "방", newbie)            # 자동 입장 인사 대기
    res = await ask(r, BOSS, [tool_call("greet_members", {"names": ["김희중"]})])
    assert "자동 입장 인사" in res[0], res
    await add_member(r.db, Room.CHAT, fake_user(31, "이수진"))
    res = await ask(r, BOSS, [tool_call("greet_members", {"names": ["이수진"]})])
    assert "방 기본 말투(비서)" in res[0] and "자동 입장 인사" not in res[0], res


@test
async def room_style_change_mentions_personal_styles_and_reset():
    r = await room()
    await r.db.set_member_style(Room.CHAT, BOSS.id, "girlfriend")
    res = await ask(r, BOSS, [tool_call("change_setting", {"key": "style", "value": "비서"})])
    assert "개인 말투를 따로 정한 1명" in res[0], res
    res = await ask(r, BOSS, [tool_call("reset_member_styles", {})])
    assert "1명" in res[0] and (await r.db.get_member(Room.CHAT, BOSS.id))["style"] is None



# ── 오너 결정 2026-10-05: 확인 카드는 위험한 것만 (ai_sanction_card 기본 risky) ──────────────
@test
async def risky_mode_runs_warn_and_mute_right_away_but_ban_still_asks():
    r = await room(ai_sanction_card="risky")
    res = await ask(r, BOSS, [tool_call("mute_member", {"names": ["캎이바라요", "조이킨"], "minutes": 3, "reason": "욕설"})])
    assert not r.svc.pending and MUTE_PERM(r, A.id) and MUTE_PERM(r, B.id), "카드 없이 두 명 다 바로"
    assert "바로 함" in res[0] and "3분" in res[0] and "<a " not in res[0] and "href" not in res[0], res   # 태그 뗀 결과 줄
    lines = [x["text"] for x in await r.db._all("SELECT text FROM ai_card_log WHERE chat_id=?", (Room.CHAT,))]
    assert lines and lines[-1].startswith("✅ 뮤트 3분 실행됨 — 2명"), lines                  # 다음 답의 근거로 남음
    audit = [x["detail"] for x in await r.db._all("SELECT detail FROM mod_log WHERE action='ask_mute'")]
    assert any("바로 실행(3분)" in d for d in audit), audit
    r2 = await room(ai_sanction_card="risky")
    await ask(r2, BOSS, [tool_call("warn_member", {"name": "조이킨", "reason": "도배"})])
    assert await r2.db.warning_count(Room.CHAT, B.id) == 1 and not r2.svc.pending
    r3 = await room(ai_sanction_card="risky")
    res = await ask(r3, BOSS, [tool_call("ban_member", {"names": ["조이킨"], "reason": "사기"})])
    assert "확인 버튼을 보냈음" in res[0] and r3.svc.pending, "밴은 여전히 카드"


@test
async def risky_mode_still_asks_after_reading_member_text_or_for_admin_targets():
    r = await room(ai_sanction_card="risky")
    r.llm.script = [tool_call("read_chat", {"hours": 1}, "c1"),
                    tool_call("mute_member", {"names": ["조이킨"], "minutes": 3, "reason": "x"}, "c2"), reply("ok")]
    ctx = ToolCtx(r.svc, r.bot, Room.CHAT, BOSS, Role.ADMIN, await r.db.get_settings(Room.CHAT))
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="뮤트", extras={})
    assert r.svc.pending and not MUTE_PERM(r, B.id), "멤버 글을 읽은 답변 = 숨은 지시 가능 → 카드"
    r2 = await room(ai_sanction_card="risky")
    r2.svc.perms.admins.add(B.id)
    res = await ask(r2, BOSS, [tool_call("mute_member", {"names": ["조이킨"], "minutes": 3})])
    assert "제재할 수 없어요" in res[0] and not r2.bot.named("restrict"), res              # 관리자는 바로 실행도 안 됨
    r3 = await room(ai_sanction_card="all")
    await ask(r3, BOSS, [tool_call("mute_member", {"names": ["조이킨"], "minutes": 3})])
    assert r3.svc.pending and not MUTE_PERM(r3, B.id), "방이 '전부' 를 고르면 예전처럼 카드"

if __name__ == "__main__":
    run_all()
