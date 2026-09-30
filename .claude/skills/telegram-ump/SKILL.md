---
name: telegram-ump
description: '텔레그램 "움프"(움직이는 프로필 사진·영상 아바타)를 사진 한 장으로 만드는 방법과 소담 봇 기능(sodam/stickerforge 부품 프레임워크 — keyframes·입자 방출기·색 보정·번쩍·번개·전환(타서 사라지기)·효과, panels/avatar.py, 옛 인자 어댑터 sodam/avatar.py)을 고치거나 새 부품·그림체·영상 API를 더할 때 쓴다. 트리거: 움프, 움직이는 프로필, 프로필 영상, video avatar, profile video, 프사 영상, 사진→영상, 입자·연기·불타기·쌍절곤 같은 연출.'
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

## 2-0. 엔진은 하나, 연출은 부품 조합 (2026-09-30 프레임워크로 다시 짬 — 오너 지시 '고정 목록 말고 프레임워크로')
**왜**: 옛 움프는 motion 6·color 8·particles 5 고정 목록(ffmpeg 필터)이라 AI 가 그 안에서만 골랐고, 최근 3일 17건 중 8건이
'줌+네온+별빛'. "쌍절곤 휘둘러"·"상품권이 다 타서 없어지게"·"담배 연기"(particles=smoke 오류)·"더 화려하게"×3 이 전부 같은 결과였다.
**지금**: 움프·스티커 모두 `sodam/stickerforge` 한 엔진. `make_profile_video(spec=…)` → `sanitize` → `forge_video`
(photo 모드·radius 0 → 512 로 그려 640 lanczos, H.264 yuv420p 무음 faststart 2MB↓).

| 부품 (spec) | 하는 일 · 핵심 값 |
|---|---|
| `motion: keyframes` | `{pivot:[x,y], keys:[{t, scale, sx, sy, rotate, x, y, opacity, ease}]}` — 아무 움직임. ease = linear/in/out/in_out/back/bounce/elastic/hold (easings.net). 쌍절곤 = 손잡이(pivot 아래) 축 ±38° 빠른 왕복 |
| `motion: 이름` | 기존 31개(zoom·punch·shake·nod…) = 값 있는 지름길 |
| `layers: particles` | 일반 방출기: shape(원·별·반짝·하트·불티·불꽃·연기·색종이·잎·꽃잎·눈·눈결정·방울·동전·지폐·번개·물방울·글자) · colors · count · size · spawn(top/bottom/left/right/edges/center/point/area/around/subject) · at · angle(0 오른쪽, 90 아래, -90 위) · speed · gravity · wind · spin · life · fade · grow · blend(normal/add) · blur · turbulence · burst |
| `layers: grade` | 밝기·대비·채도·색상·hue_spin·틴트·비네트·그레인·블룸, 값에 [a,b] = 진동 (3×4 색 행렬 하나로 PIL convert — 빠름) |
| `layers: flash / lightning` | 화면 번쩍 · 번개 줄기(+가지, +번쩍) |
| `layers: transition` | 앞에 그린 것 전체가 dissolve/burn/fade/pixelate/shatter, direction out/in, `to` 색. burn = 순위 평탄화한 값 노이즈 임계값 + 주황 가장자리 + 그을림 + 불씨 |
| `layers: 효과 이름` | 기존 fx 42개(glitch·sweep·rays·scanlines·ghost…)도 레이어로 (순서·start/end) |
| `loop` | 기본 true = 89장(2.97초) 두 바퀴 5.93초. false = 6초 한 번(ONCE_FPS 20 → 120장) — 타서 없어지기 등 |
| `cover` | 사진이 돌거나 움직여도 검은 가장자리 안 보이게 자동 확대 |

- 모든 레이어에 `start`·`end`(0~1). 그리는 순서 = 움직임 → fx → layers 순서 → 자막.
- 반복 이음새: 이름 있는 움직임·fx 는 옛 시간 t = (u·rep mod 1)·D, 입자는 태어난 시각이 u 에 주기적, grade 진동은 정수 cycles, keyframes 는 loop 면 첫 키로 돌아옴.
- 값 검사(`stickerforge.layer_params`·`_clean_layer`·`_clean_keyframes`): 이름·숫자·범위만, 색은 [r,g,b]/#hex, 글자 2자(PIL 로만), 경로·코드 없음.
  레이어(fx+layers) ≤6, keys ≤16, 입자 합계 ≤300(넘으면 비율대로 줄임), NaN/inf 버림.
- 옛 인자(motion·speed·color·particles)는 도구 설명에서 뺐고, 오면 `avatar.to_forge_spec` 이 같은 엔진 값으로 옮김(옛 이름 표는 어댑터일 뿐).
  keyframes 로 옮길 땐 키마다 움직이는 값을 **전부** 적을 것 — 빠진 값은 앞 키 값으로 채워져 zoom 이 한 번 커지고 멈췄었음(`avatar._keys`).
- AI 입력: `sticker_catalog(query)` = 부품·값 범위 + 요청과 닮은 예시(`stickerforge/examples.py`, 실패했던 실제 요청들) — 4000자 안
  (도구 결과는 agent.TOOL_RESULT_CHARS 에서 가운데가 잘림 — 예전 6,900자 카탈로그는 부품 설명이 잘려 안 보였음). section=examples/effects/recipes.
