"""🎞 모션 배너(텔레모션) 재전송 — python tests/run_all.py motionbanner

관리자가 커스텀 이모지 배너를 1:1 에 전달하면 전체폭 스페이서로 고쳐 고른 방에 재전송.
(2026-11-04 오너: 꼬꼬방 사신팀 '텔레모션' 배너가 계단으로 깨짐 → 폭 스페이서 붙여 재전송)
"""
from types import SimpleNamespace

from fakes import FakeBot, FakeQuery, fake_user, make_db, make_svc, runner

from sodam.panels import motionbanner as M

test, run_all = runner()
CHAT, CHAT2 = -1001000000111, -1001000000222
OWNER, ADMIN, OUTSIDER = 900, 901, 902


def ent(offset, cid):
    return SimpleNamespace(type="custom_emoji", offset=offset, length=2, custom_emoji_id=str(cid))


def banner_msg(user, *, forwarded=True, rows=(3, 4)):
    """🟨(UTF-16 2칸)로 rows 칸짜리 격자 배너. text_html·forward_origin 세팅."""
    text_lines, ents, html_lines, off = [], [], [], 0
    cid = 1
    for n in rows:
        line_t, line_h = [], []
        for _ in range(n):
            ents.append(ent(off, cid))
            line_t.append("🟨")
            line_h.append(f'<tg-emoji emoji-id="{cid}">🟨</tg-emoji>')
            off += 2
            cid += 1
        text_lines.append("".join(line_t))
        html_lines.append("".join(line_h))
        off += 1   # '\n'
    text = "\n".join(text_lines)
    msg = SimpleNamespace(text=text, caption=None, entities=tuple(ents), caption_entities=(),
                          from_user=user, chat_id=user.id, message_id=5,
                          text_html="\n".join(html_lines))
    msg.forward_origin = SimpleNamespace(type="channel") if forwarded else None
    return msg


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN})
    svc.perms.owner_ids = {OWNER}
    # 방을 DB 에 만들어 두면 진짜 menu.admin_groups 가 이 방들을 돌려준다 (FakePerms.candidate_chats=all_chat_ids,
    # is_admin=admins 안). 전역 함수 몽키패치(복구 안 하면 다른 테스트 오염)를 쓰지 않는다.
    await db.ensure_chat(CHAT, "꼬꼬방")
    await db.ensure_chat(CHAT2, "둘째방")
    return db, svc, FakeBot()


# ── 핵심: 전체폭 스페이서 ────────────────────────────────
@test
async def fixed_html_adds_full_width_spacer():
    msg = banner_msg(fake_user(ADMIN, "문주"))
    out = M.fixed_html(msg)
    assert out.endswith("\n" + M.SPACER), "전체폭 스페이서가 끝에 안 붙음"
    assert "<tg-emoji" in out and out.count("tg-emoji") == 2 * 7, "조각(커스텀 이모지)이 그대로여야"
    assert M.grid_cols(msg) == 4, M.grid_cols(msg)
    assert M.tile_count(msg) == 7


# ── 1:1 전달 받기 ────────────────────────────────────────
@test
async def forwarded_banner_offers_room_picker():
    db, svc, bot = await setup()
    admin = fake_user(ADMIN, "문주")
    ok = await M.maybe_offer(svc, bot, banner_msg(admin), admin)
    assert ok is True
    sent = bot.named("send_message")
    # 1) 미리보기 = 고친 배너를 실제로 1:1 에 올림 (스페이서 포함)
    assert any(M.SPACER in c[2] and "<tg-emoji" in c[2] for c in sent), "미리보기 배너를 안 올림"
    # 2) 방 고르기 버튼
    picker = next(c for c in sent if "어느 방에 올릴까" in c[2])
    kb = picker[3]["reply_markup"].inline_keyboard
    labels = [b.text for row in kb for b in row]
    assert any("꼬꼬방" in t for t in labels) and any("둘째방" in t for t in labels), labels
    assert any("고정" in t for t in labels), labels
    # 초안 저장됨
    assert await db._one("SELECT * FROM motion_drafts WHERE user_id=?", (ADMIN,))


@test
async def ignores_non_forwarded_and_too_few_tiles():
    db, svc, bot = await setup()
    admin = fake_user(ADMIN, "문주")
    assert await M.maybe_offer(svc, bot, banner_msg(admin, forwarded=False), admin) is False  # 전달 아님
    assert await M.maybe_offer(svc, bot, banner_msg(admin, rows=(2,)), admin) is False          # 2칸 = 평소 이모지
    assert not bot.named("send_message")


