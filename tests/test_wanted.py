"""🚨 수배(박제) 조회 시스템: python tests/run_all.py wanted

공동 차단 명단에 원글(박제 증거) 저장 + 누구나 조회 + 방 공개 수배 카드(원글 펼침/접기 버튼) + warn 모드.
setup·cmd·RightsBot 은 test_fedban_perms 를 재사용한다.
"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeMsg, FakeQuery, fake_user, runner
from test_fedban_perms import A, B, CREATOR, MEMBER, OWNER, STRONG, TARGET, WEAK, _drain, cmd, join_ctx, setup

from sodam import fedban, free, handlers
from sodam.permissions import Role

test, run_all = runner()


def scam_post(uid=TARGET, caption="입금 TXwallet9999 주소로 보내면 2배 사기 DM"):
    """박제 원글(사진 + 지갑주소 캡션) — 필터 없이 그대로 저장되는지 확인용."""
    return FakeMsg(A, fake_user(uid, "사기꾼"), "", caption=caption,
                   photo=[SimpleNamespace(file_id="SCAMPIC")], message_id=555)


async def register_with_evidence(svc, bot, caption="입금 TXwallet9999 주소로 보내면 2배 사기 DM"):
    # 오너가 올리면 자동 밴(=기존 등록 흐름) — 이 파일의 다른 테스트는 등록이 끝난 상태를 전제로 함
    r = await cmd(svc, bot, A, OWNER, ".공동차단 코인 사기", reply_to=scam_post(caption=caption))
    assert "공동 차단 명단" in r[0], r
    return r


@test
async def register_captures_original_post_and_number():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    info = await fedban.lookup(db, TARGET)
    assert info and info["no"] == 1, info                      # 수배번호 부여
    assert "TXwallet9999" in info["evidence"], info            # 원글 전문 저장 (필터 없이)
    assert info["media_kind"] == "photo" and info["media_id"] == "SCAMPIC", info
    # 별칭 .박제 / .수배 도 같은 등록
    r = await cmd(svc, bot, A, OWNER, ".박제 9001 다른 사기")
    assert "공동 차단 명단" in r[0] and (await fedban.lookup(db, 9001))["no"] == 2, r
    _drain()


@test
async def anyone_lookup_posts_card_with_evidence_button():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    bot.calls.clear()
    r = await cmd(svc, bot, A, MEMBER, f".조회 {TARGET}")       # 일반 멤버도 조회
    cards = [c for c in bot.named("send_message") if "수배" in c[2]]
    assert cards, (r, bot.named("send_message"))
    kb = cards[0][3]["reply_markup"].inline_keyboard
    datas = [b.callback_data for row in kb for b in row]
    assert any(d == f"wnt:open:{TARGET}" for d in datas), datas
    assert any(d == f"wnt:ban:{TARGET}" for d in datas), datas
    assert all(len(d.encode()) <= 64 for d in datas)
    # 명단에 없는 사람
    r = await cmd(svc, bot, A, MEMBER, ".조회 123456789")
    assert "명단에 없는" in r[0], r
    _drain()


@test
async def lookup_by_username_when_not_room_member():
    # 수배 명단은 보통 다른 방 사람 → 이 방 멤버가 아니어도 @아이디로 조회돼야 함 (일루왕 2026-10-21)
    db, svc, bot = await setup()
    await cmd(svc, bot, A, STRONG, ".박제 777111 코인 사기")        # 이 방 멤버 아닌 사람 등재
    await db.upsert_user(fake_user(777111, "사기꾼", username="scamx99"))
    bot.calls.clear()
    r = await cmd(svc, bot, A, MEMBER, ".조회 @scamx99")            # @아이디로 (방 멤버 아님)
    cards = [c for c in bot.named("send_message") if "수배" in c[2]]
    assert cards, (r, bot.named("send_message"))
    _drain()


@test
async def open_close_toggles_card_in_place():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    q = FakeQuery(A, fake_user(MEMBER))                          # 누구나 펼침 가능
    await fedban.on_wanted(svc, bot, q, ["open", str(TARGET)])
    assert q.edits and "TXwallet9999" in q.edits[-1], q.edits    # 원글 그대로(필터 없이) 제자리 교체
    assert "🔙 접기" in str([b.text for row in q.kb.inline_keyboard for b in row])
    await fedban.on_wanted(svc, bot, q, ["close", str(TARGET)])
    assert "TXwallet9999" not in q.edits[-1] and "📄 원글 보기" in str(
        [b.text for row in q.kb.inline_keyboard for b in row]), q.edits[-1]
    _drain()


@test
async def open_falls_back_to_plain_when_html_rejected():
    """박제 원글에 깨진 커스텀 이모지가 있어 HTML 발송이 거부돼도, '📄 원글 보기' 가 글자로라도 펼쳐진다
    (2026-10-27 버그 조사: 폴백 없이 조용히 씹히던 것)."""
    from telegram.error import BadRequest
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot, caption='입금하면 2배 <tg-emoji emoji-id="999">💎</tg-emoji> 사기')

    class PickyQuery(FakeQuery):
        async def edit_message_text(self, text, **kw):
            if kw.get("parse_mode") == "HTML" and "tg-emoji" in text:
                raise BadRequest("Document_invalid")
            self.edits.append(text)
            self.kb = kw.get("reply_markup")

    q = PickyQuery(A, fake_user(MEMBER))
    await fedban.on_wanted(svc, bot, q, ["open", str(TARGET)])
    assert q.edits and "사기" in q.edits[-1] and "tg-emoji" not in q.edits[-1], q.edits   # 글자로 펼쳐짐
    _drain()


@test
async def media_button_sends_original_to_anyone():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    bot.calls.clear()
    q = FakeQuery(A, fake_user(MEMBER))
    await fedban.on_wanted(svc, bot, q, ["media", str(TARGET)])
    assert bot.named("send_photo"), bot.calls                    # 원본 사진 전송
    _drain()


@test
async def ban_button_checks_right():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    bot.calls.clear()                                           # 등록 때 A 에서 이미 밴됨 — 버튼 눌림만 본다
    q = FakeQuery(A, fake_user(WEAK))                            # 차단 권한 없는 관리자 → 막힘
    await fedban.on_wanted(svc, bot, q, ["ban", str(TARGET)])
    assert q.answers and "권한" in q.answers[-1][0] and not [c for c in bot.named("ban") if c[1] == A], q.answers
    q = FakeQuery(A, fake_user(STRONG))                          # 권한 있는 관리자 → 밴 + 카드에 반영
    await fedban.on_wanted(svc, bot, q, ["ban", str(TARGET)])
    assert [c for c in bot.named("ban") if c[1] == A] and "내보냈어요" in (q.answers[-1][0] or ""), (bot.named("ban"), q.answers)
    assert "내보냈어요" in q.edits[-1], q.edits
    _drain()


@test
async def warn_mode_posts_public_card_no_ban():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    await db.set_setting(B, "fedban_mode", "warn")
    bot.calls.clear()
    await handlers.handle_new_member(join_ctx(svc, bot), B, "방2", fake_user(TARGET, "사기꾼"))
    cards = [c for c in bot.named("send_message") if c[1] == B and "수배" in c[2]]
    assert cards and not [c for c in bot.named("ban") if c[1] == B], (cards, bot.named("ban"))   # 경고만, 밴 X
    _drain()


@test
async def ban_mode_bans_and_posts_card():
    db, svc, bot = await setup()
    await register_with_evidence(svc, bot)
    await db.set_setting(B, "fedban_mode", "ban")
    svc.joins.clear()
    bot.calls.clear()
    await handlers.handle_new_member(join_ctx(svc, bot), B, "방2", fake_user(TARGET, "사기꾼"))
    await asyncio.sleep(0)
    assert [c for c in bot.named("ban") if c[1] == B], bot.named("ban")
    cards = [c for c in bot.named("send_message") if c[1] == B and "수배" in c[2]]
    assert cards and "내보냈어요" in cards[-1][2], cards
    _drain()


# ── 올린 사람 역할로 자동밴 vs 선택 카드 (2026-11-07 오너) ──
def room_cards(bot, chat=A):
    return [c for c in bot.named("send_message") if c[1] == chat and "수배" in c[2]]


@test
async def owner_or_creator_autobans():
    db, svc, bot = await setup()
    # 오너
    r = await cmd(svc, bot, A, OWNER, f".공동차단 {TARGET} 코인 사기")
    assert "내보냈어요" in r[0] and [c for c in bot.named("ban") if c[1] == A], r
    assert await fedban.lookup(db, TARGET), "명단 등재"
    # 방장(creator) — get_chat_member 로 creator 판정되게
    bot.member_status = {(A, CREATOR): "creator"}
    bot.calls.clear()
    r = await cmd(svc, bot, A, CREATOR, ".공동차단 8001 사기")
    assert "내보냈어요" in r[0] and [c for c in bot.named("ban") if c[1] == A], r
    _drain()


@test
async def admin_gets_choice_card_no_autoban():
    db, svc, bot = await setup()
    bot.calls.clear()
    r = await cmd(svc, bot, A, STRONG, f".공동차단 {TARGET} 코인 사기")   # 권한 있는 일반 관리자
    assert not [c for c in bot.named("ban") if c[1] == A], "관리자는 자동 밴 안 함"
    assert await fedban.lookup(db, TARGET), "그래도 수배 명단엔 올라감(박제)"
    cards = room_cards(bot)
    assert cards, bot.named("send_message")
    datas = [b.callback_data for row in cards[0][3]["reply_markup"].inline_keyboard for b in row]
    assert any(d == f"wnt:ban:{TARGET}" for d in datas) and any(d == f"wnt:ig:{TARGET}" for d in datas), datas
    # 카드의 [🚫 밴] 을 권한 관리자가 누르면 그때 밴
    q = FakeQuery(A, fake_user(STRONG), f"wnt:ban:{TARGET}")
    await fedban.on_wanted(svc, bot, q, ["ban", str(TARGET)])
    assert [c for c in bot.named("ban") if c[1] == A], "카드 밴 버튼으로 내보냄"
    _drain()


@test
async def free_member_can_report_with_card():
    db, svc, bot = await setup()
    await free.add(db, A, MEMBER, STRONG)        # 자유 멤버로 지정
    bot.calls.clear()
    r = await cmd(svc, bot, A, MEMBER, f".공동차단 {TARGET} 사기꾼")
    assert await fedban.lookup(db, TARGET), "자유 멤버도 박제(명단 등재) 가능"
    assert not [c for c in bot.named("ban") if c[1] == A], "자유 멤버는 자동 밴 안 함"
    assert room_cards(bot), "선택 카드가 뜸"
    _drain()


@test
async def no_ban_admin_can_report_but_not_ban():
    db, svc, bot = await setup()
    bot.calls.clear()
    await cmd(svc, bot, A, WEAK, f".공동차단 {TARGET} 사기")   # 차단 권한 없는 관리자
    assert await fedban.lookup(db, TARGET), "신고(박제)는 됨"
    assert not [c for c in bot.named("ban") if c[1] == A], "자동 밴은 안 함"
    assert room_cards(bot), "선택 카드가 뜸"
    q = FakeQuery(A, fake_user(WEAK), f"wnt:ban:{TARGET}")     # 권한 없는 사람이 밴 누르면 막힘
    await fedban.on_wanted(svc, bot, q, ["ban", str(TARGET)])
    assert "권한" in q.answers[-1][0] and not [c for c in bot.named("ban") if c[1] == A], q.answers
    _drain()


@test
async def plain_member_cannot_fedban():
    db, svc, bot = await setup()
    r = await cmd(svc, bot, A, MEMBER, f".공동차단 {TARGET} 사기")
    assert "관리자나 자유 멤버만" in r[0], r
    assert not await fedban.lookup(db, TARGET), "일반 멤버는 박제 못 함 — 명단에 안 올라감"
    _drain()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
