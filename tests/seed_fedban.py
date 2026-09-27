"""하네스 데이터: 🛡️ 공동 차단 명단 목록이 비어 있지 않게 (이 방이 올린 항목 + 다른 방이 올린 항목).
→ '이 방 표시 빼기'(관리자)·'완전 삭제'(오너) 토큰 버튼까지 눌린다. 관리자 1:1 알림의 [🚫 밴][무시](m:fbx)도 시작점으로."""
import time

import harness

from sodam import fedban


async def seed(svc):
    db, now = svc.db, int(time.time())
    for uid, name, chat, reason in ((880001, "코인사기 <봇> & 친구", harness.CHAT, "코인 사기 DM <링크>"),
                                    (880002, "스팸계정", -1009990000001, "도박 광고 도배")):
        await db._write("INSERT OR IGNORE INTO fedban_entries(user_id, name, ts) VALUES(?, ?, ?)", (uid, name, now))
        await db._write("INSERT OR IGNORE INTO fedban_reports(user_id, chat_id, reason, ts) VALUES(?, ?, ?, ?)",
                        (uid, chat, reason, now))
    await db._write("INSERT OR IGNORE INTO fedban_seen(chat_id, user_id, n_rooms, ts) VALUES(?, ?, 1, ?)",
                    (harness.CHAT, 880001, now))   # 이 방 관리자에게 알림을 보낸 사람
    harness.add_buttons(fedban.alert_kb(harness.CHAT, 880001))


harness.SEEDERS.append(seed)
