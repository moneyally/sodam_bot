"""하네스 데이터: 👋 퇴장 인사 — 켜 두고 문구(HTML 글자·{count})·URL 버튼을 넣어 삭제 확인·미리보기까지 누르게 한다."""
import harness


async def seed_farewell(svc):
    cid = harness.CHAT
    await svc.db.set_setting(cid, "farewell_mode", "on")
    await svc.db.set_setting(cid, "farewell_delete_after", 600)
    await svc.db.set_setting(cid, "farewell_template", "{name} 대표님 <잘 가요> & 현재 {count}명")
    await svc.db.set_setting(cid, "farewell_buttons", [["규칙 & 안내", "https://example.com/r?a=1&b=2"]])


harness.SEEDERS.append(seed_farewell)
