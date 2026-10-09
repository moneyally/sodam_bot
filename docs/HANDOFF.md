# 🤝 인수인계 (HANDOFF) — 새 세션은 CLAUDE.md 다음에 이것부터

마지막 갱신: 2026-10-09 18:40 KST (뮤직봇 3차: 겹쳐 넘기기·크기 맞추기·다시 열리면 이어 틀기·우회 길 2개·끊김 감시).
**규칙: 큰 작업이 끝날 때마다 이 파일 맨 위 '지금 상태'와 '할 일'을 고친다.** 끝난 일은 '최근 한 일'로 내리고, 1주 넘은 건 지운다
(자세한 설계는 CLAUDE.md 각 단락에 이미 있음 — 여기엔 '어디까지 했고 다음에 뭘 할지'만).

---

## 0. 새 세션 시작하면 바로 할 것 (5분)
1. `CLAUDE.md` 읽기 (말하는 법: **한국어 반말·짧게·한 것만 했다고**, 영어 문장 절대 X).
2. 이 파일 '지금 상태'·'할 일' 읽기.
3. 서버 버전 확인: `python3 .claude/skills/sodam-deploy/scripts/wait_deploy.py <커밋> --timeout 60`
   또는 `python3 tools/ssh_ws.py 'cat /opt/sodam/VERSION; cat /opt/sodam/data/update.status'`.
4. 작업 브랜치: `claude/button-panels` (머지 끝나면 `git fetch origin main && git checkout -B claude/button-panels origin/main && git push -f -u origin claude/button-panels`).
5. 테스트 환경: `python3 -m venv ~/venv && ~/venv/bin/pip install -r requirements.txt` (컨테이너 새로 뜨면). 전체 테스트 `~/venv/bin/python tests/run_all.py --jobs 2` (~6분).

## 1-0. 뮤직봇 (2026-10-09 저녁)
- 3차 9개 중 8개 구현 (CLAUDE.md 🎵 '3차' 단락). 0691204 를 18:26 에 `sodam-update --force` 로 반영 시작 (음성채팅이 닫혔는데 옛 DJ 가
  막힌 음원을 받느라 '노래 중'으로 남아 미뤄졌음 — 그 버그도 이번에 고침, `Player._or_stop`).
- **확인할 것**: ① 실제 노래 틀 때 `노래 끝 … 겹쳐 넘김 N · 바로 이어 붙임 N` 로그, music_sessions.stats 의 jitter·late ② 끊김 감시 오너 알림이
  너무 잦지 않은지 ③ data/warp_setup.status 의 두 길 IP 가 다른지 (같으면 둘째 길은 '프로그램이 죽었을 때 대비'일 뿐)
  ④ 받기 자식 기록 '[받기]' 로 우회 길이 왜 막히는지 (17:35~18:09 미리 받기가 계속 '막힘' — 그때 WARP IP 104.28.193.116)
- 서버 시험 시간: 코드가 바뀌면 여전히 ~20분 (로컬 9분). 느린 모듈: voice 180초·fix_ops 127초(update.sh 를 진짜로 돌리는 시험들)·sticker_parts 108초.
  서버 로그 'tests ok (느린 것 …)' 로 다음에 다듬기. 8번 서버 키우기는 오너 결정 (설명만 함).

## 1. 지금 상태 (2026-10-06 16:00)
- **#100 반영 확인 (5dc7023, 14:30)**: 테스트 2개 동시 첫 배포 = 서버 테스트 **16분** (14:14:13 → 14:30:20, 예전 25분). 목표 10분은 아직.
- '못 해요' 검사 PR 올림 (아래 3절) — 반영되면 `diag why` 의 refuse 건수로 효과 확인.

