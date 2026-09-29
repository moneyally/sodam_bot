---
name: sodam-mutation-test
description: 소담 봇 저장소에서 테스트를 쓰고 "뮤테이션"(고친 코드를 일부러 되돌리거나 망가뜨려 테스트가 실패하는지 확인)으로 테스트가 진짜 버그를 잡는지 검증하는 방법과, 전체 테스트(tests/run_all.py) 돌리는 법. 소담 코드를 고치거나 기능을 더한 뒤, "테스트", "뮤테이션", "검증", "제대로 잡나", "전체 테스트" 가 나오면, 그리고 배포 전에는 항상 이 스킬을 쓴다.
---

# 소담 테스트 + 뮤테이션

## 테스트 쓰기
- 파일: `tests/test_<주제>.py`, `from fakes import …` / `from fake_llm import Room` 패턴을 기존 테스트에서 따라 한다 (FakeBot·FakeMsg·FakeQuery·make_db·make_svc·Room().open(admins=…)).
- 실제 사례를 docstring 에 적는다 (날짜·방·증상). 운영 데이터 모양(예: mtproto 는 dict 목록)을 그대로 흉내 낸다 — 가짜가 실제와 다르면 버그를 놓친다.
- 실행: `/home/user/venv/bin/python tests/run_all.py test_<이름>` → 마지막 줄 "전체 결과: 모두 통과".
- 네트워크 없는 테스트: 실제 응답 샘플을 `tests/data/…` 에 작게 저장해 쓴다.
- **벽시계 시간에 기대지 않는다** (서버는 Nice 10 이라 느림). 필요하면 CPU 시간·넉넉한 기준.

## 뮤테이션
고친 줄마다 "이걸 되돌리면 테스트가 실패하나?"를 확인. 살아남은 뮤테이션 = 테스트 구멍 → 테스트를 보강한다.
```bash
bash .claude/skills/sodam-mutation-test/scripts/mutate.sh test_<이름> <파일> '<sed 식>' "<설명>"
```
- 스크립트는 **파일 경로 전체로** 백업·복원한다. (주의: 예전에 `/tmp/$(basename)` 로 백업했다가 `sodam/announce.py` 와 `sodam/panels/announce.py` 가 같은 이름이라 서로 덮어써 코드가 날아간 적 있음.)
- sed 가 안 맞으면 "NOT APPLIED" — 들여쓰기·따옴표를 확인하고 다시. 적용 안 된 뮤테이션을 "잡혔다"고 세지 않는다.
- 기능마다 의미 있는 뮤테이션 5~10개. 결과를 "실패 N개 <= 설명" 목록으로 보고.

## 전체 테스트
```bash
timeout 1700 nice -n 19 /home/user/venv/bin/python tests/run_all.py 2>&1 | grep -E "^FAIL|전체 결과"
```
- 약 20분 → 백그라운드로 돌리고 그동안 다른 일. 결과를 확인하기 전엔 "통과"라고 말하지 않는다.
- 서버에서만 떨어지는 테스트는 대개 시간 기준 문제 (→ `sodam-deploy` 4번).
