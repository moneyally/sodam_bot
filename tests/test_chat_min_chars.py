"""📏 채팅 집계 최소 글자 수 (방 설정 chat_min_chars, sodam/stats.py · db.MIN_CHARS). 2026-10-10 뉴월드 '5글자부터만 세야 하는데 그 아래도 셈'.
python tests/run_all.py chat_min_chars"""
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fakes import fake_user, make_db, make_svc, runner

from sodam import stats
from sodam.panels import reports as P

test, run_all = runner()
TZ = ZoneInfo("Asia/Seoul")
CHAT = -1004444


async def setup():
    db = await make_db()
    await db.ensure_chat(CHAT, "방")
    now = int(time.time())
    for uid, name in ((1, "가나"), (2, "다라")):
        await db.upsert_user(fake_user(uid, name))
    # 가나: 짧은 글 6개 + 긴 글 1개 / 다라: 긴 글 3개 ('안 녕 하 세' = 띄어쓰기 빼면 4글자 → 안 셈)
    for i, t in enumerate(["ㅋㅋ", "ㅇㅇ", "넵", "굿굿", "ㅎㅎㅎㅎ", "안 녕 하 세", "오늘 이벤트 언제"]):
        await db.log_message(CHAT, 1, 100 + i, t, ts=now - 60)
    for i, t in enumerate(["반갑습니다 대표님", "공지 확인했어요", "다섯글자다"]):
        await db.log_message(CHAT, 2, 200 + i, t, ts=now - 60)
    return db


@test
async def all_messages_count_by_default():
    db = await setup()
    text = await stats.ranking_text(db, CHAT, TZ, "오늘")
    assert "가나 — 7개" in text and "다라 — 3개" in text and "글자 이상" not in text, text


@test
async def five_chars_rule_counts_only_long_messages():
    db = await setup()
    await db.set_setting(CHAT, "chat_min_chars", 5)
    text = await stats.ranking_text(db, CHAT, TZ, "오늘")
    assert "5글자 이상만" in text and "다라 — 3개" in text and "가나 — 1개" in text, text
    assert text.index("다라") < text.index("가나"), "짧은 글로는 순위가 안 오름"
    me = await stats.member_text(db, CHAT, TZ, "오늘", 1, "가나")
    assert "1개" in me and "2위" in me, me
    summary = await stats.summary_text(db, CHAT, TZ, "오늘")
    assert "메시지 4개" in summary, summary
    await db.set_setting(CHAT, "chat_min_chars", 50)
    assert "기록이 아직 없어요" in await stats.ranking_text(db, CHAT, TZ, "오늘")


@test
async def setting_shows_on_report_screen_with_presets():
    db = await setup()
    svc = await make_svc(db, admins={9})
    c = SimpleNamespace(svc=svc, bot=SimpleNamespace(), cid=CHAT, uid=9, args=[], arg=lambda i: "")
    real, P._can_receive = P._can_receive, _no
    try:
        screen = await P.s_rp(c)
        assert "모든 글" in screen.text and "m:n:-1004444:chat_min_chars:5" in str(screen.kb.inline_keyboard)
        await db.set_setting(CHAT, "chat_min_chars", 5)
        screen = await P.s_rp(c)
        assert "5글자 이상" in screen.text and "● 5자↑" in str(screen.kb.inline_keyboard)
    finally:
        P._can_receive = real


async def _no(c):
    return False


if __name__ == "__main__":
    run_all()
