"""DB 안전성: python tests/run_all.py db_safety

① 디스크(DB)가 가득 차도 스팸 삭제는 계속, 공간이 생기면 다시 기록  ② 여러 단계 쓰기는 전부 아니면 전무
(포인트 차감만 되고 원장이 빠지는 일 없음, 관리자 목록이 반쯤 지워진 채 저장되지 않음)  ③ 디스크 감시·긴급 정리·알림
④ 보관 기간 정리  ⑤ SQLite 설정·인덱스  ⑥ 백업이 중간에 실패해도 반쯤 쓴 파일이 남지 않음
"""
import time
from pathlib import Path

from fake_llm import Room
from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam import diskguard
from sodam.backup import Backup
from sodam.casino import core
from sodam.db import disk_full

test, run_all = runner()
SPAM = fake_user(40, "광고")


async def page_limit(db, extra=0):
    pc = (await (await db.conn.execute("PRAGMA page_count")).fetchone())[0]
    await db.conn.execute(f"PRAGMA max_page_count={pc + extra}")


@test
async def disk_full_still_deletes_spam_and_recovers():
    r = await Room().open(admins={1}, settings={"link_filter": True, "injection_guard": False})
    await r.say(SPAM, "안녕")
    await page_limit(r.db)                                          # 지금 크기에서 더는 못 커짐 = 가득 참
    spams = [await r.say(SPAM, "가입 https://evil.example " + "가" * 3000 + str(i)) for i in range(5)]
    assert all(m.deleted for m in spams), "기록을 못 해도 스팸 삭제는 계속"
    await r.db.conn.execute("PRAGMA max_page_count=1073741823")      # 공간 생김
    await r.say(SPAM, "공간 생긴 뒤")
    assert await r.db._one("SELECT 1 FROM messages WHERE text='공간 생긴 뒤'"), "재시작 없이 다시 기록"


@test
async def debit_is_all_or_nothing():
    db = await make_db()
    await core.credit(db, -1, 7, 1000, "test")
    await db.conn.execute("CREATE TRIGGER boom BEFORE INSERT ON casino_ledger BEGIN SELECT RAISE(ABORT, 'x'); END")
    try:
        await core.debit(db, -1, 7, 300, "bet")
        raise AssertionError("원장 기록 실패면 예외")
    except Exception as e:
        assert "x" in str(e)
    await db.conn.execute("DROP TRIGGER boom")
    await db._write("INSERT INTO requests(chat_id, user_id, text, ts) VALUES(-1, 7, 'other', 0)")   # 다른 코루틴의 커밋
    assert await core.balance(db, -1, 7) == 1000, "차감만 저장되면 안 됨"
    assert (await db._one("SELECT COUNT(*) AS n FROM casino_ledger"))["n"] == 1


@test
async def admin_list_store_failure_keeps_old_list():
    db = await make_db()
    svc = await make_svc(db)
    from sodam.permissions import Permissions
    perms = Permissions(svc.cfg, db)
    await perms._store_admins(-1, {1, 2}, 100, {1: (True, True)})
    await db.conn.execute("CREATE TRIGGER boom BEFORE INSERT ON chat_admin_rights BEGIN SELECT RAISE(ABORT, 'x'); END")
    await perms._store_admins(-1, {3}, 200, {3: (True, True)})     # 실패해도 예외 없이 (메모리 캐시로 동작)
    await db.conn.execute("DROP TRIGGER boom")
    await db._write("INSERT INTO requests(chat_id, user_id, text, ts) VALUES(-1, 7, 'other', 0)")
    assert await perms._stored_admins(-1) == {1, 2}, "지운 것만 저장돼 목록이 바뀌면 안 됨"


