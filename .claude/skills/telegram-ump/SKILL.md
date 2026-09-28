---
name: telegram-ump
description: 텔레그램 "움프"(움직이는 프로필 사진·영상 아바타)를 사진 한 장으로 만드는 방법과 소담 봇 기능(sodam/avatar.py, panels/avatar.py)을 고치거나 새 움직임 스타일·그림체·영상 API를 더할 때 쓴다. 트리거: 움프, 움직이는 프로필, 프로필 영상, video avatar, profile video, 프사 영상, ffmpeg 루프 영상, 사진→영상.
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

## 2. ffmpeg 레시피 (sodam/avatar.py 가 기준)
ffmpeg 는 `imageio-ffmpeg` 정적 바이너리(`imageio_ffmpeg.get_ffmpeg_exe()`) → 시스템 ffmpeg 없어도 됨.

```
ffmpeg -y -loop 1 -i src -t 6 -filter_complex \
 "[0:v]scale=1280:1280:force_original_aspect_ratio=increase,crop=1280:1280,<STYLE>,format=yuv420p" \
 -an -c:v libx264 -profile:v main -preset veryfast -crf 26 -movflags +faststart out.mp4
```
- **끊김 없는 반복의 핵심**: 모든 움직임을 주기 = 전체 프레임(180)인 `sin(2*PI*n/180)` 으로. 처음과 끝 값이 같아서 이어진다.
  zoompan 안에서는 `on`(출력 프레임 번호), 다른 필터에선 `n` 을 쓴다.
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
- **본인 사진만**: 요청·답장에 붙은 사진은 `vision.Attached.owner == 요청자` 일 때만, 없으면 요청자 본인 프사. 남의 사진 = 거절 (딥페이크 방지).
- 움직임·그림체는 코드의 정해진 목록(STYLES·AI_STYLES)만 — AI·사용자 글이 ffmpeg 인자에 들어가지 않게.
- 한도: 사람마다 하루 FREE_DAILY 개, AI 그림체는 방 이미지 한도(image_daily)도 사용. 3단계(영상 API)를 넣으면 확인 카드 + 방 달러 한도.
- ffmpeg 는 한 번에 하나(Semaphore), 60초 시간 초과.

## 5. 검증
`python tests/run_all.py avatar` — 진짜 ffmpeg 로 스타일마다 640×640·h264·yuv420p·소리 없음·6초·2MB 이하·faststart,
본인 사진만·하루 한도·그림체 한도·정해진 스타일만. 새 스타일을 넣으면 STYLES 에 한 줄 + 이 테스트가 자동으로 검사.
눈으로 확인: 만든 mp4 를 텔레그램 프로필에 직접 올려 본다 (규격 틀리면 조용히 실패).
