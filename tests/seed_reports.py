"""하네스 데이터: 📊 활동 리포트 화면이 비어 있지 않게 (관리 기록·입장·그림 카운터). 방 이름의 HTML 특수문자는 harness 기본 제목이 이미 가짐."""
import time
from datetime import datetime

import harness
from fakes import TZ, fake_user


async def seed(svc):
    db, now = svc.db, int(time.time())
    for action, detail in (("mute", "30분 / 도배"), ("warn", "금지어 사용"), ("captcha", "5분"),
                           ("captcha_pass", ""), ("ban", "CAS 스팸DB 등록 계정"), ("raid", "30분 / 60초에 10명")):
        await db._write("INSERT INTO mod_log(chat_id, actor_id, target_id, action, detail, ts) VALUES(?,?,?,?,?,?)",
                        (harness.CHAT, None, 880100, action, detail, now - 3600))
    await db.upsert_user(fake_user(880101, "새멤버 <b>"))
    await db.touch_member(harness.CHAT, 880101, joined=True)
    await db.bump(datetime.now(TZ).strftime("%Y-%m-%d"), harness.CHAT, "image", 2)


harness.SEEDERS.append(seed)
