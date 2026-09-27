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
- 실행: `pip install -r requirements.txt` → `.env` 준비 → `python -m sodam`. 테스트: `python tests/run_all.py` (네트워크 없이 600개 이상, 전부 통과 상태로 푸시됨).
  클라우드 컨테이너에선 시스템 cryptography 가 깨져 있어서 venv 로: `python3 -m venv ~/venv && ~/venv/bin/pip install -r requirements.txt`.
- 봇 계정: @sodam_ai_bot. 이름 "소담", 호출어 "소담아/소담이/소담".
- 구조와 기능 설명은 `README.md` 참고. 주요 모듈:
  - `handlers.py` 이벤트 라우팅(그룹 메시지·입장·1:1·버튼 콜백 접두어 qz/cap/an/pay/act/m, 딥링크 `/start sub_|cfg_<방ID>`)
  - `menu.py` 버튼 메뉴(1:1 전용): `ROUTES` 라우트 테이블 → `Screen` 반환, 권한 PUBLIC/ADMIN/TG_ADMIN/OWNER, 목표값 토글·프리셋 화이트리스트,
    1회용 토큰(`m:k:<tok>`, 긴 값·삭제 확인), 글자 입력 엔진(`svc.inputs`, `menu.handle_input`) · `commands.py` `.명령어`
  - `billing.py`/`subscription.py`/`tron.py` 구독 결제 · `captcha.py` `cas.py` `moderation.py` 방 관리
  - `agent.py`/`tools.py`/`prompt.py`/`llm.py` AI 에이전트 · `costs.py` 요금·예산·방 한도 · `agentlog.py` AI 작업 기록 · `knowledge.py` 자료 학습(RAG) · `announce.py` 예약공지 마법사
  - `memory.py` 멤버 기억·방 흐름 요약·대화 기록 · `social.py` 이어 말하기·먼저 끼어들기 · `ai_settings.py` AI 설정 키
  - `casino/` 포인트 게임(! 명령, 설계 docs/GAMES.md): core 지갑·가입·채굴 · basic 주사위·슬롯·룰렛·사다리 · cards 바카라·블랙잭·하이로우 · multi 그래프·경마 · dealer 딜러 소담 대사
  - `tagnotify.py` 태그·답장 알림 · `hooks.py` 확장 지점 · `panels/*.py` 버튼 화면(greet·tagnotify·owner·announce·ai·log·room·agentlog)

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
  달러 한도(오너 요금제 × 방 관리자 %)도 같은 방식 — 아래 'AI 비용·작업 기록'.
- 재시작 때 쌓인 업데이트는 버리지 않음(`drop_pending_updates=False`, 입장 놓침 방지). 5분 넘은 메시지엔 AI 답 생략(`util.is_stale`).
- `.말투 X` 한 단어 = 본인 말투. 태그·답장·설명이 붙으면 AI 가 대상 판단(`set_member_style` 관리자 전용).
- 결제 처리+연장은 `db.pay_invoice` 로 DB 스레드에서 한 번에 (공유 연결이라 중간 commit 끼어듦 방지).
- 버그 수정은 뮤테이션 검증: 고친 줄을 되돌리면 그 테스트가 FAIL 해야 함 (tests/test_fix_*.py).
- 제재(밴·뮤트·경고·잠금·캡차 승인)는 텔레그램 **'사용자 차단' 권한**이 있는 관리자만 (`permissions.may/can`, 권한은 chat_admin_rights 에 캐시).
  봇관리자는 지정한 사람(mod_log 'bot_admin' 추가 기록)이 그 권한을 가진 TG 관리자이거나 오너일 때만 (위임).
- 공동 차단 명단(`fedban.py`): 방별 모드 끔/알림(기본)/자동밴, 올리기는 이용 기간 중인 방·하루 20명, 오너만 완전 삭제.
- 스팸: 수정된 메시지 재검사, 전달은 기본 '신규 입장자만 막기', 종류별 잠금(`LOCK_KINDS`, 기본 전부 허용),
  홍보 @아이디 막기는 선택(기본 꺼짐, promo_mentions) — 켜면 채널·그룹(getChat 조회)·bot 아이디만 (사람 아이디는 조회 불가라 허용 — 말 안 한 멤버 태그 오탐 방지).
- 대량 입장 방어(`raid.py`): 60초 10명 → 30분 방어. raid_action captcha(전원 캡차, 기본)/kick(안내 없이 내보내기, 끝나면 수 보고),
  입장 검사 훅 `hooks.add_member_join_hook`.
