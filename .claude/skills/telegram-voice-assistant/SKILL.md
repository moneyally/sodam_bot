---
name: telegram-voice-assistant
description: '텔레그램 그룹 음성채팅(보이스챗/비디오챗)에 소담이 들어가 실시간 AI 음성으로 대화하는 기능(sodam/voice/, panels/voice.py)을 설정·진단·고칠 때 쓴다. 도우미 사람 계정(@Sodam_bot2) 연결, api_id 발급, 권한, 음성채팅 자동 켜기, OpenAI Realtime 다리, 영상 칸 사진, 끊김·무음·입장 실패 디버깅. 트리거: 음성채팅, 보이스챗, 음성방, 통화, 전화 걸어줘, 멜론봇처럼, py-tgcalls, ntgcalls, Realtime, 도우미 계정, 음성 도우미, BOT_METHOD_INVALID, NoActiveGroupCall.'
---

# 소담 음성채팅 (도우미 계정 + OpenAI Realtime)

## 0. 왜 이 구조인가 (바꾸지 말 것)
- **봇 계정은 음성채팅에 못 들어간다.** core.telegram.org/method/phone.joinGroupCall = "Only users can use this method",
  봇 세션이면 400 `BOT_METHOD_INVALID` (MarshalX/tgcalls#188). 음성채팅의 소리·방송 조각을 받는 API(getGroupCallStreamChannels 등)도
  사람 계정 + 먼저 입장이 필요. Bot API 10.3(2026-08)까지 봇 음성채팅 기능 없음. → 음악봇들처럼 **사람 계정 1개(도우미)** 가 들어간다.
- 텔레그램이 봇에게 막아 둔 것을 봇 계정으로 우회·탐색하지 않는다 (정책 우회 + 봇 토큰 제한 위험). 번호 없는 대안은 미니앱 WebRTC 음성방(미구현).
- 도우미 계정 = `@Sodam_bot2` (사람 계정, 전용 번호). 개인 계정 사용 금지. 그 계정은 음성채팅 전용 — DM·대량 가입·초대 자동화 섞지 말 것.

## 1. 구조
```
[방 채팅] '소담아 음성방 들어와' → AI 도구 voice_call(start)  (panels/voice.py, 봇 프로세스)
   → precheck(권한·이용 기간·예산·한 달 분·도우미·worker 살아있음)
   → 도우미가 방에 없으면 bot.create_chat_invite_link(1명·10분) → voice_jobs 'join'
   → bot.promote_chat_member(도우미, can_manage_video_chats) 시도
   → voice_jobs 'start' {instructions(PERSONA+📝방 안내), voice, reply, max_sec, idle_sec}
[sodam-voice 프로세스] (sodam/voice/worker.py, Telethon 도우미 세션 + py-tgcalls)
   → play(외부 오디오 48k 모노 [+ 영상 640×360 2fps, VOICE_VIDEO=1 일 때만], GroupCallConfig(auto_start=True))  ← 음성채팅 없으면 켬
   → record(외부 오디오) → stream_frame(INCOMING, MICROPHONE) → Bridge.feed
   Bridge(bridge.py): 48k→24k → input_audio_buffer.append(100ms) → OpenAI Realtime(gpt-realtime-2.1-mini, server_vad)
                     ← response.output_audio.delta 24k→48k 10ms 조각 → send_frame(MICROPHONE) (절대 시각 페이서)
                     speech_started → 줄 비우고 conversation.item.truncate(들려준 ms)
   → 끝: '소담아 나가'·60초 무음·15분·관리자 stop → leave_call → voice_calls 기록 → 봇 틱이 방에 '📴 N분'
```
- 봇 ↔ worker 는 **DB 로만** (`voice_jobs` 1초 폴링, 120초 안 가져가면 no_worker). ntgcalls(네이티브)가 죽어도 봇 본체는 안전.
- 통화 대화(받아쓰기·소담 답·도구 결과)는 voice_lines 에 **7일 보관·오너만** 봄 (🎙→🗒). voice_calls = 시간·이유·말 횟수·계측 stats.
- 영상 칸: 도우미 계정 프사를 로그인 때 **한 번** I420 으로 변환(video.py) → 통화마다 같은 bytes 를 1초에 한 번. 영상이 실패하면 소리만으로 자동 재시도.

## 2. 처음 설정 (오너)
1. 도우미 계정 만들기: 텔레그램 앱에서 새 번호로 가입 (재활용 번호면 예전 계정 주의 — 모르는 대화가 보이면 쓰지 말 것). 2단계 비번 + 복구 이메일. 프사·이름 '소담 음성'.
2. API 키: 도우미 번호로 **my.telegram.org** → API development tools → App 만들기 → api_id(숫자)·api_hash(32자).
3. 소담 1:1 메뉴 → 🎙 음성채팅 → [📱 도우미 계정 연결] → ① `api_id api_hash` 한 줄(또는 `건너뛰기`=.env MTPROTO 키)
   → ② 전화번호 `+8210…` (맨 앞 0 빼기) → ③ 코드 **띄어서** `1 2 3 4 5` (붙여 보내면 텔레그램이 코드 무효화) → ④ 2단계 비번.
   입력 메시지는 바로 지우고, 값은 worker 일감으로만 넘겨 처리 즉시 payload 삭제. 키 = data/voice_assistant.api(0600), 세션 = data/voice_assistant.session(0600).
4. 서버: update.sh 가 본체 재시작 성공 뒤 requirements-voice.txt(py-tgcalls·websockets) 설치 + sodam-voice 서비스 설치·재시작.
   🎙 화면 '음성 담당 프로세스: 켜짐 ✅' 확인. 로그 `journalctl -u sodam-voice -n 100`.

## 3. 방마다 (관리자) — 🎙 화면 [📖 사용 안내] · [✅ 확인하기]
- 소담 봇 권한: 초대 링크로 사용자 초대 · 새 관리자 추가(도우미에게 음성채팅 관리 자동 부여) · 음성채팅 관리.
- 도우미: 방에 있고 '음성채팅 관리' (직접 줄 땐 그 권한만). 없으면 음성채팅을 사람이 먼저 켜야 함.
- 설정: voice_who 관리자만(기본)/누구나 · voice_reply 항상/'소담' 부를 때만(사람 많을 때).

## 4. 증상 → 원인
| 증상 | 확인 | 고치기 |
|---|---|---|
| '음성 담당이 꺼져 있어요' | voice_worker_beat 30초 안 갱신? | `systemctl status sodam-voice`, py-tgcalls 설치 실패면 update 로그 |
| '도우미 계정이 아직 연결 안 됐어요' | chat_state(0, voice_assistant) | 오너 🎙 연결. 세션 풀림이면 worker 로그 '세션이 풀렸어요' |
| no_voice_chat | 도우미에게 음성채팅 관리 없음 + 음성채팅 꺼짐 | 봇에 '새 관리자 추가' 주거나 사람이 음성채팅 켜기 |
| no_invite_right | 봇에 초대 권한 없음 | 봇 권한 '초대 링크로 사용자 초대' |
| banned | 도우미 kicked | 차단 해제 (봇이 unban 시도함) |
| 들어왔는데 말 안 함 | OpenAI 오류(로그 'Realtime 오류')·예산 초과 | OPENAI 키·DAILY_USD_BUDGET |
| 소담 말이 끊겨 들림 | 페이서 밀림(서버 CPU) | 동시 통화 VOICE_MAX_CALLS 줄이기, VOICE_VIDEO=0 |
| 자기 말에 스스로 대답 | INCOMING 만 받는지(stream_frame Direction.INCOMING) | 필터 확인 |

## 5. 설정값 (.env)
VOICE_ENGINE(realtime 기본 | live = gpt-live-1, docs/VOICE_LIVE.md) · VOICE_MODEL(gpt-realtime-2.1-mini) · VOICE_VOICE(marin, 소담=여자 비서) · VOICE_ROOM_MONTH_MIN(120) · VOICE_CALL_MAX_SEC(900) ·
VOICE_IDLE_SEC(60) · VOICE_MAX_CALLS(3) · VOICE_VIDEO(0 = 소리만, 1 = 영상 칸에 프사) · 요금 = 실제 토큰(store.cost_micro 요금표, 하루 AI 예산에 더함) — 요금표에 없는 모델이면 VOICE_USD_PER_MIN(0.08) 추정.

## 6. 테스트
`python tests/run_all.py test_voice` — 가짜 Realtime·py-tgcalls·Telethon (네트워크 없음). **실제 통화는 VPS 에서만** (컨테이너는 UDP·MTProto 막힘).
실제 확인 순서: 🎙 ✅ 확인하기 전부 ✅ → 채팅 '소담아 음성방 들어와' → 인사 들림 → 말 걸기 → 끼어들기 → '소담아 나가' → '📴 N분'.
