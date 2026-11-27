"""🐘 PostgreSQL 백엔드 — db.py 가 SQLite(aiosqlite) 대신 쓸 수 있는 '껍데기'.

핵심 설계(docs/POSTGRES_MIGRATION.md): aiosqlite 는 sqlite3(동기)를 전용 스레드 하나에서 돌린다.
여기도 psycopg(동기)를 **전용 스레드 하나**(ThreadPoolExecutor max_workers=1)에서 돌려 같은 구조를 흉내낸다.
→ db.py 의 `self.conn.execute/execute_fetchall/commit/executescript/executemany` 와 144개 atomic() 콜백
(`self.conn._execute(run, self.conn._conn)` 안에서 raw 연결에 `c.execute(sql, params)`)이 **그대로** 산다.

안전 핵심:
  · psycopg 연결은 자기 스레드에서만 만지면 안전 → 모든 작업(lastrowid·rowcount·행 읽기 포함)을 그 스레드에서
    미리 끝내 _Cur 에 '값'으로만 담는다. await 가 끝난 뒤 메인 스레드가 _Cur 를 봐도 네트워크를 안 건드림.
  · PG 는 SQLite 와 달리 에러 하나가 트랜잭션 전체를 중단시킨다 → lastval() 등 실패할 수 있는 건 savepoint 로 격리.

아직 db.py 에 안 꽂음 — 이 모듈 + 테스트만. DB_BACKEND=pg 일 때만 import (psycopg 없어도 봇은 SQLite 로 돈다).
남은 수동 변환(SCHEMA 의 FTS5→pg_trgm·sqlite-vec→pgvector, INSERT OR REPLACE 표별, strftime 다양꼴)은 db.py 통합 때.
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


# ── SQL 번역: SQLite 방언 → PostgreSQL ──────────────────────────────────
def translate(sql: str) -> str:
    """흔한 SQLite 구문을 PG 로. 자동으로 되는 것만 — 나머지(복잡 strftime·OR REPLACE)는 호출부에서."""
    s = sql
    ignore = bool(re.search(r"\bOR\s+IGNORE\b", s, re.I))
    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", s, flags=re.I)
    # 스키마
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
               "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.I)
    s = re.sub(r"\bINTEGER\b", "BIGINT", s)          # SQLite INTEGER → PG BIGINT(64bit, telegram id 안전)
    # 시각 (흔한 꼴만; '%H'·'%Y-%m-%d' 등은 db.py 통합 때 수동)
    s = re.sub(r"strftime\(\s*'%s'\s*,\s*'now'\s*\)", "extract(epoch from now())::bigint", s, flags=re.I)
    # 자리표시자 ? → %s (psycopg). 먼저 '%' 를 '%%' 로 (LIKE '%x%' 등 리터럴 보존), 그 다음 ? → %s
    s = s.replace("%", "%%")
    s = re.sub(r"\?", "%s", s)
    if ignore:
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return s


_FTS5_RE = re.compile(
    r"CREATE\s+VIRTUAL\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(\w+)\s+USING\s+fts5\s*\(([^)]*)\)", re.I)


def ddl_for_pg(stmt: str) -> str:
    """스키마(DDL) 한 문장을 PG 로. translate() 가 못 하는 두 가지를 먼저 처리:
      · FTS5 가상표 → 평범한 표(rowid BIGINT PK + 칸 TEXT). `INSERT INTO *_fts(rowid, body)` 코드가 그대로 살고,
        실제 두 글자 검색은 pg_trgm GIN 으로(search.py 통합 때). 실측: 소담 전체 스키마가 이 변환으로 0 실패.
      · COLLATE NOCASE 제거 → 대소문자 무시는 질의에서 lower()/citext 로(통합 때). 색인 생성만 통과시키면 됨.
    """
    m = _FTS5_RE.search(stmt)
    if m:
        cols = ", ".join(c.strip() + " TEXT" for c in m.group(3).split(",") if c.strip())
        return f'CREATE TABLE {m.group(1) or ""}{m.group(2)} (rowid BIGINT PRIMARY KEY, {cols})'
    return translate(re.sub(r"\s+COLLATE\s+NOCASE", "", stmt, flags=re.I))


# ── 행: int 인덱스( row[0] )·키( row["col"] ) 둘 다 되게 (aiosqlite.Row 흉내) ──
class Row:
    __slots__ = ("_vals", "_cols")

    def __init__(self, vals: tuple, cols: tuple[str, ...]):
        self._vals = vals
        self._cols = cols

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return self._vals[self._cols.index(k)]

    def keys(self):
        return list(self._cols)

    def get(self, k, default=None):
        try:
            return self[k]
        except (ValueError, IndexError):
            return default

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


def _row_factory(cursor):
    cols = tuple(d.name for d in cursor.description) if cursor.description else ()

    def make(values):
        return Row(values, cols)
    return make


# ── 커서( c.execute 결과 ) — 전용 스레드에서 미리 뽑은 '값'만 담는다 (스레드 안전) ──
class _Cur:
    __slots__ = ("_rows", "_rowcount", "_last")

    def __init__(self, rows, rowcount, lastrowid):
        self._rows = rows
        self._rowcount = rowcount
        self._last = lastrowid

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return self._rowcount

    @property
    def lastrowid(self):
        return self._last


# ── 동기 연결 래퍼 (스레드 안에서만 씀. atomic 콜백이 받는 'c' = db.py 의 self.conn._conn) ──
class SyncConn:
    def __init__(self, pg):
        self._pg = pg

    def execute(self, sql: str, params: tuple = ()):
        is_insert = bool(re.match(r"\s*INSERT", sql, re.I))
        cur = self._pg.cursor()
        cur.row_factory = _row_factory
        cur.execute(translate(sql), tuple(params))
        rows = cur.fetchall() if cur.description else []
        rowcount = cur.rowcount
        last = None
        if is_insert:
            # lastrowid 흉내: 시퀀스 없는 표면 lastval() 이 에러 → savepoint 로 격리(트랜잭션 전체 오염 방지)
            try:
                with self._pg.transaction():
                    c2 = self._pg.cursor()
                    c2.execute("SELECT lastval()")
                    last = c2.fetchone()[0]
            except Exception:
                last = None
        return _Cur(rows, rowcount, last)

    def executemany(self, sql: str, rows):
        cur = self._pg.cursor()
        cur.executemany(translate(sql), [tuple(r) for r in rows])
        return _Cur([], cur.rowcount, None)

    def executescript(self, script: str):
        # 스키마 전용 경로 — FTS5 가상표·COLLATE NOCASE 까지 처리(ddl_for_pg). db.py 는 SCHEMA/EXTRA_SCHEMA 만 이걸 씀.
        for stmt in _split_statements(script):
            if stmt.strip():
                self._pg.cursor().execute(ddl_for_pg(stmt))

    def commit(self):
        self._pg.commit()

    def rollback(self):
        self._pg.rollback()


def _split_statements(script: str) -> list[str]:
    """세미콜론 분리 (따옴표 안 ; 는 보존)."""
    out, buf, q = [], [], None
    for ch in script:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            buf.append(ch)
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


# ── 비동기 연결 (db.py 의 self.conn 자리. 전용 스레드 1개에 고정) ──────────
class PgConnection:
    def __init__(self, dsn: str):
        import psycopg
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pgdb")
        self._dsn = dsn
        # 연결도 그 스레드에서 (psycopg 연결은 만든 스레드에서만 만져야 안전)
        self._pg = self._pool.submit(lambda: psycopg.connect(dsn, autocommit=False)).result()
        self._sync = SyncConn(self._pg)
        self.row_factory = None        # db.py 가 aiosqlite.Row 로 세팅하지만 PG 는 _row_factory 로 이미 처리
        self._thread = None            # db.stop_sync 가 getattr(conn, "_thread", None) 로 봄 (None → join 안 함)

    async def _run(self, fn: Callable[[], Any]):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, fn)

    # ── aiosqlite 내부 흉내: db.py 의 atomic() 이 self.conn._execute(run, self.conn._conn) 로 씀 ──
    @property
    def _conn(self):
        return self._sync

    async def _execute(self, fn: Callable[..., Any], *args):
        """콜백 fn(*args) 를 전용 스레드에서 실행 (aiosqlite.Connection._execute 와 같은 역할)."""
        return await self._run(lambda: fn(*args))

    # ── 공개 API (db.py 의 _all/_one/_write·open 이 씀) ──
    async def execute_fetchall(self, sql: str, params: tuple = ()):
        return await self._run(lambda: self._sync.execute(sql, params).fetchall())

    async def execute(self, sql: str, params: tuple = ()):
        return await self._run(lambda: self._sync.execute(sql, params))

    async def executemany(self, sql: str, rows):
        rows = list(rows)
        return await self._run(lambda: self._sync.executemany(sql, rows))

    async def executescript(self, script: str):
        return await self._run(lambda: self._sync.executescript(script))

    async def commit(self):
        return await self._run(self._pg.commit)

    async def rollback(self):
        return await self._run(self._pg.rollback)

    async def atomic(self, fn: Callable[[SyncConn], Any]):
        """db.atomic 의 PG 판(테스트·직접호출용). db.py 는 자기 atomic() 을 _execute/_conn 로 돌리므로
        이쪽이 아니라 SAVEPOINT 경로를 탄다 — 둘 다 '전부/전무' 로 같다."""
        def run():
            try:
                r = fn(self._sync)
                self._pg.commit()
                return r
            except BaseException:
                self._pg.rollback()
                raise
        return await self._run(run)

    async def close(self):
        await self._run(self._pg.close)
        self._pool.shutdown(wait=True)

    def stop(self):
        """이벤트 루프가 끝난 뒤(Ctrl+C) 쓰는 동기 종료. db.stop_sync 가 부름."""
        try:
            self._pg.close()
        except Exception:
            pass
        self._pool.shutdown(wait=False)

    # sqlite-vec 전용 호출 — PG 는 확장이 서버에 이미 설치돼 no-op (pgvector/pg_trgm 은 CREATE EXTENSION 로)
    async def enable_load_extension(self, on: bool):
        pass

    async def load_extension(self, path: str):
        pass