@test
async def wide_banner_warns():
    db, svc, bot = await setup()
    admin = fake_user(ADMIN, "문주")
    await M.maybe_offer(svc, bot, banner_msg(admin, rows=(12,)), admin)   # 가로 12칸 > SAFE_COLS
    assert any("줄바꿈" in c[2] for c in bot.named("send_message")), "넓은 배너 경고가 없음"


# ── 방에 올리기 ──────────────────────────────────────────
async def _offer_did(svc, bot, admin):
    await M.maybe_offer(svc, bot, banner_msg(admin), admin)
    row = await svc.db._one("SELECT id FROM motion_drafts WHERE user_id=?", (admin.id,))
    return row["id"]


@test
async def post_to_room_sends_fixed_html_and_pins():
    db, svc, bot = await setup()
    admin = fake_user(ADMIN, "문주")
    did = await _offer_did(svc, bot, admin)
    await svc.db._write("UPDATE motion_drafts SET pin=1 WHERE id=?", (did,))
    bot.calls.clear()
    q = FakeQuery(ADMIN, admin, data=f"mbn:go:{did}:{CHAT}")
    await M.on_cb(svc, bot, q, ["go", str(did), str(CHAT)])
    posted = bot.named("send_message")
    assert posted and posted[0][1] == CHAT, "방에 안 올림"
    assert M.SPACER in posted[0][2] and posted[0][3].get("parse_mode") == "HTML"
    assert bot.named("pin"), "고정 켰는데 고정 안 함"
    assert any("올렸" in (a[0] or "") for a in q.answers), q.answers
    # 올린 뒤 초안 지움
    assert not await db._one("SELECT * FROM motion_drafts WHERE id=?", (did,))


@test
async def only_requester_and_room_admin_can_post():
    db, svc, bot = await setup()
    admin = fake_user(ADMIN, "문주")
    did = await _offer_did(svc, bot, admin)
    bot.calls.clear()
    # 남이 누르면 거절 (초안 주인 아님)
    q = FakeQuery(OUTSIDER, fake_user(OUTSIDER, "외부"), data=f"mbn:go:{did}:{CHAT}")
    await M.on_cb(svc, bot, q, ["go", str(did), str(CHAT)])
    assert not bot.named("send_message") and q.answers[-1][1] is True
    # 요청자지만 그 방 관리자가 아니면 거절
    db2, svc2, bot2 = await setup()
    plain = fake_user(777, "관리자아님")       # admins 에 없음
    # 초안은 직접 심어 준다 (maybe_offer 는 admin_groups 가짜라 통과하지만 is_admin 은 진짜 FakePerms)
    did2 = await M._save(svc2.db, plain.id, "x" + M.SPACER, 3)
    q2 = FakeQuery(plain.id, plain, data=f"mbn:go:{did2}:{CHAT}")
    await M.on_cb(svc2, bot2, q2, ["go", str(did2), str(CHAT)])
    assert not bot2.named("send_message") and q2.answers[-1][1] is True


@test
async def pin_toggle_flips():
    db, svc, bot = await setup()
    admin = fake_user(ADMIN, "문주")
    did = await _offer_did(svc, bot, admin)
    q = FakeQuery(ADMIN, admin, data=f"mbn:pin:{did}")
    await M.on_cb(svc, bot, q, ["pin", str(did)])
    assert (await svc.db._one("SELECT pin FROM motion_drafts WHERE id=?", (did,)))["pin"] == 1
    await M.on_cb(svc, bot, q, ["pin", str(did)])
    assert (await svc.db._one("SELECT pin FROM motion_drafts WHERE id=?", (did,)))["pin"] == 0
    # 남이 누르면 안 바뀜 (요청자 검사 — pin 경로는 방 관리자 검사가 없어 이 검사만 탄다)
    stranger = FakeQuery(OUTSIDER, fake_user(OUTSIDER, "외부"), data=f"mbn:pin:{did}")
    await M.on_cb(svc, bot, stranger, ["pin", str(did)])
    assert (await svc.db._one("SELECT pin FROM motion_drafts WHERE id=?", (did,)))["pin"] == 0
    assert stranger.answers[-1][1] is True, "남이 눌렀는데 경고 안 함"


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
