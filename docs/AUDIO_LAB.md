# 🎧 오디오 실험실 — 뮤직봇 음질 올리기 조사 (2026-10-08)

조사만 했어. 봇 코드는 안 건드림. **[확인]** = 소스·문서에서 줄까지 직접 봄, **[추측]** = 근거는 있는데 실측 전.

## 결론 먼저
- 지금 음질이 나쁜 이유는 우리 PCM 이 아니라 **ntgcalls 가 Opus 를 "모노·통화 모드·약 32kbps" 로 고정**해서야. [확인]
- 고칠 곳은 ntgcalls 파일 하나(`outgoing_audio_channel.cpp`) 몇 줄. libwebrtc 는 미리 빌드된 걸 받아 쓰니까 크롬 전체를 빌드할 필요는 없어. [확인]
- 빌드는 서버(2코어·4GB)에서 말고 **GitHub Actions(ntgcalls 저장소 포크)** 에서 휠 만들어 받는 게 맞아. 워크플로가 이미 있음. [확인]
- 스테레오는 효과 거의 없을 듯 — 듣는 쪽 텔레그램 앱이 스테레오로 안 풀 가능성이 큼. **비트레이트 올리기 + 음악 모드**가 핵심. [추측]
- RTMP 방송 모드는 음질은 좋지만 **한 명만 송출, 나머지는 듣기만** → 소담 AI 음성대화랑 같이 못 씀. 따로 "콘서트 모드"로만 의미 있음. [확인]
- 추천: 포크 휠로 A/B 실험 먼저 (128kbps 모노 vs 지금). 효과 확인되면 고정 버전으로 배포.

## 1. 어디를 고치면 되나

### 지금 코드 [확인]
ntgcalls v3.0.0 (태그 `v3.0.0`, 커밋 e3b075a) `wrtc/src/interfaces/media/channels/outgoing_audio_channel.cpp`
- 40~49줄: 서버가 준 payload-types 중 `opus` 를 찾아서 `useinbandfec=1`(43줄), `ptime=60`(44줄) **만** 넣음. 서버가 준 `parameters` 는 opus 에선 버림(피드백만 씀).
- 60·70줄: `set_bandwidth(-1)` (제한 없음 = 기본값 사용).
- 76~84줄: `RtpParameters.encodings` 를 비어 있으면 하나 만들기만 하고 `max_bitrate_bps` 안 넣음.
- 그룹통화도 이 클래스를 씀: `wrtc/src/interfaces/group_connection.cpp` 388·391줄 (서버 응답의 `audio_payload_types` 를 그대로 넘김).
- ntgcalls 어디에도 `BitrateConstraints`·`SetClientBitratePreferences` 없음 (grep 0건).

### WebRTC 가 그 값으로 하는 일 [확인]
`modules/audio_coding/codecs/opus/audio_encoder_opus.cc` (webrtc main):
- 129~135줄 `GetChannelCount`: fmtp `stereo=1` 일 때만 2채널, 아니면 1.
- 66~94줄: 기본 비트레이트 = 풀밴드 32000 × 채널 수 → 모노면 **32kbps**.
- 253~265줄: `usedtx`·`cbr`·`maxaveragebitrate` 를 fmtp 에서 읽음. 그리고 **채널 1개면 `kVoip`(통화 모드), 2개면 `kAudio`(음악 모드)** — 모드는 stereo 여부로만 정해짐.
- `media/engine/webrtc_voice_engine.cc` 239~269줄 `ComputeSendBitrate`: `encodings[0].max_bitrate_bps` 가 있으면 그 값(코덱 최대 510k 안)으로 목표 비트레이트를 잡음. 지금처럼 없고 bandwidth -1 이면 코덱 기본값(32k).

