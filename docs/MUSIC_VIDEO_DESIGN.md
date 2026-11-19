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

## ⚠️ 2단계 실측에서 뒤집힘 — ntgcalls 가 그룹콜 SHELL 을 안 돌림 (2026-11-19)
- 위 1차(MediaStream(url, VideoQuality))로 짰더니 **연결만 되고 소리·영상 둘 다 안 나옴** (오너 '영상이안나온대 소리도안나온다함').
- 서버 깊은 실측: ntgcalls 는 "Connection established / Stream started" 까지 가는데, **SHELL(자동 ffmpeg) 미디어 소스를 그룹콜에서 실제로 안 돌림**
  → ffmpeg 프로세스가 아예 안 뜸, 참가자 상태 `video_camera=false`, 소리도 0. MediaDescription(media_source=SHELL, ffmpeg 명령)은 멀쩡하고
  그 ffmpeg 명령을 손으로 돌리면 잘 됨(영상 1.3GB raw·소리 정상) → ntgcalls 레벨 한계 (그룹콜 SHELL 미실행).
- 추가 함정: `MediaStream(url, ...).check_stream()` 이 만드는 camera/mic.path 는 ffmpeg 가 **googlevideo 주소를 직접** 받게 하는데,
  그 주소는 IP 에 묶여 있고 socks5 는 ffmpeg 가 못 씀 → 프록시 있어도 ffmpeg 가 못 받음 (yt-dlp 만 socks5 됨). 이것도 SHELL 방식의 벽.

## ✅ 바뀐 설계 — AI 통화처럼 ExternalMedia + 우리가 프레임 직접 송출
- 근거: **AI 음성통화가 이미** `ExternalMedia.AUDIO|VIDEO` + `send_frame(MICROPHONE/CAMERA)` 로 소리·프사영상을 보내고 **잘 됨**.
  즉 send_frame 경로는 증명됨 → 뮤비도 같은 길로, ntgcalls SHELL 을 **우회**.
- 흐름(`worker._music_video`):
  1. `Source.fetch_video(vid)` = yt-dlp 로 480p 이하 **비디오+오디오 한 파일**을 WARP 우회 길로 **로컬에 받음**(data/music/mv_<vid>.mp4).
     → ffmpeg 는 googlevideo 가 아니라 **로컬 파일**만 읽음 (IP 잠김·socks5 문제 사라짐). (fetcher.py METHODS 에 fetch_video 추가 — 받기는 자식 프로세스에서.)
  2. `calls.play(chat, MediaStream(ExternalMedia.AUDIO|VIDEO, AudioParameters(48000,1), VideoParameters(854,480,30)), auto_start=True)`.
  3. ffmpeg **2개를 우리가** 돌림(로컬 파일):
     - 소리: `-vn -f s16le -ac 1 -ar 48000 pipe:1` → 10ms(960B) 조각 → `send_frame(MICROPHONE)` (소리 끝 = 곡 끝 → `_video_done`).
     - 영상: `-an -vf scale/pad=854x480,format=yuv420p -r 30 -f rawvideo -pix_fmt yuv420p pipe:1` → 614880B 프레임 → `send_frame(CAMERA, Frame.Info(854,480))`.
     - 실시간 페이싱(monotonic 시계로 조각/프레임 간격), `asyncio.create_subprocess_exec(limit=...)` 로 큰 프레임 버퍼 (기본 64KB 면 614KB 프레임에 churn).
  4. 끝·음챗닫힘·내보내짐 → `_video_done` 이 두 ffmpeg kill + 펌프 취소 + leave(노래·통화 없을 때만).
- **서버 실측(2026-11-19) 통과**: fetch_video 로 8.6MB mp4(640x360 h264 + aac) 받음, 소리 ffmpeg 300조각(960B)/0.05초·영상 ffmpeg 90프레임(614880B)/0.33초
  = 둘 다 정확한 크기·실시간보다 빠름. send_frame 은 AI통화와 같은 경로(증명됨). **단, 자이 세션이 worker 전용(한 계정 한 통화)이라 실제 음챗 end-to-end 는 배포 후 worker 로 테스트방에서.**

## 미착수(2차, MVP 뒤)
- 뮤비 대기열·전환, 노래↔뮤비 전환, 화질 선택(360/720), 자막, "지금 노래 영상으로".
- 배포 후 테스트방 실제 음챗 송출 확인(참가자 화면에 영상·소리), 30fps CPU 실측(한 방 전용이라 여유), 긴 영상 파일 LRU 정리 확인.
