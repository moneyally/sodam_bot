"""🧭 모델 길 고르기 (하이브리드 라우팅, 설계 docs/COST_ROUTING.md) — 비용 0 인 코드 판정 + 작은 모델이 스스로 올려 보내기.

길 3개:
- heavy  = 지금까지와 같음 (cfg.model = gpt-5.4, 관리자·오너·분석 요청은 추론). 일·분석·사진·1:1 관리.
- banter = 큰 모델, 추론 없이. 욕 받아치기(mirror)·19금 드립이 켜진 방에서 소담에게 욕·드립 — 방의 '명장면'이라 말맛 우선.
- light  = 작은 모델(cfg.light_model, 기본 gpt-5.4-mini), 추론 없이. 잡담·인사·짧은 질문·조회.
  light 로 돌다가 모델이 쓰기 도구(LIGHT_TOOLS 밖)를 부르거나 escalate 도구를 부르면 → 그 도구는 실행하지 않고
  같은 요청을 heavy 로 처음부터 한 번 다시 (agent._run). 판정이 틀려도 일은 큰 모델이 하게 되는 안전망.

판정은 신호 함수 목록(SIGNALS) 조합 — 낱말 목록 하나에 기대지 않고, 새 신호는 함수 하나 더하면 됨.
오너가 방마다 바꿈 (chat_state ROUTE_KEY, 방 관리자 못 바꿈): hybrid(기본) / saver(말싸움도 작은 모델) / best(전부 큰 모델).
.env AGENT_LIGHT_MODEL 이 비면 전부 heavy (기능 끔).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .permissions import Role
from .prompt import INSULT_RE, SEX_RE

ROUTE_KEY = "ai_route"                      # chat_state(방): 오너가 정한 길 방식
MODES = {"hybrid": "⚖️ 나눠 쓰기 (기본)", "saver": "💸 절약 (말싸움도 작은 모델)", "best": "🧠 최고 (전부 큰 모델)"}
DEFAULT_MODE = "hybrid"

# light 에서 실행해도 되는 도구 = 이 서버 데이터 읽기(tools.READ_ONLY, 실행 때 합침) + 바깥 조회만.
# 이 밖의 도구(제재·설정·전송·그림·영상·게임·기억 저장…)를 부르면 heavy 로 올려 보냄.
LIGHT_READ = frozenset({"web_search", "sports", "news_headlines", "sodam_guide", "lookup_user", "my_ids"})
# 가벼운 쓰기 (본인 것·인사·게임 — 멤버도 쓰는 도구, 틀려도 피해 작음. 09-30 실측 올려 보내기 16+건).
# light 가 이걸 실행한 뒤 올려 보내면 heavy 에 '이미 한 일' 로 알림 (agent.DONE_NOTE — 두 번 하지 않게)
LIGHT_WRITE = frozenset({"greet_members", "start_game", "save_my_note", "set_my_style", "forget_my_memory",
                         "feature_request", "tag_alerts", "point_game", "stop_tag_all"})   # 멈추기는 빨라야 (큰 모델로 안 올림)
LIGHT_EXTRA = LIGHT_READ | LIGHT_WRITE
ESCALATE_TOOL = "ask_senior"
ESCALATE_SCHEMA = {"type": "function", "function": {
    "name": ESCALATE_TOOL,
    "description": ("이 요청이 네가 확실히 하기 어려운 일일 때만 부른다: 여러 단계 판단·분석·설정/제재/예약/전송 같은 실행·"
                    "애매해서 틀리면 곤란한 답. 부르면 더 큰 모델이 처음부터 이어받는다. 잡담·인사·단순 조회엔 부르지 않는다."),
    "parameters": {"type": "object", "properties": {"reason": {"type": "string", "description": "왜 어려운지 한 줄"}},
                   "required": ["reason"]}}}

# ── 신호 (전부 코드, 돈 0) ─────────────────────────────────
# 일(실행) 말: 바꾸기·제재·보내기·만들기 동사. '해줘' 같은 흔한 꼬리는 안 봄 (잡담에도 많음).
_DO = re.compile(
    r"설정|바꿔|바꾸|변경|켜\s?(줘|주|라|기|봐)|꺼\s?(줘|주|라|기|봐)|끄\s?(고|기)|활성화|비활성|"
    r"밴(?!드)|뮤트|경고(?!등)|강퇴|추방|내보내|쫓아내|차단|해제|막아|금지|킥|조용히\s?시켜|입\s?(좀\s?)?막|"
    r"(?<![A-Za-z])(ban|mute|kick)(?![A-Za-z])|벤\s?(해|하|시켜|먹|때려)|"
    r"공지|고정|예약|알람|알림\s?(설정|걸|켜|꺼|규칙)|규칙\s?(저장|추가|바꿔|정해)|삭제|지워|청소|등록|저장해\s?(줘|주)|"
    r"그려|그림\s?(그려|만들|좀|하나|으로)|이미지|스티커|움프|프사|영상\s?(만들|제작|생성|찍|으로)|동영상|"
    r"보내\s?(줘|주|라)|올려\s?(줘|주|라)|초대|음성방|통화\s?(해|걸|하자|들어)|전화|들어와|"
    r"잠금|잠가|캡차|캡챠|말투|추가해|추가\s?해|빼\s?(줘|주)|지급|포인트\s?(줘|주)|구독|결제|연장|번역|"
    r"처리해|없애|풀어\s?(줘|주)|데려와|안내\s?(해|적용|올려|문구|설정)|신청곡|틀어\s?(줘|주)|play|완장|생성|"
    r"만들어\s?(줘|주|봐)|해\s?달래|수정해|다시\s?(해|만들|그려)", re.I)
# 분석·판단 (agent._WHY 와 같은 뜻 + 계획)
_THINK = re.compile(r"왜|원인|이유|분석|비교|판단|검토|영향|괜찮을까|어떻게\s?(해야|하면|할까)|계획|전략|추천해|정리해\s?줘|요약")
_CHAIN = re.compile(r"(찾아|확인해|알아봐|살펴|읽어|보)(서|고)[\s,]|그리고|다음에|한\s?(다음|뒤|후)|둘\s?다|각각")
LONG_CHARS = 140
MEDIA_MARK = re.compile(r"\[(사진|영상|이미지|GIF|동그라미|스티커|움직이는|파일)")
_LINK = re.compile(r"https?://|t\.me/|www\.", re.I)                            # 이보다 긴 요청 = 설명이 많은 일일 때가 많음


@dataclass
class Req:
    request: str
    role: int = Role.MEMBER
    mode: str = "call"
    in_dm: bool = False
    has_media: bool = False
    settings: dict = field(default_factory=dict)
    lines: int = 1
    recent_heavy: bool = False   # 같은 사람이 5분 안에 큰 모델로 한 일의 이어짐 ('하나 더'·'다시'·'그거 말고') → 두 번 부르지 않게


# (이름, 판정 함수, 길) — 위에서부터 처음 걸린 것
Signal = tuple[str, Callable[[Req], bool], str]
SIGNALS: list[Signal] = [
    ("media", lambda r: r.has_media, "heavy"),
    ("continue", lambda r: r.recent_heavy and len(r.request) <= 40, "heavy"),
    ("choice", lambda r: r.request.startswith("(선택"), "heavy"),              # 선택 버튼으로 이어진 일 (askchoice)
    ("dm_manage", lambda r: r.in_dm and r.role >= Role.ADMIN, "heavy"),       # 1:1 관리자·오너 = 운영 일
    ("do", lambda r: bool(_DO.search(r.request)), "heavy"),
    ("think", lambda r: bool(_THINK.search(r.request)), "heavy"),
    ("chain", lambda r: bool(_CHAIN.search(r.request)), "heavy"),
    ("long", lambda r: len(r.request) > LONG_CHARS, "heavy"),
    ("lines", lambda r: r.lines >= 3, "heavy"),                               # 여러 줄 = 목록·설명 붙은 일
    ("link", lambda r: bool(_LINK.search(r.request)), "heavy"),               # 링크 = 확인·판단할 거리
    ("banter", lambda r: (r.settings.get("ai_comeback") == "mirror" and bool(INSULT_RE.search(r.request)))
     or (bool(r.settings.get("ai_spicy")) and bool(SEX_RE.search(r.request))), "banter"),
]


@dataclass
class Route:
    lane: str        # light | banter | heavy
    why: str         # 걸린 신호 이름 (기록용)


def decide(r: Req, *, mode: str = DEFAULT_MODE, light_model: str = "") -> Route:
    """길 고르기. 끼어들기(chime·morning)는 도구가 방 자료 조회뿐이라 늘 light."""
    if not light_model or mode == "best":
        return Route("heavy", "off" if not light_model else "best")
    raw = r.request or ""
    r = Req(" ".join(raw.split()), r.role, r.mode, r.in_dm, r.has_media, r.settings,
            lines=len([x for x in raw.splitlines() if x.strip()]), recent_heavy=r.recent_heavy)
    if r.mode in ("chime", "morning"):
        return Route("light", "chime")
    for name, fn, lane in SIGNALS:
        if fn(r):
            if lane == "banter" and mode == "saver":
                return Route("light", name)
            return Route(lane, name)
    return Route("light", "chat")


def light_ok(tool_name: str, read_only: set[str] | frozenset[str]) -> bool:
    """light 길에서 실행해도 되는 도구인지 (아니면 올려 보냄)."""
    return tool_name in read_only or tool_name in LIGHT_EXTRA


CONTINUE_SEC = 300


async def recent_heavy(db, chat_id: int, user_id: int, now: float) -> bool:
    """이 사람의 바로 전 AI 실행이 5분 안이고 큰 모델(light 아님)이었는지. 조회 실패 = False."""
    try:
        row = await db._one("SELECT purpose, ts FROM agent_runs WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                            (chat_id, user_id))
    except Exception:
        return False
    return bool(row and now - (row["ts"] or 0) <= CONTINUE_SEC and ":light" not in (row["purpose"] or "")
                and ":chime" not in (row["purpose"] or "") and (row["purpose"] or "").startswith("agent:"))


async def room_mode(db, chat_id: int) -> str:
    try:
        raw = await db.get_state(chat_id, ROUTE_KEY)
    except Exception:
        return DEFAULT_MODE
    return raw if raw in MODES else DEFAULT_MODE