### 바꿀 줄 (제안)
```cpp
// outgoing_audio_channel.cpp 43~44줄 근처
codec.SetParam(webrtc::kCodecParamUseInbandFec, 1);
codec.SetParam(webrtc::kCodecParamPTime, 20);              // 60 → 20 (음악은 짧은 프레임이 나음, 선택)
codec.SetParam(webrtc::kCodecParamStereo, 1);             // kAudio(음악 모드) 켜는 유일한 길
codec.SetParam(webrtc::kCodecParamSPropStereo, 1);
codec.SetParam(webrtc::kCodecParamMaxAverageBitrate, 128000);
codec.SetParam(webrtc::kCodecParamUseDtx, 0);             // 조용한 구간에서 끊김 방지
// cbr 은 일단 끔 (같은 평균이면 VBR 이 음질 좋음, RFC 7587)

// 80~82줄 encodings 만드는 곳
updated_parameters.encodings[0].max_bitrate_bps = 128000;
```
- 값은 하드코딩 말고 **환경변수(예: `NTG_OPUS_BITRATE`, `NTG_OPUS_STEREO`)로 읽게** 패치하면 다시 빌드 없이 A/B 가능. [제안]
- 우리 PCM 은 지금 48k 모노(`worker.py` 258·564줄 `AudioParameters(48000, 1)`). stereo=1 이어도 모노 입력이면 WebRTC 가 같은 소리를 두 채널로 올림 → 비트 낭비 적음. [추측]
- `audio_network_adaptor` 는 기본 꺼짐(AudioOptions 에 설정 없음) — 손댈 필요 없음. [확인]
- **주의**: ntgcalls 는 서버 피드백(`transport-cc`)을 붙임 → 대역폭 추정(BWE)이 오디오 목표를 다시 깎을 수 있음 (`audio_encoder_opus.cc` 506~529줄 `OnReceivedUplinkAllocation`). 실제로 128k 가 유지되는지는 실측해야 앎. [추측]

### 텔레그램 서버(SFU)·공식 앱 쪽
- 공식 tgcalls `group/GroupInstanceCustomImpl.cpp`: 보낼 때 opus `minbitrate=maxbitrate=startbitrate=outgoingAudioBitrateKbit`(1886~1897줄), 기본 32(`GroupInstanceImpl.h` 159줄), ptime 120. 영상 없으면 전송 전체도 32000bps 로 묶음(2233~2253줄 `adjustBitratePreferences`). [확인] → **공식 앱도 32kbps**. 128k 는 텔레그램이 "보통 안 쓰는 값". [확인]
- 듣는 쪽: 일반 통화(지원자 ~1000명 미만)는 SFU 가 사람마다 오는 RTP 를 그대로 넘기고, 앱이 ssrc 마다 따로 디코드(tgcalls 2350줄대: payload 111 = opus 를 ssrc 채널로). 재인코딩 흔적 없음. [확인/일부 추측]
- 큰 방(그룹 1000명↑, 채널 방송 30명↑)은 서버가 듣는 사람을 "stream mode" 로 돌리고 **섞은 OGG 조각**을 줌 → 이땐 서버가 다시 인코딩. [확인 — core.telegram.org/api/group-calls] 우리 방 크기면 해당 없음.
- 스테레오: tgcalls 받는 쪽 opus 코덱에 `stereo` 값이 없음(942~944줄). WebRTC 디코더는 `stereo` 없으면 필드트라이얼 기본(`audio_decoder_opus.cc` 87줄 `GetDefaultNumChannels`)을 씀 → 텔레그램 앱(옛 WebRTC 포크)은 **모노로 풀 가능성 큼**. [추측] 그래서 stereo 는 "음악 모드 켜기용"으로만 봐.
- SFU 가 높은 비트레이트를 막거나 깎는지: 문서·소스에 근거 없음. 실측 필요. [추측]
- 서버가 join 응답에 opus `parameters` 로 뭘 주는지(`minptime` 등): ntgcalls 는 파싱만 하고(`response_payload.cpp` 107~116줄) 안 씀. 실험 0단계에서 로그로 확인할 것.

## 2. ntgcalls 빌드 방법 [확인]
- 빌드 시스템: `setup.py` + CMake. 무거운 의존성은 **전부 미리 빌드된 걸 내려받음**: libwebrtc(`cmake/FindWebRTC.cmake` → github.com/pytgcalls/webrtc-build 릴리스 m152.7977.0.3), boost·ffmpeg·glib·mesa·openh264·libx11 도 pytgcalls 릴리스(`version.properties`).
- 컴파일러: clang 22 툴체인. CI 는 미리 만든 컨테이너 `ghcr.io/pytgcalls/ntgcalls/manylinux-x86_64:latest` 에서 돎 (`targets/platforms.cfg`). 이 이미지를 직접 만들려면 `docker/build.sh` 가 LLVM 23 을 소스 빌드함 → 매우 무거움.
- 휠: `.github/workflows/build.yml` 의 `build-wheels` 잡 = cibuildwheel (`CIBW_BUILD: cp3*-manylinux_*`), 결과는 Actions 아티팩트 `ntgcalls-linux-x86_64-wheels`. `workflow_dispatch` 입력: targets·platforms·wheels·publish.
- 라이선스 LGPL-3.0 → 수정본 쓰는 건 OK, 수정한 소스는 공개(포크가 public 이면 끝).

