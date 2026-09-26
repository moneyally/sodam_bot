"""방 관리: 도배, 반복, 금지어, 링크, 사칭, 경고 누적, 제재 실행."""
import logging
import re
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable

from telegram import Bot, ChatPermissions, Message, User
from telegram.error import TelegramError

from . import casino
from .config import Config
from .db import DB
from .permissions import Permissions
from .security import find_links, find_mentions, link_allowed, normalize
from .settings import register_setting
from .util import esc, human_minutes, mention, user_name

log = logging.getLogger(__name__)

DEFAULT_MEMBER_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)

_IMPERSONATE_WORDS = ("관리자", "운영자", "운영진", "매니저", "공지", "고객센터", "admin", "support", "official", "mod")


def _has(msg, *attrs: str) -> bool:
    return any(getattr(msg, a, None) for a in attrs)


# 종류별 잠금: (설정 키, 이름, 그 종류인지). 기본은 전부 허용. 🔒 종류별 잠금 화면(panels/locks.py)에서 켠다
LOCK_KINDS: list[tuple[str, str, Callable[[Message], bool]]] = [
    ("lock_photo", "사진", lambda m: _has(m, "photo")),
    ("lock_video", "영상", lambda m: _has(m, "video", "video_note")),
    ("lock_sticker", "스티커", lambda m: _has(m, "sticker")),
    ("lock_gif", "GIF", lambda m: _has(m, "animation")),
    ("lock_voice", "음성·오디오", lambda m: _has(m, "voice", "audio")),
    ("lock_document", "파일", lambda m: _has(m, "document") and not _has(m, "animation")),  # GIF 는 document 도 채워져 옴
    ("lock_poll", "설문", lambda m: _has(m, "poll")),
    ("lock_contact", "연락처", lambda m: _has(m, "contact")),
    ("lock_location", "위치", lambda m: _has(m, "location", "venue")),
    ("lock_inline", "인라인 봇 메시지", lambda m: _has(m, "via_bot")),
]
for _key, _label, _ in LOCK_KINDS:
    register_setting(_key, False, f"{_label} 막기")
# 전달(포워드) 메시지: off 허용 / newbie 신규 입장자만 막기(newbie_link_hours 기준) / all 전부 막기
register_setting("forward_filter", "newbie", "전달(포워드) 메시지 막기",
                 choices={"off": "off", "끔": "off", "허용": "off", "newbie": "newbie", "신규": "newbie",
                          "all": "all", "전체": "all"},
                 choice_labels={"off": "허용", "newbie": "신규 입장자만 막기", "all": "전부 막기"})
# 홍보 @아이디(채널·다른 그룹·봇) 막기: 관리자가 허락한 홍보일 수도 있어서 선택 사항 (기본 꺼짐, 🔒 종류별 잠금 화면)
register_setting("promo_mentions", False, "홍보 @아이디 막기 (채널·그룹·봇)")
KIND_NOTICE_GAP = 600  # 잠긴 종류·전달 안내는 같은 사람에게 10분에 한 번
MENTION_CACHE = 86400  # @아이디가 채널·그룹인지 조회 결과 보관(초)


def _forwarded(msg) -> bool:
    # 연결된 채널 글의 자동 전달은 제외 (그건 방 쪽 글)
    return getattr(msg, "forward_origin", None) is not None and not getattr(msg, "is_automatic_forward", False)


def _squash(name: str) -> str:
    """사칭 비교용: 공백·기호·이모지 제거, 소문자, 비슷한 글자 통일."""
    name = unicodedata.normalize("NFKC", name).lower()
    name = name.translate(str.maketrans({"0": "o", "1": "l", "|": "l", "!": "i"}))
    return re.sub(r"[^0-9a-z가-힣]", "", name)


