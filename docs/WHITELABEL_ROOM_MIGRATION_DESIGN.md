# 🏷→🏷 기존 방을 클론봇으로 이관 (설계)

작성 2026-11-16. 오너 결정: **과거 기록까지 전부 옮김(완벽)**.

## 왜
화이트라벨 클론봇(`clone.py`)은 봇마다 **별도 DB**(`data/clones/<slug>/sodam.db`)로 격리된다.
그래서 **이미 본체 소담봇이 관리하던 방**을 구매자 클론봇으로 넘기면, 그 방의 구독기간·**손님 포인트**·
설정·기억이 클론 DB엔 없어서 **전부 리셋**된다. 특히 포인트는 손님 자산이라 날리면 끝장.

두 경로:
- **① 새 방 판매** (흔함): 구매자가 BotFather로 봇 만들고 **자기 새 방**에 넣음 → 이관 불필요. **이미 됨.**
- **② 기존 본체 방 이관** (이 문서): 본체 방(chat_id) 데이터를 클론 DB로 복사 → 이관 도구 필요.

## 뭘 옮기나 (오너 결정: 전부)
방(chat_id)에 묶인 표 분류. 표 목록의 **권위 있는 단일 출처 = `db.migrate_chat`**(그룹→슈퍼그룹
전환용) + 각 모듈 `register_schema(migrate=…)` 의 `EXTRA_MIGRATE`. **이걸 그대로 재사용**한다
(하드코딩 복붙 금지 — 미래에 표 추가돼도 자동 포함되게).

| 분류 | 예 | 이관 |
|---|---|---|
| A 설정 | chats, **chat_state**(savage·rank_fixed·casino_lose_pct·music_modes 등 숨은 설정 전부), banned_words, ai_instructions, brand_*, protected/free/owner_locked, alert_rules, sports_follow, scam_*, botlink_*, digest_prefs, rank_boost, summon_lists, sticker_recipes … | ✅ |
| B 구독결제 | subscriptions, invoices, payments | ✅ |
| C 포인트돈 | **members(points 컬럼=지갑 본체)**, casino_accounts, casino_ledger, casino_open, attend, lotto_* | ✅ (§돈) |
| D 기억 | member_memory, ai_turns, room_memory, knowledge_docs/chunks, ai_lessons, voice_lines | ✅ |
| E 기록통계 | messages, requests, warnings, mod_log, counters, agent_runs, casino_results, voice_calls, music_sessions, member_left, spamshield_verdicts, sticker_log, fedban_reports … | ✅ (오너 결정) |
| F 임시 | captcha, incidents, ai_cards, menu_tokens, *_sent, news_sent, ops_*, game_sessions, 캐시(chat_admins*), mtproto_* … | ❌ 재생성됨 |

### ⚠️ migrate 선언이 빠진 표 (기존 버그 — 같이 고침)
아래는 chat_id가 있는데 `migrate=` 선언이 없어서 **migrate_chat(슈퍼그룹 전환)에서도 유실**된다.
이관 전에 `register_schema(migrate=…)` 를 **추가** → migrate_chat 버그도 고치고 이관에도 자동 포함:
- `auto_replies` (autoreply.py) — 💬 자동답글 [plain]
- `rank_rooms` (chatrank.py) — 🏆 채팅순위 꾸미기 설정 [plain]
- `rank_user_reset` (chatrank.py) — 🎯 특정인 채팅수 초기화 [composite]
- `nsfw_ok` (nsfwguard.py) — 관리자 '괜찮음' 표시 [composite]

### 전역(chat_id 없는) 표 — 같이 안 옮기면 이름이 다 날아감
migrate_chat은 같은 DB라 안 건드리지만, **다른 DB로 가면 그 방 멤버의 이것들이 새 DB에 없다**:
- **`users`** (user_id PK) — 이름·@아이디·프사. 없으면 멤버·포인트·기억이 전부 "이름 없는 숫자 ID". **필수**.
- **`name_history`** (user_id, chat_id 없음) — 이름/아이디 변경 기록(사칭 추적). **필수**.
- 범위: 그 방 `members`에 있는 user_id 들만 복사 (전체 users 복붙 X — 다른 방 손님 노출 방지).
- (제외) `sticker_packs`·`emoji_packs` 는 사람 단위 개인 팩이라 방 이관과 무관.

