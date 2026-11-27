# 🐘 PostgreSQL 이관 설계 (SQLite → PostgreSQL)

소담을 SQLite(aiosqlite)에서 PostgreSQL 로 **데이터 안 날리고** 단계로 옮기는 설계.
오너 결정 2026-11-27: 클론 사업 확장 대비(여러 서버·본사 전체 집계·동시 쓰기). 지금 부하는 낮음(CPU~10%)이라 **급하지 않음 — 그래서 더 신중하게, 서두르지 않는다.**

## ⚠️ 왜 "설치하고 ㄱㄱ"가 아닌가
- SQLite 전용 코드가 280개 파일·85개 표에 흩어짐: `?` 자리표시자, `INSERT OR IGNORE/REPLACE`, `strftime`, `AUTOINCREMENT`, `db.atomic`(SAVEPOINT), FTS5(검색), sqlite-vec(벡터).
- FTS5 는 PG 직접 대응 없음 → `pg_trgm`(GIN) 또는 tsvector 로 재작성. sqlite-vec → `pgvector`.
- 라이브 데이터: 본체 + 클론 11개 = 약 1.6GB. 무중단·무손실 전환 필요.
- 업계 가이드 공통: "이유 없으면 하지 마라, 한 서버면 WAL SQLite 로 충분." → **한 세션에 몰아치면 데이터 날림.**

## 🔒 안전 6원칙 (어기지 말 것)
1. **기존 SQLite 절대 안 지움.** 전환 뒤에도 백업으로 남긴다(최소 수주).
2. **클론 하나(제일 작은 것)로 먼저 완전 검증 → 본체는 맨 마지막.**
3. **환경변수 토글** `DB_BACKEND=sqlite|pg` 로 1초 롤백.
4. 이관 전/후 **표마다 행수·합계(포인트 등) 대조**. 안 맞으면 중단·롤백.
5. **돈 표(members.points·casino_*·subscriptions·ledger)는 이관 중 쓰기 잠깐 멈추고(점검 모드) 복사 → 대조 → 재개.**
6. 각 단계 끝에 **실측**(며칠 돌려보고) 다음 단계. 한 번에 두 단계 금지.

## 🏗️ 아키텍처
- **PG 1 인스턴스, 봇마다 별도 DATABASE** (지금 봇마다 별도 .db 파일 격리를 그대로): `sodam_main`, `sodam_<slug>`(클론). 화이트라벨 격리 유지.
- 접속: **로컬 유닉스 소켓 + peer 인증**(OS `sodam` 유저 → PG `sodam` 역할, 비밀번호 없음). `listen_addresses=localhost`(외부 차단).
- 코드: `db.py` 에 **백엔드 추상화** 한 겹. 쿼리는 공통 방언으로 쓰고 백엔드가 번역. `DB_BACKEND` 로 선택.
- 라이브러리: `asyncpg`(빠름) 또는 `psycopg3`(async). 풀링은 `asyncpg.create_pool`.

