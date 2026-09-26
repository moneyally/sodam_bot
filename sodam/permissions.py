"""권한 판단. AI가 아니라 여기 코드만 권한을 결정한다."""
import logging
import secrets
import time
from enum import IntEnum

from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import NetworkError, TelegramError, TimedOut

from .config import Config
from .db import DB, register_schema

log = logging.getLogger(__name__)
ADMIN_TTL = 300            # 메모리·DB 캐시 유효 시간
ADMIN_STALE = 86400        # 이보다 오래된 방은 '내 그룹' 목록 후보에 다시 넣어 새로 확인

# 텔레그램 관리자 목록 캐시 (재시작 후에도 빠르게, '내 그룹 관리' 목록을 방 전체 조회 없이 만들려고)
register_schema("""
CREATE TABLE IF NOT EXISTS chat_admins (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS chat_admins_fetched (
    chat_id INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL
);
""", migrate={"chat_admins": "drop", "chat_admins_fetched": "drop"})


class Role(IntEnum):
    MEMBER = 0
    ADMIN = 1
    OWNER = 2


class Permissions:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self._admin_cache: dict[int, tuple[float, set[int]]] = {}
        self._forgotten: set[int] = set()
        self._admin_users: dict[int, tuple[float, list]] = {}  # 관리자 이름 (사칭 검사용)
        self.on_admins = None  # async fn(bot, chat_id, admins) — services 조립 때 연결
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
        now = time.time()
        cached = self._admin_cache.get(chat_id)
        if cached and now - cached[0] < ADMIN_TTL:
            return cached[1]
        if chat_id not in self._forgotten:  # 재시작 직후: DB 에 최근 목록이 있으면 그대로
            row = await self.db._one("SELECT ts FROM chat_admins_fetched WHERE chat_id=?", (chat_id,))
            if row and now - row["ts"] < ADMIN_TTL:
                ids = {r["user_id"] for r in await self.db._all(
                    "SELECT user_id FROM chat_admins WHERE chat_id=?", (chat_id,))}
                self._admin_cache[chat_id] = (row["ts"], ids)
                return ids
        try:
            admins = await bot.get_chat_administrators(chat_id)
        except (TimedOut, NetworkError) as e:
            # 텔레그램 연결이 잠깐 끊겨도 메시지 처리가 통째로 죽지 않게: 오래된 목록이라도 쓴다
            stale = cached[1] if cached else await self._stored_admins(chat_id)
            if stale is None:
                raise
            log.warning("관리자 목록 조회 실패(%s) → 저장된 목록 사용: chat %s", e, chat_id)
            return stale
        ids = {a.user.id for a in admins if a.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)}
        if self.on_admins is not None:  # 관리자 이름도 이름 기록에 (namehist)
            try:
                await self.on_admins(bot, chat_id, admins)
            except Exception:
                log.exception("admin name record failed")
        self._admin_cache[chat_id] = (now, ids)
        self._admin_users[chat_id] = (now, [a.user for a in admins if a.status in (
            ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)])
        self._forgotten.discard(chat_id)
        await self._store_admins(chat_id, ids, int(now))
        return ids

    async def admin_users(self, bot: Bot, chat_id: int) -> list:
        """관리자 User 목록 (이름 비교용). 캐시가 있으면 텔레그램에 다시 묻지 않는다. 연결 오류면 오래된 목록 / 빈 목록."""
        cached = self._admin_users.get(chat_id)
        if cached and time.time() - cached[0] < ADMIN_TTL:
            return cached[1]
        if not cached:  # DB 캐시엔 이름이 없어서 새로 받아야 함
            self.forget(chat_id)
        try:
            await self.telegram_admins(bot, chat_id)
        except TelegramError as e:
            log.warning("관리자 이름 조회 실패: chat %s: %s", chat_id, e)
        got = self._admin_users.get(chat_id) or cached
        return got[1] if got else []

    async def _stored_admins(self, chat_id: int) -> set[int] | None:
        if not await self.db._one("SELECT 1 FROM chat_admins_fetched WHERE chat_id=?", (chat_id,)):
            return None
        return {r["user_id"] for r in await self.db._all("SELECT user_id FROM chat_admins WHERE chat_id=?", (chat_id,))}

    async def _store_admins(self, chat_id: int, ids: set[int], ts: int) -> None:
        conn = self.db.conn
        await conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
        await conn.executemany("INSERT OR IGNORE INTO chat_admins(chat_id, user_id) VALUES(?, ?)",
                               [(chat_id, uid) for uid in ids])
        await conn.execute("INSERT INTO chat_admins_fetched(chat_id, ts) VALUES(?, ?) "
                           "ON CONFLICT(chat_id) DO UPDATE SET ts=excluded.ts", (chat_id, ts))
        await conn.commit()

    def forget(self, chat_id: int) -> None:
        """관리자 변경·결제 판단 등: 다음 확인은 메모리·DB 캐시 모두 건너뛰고 텔레그램에 묻는다."""
        self._admin_cache.pop(chat_id, None)
        self._forgotten.add(chat_id)

    async def candidate_chats(self, user_id: int) -> list[int]:
        """'내 그룹 관리' 목록 후보 (방 전체에 getChatAdministrators 를 돌리지 않게).
        그 사람이 관리자로 기록된 방 + 봇관리자로 등록된 방 + 아직 목록을 안 받았거나 오래된 방. 최종 판단은 is_admin."""
        if user_id in await self.owners():
            return [r["chat_id"] for r in await self.db._all("SELECT chat_id FROM chats WHERE chat_id < 0")]
        rows = await self.db._all(
            "SELECT c.chat_id FROM chats c LEFT JOIN chat_admins_fetched f ON f.chat_id=c.chat_id "
            "WHERE c.chat_id < 0 AND (f.ts IS NULL OR f.ts < ? "
            "  OR EXISTS(SELECT 1 FROM chat_admins a WHERE a.chat_id=c.chat_id AND a.user_id=?) "
            "  OR EXISTS(SELECT 1 FROM bot_admins b WHERE b.chat_id=c.chat_id AND b.user_id=?))",
            (int(time.time()) - ADMIN_STALE, user_id, user_id))
        return [r["chat_id"] for r in rows]

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

    async def is_tg_admin(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """텔레그램 관리자 또는 오너 (.봇관리자 로 추가된 사람은 제외). 결제·구독 화면용."""
        if user_id in await self.owners():
            return True
        return chat_id < 0 and user_id in await self.telegram_admins(bot, chat_id)
