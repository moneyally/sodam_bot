"""하네스 데이터: AI 자료·관리 기록·규칙·봇 관리자 (sodam/panels/ai.py · log.py · room.py 화면을 깊이까지 누르게)."""
import harness
from harness import BOTADM, CHAT, MEMBER, TG

from sodam import knowledge


async def seed(svc):
    db = svc.db
    for i in range(1, 10):  # 한 쪽(8개)을 넘겨서 쪽 넘김 버튼까지
        await knowledge.add_document(db, CHAT, f"안내 자료 {i}", f"{i}번 안내입니다. 영업시간은 10시~22시예요. " * 3,
                                     "메뉴 입력", TG)
    await knowledge.add_document(db, CHAT, "가격표 <특가> & 이벤트", "A 상품 <b>1만원</b> & B 상품 2만원\n" * 40,
                                 "price.txt", TG)
    await knowledge.add_document(db, 0, "공통 FAQ", "모든 방 공통으로 쓰는 자주 묻는 질문 모음입니다.", "직접 입력", 7)
    await db.set_setting(CHAT, "rules", "1. 욕설 금지 <b>진짜로</b>\n2. 광고 & 홍보 금지")
    await db.set_bot_admin(CHAT, BOTADM, True)
    for i in range(6):
        await db.log_mod(CHAT, TG, MEMBER, "warn", f"도배 {i}회 <script>&")
    await db.log_mod(CHAT, None, MEMBER, "mute", "무기한 / 캡차 실패")
    await db.log_mod(CHAT, TG, None, "setting", "captcha_enabled=False")
    await db.log_mod(CHAT, TG, None, "setting", "banned_word+=스팸,광고")
    await db.log_mod(CHAT, TG, 424242, "ban", "모르는 사람")
    await db.log_mod(CHAT, TG, None, "knowledge_add", "#3 가격표")


harness.SEEDERS.append(seed)
