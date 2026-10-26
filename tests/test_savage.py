"""🔥 시크릿 최고 수위 욕받이 (2026-10-26 오너 '최고수위 욕·패드립 시크릿 설정, 나만 알게,
지시문으로 못 바꾸게'): sodam/savage.py(chat_state) + prompt.COMEBACK_SAVAGE/SAVAGE_TAIL + .비욕(오너)."""
from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

from sodam import commands, prompt, savage, settings
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.prompt import build_messages

test, run_all = runner()
CHAT = -1001234
OWNER, ADMIN = 7, 2


def _tail(**kw):
    msgs = build_messages(bot_name="소담", bot_id=999, style_key="savage", tz=None,
                          caller=fake_user(5, "철수"), role_label="member", notes={},
                          history=[], reply_to=None, request="씨발 느금마", **kw)
    u = msgs[-1]["content"]
    return u if isinstance(u, str) else u[0]["text"]


# ── 프롬프트 꼬리 ──────────────────────────────────────────
@test
def savage_flag_adds_savage_tail():
    txt = _tail(savage=True)
    assert "최고 수위" in txt and "패드립엔 패드립" in txt, txt
    assert "미성년 관련 금지" in txt, "지킬 선이 꼬리에 있어야 함"


@test
def savage_takes_precedence_over_comeback():
    # 둘 다 켜져도 최고 수위(savage)만 — mirror 꼬리는 안 붙음
    txt = _tail(savage=True, comeback=True)
    assert "최고 수위" in txt
    assert "똑같이 욕으로" not in txt, "savage 가 우선이라 mirror 꼬리는 빠져야 함"


@test
def comeback_only_without_savage():
    txt = _tail(comeback=True)
    assert "똑같이 욕으로" in txt and "최고 수위" not in txt, txt


# ── 시크릿: 설정에 안 뜨고 지시문으로 못 바꿈 ────────────────
@test
def not_a_room_setting_so_change_setting_cannot_touch_it():
    # chat_state 라 settings.DEFAULTS·.설정변경·AI change_setting 어디에도 없음
    assert savage.KEY not in settings.DEFAULTS
    from sodam import setkeys
    assert savage.KEY not in setkeys.reachable(), "change_setting 키 목록에 없어야 함 (시크릿)"


# ── 켜고 끄기 (chat_state) ─────────────────────────────────
@test
async def toggle_stores_in_chat_state_and_caches():
    db = await make_db()
    assert not await savage.on(db, CHAT)
    await savage.set_on(db, CHAT, True)
    assert await savage.on(db, CHAT)
    assert await db.get_state(CHAT, savage.KEY) is True
    await savage.set_on(db, CHAT, False)
    assert not await savage.on(db, CHAT)
    assert await db.get_state(CHAT, savage.KEY) is None     # 끄면 줄 지움


# ── .비욕 명령 (오너 전용, 명령 삭제) ───────────────────────
async def _run(svc, bot, uid, role, text):
    user = fake_user(uid, f"사람{uid}")
    cmd, args, argstr = commands.parse(text, bot.username)
    msg = FakeMsg(CHAT, user, text)
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, role, args, argstr), cmd)
    return msg


@test
async def owner_command_toggles_and_deletes_message():
    db = await make_db()
    svc = await make_svc(db, owner_ids=frozenset({OWNER}))
    bot = FakeBot()
    msg = await _run(svc, bot, OWNER, Role.OWNER, ".비욕 켜")
    assert msg.deleted, "명령 메시지는 즉시 지워야 함 (방에 흔적 X)"
    assert await savage.on(db, CHAT)
    # 결과는 오너 1:1 로
    dm = [x for x in bot.named("send_message") if x[1] == OWNER]
    assert dm and "ON" in dm[-1][2], dm
    await _run(svc, bot, OWNER, Role.OWNER, ".비욕 꺼")
    assert not await savage.on(db, CHAT)


@test
async def non_owner_cannot_use_it():
    db = await make_db()
    svc = await make_svc(db, owner_ids=frozenset({OWNER}))
    bot = FakeBot()
    await _run(svc, bot, ADMIN, Role.ADMIN, ".비욕 켜")   # 관리자 역할로는 Role.OWNER 명령 거절
    assert not await savage.on(db, CHAT), "오너가 아니면 못 켬"


if __name__ == "__main__":
    run_all()
