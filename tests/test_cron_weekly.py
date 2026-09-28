"""매주·평일·주말 예약 + 입장·퇴장 통계 스킬: python tests/run_all.py cron_weekly"""
from datetime import datetime

from fakes import TZ, make_db, make_svc, runner

from sodam import announce, cron

test, run_all = runner()
CHAT = -1007770001


def ts(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


@test
def parse_weekly_forms():
    p = announce.parse_when
    assert p("매주 월 10:00") == ("weekly", "월 10:00", None)
    assert p("매주 월요일 9:05") == ("weekly", "월 09:05", None)
    assert p("매주 금, 월 ,수 10:00") == ("weekly", "월수금 10:00", None)      # 순서 정리
    assert p("평일 09:00") == ("weekly", "월화수목금 09:00", None)
    assert p("주말 11:30") == ("weekly", "토일 11:30", None)
    assert p("매일 00:00") == ("daily", "00:00", None)
    for bad in ("매주 10:00", "월 10:00", "매주 월", "매주 월 25:00"):
        try:
            p(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    d = announce.describe_when
    assert d("weekly", "월수금 10:00", None) == "매주 월·수·금 10:00"
    assert d("weekly", "월화수목금 09:00", None) == "평일 09:00"
    assert d("weekly", "토일 11:30", None) == "주말 11:30"


@test
def weekly_due_only_on_listed_days():
    row = {"enabled": 1, "kind": "weekly", "at_time": "월수 10:00", "last_sent": None, "interval_min": None, "at_ts": None}
    mon = ts(2026, 9, 28, 10, 0)          # 2026-09-28 = 월요일
    assert datetime.fromtimestamp(mon, TZ).weekday() == 0
    assert announce.is_due(row, mon + 60, TZ)
    assert not announce.is_due(row, mon + 86400 + 60, TZ)                     # 화요일
    assert announce.is_due(row, mon + 2 * 86400 + 60, TZ)                     # 수요일
    assert not announce.is_due({**row, "last_sent": mon + 60}, mon + 120, TZ)  # 같은 날 두 번 안 함
    assert not announce.is_due(row, mon - 60, TZ)                             # 아직 시각 전
    daily = {**row, "kind": "daily", "at_time": "10:00"}
    assert announce.is_due(daily, mon + 86400 + 60, TZ)                       # 매일은 그대로


@test
async def joins_skill_counts_since_last_run():
    db = await make_db()
    svc = await make_svc(db)
    now = int(datetime.now(TZ).timestamp())
    for uid, ago in ((1, 3), (2, 2), (3, 10)):                                # 3번은 10일 전 (7일 밖)
        await db._write("INSERT INTO users(user_id, first_name, is_bot, updated_at) VALUES(?,?,0,?)", (uid, f"u{uid}", now))
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)",
                        (CHAT, uid, now - ago * 86400, now))
    await db._write("INSERT INTO member_left(chat_id, user_id, ts) VALUES(?,?,?)", (CHAT, 9, now - 86400))
    out = await cron.joins_text(svc, CHAT, None)
    assert "입장 2명 · 퇴장 1명 · 순증 +1명" in out and "(7일)" in out, out
    out = await cron.joins_text(svc, CHAT, now - int(2.5 * 86400))            # 지난번 실행 이후만
    assert "입장 1명 · 퇴장 1명 · 순증 +0명" in out, out
    assert "입장 2명" in await cron.run_skill(svc, {"chat_id": CHAT, "text": "", "skill": "joins", "last_sent": None})


if __name__ == "__main__":
    run_all()
