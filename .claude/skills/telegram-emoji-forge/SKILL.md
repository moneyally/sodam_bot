---
name: telegram-emoji-forge
description: '긴 배너를 100x100 조각으로 나눠 이어 치는 텔레그램 프리미엄 "분할(타일) 커스텀 이모지"를 처음부터 만들거나(로얄클럽·사이트제휴 같은 금속 글자 + 금테 배지 세트), 전달받은 남의 이모지 팩을 분해·분석해 똑같은 스타일로 다시 만든다. 스타일은 하드코딩 템플릿이 아니라 ImageMagick 레시피 라이브러리(금·크롬·불·네온·유리)로 무한. 트리거: 분할 이모지, 타일 이모지, 이어지는 이모지, 이모지 팩, 커스텀 이모지, 제휴 이모지, "이 이모지처럼 똑같이", 로얄클럽처럼, addemoji, 긴 배너를 이모지로.'
---

# Telegram 분할(타일) 커스텀 이모지 공방

> 소담 봇 위치 (2026-10-22): 엔진 `sodam/stickerforge/banner.py`(타일 쪼개기)·`stylefx.py`(IM 스타일)·`analyze.py`(분해 분석) · AI 도구 `sodam/panels/tileemoji.py` (make_tile_emoji, 오너 1:1) · CLI `tools/make_tiles.py` · 팩 올리기 `tools/emoji_pack.py` · 샘플 캡처 `sodam/emojilab.py`. 글꼴 `sodam/data_files/fonts/`.

## 핵심 원칙 (절대 어기지 말 것)
**이음새 0**: 타일을 따로 그리거나 따로 축소하지 않는다. **전체 배너를 큰 캔버스에 한 번 그려 → 한 번만 축소 → 정확한 100×100 격자로 crop.** `banner.build_tiles(spec)` 가 이걸 하고 `qa_error`(재조립 오차)가 **0 이어야** 이음새가 없다. 0 이 아니면 틀린 것.

## 규격
- 커스텀 이모지: 정확히 **100×100** (정지 WEBP / 영상 WebM 64KB↓). 쓰는 사람은 **텔레그램 프리미엄** 필요. 봇은 메시지에 커스텀 이모지를 못 써 — 링크(`t.me/addemoji/<이름>`)로만 준다.
- 배경은 **투명**(`bg:"transparent"`), 틀은 `frames`(어두운 배지 + 금테). 불투명 bg 를 쓰면 이모지가 네모로 보인다.

## 스타일 = 하드코딩 아님 (오너 결정: 손님이 뭘 원할지 모름)
- `stylefx.render(text, style, ...)` → ImageMagick 레시피. `style` = **gold·metal·chrome·silver·fire·neon·glass** (새 스타일 = `stylefx.STYLES` 에 함수 한 줄, `convert` 조립). IM 없으면 None → banner 가 **PIL `_metal_fill` 폴백**(금색은 그대로, 나머지는 금속 비슷).
- 로컬 컨테이너엔 IM 6.9.12 있음(`convert`). 서버는 `update.sh imagemagick_setup` 이 깖(소담 런타임용).
- 글꼴(`layout.FONTS`/`DISPLAY_FONTS`): 한글 bold·serif(명조)·deco(장식) · 영문 royal(Cinzel 로열세리프)·block(Bebas)·heavy(Anton)·urban(Bungee). 한글은 bold, 영문 금색은 royal 이 자동.
- 금색 토큰(ChatGPT 로얄클럽 역설계): colors `[[240,240,179],[242,217,23],[130,113,42]]`, frames fill `[17,18,20]`·border_color `[172,143,47]`·inset `[213,173,19]`. PIL 금속은 `band·bevel·inner` 로 반사밴드·베벨·안쪽그림자.

## ① 처음부터 만들기
**소담/오너용(제일 쉬움)**: 오너 1:1 에서 "소담아 로얄클럽 금색 이모지 만들어" → AI 도구 `make_tile_emoji(text, name, style, frame, font)` 가 글자당 타일 → 팩 생성 → addemoji 링크. (오너 전용)

**클로드가 ssh 로 직접**:
1. spec JSON 작성 (`banner.py` 머리말 참고): `{cols, rows, bg:"transparent", frames, layers:[{type:"text", text:"로", style:"gold", font:"bold", at:[x,y], height}]}` — 글자 한 개당 layer 하나, at 은 그 타일 중심(0~1).
2. 로컬에서 `python tools/make_tiles.py SPEC.json OUTDIR` → `00.png…` + preview, **재조립 오차 0 확인**.
3. 미리보기를 오너에게 보내 확인받는다.
4. 서버로: `ssh_ws --put` 로 조각들 올리고 `python tools/emoji_pack.py --dir … --name 영문이름 --title 제목 --user <오너ID>` → `t.me/addemoji/<이름>_by_<봇>` 링크.

## ② "이 이모지처럼 똑같이" (복제)
봇은 MTProto 로 메시지 기록·custom_emoji 번호를 못 뽑는다 → **샘플을 소담 1:1 에 보내야** `emojilab` 이 조각 원본을 `data/emoji_lab/<시각>/` 에 저장(getCustomEmojiStickers). 그 다음:
1. ssh 로 조각 webp/webm 을 가져와 PIL 로 연다.
2. `analyze.reproduce_spec(sample_tiles, 새_글자)` → 조각마다 스타일·색·프레임을 읽어 새 글자 spec 을 만든다 (금테 금색이면 금테 금색으로). `analyze.analyze_tile(img)` = {style, colors, has_frame, glow, vector}.
3. `banner.build_tiles(spec)` 로 렌더 → ① 의 2~4 단계로 팩.
- 분석은 추정이다(폰트는 못 알아냄 — 한글 bold·영문 royal 로 근사). 토큰은 "시작값"이고, 결과를 다시 `analyze.guess_style` 로 확인해 어긋나면 colors·style 을 손본다.
- `tile_features(img)` = 지문 벡터(VEC_DIM=12). 미착수: sqlite-vec 에 쌓아 "이 샘플 = 85% 금색 카지노" 매칭·학습 (오너 '벡터로 DB').

## 못 하는 것
- 복잡한 왕관·문장(엠블럼) 로고: 폰트 글리프(♛)는 모양이 달라 불안정 → PIL 다각형 벡터로 그려야 함 (미착수). ?·!·%·숫자·HOT/NEW 는 글자 스타일로 그대로 된다.
- 폰트 그대로 알아맞히기(견본과 똑같은 글꼴): 못 함. 가장 가까운 글꼴로.

## 시험
`tests/test_banner.py`(이음새 0·격자·금속)·`test_stylefx.py`(IM 유무·폴백)·`test_tileemoji.py`(도구·팩)·`test_analyze.py`(분해·재현·벡터). 부품·스타일·폰트를 바꾸면 여기가 먼저 깨진다. 배포는 `sodam-deploy` 스킬.
