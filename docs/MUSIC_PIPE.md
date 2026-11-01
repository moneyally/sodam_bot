# 🎵 음악 송신 파이프 전환 설계 (ExternalMedia → FIFO/ffmpeg)

2026-11-01 오너 결정: 끊김(our_stall) 근본 해결. 조사(ntgcalls 소스) 결과 범인은 **`send_frame` 100회/초가 코어 수(2 vCPU)짜리 공용 스레드풀에서 경합**하는 것. 하드웨어·FEC·대기열 아님.

## 목표
`send_frame`(파이썬 100/초, 2스레드풀 경합) 을 없애고, **파이썬이 섞은 PCM 을 FIFO 에 쓰면 ntgcalls C++ 의 ThreadedReader(실시간 우선순위 스레드 + ~100ms 버퍼)가 페이싱**하게. 파이썬 왕복·스레드풀 경합 제거 = our_stall 제거.

## 구조
```
[파이썬 Player] 노래 디코드 + xfade + AI 더킹 + 볼륨 → 48kHz 모노 s16le PCM 연속 스트림
     ↓ os.write (FIFO 버퍼 차면 블록 = 자연 페이싱, 10ms pacer 불필요)
[named FIFO  data/run/dj_<chat>.pcm]
     ↓ ntgcalls 가 ffmpeg 로 읽음:  ffmpeg -f s16le -ar 48000 -ac 1 -i <fifo> → ThreadedReader(kRealtime + 100ms 버퍼)
[ntgcalls → WebRTC → 텔레그램]
```
- py-tgcalls API 확인됨: `MediaStream(fifo_path, audio_parameters=AudioParameters(48000,1), ffmpeg_parameters="-f s16le -ar 48000 -ac 1", video_flags=Flags.IGNORE)` → ntgcalls 가 `ffmpeg [params] -i <fifo>` 로 raw PCM FIFO 를 읽음.
- **ntgcalls 는 "하나의 연속 오디오"로만 봄.** 곡 경계·전환·이동을 모름 — 전부 파이썬이 FIFO 에 쓰는 내용으로 만든다.

## 기능 매핑 (전부 파이썬의 FIFO 쓰기 내용으로)
| 기능 | 지금 (send_frame) | 바뀐 뒤 (FIFO write) |
|---|---|---|
| 재생 | 10ms 조각 send_frame | 노래 PCM 을 FIFO 에 연속 write |
| 일시정지 | pacer 멈춤 | **무음(0) PCM 계속 write** (스트림 안 끊음 → underrun 방지) |
| 이동/스킵 | 디코더 위치 바꿈 | 다음 위치·다음 곡 PCM 으로 이어서 write (ntgcalls 버퍼 ~100ms 뒤 반영) |
| xfade(곡 겹침) | 파이썬 섞기 | 파이썬이 곡 끝+다음 곡 시작 섞어 write (그대로) |
| 볼륨/더킹 | 조각마다 곱 | 쓰기 전 PCM 에 곱 (그대로) |
| AI 더킹(소담 말할 때 노래↓) | voice_frame 큐 → 섞어 send | AI PCM 을 같은 FIFO 스트림에 섞어 write |
| 곡 전환 | Player 가 다음 곡 | ntgcalls 모름, 파이썬이 경계 이어붙임 |

## 전제
- **ntgcalls 3.0.0 → 3.0.4+ 올리기** (시작 2배속/드리프트 수정, 이슈 추적상 3.0.4에서 고침). 서버 실측 필수.
- FIFO 는 방마다 하나, 재시작·곡끝·나가기 때 정리(unlink). `data/run/` (tmpfs 아니어도 됨, FIFO 는 디스크 안 씀).
- ffmpeg 가 FIFO 를 열 때 **writer(파이썬)가 먼저 열려 있어야** 함 (FIFO 는 양쪽 열려야 통과) → 순서: 파이썬이 FIFO write 끝을 먼저 열고(O_RDWR 또는 writer 스레드 시작) → ntgcalls play.

## 리스크 (서브에이전트 검토 대상)
1. **FIFO underrun** — 파이썬이 제때 못 쓰면 ffmpeg 가 입력 고갈 → 끊김/EOF. 무음 패딩·충분한 선버퍼로 방지. (근데 send_frame 경합이 없어지니 파이썬이 큰 블록을 한 번에 써도 됨 = 오히려 쉬움)
2. **ffmpeg FIFO 열기 순서** — writer 없으면 open 블록. 열기 순서·O_NONBLOCK 주의.
3. **일시정지 = 무음 write** — 영원히 무음이면 CPU 약간(근데 send_frame 보다 훨씬 쌈). 길면 아예 leave 하는 게 나을 수도.
4. **이동/스킵 반응 지연** — ntgcalls 버퍼(~100ms)만큼 늦게 반영. 음악엔 무방.
5. **재시작/크래시 시 FIFO·ffmpeg 자식 정리** — 좀비 ffmpeg, 남은 FIFO.
6. **AI 더킹 타이밍** — AI 음성(Bridge)이 실시간인데 FIFO 버퍼 100ms 만큼 늦게 섞이면 더킹이 밀림. 수용 가능한지.
7. **ntgcalls 가 raw s16le FIFO 를 ffmpeg_parameters 로 실제 잘 읽나** — py-tgcalls 버전별 확인·서버 실측.

