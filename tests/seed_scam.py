"""하네스 데이터: 🕵️ 사기 의심 검사 — 키워드 목록(삭제 토큰 버튼)과 '괜찮음' 신뢰 목록(비우기 버튼)이 보이게.
관리자 1:1 사기 의심 알림의 [🗑][🚫][🔇][✅](m:sgx)도 시작점으로 (지우기 뒤·뮤트 뒤 화면까지)."""
import time

import harness

from sodam import scamguard


async def seed(svc):
    await svc.db.set_setting(harness.CHAT, "scam_guard", True)
    await scamguard.add_keywords(svc.db, harness.CHAT, ["수익 보장", "리딩<방> & DM"])
    await svc.db._write("INSERT OR IGNORE INTO scam_trust(chat_id, user_id, by_id, ts) VALUES(?, ?, ?, ?)",
                        (harness.CHAT, 880003, None, int(time.time())))
    aid = await svc.db._write("INSERT INTO scam_alerts(chat_id, user_id, msg_id, name, reason, ts) VALUES(?,?,?,?,?,?)",
                              (harness.CHAT, 880004, 5, "수상한 <계정> & 친구", "지갑주소 + DM 유도", int(time.time())))
    harness.add_buttons(scamguard.alert_kb(harness.CHAT, aid, False))


harness.SEEDERS.append(seed)
