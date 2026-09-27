"""하네스 데이터: 📊 활동 리포트 화면이 비어 있지 않게 (관리 기록·입장·그림 카운터). 방 이름의 HTML 특수문자는 harness 기본 제목이 이미 가짐.
🧠 하루 요약: 등록한 대표님(harness.TG, ensure_trial) 이름에 HTML 특수문자 · 1:1 막힘 기록(⚠️ 줄) · 개인 시각 21시(⏰ 화면 ●).
하루 요약 1:1 메시지 끝의 [⏰ 받는 시각](m:dgp:new)·[🔕 그만 받기](m:dgp:stop)도 시작점으로."""
import time
from datetime import datetime

from telegram import InlineKeyboardMarkup

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
    from sodam import reports
    await db.upsert_user(fake_user(harness.TG, "김대표 <&>"))
    await db._write("INSERT OR REPLACE INTO digest_log(user_id, day, rooms, status, ts) VALUES(?,?,?,?,?)",
                    (harness.TG, "2026-01-01", str(harness.CHAT), "forbidden", now))
    await reports.set_pref(db, harness.TG, reports.ALL_ROOMS, hour=21)
    harness.add_buttons(InlineKeyboardMarkup([reports.PREF_ROW]))


harness.SEEDERS.append(seed)