- 스팸 명단(`cas.py`): CAS + lols 동시 조회, 한 곳이라도 등록이면 차단, 조회 실패는 등록 아님(캐시 안 함).
- 최근 계정(`accountage.py`): ID→가입일 추정(npm telegram-id-age MIT 데이터, 최장 비감소 부분수열 99점, 선형 보간). 오차 수개월이라
  캡차 강제(recent_account_captcha, 기본 켬)에만 씀. 기준 데이터(2025-11)보다 큰 ID = 최근.
- 가입 신청 1:1 확인(`joinreq.py`, join_verify 기본 꺼짐): 신청자에게 5분 안에 1:1 그림 버튼(captcha.puzzle) → 맞히면 승인·3회 틀림/시간초과 거절,
  통과자는 1시간 안 입장 시 방 캡차·대량 입장 내보내기 생략. 1:1 불가면 관리자 수동 승인으로 둠. 봇에 '사용자 초대' 권한 필요.
- 자유 멤버(`free.py`, `.free @user` / `.free해제` / `.free목록`, '사용자 차단' 권한 관리자만): 지정 시 채팅 금지·캡차 대기·경고 해제,
  이후 자동 통제(도배·링크·금지어·잠금·사칭·입장 캡차·CAS·공동 차단·사기 의심·봇 조작 경고) 제외. 관리자 권한은 아님.
- 사기 의심 검사(`scamguard.py`, 🕵️ 메뉴): 선택(기본 꺼짐). 관리자 키워드·지갑주소·초대링크·신규 첫 메시지 → (선택) mini AI 확인 0.8 이상 →
  관리자 1:1 알림 + [지우기·밴·뮤트·괜찮음] 버튼 (권한 `may`). 자동 제재 없음, '가리고 확인' 모드만 삭제. 평가 `tools/ai_eval_scam.py`.
- 활동 리포트·AI 하루 요약(`reports.py`, 📊 메뉴): 기록된 데이터로만 셈(mod_log·counters rep_*·ai_turns). 체험 마지막 날 관리자 1:1 리포트+결제 화면 1번,
  하루 요약 = **받는 사람(대표님) 기준** (실제 문제: 관리자 대부분 1:1 을 안 열어 조용히 실패·여러 방 관리자는 방마다 한 통·부관리자까지 전부 받고 못 끔).
  받는 사람: 방 등록자(subscriptions.added_by)가 지금도 TG 관리자일 때 + 오너(관리자인 방) + 📊 화면 '🧠 하루 요약 받기 (나)'(m:rpme, TG 관리자만)를 켠 관리자.
  한 사람 하루 한 통(digest_log 차지): 방 1개 = 긴 요약, 여러 개 = 묶음 + [📊 방](m:rp) 버튼, 끝에 [⏰ 받는 시각](m:dgp:new, 새 메시지)·[🔕 그만 받기](m:dgp:stop, 모든 방).
  사람 시각 = digest_prefs(user_id, chat_id=0).hour (9/18/21/23), 없으면 그 사람 방들의 방 기본 시각(digest_hour, -1=방 전체 끔) 중 가장 이른 것.
  방 요약은 그날 처음 필요할 때 1번 만들어 digest_cache(JSON)에 → 받는 사람 수·재시작과 무관하게 AI 는 방마다 하루 1번 (만드는 중 죽으면 그날은 안 만듦).
  1:1 막힘(Forbidden) → digest_log status=forbidden, 그 사람이 등록한 방에 금액 없는 안내 + [▶️ 1:1 열기](?start=cfg_<방>) 방마다 7일 1번.
  이용 중인 방만, 대화는 nonce 태그 안 데이터·flagged 제외.
- "누구 얘기인지": `addressee.py` 가 단서(답장·태그·이름·방금 입장)만 모으고 AI 가 판단. 평가 `python tools/ai_eval_addressee.py` (42상황: 인사·말투·제재 확인 버튼, 목표 엉뚱한 멘션 0).

## 제재·AI 도구 권한
- 도구 목록 = AI 가 할 수 있는 일 (`tools.available(role, settings, in_dm)`): 방 관리 도구(where=room)는 1:1 에서 안 보임,
  오너 도구(owner_rooms·owner_sanction, where=owner_dm)는 오너의 1:1 에서만. 없는 도구로는 '된다'고 못 함 (프롬프트 규칙 8).