## 🔧 SQLite → PG 변환 (추상화 계층이 처리)
| SQLite | PostgreSQL |
|---|---|
| `?` 자리표시자 | `$1,$2…` |
| `INSERT OR IGNORE` | `INSERT … ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE` | `INSERT … ON CONFLICT(key) DO UPDATE` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED ALWAYS AS IDENTITY` |
| `strftime('%s',…)`, `strftime('%H',ts,'unixepoch')` | `to_timestamp`, `extract(epoch…)`, `to_char` |
| `db.atomic`(SAVEPOINT) | `async with conn.transaction()` |
| FTS5 (`messages_fts`, `knowledge_fts`) | `pg_trgm` GIN 인덱스(두 글자 겹침 대응) 또는 tsvector |
| sqlite-vec (`vec0`, cosine) | `pgvector`(`vector(256)`, `<=>` cosine, HNSW 인덱스) |
| `PRAGMA`, `busy_timeout`, WAL | PG 는 MVCC(동시 쓰기 됨) → busy_timeout 불필요 |
| 불리언(0/1 정수) | `boolean` 캐스팅 규칙 필요(pgloader 기본은 정수로 둠) |

## 📋 단계
- **0 (완료)**: 서버에 PG16 + pgvector + pg_trgm 설치, sodam peer 인증, localhost 전용, 시험 DB `sodam_probe` 로 확장 검증. 라이브 0 접촉.
- **1**: `db.py` 백엔드 추상화 작성(SQLite·PG 둘 다). `DB_BACKEND=pg` 로 **전체 시험(tests/run_all.py) 통과**. FTS5→pg_trgm, sqlite-vec→pgvector 재작성. 아직 라이브 0 접촉.
- **2**: 제일 작은 클론(`ttingone2bot` 6.4MB 등) → `pgloader` 로 데이터 복사 → 표별 행수·합계 대조 → 그 클론만 `DB_BACKEND=pg` 로 며칠 운영. 문제 시 env 되돌리면 SQLite 로 즉시 복귀.
- **3**: 클론들 차례로(작은 것부터). 돈 표는 원칙 5(점검 모드) 적용.
- **4**: **본체 맨 마지막.** 저부하 시간대, 백업·대조·롤백 준비 완료 후.
- **5**: 수주 안정 뒤 SQLite 는 백업 보관만. 이후 멀티서버·본사 전체 집계·Redis 공유 캐시 확장 가능.

## ✅ 검증·롤백
- 이관 직후: `SELECT count(*)` 표별 대조 + `SUM(points)`(members)·`SUM(delta)`(casino_ledger)·구독 만료일 대조.
- 롤백: `DB_BACKEND=sqlite` 로 바꾸고 재시작 → 원래 SQLite 그대로(이관 중 SQLite 는 안 지우고 안 멈췄으니 최신). 돈 표만 점검 모드라 그 시간 쓰기 없음.
- FTS/벡터: 이관 후 재색인(tick 이 자동) — 검색은 잠깐 단어검색만 될 수 있음(기능 안 죽음).

## 🧰 도구·주의
- `pgloader`: SQLite 파일 직접 읽어 스키마·타입·데이터·인덱스·시퀀스 한 번에. **불리언 캐스트·깨진 외래키·시퀀스 리셋**이 흔한 실패 → 이관 후 반드시 대조.
- `asyncpg` 풀: 봇당 작은 풀(min 1, max ~5). 음성 워커·클론은 자기 DB 풀.
- 디스크: PG 는 SQLite 대비 2~3배. 지금 11GB 여유 → 클론 하나씩 옮기며 감시(한꺼번에 X).
- 백업: `backup.sh` 에 PG `pg_dump` 추가(SQLite 백업과 병행).

## ✅ 아키텍처 검증 (PoC, `tools/pg_poc.py`, 2026-11-27)
**결론: 동기 psycopg + SQL 번역 껍데기로 가면 144개 atomic() 콜백을 안 뜯고 PG 사용 가능.** 로컬 PG 로 증명:
- ✅ 스키마(AUTOINCREMENT→IDENTITY) · INSERT OR IGNORE→ON CONFLICT DO NOTHING · **atomic 콜백(sync c.execute) 무수정 동작**
- ✅ **pg_trgm 한글 두 글자 검색**(FTS5 대체) · **pgvector cosine**(sqlite-vec 대체)
- 핵심: aiosqlite 가 sqlite3(동기)를 백그라운드 스레드에서 돌리는 구조 그대로, **psycopg(동기)를 같은 자리에** + `self.conn` 을 sqlite3 흉내 껍데기로. 그래서 `db.atomic(fn)` 콜백·`conn.execute` 직접 호출이 다 그대로 산다.

### 작업량 (실측 2026-11-27)
atomic() 콜백 **144개**(58파일) · 직접 conn.execute 28 · INSERT OR IGNORE/REPLACE **85** · strftime **116** · AUTOINCREMENT **55**.

### 번역 껍데기가 자동 처리 / 수동 필요
- 자동: `?`→`%s`, INSERT OR IGNORE→ON CONFLICT DO NOTHING, AUTOINCREMENT→IDENTITY, PRAGMA 무시, lastrowid→lastval(savepoint 격리), `strftime('%s','now')`.
- **수동(이게 남은 큰 일)**: ① `INSERT OR REPLACE`(PK·전체 컬럼 알아야 ON CONFLICT DO UPDATE — 표마다) ② `strftime('%H'/'%Y-%m-%d'/…)` 다양한 꼴 116곳 → PG `to_char`/`extract` 매핑 ③ FTS5 색인/쿼리를 pg_trgm GIN 으로 교체(search.py·semsearch.py) ④ sqlite-vec vec0 → pgvector 표·HNSW(semsearch.py) ⑤ **PG 트랜잭션 중단 의미**(에러 하나가 트랜잭션 전체를 막음 — best-effort insert 하는 콜백 재검토).

## 현황
- **0단계 완료**(PG16+pgvector 서버·로컬 설치) · **아키텍처 PoC 검증 완료**(접근 확정: 동기 psycopg+번역 껍데기).
- **1단계 진행 중 — 조각① 완료**: `sodam/pgbackend.py`(백엔드 껍데기 모듈) + `tests/test_pgbackend.py`(실제 로컬 PG 로 6개, 뮤테이션 3개). `requirements.txt` 에 `psycopg[binary]`(선택). **db.py 에 아직 안 꽂음 — 라이브 0 접촉.**
  - 모듈 핵심: `PgConnection` 이 aiosqlite 흉내 — `execute/execute_fetchall/executemany/executescript/commit` + **내부 `_execute(fn, *args)`·`_conn`(SyncConn)** 까지 흉내 내서 **db.py 의 `atomic()`(`self.conn._execute(run, self.conn._conn)` + SAVEPOINT) 을 안 고치고** 통과. psycopg 연결은 전용 스레드 1개에 고정, `lastrowid`·`rowcount`·행은 **그 스레드에서 미리 뽑아 _Cur 에 값으로** 담음(메인 스레드가 psycopg 소켓을 안 만지게 — 스레드 안전).
  - 시험 통과(로컬 PG): 번역 규칙 · Row int/키 접근 · INSERT OR IGNORE→ON CONFLICT · **db.py 식 atomic(SAVEPOINT 전부/전무)** · pg_trgm 한글 검색 · pgvector cosine.
- **조각② 완료 — 스키마 전체가 PG 에 뜬다(실측)**: `pgbackend.ddl_for_pg()` + `executescript` 가 **소담 진짜 전체 스키마**(db.SCHEMA + EXTRA_SCHEMA 81조각, 232문·~170표)를 **db.py 의 open() 경로 그대로** PG 에 **0 실패**로 생성(시험 `full_sodam_schema_creates_on_pg`, 뮤테이션 2개). 필요한 변환은 셋뿐: `translate()`(AUTOINCREMENT→IDENTITY·INTEGER→BIGINT 등) + **FTS5 가상표→평범표**(rowid BIGINT PK+TEXT, `INSERT INTO *_fts(rowid,body)` 코드 그대로 삼) + **COLLATE NOCASE 제거**. 전제 밝힘: 트리거·vec0·CHECK·FK·WITHOUT ROWID 가 스키마에 **하나도 없어서** 깔끔(실측). **db.py 미통합·라이브 0 접촉.**
- **조각③ 완료 — db.py 가 PG 로 진짜 부팅됨(실측)**: `DB.__init__` 이 `DB_BACKEND`(기본 sqlite)를 읽고, `open()` 이 pg 면 `_open_pg()`(PgConnection 연결 → SCHEMA → `_migrate_pg`(information_schema 로 컬럼 추가) → `_backfill_fts`)로 조기 분기. **SQLite 경로는 조기 반환 뒤 한 글자도 안 바뀜**(전체 시험 통과로 확인 — away 7개는 알려진 시계 flake, 단독 15/15). 공유 `_wanted_columns()` 로 컬럼 목록 한 곳 관리(SQLite·PG 공용). 이식성 수정 하나: `_backfill_fts` 의 `AND 1` → `AND 1=1`(PG 는 AND 뒤 불리언 필요, SQLite 도 OK). 시험 `db_boots_on_pg_and_atomic_roundtrip`(DB_BACKEND=pg 로 열고 쓰기·atomic 포인트이동·읽기·컬럼 추가 확인) + 뮤테이션 2개.
  - **dsn**: `PG_DSN` 또는 `dbname=${PG_DB:-sodam_main}`. 의미검색(pgvector)은 아직 꺼둠(`self.vec=False`, 단어검색은 됨).
- **조각④ 완료 — INSERT OR REPLACE 자동 upsert(실측)**: `SyncConn._replace_to_upsert` 가 `INSERT OR REPLACE INTO t(cols) VALUES(…)` 를 **표의 기본키를 PG 카탈로그(pg_index)에서 뽑아** `INSERT … ON CONFLICT (pk…) DO UPDATE SET 비-pk=EXCLUDED.…`(비-pk 없으면 DO NOTHING)로 자동 변환. **표마다 하드코딩 X** — 32개 표(+미래 표)·컬럼 명시형/미명시형·execute/executemany 다 커버(PK·전체컬럼 캐시). 시험 `insert_or_replace_becomes_upsert`(덮어쓰기+중복row 없음, 뮤테이션 2개).
- **조각⑤ 완료 — SQL strftime(실측: 116곳이 아니라 2곳)**: 전수조사하니 strftime 114개 중 **112개가 파이썬**(`time.strftime`/`datetime` — 번역 불필요), **SQL 안 strftime 은 딱 2곳**(db.py count_filter 한국시 집계·diary 바쁜시간). db.py 에 방언 헬퍼 `hour_sql`·`hour_offset_sql`(`_backend()` 가 env 매번 읽어 분기) → SQLite 는 기존 문자열 **그대로**, PG 는 `extract(hour from to_timestamp(...) at time zone 'UTC')`(세션 tz 무관). 시험 `kst_hour_sql_computes_on_pg`(비-UTC 세션에서도 한국시 9시, 뮤테이션 1개) + count_filter 쓰는 모듈(chat_min_chars·chatrank·rankboost·diary) SQLite 회귀 0.
- **조각⑥ 완료 — 전문검색이 PG 에서 됨(실측: 한국어 2글자)**: FTS5 쿼리 2곳(search_messages·knowledge_candidates)을 백엔드별 `_fts_where` 로. SQLite 는 `*_fts MATCH ?`(기존 문자열 그대로 — 회귀 0, 검색 모듈 green), PG 는 평범표 body(bigram 공백연결)에 `body LIKE '%조각%' OR …`(bm25 없어 최신순 후보 → 관련도는 기존 파이썬 재정렬이 담당). `_open_pg` 가 `pg_trgm` 확장 + 두 fts 표에 GIN(gin_trgm_ops) 색인(둘 다 권한 없으면 색인 없이 LIKE 로 동작). **AUTOINCREMENT→`GENERATED BY DEFAULT AS IDENTITY`**(ALWAYS 아님 — clone_migrate 가 id 보존 복사하므로 명시 id 삽입 허용, 대량복사 뒤 시퀀스 setval 은 이관 단계). 시험 `fts_search_works_on_pg`(‘원두’ 검색이 해당 글만, 2낱말도) + 뮤테이션 2개.
- **조각⑦ 완료 — 의미검색 sqlite-vec→pgvector(실측)**: semsearch.py 3곳 백엔드별 — `_table`(PG = `vector({DIM})` 평범표 + HNSW cosine 색인), `_veclit`(PG = '[...]' 문자열, pgvector 암묵 캐스트 / SQLite = float32 blob), KNN 쿼리(PG = `embedding <=> ? … ORDER BY … LIMIT` cosine / SQLite = vec0 MATCH). `_open_pg` 가 `vector` 확장 켜고 `self.vec=True`. `INSERT OR REPLACE INTO msg_vec` 는 조각④ upsert 가 자동 처리. 시험 `pgvector_semsearch_path_on_pg`(_table·_veclit·upsert·KNN 거리순, 뮤테이션 1개 — _veclit/_table). 참고: semsearch.search()의 쿼리 선택 분기 한 줄은 조각⑧(하네스 PG화)에서 test_semsearch 가 PG 로 돌 때 완전 커버.
  - **이로써 SQL 방언 변환은 전부 끝**(스키마·atomic·OR IGNORE/REPLACE·strftime·FTS5·pgvector). SQLite 경로는 매 조각 회귀 0.
- **조각⑧ 진행 중 — PG 테스트 하네스 + 방언 메우기(실측)**: `tests/fakes.py` 의 `make_db()` 가 `DB_BACKEND=pg` 면 **테스트마다 고유 스키마**(search_path 격리, close 때 DROP)로 PG DB 를 만듦 → 실제 소담 코드가 PG 에서 돌아가는지 모듈별로 검증. `DB(path, pg_dsn=)` 로 테스트가 격리 dsn 전달, `_migrate_pg` 는 `current_schema()`. 돌려 보며 나온 **방언 차이를 translate 에 체계적으로**(전부 PG 전용, SQLite 경로 무관):
  - `char(N)`→`chr(N)` · `PRAGMA …`→no-op · 스칼라 `MAX/MIN(a,b)`→`GREATEST/LEAST` · `col=? COLLATE NOCASE`→`LOWER(col)=LOWER(?)` · `GROUP BY` 엄격(top_chatters 에 컬럼 추가) · `ON CONFLICT DO UPDATE SET col=col±…` 기존행을 표이름으로 수식(points=members.points+…, 돈 카운터 12곳 자동).
  - **PG 통과 확인**: chat_min_chars 8/8 · budget 핵심 · memory_quality 4/4 · name_key(트리거 주입 제외). translate 규칙은 뮤테이션 보호(translate_rules).
  - **남은 것(grind·2차)**: ① **테스트 전용 sqlite 문법**(`CREATE TRIGGER … WHEN NEW.…` 로 실패 흉내, `usage_report.py` 등 .db 직접 읽는 도구) 쓰는 테스트는 PG 에선 skip/적응 필요 ② 모듈별 자잘한 방언 계속(group_concat→string_agg 등 나오면) ③ **⚠️ PG 쿼리 지연**: 이 dev 컨테이너선 게임 명령 1회가 ~1s(쿼리 수십 개 × 스레드홉+소켓) — 200회 스트레스 테스트가 분 단위라 느림. **실서버 VPS(로컬 소켓·튜닝) 지연 실측 필수**, 필요하면 핫패스 쿼리 묶기.
- 다음: 조각⑧ 마저(핵심 모듈 PG green 넓히기) → **2단계**: 제일 작은 클론부터 `pgloader` 로 데이터 복사·대조·며칠 운영 → 본체 맨 마지막. **코드만(1단계), 라이브 0 접촉.**
