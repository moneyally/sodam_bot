"""하네스 데이터: 🧭 이상징후 감지 — 확인 전 알림(상세·보안 강화·무시 버튼까지)과 처리된 알림, 보안 강화 중 상태(🔓 끄기)가 보이게.
이름·링크에 HTML 글자를 넣어 이스케이프까지 검사."""
import json
import time

import harness

from sodam import anomaly


def _detail(n: int) -> str:
    users = [[880100 + i, f"<b>에어드랍</b> & {i}", f"airdrop_{i}", i % 2 == 0, True] for i in range(n)]
    return json.dumps({"joins": n, "base": 0.3, "same_name": n, "cluster": "에어드랍", "recent_pct": 50,
                       "links": [["evil<x>.xyz", 8, 3], ["t.me/+AbC&d", 4, 2]], "flood": [3, 2], "raid": True,
                       "users": users, "more": 0,
                       "timeline": [[int(time.time()) - 300, n, 8, 3]],
                       "reasons": ["입장 몰림: 10분에 12명 (평소 10분 평균 0.3명)", "같은 링크 반복: evil<x>[.]xyz 8회 · 3명"]},
                      ensure_ascii=False)


async def seed(svc):
    now = int(time.time())
    for status in (None, "ignored"):
        await svc.db._write("INSERT INTO anomaly_alerts(chat_id, ts, day, score, summary, detail, sent, status) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (harness.CHAT, now - 60, time.strftime("%Y-%m-%d"), 75,
                             "입장 +12 · 비슷한 이름 12명 · 같은 링크 8회 <&>", _detail(12), 1, status))
    await svc.db.set_state(harness.CHAT, anomaly.HARDEN_KEY,
                           {"since": now - 60, "until": now + 3600, "set": {"forward_filter": "newbie"},
                            "restore": {"forward_filter": "off"}, "raid": False, "by": harness.TG})
    anomaly._due.pop(harness.CHAT, None)


harness.SEEDERS.append(seed)