- 제재는 확인 카드 한 장에 최대 5명(names). 봇에게 그 방 '사용자 차단' 권한이 없으면 카드 없이 이유를 돌려줌.
  오너 1:1 카드: 오너만 누름, [실행+방에 안내]/[실행만]/[취소], 방 안내는 정해진 문구+사유.
- 자동 입장 인사 뒤 10분 안의 AI 인사 요청은 중복으로 봄. 제3자(신입)에게 하는 인사는 방 기본 말투.

- 오너 보고(도배 뮤트·사칭·자동 밴·CAS)에 바로가기 버튼 [풀기][1일 연장][내보내기](무기한 사칭 뮤트엔 연장 없음)/[밴 해제] (ow:, 오너만).
- 오너 1:1 방 기록 조회 `owner_room_log(room, kind=sanction|attempt|requests|all, days)`: 정해진 조회만 (자유 SQL 없음 —
  SQLite mode=ro 만으론 ATTACH·temp 표로 쓰기가 됨, 필요하면 authorizer+ATTACH 0+progress 제한으로). 제재 요청은 mod_log
  ask_<종류>(거절 사유·카드), 확인 버튼은 press_<종류>(취소·거절). 기록을 읽은 답변에선 `tools.READ_ONLY` 도구만 (ctx.tainted:
  멤버 글 속 지시가 제재·전송·외부 검색으로 못 이어지게). AI 답은 흔한 TLD 도메인 주소 지우고 링크 미리보기 끔.
  ask_/press_ 는 방 관리자 기록(.기록·🗂️)엔 안 보임(db.NOT_AUDIT), 기록 실패해도 버튼·카드 계속(db.audit), 거절 연타는 1줄.
- 장시간 게임 알림(`gametime.py`, 🎮 메뉴, 기본 꺼짐): 멤버가 보낸 게임 명령(/ ! 🎲 또는 gt_cmds 목록) 시각으로 연속 세션
  (gt_gap 분 쉬면 새로) → 10분 job 이 gt_hours 넘은 세션 1번 알림(방 + 알릴 관리자 1:1). 조치 notify/button(뮤트 버튼)/auto(자동 뮤트,
  관리자·자유 멤버 제외). 다른 게임봇 결과는 텔레그램이 봇끼리 안 보여줘서 못 봄. AI 도구 game_alert(요청한 관리자가 받음), 📤 다른 방에 복사.
- 예약 작업(`cron.py`, schedules 확장 action post/remind/ai · kind once): 알람·AI 작업. AI 작업은 에이전트가 아니라 **스킬 파이프라인**
  (summary 대화 요약·search 격리 웹검색·stats 통계·write 글쓰기) — 실행 때 AI 에 도구 없음(plan-then-execute), 출력 필터·미리보기 끔,
  만든 관리자가 더는 관리자가 아니면 끔. 말로 예약(schedule_task)은 방에 확인 카드(menu 토큰, 요청자만), 1:1 🗓️ 에서 ⏰/🤖 입력·📤 복사.
- 예약 시각: 매일 HH:MM · 매주 월,수 HH:MM · 평일/주말 HH:MM (kind weekly, at_time '월수 10:00', `announce.is_due` 가 요일 확인) ·
  반복 N분 · N분 뒤 · 오늘/내일/MM-DD HH:MM. AI 스킬 joins = 입장·퇴장 통계(AI 없음, 지난 실행 이후·최대 31일).
- 방에 올리는 확인 카드(schedule_task·alert_rule)는 `menu.lasting_token` 으로 DB(menu_tokens)에도 저장 — 봇 재시작(배포) 뒤에도 30분 유효
  (실제 사례: OTC 방 예약 카드 → 1분 뒤 배포 재시작 → [✅ 예약] 만료). 예약 deliver room/me(만든 관리자 1:1), action post=정해진 글.
- menu 토큰: 다른 사람이 누르면 '요청한 사람만' 으로 거절하고 토큰은 남김 (방에 뜬 카드를 남이 눌러 무효화 못 하게).
- 게임 중: 게임 답(AI 답에 단 답장 포함)이 AI 보다 먼저, AI 엔 진행 중 게임 단서(ai_hint). start_game 성공이면 AI 답 안 보냄(ctx.quiet).
- 스팸 명단 차단 안내에 [↩️ 차단 풀기](이 방에선 다시 안 막음, chat_state cas_ok:<id>) [🔕 끄기] ('사용자 차단' 권한 관리자).
- 알림 규칙(`rules.py`, 🔔 메뉴, AI 도구 alert_rule + 확인 카드): 규칙 = 데이터(정해진 부품만, AI 는 말→부품 번역).
  언제 keyword/user/join/quiet · 하면 dm/call/post · 쿨다운(한 문장 UPDATE 로 차지)·하루 30번·만든 사람 관리자 재확인. 제재 부품 없음.
