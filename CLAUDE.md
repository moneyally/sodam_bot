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
- 실행: `pip install -r requirements.txt` → `.env` 준비 → `python -m sodam`. 테스트: `python tests/run_all.py` (네트워크 없이 352개, 전부 통과 상태로 푸시됨).
  클라우드 컨테이너에선 시스템 cryptography 가 깨져 있어서 venv 로: `python3 -m venv ~/venv && ~/venv/bin/pip install -r requirements.txt`.
- 봇 계정: @sodam_ai_bot. 이름 "소담", 호출어 "소담아/소담이/소담".
- 구조와 기능 설명은 `README.md` 참고. 주요 모듈:
  - `handlers.py` 이벤트 라우팅(그룹 메시지·입장·1:1·버튼 콜백 접두어 qz/cap/an/pay/act/m, 딥링크 `/start sub_|cfg_<방ID>`)
  - `menu.py` 버튼 메뉴(1:1 전용): `ROUTES` 라우트 테이블 → `Screen` 반환, 권한 PUBLIC/ADMIN/TG_ADMIN/OWNER, 목표값 토글·프리셋 화이트리스트,
    1회용 토큰(`m:k:<tok>`, 긴 값·삭제 확인), 글자 입력 엔진(`svc.inputs`, `menu.handle_input`) · `commands.py` `.명령어`
  - `billing.py`/`subscription.py`/`tron.py` 구독 결제 · `captcha.py` `cas.py` `moderation.py` 방 관리
  - `agent.py`/`tools.py`/`prompt.py`/`llm.py` AI 에이전트 · `knowledge.py` 자료 학습(RAG) · `announce.py` 예약공지 마법사
  - `memory.py` 멤버 기억·방 흐름 요약·대화 기록 · `social.py` 이어 말하기·먼저 끼어들기 · `ai_settings.py` AI 설정 키
  - `casino/` 포인트 게임(! 명령, 설계 docs/GAMES.md): core 지갑·가입·채굴 · basic 주사위·슬롯·룰렛·사다리 · cards 바카라·블랙잭·하이로우 · multi 그래프·경마 · dealer 딜러 소담 대사
  - `tagnotify.py` 태그·답장 알림 · `hooks.py` 확장 지점 · `panels/*.py` 버튼 화면(greet·tagnotify·owner·announce·ai·log·room)

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
- 이름·아이디 기록(SangMata 방식)은 **누구나 전부 조회** 가능 (사용자 결정 2026-09-27: 익명 닉네임 방이라 사칭 확인 우선).
  범위를 좁히려면 `namehist.can_view` 한 곳만 바꾸면 됨.
- **다른 봇 이름(SangMata·GroupHelp 등)을 사용자에게 보이는 문구·버튼·명령어 별칭에 넣지 않는다** (사용자 결정). 코드엔 참고용으로도 쓰지 않음.
- AI 기억(멤버·방)은 항상 user 메시지의 nonce 태그 안 '데이터'로만 넣는다 (system 에 넣지 않음 → 캐시·인젝션 안전).
  기억에는 본인 얘기만, 인젝션 판정 메시지·다른 멤버 이름·링크·전화번호는 저장 안 함. 멤버는 `.기억 지우기`.
- 먼저 끼어들기(`ai_chime_in`)는 **기본 꺼짐**. 켜도 120분 간격·하루 4회, 관리자 공지 직후·게임 중엔 안 함.
- 단톡방 답 길이: 요청 맨 끝(tail)에 '1~3문장, 목록·제안 문장 없이' 를 매번 붙인다 (실측: 평균 ~110자).
- 라이브 점검: `python tools/ai_live.py` (실제 OpenAI, 가짜 텔레그램) · 오프라인: `python tools/ai_dryrun.py`.
- AI 제재(경고·뮤트·밴)는 **항상 확인 버튼** (`tools._ask_sanction` → `handlers._confirm_action`), 한 번 답변에 1회만. 대화 속 숨은 지시로 제재 안 되게.
- 방 AI 토큰 한도 `ai_room_daily_tokens` 는 기본값=상한(60만)이라 방 관리자는 줄이기만 가능. 0=무제한 없음.
- 재시작 때 쌓인 업데이트는 버리지 않음(`drop_pending_updates=False`, 입장 놓침 방지). 5분 넘은 메시지엔 AI 답 생략(`util.is_stale`).
- `.말투 X` 한 단어 = 본인 말투. 태그·답장·설명이 붙으면 AI 가 대상 판단(`set_member_style` 관리자 전용).
- 결제 처리+연장은 `db.pay_invoice` 로 DB 스레드에서 한 번에 (공유 연결이라 중간 commit 끼어듦 방지).
- 버그 수정은 뮤테이션 검증: 고친 줄을 되돌리면 그 테스트가 FAIL 해야 함 (tests/test_fix_*.py).
- "누구 얘기인지": `addressee.py` 가 단서(답장·태그·이름·방금 입장)만 모으고 AI 가 판단. 평가 `python tools/ai_eval_addressee.py` (42상황: 인사·말투·제재 확인 버튼, 목표 엉뚱한 멘션 0).

