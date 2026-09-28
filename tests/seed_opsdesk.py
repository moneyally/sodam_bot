"""하네스 데이터: 📥 처리할 일 · 🧭 운영센터 · 💸 비용 예측 (panels/opsdesk.py) 이 깊은 곳까지 눌리도록.

안 누른 확인 카드(예약) · 처리 전 사기 의심 알림 여러 개(📥 2쪽) · 실패/꺼진 예약(있는 예약·지워진 예약) · AI 사용량 90% · 지난 날 요금 기록.
이름·제목에 HTML 글자(<b>, &)를 넣어 이스케이프까지 검사. (이상징후·하루 요약 못 받음은 seed_anomaly·seed_reports 가 넣음)
"""
import time
from datetime import datetime, timedelta

import harness

from sodam import costs, menu
from sodam.ai_settings import ROOM_TOKENS_MAX
from sodam.llm import ROOM_TOKENS

SCAMS = 7


async def seed(svc):
    db, now, cid = svc.db, int(time.time()), harness.CHAT
    # 방에 뜬 예약 확인 카드 (✅/❌ 둘 다 남음 = 아직 안 누름)
    spec = {"when": ["daily", "09:00", None, None], "action": "remind", "skill": None, "text": "<b>회의</b> & 점검",
            "title": "", "deliver": "room"}
    await menu.lasting_token(svc, harness.TG, cid, "cron_save", spec, 1800)
    await menu.lasting_token(svc, harness.TG, cid, "cron_no", None, 1800)
    for i in range(SCAMS):
        await db._write("INSERT INTO scam_alerts(chat_id, user_id, msg_id, name, reason, deleted, ts) VALUES(?,?,?,?,?,?,?)",
                        (cid, 770000 + i, 50 + i, f"<b>사기</b> & 계정{i}", "지갑 주소 <a> & 수익 보장", 0, now - 600 - i))
    # 예약 실패: #1 = test_announce_panel 시더가 곧 만드는 첫 예약 (여기서 예약을 만들면 그 테스트의 개수가 바뀜) · 지워진 예약
    for why, ref, ago in (("send", 1, 900), ("send", 1, 300), ("creator", 999_999, 200)):
        await db._write("INSERT INTO ops_events(chat_id, kind, ref, detail, ts) VALUES(?,?,?,?,?)",
                        (cid, f"sched_{why}", ref, "Forbidden <x>", now - ago))
    tz = svc.cfg.tz
    day = datetime.now(tz)
    await db.bump(day.strftime("%Y-%m-%d"), cid, ROOM_TOKENS, ROOM_TOKENS_MAX * 9 // 10)   # 오늘 90%
    for k in (1, 2):   # 지난 날 요금 (하루 평균이 '기록된 지난 날'로 잡히게)
        d = (day - timedelta(days=k)).strftime("%Y-%m-%d")
        await db.bump(d, 0, costs.USD, 3_000_000)
        await db.bump(d, 0, "tokens", 2_000_000)
        await db.bump(d, cid, costs.ROOM_USD, 1_500_000)


harness.SEEDERS.append(seed)
