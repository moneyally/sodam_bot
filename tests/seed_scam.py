"""하네스 데이터: 🕵️ 사기 의심 검사 — 키워드 목록(삭제 토큰 버튼)과 '괜찮음' 신뢰 목록(비우기 버튼)이 보이게."""
import time

import harness

from sodam import scamguard


async def seed(svc):
    await svc.db.set_setting(harness.CHAT, "scam_guard", True)
    await scamguard.add_keywords(svc.db, harness.CHAT, ["수익 보장", "리딩<방> & DM"])
    await svc.db._write("INSERT OR IGNORE INTO scam_trust(chat_id, user_id, by_id, ts) VALUES(?, ?, ?, ?)",
                        (harness.CHAT, 880003, None, int(time.time())))


harness.SEEDERS.append(seed)