- 대표님 비서(`my_rooms`, where=dm): 1:1 에서 내가 '지금' 관리자인 방들 24시간 현황(코드) / 방 하나 대화 요약(도구 없는 AI) → tainted.
  프롬프트 user 에 '지금 대화: 1:1/그룹방' 한 줄 (1:1 인지 몰라서 '여기선 못 해요' 하던 것).
- 연달아 보낸 말은 BURST_SECONDS(1.5초) 안이면 마지막 메시지에 한 번 답(요청 합침) · '천천히' 안내 1분 1번 · user_rate_per_min 기본 5.
  테스트는 fakes.py 가 BURST_SECONDS=0.
- 끝말잇기(`games.py`): 판정은 AI 가 아니라 `sodam/data_files/words_ko.txt.gz`(표준국어대사전 표준어 명사 32만, CC BY-SA —
  WORDS_LICENSE.md, 41ways/kkeutmal 가공본) → 즉시·비용 0. 자유 모드 '끝말잇기' = 선착순(await 전에 상태 변경), 늦은 답 🙈·사전 없음 🤔·
  중복 🤨 은 글 대신 반응, 봇 말 GAP_SECONDS(3초) 간격(그룹 분당 20개 한도). 차례 모드 '끝말잇기 차례' = [🙋 참가](wc:j)·[▶️ 바로 시작](wc:go,
  시작한 사람·관리자) → 차례(20→8초) → 시간 초과 탈락 → 마지막 1명 우승 +5+2×참가자. 첫 낱말·봇 낱말은 한방 단어 안 씀(can_follow).
  봇 낱말은 흔한 말 먼저, 없으면 사전 전체(_BY_FIRST) — 시뮬레이션 주고받기 중앙값 24→35번.
  소담이 수 = `wordbot.py` 클로드코드식: LLM(guard 모델)이 도구 find_words(후보+'이을 말 수')/check_word/play 로 골라 한마디,
  코드가 why_not 으로 다시 검사('안 됨' 돌려주고 재선택), 6초 넘거나 실패하면 code_move(난이도대로). 생각 중(thinking) 들어온 답은 🙈.
  후보 12개를 요청에 미리 넣어 보통 호출 1번(실측 2번 1,125→1번 ~640 토큰). 캐시용으로 프롬프트 부풀리기는 오히려 비쌈 — 하지 말 것.
  사전에 있는데 첫 글자가 틀린 답('wrong') = 사람당 게임 1번 '지금은 X→Y' 힌트(무시당하는 느낌 방지). 두음(dueum)은 ㄹ 제11·12항만.
  사전 로드는 GameManager.start 에서 방 등록 전(to_thread) — 로딩 중 방이 '게임 중'으로 묶이지 않게. 타이머: 봇 수 뒤 set_timer 를
  say 보다 먼저(전송 실패해도 게임 안 멈춤), 울린 타이머는 _timer 에서 떼어 cancel_timer 가 안내를 못 끊음, wc:go 는 _start 의
  joining 검사로 참가 타이머와 경쟁해도 한 번만 시작. 방마다 게임 독립(chat_id 키) — '한쪽 멈춤'은 대개 틀린 첫 글자 무시였음.
  🧩 기능 화면: wc_ai(AI 선수 끄면 비용 0)·wc_level 쉬움/보통/어려움(어려움 = 이을 말 가장 적은 흔한 말 먼저).
  참고 설계: On9 Word Chain(텔레그램, 참가·차례·탈락). 테스트는 fakes.py 가 GAP_SECONDS=0.
- 프롬프트 캐시: prompt_cache_key=sodam:<purpose>, 보관 기본 24h(gpt-5.4·mini 실제 호출로 적중 확인, 추가 요금 없음). 1024 토큰 미만 요청은
  캐시 안 됨(끝말잇기 한 수 등). 기능별 적중은 counters prompt:<purpose>/cached:<purpose> → 오너 `.사용량` 에 '캐시 안 된 입력이 많은 기능'.
  실측(대화 10번): agent 65~71% — 나머지는 매번 바뀌는 대화 기록·요청이라 구조상 한계.
