"""하네스 데이터: 📝 AI 방 안내 (panels/instructions.py) — 안내가 있는 화면(🗑·👁)까지 눌리도록, 글에 HTML 글자(<b>, &)."""
import harness

from sodam import ai_instructions


async def seed(svc):
    await ai_instructions.save(svc.db, harness.CHAT, "room", "<b>사장님</b>이라고 부르고 & 짧게", harness.TG)
    await ai_instructions.save(svc.db, harness.CHAT, "owner", "밝게 <i>말해요</i> & 짧게", harness.OWNER)


harness.SEEDERS.append(seed)
