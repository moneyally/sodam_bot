---
name: sodam-guide-docs
description: 소담 공식 안내서(sodam/guide/*.md — 소담이 AI 도구 sodam_guide 로 읽고 방에서 답하는 지식 문서)를 쓰거나 고치는 규칙. 소담 기능·가격·결제·버튼·명령이 바뀌었을 때, 사용자가 "소담이가 ○○를 모르던데", "가격 안내", "문서 만들어", "안내서", "지식그래프", "소담이 알게 해줘" 라고 할 때, 새 기능을 추가해서 소담이 그 기능을 설명할 수 있어야 할 때 꼭 이 스킬을 쓴다.
---

# 소담 안내서 문서

소담은 자기 자신에 대한 질문(가격·결제·데려오기·기능·모드·명령어…)을 받으면 `sodam_guide(topic)` 도구로 `sodam/guide/<key>.md` 한 개를 읽고 답한다. 문서가 틀리면 소담이 방에서 틀린 말을 하므로, **코드와 대조한 사실만** 쓴다 (버튼 이름·메뉴 경로·기본값을 실제 코드에서 확인).

## 파일 모양
```markdown
---
title: 요금·무료 체험
summary: 방 하나당 {days}일(1달) {price} USDT예요. …   ← 한 문장, 해요체, 자리표시자 가능
tags: 가격, 얼마, 요금, …                                ← 사용자 입말·붙여쓴 꼴, 다른 문서와 겹치면 안 됨
related: payment, invite, features                        ← 이어지는 주제 (지식 그래프)
audience: 모두 | 방 관리자
type: reference | howto | policy | faq | index
---

## 한 줄 답
(해요체, 이것만 읽어도 답이 되게)

## 하는 법 / 내용 (번호·표, 한 문장 한 정보)

## 조건·예외 ("~면 → ~")

## 소담이 하지 말 것 (지어내지 말 것, 주소·링크 금지 등)

## 관련 문서
- [제목](key.md) · …        ← 사람용(GitHub). AI 에겐 빠지고 related 로 감
```
- 한 줄 `키: 값` 만 (파서가 YAML 목록 못 읽음).
- 숫자는 설정값 자리표시자: `{price} {days} {trial} {free_ai} {invoice_min} {voice_min} {call_min}` (`panels/guidebook.py render`). 새 숫자가 필요하면 render 에 추가.
- 버튼은 화면 글자 그대로 `[💳 구독하기]` — 테스트가 코드에 그 글자가 있는지 검사.
- 본문 1,500자 안. 입금 주소·링크·다른 봇 이름·"곧/이번 달부터" 금지.
- 새 문서는 `index.md` 목차에 링크.

## 고친 뒤
1. `tests/test_guidebook.py` 의 `EVAL`(실제 질문 → 기대 문서)에 새 질문 2~3개 추가.
2. `/home/user/venv/bin/python tests/run_all.py test_guidebook` — 규칙 위반(태그 겹침·숫자 직접·코드에 없는 버튼·목차 누락)을 잡아 준다.
3. `find()` 가 엉뚱한 문서를 고르면 태그를 조정 (tags 는 붙여쓴 질문에 부분 문자열로 매칭, 2점).
4. 배포는 `sodam-deploy`.
