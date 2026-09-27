"""🎞️ 사건 재현 · 🔬 설정 시뮬레이터 AI 도구 등록 (sodam/replay.py 를 불러오면 register_tool). 버튼 화면은 없음.

panels 패키지가 이 폴더 모듈을 전부 불러오므로, 여기서 import 만 하면 봇 시작 때 도구가 등록된다.
"""
from .. import replay  # noqa: F401  AI 도구 build_incident_case·simulate_setting_change 등록