- 비용 점검: `python tools/usage_report.py [--day] [--db]` (읽기 전용) — 대화·AI·예산·모델별 요금(`sodam/costs.py` 요금표)·방별·기능별·DB.
  모델별 토큰은 counters m:<모델>:in/cached/out/calls (2026-09-27 21시대부터), 그 전 날짜는 범위만. 웹 검색 web_search_calls.
- 하루 1번 봇에게 관리 권한 없는 방을 오너에게 알림 (job_rights). 하트비트는 텔레그램 get_webhook_info 성공 때만 기록.

- 네트워크 재전송(`util.send_retry`): 방에 보이는 글은 연결 자체가 실패한 경우만(확실히 안 보내짐) 다시, 응답만 끊긴 경우는
  이미 보내졌을 수 있어 안 보냄(중복 방지). 오너 보고는 둘 다 다시. 이름 순찰은 한 차례 40초 제한.

## 게임 돈·안정성 (2026-09-27 심층 감사 수정, tests/test_fix_games_audit.py · 뮤테이션 23개)
- 열린 베팅 = DB `casino_open`(방·사람별 합계): `debit('bet:')` 이 같은 트랜잭션에서 더하고, 정산·환불은 `credit(..., close=베팅)` 으로
  지급과 함께 뺀다 (`settle` 이 자동으로). **새 게임도 베팅은 take_bet, 끝은 settle 또는 credit(close=) — 안 닫으면 재시작 때 환불(=포인트 생김).**
  봇 시작 때 `casino.startup` → `core.recover_open` 이 남은 걸 환불(kill -9·컨테이너 회수 대비). 게임 맡는 프로세스만(bot_role != main).
  옛 `casino_open_stakes` 는 옛 버전이 남긴 줄만 시작 때 환불 (멀티는 더 안 씀).
- 파산 구제: 먼저 SWEEPS(만료 카드 판 정산) → 지급 UPDATE 한 문장에서 잔액<최소·열린 베팅 없음·하루 1번을 다시 확인 (당첨과 경쟁 불가).
  차감(debit) 자체가 오류면 오류 환불 안 함 (안 뺀 돈 환불 방지).
- 카드 판: 정산 실패면 판을 다시 열어 둠(_settle_hand) · 중간 화면 수정 실패면 새 메시지로, 그것도 안 되면 옛 버튼 유지(_live) ·
  만료 정리(sweep)는 돈만 바로, 화면은 뒤에서(_BG) · q.answer 실패해도 결과 연출. 하이로우 상금은 정수 센트.
- 멀티: 경마는 정산 먼저 → 경주 GIF · 그래프는 차트가 방에 뜬 뒤부터 배수 오름(그 전 !스톱 = '곧 출발') · 꽝도 pay(p, 0) 으로 닫음 ·
  🎫 참가 확인은 출발 때 deleteMessages 한 번 · TICK 4초 · 🛑 팝업은 html.unescape.
- 전송 제한기 `sodam/ratelimit.py` ChatRateLimiter: 그룹 429 는 그 방만 쉼 (PTB 기본은 봇 전체 멈춤 → 한 방 게임이 다른 방까지 멈춤).
- 끝말잇기: on_text 가 예상 못 한 오류면 게임 종료(방이 '게임 중'으로 묶이지 않게) · stale = (지난 문제, 방금 사람 말) 로 늦은 답 🙈,
  thinking 은 소담이 말이 올라갈 때까지 · 차례 안내는 pace 뒤 그때 차례로(seq, 머리글 합침), 타이머는 안내 직전 ·
  차례 모드 버튼 `wc:j:<gid>`/`wc:go:<gid>` (지난 판 버튼 거절).

## AI 비용·작업 기록 (관측, tests/test_budget.py · test_agentlog.py · 뮤테이션 17개)
- 하루 예산은 **달러**: `llm._record` 가 요금(`costs.usd_micro`, 정수 마이크로달러, 요금표에 없는 모델은 기본 모델 요금 → 그것도 없으면
  가장 비싼 요금)을 counters `usd_micro`(chat_id=0 전체)·`room_usd_micro`(방·1:1) 에 셈. 한 번의 `db.atomic` 으로 모든 카운터를 같이.
  `DAILY_USD_BUDGET`(기본 8, 0=끔) 넘으면 BudgetExceeded("usd"). 토큰 예산은 `.env` 에 `DAILY_TOKEN_BUDGET` 을 **적은 경우만** (캐시 입력도
  전액으로 세서 2M 토큰(=실제 $1~3)에 막히던 것). 웹 검색은 호출당 $0.01 을 extra_micro 로.
