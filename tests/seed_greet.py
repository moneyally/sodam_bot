"""하네스 데이터: 인사 편집기에 인사말·사진·URL 버튼을 넣어 두어 보기·삭제·미리보기 화면까지 누르게 한다."""
import harness


async def seed_greeting(svc):
    cid = harness.CHAT
    # 이스케이프 검사용으로 <태그> 와 & 를 섞음
    await svc.db.set_setting(cid, "greet_template", "{names} 대표님 환영해요! <공지> & 규칙 꼭 읽어주세요 🙌")
    await svc.db.set_setting(cid, "greet_media_type", "photo")
    await svc.db.set_setting(cid, "greet_media_id", "AgACAgFakePhotoId")
    await svc.db.set_setting(cid, "greet_buttons", [["📢 공지 채널", "https://t.me/sodam_notice"],
                                                    ["규칙 & 안내", "https://example.com/rules?a=1&b=2"]])


harness.SEEDERS.append(seed_greeting)
