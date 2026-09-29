---
name: telegram-ump
description: '텔레그램 "움프"(움직이는 프로필 사진·영상 아바타)를 사진 한 장으로 만드는 방법과 소담 봇 기능(sodam/avatar.py, panels/avatar.py)을 고치거나 새 움직임 스타일·그림체·영상 API를 더할 때 쓴다. 트리거: 움프, 움직이는 프로필, 프로필 영상, video avatar, profile video, 프사 영상, ffmpeg 루프 영상, 사진→영상.'
---

# 텔레그램 움프 만들기

## 1. 규격 (하나라도 틀리면 텔레그램이 오류 없이 버림)
| 항목 | 값 |
|---|---|
| 모양 | 정사각형, 800×800 이하 (소담은 640×640) |
| 길이 | 10초 이하 (소담은 6초 = 30fps × 180프레임) |
| 크기 | 2MB 이하 (넘으면 crf 올려 한 번 더) |
| 코덱 | H.264 (`-c:v libx264 -profile:v main`), `yuv420p` |
| 소리 | 없음 (`-an`) |
| 컨테이너 | MP4 + `-movflags +faststart` |
| 보내기 | **send_document** (파일). send_video 는 텔레그램이 다시 압축해 규격이 깨질 수 있음 |

봇은 남의 프로필을 바꿀 수 없다 → 파일을 보내고 "설정 → 프로필 사진 설정 → 이 파일" 안내.
`bot.get_user_profile_photos(uid, limit=1)` 로 요청자 본인의 지금 프사를 가져올 수 있다 (공개 설정이 막혀 있으면 빈 값).

## 2-0. 기본 경로 = 스티커 엔진 (2026-09-29 보강)
실제 방에서 옛 부품(흔들림 2°·확대 ±8%·색 필터)은 "약하다·별로" 였다. 지금 기본은 **스티커 엔진**(`sodam/stickerforge`):
`make_profile_video(spec=…)` → `stickerforge.forge_video(image, spec)` → photo 모드(framing auto 로 얼굴 보존, radius 0)·
움직임(zoom·punch·shake·pan)·효과(sweep·sparkle·glitch·rays·glow·meteors·slice_glitch)·자막까지 같은 프레임 → 640×640 H.264
(3초 루프 두 바퀴 = 5.93초, yuv420p, 무음, faststart, 2MB↓; 512→640 lanczos+unsharp). 반복은 프레임 89 = 프레임 0.
qc 경고(자막 겹침·잘림·밋밋/요란·하얗게 날아감)가 Result.warnings 로 오고, 도구는 경고면 안 보내고 한 번 고치게 한다.
"글리치" = 잠깐 R/B 어긋남(glitch), "네온" = glow(color)·sweep — **전체 색조·채도 돌리기가 아님**(그게 "별로"의 원인).
원본이 이미 그림이면 `stickerforge.looks_illustrated` 가 art(그림체) 를 건너뛴다. 명령줄: `tools/sticker_forge.py IMAGE SPEC out.mp4 --mp4`.
하트·눈·꽃잎·색종이·돈비·방울은 이제 스티커 엔진 fx(hearts·snow·petals·confetti·money·bubbles)에도 있어 spec 경로로 같이 쓸 수 있다.
학습(sticker_log product='ump' · sticker_recipes · `sticker_catalog(for_video=true)`) 과 없는 효과의 `wanted`(featreq) 는 스티커와 같다.
아래 2~3 은 옛 부품 경로(spec 없이 부를 때).

## 2. ffmpeg 레시피 (sodam/avatar.py 가 기준 — 옛 부품 경로)
ffmpeg 는 `imageio-ffmpeg` 정적 바이너리(`imageio_ffmpeg.get_ffmpeg_exe()`) → 시스템 ffmpeg 없어도 됨.

```
ffmpeg -y -loop 1 -i src -t 6 -filter_complex \
 "[0:v]scale=1280:1280:force_original_aspect_ratio=increase,crop=1280:1280,<STYLE>,format=yuv420p" \
 -an -c:v libx264 -profile:v main -preset veryfast -crf 26 -movflags +faststart out.mp4
```
- **끊김 없는 반복의 핵심**: 모든 움직임을 `sin(2*PI*k*n/180)` (k = 정수 바퀴)로. 처음과 끝 값이 같아서 이어진다.
  zoompan 안에서는 `on`(출력 프레임 번호), 다른 필터에선 `n` 을 쓴다.
