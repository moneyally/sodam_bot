# 📞 음성채팅 GPT-Live 전환 설계 (2026-10-01)

오너 질문 "우리 VOICE 가 GPT Live 임?" → 아님. 지금은 **OpenAI Realtime API + gpt-realtime-2.1-mini**.
OpenAI 가 2026-09-10 GA 한 **gpt-live-1** (전이중 음성 + 백엔드 위임)로 갈 수 있게, 기존 경로는 그대로 두고
**`VOICE_ENGINE=live` 일 때만** 새 다리(`sodam/voice/live.py` LiveBridge)를 쓴다. 기본값은 `realtime` (안 깨짐).

근거: OpenAI 문서 (models/gpt-live-1 · guides/live · voice-websockets · live-delegation · live-migration · live-prompting ·
voice-latency-cost · pricing · reference/live/primary-websocket) + **설치된 openai-python 3.19.2 의 `openai/resources/live`·`types/live` 소스**
(이벤트 이름·필드는 여기서 그대로 확인 — 추측 없음).

## 1. 무엇이 다른가 (Realtime → Live)

| 항목 | 지금 (Realtime, bridge.py) | Live (live.py) |
|---|---|---|
| 연결 | `oai.realtime.connect(model=…)` | `oai.live.connect(max_retries=0)` → `session.start` → `session.started` 받은 뒤 소리 |
| 세션 설정 | `session.update` (VAD·잡음 제거·받아쓰기·truncation·reasoning·max_output·tools) | `session.start` {model, instructions(바꿀 수 없음), audio{format pcm 24000, output.voice}, delegation} — VAD·잡음·받아쓰기·온도·출력 상한 **없음** |
| 차례 | server_vad + `response.create` (우리가 '소담' 부를 때만 등 조절) | **모델이 알아서** 말함 (전이중). `response.create` 로 말 시키기 없음 → '부를 때만'은 지시문으로 |
| 끊기 | `speech_started` → 줄 비우고 `conversation.item.truncate(들려준 ms)` | 끊기 이벤트·truncate **없음** — 모델이 스스로 멈춤. 우리는 **로컬 줄만 비움** (문서: "discard locally queued audio") |
| 받아쓰기 | `…input_audio_transcription.completed` (말 한 덩어리·item_id) | `session.input_transcript.delta` / `output_transcript.delta` 조각(start_ms·end_ms, item·done 없음) → **우리가 줄 단위로 묶음** |
| 도구 | 모델이 직접 function call → `conversation.item.create(function_call_output)` + `response.create` | 모델은 도구 없음 → `session.delegation.created` → **Responses 백엔드**(gpt-6-luna)가 우리 함수 호출 → `response.event`(안의 `response.output_item.done` function_call) → `response.item.create(function_call_output)` + `response.create` |
| 오류 | 코드별 benign 목록 | `error` {code,message,param} + `session.closed`{reason: close_requested·expired·content·remote_hangup·connection_lost} |
| 사용량 | `response.done.usage` 토큰 | `session.usage.updated.usage.seconds`(누적) + 백엔드 토큰은 `response.event` 의 `response.completed.response.usage` |
| 요금 | 음성 토큰 (mini: 입력 $10·출력 $20 /1M) + 받아쓰기 | **분당 $0.05, 초 단위, 조용해도 과금** + 백엔드 토큰(gpt-6-luna $0.10·캐시 $0.01·출력 $0.50 /1M) |
| 목소리 | 10개 | 같은 10개 다 있음 (+ 새 목소리) — 그대로 |
| 소리 형식 | PCM16 24 kHz | PCM16 24 kHz 그대로 (16k 도 됨) |

## 2. 그대로 쓰는 것
텔레그램 48k↔24k 변환(audio.py) · 100 ms 묶어 보내기·밀림 버림 · 10 ms 페이서·늦음 계측 · 15분/60초 감시(idle) ·
CPU·루프 지연 계측 · 말한 사람(ssrc 소리 크기 70%) · 권한별 도구(toolset.py — 읽기는 관리자/누구나, 쓰기는 확인 카드) ·
DB 기록(voice_calls·voice_lines 7일) · 1:1 화면·말투·목소리 · worker(도우미 계정·py-tgcalls·영상 칸).
→ LiveBridge 는 Bridge 를 **물려받고** OpenAI 와 말하는 부분만 바꿈.

