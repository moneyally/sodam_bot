"""📣 집합(소집) — 선택형 소집: python tests/run_all.py summon

관리자가 부를 사람만 골라 한 번에 @태그. summon.py 코어 + panels/summon.py 화면·명령·AI 도구.
오너 결정 2026-10-21: 자동 규칙 없이 직접 선택."""
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner

import sodam.__main__  # noqa: F401  (패널·설정 등록)
from sodam import commands, summon
from sodam.commands import CmdCtx
from sodam.menu import PanelCtx
from sodam.permissions import Role
from sodam.panels import summon as P

summon.GAP = 0   # 테스트에서 배치 사이 대기 없음

test, run_all = runner()

CHAT = -1001000000777
OWNER, ADMIN, MEMBER = 900, 901, 902
A, B, C = 910, 911, 912


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    svc.perms.owner_ids = {OWNER}
    bot = FakeBot()
    bot.member_status = {(CHAT, ADMIN): "administrator"}
    people = [(OWNER, "오너", None), (ADMIN, "문주", "munju"), (MEMBER, "쓰잔", None),
              (A, "철수", "cheol"), (B, "영희", "young"), (C, "민수", "minsu")]
    for uid, name, un in people:
        await db.upsert_user(fake_user(uid, name, username=un))
        await add_member(db, CHAT, fake_user(uid, name, username=un))
    return db, svc, bot


def ctx(svc, bot, uid, cid, args):
    return PanelCtx(svc, bot, uid, cid, list(args))


# ── 코어 ─────────────────────────────────────────────────
@test
async def toggle_and_pick_and_clear():
    db, svc, bot = await setup()
    assert await summon.picked(db, CHAT, ADMIN) == []
    assert await summon.toggle(db, CHAT, ADMIN, A) is True
    assert await summon.toggle(db, CHAT, ADMIN, B) is True
    assert await summon.picked(db, CHAT, ADMIN) == [A, B]
    assert await summon.toggle(db, CHAT, ADMIN, A) is False        # 다시 누르면 빠짐
    assert await summon.picked(db, CHAT, ADMIN) == [B]
    # 관리자마다 따로
    await summon.toggle(db, CHAT, MEMBER, C)
    assert await summon.picked(db, CHAT, ADMIN) == [B]
    assert await summon.picked(db, CHAT, MEMBER) == [C]
    await summon.clear(db, CHAT, ADMIN)
    assert await summon.picked(db, CHAT, ADMIN) == []


@test
async def add_ids_dedupes_and_caps():
    db, svc, bot = await setup()
    assert await summon.add_ids(db, CHAT, ADMIN, [A, B, A]) == 2    # 중복 1번만
    assert await summon.add_ids(db, CHAT, ADMIN, [B, C]) == 1       # B 는 이미 있음
    assert await summon.picked(db, CHAT, ADMIN) == [A, B, C]


@test
async def save_load_delete_list():
    db, svc, bot = await setup()
    await summon.add_ids(db, CHAT, ADMIN, [A, B])
    lid = await summon.save_list(db, CHAT, "단골", [A, B], ADMIN)
    assert lid is not None
    assert await summon.list_ids(db, CHAT, lid) == [A, B]
    rows = await summon.lists(db, CHAT)
    assert rows and rows[0]["name"] == "단골" and rows[0]["n"] == 2
    # 같은 이름 저장 = 덮어씀
    await summon.save_list(db, CHAT, "단골", [C], ADMIN)
    assert len(await summon.lists(db, CHAT)) == 1
    assert await summon.list_ids(db, CHAT, lid) == [C]
    assert await summon.delete_list(db, CHAT, lid) is True
    assert await summon.lists(db, CHAT) == []


@test
async def fire_posts_mentions_in_batches():
    db, svc, bot = await setup()
    summon.BATCH = 2
    try:
        n = await summon.fire(svc, bot, CHAT, [A, B, C], "📣 집합!")
    finally:
        summon.BATCH = 20
    assert n == 3
    sent = bot.named("send_message")
    assert len(sent) == 2, sent                                    # 2명 + 1명 = 2 메시지
    assert "📣 집합!" in sent[0][2]                                 # 머리글은 첫 메시지만
    assert "📣 집합!" not in sent[1][2]
    # 멘션은 tg://user 링크 (알림 감)
    assert f"tg://user?id={A}" in sent[0][2] and f"tg://user?id={C}" in sent[1][2]


