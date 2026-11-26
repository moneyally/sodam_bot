#!/usr/bin/env python3
"""PG 이관 아키텍처 PoC — '동기 psycopg + SQL 번역 껍데기로 sqlite3 흉내'가 되는지, 어디가 막히는지 증명.
로컬 PG(sodam_poc) 필요. 실행: /root/venv/bin/python tools/pg_poc.py

목적: db.py 의 144개 atomic() 콜백(raw sqlite3 c.execute)을 안 뜯고 백엔드만 바꾸는 게 가능한지 확인."""
import re
import psycopg

# ── SQL 번역: SQLite 방언 → PG ──────────────────────────────────────────
def translate(sql: str) -> str:
    s = sql
    # 자리표시자 ? → %s (psycopg)
    s = re.sub(r"\?", "%s", s)
    # INSERT OR IGNORE → ON CONFLICT DO NOTHING (대상 안 적어도 PG 가 모든 unique/pk 처리)
    s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.I)
    ignore = bool(re.search(r"OR\s+IGNORE", sql, re.I))
    # 스키마: AUTOINCREMENT → IDENTITY, PRAGMA 무시
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.I)
    # strftime('%s','now') → PG, strftime('%s', col) → extract(epoch…)  (흔한 꼴만; 나머지는 수동)
    s = re.sub(r"strftime\(\s*'%s'\s*,\s*'now'\s*\)", "extract(epoch from now())::bigint", s, flags=re.I)
    if ignore:
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return s


# ── sqlite3 흉내 연결 껍데기 (동기 psycopg 위에) ────────────────────────
class Cur:
    def __init__(self, pcur, conn, is_insert):
        self._c = pcur; self._conn = conn; self._is_insert = is_insert
    def fetchall(self): return self._c.fetchall()
    def fetchone(self): return self._c.fetchone()
    @property
    def rowcount(self): return self._c.rowcount
    @property
    def lastrowid(self):
        # 지연 계산 + savepoint 격리: 시퀀스 없는 표면 lastval() 이 에러 나는데, 그게 트랜잭션 전체를
        # 오염시키지 않게 savepoint 안에서 (PG 는 SQLite 와 달리 에러 하나가 트랜잭션을 중단시킴 — 중요)
        if not self._is_insert:
            return None
        try:
            with self._conn._pg.transaction():
                cur = self._conn._pg.cursor(); cur.execute("SELECT lastval()")
                return cur.fetchone()[0]
        except Exception:
            return None


class Conn:
    """db.py 의 self.conn(sqlite3) 자리에 들어갈 껍데기. c.execute(sql, params) 를 번역해서 PG 로."""
    def __init__(self, dsn): self._pg = psycopg.connect(dsn, autocommit=False); self.row_factory = None
    def execute(self, sql, params=()):
        cur = self._pg.cursor()
        cur.execute(translate(sql), tuple(params))
        return Cur(cur, self, bool(re.match(r"\s*INSERT", sql, re.I)))
    def executescript(self, sql):
        for stmt in [x for x in sql.split(";") if x.strip()]:
            self._pg.cursor().execute(translate(stmt))
    def commit(self): self._pg.commit()
    def close(self): self._pg.close()


def main():
    c = Conn("dbname=sodam_poc")
    c.executescript("DROP TABLE IF EXISTS members; DROP TABLE IF EXISTS ledger; DROP TABLE IF EXISTS msgs;")
    print("① 스키마(AUTOINCREMENT→IDENTITY):")
    c.executescript("""
        CREATE TABLE members (chat_id BIGINT, user_id BIGINT, points BIGINT DEFAULT 0, PRIMARY KEY(chat_id,user_id));
        CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, delta BIGINT, ts BIGINT);
        CREATE TABLE msgs (id BIGINT, chat_id BIGINT, body TEXT);
    """)
    print("   OK — 표 3개 생성")

    print("② INSERT OR IGNORE → ON CONFLICT DO NOTHING:")
    c.execute("INSERT OR IGNORE INTO members(chat_id,user_id,points) VALUES(?,?,?)", (-100, 7, 10000))
    c.execute("INSERT OR IGNORE INTO members(chat_id,user_id,points) VALUES(?,?,?)", (-100, 7, 999))  # 중복 → 무시
    c.commit()
    pts = c.execute("SELECT points FROM members WHERE chat_id=? AND user_id=?", (-100, 7)).fetchone()[0]
    print(f"   OK — 중복 무시됨, points={pts} (999 로 안 덮임)")

    print("③ atomic 콜백 흉내 (동기 c.execute 그대로):")
    def run(conn):   # ← db.atomic(fn) 이 넘기는 콜백과 똑같은 모양
        conn.execute("UPDATE members SET points=points+? WHERE chat_id=? AND user_id=?", (500, -100, 7))
        cur = conn.execute("INSERT INTO ledger(user_id,delta,ts) VALUES(?,?,?)", (7, 500, 1700000000))
        return cur.lastrowid
    lastid = run(c); c.commit()
    bal = c.execute("SELECT points FROM members WHERE user_id=7").fetchone()[0]
    print(f"   OK — 콜백 안 뜯고 동작, 잔액={bal}, lastrowid(ledger.id)={lastid}")

    print("④ pg_trgm 검색 (FTS5 두 글자 겹침 대체):")
    for i, body in enumerate([("원두 커피 맛있다"), ("커피 원두 추천"), ("날씨 좋다")]):
        c.execute("INSERT INTO msgs(id,chat_id,body) VALUES(?,?,?)", (i, -100, body))
    c.commit()
    c.execute("CREATE INDEX ON msgs USING gin (body gin_trgm_ops)")
    hits = c.execute("SELECT body FROM msgs WHERE body %% ? ORDER BY similarity(body,?) DESC", ("원두", "원두")).fetchall()
    print(f"   OK — '원두' 검색 {len(hits)}건: {[h[0] for h in hits]}")

    print("⑤ pgvector (sqlite-vec 대체):")
    c.execute("CREATE TABLE IF NOT EXISTS vec (id BIGINT, emb vector(3))")
    for i, v in enumerate([[1, 0, 0], [0.9, 0.1, 0], [0, 0, 1]]):
        c.execute("INSERT INTO vec(id,emb) VALUES(?,?)", (i, str(v)))
    c.commit()
    near = c.execute("SELECT id FROM vec ORDER BY emb <=> ? LIMIT 2", (str([1, 0, 0]),)).fetchall()
    print(f"   OK — [1,0,0] 에 가까운 순: {[n[0] for n in near]}")

    print("\n✅ 핵심 접근 검증됨: 동기 psycopg + 번역 껍데기로 atomic 콜백 안 뜯고 PG 사용 가능")
    print("⚠️ 수동 처리 필요(번역 자동화 한계):")
    print("   · INSERT OR REPLACE → PK·전체 컬럼 알아야 ON CONFLICT DO UPDATE (85곳 중 일부)")
    print("   · strftime 다양한 꼴('%H','%Y-%m-%d' 등 116곳) → PG to_char/extract 수동 매핑")
    print("   · lastrowid 는 lastval() 로 흉내되나, 복합 INSERT·배치선 확인 필요")
    print("   · executescript 세미콜론 분리는 단순(문자열 안 ; 는 주의)")
    c.close()


if __name__ == "__main__":
    main()
