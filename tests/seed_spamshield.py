"""하네스 데이터: 🛡️ 스팸 방패 — 기록만 모드 + 걸렸을 글 목록(상세·처리·맞음/틀림 버튼)까지 눌리게.

이름·글에 HTML 특수문자와 링크·@아이디를 넣어 이스케이프·눌리지 않게 바꾸기까지 검사한다.
"""
import json
import time

import harness

from sodam import spamshield


async def seed(svc):
    db, cid = svc.db, harness.CHAT
    await db.set_setting(cid, "spamshield_mode", "shadow")
    now = int(time.time())
    rows = [
        # (사람, 이름, 글, 사기, 광고, 사실, 상태, 지움, 피드백, 보낸 수)
        (880101, "코인<b>고수</b> & 친구", "무료 리딩방 https://evil.xyz/join?a=1&b=2 @spam_channel 로 오세요 <script>",
         0.93, 0.2, ["초대 링크: t[.]me/+abc", "다른 방 2곳에서 다른 계정 3명도 올린 외부 링크 도메인: evil[.]xyz (48시간 안)"],
         None, 0, None, 0),
        (880102, "평범한 사람", "안녕하세요 USDT 시세 얼마예요?", 0.81, 0.05, [], None, 0, "w", 0),
        (880103, "지갑 뿌리기", "여기로 보내면 2배 TQrZ9wBzVh9Yr4Uc3r1jWZgLvX5mRj8kAb", 0.99, 0.1,
         ["지갑주소", "수익 약속 + 1:1 유도"], "m", 1, None, 2),
    ]
    for i, (uid, name, text, scam, ad, facts, status, deleted, fb, sent) in enumerate(rows):
        await db._write(
            "INSERT INTO spamshield_verdicts(chat_id, user_id, msg_id, name, kind, mode, ts, scam, ad, ai, facts, strong, "
            "excerpt, would_alert, sent, status, deleted, feedback) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, 1, ?, 1, ?, ?, ?, ?)",
            (cid, uid, 500 + i, name, "edit" if i == 2 else "msg", "shadow" if i < 2 else "alert", now - 60 * i,
             scam, ad, json.dumps(facts, ensure_ascii=False), spamshield.defang(text), sent, status, deleted, fb))
    # 걸리지 않은 검사 기록 (통계 '검사' 숫자만)
    await db._write("INSERT INTO spamshield_verdicts(chat_id, user_id, msg_id, name, mode, ts, scam, ad, ai) "
                    "VALUES(?, 880104, 600, '보통', 'shadow', ?, 0.05, 0.9, 'ok')", (cid, now))
    await spamshield.trust(db, cid, 880105, harness.TG)


harness.SEEDERS.append(seed)