# ── 화면 ─────────────────────────────────────────────────
@test
async def panel_toggle_then_fire():
    db, svc, bot = await setup()
    # 최근활동 화면에서 토글 (복귀=r0)
    c = ctx(svc, bot, ADMIN, CHAT, [str(A), "r0"])
    s = await P.r_toggle(c)
    assert await summon.picked(db, CHAT, ADMIN) == [A], s.toast
    c = ctx(svc, bot, ADMIN, CHAT, [str(B), "m"])
    await P.r_toggle(c)
    # 메인 화면에 선택 2명 표시
    s = await P.s_smn(ctx(svc, bot, ADMIN, CHAT, []))
    assert "선택됨 2명" in s.text
    # 집합 보내기
    bot.calls.clear()
    s = await P.r_fire(ctx(svc, bot, ADMIN, CHAT, []))
    assert bot.named("send_message") and "집합" in bot.named("send_message")[0][2]
    assert "2명" in (s.toast or "")


@test
async def buttons_get_colors():
    # btnstyle 자동 색: 집합 보내기=🟢 · 비우기=🔴 · 검색=🔵 (style_for 로 직접 판정 — install 상태와 무관)
    from sodam import btnstyle
    db, svc, bot = await setup()
    await summon.add_ids(db, CHAT, ADMIN, [A])
    s = await P.s_smn(ctx(svc, bot, ADMIN, CHAT, []))
    st = {b.text: btnstyle.style_for(b) for row in s.kb.inline_keyboard for b in row}
    assert next(v for k, v in st.items() if "집합 보내기" in k) == "success", st
    assert next(v for k, v in st.items() if "비우기" in k) == "danger", st
    assert next(v for k, v in st.items() if "검색" in k) == "primary", st


@test
async def search_screen_lists_matches():
    db, svc, bot = await setup()
    await db.set_state(CHAT, P._qkey(ADMIN), "철")
    s = await P.s_search(ctx(svc, bot, ADMIN, CHAT, []))
    assert "철수" in s.text or any("철수" in b.text for row in s.kb.inline_keyboard for b in row)


# ── 명령 ─────────────────────────────────────────────────
async def run_cmd(svc, bot, uid, role, text, reply_to=None):
    user = fake_user(uid, "문주")
    parsed = commands.parse(text, bot.username)
    msg = FakeMsg(CHAT, user, text, reply_to=reply_to)
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, role, parsed[1], parsed[2]), parsed[0])
    return msg, bot


@test
async def command_fires_by_at_id_and_list():
    db, svc, bot = await setup()
    bot.calls.clear()
    await run_cmd(svc, bot, ADMIN, Role.ADMIN, ".집합 @cheol @young")
    assert bot.named("send_message"), "@아이디로 집합이 안 나감"
    assert f"tg://user?id={A}" in bot.named("send_message")[0][2]
    # 저장 명단 이름으로
    await summon.save_list(db, CHAT, "브이아이피", [C], ADMIN)
    bot.calls.clear()
    await run_cmd(svc, bot, ADMIN, Role.ADMIN, ".집합 브이아이피")
    assert bot.named("send_message") and f"tg://user?id={C}" in bot.named("send_message")[0][2]


@test
async def command_no_target_guides():
    db, svc, bot = await setup()
    msg, _ = await run_cmd(svc, bot, ADMIN, Role.ADMIN, ".집합")
    assert any("부를 사람" in r for r in msg.replies), msg.replies


@test
async def command_reply_summons_that_person():
    db, svc, bot = await setup()
    bot.calls.clear()
    reply = FakeMsg(CHAT, fake_user(A, "철수"), "글")
    await run_cmd(svc, bot, ADMIN, Role.ADMIN, ".집합", reply_to=reply)
    assert bot.named("send_message") and f"tg://user?id={A}" in bot.named("send_message")[0][2]


# ── AI 도구 ──────────────────────────────────────────────
@test
async def ai_tool_summons_and_blocks_member():
    db, svc, bot = await setup()
    bot.calls.clear()
    c = SimpleNamespace(svc=svc, bot=bot, chat_id=CHAT, role=Role.ADMIN)
    out = await P.t_summon(c, {"names": ["철수", "영희"]})
    assert bot.named("send_message") and ("불렀" in out or "명" in out), out
    # 멤버는 거절
    bot.calls.clear()
    c = SimpleNamespace(svc=svc, bot=bot, chat_id=CHAT, role=Role.MEMBER)
    out = await P.t_summon(c, {"names": ["철수"]})
    assert "관리자" in out and not bot.named("send_message"), out
    # 1:1 은 거절
    c = SimpleNamespace(svc=svc, bot=bot, chat_id=ADMIN, role=Role.ADMIN)
    out = await P.t_summon(c, {"names": ["철수"]})
    assert "그룹방" in out, out


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
