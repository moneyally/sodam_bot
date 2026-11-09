# 🖼 검색 결과에 화면 이미지·출처 붙이기 (B안 설계)

소담이 웹검색·뉴스·시세·스포츠 등으로 답할 때, **글만** 주지 말고 **대표 화면 이미지 + 출처 링크**를 방에 같이 올리자 (오너 요청 2026-11-09).
"출처 링크와 화면 이미지는 첨부할 수 없어, 자료 이름만 적습니다" 로 끝나던 걸 없앤다.

## 조사 (추측 아님 — 실제 문서·실측)

### OpenAI web_search — 이미지 결과를 **직접** 준다 (공식 확인)
- 출처: developers.openai.com `api/docs/guides/tools-web-search`
- 도구에 `search_content_types: ["text", "image"]` + `image_settings: {max_results, caption}` 를 주고,
  `include` 에 `"web_search_call.results"` 를 넣으면 → 응답의 `web_search_call.results[]` 에 `image_result` 가 옴.
- 각 `image_result` 칸: **`image_url`**, **`source_website_url`**, `thumbnail_url`(있을 때), `caption`(있을 때).
- 텍스트 출처는 지금도 쓰는 `web_search_call.action.sources` (전체 url·도메인), 본문 인용은 `url_citation`(url·title).
- **핵심: 우리가 브라우저·스크린샷을 안 돌려도 OpenAI 가 web-grounded 이미지 + 출처 url 을 묶어서 준다.** → 서버 부담 0.

### 텔레그램 — 이미지 올리는 세 길 (공식/라이브러리 문서)
- 출처: core.telegram.org/bots/api, docs.python-telegram-bot.org `LinkPreviewOptions`
- ① **sendPhoto(이미지 URL)** — 텔레그램이 그 URL 에서 이미지를 받아 사진으로 올림 (우리가 파일 안 받음).
- ② **링크 미리보기** `LinkPreviewOptions{url, prefer_large_media, show_above_text}` — 메시지에 url 넣고 큰 미리보기. 서버 부담 0,
  근데 **OG 태그 있는 사이트만**·이미지 통제 불가. PTB 20.8+ (우리 22.x → 됨).
- ③ **URL 버튼** `InlineKeyboardButton(url=…)` — 출처를 눌러서 바로 감. 본문 링크 필터·미리보기 끔 정책과 **무관**(버튼은 안 걸림).

### 그 밖 (백업 소스)
- og:image 직접 추출: requests+BeautifulSoup 또는 `opengraph-parse`(4.5KB). OpenAI 가 이미지를 못 줄 때 폴백.
- 실제 화면 스크린샷: Playwright/Chromium. **서버에 아직 없음**(실측). 메모리 여유는 있음(7.7GB 중 6.3GB free, 4코어) —
  설치는 되지만 페이지당 2~5초·수백 MB 라 **가격표·UI 를 정확히 보여줘야 할 때만** 선택적.

## 우리 코드 대조 (어디를 고치나)
- `llm.web_search`(llm.py:599)·`_search`(626) — 지금 **텍스트만** 리턴. `include=["web_search_call.action.sources"]` 이미 씀,
  `search_actions`/`_search_meta`(111) 가 sources 에서 **full url** 을 이미 뽑음(도메인만 쓰는 중).
  → `_search` tool 에 `search_content_types`·`image_settings` 추가, `include` 에 `web_search_call.results` 추가,
    `search_actions` 가 `image_result[]`(image_url·source_website_url·caption)도 파싱.
- 방 답 정책(CLAUDE.md): 본문 링크 도메인 삭제 + 링크 미리보기 끔. → **sendPhoto·URL 버튼은 이 필터를 우회**하므로 충돌 없음.
- 재사용: URL 버튼 틀(composer/greet `button_rows`), `mediastore`(이미지 전송·봇 교체 대비), `security.defang`(링크 안전).
- web_search 는 "격리 검색"이라 지금 텍스트 str 만 위로 보냄 → 이미지 url 을 핸들러까지 올릴 **곁채널** 필요
  (예: `ctx.search_images` 에 담아 답 올린 뒤 핸들러가 sendPhoto, 또는 web_search 가 (text, images[]) 리턴).

## 설계 (단계별, 가벼운 것부터)

### 1단계 — OpenAI 이미지 결과 + URL 버튼 (추천, 서버 부담 0)
1. `_search` 에 `search_content_types=["text","image"]`, `image_settings={max_results: 2, caption: True}`,
   `include += ["web_search_call.results"]`. (옵션 거절하는 모델/계정이면 `search_opts_off` 로 예전 모양 — 이미 있는 안전망에 묶음.)
2. `search_actions` 가 `web_search_call.results[]` 에서 `image_result` 를 뽑아 `{image_url, source_url, caption}` 리스트로.
3. `web_search` 가 텍스트와 함께 이미지 목록을 곁채널(`ctx.search_images`)로 올림.
4. 답을 방에 보낸 뒤, 핸들러가 **상위 1~2장**을 `bot.send_photo(image_url, caption=출처제목)` + 그 사진에
   **[📄 출처](source_url) URL 버튼** 으로. (여러 장이면 앨범 `send_media_group`, 버튼은 못 다니 사진 1장일 때만 버튼.)
5. 안전: `https` 이미지 url 만·`defang` 통과·OpenAI web-grounded(1차 필터)·사람·방 **하루 한도**(외부 이미지 과다·부적절 방지),
   방 설정 **`search_images`**(기본값 결정 필요) 로 끄기.

### 2단계 — 화면 스크린샷 (선택, 무거움)
- "이 사이트 화면 캡처해줘"·가격표·UI 처럼 **대표 이미지로 안 되는** 요청만. 서버에 Chromium 설치(배포 스크립트 `update.sh`),
  Playwright 로 열어 캡처 → mediastore 로 전송. 한 번에 하나(`_LOCK`)·타임아웃. **1단계부터 내고, 수요 보고 나중.**

## 미정 (오너 결정 필요)
1. **기본 on/off**: `search_images` 방 설정 기본 켬? 끔? (뉴스·시세·스포츠엔 이미지가 잘 어울림)
2. **몇 장**: 답당 1장? 2장? (도배 방지 = 1~2장 추천)
3. **부적절 이미지**: OpenAI web-grounded 1차 필터 믿고 그대로? 성인방은 허용?
4. **어느 기능에**: 일반 web_search 만? 뉴스·시세·스포츠·수배조회까지?
5. **스크린샷(2단계)**: 아예 넣을지, 1단계만 하고 볼지.

## 비용·부하
- 이미지 결과는 web_search **같은 호출**에 포함(별도 fetch 0) — 요금은 web_search 호출당 $0.01 그대로일 가능성, **공식 요금표 재확인 필요**.
- sendPhoto(by URL)는 텔레그램 무료. 1단계 전체가 서버 CPU·메모리 거의 0.
- 스크린샷(2단계)만 페이지당 2~5초·수백 MB.
