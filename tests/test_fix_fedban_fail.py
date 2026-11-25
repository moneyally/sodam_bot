"""🚨 박제(공동차단) 밴 실패 안내 구분 — python tests/run_all.py fix_fedban_fail

2026-11-25 일루왕 오너: 방 밖 사기꾼(@spay_2)을 박제했더니 "내보내기 실패 (봇 권한 확인)" 이 떠서
박제가 안 된 줄 알았다 — 실제론 명단 등재는 성공(#58)하고, 그 사람이 방에 없어서 '지금 내보내기'만 실패한 것.
→ 밴 실패를 ① 봇 권한 부족(권한 확인) ② 방에 없는 사람(명단 올림·들어오면 자동 차단)으로 구분해 안내."""
from types import SimpleNamespace

from telegram.error import BadRequest, Forbidden

from fake_llm import Room
from fakes import fake_user, runner

from sodam import fedban

test, run_all = runner()
ADMIN = fake_user(1, "관리자")
SCAMMER = 8725575586
CHAT = Room.CHAT


async def world():
    r = await Room().open(admins={1})
    r.svc.billing = None            # paid_features → True (이용 기간 통과)
    return r


async def do_add(r):
    return await fedban.add(r.svc, r.bot, CHAT, SCAMMER, "S_PAY", "원화 환전 사기 500만원",
                            ADMIN.id, auto_ban=True)


@test
async def classifier_tells_perm_from_absent():
    assert fedban._ban_fail_is_perm(BadRequest("Not enough rights to restrict/unrestrict chat member"))
    assert fedban._ban_fail_is_perm(Forbidden("bot was kicked"))
    assert not fedban._ban_fail_is_perm(BadRequest("Participant_id_invalid"))
    assert not fedban._ban_fail_is_perm(BadRequest("PEER_ID_INVALID"))


@test
async def absent_person_shows_listed_and_auto_ban_not_permission():
    r = await world()

    async def ban_absent(chat_id, user_id, **kw):
        raise BadRequest("Participant_id_invalid")
    r.bot.ban_chat_member = ban_absent

    out = await do_add(r)
    assert "공동 차단 명단" in out and "올리고" in out        # 박제는 됐다
    assert "들어오면 자동으로 차단" in out                   # 들어오면 자동 차단 안내
    assert "수배 #" in out                                   # 수배번호 보여 줌
    assert "권한" not in out, out                            # 권한 탓 안 함 (오해 원인 제거)
    # 실제로 명단에 등재됐는지 DB 확인
    assert await fedban.lookup(r.db, SCAMMER) is not None


@test
async def real_permission_failure_still_asks_to_check_rights():
    r = await world()

    async def ban_noperm(chat_id, user_id, **kw):
        raise BadRequest("Not enough rights to restrict/unrestrict chat member")
    r.bot.ban_chat_member = ban_noperm

    out = await do_add(r)
    assert "공동 차단 명단" in out and "올리고" in out        # 박제는 여전히 됨
    assert "'사용자 차단' 권한" in out                       # 진짜 권한 문제일 땐 권한 확인 안내
    assert "자동으로 차단" not in out, out


@test
async def successful_ban_says_removed():
    r = await world()
    out = await do_add(r)                                   # FakeBot 기본 ban 성공
    assert "이 방에서 내보냈어요" in out and ("ban", CHAT, SCAMMER) in r.bot.calls


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
