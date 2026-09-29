---
name: sodam-research
description: 소담 봇 작업에서 모르는 것(외부 API·요금·약관·텔레그램 규칙·라이브러리 동작·경쟁 서비스)을 추측하지 않고 직접 조사하는 방법 — 웹 검색·공식 문서·curl 실측·코드 대조 후 한국어로 출처와 함께 정리. 사용자가 "검색해봐", "조사", "알아서 찾아", "API 필요해?", "방법 모르니까", "리서치", "최신 사례", 가격·약관을 물을 때, 또는 새 기능에 외부 데이터가 필요할 때(스포츠·뉴스·결제·음성) 꼭 쓴다.
---

# 소담 직접 조사

사용자는 "모르면 검색해서 직접 리서치"를 원한다. 기억으로 답하면 틀리기 쉽다 (API 요금·약관·텔레그램 규칙은 자주 바뀜).

## 조사할 곳 (추측 대신 이것들)
| 무엇 | 어디 |
|---|---|
| OpenAI (Realtime·Responses·가격·모델) | platform.openai.com/docs, developers.openai.com, OpenAI Cookbook(github.com/openai/openai-cookbook), openai-python 소스·이슈 |
| Anthropic·Claude | docs.anthropic.com / platform.claude.com, anthropic.com/engineering 글 (에이전트·도구·컨텍스트) — Context7 MCP 로 라이브러리 문서도 |
| Codex·다른 에이전트 구현 | github.com/openai/codex 소스 (도구 설계·샌드박스·프롬프트), 공개 에이전트 저장소 |
| xAI Grok | docs.x.ai, 공개 시스템 프롬프트(github.com/xai-org), 최신 사례는 웹 검색 |
| 텔레그램 | core.telegram.org/bots/api (curl 로 원문 grep), core.telegram.org 의 MTProto·calls, python-telegram-bot 문서·소스 |
| 라이브러리 버그·성능 | 해당 GitHub 저장소의 이슈·PR·릴리스 노트 (예: pytgcalls/ntgcalls, telethon, aiosqlite, sqlite-vec) — "이미 알려진 버그인지" 먼저 |
| 방법론·성능 | 논문(arXiv, Google Scholar), 공식 벤치마크, 엔지니어링 블로그 |
| 국내 | 법령(law.go.kr), 네이버·카카오 개발자 문서, 토스 기술 블로그 |
원칙: **공식 문서 > 소스 코드·이슈 > 논문 > 블로그 > 추측.** 여러 곳이 다르면 날짜가 최신인 공식 쪽, 그래도 애매하면 실측으로 가른다.
좋은 구현을 가져올 땐 라이선스를 확인하고, 그대로 베끼기보다 우리 코드 스타일로 옮긴다.

## 순서
1. **코드부터**: 지금 저장소가 이미 뭘 쓰는지 (`grep`, CLAUDE.md). 같은 걸 두 번 만들지 않는다.
2. **공식 문서**: WebFetch / `curl -s` 로 원문. 예) 텔레그램 Bot API 는 `curl -s https://core.telegram.org/bots/api` 받아서 문장을 grep (WebFetch 요약은 중요한 문장을 빠뜨린 적 있음 — custom emoji 조건).
3. **실측**: 후보 API 는 컨테이너에서 curl 로 실제 호출해 응답 모양·개수·지연을 확인 (HTTPS 는 프록시로 됨; 막히면 `/root/.ccr/README.md`). 샘플은 테스트용으로 `tests/data/…` 에 작게 저장.
4. **약관·위험**: robots.txt, 상업 이용 조건, 저작권 표기(예: "AI 학습 및 활용 금지"), 국내 법(도박·개인정보). 위험하면 기본 꺼짐 + 환경변수로 켜게 하고 오너에게 보고.
5. **비용**: 무료 한도·유료 가격·가입 주소·넣을 환경변수 이름.
6. 여러 갈래면 조사 에이전트를 병렬로 (각자 보고서를 scratchpad 에 파일로, 요약만 받기).

## 보고 (한국어만 — 사용자는 영어를 모른다)
- 결론 한 줄 → 표(선택지·비용·키 필요·범위·위험) → 추천 → 오너가 할 일(가입 주소·환경변수).
- 출처 링크. 확인 못 한 건 "미확인" / 추정은 "추정".
