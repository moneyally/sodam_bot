"""하네스 시드: 🎙 오너 통화 기록(m:vclg → m:vclv) 까지 눌리게 통화 1개 + 대화 3줄."""

import harness
from sodam.voice import store


async def seed(svc):
    db = svc.db
    call_id = await store.call_started(db, harness.CHAT, harness.TG)
    await store.call_ended(db, call_id, 75, "bye", 1, 1)
    await store.add_line(db, call_id, harness.CHAT, "user", harness.TG, "소담아 <b>통계</b> 알려줘")
    await store.add_line(db, call_id, harness.CHAT, "tool", None, "chat_stats {} → 관리자만")
    await store.add_line(db, call_id, harness.CHAT, "sodam", None, "관리자만 들을 수 있어요 & 미안해요")


harness.SEEDERS.append(seed)