### 구체 순서 (GitHub Actions, 추천)
```bash
# 1) pytgcalls/ntgcalls 를 우리 계정으로 포크, v3.0.0 태그에서 브랜치
git clone --branch v3.0.0 https://github.com/<우리>/ntgcalls && cd ntgcalls
git checkout -b sodam-hq
# 2) outgoing_audio_channel.cpp 패치 + build.yml 의 CIBW_BUILD 를 cp312-manylinux_x86_64 하나로 줄이기(시간 절약)
# 3) Actions → "Build And Publish" 수동 실행: targets=python? (없으면 all) platforms=linux-x64 wheels=true publish=false
gh workflow run build.yml -f platforms=linux-x64 -f wheels=true -f publish=false
gh run download <run-id> -n ntgcalls-linux-x86_64-wheels
# 4) 서버: 기존 휠 백업 후 pip install --force-reinstall --no-deps ntgcalls-3.0.0-cp312-...whl
```
- 걸리는 시간·용량: 공개된 실행 시간 기록을 못 봄(API 막힘). libwebrtc 를 안 빌드하니 **수십 분대**로 봄. [추측]
- 서버(2 vCPU·4GB)에서 직접: 컨테이너 이미지 받고(수 GB) clang 으로 C++ 수백 파일 → 4GB 램이면 `-j1~2` 로 1시간↑, 디스크 여유 10GB↑ 필요, **통화 중 CPU 뺏김**. 비추천. [추측]
- 같은 패치는 ntgcalls 버전 올릴 때마다 다시 적용해야 함 → 포크에 패치 한 커밋으로 유지.

## 3. 다시 빌드 안 하는 길

### (a) RTMP 방송 모드 [확인 — core.telegram.org/api/group-calls, method/phone.getGroupCallStreamRtmpUrl]
- 도우미 계정(관리자 필요, 봇 계정 불가)이 `phone.getGroupCallStreamRtmpUrl(peer, revoke)` 로 URL·키를 받고, 통화는 `phone.createGroupCall(rtmp_stream=True)` 로 만들어야 함.
- 그 통화는 **"외부 송출 1개 + 나머지 전원 듣기만"**, 듣는 사람은 WebRTC 가 아니라 조각(chunk) 다운로드로 재생.
- 송출: `ffmpeg -re -i 곡 -loop 1 -i cover.jpg -c:v libx264 -tune stillimage -c:a aac -b:a 160k -ar 48000 -ac 2 -f flv rtmp://…/키` → AAC 스테레오 고음질 가능. 영상 트랙이 꼭 필요한지는 미확인. [추측]
- 한계: 조각 방식이라 **수 초 지연**(추측), 멤버가 말 못 함, 소담 AI 음성대화(듣고 답하기) 불가, 방에 통화는 하나라 일반 음성방과 동시에 못 씀, 통화 종류를 바꾸려면 끝내고 다시 만들어야 함.
- → "노래만 트는 라디오 방"용 옵션으로는 쓸 만. 지금 뮤직봇(대화 + 노래 같이) 대체는 아님.

### (b) 다른 옵션·포크
- py-tgcalls `AudioQuality`(STUDIO 96k/2ch, HIGH 48k/2ch …)는 **입력 PCM 형식**일 뿐, Opus 비트레이트와 무관. [확인 — pytgcalls/types/stream/audio_quality.py]
- ntgcalls 공개 API 에 비트레이트 설정 없음. [확인 — 소스 grep]
- `tgcaller` 문서에 `AudioConfig.high_quality() 128kbps stereo` 프리셋이 있지만 실제 Opus 까지 가는지 미확인 (ntgcalls 위 래퍼면 똑같이 32k). [추측]