- 같은 조합 막기: 같은 사람·하루 안·요청 글은 다른데 조합이 값까지(`stickerlearn.fingerprint`: 부품 이름 + 값 소수 한 자리, seed 제외 — 이름만 비교하면 breathe+warm 과 zoom+mono 가 같다고 거절됐음) 직전과 같으면 안 그리고 돌려줌
  (정말 같게면 accept_warnings). seed 를 안 주면 코드가 매번 새로.
- 못 하는 것: 사진 속 사람의 팔다리·표정이 실제로 움직이기 = 생성형 영상 모델(아래 3단계) 필요 → 가장 가까운 전체 움직임 + `wanted`(기능 요청) + 한계 한마디.
- qc 경고: 밋밋·요란·하얗게 날아감·**거의 까맣거나 빈 프레임 50%↑(dark_ratio)**·자막 겹침. 경고면 안 보내고 한 번 고치게.
- CPU (2vCPU VPS): 무거운 조합(입자 200 + 불씨 60 + burn + bloom, 6초 20fps) 실측 CPU 18.5초(그리기 ~9.5·인코딩 ~8). 줄인 것: 입자 합성은
  PIL C 함수(alpha_composite·add·lighter), grade 는 색 행렬 한 번, 평탄화는 alpha_composite, 스프라이트 lru 캐시(크기·15° 양자화),
  x264 crf 는 넘친 비율만큼 한 번에 건너뜀. 한 번에 하나(_LOCK). 명령줄: `tools/sticker_forge.py IMAGE SPEC out.mp4 --mp4`.

## 2. (옛) ffmpeg 필터 방식 — 2026-09-30 에 없앰
예전 `avatar.graph` 는 zoompan·eq·hue 필터 + PIL 타일 두 장을 흘리는 overlay 였다. 배운 것만 남김: 입력에 `-framerate 30`(기본 25fps 면
주기가 어긋나 이음새), 흐르는 타일은 정수 칸, 모든 주기는 정수 바퀴. 지금은 엔진 안에서 같은 규칙(정수 바퀴·주기적 입자)을 지킨다.

## 3. 방식 3단계 (비용 순)
1. **효과만 (무료)**: 위 부품 조합(stickerforge). 5~20초 CPU, 비용 0.
2. **AI 그림체 + 효과 (~$0.03)**: gpt-image 고치기(`llm.image(prompt, Attached)`)로 애니·3D·네온·수채화 → 1번 효과.
   프롬프트는 항상 "Keep the same person, face, pose, outfit and composition … square avatar framing".
3. **AI 진짜 움직임 (5초 $0.15~0.5)**: 사진→영상 API. Veo 3.1 Lite 720p $0.03/초(가장 쌈, Google AI Studio 키) ·
   Kling 사진→영상 ~$0.07/초 · Wan 2.7 $0.08~0.10/초(fal 등). 출력은 16:9/9:16 → ffmpeg 로 가운데 정사각형 crop·10초 안으로 자르기·
   소리 빼기·압축. 움직임 프롬프트: "subtle hair movement, gentle breathing, occasional slow blink, static camera,
   soft light particles, seamless loop, keep face and outfit identical". (OpenAI Sora 2 API 는 2026-09 종료.)
   얼굴 표정까지 움직이는 오픈소스 LivePortrait 는 GPU 필요 → 컨테이너·일반 VPS 에는 안 맞음.

## 4. 안전 규칙 (바꾸지 말 것)
- 원본: 요청·답장에 붙은 사진(**남의 사진도 됨** — 사용자 결정 2026-09-28, 하루 한도로 충분), 없으면 요청자 프사. `Attached.owner` 는 기록용.
- 연출은 부품 + 값(sanitize 가 이름·숫자·범위만 통과), 그림체는 AI_STYLES 목록만 — AI·사용자 글이 ffmpeg 인자·파일 경로·코드로 안 들어감
  (ffmpeg 에는 코드가 만든 고정 인자와 raw 프레임만 감).
- 한도: 사람마다 하루 FREE_DAILY 개, AI 그림체는 방 이미지 한도(image_daily)도 사용. 3단계(영상 API)를 넣으면 확인 카드 + 방 달러 한도.
- ffmpeg 는 한 번에 하나(Semaphore), 60초 시간 초과.

## 5. 검증
`python tests/run_all.py animation avatar sticker_upgrade` — animation: 값 검사(경로·거대한 수·NaN·모르는 이름)·이징·keyframes pivot·
부품마다 프레임·전환 끝 투명/색·입자 방향·중력·바람·옛 인자 어댑터·카탈로그 4000자·예시 전부 그리기·무거운 조합 CPU 상한·
같은 조합 막기·seed. avatar: 옛 인자 → 640×640·h264·yuv420p·무음·5.93초·2MB↓·faststart, **끊김 없는 반복**(90번째 장 = 첫 장 —
무늬 있는 사진으로! 단색이면 확대·이동이 안 보임), 하루 한도·그림체 한도.
눈으로 확인: 만든 mp4 를 텔레그램 프로필에 직접 올려 본다 (규격 틀리면 조용히 실패).
