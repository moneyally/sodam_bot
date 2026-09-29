"""하네스 데이터: ⚽ 스포츠 알림 — 리그·팀 구독이 있어야 🗑 지우기 버튼(토큰)까지 눌러 봄."""
import harness


async def seed(svc):
    for league, team, label in (("epl", "", "EPL"), ("epl", "Tottenham Hotspur", "토트넘 (EPL)")):
        await svc.db._write("INSERT OR IGNORE INTO sports_follow VALUES(?, ?, ?, ?, 1, 0)", (harness.CHAT, league, team, label))


harness.SEEDERS.append(seed)
