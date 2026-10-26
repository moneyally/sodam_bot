"""📏 채팅 집계 최소 글자 수 (방 설정 chat_min_chars, sodam/stats.py · db.MIN_CHARS). 2026-10-10 뉴월드 '5글자부터만 세야 하는데 그 아래도 셈'.
python tests/run_all.py chat_min_chars"""
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fakes import fake_user, make_db, make_svc, runner

from sodam import stats
from sodam.db import CountRule
from sodam.panels import reports as P

test, run_all = runner()
TZ = ZoneInfo("Asia/Seoul")
CHAT = -1004444
QCHAT = -1004445


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
async def quiet_hours_excluded_from_count():
    """새벽 등 특정 시간대(한국 시각) 글은 집계에서 뺌 (2026-10-26 뉴월드 '새벽 1~7시 X')."""
    assert stats.parse_quiet("1-7") == (1, 7) and stats.parse_quiet("23-7") == (23, 7)
    assert stats.parse_quiet("off") is None and stats.parse_quiet("") is None and stats.parse_quiet("5-5") is None
    assert stats.valid_quiet("1-7") == "1-7" and stats.valid_quiet("없음") == "off"
    for bad in ("99-7", "1", "abc"):
        try:
            stats.valid_quiet(bad)
            raise AssertionError(bad)
        except ValueError:
            pass

    db = await make_db()
    await db.ensure_chat(QCHAT, "밤방")
    await db.upsert_user(fake_user(1, "밤이"))
    base = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)                 # 월요일 0시 KST
    for i, hour in enumerate([0, 2, 5, 7, 9, 23]):                # 한국 시각 그 시에 글 하나씩
        await db.log_message(QCHAT, 1, 300 + i, f"글 {hour}시 내용입니다", ts=int(base.replace(hour=hour, minute=30).timestamp()))
    since, until = int(base.timestamp()), int(base.replace(hour=23, minute=59, second=59).timestamp()) + 1

    async def top(rule):
        rows = await db.top_chatters(QCHAT, since, 10, until, rule)
        return rows[0]["n"] if rows else 0
    assert await top(CountRule()) == 6                            # 제외 없음 = 전부
    assert await top(CountRule(quiet=(1, 7))) == 4                 # 2·5시 빠짐 (0·7·9·23 남음)
    assert await top(CountRule(quiet=(23, 7))) == 2               # 자정 넘김: 23·0·2·5 빠짐 (7·9 남음)

    await db.set_setting(QCHAT, "chat_quiet_hours", "1-7")        # 설정 연동
    rule = await stats.min_chars(db, QCHAT)
    assert rule.quiet == (1, 7) and await top(rule) == 4
    assert "1~7시 빼고" in stats._rule(rule)
    await db.set_setting(QCHAT, "chat_quiet_hours", "off")
    assert (await stats.min_chars(db, QCHAT)).quiet is None and await top(await stats.min_chars(db, QCHAT)) == 6


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
async def over_text_counts_people_above_threshold():
    db = await setup()   # 가나 7개, 다라 3개
    t2 = await stats.over_text(db, CHAT, TZ, "오늘", 2)
    assert "2개 이상 친 사람 2명" in t2 and "가나(7)" in t2 and "다라(3)" in t2, t2
    t4 = await stats.over_text(db, CHAT, TZ, "오늘", 4)
    assert "4개 이상 친 사람 1명" in t4 and "가나(7)" in t4 and "다라" not in t4, t4
    t10 = await stats.over_text(db, CHAT, TZ, "오늘", 10)
    assert "10개 이상 친 사람은 없어요" in t10, t10


@test
async def over_text_respects_min_chars_rule():
    db = await setup()
    await db.set_setting(CHAT, "chat_min_chars", 5)   # 가나 1개(긴 글만), 다라 3개
    t = await stats.over_text(db, CHAT, TZ, "오늘", 2)
    assert "2개 이상 친 사람 1명" in t and "다라(3)" in t and "가나" not in t, t


@test
async def chat_stats_tool_over_param():
    from sodam import tools
    db = await setup()
    svc = await make_svc(db, admins=set())
    ctx = SimpleNamespace(svc=svc, chat_id=CHAT, caller=fake_user(1, "가나"),
                          name_notes=[], bot=SimpleNamespace(id=999))
    out = await tools.t_chat_stats(ctx, {"over": 2, "period": "오늘"})
    assert "2개 이상 친 사람 2명" in out, out


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


@test
async def no_repeat_counts_same_text_within_ten_minutes_once():
    db = await make_db()
    await db.ensure_chat(CHAT, "방")
    now = int(time.time()) - 3600
    for uid, name in ((1, "도배"), (2, "보통")):
        await db.upsert_user(fake_user(uid, name))
    for i in range(8):                                            # 같은 글 8번 (1분 간격) → 1번
        await db.log_message(CHAT, 1, 300 + i, "출석체크 이벤트", ts=now + i * 60)
    await db.log_message(CHAT, 1, 320, "출석체크 이벤트", ts=now + 7 * 60 + 601)   # 마지막 반복 뒤 10분 넘음 → 다시 셈
    await db.log_message(CHAT, 3, 330, "출석체크 이벤트", ts=now + 30)            # 다른 사람 같은 글은 셈
    await db.upsert_user(fake_user(3, "남"))
    await db.log_message(CHAT, 4, 340, "두번", ts=now)                # 같은 초에 두 번 → 1번
    await db.log_message(CHAT, 4, 341, "두번", ts=now)
    await db.upsert_user(fake_user(4, "같은초"))
    for i, t in enumerate(["안녕", "뭐해", "밥 먹었어", "ㅋㅋ"]):
        await db.log_message(CHAT, 2, 400 + i, t, ts=now + i * 60)
    text = await stats.ranking_text(db, CHAT, TZ, "7일")          # '오늘'이면 자정 직후에 시험이 흔들림
    assert "도배 — 9개" in text, text
    await db.set_setting(CHAT, "chat_no_repeat", True)
    text = await stats.ranking_text(db, CHAT, TZ, "7일")
    assert "도배 빼고" in text and "보통 — 4개" in text and "도배 — 2개" in text and "남 — 1개" in text and "같은초 — 1개" in text, text
    assert text.index("보통 —") < text.index("도배 —")
    me = await stats.member_text(db, CHAT, TZ, "7일", 1, "도배")
    assert "2개" in me and "2위" in me, me
    totals = await db.chat_totals(CHAT, 0, None, await stats.min_chars(db, CHAT))
    assert totals["messages"] == 8


if __name__ == "__main__":
    run_all()