- 방 하루 한도 = 오너 요금제(chat_state `ai_usd_plan` 센트, $0.5/1.5/3/5, 기본 $1.50 — 방 설정이 아니라서 `.설정변경`·AI 도구로 못 바꿈)
  × 방 관리자 `ai_room_budget_pct`(10~100%, 기본=상한 100 → 줄이기만, 저장값도 잘라 씀). 오너 1:1 은 방 달러 한도 없음. 토큰 한도(60만)도 그대로.
- AI 작업 기록(`agentlog.py`, 표 agent_runs, 14일): run_agent 1번 = 1줄 (요청 200자·방식·도구 호출(이름+인자 요약, 비밀값 모양 가림)+결과 120자·
  answered/tool_only/empty/error/budget·토큰·요금). 토큰은 ContextVar `agentlog.current` 로 → 도구 안에서 부른 AI(웹검색 등)도 같은 실행에,
  중첩 실행은 끝날 때 바깥에 더함. 기록 실패는 삼킴(답은 그대로). 정리는 넣을 때 한 시간에 한 번 같은 atomic 안에서.
- 화면(`panels/agentlog.py`): 오너 메인 📒 AI 비용·기록(m:al 오늘 요금·예산 %·많이 쓴 곳 / alr·alv 모든 방 기록 / alp·alq·alqs 방 요금제).
  방 관리자 그룹 허브 📒(m:alg·agv) = 자기 방 기록만, **금액은 안 보임**(한도의 %만), 한도 % 프리셋. 저장된 글은 전부 esc.
- 오너 `.사용량`(commands.py)은 아직 토큰 기준 — 달러는 📒 화면·`tools/usage_report.py`(방별 요금 vs 요금제).

## 멤버 타임라인·상황 분석 (`insight.py`, tests/test_insight.py · 뮤테이션 15개)
- 사실은 코드가 기록에서만 셈, 해석은 AI. 도구 안에서 AI 호출 없음. '위험' 같은 딱지 없이 숫자·날짜만.
- 🧾 타임라인(👥 멤버 목록의 🧾 번호 → `m:mbt:<방>:<사람>:<정렬>`, `panels/insight.py`, 방 관리자만·그 방 멤버만):
  처음 본 날·입장·이름/아이디 변경(namehist)·메시지 7일/30일/보관 전체·답장(db.reply_stats)·링크 글·경고·제재(mod_log 90일, 자동/관리자)·
  최근 24시간 vs 그 전 7일 하루 평균. **📋 기록된 사실과 🧠 AI 기억 메모(member_memory+notes, '확인 안 됨')는 칸을 나눔.**
- AI 도구(전부 read_only, 방 관리자·where=room): `member_timeline`(메모 포함 → ctx.tainted) · `analyze_member(name, days≤30)`
  = 신호 + **코드 규칙 추천 후보**(`insight.suggest`: 경고 3↑ 뮤트 1일, 2 뮤트 1시간, 뮤트 뒤 또 경고 1일, 링크 글 3↑ 뮤트 1시간,
  사기 의심 2↑ 밴 검토, 이름 2번↑ 사칭 확인, 급증 도배 확인, 관리자는 없음) — 추천만, 실행은 기존 warn/mute/ban 확인 카드 ·
  `room_changes(today|24h|7d)` = 입장·나감(member_left, 사람당 마지막 1번)·내보낸 기록·시간대(7d 는 날짜) 메시지/관리 기록(reports.MOD_ITEMS 분류)/
  예약 공지·가장 바쁜 3칸, 원인은 '추정'. 오너 1:1 `owner_room_insight(room, kind)` (tainted).
- 사람별 링크 '삭제' 기록은 아직 없음(moderation 은 방 단위 counters rep_link 만) → 링크는 '링크 들어간 글' 수(삭제 여부 무관).
  mod_log `link_del`(대상=사람)을 남기면 insight 가 자동으로 '링크 지움'으로 셈.

