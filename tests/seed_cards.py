"""하네스 데이터: 🗳 AI 확인 카드 (sodam/cards.py) — [✅][❌][✅ + 오늘은 확인 생략] 을 역할마다 눌러 봄.

TG 관리자가 요청한 알림 규칙 카드 (저장은 형식 오류로 실패 → 다른 시더의 규칙 개수는 그대로, 결과 한 줄만 남음).
요청자가 아닌 역할은 '요청한 사람만', 요청자는 먼저 누른 버튼 하나만 처리되고 나머지는 '만료'.
"""
import harness

from sodam import cards


async def seed(svc):
    spec = {"trig": "bogus", "arg": "<b>입금</b> & x", "action": "dm", "text": "", "who": "all", "cooldown": 10}
    harness.add_buttons(await cards.card(svc, harness.TG, harness.CHAT, "alert_rule", "rule_save", "rule_no", spec,
                                         ok_label="✅ 만들기"))


harness.SEEDERS.append(seed)
