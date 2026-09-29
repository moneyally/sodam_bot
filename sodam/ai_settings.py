"""AI 사교 기능 설정 키 (settings.py 를 고치지 않고 register_setting 으로 추가).

tools.py 가 import 할 때 먼저 불러서, change_setting 도구의 키 목록에도 들어가게 한다.
"""
from .settings import register_setting

register_setting("ai_memory", True, "AI 멤버 기억")                   # 멤버가 자기 얘기한 것 기억
register_setting("ai_room_memory", True, "AI 방 흐름 기억")            # 방 대화 흐름 요약
register_setting("ai_follow_up", True, "이름 없이 이어 말하기")         # 방금 대화한 사람이 이어서 물으면 호출어 없이 답
register_setting("ai_comeback", "wit", "욕 받아치기",                  # 소담이 욕받이가 되지 않게 (오너 요청 2026-09-29)
                 choices={"wit": "wit", "센스": "wit", "욕없이": "wit", "mirror": "mirror", "똑같이": "mirror", "욕으로": "mirror"},
                 choice_labels={"wit": "센스로 받아치기(욕 없이)", "mirror": "똑같이 욕으로"})
register_setting("ai_spicy", False, "19금 드립 받아치기")               # 기본 꺼짐: 켠 방에서만 야한 드립에 은유 수준으로 (노골적 X)
register_setting("ai_chime_in", False, "AI 먼저 끼어들기")             # 기본 꺼짐: 답 없는 질문·아침 인사에 가끔 한마디
register_setting("ai_chime_gap_min", 120, "끼어들기 최소 간격(분)", range_=(30, 1440))
register_setting("ai_chime_daily", 4, "끼어들기 하루 최대", range_=(1, 20))
# 방 하루 토큰 한도: 기본값이 상한이라 방 관리자는 줄이기만 가능 (0=무제한·큰 값으로 전체 예산을 못 씀). llm 도 저장값을 상한으로 자름
ROOM_TOKENS_MAX = 3_000_000
register_setting("ai_room_daily_tokens", ROOM_TOKENS_MAX, "방 하루 AI 토큰 한도", range_=(10_000, ROOM_TOKENS_MAX))
# 방 관리자가 바꿀 수 있는 비용 관련 설정의 상한 (웹검색은 호출마다 요금, 분당 호출은 폭주 방지)
register_setting("web_search_daily", 30, "하루 웹검색 한도", range_=(0, 100))
register_setting("room_rate_per_min", 20, "방 분당 AI 호출", range_=(1, 60))
register_setting("image_daily", 5, "하루 이미지 만들기(장)", range_=(0, 20))   # 0 = 끔. 한 장이 대화 수십 번 값
