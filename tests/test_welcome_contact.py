"""봇 초대 인사에 문의처(@ban_1599) 노출 — 새 관리자가 쓸 줄 몰라도 문의처를 보게.
2026-11-01 오너 요청. AI 안 씀(정해진 문구). python tests/run_all.py welcome_contact"""
import dataclasses

from fakes import make_db, make_svc, runner

from sodam import handlers

test, run_all = runner()


@test
async def welcome_shows_contact_and_steps():
    db = await make_db()
    svc = await make_svc(db)
    svc.cfg = dataclasses.replace(svc.cfg, support_contact="@ban_1599")
    intro = handlers._welcome_intro(svc, True, 3)
    assert "@ban_1599" in intro and "문의" in intro, "문의처가 인사에 나와야 함"
    assert "처음 설정하는 법" in intro, "설정 안내(SETUP_STEPS) 유지"
    assert "3일" in intro, "체험 중이면 체험 안내"
    await db.close()


@test
async def no_contact_when_empty():
    db = await make_db()
    svc = await make_svc(db)
    svc.cfg = dataclasses.replace(svc.cfg, support_contact="")
    intro = handlers._welcome_intro(svc, True, 3)
    assert "문의하세요" not in intro, "문의처 비우면 그 줄 없음"
    assert "처음 설정하는 법" in intro, "안내 본문은 그대로"
    await db.close()


@test
async def no_trial_line_when_not_trial():
    db = await make_db()
    svc = await make_svc(db)
    svc.cfg = dataclasses.replace(svc.cfg, support_contact="@ban_1599")
    intro = handlers._welcome_intro(svc, False, 3)
    assert "모든 기능을 써보실" not in intro, "체험 아님이면 체험 안내 없음"
    assert "@ban_1599" in intro, "문의처는 체험과 무관하게 나옴"
    await db.close()


@test
async def music_no_helper_names_assistant():
    """노래 신청 시 도우미(자이)가 방에 없으면 그 계정 아이디를 콕 집어 초대 안내 (오너 요청)."""
    from sodam.panels import music
    m = music._no_helper_msg("@Ban_1599", True)
    assert "@Ban_1599" in m and "초대" in m, "자이 아이디 명시 + 초대 안내"
    assert "사용자 초대" in m, "권한 안내도"
    m2 = music._no_helper_msg("@Ban_1599", False)
    assert "@Ban_1599" in m2 and "없어요" in m2, "도우미 없음 + 아이디 명시"


if __name__ == "__main__":
    import sys
    sys.exit(1 if __import__("asyncio").run(run_all()) else 0)