## 💰 돈 무결성 (제일 위험)
- **잔액 본체 = `members.points`** (casino_accounts 아님). 이것 + `casino_ledger`(원장)를 옮겨야 포인트가 산다.
- **`casino_open` = 열린 베팅 에스크로**. `debit('bet:')`이 걸 때 +, 정산/환불이 뺌. `core.recover_open`이
  봇 시작 때 남은 casino_open을 members.points로 **환불**(kill-9 대비).
- **그래서 이관은 반드시 `casino_open`이 빈 상태(게임 멈춤)에서.** 진행 중이면:
  - 복사 후 옛 봇이 재시작 → recover_open이 그 베팅 환불 → 새 DB에도 복사돼 있으면 **이중 지급(포인트 생성)**.
  - 또는 차감된 잔액만 복사 → 베팅 결과 없이 **포인트 증발**.
- 특히 `multi`(경마·회차바카라·그래프)는 "봉인 후 진행"이라 중간에 끊으면 꼬임.

## 복사 메커니즘
- **ATTACH DATABASE는 코드에 아직 없음** → 새로 짠다. (읽기 전용 격리 분석용 `workshop/snapshot.py`는
  4개 표만 평탄화라 이관엔 부적합)
- `db.migrate_chat`의 하드코딩 목록(single/composite/plain)을 **모듈 상수로 추출** → migrate_chat과
  크로스-DB 복사가 **같은 목록 공유**.
- 복사 = `ATTACH '<clone>/sodam.db' AS dst` → 각 표 `INSERT INTO dst.<표> SELECT * FROM <표> WHERE chat_id=?`
  (drop 분류·F 임시 제외). 전역표는 `WHERE user_id IN (그 방 members의 user_id)`.
- **FTS5·sqlite-vec 색인은 복사 안 함** — 클론 봇이 켜질 때 "DB 열 때 빠진 색인 자동 채움"으로 재생성(임베딩 비용 소소).
- 전부 한 트랜잭션(ATTACH는 트랜잭션 밖에서 붙이고, 복사는 `BEGIN…COMMIT`).

## 안전 실행 절차
1. 오너 1:1에서 **넘길 방 + 받을 클론** 지정 → 확인 카드.
2. 본체에서 그 방 **'이관중' 잠금**(chat_state `migrating`): AI 답·게임 베팅·결제 확인·새 입장 처리 정지.
3. **`casino_open` 빌 때까지 대기**(진행 게임·회차·경마·그래프 종료). 타임아웃 시 중단(강제 안 함).
4. 클론 DB **ATTACH → 표별 INSERT SELECT**(설정·구독·포인트·기억·기록 전부) + 전역표(users/name_history 멤버분).
5. **검증**: 포인트 합·멤버 수·구독 만료가 양쪽 일치하는지 대조. 불일치면 클론 DB 쪽 롤백.
6. 본체에서 그 방 **비활성**: 구독 끄기 + chat_state `migrated_to=<slug>`. **데이터는 삭제 안 함**(롤백용 N일 보관).
7. **봇 교체**(수동): 방에서 본체 소담 내보내고 클론 봇을 관리자로. (한 방 두 봇 = 둘 다 반응 방지)
8. 클론 켜기 → 색인 자동 재생성 → 손님은 포인트·기억 그대로.
9. **롤백**: N일 내 문제 시 본체 복원(비활성 해제) + 클론에서 그 방 제거.

## 트리거 UI
- 🏷 클론 상세(`panels/clone.py` s_clone) → [📦 기존 방 넘기기] → 본체 방 목록(오너가 아는 방) 선택
  → 미리보기(멤버 N·포인트 합·구독) → [✅ 이관] 확인 카드(오너만).
- 게임 진행 중이면 "게임 끝나면 자동 진행" 안내(폴링) 또는 "지금은 못 함".

## 미착수·주의
- **봇 교체(방에서 본체 빼고 클론 넣기)는 수동** — 텔레그램 권한이라 자동 불가. 안내 문구로.
- 이관 중 손님이 그 방에서 게임·결제하면? → '이관중' 잠금이 막음(그 사이 메시지는 평범히 흘림).
- **리허설 필수**: 실제 판매 전, 테스트 방 하나로 전 과정 1회 — 포인트 합 대조.
- 양쪽 봇이 같은 순간 그 방 DB를 쓰지 않도록(본체 잠금 + 클론은 아직 그 방에 없음).
- 구현은 돈이 걸려 **테스트 + 뮤테이션 필수**(포인트 합 보존·casino_open 대기·이중지급 방지·전역표 범위).
