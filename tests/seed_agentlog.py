"""하네스 데이터: 📒 AI 작업 기록·💵 AI 비용 화면이 깊은 곳(상세·다음 쪽·요금제)까지 눌리도록 기록·사용량을 넣는다.
요청·도구 결과에 HTML 글자(<b>, &)를 넣어 이스케이프도 검사한다."""
from datetime import datetime

import harness
from sodam import agentlog, costs


async def seed(svc):
    db = svc.db
    for i in range(9):   # 이 방 9건 → 2쪽 (한 쪽 8건)
        run = agentlog.Run(harness.CHAT, harness.MEMBER, "call", f"소담아 <b>질문</b> {i} & 가격 알려줘 " + "길게 " * 40)
        run.purpose = "agent:member"
        if i % 3 == 0:
            run.step("web_search", '{"query": "<script>&", "api_key": "sk-abcdefghijklmnopqrstuv"}', "검색 결과 <i>요약</i> & 끝")
            run.step("search_knowledge", '{"query": "가격표"}', "자료 없음")
        run.add("gpt-5.4", 12_000, 9_000, 300, costs.usd_micro("gpt-5.4", 12_000, 9_000, 300))
        await agentlog.save(db, run, ("answered", "tool_only", "budget", "error")[i % 4])
    dm = agentlog.Run(harness.MEMBER, harness.MEMBER, "call", "1:1 <질문>")   # 1:1 은 오너 화면에만
    await agentlog.save(db, dm, "answered")
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    await db.bump(day, 0, costs.USD, 2_345_678)
    await db.bump(day, 0, "tokens", 1_900_000)
    await db.bump(day, harness.CHAT, costs.ROOM_USD, 1_200_000)
    await db.bump(day, harness.MEMBER, costs.ROOM_USD, 30_000)


harness.SEEDERS.append(seed)
