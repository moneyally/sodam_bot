# 소담(sodam) 봇 — Claude 작업 인수인계

이 파일은 Claude 세션이 바뀌어도(다른 PC, Claude 클라우드) 맥락을 이어가기 위한 메모다. 새 세션은 이 파일부터 읽는다.
다음 작업 계획은 `docs/NEXT.md`.

## 사용자
- 한국어로 짧게 반말 섞어 대화함 ("ㄱㄱ", "해줘"). 답은 한국어로, 짧고 실행 위주로.
- 텔레그램 업자 소통방 운영자. 봇을 여러 방 관리자(대표님들)에게 **방당 월 30 USDT(TRC20) 구독**으로 판매하려 함.
- GroupHelp / SangMata 같은 **버튼식 UX**를 원함. 결제 정보는 방에 절대 노출하지 말고 관리자 1:1에서만.
- PC방에서 작업하는 경우가 많음 → 로컬 파일은 사라진다고 가정하고, 중요한 건 GitHub 에 푸시.

## 프로젝트 요약
- Python 3.11+, python-telegram-bot 21 (`concurrent_updates=True`), OpenAI SDK 3.x (chat.completions + responses web_search), SQLite(aiosqlite, WAL).
- 실행: `pip install -r requirements.txt` → `.env` 준비 → `python -m sodam`. 테스트: `python tests/run_all.py` (네트워크 없이 69개, 전부 통과 상태로 푸시됨).
- 봇 계정: @sodam_ai_bot. 이름 "소담", 호출어 "소담아/소담이/소담".
- 구조와 기능 설명은 `README.md` 참고. 주요 모듈:
  - `handlers.py` 이벤트 라우팅(그룹 메시지·입장·1:1·버튼 콜백 접두어 qz/cap/an/pay/act/m, 딥링크 `/start sub_|cfg_<방ID>`)
  - `menu.py` 버튼 메뉴(1:1 전용, 누를 때마다 관리자 재확인) · `commands.py` `.명령어`
  - `billing.py`/`subscription.py`/`tron.py` 구독 결제 · `captcha.py` `cas.py` `moderation.py` 방 관리
  - `agent.py`/`tools.py`/`prompt.py`/`llm.py` AI 에이전트 · `knowledge.py` 자료 학습(RAG) · `announce.py` 예약공지 마법사

## 중요한 결정 (바꾸지 말 것, 바꾸려면 사용자에게 확인)
- **비밀값은 git 에 절대 올리지 않는다.** 저장소는 public. `.env` 와 `data/` 는 .gitignore.
  `.env.backup-template` = 실제 .env 에서 `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `TRONGRID_API_KEY` 만 비운 것.
- 결제: 서버엔 받는 주소만(`PAY_ADDRESS`), 개인키·시드 금지. 입금은 TronGrid 로 공식 USDT 컨트랙트
  `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`·받는 주소·정확한 금액(청구서마다 끝자리 다름)·확정거래·유효시간·tx 1회만 검사.
  `check_pending` 은 asyncio.Lock, 연장은 SQL 한 문장(`db.extend_paid`). 설정: 월 30 USDT / 30일 / 체험 3일 / 비구독 방 AI 하루 10회.
- 결제 UI 는 **관리자 1:1 에서만**. 방엔 "⚙️ 봇 설정 (관리자)" 버튼과 금액 없는 문구만. 방 명령 `.구독/.설정` 은 명령 메시지 삭제 + 15초 뒤 사라지는 안내.
- 방 관리자 = 텔레그램 관리자 자동 인식(코드 불필요). **봇 오너**(운영자 1명)만 서버 로그의 1회용 8자리 코드로 `/owner 코드` 등록 (1인 5회/전체 20회 틀리면 코드 폐기).
- OpenAI: gpt-5.x 는 chat.completions 에서 **도구 + reasoning_effort 동시 사용 불가** → 도구 있는 호출은 `reasoning_effort="none"` (`llm._extra`).
  프롬프트는 캐시용으로 [고정 규칙 system] → [말투 system] → [매번 바뀌는 user] 순서. 고정 규칙에 시각·이름 등 변하는 값 넣지 말 것.
- 말투: 자유분방 = 완전 반말("야 대표야, 뭐 했어?"), 나머지(정중·친근·간결·비서·츤데레)는 존댓말.
- 사용자가 `prompt.py` 에서 "송금·코인·도박 안내 금지" 규칙을 직접 지웠음 → 되돌리지 말 것. 출력 필터(링크·지갑주소 제거)는 별개로 유지.
- 1:1 채팅 AI 는 결제 여부와 무관하게 하루 무료 한도 적용(외부인이 전체 예산 소모 방지). 오너는 무제한.
- 야간 모드는 사용자 요청으로 **제외**됨.

## 실행 환경 메모 (윈도우 + Claude 데스크톱 앱)
- Claude 앱은 AppData 를 `…\Packages\Claude_pzs8sxrjxfjjc\LocalCache\…` 로 가상화함 → 앱 내부에서 설치한 python/git 경로가 앱 밖 터미널에선 다름.
- 앱 내부 셸에선 GitHub 푸시 인증이 안 됨(대화형 로그인 불가, schannel 폐기확인 오류) → 푸시는 사용자 터미널에서.
- 이 PC 에선 파이썬 SSL 이 가끔 "EE certificate key too weak" 로 실패(VPN/보안 프로그램 추정). `tools/tls_check.py` 로 진단.
- 보안 분류기가 `yua-secrets` 저장소에서 가져온 OpenAI 키 사용을 차단한 적 있음 → 키는 사용자가 직접 `.env` 에 넣고 봇도 사용자가 직접 실행.

## 남은 소소한 버그 (알려진 것)
- `commands.c_unban`/`c_cas` 의 `arg.isdigit()` → `util.to_int` 로 교체 필요 ("²" 같은 입력에 int() 예외).
- `handlers._is_admin_safe` 가 매번 `perms.forget()` → `/start sub_…` 연타 시 getChatAdministrators 과다 호출. 결제 노출 판단에만 fresh 확인하도록 인자 분리.
- `subscription.on_deep_link` 는 실사용 경로에서 안 쓰임(딥링크는 `menu.group_panel` 로 감) → 정리 대상.