## 단계 (점진적, 각 단계 서버 실측)
1. ntgcalls 3.0.4+ 올리기 → 서버에서 지금 방식(ExternalMedia) 그대로 통화·음악 되는지 확인 (버전만 올림)
2. FIFO + MediaStream(ffmpeg_parameters) 셋업 — **재생만** (더킹·제어 없이) 한 방 실측
3. Player 를 FIFO writer 로 (10ms pacer 제거, 블록 write) — 재생·곡전환·xfade
4. 더킹·볼륨·이동·스킵·일시정지 포팅
5. netwatch our_stall **before/after 실측** (수치로 개선 증명)

## 검증
- 로컬: FIFO writer 로직 단위 테스트 (가짜 FIFO·가짜 ntgcalls — "테스트로만")
- 서버: 실제 통화 끊김 측정 (netwatch our_stall 건수·길이)
- 리스크 커서 **브랜치 분리 + 한 방 먼저 실측 → 괜찮으면 전체**. 음성/음악 전체 경로라 롤백 쉽게.

## ⚠️ 서브에이전트 코드검토 결과 (2026-11-01) — 설계 수정됨
진단(send_frame 이 2스레드 공용 executor 풀 min(32,nproc)에서 경합)은 **소스로 확정**. FIFO 도 원리상 성립(ntgcalls `threaded_reader.cpp` kRealtime 스레드가 SHELL/FIFO 를 realtime 페이싱하고 executor 를 안 탐). **단 설계에 틀린 가정·빠진 케이스 발견:**
1. **치명적 — "큰 블록 write·pacer 제거" 틀림.** 파이프 버퍼(리눅스 64KB≈0.68초)+ffmpeg+ntgcalls 배치 때문에 앞서 쓰면 **~0.7~1.5초 지연 버퍼**가 생김 → `worker._send` 가 노래 중 AI 음성을 같은 스트림에 섞는데(music.py:936 더킹) **소담 말이 ~1초 늦게 재생 = 실시간 대화 망가짐.** 스킵·이동 반영도 100ms 아니라 0.7~1.5초. → **파이썬 pacer 를 그대로 둬야**(앞서 쓰지 말 것). 그러면 이득은 "pacer 제거"가 아니라 **executor 왕복 제거뿐.**
2. **ffprobe 가 FIFO 를 건드림** — `MediaStream.check_stream` 이 SHELL 오디오에 ffprobe 실행 → 초기 PCM 먹거나 writer 없으면 20초 블록. play 전 무음 미리 쓰기 필요.
3. **EOF = 노래 영구 정지** — writer fd 를 잠깐이라도 닫으면 ffmpeg EOF → reader 파괴 → 노래 멈춤(자동 leave 는 안 함). **절대 안 닫는 영속 O_RDWR writer** 필수, 곡 전환도 fd 안 닫고 이어붙임.
4. **AI 단독 통화↔음악 소스 스왑** — 지금 AI 단독은 ExternalMedia. 음악 시작/종료 때 `set_stream_sources`(play 재호출로 교체 가능, 확인됨)로 스왑해야 하고 글리치 설계 없음.
5. 일시정지는 ntgcalls `pause` API 있음(무음 쓰기 대안).
6. **버전**: py-tgcalls==3.0.0 은 ntgcalls 를 플로팅(>=3.0.2). "3.0.4" 부정확 → **VPS 에서 `pip show ntgcalls` 실제 버전 먼저 확인.**

### 🟢 더 싸고 덜 위험한 1차 대안 = vCPU 2→4
- 전용 스레드/풀 리사이즈는 불가(C++ 안 lazy 생성 `min(32,nproc)`). **hardware_concurrency=4 로 올리면 executor 풀 자동 4스레드 — 코드 0줄·즉시 롤백.** 끊김이 send_frame 경합이면 FIFO 재설계보다 압도적 저위험·저비용. 덤으로 2코어 경합 전부(시험이 노래 끊음·스티커 throttle·run_all --jobs 2) 완화.
- **권장 순서: ① VPS ntgcalls 버전 + netwatch our_stall before 수치 고정 → ② 4 vCPU 로 올려 before/after 비교(가설 검증) → ③ 그래도 부족하면 FIFO(pacer 유지·영속 O_RDWR writer·음악 전용·ffprobe 회피).**

## 참고
- 조사 근거: ntgcalls `targets/python/support/py_async.hpp`(스레드풀=min(32,nproc)), `io/threaded_reader.cpp`(kRealtime+100ms 배치), `shell_reader.cpp`(asio::read 블록·EOF), `ntgcalls.cpp:61`(onStreamEnd), `methods/stream/play.py:46-56`(set_stream_sources 스왑), `ffmpeg.py`(check_stream=ffprobe). Yukki `room_control.go`(MediaSourceShell+ffmpeg).
- 음질(128k/stereo)은 별개 트랙 `docs/AUDIO_LAB.md`. 이 전환은 **끊김**만.
