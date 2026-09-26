"""권한 판단. AI가 아니라 여기 코드만 권한을 결정한다."""
import logging
import secrets
import time
from enum import IntEnum

from telegram import Bot
from telegram.constants import ChatMemberStatus

from .config import Config
from .db import DB

log = logging.getLogger(__name__)


class Role(IntEnum):
    MEMBER = 0
    ADMIN = 1
    OWNER = 2


class Permissions:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self._admin_cache: dict[int, tuple[float, set[int]]] = {}
        self._owners: set[int] | None = None
        self.claim_code: str | None = None  # 오너가 없을 때만 생성, 서버 로그에만 출력

    # ── 오너 ──────────────────────────────────────────────
    async def owners(self) -> set[int]:
        if self._owners is None:
            self._owners = set(self.cfg.owner_ids) | await self.db.owner_ids()
        return self._owners

    CLAIM_MAX_PER_USER = 5
    CLAIM_MAX_TOTAL = 20

    async def prepare_claim_code(self) -> str | None:
        """오너가 한 명도 없으면 1회용 8자리 등록 코드를 만든다 (서버를 켠 사람만 로그에서 볼 수 있음)."""
        if await self.owners():
            return None
        self.claim_code = f"{secrets.randbelow(10**8):08d}"
        self._claim_fails: dict[int, int] = {}
        return self.claim_code

    async def claim(self, user_id: int, code: str) -> bool:
        """무작위 대입 방지: 1인 5회, 전체 20회 틀리면 코드 폐기 (서버 재시작으로 새 코드)."""
        if not self.claim_code:
            return False
        fails = self._claim_fails
        if fails.get(user_id, 0) >= self.CLAIM_MAX_PER_USER:
            return False
        if not secrets.compare_digest(code.strip(), self.claim_code):
            fails[user_id] = fails.get(user_id, 0) + 1
            if sum(fails.values()) >= self.CLAIM_MAX_TOTAL:
                self.claim_code = None
                log.warning("오너 등록 코드가 %d회 틀려서 폐기됐어요. 재시작하면 새 코드가 나와요.",
                                                    self.CLAIM_MAX_TOTAL)
            return False
        await self.db.add_owner(user_id)
        self.claim_code = None  # 1회용
        self._owners = None
        return True

    # ── 관리자 ────────────────────────────────────────────
    async def telegram_admins(self, bot: Bot, chat_id: int) -> set[int]:
        cached = self._admin_cache.get(chat_id)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        admins = await bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins if a.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)}
        self._admin_cache[chat_id] = (time.time(), ids)
        return ids

    def forget(self, chat_id: int) -> None:
        self._admin_cache.pop(chat_id, None)

    async def role(self, bot: Bot, chat_id: int, user_id: int) -> Role:
        if user_id in await self.owners():
            return Role.OWNER
        if chat_id > 0:  # 1:1 채팅에는 관리자 개념이 없음
            return Role.MEMBER
        if user_id in await self.telegram_admins(bot, chat_id):
            return Role.ADMIN
        if user_id in await self.db.bot_admin_ids(chat_id):
            return Role.ADMIN
        return Role.MEMBER

    async def is_admin(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        return await self.role(bot, chat_id, user_id) >= Role.ADMIN

    async def protected(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """제재할 수 없는 대상 (오너·관리자·봇 자신)."""
        return user_id == bot.id or await self.is_admin(bot, chat_id, user_id)
