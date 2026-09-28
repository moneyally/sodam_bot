"""하네스 데이터: 💡 기능 요청 (panels/featreq.py) 이 깊은 곳까지 눌리도록.

열린 요청 여러 개(목록 2쪽) · 여러 명이 묶인 요청 · 진행 중·완료(메모)·안 함 · 1:1 에서 온 요청.
요청 글·이름·방 이름에 HTML 글자(<b>, &)를 넣어 이스케이프까지 검사.
"""
import time

import harness

from sodam import featreq


async def seed(svc):
    db, cid, now = svc.db, harness.CHAT, int(time.time())
    names = {harness.MEMBER: "멤버 <b>굵게</b> & 친구", harness.TG: "관리자", 555001: "외부 <i>", 555002: "손님&"}
    base = [("입장 때 규칙 퀴즈", "<script>alert(1)</script> 입장하면 규칙 퀴즈 & 통과해야 말하기"),
            ("출석 체크", "매일 출석 체크 & 포인트"), ("투표 만들기", "방 투표 <b>기능</b>"),
            ("음성 메시지 요약", ""), ("유튜브 링크 요약", "링크 주면 요약"), ("생일 알림", "멤버 생일 축하"),
            ("환율 알림 & <계산>", "USDT 환율 알림"), ("공지 번역", "영어 공지 번역")]
    for i, (s, d) in enumerate(base):   # 한 사람 하루 5번 한도라 요청자를 나눔
        uid = harness.MEMBER if i < 3 else 600000 + i
        await featreq.submit(db, uid, names.get(uid, f"요청자{i} <u>"), cid, s, d, now=now - 3600 * (i + 1))
    for uid in (harness.TG, 555001, 555002):   # 다른 사람들이 같은 요청 → 👍 묶음 (한 명은 1:1 에서)
        await featreq.submit(db, uid, names[uid], cid if uid != 555002 else uid, "입장할 때 규칙 퀴즈", "퀴즈 내기", now=now - 60)
    await featreq.submit(db, harness.MEMBER, names[harness.MEMBER], cid, "입장 때 규칙퀴즈", "", now=now - 30)   # 같은 사람 → 한 번 더
    rows = await featreq.list_groups(db, featreq.OPEN, "n", 50)
    by = {r["summary"]: r["id"] for r in rows}
    await featreq.set_status(db, by["출석 체크"], "doing", harness.OWNER)
    await featreq.set_status(db, by["생일 알림"], "done", harness.OWNER, "메모 <b>굵게</b> & 끝")
    await featreq.set_status(db, by["공지 번역"], "wont", harness.OWNER)


harness.SEEDERS.append(seed)
