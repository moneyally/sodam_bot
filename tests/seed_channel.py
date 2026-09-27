"""하네스 데이터: 📢 채널 2개(활성·빠짐) — 방과 연결·새 글 알림, 예약 글, 올린 글(고치기), 가입 신청, 구독자 기록,
TG 관리자의 쓰던 초안(HTML 특수문자·버튼 2줄). 하네스 권한으로는 오너·TG 관리자가 채널 관리자."""
import json
import time
from datetime import datetime, timedelta

import harness

CH, CH_OFF = -1009000000001, -1009000000002


async def seed(svc):
    db, now = svc.db, int(time.time())
    await db._write("INSERT INTO channels(chat_id, title, username, active, can_post, can_edit, can_delete, can_invite, "
                    "linked_chat_id, added_by, room_id, notify, join_mode, updated) "
                    "VALUES(?, '대표님 <채널> & 공지', 'boss_news', 1, 1, 1, 1, 1, ?, ?, ?, 1, 'manual', ?)",
                    (CH, harness.CHAT, harness.TG, harness.CHAT, now))
    await db._write("INSERT INTO channels(chat_id, title, active, updated) VALUES(?, '예전 채널', 0, ?)", (CH_OFF, now))
    buttons = json.dumps([[{"t": "📞 문의", "u": "https://t.me/boss"}, {"t": "사이트", "u": "https://example.com/?a=1&b=2"}],
                          [{"t": "앱 열기", "u": "tg://resolve?domain=boss"}]], ensure_ascii=False)
    body = "<b>이벤트</b> 안내 &amp; <a href=\"https://example.com\">링크</a>\n<blockquote>한정</blockquote>"
    for uid in (harness.TG, harness.OWNER):
        await db._write("INSERT INTO composer_drafts(user_id, target, body, buttons, updated) VALUES(?,?,?,?,?)",
                        (uid, CH, body, buttons, now))
    sched_draft = await db._write("INSERT INTO composer_drafts(user_id, target, body, state, updated) VALUES(?,?,?,?,?)",
                                  (harness.TG, CH, "<i>매일 아침</i> 공지", "scheduled", now))
    await db._write("INSERT INTO channel_sched(chat_id, draft_id, kind, at_time, created_by) VALUES(?,?,'daily','09:00',?)",
                    (CH, sched_draft, harness.TG))
    snap = {"body": body, "media_type": None, "media_id": None, "buttons": buttons, "preview": 1}
    await db._write("INSERT INTO channel_msgs(chat_id, msg_id, ts, text, draft, by_user) VALUES(?, 501, ?, ?, ?, ?)",
                    (CH, now - 3600, "이벤트 안내 & 링크 <한정>", json.dumps(snap, ensure_ascii=False), harness.TG))
    await db._write("INSERT INTO channel_msgs(chat_id, msg_id, ts, text) VALUES(?, 502, ?, '사람이 올린 글')", (CH, now - 60))
    await db._write("INSERT INTO channel_joinreqs(chat_id, user_id, name, ts) VALUES(?, 880001, '신청 <자> & 1', ?)", (CH, now))
    await db._write("INSERT INTO channel_joinreqs(chat_id, user_id, name, ts) VALUES(?, 880002, '두번째', ?)", (CH, now))
    for i in range(10):
        day = (datetime.now(svc.cfg.tz) - timedelta(days=i)).strftime("%Y-%m-%d")
        await db._write("INSERT INTO channel_stats(chat_id, day, n) VALUES(?,?,?)", (CH, day, 1000 - i * 7))


harness.SEEDERS.append(seed)