class Moderator:
    def __init__(self, cfg: Config, db: DB, perms: Permissions):
        self.cfg = cfg
        self.db = db
        self.perms = perms
        self._flood: dict[tuple[int, int], deque[float]] = {}
        self._dups: dict[tuple[int, int], tuple[str, int]] = {}
        self._checked_names: set[tuple[int, str]] = set()
        self._unreachable: set[int] = set()
        self._kind_noticed: dict[tuple[int, int], float] = {}
        self._chat_kind: dict[str, tuple[float, bool]] = {}   # @아이디 → (조회 시각, 채널·그룹인지)

    # ── 제재 실행 ─────────────────────────────────────────
    async def mute(self, bot: Bot, chat_id: int, user_id: int, minutes: int | None,
                   actor_id: int | None, reason: str) -> None:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes) if minutes else None
        await bot.restrict_chat_member(chat_id, user_id, ChatPermissions.no_permissions(), until_date=until)
        await self.db.log_mod(chat_id, actor_id, user_id, "mute",
                              f"{human_minutes(minutes) if minutes else '무기한'} / {reason}")

    async def unmute(self, bot: Bot, chat_id: int, user_id: int, actor_id: int | None) -> None:
        # 모든 권한 True = 개인 제한 해제 (이후 방 기본 권한을 따름).
        # 방의 현재 권한을 복사해 넣으면, 방이 잠겨 있을 때 그 상태로 개인에게 고정되는 문제가 있다.
        await bot.restrict_chat_member(chat_id, user_id, ChatPermissions.all_permissions())
        await self.db.delete_captcha(chat_id, user_id)
        await self.db.log_mod(chat_id, actor_id, user_id, "unmute")

    # ── 방 잠금 ───────────────────────────────────────────
    async def lock(self, bot: Bot, chat_id: int, actor_id: int) -> bool:
        """방 기본 권한을 저장하고 전부 막는다. 이미 잠겨 있으면 False."""
        if await self.db.get_state(chat_id, "locked"):
            return False
        chat = await bot.get_chat(chat_id)
        saved = chat.permissions.to_dict() if chat.permissions else None
        await bot.set_chat_permissions(chat_id, ChatPermissions.no_permissions(), use_independent_chat_permissions=True)
        await self.db.set_state(chat_id, "saved_permissions", saved)
        await self.db.set_state(chat_id, "locked", True)
        await self.db.log_mod(chat_id, actor_id, None, "lock")
        return True

    async def unlock(self, bot: Bot, chat_id: int, actor_id: int) -> None:
        """잠그기 전 권한으로 되돌린다. 저장된 게 없으면 일반 멤버 기본 권한."""
        saved = await self.db.get_state(chat_id, "saved_permissions")
        # de_json: 저장 뒤 PTB/API 에 생긴·없어진 필드가 있어도 TypeError 없이 (모르는 건 api_kwargs 로)
        perms = ChatPermissions.de_json(saved, bot) if saved else DEFAULT_MEMBER_PERMISSIONS
        await bot.set_chat_permissions(chat_id, perms, use_independent_chat_permissions=True)
        await self.db.set_state(chat_id, "locked", None)
        await self.db.set_state(chat_id, "saved_permissions", None)
        await self.db.log_mod(chat_id, actor_id, None, "unlock")

    async def ban(self, bot: Bot, chat_id: int, user_id: int, actor_id: int | None, reason: str) -> None:
        await bot.ban_chat_member(chat_id, user_id)
        await self.db.log_mod(chat_id, actor_id, user_id, "ban", reason)

    async def unban(self, bot: Bot, chat_id: int, user_id: int, actor_id: int | None) -> None:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await self.db.log_mod(chat_id, actor_id, user_id, "unban")

    async def kick(self, bot: Bot, chat_id: int, user_id: int, actor_id: int | None, reason: str) -> None:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await self.db.log_mod(chat_id, actor_id, user_id, "kick", reason)

    async def warn(self, bot: Bot, chat_id: int, user_id: int, name: str,
                   actor_id: int, reason: str) -> str:
        """경고 1회 + 누적 단계 처리. 방에 보낼 안내문을 돌려준다."""
        s = await self.db.get_settings(chat_id)
        count = await self.db.add_warning(chat_id, user_id, actor_id, reason)
        await self.db.log_mod(chat_id, actor_id, user_id, "warn", reason)
        who = mention(user_id, name)
        text = f"⚠️ {who}님 경고 {count}회 ({esc(reason)})"
        try:
            if count >= s["warn_ban_at"]:
                await self.ban(bot, chat_id, user_id, actor_id, f"경고 {count}회 누적")
                text += f"\n🚫 경고 {count}회 누적으로 내보냈어요."
                await self.report(bot, f"[자동 밴] chat {chat_id} / {esc(name)}({user_id}) 경고 {count}회 누적 ({esc(reason)})")
            elif count >= s["warn_mute_at"]:
                await self.mute(bot, chat_id, user_id, s["warn_mute_minutes"], actor_id, f"경고 {count}회 누적")
                text += f"\n🔇 경고 누적으로 {human_minutes(s['warn_mute_minutes'])} 채팅 금지예요."
            else:
                left = s["warn_mute_at"] - count
                text += f"\n(앞으로 {left}회 더 받으면 채팅 금지)"
        except TelegramError as e:
            log.warning("escalation failed: %s", e)
            text += "\n(봇 권한이 부족해서 제재는 못 했어요)"
        return text

    async def report(self, bot: Bot, text: str) -> None:
        """관리자 보고: 관리 로그방(LOG_CHAT_ID) + 오너 개인 텔레그램으로 전달.

        봇은 먼저 대화를 시작한 사람에게만 개인 메시지를 보낼 수 있어서,
        오너가 봇과 1:1 채팅을 한 번도 안 했으면 그 오너에게는 조용히 건너뛴다.
        """
        targets = ([self.cfg.log_chat_id] if self.cfg.log_chat_id else []) + sorted(await self.perms.owners())
        for chat_id in targets:
            try:
                await bot.send_message(chat_id, "📣 " + text, parse_mode="HTML")
            except TelegramError as e:
                if chat_id not in self._unreachable:
                    self._unreachable.add(chat_id)
                    log.warning("관리자 보고 전송 실패 (%s): %s — 봇과 1:1 채팅을 먼저 시작해야 받을 수 있어요", chat_id, e)

    # ── 메시지 자동 검사 (관리자는 호출하지 않음) ─────────
    async def check_message(self, bot: Bot, msg: Message, text: str, *, game_active: bool = False) -> str | None:
        """문제가 있으면 처리하고 방에 보낼 안내문을 돌려준다. 정상이면 None.
        빈 문자열 = 지웠지만 안내는 생략(10분 안에 같은 사람에게 이미 안내함) → 호출한 쪽은 여기서 멈춘다."""
        chat_id, user = msg.chat_id, msg.from_user
        s = await self.db.get_settings(chat_id)
        name = user_name(user)
        key = (chat_id, user.id)
        # 0) 잠긴 종류·전달 메시지: 지우고 끝 (도배 수에도 안 넣음)
        blocked = await self._check_kind(msg, s, name)
        if blocked is not None:
            return blocked
        # 포인트 게임 명령(!홀짝 1000 홀 …)은 같은 말을 빠르게 반복하는 게 정상 → 도배·반복 검사에서 뺀다
        # (게임 쪽에 1인 2초 간격 제한이 따로 있음). 금지어·링크 검사는 그대로.
        is_game_cmd = casino.parse(text.strip()) is not None

        # 1) 도배: N초에 M개. 게임 중엔 정답을 빠르게 치니까 기준을 2배로 완화
        # 처리 시각이 아니라 보낸 시각(msg.date) 기준: 재시작 뒤 밀린 메시지를 몰아서 처리해도 간격이 그대로라
        # 정상 멤버가 도배로 뮤트되지 않고, 쉬는 동안 실제로 도배한 건 그대로 잡힌다
        now = msg.date.timestamp() if isinstance(getattr(msg, "date", None), datetime) else time.time()
        limit = s["flood_count"] * (2 if game_active else 1)
        q = self._flood.setdefault(key, deque(maxlen=200))
        if not is_game_cmd:
            q.append(now)
        while q and now - q[0] > s["flood_seconds"]:
            q.popleft()
        if len(q) >= limit:
            q.clear()
            try:
                await self.mute(bot, chat_id, user.id, s["flood_mute_minutes"], None, "도배")
            except TelegramError as e:
                log.warning("flood mute failed: %s", e)
                return None
            await self.report(bot, f"[도배 뮤트] chat {chat_id} / {esc(name)}({user.id}) "
                                   f"{human_minutes(s['flood_mute_minutes'])}")
            return (f"🔇 {mention(user.id, name)}님 {s['flood_seconds']}초에 {s['flood_count']}개 이상 "
                    f"보내셔서 {human_minutes(s['flood_mute_minutes'])} 채팅 금지예요.")

        norm = normalize(text).lower()
        if not norm:
            return None

        # 2) 같은 말 반복 (게임 중 짧은 답은 겹칠 수 있어서 이 검사만 건너뜀)
        if not (game_active and len(norm) <= 10) and not is_game_cmd:
            last, n = self._dups.get(key, ("", 0))
            n = n + 1 if norm == last else 1
            self._dups[key] = (norm, n)
            if n >= s["dup_limit"]:
                self._dups[key] = ("", 0)
                await self._delete(msg)
                return await self.warn(bot, chat_id, user.id, name, bot.id, "같은 메시지 반복")

        return await self._check_content(bot, msg, text, s)

    async def check_edited(self, bot: Bot, msg: Message, text: str) -> str | None:
        """수정된 메시지(캡션 포함) 재검사: 금지어·링크·외부 @아이디만 (도배·반복 수는 세지 않음)."""
        s = await self.db.get_settings(msg.chat_id)
        return await self._check_content(bot, msg, text, s)

    async def _check_content(self, bot: Bot, msg: Message, text: str, s: dict) -> str | None:
        """금지어 → 링크·외부 @아이디. 새 메시지·수정된 메시지 공용."""
        chat_id, user = msg.chat_id, msg.from_user
        name = user_name(user)
        norm = normalize(text).lower()
        # 3) 금지어
        if norm:
            for word in await self.db.banned_words(chat_id):
                if word in norm:
                    await self._delete(msg)
                    return await self.warn(bot, chat_id, user.id, name, bot.id, "금지어 사용")

        # 4) 링크 + 이 방 멤버가 아닌 @아이디 (채널·다른 계정 홍보). 허용 도메인·신규 입장자 규칙 그대로
        domains = find_links(text)
        for ent in [*(msg.entities or ()), *(msg.caption_entities or ())]:
            if ent.type == "text_link" and ent.url:
                domains += find_links(ent.url) or ["link"]
        bad_link = bool(domains) and not link_allowed(domains, s["whitelist_domains"])
        if not bad_link and not (s["promo_mentions"] and await self._outside_mentions(bot, chat_id, text)):
            return None
        newbie = await self._is_newbie(chat_id, user.id, s["newbie_link_hours"])
        if not (s["link_filter"] or newbie):
            return None
        await self._delete(msg)
        if bad_link:
            why = "신규 입장 후 링크 제한 시간이에요" if newbie and not s["link_filter"] else "링크는 관리자만 올릴 수 있어요"
            return f"🔗 {mention(user.id, name)}님, {why}."
        why = ("신규 입장 후엔 다른 채널·계정 @아이디를 올릴 수 없어요" if newbie and not s["link_filter"]
               else "이 방 멤버가 아닌 @아이디(채널·다른 계정 홍보)는 관리자만 올릴 수 있어요")
        return f"🔗 {mention(user.id, name)}님, {why}."

    async def _outside_mentions(self, bot: Bot, chat_id: int, text: str) -> list[str]:
        """홍보로 보는 @아이디: 채널·다른 그룹, 또는 봇(아이디가 bot 으로 끝남). 이 방 멤버·관리자·봇 자신은 제외.
        일반 사람 아이디는 봇 API 로 알 수 없어서 허용 (말 안 한 멤버를 태그한 걸 지우는 오탐 방지)."""
        names = set(find_mentions(text)) - {(getattr(bot, "username", None) or "").lower()}
        out = []
        for n in sorted(names):
            if await self.db._one(
                    "SELECT 1 FROM members m JOIN users u ON u.user_id=m.user_id "
                    "WHERE m.chat_id=? AND u.username=? COLLATE NOCASE", (chat_id, n)):
                continue
            if n.endswith("bot") or await self._is_public_chat(bot, chat_id, n):
                out.append(n)
        if out:  # 관리자는 DB 에 없을 수도 (말 안 한 관리자·다른 관리 봇)
            admins = {(a.username or "").lower() for a in await self.perms.admin_users(bot, chat_id)}
            out = [n for n in out if n not in admins]
        return out

    async def _is_public_chat(self, bot: Bot, chat_id: int, username: str) -> bool:
        """@아이디가 채널·그룹(이 방 제외)인지. 사람 계정은 조회가 안 돼 False. 결과는 하루 저장."""
        now = time.time()
        hit = self._chat_kind.get(username)
        if hit and now - hit[0] < MENTION_CACHE:
            return hit[1]
        try:
            chat = await bot.get_chat(f"@{username}")
            public = chat.type in ("channel", "group", "supergroup") and chat.id != chat_id
        except TelegramError:
            public = False                      # 사람 계정·없는 아이디
        if len(self._chat_kind) > 5000:
            self._chat_kind.clear()
        self._chat_kind[username] = (now, public)
        return public

    async def _check_kind(self, msg: Message, s: dict, name: str) -> str | None:
        """잠긴 종류·막힌 전달 메시지면 지우고 안내문(10분 안에 또 걸리면 빈 문자열), 괜찮으면 None."""
        chat_id, uid = msg.chat_id, msg.from_user.id
        label = next((label for key, label, has in LOCK_KINDS if s.get(key) and has(msg)), None)
        if label:
            await self._delete(msg)
            return self._kind_notice(chat_id, uid, f"🔒 {mention(uid, name)}님, 이 방은 {label} 금지라 지웠어요.")
        mode = s.get("forward_filter", "off")
        if mode != "off" and _forwarded(msg):
            if mode == "all":
                why = "이 방은 전달(포워드) 메시지 금지라 지웠어요."
            elif await self._is_newbie(chat_id, uid, s["newbie_link_hours"]):
                why = f"들어온 지 {s['newbie_link_hours']}시간 동안은 전달(포워드) 메시지를 올릴 수 없어요."
            else:
                return None
            await self._delete(msg)
            return self._kind_notice(chat_id, uid, f"📨 {mention(uid, name)}님, {why}")
        return None

    def _kind_notice(self, chat_id: int, uid: int, text: str) -> str:
        now = time.time()
        if len(self._kind_noticed) > 5000:
            self._kind_noticed = {k: t for k, t in self._kind_noticed.items() if now - t < KIND_NOTICE_GAP}
        if now - self._kind_noticed.get((chat_id, uid), 0) < KIND_NOTICE_GAP:
            return ""
        self._kind_noticed[(chat_id, uid)] = now
        return text

    async def _is_newbie(self, chat_id: int, user_id: int, hours: int) -> bool:
        if hours <= 0:
            return False
        member = await self.db.get_member(chat_id, user_id)
        joined = member["joined_at"] if member else None
        return bool(joined) and time.time() - joined < hours * 3600

    async def _delete(self, msg: Message) -> None:
        try:
            await msg.delete()
        except TelegramError as e:
            log.info("delete failed: %s", e)

    # ── 관리자 사칭 ───────────────────────────────────────
    async def check_impersonation(self, bot: Bot, chat_id: int, user: User) -> str | None:
        s = await self.db.get_settings(chat_id)
        if not s["impersonation_guard"] or user.is_bot:
            return None
        full = _squash(f"{user.first_name or ''}{user.last_name or ''}")
        uname = _squash(user.username or "")
        # 사람(ID)별로, '통과한' 결과만 캐시. 같은 이름의 다른 계정은 다시 검사해야 한다
        cache_key = (chat_id, user.id, full + "|" + uname)
        if cache_key in self._checked_names:
            return None
        if await self.perms.is_admin(bot, chat_id, user.id):
            self._checked_names.add(cache_key)
            return None

        suspicious = None
        for a in await self.perms.admin_users(bot, chat_id):
            if a.id == user.id or a.is_bot:
                continue
            a_full = _squash(f"{a.first_name or ''}{a.last_name or ''}")
            a_uname = _squash(a.username or "")
            if (a_full and full == a_full) or (a_uname and uname and uname.strip("_") == a_uname):
                suspicious = user_name(a)
                break
            if a_full and len(a_full) >= 2 and a_full in full and any(w in full for w in _IMPERSONATE_WORDS):
                suspicious = user_name(a)
                break
        if not suspicious:
            self._checked_names.add(cache_key)
            return None
        try:
            await self.mute(bot, chat_id, user.id, None, bot.id, f"관리자({suspicious}) 사칭 의심")
        except TelegramError as e:
            log.warning("impersonation mute failed: %s", e)
            return None
        text = (f"🛡️ {mention(user.id, user_name(user))}님은 관리자 {esc(suspicious)}님과 이름이 비슷해서 "
                f"사칭 방지로 채팅을 막았어요. 오해라면 관리자가 <code>.뮤트해제</code> 해주세요.\n"
                f"※ 관리자는 절대 먼저 개인 메시지로 송금·코인을 요구하지 않아요.")
        await self.report(bot, f"[사칭 의심] chat {chat_id} / user {user.id} → {esc(suspicious)}")
        return text
