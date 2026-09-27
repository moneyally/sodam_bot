"""하네스 데이터: 🔔 알림 규칙 — 폭주로 멈춘 규칙([▶️ 계속 실행][⏸ 오늘 중지])·🔎 미리 보기까지 눌리게.
낱말·대화에 HTML 글자를 넣어 이스케이프까지 검사."""
import time

import harness

from sodam import rules


async def seed(svc):
    now = int(time.time())
    rid = await rules.add(svc, harness.CHAT, harness.TG, {"trig": "keyword", "arg": "<입금>&", "action": "dm"})
    await rules.add(svc, harness.CHAT, harness.TG, {"trig": "quiet", "arg": "3", "action": "dm"})
    for i in range(5):
        await svc.db.log_message(harness.CHAT, 20, i, f"<입금>& 확인 {i}")
    await svc.db._write("INSERT INTO alert_rule_pauses(rule_id, chat_id, ts, until, hits) VALUES(?, ?, ?, ?, ?)",
                        (rid, harness.CHAT, now, now + rules.PAUSE_SECONDS, 42))


harness.SEEDERS.append(seed)