## 3. 바뀌거나 깨지는 것 (주의)
1. **'소담 부를 때만'(voice_reply=name)** — 코드로 막을 수 없음. 지시문 "선택된 요청에만 답한다"(live-prompting) 로만. 실제 통화로 확인 필요.
2. **끼어들기** — 서버가 끊기 신호를 안 줌. 우리 줄에 소담 소리가 남아 있는데 사람 소리(RMS ≥ LOUD)가 0.3초 이어지면 줄을 비움.
   음악봇·잡음도 끊을 수 있음 → 실측 필요. 모델 쪽은 스스로 멈춤.
3. **조용해도 과금** — 1시간 켜 두면 $3. idle 60초 감시가 더 중요해짐 (지금 그대로 동작: 받아쓰기·소담 소리·큰 소리로 활동 판단).
4. **받아쓰기 줄 묶기** — 조각이 1.2초 끊기면 한 줄. 줄 단위 '누가 말했나'는 그 줄 동안 모은 소리 크기.
5. **도구 결과 오염 표시(taint)** — 예전 Realtime response_id 대신 위임(delegation)의 response_id 로 묶음 (같은 위임 안에서 읽기 뒤 쓰기 거절 그대로).
6. **인사** — `response.create(instructions=…)` 가 없음 → 지시문 끝에 "통화 시작하면 한 문장 인사".
7. **다시 연결** — 새 `session.start` (대화 맥락은 새로, 한 번만) — 지금과 같음.
8. **세션 수명** — `expires_at` 최대 길이는 문서에 없음 → `expired` 로 닫히면 time 으로 끝냄.
9. **그룹 여러 명·에코** — 문서에 없음. 텔레그램은 남의 소리만 주므로 소담 자기 소리 에코는 없음.

## 4. 요금 비교 (추정, 15분 통화·소담이 1/3 말함)
- Realtime mini: 실측 2문장×2번 ≈ 입력 2.2k·출력 0.8k 토큰 → 대화가 길면 분당 약 $0.03~0.08 (VOICE_USD_PER_MIN 0.08 은 넉넉한 추정).
- Live: 분당 $0.05 고정 + 도구 쓸 때만 백엔드(gpt-6-luna, 한 번 수백 토큰 ≈ $0.0001).
- → 말이 많을수록 Live 가 유리, 조용히 켜 둔 시간이 많을수록 Realtime 이 유리. 실제 비교는 `store.cost_micro` 로 통화마다 기록해서 봄.

## 5. 구현 (이 커밋)
- `sodam/voice/live.py` `LiveBridge(Bridge)` + `live_config()` + `BACKEND_PROMPT`.
- `worker.py`: `VOICE_ENGINE`(realtime 기본 | live), live 면 `oai.live.connect(max_retries=0)`·모델 `gpt-live-1`(VOICE_LIVE_MODEL)·백엔드 `VOICE_BACKEND_MODEL`(gpt-6-luna).
- `store.cost_micro`: gpt-live-1 = 초 × $0.05/60 + 백엔드 토큰.
- 테스트 `tests/test_voice_live.py` (가짜 Live 연결 — SDK 이벤트 모양 그대로).

## 6. 켜는 순서 (오너)
1. 서버 `.env` 에 `VOICE_ENGINE=live` → `systemctl restart sodam-voice` (통화 없을 때).
2. 한 방에서 짧게 통화: 인사·질문·끼어들기·'날씨 알려줘'(도구)·'소담아 나가' 확인.
3. 이상하면 `VOICE_ENGINE=realtime` 로 되돌리고 재시작 (코드는 둘 다 있음).
4. 통화 끝 로그 '통화 요금 … (live)' 와 diag voice 로 비용·끊김 비교.
