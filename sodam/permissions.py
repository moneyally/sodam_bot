"""권한 판단. AI가 아니라 여기 코드만 권한을 결정한다."""
import logging
import secrets
import sqlite3
import time
from enum import IntEnum

from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut

from .config import Config
from .db import DB, register_schema

log = logging.getLogger(__name__)
CLAIM_FAIL_KEY = "owner_claim_fail"   # counters(날짜, 사람 ID): /owner 코드 틀린 횟수
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
# 관리자별 텔레그램 권한 (사용자 차단·메시지 삭제). 이 표가 비어 있는 방 = 옛 형식 캐시 → 텔레그램에 다시 묻는다
register_schema("""
CREATE TABLE IF NOT EXISTS chat_admin_rights (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    can_restrict INTEGER NOT NULL,
    can_delete   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"chat_admin_rights": "drop"})

RIGHT_LABEL = {"restrict": "사용자 차단", "delete": "메시지 삭제"}


def no_right_text(right: str = "restrict") -> str:
    return (f"텔레그램에서 '{RIGHT_LABEL[right]}' 권한이 있는 관리자만 할 수 있어요.\n"
            "(방장이 텔레그램 관리자 설정에서 권한을 켜 주거나, 그런 관리자가 해야 해요)")


async def may(perms, bot: Bot, chat_id: int, user_id: int, right: str = "restrict") -> bool:
    """제재 권한 확인 (명령·AI 제재·확인 버튼·캡차 버튼 공용). 확인 중 연결 오류면 막는 쪽(False)."""
    try:
        return await perms.can(bot, chat_id, user_id, right)
    except TelegramError as e:
        log.warning("권한 확인 실패(%s) → 거절: chat %s user %s", e, chat_id, user_id)
        return False


def _rights_of(member) -> tuple[bool, bool]:
    """getChatAdministrators 한 명 → (사용자 차단, 메시지 삭제). 방장(creator)은 전부."""
    if member.status == ChatMemberStatus.OWNER:
        return True, True
    return bool(getattr(member, "can_restrict_members", False)), bool(getattr(member, "can_delete_messages", False))


class Role(IntEnum):
    MEMBER = 0
    ADMIN = 1
    OWNER = 2


class Permissions:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self._admin_cache: dict[int, tuple[float, set[int]]] = {}
        self._rights: dict[int, dict[int, tuple[bool, bool]]] = {}  # 방 → 관리자 → (사용자 차단, 메시지 삭제)
        self._forgotten: set[int] = set()
        self._admin_users: dict[int, tuple[float, list]] = {}  # 관리자 이름 (사칭 검사용)
        self._bot_rights: dict[int, tuple[float, bool]] = {}   # 봇이 관리 권한이 있는지
        self.on_admins = None  # async fn(bot, chat_id, admins) — services 조립 때 연결
        self._owners: set[int] | None = None
        self.claim_code: str | None = None  # 오너가 없을 때만 생성, 서버 로그에만 출력

    # ── 오너 ──────────────────────────────────────────────
    async def owners(self) -> set[int]:
        if self._owners is None:
            self._owners = set(self.cfg.owner_ids) | await self.db.owner_ids()
        return self._owners

    CLAIM_MAX_PER_USER = 5   # 1인 (하루, 재시작해도)
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
        # 1인 한도는 DB(오늘 카운터)에도 → 재시작(=새 코드)마다 5번씩 다시 시도하지 못하게
        day = time.strftime("%Y-%m-%d")
        if fails.get(user_id, 0) >= self.CLAIM_MAX_PER_USER or \
                await self.db.counter(day, user_id, CLAIM_FAIL_KEY) >= self.CLAIM_MAX_PER_USER:
            return False
        if not secrets.compare_digest(code.strip(), self.claim_code):
            await self.db.bump(day, user_id, CLAIM_FAIL_KEY)
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
            rights = await self._stored_rights(chat_id) if row and now - row["ts"] < ADMIN_TTL else None
            if rights:  # 권한까지 저장된 새 형식만 (옛 형식이면 아래에서 다시 조회)
                ids = {r["user_id"] for r in await self.db._all(
                    "SELECT user_id FROM chat_admins WHERE chat_id=?", (chat_id,))}
                self._admin_cache[chat_id] = (row["ts"], ids)
                self._rights[chat_id] = rights
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
        except (Forbidden, BadRequest) as e:
            # 봇이 강퇴됐거나 없어진 방: 조회 시각만 DB 에 남겨 '내 그룹' 후보(candidate_chats)에서 하루 뺀다.
            # 예전엔 기록이 안 남아 메인 메뉴를 열 때마다(누구든) 이런 방들을 텔레그램에 다시 물었음.
            # 메모리엔 안 둠 → 봇이 다시 들어오면 다음 확인에서 바로 새로 받음 (DB 에 권한 줄이 없어서)
            log.info("관리자 목록 조회 불가(%s): chat %s", e, chat_id)
            await self._store_admins(chat_id, set(), int(now), {})
            return set()
        ids = {a.user.id for a in admins if a.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)}
        rights = {a.user.id: _rights_of(a) for a in admins if a.user.id in ids}
        if self.on_admins is not None:  # 관리자 이름도 이름 기록에 (namehist)
            try:
                await self.on_admins(bot, chat_id, admins)
            except Exception:
                log.exception("admin name record failed")
        self._admin_cache[chat_id] = (now, ids)
        self._rights[chat_id] = rights
        self._admin_users[chat_id] = (now, [a.user for a in admins if a.status in (
            ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)])
        self._forgotten.discard(chat_id)
        await self._store_admins(chat_id, ids, int(now), rights)
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

    async def bot_can_moderate(self, bot: Bot, chat_id: int) -> bool:
        """봇이 그 방에서 메시지 삭제 + 사용자 제한 권한이 있는지 (10분 캐시, 봇 권한이 바뀌면 forget_bot).
        권한 없는 방(일반 멤버로만 초대)에선 도배·금지어·사칭·캡차·CAS 같은 관리 기능을 건너뛴다."""
        hit = self._bot_rights.get(chat_id)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            me = await bot.get_chat_member(chat_id, bot.id)
            ok = me.status == ChatMemberStatus.OWNER or (
                me.status == ChatMemberStatus.ADMINISTRATOR
                and bool(getattr(me, "can_delete_messages", False)) and bool(getattr(me, "can_restrict_members", False)))
        except (Forbidden, BadRequest):
            ok = False                      # 봇이 강퇴됐거나 없는 방
        except TelegramError:
            return hit[1] if hit else True  # 네트워크 등으로 확인 못 하면 예전 값, 처음이면 시도는 해 본다
        self._bot_rights[chat_id] = (time.time(), ok)
        return ok

    def forget_bot(self, chat_id: int) -> None:
        self._bot_rights.pop(chat_id, None)

    async def _stored_admins(self, chat_id: int) -> set[int] | None:
        if not await self.db._one("SELECT 1 FROM chat_admins_fetched WHERE chat_id=?", (chat_id,)):
            return None
        return {r["user_id"] for r in await self.db._all("SELECT user_id FROM chat_admins WHERE chat_id=?", (chat_id,))}

    async def _stored_rights(self, chat_id: int) -> dict[int, tuple[bool, bool]]:
        return {r["user_id"]: (bool(r["can_restrict"]), bool(r["can_delete"])) for r in await self.db._all(
            "SELECT user_id, can_restrict, can_delete FROM chat_admin_rights WHERE chat_id=?", (chat_id,))}

    async def _store_admins(self, chat_id: int, ids: set[int], ts: int,
                            rights: dict[int, tuple[bool, bool]] | None = None) -> None:
        def run(c) -> None:   # 지우고 다시 넣기를 한 번에 (지운 것만 저장돼 관리자 목록이 비지 않게)
            c.execute("DELETE FROM chat_admins WHERE chat_id=?", (chat_id,))
            c.executemany("INSERT OR IGNORE INTO chat_admins(chat_id, user_id) VALUES(?, ?)", [(chat_id, u) for u in ids])
            c.execute("DELETE FROM chat_admin_rights WHERE chat_id=?", (chat_id,))
            c.executemany("INSERT OR IGNORE INTO chat_admin_rights VALUES(?, ?, ?, ?)",
                          [(chat_id, u, int(r[0]), int(r[1])) for u, r in (rights or {}).items()])
            c.execute("INSERT INTO chat_admins_fetched(chat_id, ts) VALUES(?, ?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET ts=excluded.ts", (chat_id, ts))
        try:
            await self.db.atomic(run)
        except sqlite3.Error as e:   # 저장 못 해도 메모리 캐시로 동작 (디스크 가득 참 등)
            log.warning("관리자 목록 저장 실패: chat %s: %s", chat_id, e)

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

    # ── 세부 권한 (사용자 차단·메시지 삭제) ────────────────
    async def _tg_right(self, bot: Bot, chat_id: int, user_id: int, right: str) -> bool | None:
        """텔레그램 관리자면 그 권한이 있는지, 관리자가 아니면 None."""
        if user_id not in await self.telegram_admins(bot, chat_id):
            return None
        got = self._rights.get(chat_id, {}).get(user_id)
        if got is None:  # 연결 오류로 옛 형식(권한 없는) 목록을 쓴 경우: 저장된 권한 → 그래도 없으면 막음
            got = (await self._stored_rights(chat_id)).get(user_id, (False, False))
        return got[0] if right == "restrict" else got[1]

    async def can(self, bot: Bot, chat_id: int, user_id: int, right: str = "restrict") -> bool:
        """right = "restrict"(밴·뮤트·경고·잠금·캡차 승인) | "delete"(메시지 삭제).
        오너: 항상 · 텔레그램 관리자: 텔레그램에서 그 권한이 켜져 있을 때(방장은 전부) ·
        봇관리자: 지정한 사람이 지금 그 권한이 있는 텔레그램 관리자이거나 오너일 때 (위임).
        지정 기록이 없는 봇관리자 = 오너가 .봇관리자 명령으로 넣은 옛 기록 → 허용."""
        if user_id in await self.owners():
            return True
        if chat_id > 0:
            return False
        tg = await self._tg_right(bot, chat_id, user_id, right)
        if tg is not None:
            return tg
        if user_id not in await self.db.bot_admin_ids(chat_id):
            return False
        row = await self.db._one(
            "SELECT actor_id FROM mod_log WHERE chat_id=? AND target_id=? AND action='bot_admin' AND detail='추가' "
            "ORDER BY id DESC LIMIT 1", (chat_id, user_id))
        if row is None or row["actor_id"] is None or row["actor_id"] in await self.owners():
            return True
        return bool(await self._tg_right(bot, chat_id, row["actor_id"], right))

    async def can_restrict(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        return await self.can(bot, chat_id, user_id, "restrict")

    async def is_tg_admin(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """텔레그램 관리자 또는 오너 (.봇관리자 로 추가된 사람은 제외). 결제·구독 화면용."""
        if user_id in await self.owners():
            return True
        return chat_id < 0 and user_id in await self.telegram_admins(bot, chat_id)
