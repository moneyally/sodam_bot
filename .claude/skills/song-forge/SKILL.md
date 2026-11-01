---
name: song-forge
description: 레퍼런스 곡(mp3)을 코드로 분석(BPM·키·길이·파형/에너지 구간·가사 받아쓰기)하고, 그 느낌에 맞춘 새 가사와 AI 음악 스타일 프롬프트를 "부품+값 틀"로 세심하게 지어 준다(뮤레카 Mureka·수노 Suno 붙여넣기용). 장르는 고정 목록이 아니라 틀에 값만 바꿔 끼움 — 발라드·댄스·R&B·힙합·시티팝·트로트·락·OST 등 무엇이든. 트리거: 노래 만들어, 곡 제작, 작사, 가사 써줘, 뮤레카, Mureka, Suno, BGM·테마곡 만들어, 이 노래 비슷하게, 레퍼런스 분석, BPM·키·파형·가사 뽑아줘, 발라드/신나는 노래 만들어.
---

# 🎵 song-forge — AI 노래 제작 (레퍼런스 분석 → 가사·스타일 프롬프트, 틀로)

작업 환경은 **소리를 못 들어** (재생 X — 과장 말고 사실대로 말할 것). 대신 **코드로 분석·제작**한다: 레퍼런스 곡의 BPM·키·파형·가사를 숫자/글자로 뽑고, 그 느낌에 맞춰 **새 가사 + 스타일 프롬프트**를 만들어 사용자가 뮤레카/수노 웹에 붙여넣는다. "이 노래 좋아?" 같은 주관 평가는 못 함.

뮤레카(Mureka)는 **유료 Basic(약 만원/월) = 상업적 사용권**이 있음(오너가 구독 중). 웹 UI 로 씀(공식 API 키는 없음 → 내가 만든 가사·프롬프트를 사용자가 붙여넣는 방식). ⚠️ 뮤레카 자체 "배포 서비스"를 쓰면 독점 라이선스가 걸려 사클 등 다른 곳에 못 올림 → **그냥 MP3 다운로드만** 받게 안내.

## ⚠️ 제1원칙: 하드코딩 금지, 틀(부품+값)로
장르·BPM·악기·보컬을 **정해진 몇 개 중 고르기로 만들지 말 것.** 슬롯(부품)을 두고 요청·레퍼런스에 맞는 **값만 바꿔 끼운다.** 아래 예시들은 "틀에 값 하나 채운 모습"일 뿐 — 발라드가 필요하면 같은 슬롯에 발라드 값을, 트로트면 트로트 값을 넣는다. 새 장르가 와도 목록에 이름 추가하지 말고 **슬롯 값으로** 표현.

## 전체 흐름
1. **레퍼런스 분석**(레퍼런스 곡이 있을 때) — 코드로 BPM·키·길이·파형/에너지·가사
2. **기획** — 용도(방 테마곡·BGM·감정)·언어·분위기·장르 방향·보컬 성별을 요청에서 뽑기(모르면 1~2개만 되물음)
3. **가사 제작**(세심하게) — 구조 태그 + 훅 + 균일한 줄 + 감정 아크
4. **스타일 프롬프트**(틀) — 장르별로 여러 버전, 1024자 이내, 구체적으로
5. **붙여넣기 안내** — 뮤레카/수노 Custom 모드, Lyrics·Style 칸
6. **(선택) 결과 재분석** — 만든 곡 BPM·파형 다시 뽑아 레퍼런스와 비교

## 1단계 — 레퍼런스 곡 분석 (코드)
라이브러리 설치(한 번, venv):
```bash
~/venv/bin/pip install -q librosa matplotlib faster-whisper
```
- **BPM·키**: `librosa` — `beat_track`(템포), chroma 평균 → 조성 추정
- **길이·비트레이트·커버**: `ffprobe`
- **파형·에너지 구간**: `librosa` RMS → `matplotlib` 그림(벌스/코러스 큰 구간 눈으로), 산출 PNG 는 스크래치패드에 저장 후 `SendUserFile`
- **가사 받아쓰기**: `faster-whisper`(`WhisperModel("small", device="cpu", compute_type="int8")`, `transcribe(f, beam_size=5, vad_filter=True)`, 언어 자동감지) — 받아쓰기라 틀릴 수 있음(그대로 안내)
- 산출: **표**(BPM·키·길이) + **파형 그림** + **가사 전문**. 이 숫자들이 2·3단계 값의 출발점.

> 레퍼런스가 없으면 이 단계 건너뛰고, 장르·무드에서 BPM/키 값을 잡는다(아래 슬롯 가이드).

