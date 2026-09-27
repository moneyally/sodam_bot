"""하네스용: 🧾 멤버 타임라인이 비어있지 않게 (메시지·답장·링크·경고·제재·기억 메모, HTML 특수문자 포함)."""
import time

import harness

from sodam import memory


async def seed(svc):
    db, now = svc.db, int(time.time())
    for i in range(6):
        await db.log_message(harness.CHAT, harness.MEMBER, None, f"안녕 <b>{i}</b> & 링크 https://ex.xyz/{i}",
                             ts=now - 3600 * i, reply_to_user=harness.TG if i % 2 else None)
    await db._write("INSERT INTO warnings(chat_id, user_id, by_id, reason, ts) VALUES(?,?,?,?,?)",
                    (harness.CHAT, harness.MEMBER, 999, "<script>&경고", now - 3600))
    await db._write("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                    (harness.CHAT, harness.TG, harness.MEMBER, "mute", "1시간 / a<b>&c", now - 1800))
    await memory.add_facts(db, harness.CHAT, harness.MEMBER, ["커피 <좋아함> & 라떼"])


harness.SEEDERS.append(seed)