- **입력에도 `-framerate 30`** (사진 -loop 1 입력은 기본 25fps → n 기준 효과가 7.2초 주기가 돼 이음새 — 실제로 난 버그).
- **날리는 것(하트·눈·별빛·방울·꽃잎)**: PIL 로 투명 타일 하나를 그리고 세로로 두 장 이어 붙임(가장자리 입자는 반대편에도 그림) →
  두 번째 입력으로 `crop=640:640:0:'640*mod(cells*n/180,1)'` 창을 흘려 overlay. cells 정수 = 영상 한 번에 정확히 칸 단위로 흐름.
- **사용자가 원하는 효과**: `avatar.Spec(motion × speed × color × particles)` 부품 조합. AI 는 말한 효과를 가장 가까운 부품으로
  옮기고(없으면 무엇으로 대신했는지 한마디 + feature_request), 새 부품은 MOTIONS/COLORS/PARTICLES 에 한 줄 + 반복 테스트가 자동 검사.
- 스타일 예: 숨쉬기 `zoompan=z='1.10+0.08*sin(2*PI*on/180)':x=…:y=…:d=1:s=640x640:fps=30` ·
  반짝임 `eq=brightness='0.07*sin(4*PI*n/180)':eval=frame` · 무지개 `hue=h='360*n/180'` · 흔들 `rotate='0.035*sin(2*PI*n/180)'`.
- zoompan 은 확대 여유가 있어야 흔들림이 부드러움 → 먼저 출력의 2배로 키워 정사각형으로 자른다.

## 3. 방식 3단계 (비용 순)
1. **효과만 (무료)**: 위 ffmpeg 스타일. 1~2초, 비용 0.
2. **AI 그림체 + 효과 (~$0.03)**: gpt-image 고치기(`llm.image(prompt, Attached)`)로 애니·3D·네온·수채화 → 1번 효과.
   프롬프트는 항상 "Keep the same person, face, pose, outfit and composition … square avatar framing".
3. **AI 진짜 움직임 (5초 $0.15~0.5)**: 사진→영상 API. Veo 3.1 Lite 720p $0.03/초(가장 쌈, Google AI Studio 키) ·
   Kling 사진→영상 ~$0.07/초 · Wan 2.7 $0.08~0.10/초(fal 등). 출력은 16:9/9:16 → ffmpeg 로 가운데 정사각형 crop·10초 안으로 자르기·
   소리 빼기·압축. 움직임 프롬프트: "subtle hair movement, gentle breathing, occasional slow blink, static camera,
   soft light particles, seamless loop, keep face and outfit identical". (OpenAI Sora 2 API 는 2026-09 종료.)
   얼굴 표정까지 움직이는 오픈소스 LivePortrait 는 GPU 필요 → 컨테이너·일반 VPS 에는 안 맞음.

## 4. 안전 규칙 (바꾸지 말 것)
- 원본: 요청·답장에 붙은 사진(**남의 사진도 됨** — 사용자 결정 2026-09-28, 하루 한도로 충분), 없으면 요청자 프사. `Attached.owner` 는 기록용.
- 움직임·그림체는 코드의 정해진 목록(STYLES·AI_STYLES)만 — AI·사용자 글이 ffmpeg 인자에 들어가지 않게.
- 한도: 사람마다 하루 FREE_DAILY 개, AI 그림체는 방 이미지 한도(image_daily)도 사용. 3단계(영상 API)를 넣으면 확인 카드 + 방 달러 한도.
- ffmpeg 는 한 번에 하나(Semaphore), 60초 시간 초과.

## 5. 검증
`python tests/run_all.py avatar sticker_upgrade` (sticker_upgrade: 스티커 엔진 움프 규격·반복·경고·그림 판별)
`python tests/run_all.py avatar` — 진짜 ffmpeg 로 부품마다 640×640·h264·yuv420p·소리 없음·6초·2MB 이하·faststart,
**끊김 없는 반복**(181번째 장을 디코드해 첫 장과 비교 — 무늬 있는 사진으로! 단색이면 확대·이동이 안 보여 검사가 무의미,
날리는 것은 단색 배경에서 따로), 하루 한도·그림체 한도·정해진 부품만(글이 ffmpeg 인자에 못 들어감).
눈으로 확인: 만든 mp4 를 텔레그램 프로필에 직접 올려 본다 (규격 틀리면 조용히 실패).
