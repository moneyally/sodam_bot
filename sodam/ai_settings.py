"""AI 사교 기능 설정 키 (settings.py 를 고치지 않고 register_setting 으로 추가).

tools.py 가 import 할 때 먼저 불러서, change_setting 도구의 키 목록에도 들어가게 한다.
"""
from .settings import register_setting

register_setting("ai_memory", True, "AI 멤버 기억")                   # 멤버가 자기 얘기한 것 기억
register_setting("ai_room_memory", True, "AI 방 흐름 기억")            # 방 대화 흐름 요약
register_setting("ai_follow_up", True, "이름 없이 이어 말하기")         # 방금 대화한 사람이 이어서 물으면 호출어 없이 답
register_setting("ai_chime_in", False, "AI 먼저 끼어들기")             # 기본 꺼짐: 답 없는 질문·아침 인사에 가끔 한마디
register_setting("ai_chime_gap_min", 120, "끼어들기 최소 간격(분)", range_=(30, 1440))
register_setting("ai_chime_daily", 4, "끼어들기 하루 최대", range_=(1, 20))
register_setting("ai_room_daily_tokens", 600_000, "방 하루 AI 토큰 한도(0=무제한)", range_=(0, 50_000_000))
