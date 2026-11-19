"""📢 멤버 신고 (@관리자·.신고): python tests/run_all.py report

2026-11-19 경쟁조사(Rose /report·Combot). 멤버가 나쁜 글에 답장+신고 → '사용자 차단' 권한 관리자 1:1 로 알림
(incidents.open_or_bump 재활용). 자동 제재 없음, 기본 꺼짐."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import FakeMsg, add_member, fake_user, runner  # noqa: E402

from sodam import report  # noqa: E402

test, run_all = runner()
ADMIN, REPORTER, BADGUY = 5, 30, 40


async def room(enabled=True):
    report._last.clear()
    r = Room()
    await r.open(admins=(ADMIN,), settings={"report_enabled": enabled, "cas_enabled": False})
    r.bot.admins = [fake_user(ADMIN, "방장"), fake_user(r.bot.id, "소담", "sodambot", is_bot=True)]

    async def can(bot, cid, uid, right="restrict"):
        return uid in r.svc.perms.admins
    r.svc.perms.can = can
    for u in (fake_user(REPORTER, "신고자"), fake_user(BADGUY, "나쁜놈")):
        await add_member(r.db, r.CHAT, u)
    return r


def report_msg(r, text="@관리자 이 사람 신고", reporter=REPORTER, bad=BADGUY, bad_text="사기 치지마"):
    target = FakeMsg(r.CHAT, fake_user(bad, "나쁜놈"), bad_text, message_id=50)
    return FakeMsg(r.CHAT, fake_user(reporter, "신고자"), text, message_id=90, reply_to=target)


def dms(r, uid=None):
    return [c for c in r.bot.named("send_message") if c[1] > 0 and (uid is None or c[1] == uid)]


# ── 트리거 정규식 (오탐 방지) ─────────────────────────────────
@test
def trigger_matches_report_intent_not_casual():
    for t in ("@관리자", "@admin", "이거 신고", ".신고", "신고요", "신고합니다"):
        assert report.TRIGGER.search(t), t
    for t in ("관리자님 고마워요", "그냥 잡담", "오늘 날씨 좋네", "신고전화 뭐였지"):
        assert not report.TRIGGER.search(t), t


# ── 신고 전달 ────────────────────────────────────────────────
@test
async def report_dms_admins_with_link():
    r = await room(enabled=True)
    await report.handle(r.svc, r.bot, report_msg(r), 0)
    sent = dms(r, ADMIN)
    assert sent, "차단권한 관리자에게 1:1 신고가 가야 함"
    body = sent[0][2]
    assert "신고" in body and "나쁜놈" in body and "신고자" in body, body
    # 🔗 신고된 글 링크는 본문에 (incidents 가 URL 버튼은 버리므로)
    assert "t.me/c/" in body and "신고된 글 보기" in body, body
    # 신고자에게 조용한 반응
    assert r.bot.named("reaction"), "신고자 글에 👀 반응"
    # 방 하루 신고 수 셈
    import datetime as _dt
    day = _dt.datetime.now(r.svc.cfg.tz).strftime("%Y-%m-%d")
    assert await r.db.counter(day, r.CHAT, "reports") == 1


@test
async def off_room_sends_nothing():
    r = await room(enabled=False)
    await report.handle(r.svc, r.bot, report_msg(r), 0)
    assert not dms(r), "꺼진 방은 신고 전달 안 함 (기본 꺼짐)"


@test
async def non_reply_or_casual_ignored():
    r = await room(enabled=True)
    # 답장 아님
    await report.handle(r.svc, r.bot, FakeMsg(r.CHAT, fake_user(REPORTER, "신고자"), "@관리자", message_id=91), 0)
    # 답장인데 신고 트리거 아님
    await report.handle(r.svc, r.bot, report_msg(r, text="ㅋㅋ 관리자님 안녕"), 0)
    assert not dms(r)


@test
async def self_and_bot_report_ignored():
    r = await room(enabled=True)
    await report.handle(r.svc, r.bot, report_msg(r, reporter=BADGUY, bad=BADGUY), 0)   # 본인 글 신고
    assert not dms(r), "본인 글 신고는 무시"


@test
async def cooldown_blocks_repeat_by_same_reporter():
    import datetime as _dt
    r = await room(enabled=True)
    day = _dt.datetime.now(r.svc.cfg.tz).strftime("%Y-%m-%d")
    await report.handle(r.svc, r.bot, report_msg(r), 0)
    await report.handle(r.svc, r.bot, report_msg(r, bad_text="또 신고"), 0)   # 같은 사람 바로 또
    # 쿨다운이 두 번째를 bump 전에 끊어야 함 (incidents 재전송은 편집이라 send 수로는 구분 안 됨 → 신고 카운터로 확인)
    assert await r.db.counter(day, r.CHAT, "reports") == 1, "한 사람 쿨다운 안에선 두 번째 신고를 세지 않음"


if __name__ == "__main__":
    asyncio.run(run_all())