## DB 안전 규칙
- 여러 문장 쓰기는 반드시 `db.atomic(fn)` (DB 스레드에서 SAVEPOINT 로 전부/전무). 연결을 코루틴들이 같이 써서
  `conn.execute` 여러 번 + `commit` 은 반쯤 된 변경이 다른 코루틴 commit 에 묻어 저장될 수 있음 (포인트만 빠지고 원장 없음 등).
- 디스크 가득 참(SQLITE_FULL): DB 는 안 깨지고 쓰기만 실패. 대화·멤버 기록은 `handlers._record` 로 실패해도 관리·명령·AI 계속.
  `diskguard.py` 1시간마다 여유 500MB 미만이면 기록 30일로 긴급 정리 + 오너 알림(6시간 1번).
- 보관: 대화·요청 90일, 카운터 400일, AI 기록 14일. 결제·포인트 원장·이름·관리 기록은 안 지움. PRAGMA synchronous=NORMAL(WAL 안전), journal_size_limit 64MB.

## 검색
- 대화·자료 검색 = SQLite FTS5 + 두 글자 겹침 색인(`search.py`, trigram 은 3글자부터라 '원두' 못 찾음). messages_fts/knowledge_fts 의
  rowid = 원본 id, 기록·정리·자료 삭제 때 같은 db.atomic 안에서 함께. DB 열 때 빠진 색인 자동 채움. 여러 낱말: 맞는 낱말 수 → bm25 → 최신.
- 답장 관계: messages.reply_to_msg_id/reply_to_user (예전 DB 는 `_migrate` 가 컬럼 추가). 기록은 `handlers.reply_ref`
  (포럼 토픽 첫 글 답장 제외, 채널·익명 관리자 글은 사람 없음) — 그룹·1:1·봇 AI 답(부른 사람에게). 답장받은 사람도 users 에 upsert.
  AI chat_log·방 흐름 요약 입력에 `[시각] 이름(ID) ↩상대: 글` (`prompt.reply_mark`, 이름은 db.REPLY_JOIN 한 번, 20자, 글 인용 없음).
  집계 `db.reply_stats(chat_id, since, user_id=None)` = (from_id, to_id, n), 봇·자기 답장 제외. 수정된 메시지는 다시 기록 안 함.
- 다음 단계(필요할 때): 관계 표(사기 계정 무리·평판) → 의미 검색(sqlite-vec). PostgreSQL 은 여러 서버로 나눌 때.

## 클라우드 세션 서버 실행
- 봇은 커밋된 코드만 `/home/user/sodam_run` 에 풀어서 실행 (작업 중 파일이 서버에 섞이지 않게). data·.env 는 원본 폴더를 링크.
  갱신: `git --work-tree=/home/user/sodam_run checkout HEAD -- sodam tests tools docs requirements.txt` 후
  `pkill -f "venv/bin/python -m sodam$"` (봇만 끄면 감시가 새 코드로 다시 켬 — 봇을 직접 nohup 으로 켜지 말 것, 두 개 뜸).
- 감시: `tools/supervise.sh` (죽으면 5초 뒤·하트비트 3분 멈추면 재시작, flock 으로 하나만). 세션 시작 훅(~/.claude/settings.json)이 켬.
- 감시 자체를 바꿔 다시 켤 땐 `pkill -f` 에 명령줄 글자를 쓰지 말 것 (그 명령을 실행한 셸도 같이 죽음). PID 로 kill → 훅 명령으로 다시 켬.
- 컨테이너가 회수되면 안에서는 못 살림 → Routine '소담 봇 생존 확인'(매시 49분)이 세션을 깨워 훅이 다시 켬. 최대 약 1시간 공백.

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
- 새 AI 도구 = 자기 모듈(패널 파일 등, 자동 로드)에서 `tools.register_tool(Tool(...), read_only=...)` (tools.py 수정 불필요).
- 검증 하네스: `tests/harness.py` 가 4개 역할(오너·TG 관리자·봇관리자·멤버)로 모든 버튼을 BFS 로 눌러
  예외·answer 1회·64바이트·HTML·권한 누출을 검사 (`tests/test_harness.py`). 화면 확인: `python tools/render_screens.py` → docs/SCREENS.md.
  패널별 하네스 데이터는 `tests/seed_<이름>.py` 에서 `harness.SEEDERS.append(async fn(svc))` (자동 로드) → 깊은 화면까지 누른다.
- 테스트 러너는 `tests/test_*.py` 자동 발견. `python tests/run_all.py [모듈명]`.
