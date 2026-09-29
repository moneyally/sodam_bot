---
name: sodam-diag
description: 소담 봇 운영 서버를 클로드 작업 환경에서 읽기 전용으로 들여다보는 방법 — 원격 점검 창구(tools/diag.py — health·rooms·settings·messages·agent_runs·voice·modlog·counters·user·tables·logs)와 텔레그램 Bot API 읽기(getChat·getChatMember)로 "실측". 사용자가 "db 봐봐", "서버 로그", "왜 안 먹혀?", "이 사람 누구야/어느 방에 있어", "권한 왜 안 돼", "설정 확인", "실측으로 체크", 방 이름(벳블리·백악관·뉴월드 등)을 대며 무슨 일이 있었는지 물을 때, 또는 버그 원인을 추측이 아니라 운영 데이터로 확인해야 할 때 꼭 쓴다.
---

# 소담 원격 점검 (실측)

추측으로 답하지 말고 운영 데이터를 본 뒤 답한다. 사용자는 "db 보고 실측으로"를 자주 요구하고, 확인 안 한 걸 확인했다고 하면 크게 신뢰를 잃는다.

## 준비
- 저장소 루트에서 `/home/user/venv/bin/python tools/diag.py <route> k=v …`
- 토큰: 환경변수 `SODAM_DIAG_TOKEN` (값을 출력·커밋하지 않는다). 없으면 오너에게 봇 1:1 [🔌 원격 점검] → 토큰을 클로드 환경 설정 환경변수에 넣어 달라고 한국어로 안내.
- 이 컨테이너는 서버에 SSH 못 한다 (프록시가 HTTPS 만). 창구가 유일한 통로.

## 경로 (chat= 은 숫자 ID 또는 방 이름 일부)
| route | 쓰임 |
|---|---|
| `health` | 서버 버전(VERSION)·유닛 상태·heartbeat·음성 담당 |
| `rooms q=이름` | 방 목록·ID·체험/구독 |
| `settings chat=` | 방 설정 전체 |
| `messages chat= hours≤336 [q=] [user=] [limit≤500]` | 대화. **최신 limit 개만** — "없다"고 단정하려면 hours 를 줄여 여러 번 나눠 본다 |
| `agent_runs chat= [hours] [limit]` | AI 호출·도구 단계·결과 (왜 거절/실패했는지 여기서) |
| `modlog chat= hours=` | 제재·설정 변경 기록 (누가 풀었나 등) |
| `voice chat= hours=` | 음성 통화 기록·종료 이유 |
| `user q=숫자ID·@·예전@·이름` | 이름 기록·들어가 있는 방·방별 글 수 |
| `counters [chat] [day]`, `tables` | 집계·표 크기 |
| `logs unit=sodam|sodam-voice|sodam-autoupdate|sodam-diag lines=… [grep=] [since=]` | 서버 로그 |
시각은 유닉스 초 → KST(UTC+9)로 바꿔 보고한다.

## 텔레그램에 직접 물어보기 (읽기만)
- 봇 토큰은 `.env.migrated-to-vps` 의 `TELEGRAM_BOT_TOKEN` (출력 금지). `getChatMember`(권한·상태: administrator/restricted/kicked, can_restrict_members …), `getChat`.
- **절대 `getUpdates` 를 부르지 않는다** (운영 봇의 업데이트를 빼앗음). 봇을 컨테이너에서 실행하지 않는다. 메시지를 보내지 않는다.

## 자주 쓰는 진단 흐름
- "왜 뮤트/밴이 안 먹혀?" → `agent_runs`(도구 결과 문구) + `modlog` + getChatMember(요청자·봇·대상 권한).
- "왜 답을 안 해?" → `settings`(ai_enabled·style…) + `agent_runs` 최근 + 구독 상태(`rooms`).
- "풀렸다/바뀌었다" → `modlog` 에서 누가 언제.
- 서버 갱신 실패 → `logs unit=sodam-autoupdate` (→ `sodam-deploy` 스킬).

## 보안
- 창구 출력은 비밀값을 `[가림]` 처리하지만, 새 모양의 비밀값이 보이면 즉시 `sodam/diag.py` 의 `SECRET_RE` 를 고치고 테스트 추가, 사용자에게 토큰 재발급 권유.
- 사람 개인정보는 필요한 만큼만 보고.

## 보고
한국어, 표 하나 + 원인 한 줄 + 방법 번호 목록. 확인 못 한 건 "미확인".