### (c) 다른 뮤직봇들
- Yukki·Anon 류 공개 뮤직봇은 전부 pip 의 py-tgcalls/ntgcalls 그대로 → 같은 32kbps 모노로 봄. 비트레이트 올린 공개 사례는 못 찾음. [추측]
- 즉 패치하면 **다른 음악봇보다 확실히 나은 음질**이 차별점이 될 수 있음.

## 4. 실험 계획
0. **서버 응답 기록**: 도우미가 들어갈 때 join 응답 JSON(`updateGroupCallConnection.params`)에서 audio payload-types·parameters 를 로그로 남김 (읽기만, 코드 변경은 실험 브랜치에서).
1. 포크 휠 빌드 (환경변수로 비트레이트·stereo 조절 가능하게).
2. 테스트 방 하나(우리 테스트 그룹)에서 같은 곡 30초를 세 설정으로: A 지금(32k 모노) / B 128k 모노 kVoip / C 128k stereo=1(kAudio).
3. 측정:
   - **실제 송출 비트레이트**: 서버에서 도우미 프로세스 UDP 송신 바이트를 1초마다 (`nft` 카운터 또는 `/proc/net/dev`, 다른 트래픽 없을 때). 32k→128k 가 실제로 나가는지, BWE 가 깎는지 바로 보임.
   - **받는 쪽 소리**: 두 번째 계정(사람 계정 하나 더) + py-tgcalls `record` 로 통화를 PCM 녹음 → 원본과 시간 맞춰 스펙트럼 비교(고역 차단 주파수, 8~20kHz 에너지), 가능하면 ViSQOL(Google, 오픈소스) 점수.
   - **공식 앱 귀 테스트**: 안드로이드·아이폰·데스크톱에서 오너가 블라인드로 A/B/C 듣기 (공식 앱 디코더가 진짜 기준).
   - CPU: 128k 인코딩 추가 부하(`stats.cpu_sec`) — 통화 끊김 없는지.
4. 판정: B 나 C 가 귀로 확실히 좋고 비트레이트 유지되면 → 서버 배포 (requirements-voice 에 휠 URL 고정).

### 위험
- **유지보수**: ntgcalls 올릴 때마다 패치·빌드 다시. 버전 고정 + 포크 CI 로 반자동.
- **크래시·호환**: 패치가 작아서 위험은 낮지만, SFU 가 낯선 비트레이트에 이상하게 반응할 수 있음 (공식 앱은 32k 고정). 실측 전엔 몰라.
- **약관**: 사람 계정 도우미 + 자기 통화 송출 비트레이트 조절은 클라이언트 쪽 설정이라 API 약관상 문제 될 근거 못 찾음. 서버 측 제한을 우회하는 게 아님. [추측]
- **배포**: update.sh 의 voice 패키지 설치가 PyPI 휠로 덮어쓰지 않게 버전·해시 고정 필요.

### 추천
1순위 = ntgcalls 포크 + 128k + 음악 모드(stereo=1) 패치, GitHub Actions 빌드, 위 A/B 실험.
2순위(나중) = 노래 전용 방이 필요하면 RTMP 라디오 모드 따로.

## 출처
- ntgcalls v3.0.0: https://github.com/pytgcalls/ntgcalls (위 파일·줄, `.github/workflows/build.yml`, `targets/platforms.cfg`, `cmake/FindWebRTC.cmake`, `version.properties`)
- tgcalls: https://github.com/TelegramMessenger/tgcalls/blob/master/tgcalls/group/GroupInstanceCustomImpl.cpp · `GroupInstanceImpl.h`
- WebRTC: https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_coding/codecs/opus/audio_encoder_opus.cc · `media/engine/webrtc_voice_engine.cc` · `api/audio_codecs/opus/audio_decoder_opus.cc`
- 텔레그램: https://core.telegram.org/api/group-calls · https://core.telegram.org/method/phone.getGroupCallStreamRtmpUrl
- Opus 권장 비트레이트(음악 스테레오 64~128k, VBR > CBR): RFC 7587 https://www.rfc-editor.org/rfc/rfc7587
- py-tgcalls 3.0.0 휠 `pytgcalls/types/stream/audio_quality.py`
- tgcaller: https://tgcaller.readthedocs.io/api
