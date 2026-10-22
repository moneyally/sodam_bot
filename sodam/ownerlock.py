"""🔒 오너 잠금 제재: 오너가 건 영구 뮤트/밴은 **오너만** 풀 수 있다 (관리자도 못 풂).

왜 (2026-10-22 오너 '오너 아니면 관리자도 절대 못 풀게'): 백문주 같은 사람을 관리자가 몰래 풀지 못하게.
음챗까지 막으려면 밴이어야 함 — 텔레그램이 음챗 발언권을 채팅 권한과 **따로** 관리해서 Bot API 엔 '음챗 금지'가 없음
(조사 2026-10-22, "Can Talk" 권한은 미구현 요청). 그래서 뮤트·밴 둘 다 잠금 지원. 백문주는 영구 잠금 뮤트.

세 겹:
① 소담 통한 해제(.뮤트해제·.밴해제·.free·AI)는 `moderation.unmute`/`unban` 이 `OwnerLocked` 로 거절 (actor 가 오너 아니면).
② 관리자가 텔레그램 앱에서 직접 풀면 `handlers.on_chat_member` → `on_status_change` 가 알아채고 **즉시 다시** 걸음 + 오너 1:1 알림.
③ 넣고 빼기는 오너만 (`.잠금뮤트`/`.잠금밴`/`.잠금해제`, AI 도구 `owner_lock`, 둘 다 Role.OWNER).

구조는 protect.py 를 따름 (방별 캐시, add/remove 가 즉시 비움). 오너가 **직접 앱에서** 풀면 오너 의도로 보고 잠금도 해제.
"""
from __future__ import annotations

import logging
import time

from telegram.constants import ChatMemberStatus as S
from telegram.error import TelegramError

from . import persist
from .db import DB, register_schema

log = logging.getLogger(__name__)

CLAIM_SECONDS = 20       # 같은 '풀림'이 두 길(서비스 메시지·chat_member)로 와도 재적용 한 번만
KINDS = ("mute", "ban")

