"""하네스 데이터: 🎮 장시간 게임 알림 — 관리자 1:1 알림의 [🔇 N시간 뮤트][👌 괜찮음] → [🔊 풀기](m:gtb)를 시작점으로."""
import harness

from sodam import gametime


async def seed(svc):
    hours = (await svc.db.get_settings(harness.CHAT))["gt_mute_hours"]
    harness.add_buttons(gametime.button_kb(harness.CHAT, 880005, False, hours))


harness.SEEDERS.append(seed)
