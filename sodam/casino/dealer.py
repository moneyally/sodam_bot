"""딜러 소담: 게임 결과에 한마디 (AI 호출 없이 대사 모음에서 → 비용 0, 즉시).

방 말투가 자유분방이면 반말, 아니면 존댓말. 결과 크기(대박·승·본전·패·올인 패)에 따라 다른 대사.
"""
from __future__ import annotations

import secrets

LINES = {
    "polite": {
        "jackpot": ["와… 대박입니다 대표님!! 오늘 운 다 쓰신 거 아니에요? 🎊", "잭팟! 이 방 전설 되셨습니다 👑",
                    "딜러 손이 떨리네요… 크게 가져가십니다 🔥"],
        "win": ["축하드립니다 대표님, 가져가세요 😎", "오 감 좋으신데요? 한 번 더 가시죠!", "딜러가 졌습니다, 깔끔하네요 👏",
                "역시 대표님, 흐름 타셨네요."],
        "push": ["본전입니다. 숨 고르고 다시 가시죠.", "무승부! 판은 계속됩니다 🤝"],
        "lose": ["아쉽습니다… 다음 판은 대표님 차례예요.", "딜러가 이번엔 챙겼습니다 😏", "흐름 한 번 끊고 가시죠. ⛏ 채굴도 있어요!",
                 "괜찮습니다, 원래 한 판은 내주는 거예요."],
        "allin_lose": ["올인이었는데… 🫡 파산하면 <code>!파산</code> 으로 다시 일어서요.", "과감하셨습니다… 오늘은 딜러가 웃네요."],
    },
    "free": {
        "jackpot": ["야 미쳤다 대박!!! 🎊 오늘 로또 사러 가라 ㅋㅋ", "잭팟 터졌다!! 이 방 레전드 등극 👑", "딜러 멘붕… 다 털어가네 🔥"],
        "win": ["오 먹었네 ㅋㅋ 가져가~ 😎", "감 좋은데? 한 판 더 가자!", "아 딜러가 졌다 인정 👏", "흐름 탔네 대표야 ㅋㅋ"],
        "push": ["본전 ㅋㅋ 다시 가보자", "무승부~ 판은 계속된다 🤝"],
        "lose": ["아 아깝다 ㅋㅋ 다음 판 가자", "이번 판은 딜러 꺼 😏", "쉬었다 가~ ⛏ 채굴이나 한 판?", "괜찮아 원래 한 판은 주는 거야"],
        "allin_lose": ["올인 ㅋㅋㅋ 🫡 거지 되면 <code>!파산</code> 해", "간이 크네… 오늘은 딜러가 웃는다 ㅋㅋ"],
    },
}


def line(style: str, bet: int, payout: int, balance_after: int) -> str:
    """결과에 맞는 딜러 한마디 (HTML)."""
    tone = LINES["free" if style == "free" else "polite"]
    if payout >= bet * 10:
        kind = "jackpot"
    elif payout > bet:
        kind = "win"
    elif payout == bet:
        kind = "push"
    elif balance_after < 100:
        kind = "allin_lose"
    else:
        kind = "lose"
    choices = tone[kind]
    return "🃏 <b>딜러 소담</b>: " + choices[secrets.randbelow(len(choices))]