@test
async def disk_guard_prunes_and_alerts_once():
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot()
    reports = []

    async def report(_bot, text):
        reports.append(text)
    svc.mod.report = report
    old = int(time.time()) - 40 * 86400
    await db.log_message(-1, 7, 1, "40일 전", ts=old)
    await db.log_message(-1, 7, 2, "오늘")
    diskguard._last_alert = 0.0
    assert not await diskguard.check(svc, bot, free=diskguard.LOW_MB)          # 기준 이상이면 아무것도 안 함
    assert await db._one("SELECT 1 FROM messages WHERE text='40일 전'")
    assert await diskguard.check(svc, bot, free=diskguard.LOW_MB - 1)
    assert not await db._one("SELECT 1 FROM messages WHERE text='40일 전'") and await db._one(
        "SELECT 1 FROM messages WHERE text='오늘'")
    await diskguard.check(svc, bot, free=10)
    assert len(reports) == 1 and "499MB" in reports[0] and "30일" in reports[0], reports   # 6시간에 1번만


@test
async def daily_prune_keeps_recent_and_ledgers():
    db = await make_db()
    now = int(time.time())
    await db.log_message(-1, 7, 1, "옛날", ts=now - 100 * 86400)
    await db.log_message(-1, 7, 2, "최근", ts=now - 10 * 86400)
    await db._write("INSERT INTO requests(chat_id, user_id, text, ts) VALUES(-1, 7, '옛요청', ?)", (now - 100 * 86400,))
    await db._write("INSERT INTO counters(day, chat_id, key, n) VALUES('2020-01-01', -1, 'x', 1)")
    await db.bump(time.strftime("%Y-%m-%d"), -1, "x")
    await core.credit(db, -1, 7, 5, "old")
    await db._write("UPDATE casino_ledger SET ts=0")
    await db.prune(now)
    texts = {r["text"] for r in await db._all("SELECT text FROM messages")}
    assert texts == {"최근"}, texts
    assert not await db._one("SELECT 1 FROM requests") and not await db._one("SELECT 1 FROM counters WHERE day='2020-01-01'")
    assert await db._one("SELECT 1 FROM counters WHERE key='x'") and await db._one("SELECT 1 FROM casino_ledger")


@test
async def sqlite_settings_and_index():
    db = await make_db()
    assert (await (await db.conn.execute("PRAGMA synchronous")).fetchone())[0] == 1       # NORMAL
    assert (await (await db.conn.execute("PRAGMA journal_size_limit")).fetchone())[0] == 64 * 1024 * 1024
    plan = " ".join(str(tuple(r)) for r in await db._all(
        "EXPLAIN QUERY PLAN SELECT action FROM mod_log WHERE chat_id=? AND ts>=? AND ts<?", (-1, 0, 1)))
    assert "idx_mod_log_chat_ts" in plan, plan


@test
async def backup_failure_leaves_no_partial_file():
    db = await make_db()
    svc = await make_svc(db)
    b = Backup(svc.cfg, db)
    b.dir = Path(db.path).parent / "bk"

    async def broken(path):
        Path(path).write_bytes(b"x" * 1000)          # 반쯤 쓰다가
        raise OSError(28, "No space left on device")
    db.backup_to = broken
    try:
        await b.run()
        raise AssertionError("실패해야 함")
    except OSError:
        pass
    assert list(b.dir.iterdir()) == [], list(b.dir.iterdir())
    del db.backup_to                                  # 백업은 되고 압축하다 가득 참

    def half_gz(raw):
        raw.with_name(raw.name + ".gz").write_bytes(b"x" * 500)
        raise OSError(28, "No space left on device")
    b._verify_and_compress = half_gz
    try:
        await b.run()
        raise AssertionError("실패해야 함")
    except OSError:
        pass
    assert list(b.dir.iterdir()) == [], list(b.dir.iterdir())


@test
async def disk_full_detector():
    import sqlite3
    assert disk_full(sqlite3.OperationalError("database or disk is full"))
    assert not disk_full(sqlite3.OperationalError("database is locked"))


@test
async def disk_job_registered():
    from datetime import timezone
    from telegram.ext import ApplicationBuilder
    from sodam import handlers
    app = ApplicationBuilder().token("1:x").build()
    handlers.register(app, timezone.utc)
    jobs = {j.name: j for j in app.job_queue.jobs()}
    assert "disk" in jobs and jobs["disk"].callback is handlers.job_disk


if __name__ == "__main__":
    run_all()