## 2단계 — 가사 제작 (세심하게, 틀)
**구조 = 섹션 태그 부품을 배열**(고정 순서 아님 — 곡에 맞게 골라 쌓음):
`[Intro] [Verse] [Pre-Chorus] [Chorus] [Post-Chorus] [Bridge] [Hook] [Drop] [Outro]`
세심하게 지키는 원칙(값은 곡마다 다름):
- **훅 하나에 집중** — 반복되는 핵심 한 줄(제목이 되는 문구). 코러스 첫 줄이 가장 세게.
- **줄 길이 균일하게** — AI 보컬이 박자에 자연스럽게 얹힘(들쭉날쭉하면 발음 깨짐).
- **감정 아크** — Verse(상황)→Pre(고조)→Chorus(폭발)→Bridge(전환). 용도 반영(방 테마곡이면 방 이름·분위기, 신나면 떼창 포인트).
- **언어는 값** — 한국어만 / 한국어+영어 훅 / 영어 — 요청·레퍼런스대로. 영어 훅은 짧고 발음 쉬운 걸로.
- 라임·구체적 이미지. 금지: 실존 저격·혐오·개인정보.
- 뮤레카/수노가 **태그를 인식**하므로 태그는 그대로 둔 채 가사만 채움.

## 3단계 — 스타일 프롬프트 (틀 = 슬롯에 값 채우기)
**스타일 = 아래 슬롯을 쉼표로 이은 한 줄**(1024자 이내, 장르만 적지 말고 구체적으로). 장르별로 **여러 버전**을 뽑아 비교하게 준다.

| 슬롯(부품) | 뭘 넣나 | 값 예(무엇이든 가능, 고정 아님) |
|---|---|---|
| 장르/서브장르 | 곡 색깔 | ballad, piano ballad, dance-pop, EDM, future bass, R&B, city pop, hiphop, boom-bap, rock, band, trot, lo-fi, OST, acoustic … |
| 언어 | 보컬 언어 | korean / korean + english / english |
| 템포(BPM) | 레퍼런스값 또는 장르 통상 | 숫자 (느린 발라드 60~76, 미디엄 90~110, 신나는 120~140+) |
| 키(조성) | 레퍼런스값 또는 무드 | C major, A minor … (밝음=major, 애절=minor 경향) |
| 악기 | 편성 | piano, strings, acoustic guitar, 808 bass, synths, pads, drums, band, brass … |
| 보컬 | 성별·톤 | female/male, emotional, airy, powerful belting, soft, raspy, autotuned, breathy, choir … |
| 무드/다이내믹 | 분위기·전개 | emotional build, big last chorus, feel-good, melancholic, anthemic, chill, cinematic … |
| 훅/특징 | 한 방 포인트 | catchy hook, whisper intro, key change, hand-clap, drop … |

**틀에 값 채운 예시(= 이게 전부 아님, 요청대로 값만 바꿔):**
- (신나는/힙합) `melodic hiphop, hyperpop, korean + english, 143 bpm, F major, 808 bass, bright synths, autotuned emotional vocals, catchy hook, big last chorus`
- (발라드) `piano ballad, korean, 68 bpm, A minor, soft piano + strings, airy emotional female vocal, melancholic, big last chorus with key change`
- (댄스팝) `k-pop dance-pop, korean + english hook, 128 bpm, C major, punchy synths + drums, bright powerful mixed vocals, feel-good, hand-clap, anthemic chorus`

→ 같은 8슬롯에 **값만 다르게** 끼운 것. 트로트·시티팝·락·OST 도 똑같이 슬롯 값으로 만든다.

## 4단계 — 붙여넣기 안내 (뮤레카/수노)
- **Custom(커스텀) 모드** 켜기
- **Lyrics 칸**: 2단계 가사(태그 포함 그대로)
- **Style 칸**: 3단계 스타일 한 줄(≤1024자). 보컬 성별 옵션 있으면 같이 지정.
- 여러 번 생성해 비교, 맘에 드는 것 **MP3 다운로드**. ⚠️ 뮤레카 **자체 배포 서비스는 쓰지 말 것**(독점 라이선스).
- 수노도 흐름 동일(Create → Custom → Lyrics/Style).

## 5단계 — (선택) 결과 재분석
사용자가 만든 MP3 보내면 1단계 코드로 BPM·키·파형 다시 뽑아 **레퍼런스와 비교**("템포 143 맞췄네", "코러스 에너지 이 구간"). 소리 평가는 못 하고 숫자로만.

## 권리·활용 (오너에게 바로 쓰는 결론)
- 유료 플랜이라 **상업권 있음** → 방 BGM·테마곡·홍보에 자유롭게.
- **사클(SoundCloud) 수익 기대 X** — 재생당 몇 분의 1센트라 인기곡 아니면 푼돈. 사클은 "공유 링크·명함"용으로만.
- AI곡은 **저작권이 법적으로 약함**(뮤레카도 "있다면"으로 하자) — 남이 베껴도 막기 어려움. 수익보다 **내 방 활용**이 제값.
- 소담 음성방에 **자동재생/테마곡**으로 틀고 싶으면 뮤직봇으로 붙일 수 있음.

## 산출물 형식 (항상)
답에는 **복붙 가능한 블록**으로:
1. (레퍼런스 있으면) 분석 표 + 파형 그림 + 가사 받아쓰기
2. **가사** (코드블록, 태그 포함)
3. **스타일 프롬프트 N개**(장르별, 각 코드블록) — 슬롯 틀로 만든 것
4. 붙여넣기 3줄 안내 + 고칠 포인트 물어보기(톤·훅 문구·BPM 등)