register_schema("""
CREATE TABLE IF NOT EXISTS owner_locked (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'mute',   -- 'mute' | 'ban'
    by_id   INTEGER,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"owner_locked": "composite"})


async def locked_kind(db: DB, chat_id: int, user_id: int) -> str | None:
    """이 사람에게 걸린 오너 잠금 종류('mute'·'ban') 또는 None. 방마다 캐시 (add/remove 가 지움)."""
    async def load():
        return {r["user_id"]: r["kind"] for r in
                await db._all("SELECT user_id, kind FROM owner_locked WHERE chat_id=?", (chat_id,))}
    return (await db.cached(("owner_locked", chat_id), load)).get(user_id)


async def is_locked(db: DB, chat_id: int, user_id: int, kind: str | None = None) -> bool:
    k = await locked_kind(db, chat_id, user_id)
    return k is not None and (kind is None or k == kind)


async def add(db: DB, chat_id: int, user_id: int, kind: str, by_id: int) -> None:
    await db._write("INSERT OR REPLACE INTO owner_locked(chat_id, user_id, kind, by_id, ts) VALUES(?, ?, ?, ?, ?)",
                    (chat_id, user_id, kind if kind in KINDS else "mute", by_id, int(time.time())))
    db.uncache(("owner_locked", chat_id))


async def remove(db: DB, chat_id: int, user_id: int) -> bool:
    if await locked_kind(db, chat_id, user_id) is None:
        return False
    await db._write("DELETE FROM owner_locked WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    db.uncache(("owner_locked", chat_id))
    return True


async def members(db: DB, chat_id: int) -> list:
    return await db._all("SELECT o.user_id, o.kind, u.first_name, u.last_name, u.username FROM owner_locked o "
                         "LEFT JOIN users u ON u.user_id=o.user_id WHERE o.chat_id=? ORDER BY o.ts", (chat_id,))


def _can_send(m) -> bool:
    """이 멤버 상태로 지금 글을 쓸 수 있나 (= 뮤트가 안 걸린 상태)."""
    st = getattr(m, "status", "")
    if st in (S.ADMINISTRATOR, S.OWNER, S.MEMBER):
        return True
    if st == S.RESTRICTED:
        return bool(getattr(m, "can_send_messages", False))
    return False     # left / kicked(banned) → 못 씀


async def _tell_owners(svc, bot, text: str) -> None:
    for oid in await svc.perms.owners():
        try:
            await bot.send_message(oid, text, parse_mode="HTML")
        except TelegramError:
            pass


async def on_status_change(context, cmu) -> None:
    """handlers.on_chat_member 이 멤버 상태 변경마다 부름. 잠긴 사람이 풀리면 즉시 다시 건다.
    오너가 **직접** 풀면 오너 의도 → 잠금 해제. 관리자가 풀면 재적용 + 오너 알림. 실패해도 다른 처리는 계속."""
    try:
        svc = context.bot_data["svc"]
        chat_id = cmu.chat.id
        old, new = cmu.old_chat_member, cmu.new_chat_member
        uid = new.user.id
        kind = await locked_kind(svc.db, chat_id, uid)
        if not kind:
            return
        by = getattr(cmu, "from_user", None)
        by_id = getattr(by, "id", None)
        if by_id == context.bot.id:
            return                                  # 소담 자신이 만든 변화 (우리가 다시 건 것 등)

        if kind == "ban":
            was_blocked = old.status == S.BANNED
            now_free = new.status != S.BANNED
        else:  # mute
            was_blocked = not _can_send(old)
            now_free = _can_send(new)
        if not (was_blocked and now_free):
            return                                  # 풀린 게 아님

        owners = await svc.perms.owners()
        if by_id in owners:                          # 오너가 직접 풀었으면 잠금도 해제 (오너 의도)
            await remove(svc.db, chat_id, uid)
            return
        if not await persist.claim(svc.db, f"ownerlock:{chat_id}:{uid}", CLAIM_SECONDS):
            return

        from .util import mention
        try:
            name = (new.user.full_name or "").strip() or str(uid)
        except Exception:
            name = str(uid)
        who = mention(uid, name)
        promoted = new.status in (S.ADMINISTRATOR, S.OWNER)
        if promoted:                                 # 관리자로 승격당하면 소담이 못 걺 (관리자는 제재 불가)
            await _tell_owners(svc, context.bot, f"🔒 오너 잠금: {who}님이 관리자로 승격돼서 다시 {'밴' if kind=='ban' else '뮤트'}을 "
                               "못 걸었어요. 강등해야 걸 수 있어요.")
            return
        try:
            if kind == "ban":
                await svc.mod.ban(context.bot, chat_id, uid, context.bot.id, "오너 잠금 밴 — 자동 재적용")
            else:
                await svc.mod.mute(context.bot, chat_id, uid, None, context.bot.id, "오너 잠금 뮤트 — 자동 재적용")
        except TelegramError as e:
            log.warning("오너 잠금 재적용 실패 %s/%s: %s", chat_id, uid, e)
            await _tell_owners(svc, context.bot, f"🔒 오너 잠금: {who}님을 누가 풀어서 다시 걸려 했는데 실패했어요 "
                               f"(봇 '사용자 차단' 권한 확인): {e.message}")
            return
        await svc.db.log_mod(chat_id, context.bot.id, uid, "ownerlock", f"자동 재적용({kind})")
        await _tell_owners(svc, context.bot, f"🔒 오너 잠금: 관리자가 {who}님을 풀어서 소담이 바로 다시 "
                           f"{'밴' if kind == 'ban' else '뮤트'}했어요.")
    except Exception:
        log.exception("ownerlock on_status_change failed in %s", getattr(getattr(cmu, "chat", None), "id", "?"))
