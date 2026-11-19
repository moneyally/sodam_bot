# 🎬 뮤비 재생 (음챗 비디오) 설계 — "소담아 뮤비로 틀어"

2026-11-19 오너 요청: "비디오도 나오게". 480p, 깊은 실측·테스트로 버그 없게.

## 조사 결론 (1단계 실측 통과)
- py-tgcalls 3.0.0 (서버 `/opt/sodam/.venv`) 공식 비디오 지원:
  `calls.play(chat, MediaStream(url_or_path, AudioQuality.HIGH, VideoQuality.SD_480p, ytdlp_parameters=..., ffmpeg_parameters=...))`
- 480p VP8 realtime 인코딩 = 1코어 **7배속** (실시간에 1코어 15% 미만). cx32 4코어 load 0.8 → 여유 충분.
- 음원 차단 우회(WARP 프록시·쿠키)는 `ytdlp_parameters='--proxy socks5://127.0.0.1:40000 --cookies ...'` 로 넘김 (공식).
- VideoQuality: SD_360p / **SD_480p** / HD_720p / FHD_1080p / QHD_2K / UHD_4K.

## 핵심 차이 (왜 기존 음악과 분리)
- 기존 🎵 음악 = `ExternalMedia.AUDIO` + `Player.send_frame` (우리가 오디오 조각 직접 공급 → 크로스페이드·AI음성 덕킹·볼륨정규화·대기열).
- 뮤비 = `MediaStream(url, VideoQuality)` → **py-tgcalls 가 yt-dlp+ffmpeg 로 비디오+오디오 통째 처리**. 우리 오디오 파이프 안 거침.
- **한 음챗에 스트림 하나** → 노래·AI통화와 같은 방에서 동시 불가 (play 가 스트림 교체 = 끊김).

## MVP 범위 (1차, 안전 우선)
1. `.뮤비 <곡>` (별칭 `.mv`·`.영상`) · AI 도구 `music` 에 `video=true` 또는 "뮤비로 틀어" → 큰 모델이 판단.
2. 곡 검색은 기존 `musicmatch`/`Source.search` 재사용 → **비디오 URL** 1개.
3. **충돌 가드**: 그 방 `players`(노래)·`bridges`(AI통화) 있으면 거절 "노래/통화 끝나고 틀어줘".
4. `calls.play(chat, MediaStream(url, HIGH, SD_480p, ytdlp_parameters=프록시+쿠키, ffmpeg_parameters=프록시), GroupCallConfig(auto_start=True))`.
5. `stream_end`(Update) → 음챗 나가기 + 방에 "뮤비 끝" 1줄.
6. 대기열·크로스페이드·되감기 **없음** (단건). 권한은 기존 `music_who`.
7. 한 방 동시 1개(`video_calls` 집합). MAX_CALLS 합산에 포함.

## 일감·흐름
- 봇 → `voice_jobs` kind=`music_video` payload {chat, url_or_query, by, name, quality}.
- worker `_music_video(chat_id, p)`:
  - 가드(통화·노래·MAX·이용기간) → 검색 없으면 query 로 `Source.search` → 첫 원곡 URL.
  - `self.video_calls.add(chat)` → play(MediaStream 480p) → 방에 "🎬 재생 중" → stream_end 대기.
  - 실패/시간초과 → 방에 이유, video_calls.discard, leave(노래·통화 없을 때만).

## 안전·재시작
- 재시작(SIGTERM): 뮤비는 **위치 저장 안 함**(단건) → 그냥 끊고 나감 (노래처럼 이어틀기 X).
- worker 죽으면 py-tgcalls 가 스트림 정리. video_calls 는 메모리(재시작 시 비움).
- 뮤비 중 그 방 노래 신청 오면 거절("뮤비 끝나고"). 반대도.

## 2단계(코드 후) 실측 — 테스트방에서
- 실제 480p 송출 CPU·화질·끊김, `ytdlp_parameters` WARP/쿠키로 음원 받아지는지, 참가자 화면에 뜨는지, stream_end 정상, 노래와 번갈아 충돌 없는지.
- 자이 세션이 worker 전용 → 독립 실측 불가(AUTH_KEY_DUPLICATED). **배포 후 worker 통해서만** 실측.

## 미착수(2차, MVP 뒤)
- 뮤비 대기열·전환, 노래↔뮤비 전환, 화질 선택(360/720), 자막, "지금 노래 영상으로".
