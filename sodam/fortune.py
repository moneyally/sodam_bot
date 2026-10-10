"""🔮 오늘의 운세 (설계 docs/SPORTS_ENGAGE_DESIGN.md §3 E, 2026-10-11 FOX 고객 '출석 … 오늘의운세 오늘의픽').

- 사람 + 한국 날짜로 고정 (같은 날 몇 번 물어도 같음 — 조사: omikuji·슬랙 운세 봇이 다 이렇게, 다시 뽑기로 좋은 운 고르기 방지).
- 문구는 우리가 직접 쓴 조각 조합 (공개 운세 데이터는 저작권이 불분명 — 조사 2026-10-11). AI·DB·돈 0원.
- 돈·도박 권유 문구 없음 (재물운도 '아끼기·정리' 쪽으로). 재미용이라고 끝에 표시.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime

GRADES = (("🌟 대길", 10), ("😊 길", 30), ("🙂 소길", 35), ("😐 평", 20), ("🌧 조심", 5))    # (이름, 비율 %)

OVERALL = {
    "🌟 대길": ("오늘은 뭘 해도 술술 풀리는 날이에요.", "미뤄 둔 일을 시작하기 딱 좋은 날이에요.", "주변에서 좋은 소식이 들려올 거예요."),
    "😊 길": ("작은 행운이 여러 번 찾아와요.", "웃을 일이 평소보다 많은 하루예요.", "먼저 건넨 한마디가 좋은 인연이 돼요."),
    "🙂 소길": ("무난하게 흘러가는데 저녁쯤 기분 좋은 일이 있어요.", "천천히 가면 결국 원하는 데 닿아요.", "평소 하던 대로가 정답인 날이에요."),
    "😐 평": ("특별한 일 없이 조용한 하루, 쉬어 가기 좋아요.", "큰 결정은 내일로 미뤄도 괜찮아요.", "작은 실수만 조심하면 무난해요."),
    "🌧 조심": ("말 한마디를 한 번 더 생각하면 지나가는 날이에요.", "서두르면 꼬여요. 오늘은 느긋하게.", "물건 잃어버리기 쉬운 날, 지갑·폰 한 번 더 확인!"),
}
MONEY = ("충동구매만 참으면 지갑이 든든해요.", "작은 돈이 새는 곳을 찾아 막기 좋은 날.", "누가 밥 한 끼 사 줄지도 몰라요.",
         "정리하다 잊고 있던 쿠폰을 찾을 수 있어요.", "오늘 아낀 돈이 다음 주에 빛을 봐요.", "가계부를 한 번 열어 보면 좋은 날.")
LOVE = ("연락 기다리던 사람에게서 답이 와요.", "솔직한 한마디가 마음을 움직여요.", "오래된 친구와의 대화가 즐거워요.",
        "작은 배려가 크게 돌아와요.", "괜히 설레는 일이 하나 생겨요.", "혼자만의 시간이 오히려 좋은 날.")
HEALTH = ("물 많이 마시기!", "스트레칭 5분이면 몸이 가벼워져요.", "오늘은 일찍 자는 게 최고의 보약.",
          "가벼운 산책이 기분까지 살려 줘요.", "눈이 피곤한 날, 화면 좀 쉬어 가요.", "따뜻한 음식이 잘 맞는 날이에요.")
COLORS = ("빨강", "주황", "노랑", "초록", "하늘", "파랑", "보라", "분홍", "흰색", "검정", "금색", "민트")
TIPS = ("오늘의 한마디: 고마워요를 한 번 더.", "오늘의 한마디: 일단 해 보자.", "오늘의 한마디: 천천히 가도 괜찮아.",
        "오늘의 한마디: 웃으면 반은 해결.", "오늘의 한마디: 먼저 인사하기.", "오늘의 한마디: 오늘 할 일은 오늘.",
        "오늘의 한마디: 쉬는 것도 실력.", "오늘의 한마디: 좋은 건 나누면 커져요.")


def _seed(user_id: int, day: str) -> bytes:
    return hashlib.sha256(f"sodam-fortune:{user_id}:{day}".encode()).digest()


def _pick(seq, b: int):
    return seq[b % len(seq)]


def today(tz, user_id: int, now: float | None = None) -> dict:
    """사람·날짜로 고정된 운세 조각."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    h = _seed(user_id, day)
    roll, acc, grade = h[0] * 100 // 256, 0, GRADES[-1][0]
    for name, pct in GRADES:
        acc += pct
        if roll < acc:
            grade = name
            break
    return {"day": day, "grade": grade, "overall": _pick(OVERALL[grade], h[1]), "money": _pick(MONEY, h[2]),
            "love": _pick(LOVE, h[3]), "health": _pick(HEALTH, h[4]), "number": h[5] % 45 + 1, "color": _pick(COLORS, h[6]),
            "tip": _pick(TIPS, h[7])}


def text(f: dict, name: str = "") -> str:
    """방·1:1 에 보낼 글 (HTML, name 은 이미 esc/mention 된 것)."""
    head = f"🔮 {name}님의 오늘 운세" if name else "🔮 오늘의 운세"
    return (f"{head} ({f['day'][5:].replace('-', '/')})\n<b>{f['grade']}</b> — {f['overall']}\n"
            f"💰 {f['money']}\n💕 {f['love']}\n💪 {f['health']}\n"
            f"🍀 행운 숫자 {f['number']} · 행운 색 {f['color']}\n{f['tip']}\n<i>재미로 보는 운세예요</i>")


def short(f: dict) -> str:
    """버튼 팝업용 (텔레그램 알림창 200자 안)."""
    return f"🔮 {f['grade']} — {f['overall']}\n💰 {f['money']}\n🍀 숫자 {f['number']} · 색 {f['color']}"[:200]
