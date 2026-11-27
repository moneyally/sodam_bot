"""🐘 PG 백엔드 껍데기 검증: python tests/run_all.py pgbackend

PG(psycopg + 로컬 서버) 없으면 조용히 건너뜀(PASS) — 서버·개발 컨테이너엔 PG16 설치돼 실제로 돈다.
db.py 에 아직 안 꽂음 — 이 모듈만. 라이브 0 접촉."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import runner  # noqa: E402

from sodam import pgbackend as P  # noqa: E402
from sodam.pgbackend import translate  # noqa: E402

test, run_all = runner()


def _conn():
    """로컬 PG 에 붙어보고, 안 되면 None(건너뜀).

    ⚠️ 라이브 PG 접속은 **opt-in 일 때만** (PG_TEST=1 또는 PG_TEST_DSN 지정).
    기본값(서버 자동배포 포함)에선 접속 안 함 → 번역·Row 순수함수만 돌아서, 혹시 모를 PG 환경 차이로
    전체 배포가 막히는 일이 없게. PG 백엔드 개발·검증은 로컬에서 PG_TEST=1 로 돌린다."""
    dsn = os.getenv("PG_TEST_DSN", "")
    if not dsn:
        if os.getenv("PG_TEST") != "1":
            return None
        dsn = "dbname=sodam_probe"
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return None
    try:
        return P.PgConnection(dsn)
    except Exception:
        return None


# ── 번역은 PG 없어도 검사 (순수 함수) ──────────────────────────────────
@test
async def translate_rules():
    assert translate("SELECT * FROM t WHERE a=?") == "SELECT * FROM t WHERE a=%s"
    assert translate("INSERT OR IGNORE INTO t VALUES(?,?)").endswith("ON CONFLICT DO NOTHING")
    assert "GENERATED ALWAYS AS IDENTITY" in translate("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    assert "AUTOINCREMENT" not in translate("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    assert "extract(epoch from now())" in translate("INSERT INTO t(ts) VALUES(strftime('%s','now'))")
    assert translate("SELECT x FROM t WHERE b LIKE '%a%'") == "SELECT x FROM t WHERE b LIKE '%%a%%'"   # % 보존


@test
async def row_index_and_key():
    r = P.Row((7, "커피"), ("id", "name"))
    assert r[0] == 7 and r["id"] == 7 and r["name"] == "커피" and r.get("nope", 0) == 0
    assert list(r.keys()) == ["id", "name"]


# ── 아래는 PG 있을 때만 (없으면 건너뜀) ────────────────────────────────
@test
async def basic_ops_and_insert_or_ignore():
    c = _conn()
    if c is None:
        print("  (PG 없음 — 건너뜀)")
        return
    try:
        await c.executescript("DROP TABLE IF EXISTS pgt_m; CREATE TABLE pgt_m(chat_id BIGINT, user_id BIGINT, points BIGINT DEFAULT 0, PRIMARY KEY(chat_id,user_id));")
        await c.execute("INSERT OR IGNORE INTO pgt_m(chat_id,user_id,points) VALUES(?,?,?)", (-1, 7, 10000))
        await c.execute("INSERT OR IGNORE INTO pgt_m(chat_id,user_id,points) VALUES(?,?,?)", (-1, 7, 999))  # 중복 무시
        await c.commit()
        rows = await c.execute_fetchall("SELECT points FROM pgt_m WHERE chat_id=? AND user_id=?", (-1, 7))
        assert len(rows) == 1 and rows[0]["points"] == 10000, rows   # 999 로 안 덮임
        await c.executescript("DROP TABLE pgt_m;")
        await c.commit()
    finally:
        await c.close()


@test
async def atomic_callback_runs_unchanged():
    """db.atomic(fn) 처럼 콜백이 raw 연결에 c.execute 하는 걸 안 뜯고 동작해야 (제일 큰 난관)."""
    c = _conn()
    if c is None:
        print("  (PG 없음 — 건너뜀)")
        return
    try:
        await c.executescript("DROP TABLE IF EXISTS pgt_m; DROP TABLE IF EXISTS pgt_l;"
                              "CREATE TABLE pgt_m(user_id BIGINT PRIMARY KEY, points BIGINT DEFAULT 0);"
                              "CREATE TABLE pgt_l(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, delta BIGINT);")
        await c.execute("INSERT INTO pgt_m(user_id,points) VALUES(?,?)", (7, 1000))
        await c.commit()

        def run(conn):                      # ← db.atomic 콜백과 똑같은 모양 (동기 c.execute)
            conn.execute("UPDATE pgt_m SET points=points+? WHERE user_id=?", (500, 7))
            cur = conn.execute("INSERT INTO pgt_l(user_id,delta) VALUES(?,?)", (7, 500))
            return cur.lastrowid
        lastid = await c.atomic(run)
        bal = (await c.execute_fetchall("SELECT points FROM pgt_m WHERE user_id=7"))[0]["points"]
        assert bal == 1500 and lastid == 1, (bal, lastid)

        # 실패하면 전부 롤백 (전부/전무)
        def boom(conn):
            conn.execute("UPDATE pgt_m SET points=points+? WHERE user_id=?", (9999, 7))
            raise RuntimeError("일부러")
        try:
            await c.atomic(boom)
        except RuntimeError:
            pass
        bal2 = (await c.execute_fetchall("SELECT points FROM pgt_m WHERE user_id=7"))[0]["points"]
        assert bal2 == 1500, f"롤백 안 됨: {bal2}"
        await c.executescript("DROP TABLE pgt_m; DROP TABLE pgt_l;")
        await c.commit()
    finally:
        await c.close()


@test
async def db_style_atomic_via_execute_and_conn():
    """db.py 의 atomic() 을 글자 그대로 흉내: self.conn._execute(run, self.conn._conn) + SAVEPOINT.
    이게 되면 144개 atomic 콜백이 db.py 수정 없이 산다 (통합의 핵심)."""
    c = _conn()
    if c is None:
        print("  (PG 없음 — 건너뜀)")
        return
    try:
        await c.executescript("DROP TABLE IF EXISTS pgt_m; DROP TABLE IF EXISTS pgt_l;"
                              "CREATE TABLE pgt_m(user_id BIGINT PRIMARY KEY, points BIGINT DEFAULT 0);"
                              "CREATE TABLE pgt_l(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, delta BIGINT);")
        await c.execute("INSERT INTO pgt_m(user_id,points) VALUES(?,?)", (7, 1000))
        await c.commit()

        def db_atomic_run(conn):                    # ← db.py atomic() 의 run() 과 똑같은 몸체
            conn.execute("SAVEPOINT atomic")
            try:
                conn.execute("UPDATE pgt_m SET points=points+? WHERE user_id=?", (500, 7))
                cur = conn.execute("INSERT INTO pgt_l(user_id,delta) VALUES(?,?)", (7, 500))
                out = cur.lastrowid
            except BaseException:
                conn.execute("ROLLBACK TO atomic")
                conn.execute("RELEASE atomic")
                raise
            conn.execute("RELEASE atomic")
            conn.commit()
            return out
        last = await c._execute(db_atomic_run, c._conn)   # ← db.py line 451 과 동일
        bal = (await c.execute_fetchall("SELECT points FROM pgt_m WHERE user_id=7"))[0]["points"]
        assert bal == 1500 and last == 1, (bal, last)

        def db_atomic_boom(conn):
            conn.execute("SAVEPOINT atomic")
            try:
                conn.execute("UPDATE pgt_m SET points=points+? WHERE user_id=?", (9999, 7))
                raise RuntimeError("일부러")
            except BaseException:
                conn.execute("ROLLBACK TO atomic")
                conn.execute("RELEASE atomic")
                raise
        try:
            await c._execute(db_atomic_boom, c._conn)
        except RuntimeError:
            pass
        await c.commit()
        bal2 = (await c.execute_fetchall("SELECT points FROM pgt_m WHERE user_id=7"))[0]["points"]
        assert bal2 == 1500, f"SAVEPOINT 롤백 안 됨: {bal2}"
        await c.executescript("DROP TABLE pgt_m; DROP TABLE pgt_l;")
        await c.commit()
    finally:
        await c.close()


@test
async def insert_or_replace_becomes_upsert():
    """INSERT OR REPLACE → ON CONFLICT DO UPDATE (표 PK 자동 introspect). 컬럼 명시형·미명시형 둘 다."""
    c = _conn()
    if c is None:
        print("  (PG 없음 — 건너뜀)")
        return
    try:
        await c.executescript(
            "DROP TABLE IF EXISTS pgt_m; DROP TABLE IF EXISTS pgt_p;"
            "CREATE TABLE pgt_m(chat_id BIGINT, user_id BIGINT, points BIGINT DEFAULT 0, name TEXT, PRIMARY KEY(chat_id,user_id));"
            "CREATE TABLE pgt_p(id BIGINT PRIMARY KEY, a TEXT, b TEXT);")
        # ① 컬럼 명시형: 같은 PK 로 다시 넣으면 덮어써야(REPLACE 의미)
        await c.execute("INSERT OR REPLACE INTO pgt_m(chat_id,user_id,points,name) VALUES(?,?,?,?)", (-1, 7, 100, "가"))
        await c.execute("INSERT OR REPLACE INTO pgt_m(chat_id,user_id,points,name) VALUES(?,?,?,?)", (-1, 7, 555, "나"))
        await c.commit()
        row = (await c.execute_fetchall("SELECT points,name FROM pgt_m WHERE chat_id=? AND user_id=?", (-1, 7)))[0]
        assert row["points"] == 555 and row["name"] == "나", dict(zip(row.keys(), row))   # 덮어써짐
        n = (await c.execute_fetchall("SELECT count(*) AS n FROM pgt_m"))[0]["n"]
        assert n == 1, n   # 중복 row 안 생김
        # ② 컬럼 미명시형 (VALUES 만): executemany 도
        await c.executemany("INSERT OR REPLACE INTO pgt_p VALUES(?,?,?)", [(1, "x", "y"), (1, "x2", "y2")])
        await c.commit()
        p = (await c.execute_fetchall("SELECT a,b FROM pgt_p WHERE id=1"))[0]
        assert p["a"] == "x2" and p["b"] == "y2", dict(zip(p.keys(), p))
        assert (await c.execute_fetchall("SELECT count(*) AS n FROM pgt_p"))[0]["n"] == 1
        await c.executescript("DROP TABLE pgt_m; DROP TABLE pgt_p;")
        await c.commit()
    finally:
        await c.close()


@test
async def pg_trgm_and_pgvector():
    """FTS5 대체(pg_trgm 한글 두 글자)·sqlite-vec 대체(pgvector) 동작."""
    c = _conn()
    if c is None:
        print("  (PG 없음 — 건너뜀)")
        return
    try:
        try:
            await c.executescript("CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector;")
            await c.commit()
        except Exception as e:
            print(f"  (확장 못 켬 — 건너뜀: {e})")
            return
        await c.executescript("DROP TABLE IF EXISTS pgt_msg; CREATE TABLE pgt_msg(id BIGINT, body TEXT);")
        for i, b in enumerate(("원두 커피 맛있다", "커피 원두 추천", "날씨 좋다")):
            await c.execute("INSERT INTO pgt_msg(id,body) VALUES(?,?)", (i, b))
        await c.commit()
        hits = await c.execute_fetchall("SELECT body FROM pgt_msg WHERE body LIKE ?", ("%원두%",))
        assert len(hits) == 2, hits

        await c.executescript("DROP TABLE IF EXISTS pgt_v; CREATE TABLE pgt_v(id BIGINT, emb vector(3));")
        for i, v in enumerate(([1, 0, 0], [0.9, 0.1, 0], [0, 0, 1])):
            await c.execute("INSERT INTO pgt_v(id,emb) VALUES(?,?)", (i, str(v)))
        await c.commit()
        near = await c.execute_fetchall("SELECT id FROM pgt_v ORDER BY emb <=> ? LIMIT 2", (str([1, 0, 0]),))
        assert [n["id"] for n in near] == [0, 1], near
        await c.executescript("DROP TABLE pgt_msg; DROP TABLE pgt_v;")
        await c.commit()
    finally:
        await c.close()


@test
async def ddl_translation_rules():
    """스키마 DDL 변환 (순수 함수 — PG 없어도 검사)."""
    from sodam.pgbackend import ddl_for_pg
    out = ddl_for_pg("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(body)")
    assert out == "CREATE TABLE IF NOT EXISTS messages_fts (rowid BIGINT PRIMARY KEY, body TEXT)", out
    assert ddl_for_pg("CREATE VIRTUAL TABLE knowledge_fts USING fts5(body)") == \
        "CREATE TABLE knowledge_fts (rowid BIGINT PRIMARY KEY, body TEXT)"
    assert "COLLATE" not in ddl_for_pg("CREATE INDEX i ON users(username COLLATE NOCASE)")
    assert "IDENTITY" in ddl_for_pg("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)")


@test
async def full_sodam_schema_creates_on_pg():
    """진짜 소담 전체 스키마(db.SCHEMA + EXTRA_SCHEMA 81조각, ~170표)가 PG 에 통째로 생성되는지.
    db.py 가 open() 에서 하는 그대로 executescript 로 — 이게 되면 db.py 통합의 제일 큰 미지수가 풀림.
    PG_TEST=1 opt-in 일 때만 (서버 배포엔 접속 안 함)."""
    c = _conn()
    if c is None:
        print("  (PG 없음 — 건너뜀)")
        return
    try:
        import sodam.handlers  # noqa: F401  — 모든 모듈 로드 → register_schema 전부 실행
        from sodam import db as dbmod
        # 깨끗한 스키마(앞 테스트 잔재 제거): public 싹 비우고 확장 다시
        await c.executescript("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await c._run(lambda: __import__("psycopg") and c._pg.cursor().execute(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector"))
        await c.commit()
        await c.executescript(dbmod.SCHEMA)
        for extra in dbmod.EXTRA_SCHEMA:
            await c.executescript(extra)
        await c.commit()
        n = (await c.execute_fetchall(
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'"))[0]["n"]
        assert n >= 150, f"표가 너무 적음: {n}"   # 실측 ~170
        # FTS 표가 평범한 표로 만들어졌고 rowid 로 넣을 수 있나
        await c.execute("INSERT INTO messages_fts(rowid, body) VALUES(?, ?)", (1, "원두 커피"))
        await c.commit()
        got = (await c.execute_fetchall("SELECT body FROM messages_fts WHERE rowid=?", (1,)))[0]["body"]
        assert got == "원두 커피", got
    finally:
        await c.close()


@test
async def db_boots_on_pg_and_atomic_roundtrip():
    """db.py 자체가 DB_BACKEND=pg 로 켜지고(스키마+컬럼추가+FTS채우기) 쓰기·atomic·읽기가 도는지.
    SQLite 경로는 안 건드렸으니(조기 반환) 이게 통합의 첫 '진짜 부팅' 증거. PG_TEST=1 opt-in."""
    pre = _conn()
    if pre is None:
        print("  (PG 없음 — 건너뜀)")
        return
    dsn = os.getenv("PG_TEST_DSN") or "dbname=sodam_probe"
    # 깨끗한 스키마로 시작
    await pre.executescript("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await pre._run(lambda: pre._pg.cursor().execute(
        "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector"))
    await pre.commit()
    await pre.close()

    import sodam.handlers  # noqa: F401  — register_schema/register_columns 전부
    from sodam.db import DB
    os.environ["DB_BACKEND"] = "pg"
    os.environ["PG_DSN"] = dsn
    db = DB("unused-on-pg")
    try:
        assert db.backend == "pg"
        await db.open()   # ← 스키마 + _migrate_pg(컬럼) + _backfill_fts 전부 PG 에서
        await db._write("INSERT INTO members(chat_id,user_id,points) VALUES(?,?,?)", (-10, 7, 1000))
        # atomic: 포인트 옮기기 (db.py 의 SAVEPOINT 경로 그대로 PG 에서)
        def move(c):
            c.execute("UPDATE members SET points=points-? WHERE chat_id=? AND user_id=?", (300, -10, 7))
            c.execute("INSERT INTO members(chat_id,user_id,points) VALUES(?,?,?)", (-10, 8, 300))
        await db.atomic(move)
        a = (await db._one("SELECT points FROM members WHERE chat_id=? AND user_id=?", (-10, 7)))["points"]
        b = (await db._one("SELECT points FROM members WHERE chat_id=? AND user_id=?", (-10, 8)))["points"]
        assert a == 700 and b == 300, (a, b)
        # _migrate_pg 가 schedules 에 나중 컬럼(fmt 등)을 넣었나
        cols = await db._all("SELECT column_name FROM information_schema.columns WHERE table_name='schedules'")
        names = {r["column_name"] for r in cols}
        assert {"fmt", "rich", "buttons", "del_prev"} <= names, names
    finally:
        await db.close()
        os.environ.pop("DB_BACKEND", None)
        os.environ.pop("PG_DSN", None)


if __name__ == "__main__":
    import asyncio
    sys.exit(1 if asyncio.run(run_all()) else 0)
