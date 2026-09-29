---
name: sodam-deploy
description: 소담 봇(sodam_bot) 코드를 서버에 올리는 전체 흐름 — 전체 테스트 → 커밋·푸시(claude/button-panels) → PR 만들고 main 에 합치기 → 서버 자동 갱신(10분 타이머, 서버에서 테스트 약 20분)을 원격 점검 창구로 실제 버전까지 확인 → 한국어로 보고. 사용자가 "배포", "올려", "서버 반영됐어?", "배포됐어?", "PR 합쳐", "서버 갱신 실패" 메시지(🛠 서버 갱신 실패 tests_failed …)를 보여 줄 때, 또는 기능·버그 수정이 끝나 서버에 반영해야 할 때 꼭 이 스킬을 쓴다.
---

# 소담 배포

소담은 `main` 에 합쳐지면 서버(Hetzner VPS)의 `sodam-autoupdate.timer` 가 10분마다 새 커밋을 받아 **서버에서 전체 테스트(Nice 10, 약 17~20분)** 를 돌리고, 통과하면 봇을 재시작한다. 실패하면 봇은 이전 버전으로 계속 돌고 오너 1:1 로 "🛠 서버 갱신 실패 … FAIL <테스트이름>" 이 간다.
그래서 "합쳤다" ≠ "배포됐다". 사용자는 과장 보고를 아주 싫어한다 — **서버 버전을 직접 확인한 것만 "배포 완료"**, 아니면 "서버 반영 대기(미확인)" 라고 쓴다.

## 1. 올리기 전
1. 전체 테스트: `timeout 1700 nice -n 19 /home/user/venv/bin/python tests/run_all.py 2>&1 | grep -E "^FAIL|전체 결과"` (백그라운드로, 약 20분). "전체 결과: 모두 통과" 가 아니면 올리지 않는다.
2. 새 기능·버그 수정엔 테스트 + 뮤테이션(→ `sodam-mutation-test` 스킬).
3. `git status` 로 비밀값(.env, data/, 토큰) 안 들어갔는지 확인. 저장소는 공개.

## 2. 커밋·PR·합치기
```bash
git add <파일들> && git commit -m "<한국어 요약>

Co-Authored-By: ...(시스템 안내의 attribution 줄)" && git push -u origin claude/button-panels
python .claude/skills/sodam-deploy/scripts/pr.py create "<제목>" "<본문: 실제 사례·원인·고침·테스트>"   # → PR 번호
python .claude/skills/sodam-deploy/scripts/pr.py merge <번호>
```
- `GH_TOKEN` 환경변수 필요. PR 본문 끝에 시스템 안내의 PR attribution 줄.
- 커밋·PR·코드 주석에 모델 이름을 쓰지 않는다.

## 3. 서버 반영 확인 (필수)
```bash
python .claude/skills/sodam-deploy/scripts/wait_deploy.py <커밋 sha 앞 7자리> [--timeout 3600]
```
- 원격 점검 창구(`tools/diag.py health`)의 `version` 이 그 커밋(또는 그 뒤 커밋)이 될 때까지 기다리고, `logs unit=sodam-autoupdate` 에서 `FAIL`·`tests_failed` 가 보이면 바로 멈추고 실패 테스트 이름을 출력한다.
- 서버는 한 번에 한 커밋씩 테스트한다: 앞 커밋 테스트 중이면 그다음 차례(각 약 20분). 기다리는 동안 사용자에게 예상 시각을 한국어로.
- 오래 걸리면 `send_later` 로 30~40분 뒤 확인 예약.

## 4. 서버 테스트 실패 때
- 서버는 느리고(Nice 10, 봇과 CPU 경쟁) 컨테이너보다 느리다. **벽시계 시간에 기대는 테스트**(asyncio.wait_for 몇 초, 실행 시간 < N초)가 서버에서만 떨어지는 일이 잦다 → 로그에서 그 테스트를 찾아 원인을 확인하고, 시간 기준을 넉넉히(예: 5초→60초, CPU 시간으로 재기) 고친 뒤 다시 1~3 단계.
- 실패 로그: `python tools/diag.py logs unit=sodam-autoupdate lines=400 | grep -B25 "FAIL <이름>"`.

## 5. 보고 (한국어만, 짧게)
| 항목 | 상태 |
|---|---|
| 코드 | 합쳐짐 (PR #N) |
| 서버 버전 | 실제 값 (확인 시각) |
| 반영 | ✅ 확인 / ⏳ 대기(예상 HH:MM) / ❌ 실패(테스트 이름) |