## 클라우드 세션 서버 실행
- 봇은 커밋된 코드만 `/home/user/sodam_run` 에 풀어서 실행 (작업 중 파일이 서버에 섞이지 않게). data·.env 는 원본 폴더를 링크.
  갱신: `git --work-tree=/home/user/sodam_run checkout HEAD -- sodam tests tools docs requirements.txt` 후 재시작.

## 실행 환경 메모 (윈도우 + Claude 데스크톱 앱)
- Claude 앱은 AppData 를 `…\Packages\Claude_pzs8sxrjxfjjc\LocalCache\…` 로 가상화함 → 앱 내부에서 설치한 python/git 경로가 앱 밖 터미널에선 다름.
- 앱 내부 셸에선 GitHub 푸시 인증이 안 됨(대화형 로그인 불가, schannel 폐기확인 오류) → 푸시는 사용자 터미널에서.
- 이 PC 에선 파이썬 SSL 이 가끔 "EE certificate key too weak" 로 실패(VPN/보안 프로그램 추정). `tools/tls_check.py` 로 진단.
- 보안 분류기가 `yua-secrets` 저장소에서 가져온 OpenAI 키 사용을 차단한 적 있음 → 키는 사용자가 직접 `.env` 에 넣고 봇도 사용자가 직접 실행.

## 해결된 버그 (2026-09-26, 회귀 테스트 있음 — tests/test_menu.py)
- `'²'` 같은 입력: `isdigit()` 은 True 인데 `int()` 가 터짐 → `util.to_int` / `isdecimal()` 로 교체
  (commands 밴해제·cas·예약공지·지식삭제, db.find_members, captcha, games 업다운·퀴즈 콜백).
- `_is_admin_safe(fresh=)` : 결제 노출 판단(봇 초대·만료 안내)만 캐시 무시 + 텔레그램 관리자(`perms.is_tg_admin`) 기준. 딥링크 연타는 캐시 사용.
- `subscription.on_deep_link` 삭제 (딥링크는 `menu.group_panel` 로). 결제 버튼(`pay:new`)도 TG 관리자·fresh 확인.

## 확장 규칙 (여러 에이전트가 동시에 작업해도 파일이 안 겹치게)
- 새 버튼 화면 = `sodam/panels/<이름>.py` 하나 추가. `menu.register_screen / register_hub / register_main /
  register_toggle / register_preset / register_input / register_token_action / register_screen_extra` 로 등록 (menu.py 수정 불필요).
- 새 설정 키 = `settings.register_setting(...)`, 새 테이블 = `db.register_schema("CREATE TABLE IF NOT EXISTS …")`
  + 그 모듈 안에서 `db._all/_one/_write` 로 쿼리.
- 그룹 메시지 후처리(태그 알림 등) = `hooks.add_group_message_hook(async fn(svc, bot, msg, role))`.
- 검증 하네스: `tests/harness.py` 가 4개 역할(오너·TG 관리자·봇관리자·멤버)로 모든 버튼을 BFS 로 눌러
  예외·answer 1회·64바이트·HTML·권한 누출을 검사 (`tests/test_harness.py`). 화면 확인: `python tools/render_screens.py` → docs/SCREENS.md.
  패널별 하네스 데이터는 `tests/seed_<이름>.py` 에서 `harness.SEEDERS.append(async fn(svc))` (자동 로드) → 깊은 화면까지 누른다.
- 테스트 러너는 `tests/test_*.py` 자동 발견. `python tests/run_all.py [모듈명]`.
