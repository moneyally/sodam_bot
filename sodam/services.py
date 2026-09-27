"""봇 전체가 공유하는 객체 묶음."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .util import RateLimiter

if TYPE_CHECKING:
    from .announce import Announcer
    from .backup import Backup
    from .billing import Billing
    from .captcha import Captcha
    from .cas import Cas
    from .config import Config
    from .db import DB
    from .games import GameManager
    from .greet import Greeter
    from .llm import LLM
    from .moderation import Moderator
    from .permissions import Permissions
    from .sports import Sports


@dataclass
class PendingAction:
    """관리자 확인 버튼이 필요한 동작 (경고·뮤트·밴)."""
    chat_id: int
    kind: str
    target_id: int
    target_name: str
    reason: str
    requested_by: int
    expires: float = field(default_factory=lambda: time.time() + 120)
    minutes: int = 0   # mute 기간
    extra: tuple[tuple[int, str], ...] = ()   # 같은 확인 버튼으로 함께 처리할 대상들 (한 번에 여러 명)
    from_dm: bool = False   # 오너가 1:1 에서 요청 → 확인 카드는 1:1 에, 원하면 방에 안내
    refused: set[int] = field(default_factory=set)   # 권한 없이 누른 사람 (기록은 한 번만)

    @property
    def targets(self) -> list[tuple[int, str]]:
        return [(self.target_id, self.target_name), *self.extra]


@dataclass
class PendingInput:
    """버튼 메뉴에서 '글자로 보내주세요' 를 기다리는 중 (1:1, 한 사람당 1개)."""
    kind: str
    chat_id: int
    args: list[str] = field(default_factory=list)  # 예: 인사 편집기에서 어느 칸을 고치는지
    expires: float = field(default_factory=lambda: time.time() + 300)


@dataclass
class MenuToken:
    """버튼에 담기엔 긴 값(금지어 등)·파괴적 동작용 서버 쪽 1회용 토큰."""
    user_id: int
    chat_id: int
    action: str
    arg: Any
    expires: float


@dataclass
class Services:
    cfg: Config
    db: DB
    perms: Permissions
    mod: Moderator
    llm: LLM
    sports: Sports
    cas: Cas = None
    backup: Backup = None
    billing: Billing = None
    pay_check_times: dict[int, float] = field(default_factory=dict)  # '입금했어요' 버튼 연타 방지
    joins: dict[tuple[int, int], float] = field(default_factory=dict)  # 입장 중복 처리 방지 (나가면 즉시 삭제)
    games: GameManager = None  # 아래는 Services 를 참조해서 생성 후 채운다
    greeter: Greeter = None
    captcha: Captcha = None
    announcer: Announcer = None
    pending: dict[str, PendingAction] = field(default_factory=dict)
    inputs: dict[int, PendingInput] = field(default_factory=dict)      # user_id → 메뉴 글자 입력 대기
    menu_tokens: dict[str, MenuToken] = field(default_factory=dict)
    menu_limiter: RateLimiter = field(default_factory=RateLimiter)
    panel_msgs: dict[int, int] = field(default_factory=dict)  # user_id → 지금 살아있는 메뉴 메시지 (옛 메뉴 버튼 정리용)

    async def paid_features(self, chat_id: int) -> bool:
        """구독(또는 체험) 중인 방인지. 결제 기능이 꺼져 있으면 항상 True.
        유료 기능: AI(무료 한도 초과분)·게임·예약공지·자료 등록·스포츠 알림·일일 리포트.
        방 관리(캡차·도배·CAS·경고·명령어)는 구독과 상관없이 동작."""
        return self.billing is None or await self.billing.active(chat_id)

    def add_pending(self, action: PendingAction) -> str:
        now = time.time()
        for key in [k for k, v in self.pending.items() if v.expires < now]:
            del self.pending[key]
        key = secrets.token_hex(4)
        self.pending[key] = action
        return key
