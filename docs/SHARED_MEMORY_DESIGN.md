# 🧠 방 넘어 기억 공유 설계 (ChatGPT 쓰레드처럼)

> 오너 2026-10-26: "ChatGPT 는 쓰레드가 여러 개여도 너를 기억함. 소담도 A방에서 알게 된 사람을 B방에서도 기억하게." 각 방 = 쓰레드.
> 공유 범위(오너 결정): **① 본인 사실 + ② 최근 대화 맥락** 둘 다. **구현 전 설계만.**

---

## 0. 지금 구조 (코드 실측, `sodam/memory.py`)

- **본인 사실** = `member_memory` 표, 열쇠 **(chat_id, user_id)** → 방마다 따로. 저장 규칙이 이미 **"본인 얘기만"**(다른 멤버 이름·링크·전화·계좌·지시문 다 버림, `clean_fact`) → **사람 중심이라 방 넘어 공유해도 안전한 종류.**
- **최근 대화 맥락**:
  - `ai_turns` (chat_id, user_id) = 이 사람↔소담 주고받은 Q&A, 14일. → `past_turns`(최근 3개)·`last_exchange`.
  - `speaker_thread` (db) = 지금 방 최근 30줄보다 **앞의** 이 사람 쪽 대화. **그 방 안 흐름**이라 방 전용.
- **방 흐름 요약** `room_memory` = 방 전체 분위기. **방 전용**(공유하면 안 됨 — 딴 방 분위기 섞이면 혼란·누출).
- 전부 `context_for()` 가 모아 `build_messages` 의 user 메시지 nonce 태그(데이터)로 넣음.

---

## 1. 무엇을 공유 / 무엇을 방에 남길지

| 기억 | 지금 | 공유? | 이유 |
|---|---|---|---|
| **본인 사실** (member_memory) | 방마다 | ✅ **방 넘어** | 사람에 대한 사실, 이미 필터됨. ChatGPT 기억과 가장 비슷·가장 안전 |
| **최근 Q&A** (ai_turns → past_turns) | 방마다 | ✅ **방 넘어** | 그 사람 본인의 질문·소담 답. 본인에게 다시 보여주는 거라 OK |
| speaker_thread | 방 전용 | ❌ 방 전용 | 지금 이 방 흐름 짜깁기용 — 딴 방 섞으면 뒤죽박죽 |
| room_memory (방 흐름) | 방 전용 | ❌ 방 전용 | 방 전체 분위기 = 누출 위험 최고, 공유 안 함 |

→ **공유하는 건 "그 사람 본인에 대한 것"뿐.** 방 분위기·남 얘기는 절대 안 넘어감.

---

## 2. 어떻게 (최소·안전)

**핵심: 두 표에 `shared` 플래그 한 칸씩 + 읽을 때 방 넘어 합치기.** (chat_id=0 전역 복사본은 안 씀 — 두 벌 관리·중복 지저분)

### 2-1. 플래그 (대칭 — 끈 방은 주지도 받지도 않음)
- `member_memory.shared` (기본 1), `ai_turns.shared` (기본 1) 칸 추가 (`register_columns`, 무중단).
- **쓸 때** 그 방 설정 `ai_memory_shared` 값을 넣음: 기억 정리(`_extract_from`)·`record_turn` 이 `shared = int(방 설정)`.
- 끈 방(ai_memory_shared=0)의 기억은 shared=0 → 딴 방이 못 봄. 그 방도 딴 방 걸 안 봄 (대칭).

### 2-2. 읽을 때 (`context_for`)
- 지금 방 설정 `ai_memory_shared` **on** 이면:
  - 본인 사실 = **지금 방 전부 ∪ 딴 방 중 shared=1** → `_key` 로 중복 제거(같은 사실은 지금 방 것·더 자주 쓰인 것 우선), 최대 12개.
  - past_turns = 지금 방 ∪ 딴 방 shared=1, 14일, 최근 3개 (방 이름은 안 붙임 — 그냥 '지난 대화').
- **off** 면 지금 방 것만 (지금 동작 그대로 = 회귀 0).
- speaker_thread·room_memory 는 손 안 댐.

### 2-3. 새 함수 (memory.py)
- `shared_facts(db, chat_id, user_id)` = `SELECT … WHERE user_id=? AND (chat_id=? OR shared=1)` → python 에서 `_key` 중복 제거·정렬·12개.
- `recent_turns` 에 `shared` 옵션 (딴 방 shared=1 포함).
- `record_turn(… , shared=True)` 인자 추가 (기본 True, 호출부 9곳은 설정 넘기게 — 설정 모르는 곳은 기본 True).
- `_extract_from` 가 shared 값을 `add_facts` 로 전달 → INSERT 에 shared 넣음.

### 2-4. 설정·화면
- `ai_settings.register_setting("ai_memory_shared", True, "🧠 방 넘어 기억 공유")` **기본 ON** (오너 '씹어먹자').
- aip 화면 `SOCIAL_TOGGLES` 에 추가 → 방장이 끌 수 있음. change_setting·room_checkup 자동 반영.

---

## 3. 지켜야 할 선 (프라이버시)

- **같은 봇 안에서만.** (화이트라벨 클론은 별도 DB라 원래 안 넘어감 — docs/WHITELABEL_DESIGN.md)
- 공유되는 건 **본인 사실 + 본인 Q&A 뿐.** 남 얘기·링크·전화·방 분위기(room_memory)는 애초에 저장 안 되거나 공유 안 함.
- 끈 방은 **대칭으로 완전 격리** (주지도 받지도 않음) — 경쟁방·민감한 방 보호.
- 사실은 전부 `clean_fact`(지시문·권한·링크 거름) 통과한 것만 → 전역으로 새도 안전.
- ⚠️ past_turns(딴 방 Q&A)가 소소하게 샐 수 있는 유일한 곳 → **본인에게 답할 때만** 실리고(원래 past_turns 가 그럼), 방 이름·상대는 안 붙임.

---

## 4. 시험·검증

- tests/test_shared_memory.py (새): ① 방A 사실이 방B 답 맥락에 뜸(공유 on) ② off 방은 안 뜸(대칭 — 주지도 받지도) ③ 남 얘기·room_memory 는 절대 안 넘어감 ④ 중복 사실 _key 합침 ⑤ 끈 방이 켠 방에 영향 0.
- 뮤테이션: shared 필터 빼기 / 대칭 끄기 / room_memory 공유되게 → 테스트 FAIL 해야.
- 전체 시험 → 서버 반영 확인(sodam-deploy).

---

## 5. 개발 규모
중간. member_memory·ai_turns 칸 2개 + `shared_facts`/`recent_turns` 읽기 + `context_for` 머지 + `record_turn`/`_extract_from` 쓰기 + 설정·토글. 뼈대 재사용이라 새 표·새 화면 없음.