- 서버 버전: **ff92ad5** (#99) 확인. **5dc7023 (#100)** 은 14:14 부터 서버 테스트 중 — 새 방식(테스트 2개 동시) 첫 배포.
  → 새 세션 첫 일: 반영 확인 + **배포 걸린 시간 측정** (`journalctl -u sodam-autoupdate` 에서 'tests…' → 'tests ok' 시각 차이. 예전 ~25분, 목표 ~10분).
- 서버 .env: OPENAI_MODEL=gpt-6-sol · AGENT_LIGHT_MODEL=gpt-6-luna · guard gpt-5.4-mini (백업 /opt/sodam/.env.bak-before-gpt6).
- 베베방 말투 = 자유분방(free) (DB 직접 씀, mod_log actor 0).
- 배포 = main 머지 → 서버 타이머 **2분**마다 확인 → 전체 테스트(2개 동시, 1.5코어) 통과해야 재시작.

## 2. 할 일 (우선순위)
0. **내일: 오늘 바꾼 것 효과를 서버 agent_runs 로 원화 보고** — refuse 건수 · 길별(light/heavy/banter) 비용·시간 · terminal·parallel 이벤트 수.
   스티커: sticker_items·sticker_packs 줄 수(팩 버튼 실제 사용) · read_text 로 막힌 것(sticker_log outcome=fail).
   다음 스티커 단계: '내가 올린 스티커' 자동 붙이기(medialog.recent) · mediaintent '스티커' 말 → make_sticker (21:30 영상으로 샌 사례) · AI 도구로 '내 팩에 넣어줘'.
1. **#100 반영 확인 + 배포 시간 측정** (위). 오너에게 숫자로 보고.
2. **캐시·요금 재측정** (저녁에 실행 수십 번 쌓인 뒤). 기준선:
   | 구간 | 실행 | 적중 | 실행당 |
   |---|---|---|---|
   | 10-05 낮 | 112 | 61% | $0.017 |
   | 10-06 0시~13:25 | 26 | 52% | $0.032 |
   | 13:25~ (#97 지점 3개 + #98 핵심 목록) | 9 | 71% (heavy 79%) | $0.030 (차트·움프 같은 비싼 일 많았음) |
   쿼리(ssh_ws): `sqlite3 -readonly data/sodam.db` 에서 agent_runs 의 tok_in·tok_cached·usd_micro 를 구간별 합,
   `events LIKE '%find_tools%'` = 중간에 도구 불러온 실행(캐시 깨짐 원인). 14일 불러온 도구:
   `SELECT json_extract(e.value,'$.names'), COUNT(*) FROM agent_runs, json_each(events) e WHERE json_extract(e.value,'$.e')='find_tools' ...`
   → 자주 불리는 게 있으면 tools.CORE_TOOLS 에 추가 (#100 에서 owner_server_status 추가함).
3. **오너 실사용 확인 요청 중** (오너가 해보면 DB 확인):
   - 레시피 2단계 (예약 작업 skill=code: '매일 9시에 어제 시간대별 채팅 차트 올려줘' 같은 것) → schedules·ops_events 확인.
   - 바카라 회차판 `!회차 1000 플` → casino_results·사진 수정 4번·그룹 제한 걸림 여부.
   - 오너 1:1 '업데이트 보고' → 최근 변경 목록 나오는지 + find_tools 안 불렀는지(캐시).
4. **배포 더 빠르게 (오너에게 제안만 함, 아직 안 함)**: C. 스티커·움프 테스트(합 ~6분)를 그 코드가 바뀐 배포에서만 + 밤에 전체 1번 /
   무거운 렌더 테스트를 작은 그림으로.
5. **선택 제안 (오너 결정 대기)**: run_code 를 오너·관리자 1:1 에서 방 이름으로 · 작업실 꺼졌을 때 OpenAI 코드 인터프리터 대체.
6. **오래된 것**: whyfail 오탐 정리 · 바카라 안 고른 경우 · 스포츠 국가대표/NHL · 음성 렉 (#29, 라이브 통화로만 확인 가능) ·
   아침 보고 (#32) · **API-Sports 키 거절('Missing application key') — 오너가 dashboard.api-football.com 확인해야 함**.

## 3. 오늘(10-05~06) 한 일 — PR 번호
- 스티커 글자·도형 레이어 프레임워크(글꼴 5개·말풍선·여러 줄) + run_code src_*.png → 스티커 원본 + 안내서 sticker.md.
- 스티커: [📦 내 팩에 넣기] 버튼(봇이 팩 직접) · 정지 스티커 · '글자만 바꿔' = redraw + 코드 글자 + 남은 글자 검사 · 채굴 간격 방 설정(mine_minutes).
- 도구 추론 low + 조회 병렬 · 말싸움 길 도구 없이 · 끝 도구 뒤 호출 생략 · '소담이도' 호출 · 방 미디어 기록(media_log 3일).
- '해줘'에 '못 해요' 고침: 보내기 전 refuse 검사·light 거절 → 큰 모델·지시문 '못 해요는 마지막 수단'·오너 스킬 추가 요청 접수 (서버 600건 중 9건 실측).
  조사 보고서(아티팩트·코드 실행·자가개선·Claude vs 소담)는 세션 scratchpad 에만 — 결론: 자가개선은 '후보 → 오프라인 평가 → 오너 승인 카드' 가 빠짐 (다음 후보).
- 농담·맥락 못 읽음: 프롬프트 [장난 읽기], 사실 규칙은 정보 질문에만, 놀림(tease) → 큰 모델 말싸움 길 (route._TEASE).
- 베베 기본 말투가 존댓말이라 재미없다는 클레임 → 자유분방으로 (DB).
- GPT-6 (sol·luna) 전환 + 캐시: GPT-6 은 24h 보관 없음(30분, 쓸 때마다 연장). explicit 모드 지점 3개 (#97).
- 도구 핵심 목록을 실제 사용으로 다시 (33→34개, #98·#100) — 중간에 불러오면 캐시가 통째로 깨짐.
- 작업실(격리 코드 실행) + run_code + 코드 레시피 (예약 작업 skill=code) — 서버 설치·점검 통과.
- 내 알람: '11시55분에 나 불러줘' (멤버도, 시각만 말해도) + 말로만 약속하면 보내기 전 검사.
- 바카라 회차판 + 실사 카드(CC0) — 사진 한 장 고쳐 가며 공개 (#96).
- 배포 빠르게 (#99): 테스트 2개 동시·혼자 재시도, 1.5코어, 2분 타이머, pip 건너뛰기. 업데이트 보고에 최근 변경 목록.

## 4. 절대 지킬 것 (오너 지시, 계속 유효)
- 비밀값 커밋 금지 (저장소 public). 봇 토큰은 서버 .env 에만 — 출력·반복 금지.
- **운영 DB 에 쓰기 전엔 오너에게 물어볼 것.** 읽기는 원격 점검 창구·ssh_ws 로 자유.
- AI 키 쓰는 테스트(유료) 허락 없이 X. AI 채점·말투 테스트 X. 컨테이너에서 봇 실행 X (409 + DB 갈라짐).
- 실제 돈·베팅 기능 X (카지노는 가상 포인트만). 미성년+성적, 실제 사람 사진+성적은 끝까지 막음.
- 한국어 반말로만 보고, 서버에서 확인한 것만 '완료'.
- 커밋 끝: Co-Authored-By + Claude-Session 줄. PR 본문 끝: Generated with 줄 + 세션 주소. 커밋·PR 에 모델 이름 X.
- 작업실은 /opt/sodam 을 절대 못 보게 (.env 백업·diag.token 있음).

## 5. 도구·명령 빠른 참고
- 서버 명령: `python3 tools/ssh_ws.py "명령"` (환경 비밀값 SODAM_SSH_KEY·SODAM_DIAG_TOKEN 필요). 원격 점검: `python3 tools/diag.py health|why hours=24|…`.
- 배포 대기: `python3 .claude/skills/sodam-deploy/scripts/wait_deploy.py <짧은커밋> --timeout 1700` (백그라운드로, 30분 넘기면 끊김 → 다시).
- 뮤테이션: 고친 줄을 되돌려 테스트가 FAIL 하는지 (`.claude/skills/sodam-mutation-test`).
- PR: `gh api repos/{owner}/{repo}/pulls -X POST -f title=… -f head=claude/button-panels -f base=main -f body=…` → `…/pulls/N/merge -X PUT -f merge_method=squash`.
- 서버 DB 읽기: `python3 tools/ssh_ws.py 'cd /opt/sodam && sqlite3 -readonly -header -column data/sodam.db "SELECT …"'` (시간은 KST, `strftime('%s','2026-10-06 04:25:00')` = KST 13:25).
