"""📢 채널 관리 (소담이 채널 관리자가 되면 자동 등록 — 헬퍼 셋팅).

- 등록: my_chat_member(채널)로 관리자 지정·권한 변경·해제를 받아 channels 에 권한(글 올리기·수정·삭제·초대)·토론 그룹
  (getChat.linked_chat_id)·지정한 사람을 적고, 지정한 사람 1:1 에 빠진 권한 체크리스트. 토론 그룹이 소담이 있는 방이고
  지정한 사람이 그 방 관리자면 그 방과 자동 연결.
- 관리자(매니저) = 그 채널의 지금 텔레그램 관리자(getChatAdministrators, 방과 같은 캐시) + 봇 오너. 누를 때마다 다시 확인.
- 이용: 결제는 방 단위 그대로 — 이용 중(구독·체험)인 방과 연결된 채널이면 예약 글·AI 초안·새 글 알림 가능 (방 1개당 채널 1개,
  연결은 그 방의 텔레그램 관리자만). 지금 올리기·가입 신청·구독자 추이는 무료. 금액은 여기 어디에도 없음.
- 예약 글(channel_sched): 초안(composer_drafts, state=scheduled)을 announce 와 같은 시각 규칙(parse_time·is_due)으로.
  실행은 hooks 틱(30초)에서 last_sent 를 한 문장 UPDATE 로 차지 → 한 번만. 만든 사람이 더는 관리자가 아니면 끔.
- 채널 새 글(channel_post)은 channel_msgs 에 글자만 → 연결된 방 AI 가 channel_posts 도구로 (nonce 데이터). 새 글 알림(기본 꺼짐, 10분 1번).
- 구독자 수는 하루 1번 getChatMemberCount → channel_stats. 들어온·나간 수는 chat_member 업데이트로 셈(counters ch_join/ch_left),
  svc.mtproto 가 있으면 전체 명단 비교로도 (mtproto.participants — 없으면 건너뜀).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError

from . import announce, composer, hooks, joinreq
from .db import register_schema
from .util import esc, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

register_schema("""
CREATE TABLE IF NOT EXISTS channels (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    username   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,     -- 소담이 지금 관리자
    can_post   INTEGER NOT NULL DEFAULT 0,
    can_edit   INTEGER NOT NULL DEFAULT 0,
    can_delete INTEGER NOT NULL DEFAULT 0,
    can_invite INTEGER NOT NULL DEFAULT 0,
    linked_chat_id INTEGER,                    -- 텔레그램 토론 그룹
    added_by   INTEGER,
    room_id    INTEGER,                        -- 연결한 방 (방 1개당 채널 1개)
    notify     INTEGER NOT NULL DEFAULT 0,     -- 방에 새 글 알림
    notify_at  INTEGER NOT NULL DEFAULT 0,
    join_mode  TEXT NOT NULL DEFAULT 'off',    -- off / puzzle / manual
    updated    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS channels_room ON channels(room_id) WHERE room_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS channel_msgs (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    text       TEXT NOT NULL DEFAULT '',       -- 글자만 (방 AI 참고용)
    draft      TEXT,                           -- 소담이 올린 글: 고칠 때 쓸 원본 (composer 초안 JSON)
    by_user    INTEGER,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS channel_sched (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    draft_id     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    at_time      TEXT,
    interval_min INTEGER,
    at_ts        INTEGER,
    last_sent    INTEGER,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_stats (
    chat_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    n       INTEGER NOT NULL,
    joined  INTEGER,                           -- mtproto 명단 비교 (있을 때만)
    left_n  INTEGER,
    PRIMARY KEY (chat_id, day)
);
CREATE TABLE IF NOT EXISTS channel_subs (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS channel_joinreqs (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name    TEXT NOT NULL DEFAULT '',
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""")

RIGHTS = (("can_post", "can_post_messages", "✍️ 글 올리기"), ("can_edit", "can_edit_messages", "✏️ 글 수정"),
          ("can_delete", "can_delete_messages", "🗑 글 삭제"), ("can_invite", "can_invite_users", "🙋 사용자 초대 (가입 신청)"))
JOIN_MODES = {"off": "끔 (텔레그램에서 직접)", "puzzle": "🧩 1:1 그림 확인", "manual": "✋ 여기서 승인"}
MAX_SCHED = 20
NOTIFY_GAP = 600
KEEP_MSGS = 90 * 86400
_snap_fail: dict[int, str] = {}    # 오늘 구독자 수를 못 받은 채널 (30초마다 다시 묻지 않게)


# ── 조회 · 권한 ───────────────────────────────────────────
async def get(db, chat_id: int):
    return await db._one("SELECT * FROM channels WHERE chat_id=?", (chat_id,))


async def is_manager(svc: Services, bot, chat_id: int, uid: int, fresh: bool = False) -> bool:
    """채널의 지금 텔레그램 관리자 또는 오너. fresh = 캐시 말고 텔레그램에 다시 (올리기·예약·승인 같은 실행)."""
    try:
        if uid in await svc.perms.owners():
            return True
        if fresh:
            svc.perms.forget(chat_id)
        return await svc.perms.is_tg_admin(bot, chat_id, uid)
    except TelegramError:
        return False


async def mine(svc: Services, bot, uid: int) -> list:
    """이 사람이 관리하는 등록 채널. 관리자 목록 캐시(chat_admins)에 있거나 아직 안 받은 채널만 텔레그램에 확인."""
    if uid in await svc.perms.owners():
        return await svc.db._all("SELECT * FROM channels ORDER BY active DESC, title")
    rows = await svc.db._all(
        "SELECT ch.* FROM channels ch LEFT JOIN chat_admins_fetched f ON f.chat_id=ch.chat_id WHERE ch.active=1 AND "
        "(f.ts IS NULL OR f.ts < ? OR EXISTS(SELECT 1 FROM chat_admins a WHERE a.chat_id=ch.chat_id AND a.user_id=?)) "
        "ORDER BY ch.title LIMIT 50", (int(time.time()) - 86400, uid))
    return [r for r in rows if await is_manager(svc, bot, r["chat_id"], uid)]


async def visible(svc: Services, bot, uid: int) -> bool:
    """메인 메뉴 '📢 내 채널' 버튼: 오너 또는 등록 채널을 하나라도 관리하는 사람."""
    return uid in await svc.perms.owners() or bool(await mine(svc, bot, uid))


async def covered(svc: Services, ch) -> bool:
    """예약 글·AI 초안·새 글 알림: 결제가 꺼져 있거나, 연결된 방이 이용 중(구독·체험)."""
    if not (svc.billing and svc.billing.enabled):
        return True
    return bool(ch["room_id"]) and await svc.billing.active(ch["room_id"])


def checklist(ch) -> str:
    lines = [f"{'✅' if ch[k] else '❌'} {label}" for k, _, label in RIGHTS]
    missing = [label for k, _, label in RIGHTS if not ch[k]]
    if missing:
        lines.append("\n빠진 권한은 채널 → 관리자 → 소담 에서 켜 주세요.\n"
                     "(✍️ 글 올리기는 꼭 필요해요 · 🙋 초대는 가입 신청을 받을 때만)")
    return "\n".join(lines)


def post_link(ch, msg_id: int) -> str:
    if ch["username"]:
        return f"https://t.me/{ch['username']}/{msg_id}"
    return f"https://t.me/c/{str(ch['chat_id']).removeprefix('-100')}/{msg_id}"


async def _dm(bot, uid: int | None, text: str, kb=None) -> bool:
    if not uid:
        return False
    try:
        await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        return True
    except TelegramError as e:
        log.info("channel dm %s failed: %s", uid, e)
        return False


def _open_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("📢 채널 관리 열기", callback_data=f"m:ch:{chat_id}")]])


# ── 소담 권한 변경 (my_chat_member) ───────────────────────
async def on_bot_status(svc: Services, bot, cmu) -> None:
    chat, new, by = cmu.chat, cmu.new_chat_member, cmu.from_user
    svc.perms.forget(chat.id)
    svc.perms.forget_bot(chat.id)
    old = await get(svc.db, chat.id)
    now = int(time.time())
    if new.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):   # 빠짐·강등
        if not old or not old["active"]:
            return
        await svc.db.atomic(lambda c: (
            c.execute("UPDATE channels SET active=0, can_post=0, can_edit=0, can_delete=0, can_invite=0, updated=? "
                      "WHERE chat_id=?", (now, chat.id)),
            c.execute("UPDATE channel_sched SET enabled=0 WHERE chat_id=?", (chat.id,))))
        await svc.mod.report(bot, f"[채널 해제] {esc(old['title'])} ({chat.id}) by "
                                  f"{esc(user_name(by)) if by else '?'} · 예약 글 멈춤")
        return
    rights = {col: int(bool(getattr(new, attr, False))) for col, attr, _ in RIGHTS}
    try:
        linked = getattr(await bot.get_chat(chat.id), "linked_chat_id", None)
    except TelegramError:
        linked = old["linked_chat_id"] if old else None
    adder = by.id if by and not by.is_bot else (old["added_by"] if old else None)
    changed = not old or not old["active"] or any(old[k] != v for k, v in rights.items())
    room = old["room_id"] if old else None
    if room is None and linked and await svc.db.has_chat(linked) and adder and await is_manager(svc, bot, linked, adder, True):
        room = linked      # 토론 그룹이 소담이 있는 방이고 지정한 사람이 그 방 관리자 → 자동 연결 (이미 다른 채널이면 아래서 안 함)
    vals = dict(title=chat.title or "", username=chat.username, active=1, **rights, linked_chat_id=linked,
                added_by=adder, updated=now)

    def run(c):
        c.execute(f"INSERT INTO channels(chat_id, {', '.join(vals)}) VALUES(?{', ?' * len(vals)}) ON CONFLICT(chat_id) "
                  f"DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in vals)}", (chat.id, *vals.values()))
        if room is not None:
            c.execute("UPDATE channels SET room_id=? WHERE chat_id=? AND room_id IS NULL AND NOT EXISTS("
                      "SELECT 1 FROM channels WHERE room_id=?)", (room, chat.id, room))
    await svc.db.atomic(run)
    if not changed:
        return
    ch = await get(svc.db, chat.id)
    head = "관리자 권한이 바뀌었어요" if old and old["active"] else "관리자로 들어왔어요!"
    text = (f"📢 <b>{esc(ch['title'])}</b> 채널에 소담이 {head}\n\n<b>소담 권한 확인</b>\n{checklist(ch)}\n\n"
            "버튼으로 새 글(HTML 서식·사진·URL 버튼)·예약 글·가입 신청·구독자 추이를 관리할 수 있어요.")
    if ch["room_id"] and not (old and old["room_id"]):
        text += "\n🔗 토론 그룹 방과 자동으로 연결했어요."
    if not old:
        await svc.mod.report(bot, f"[채널 등록] {esc(ch['title'])} ({chat.id}) by {esc(user_name(by)) if by else '?'}")
    await _dm(bot, adder, text, _open_kb(chat.id))


async def sync(svc: Services, bot, chat_id: int, title: str | None = None, username: str | None = None):
    """텔레그램에 소담 권한·토론 그룹을 지금 다시 물어 저장 (🔧 다시 확인 · 기능 배포 전부터 관리자였던 채널의 첫 글).
    연결된 방·지정한 사람은 그대로. 확인 못 하면 TelegramError."""
    me = await bot.get_chat_member(chat_id, bot.id)
    full = await bot.get_chat(chat_id)
    boss = me.status == ChatMemberStatus.OWNER
    admin = boss or me.status == ChatMemberStatus.ADMINISTRATOR
    vals = {col: int(admin and (boss or bool(getattr(me, attr, False)))) for col, attr, _ in RIGHTS}
    vals |= dict(active=int(admin), linked_chat_id=getattr(full, "linked_chat_id", None), updated=int(time.time()))
    title = title or getattr(full, "title", None)
    await svc.db._write(
        f"INSERT INTO channels(chat_id, title, username, {', '.join(vals)}) VALUES(?, ?, ?{', ?' * len(vals)}) "
        f"ON CONFLICT(chat_id) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in vals)}, "
        "title=COALESCE(NULLIF(?, ''), title), username=COALESCE(?, username)",
        (chat_id, title or "", username, *vals.values(), title or "", username))
    return await get(svc.db, chat_id)


async def on_member(svc: Services, bot, cmu) -> None:
    """채널 구독자 들어옴·나감(소담이 관리자라 받음) → 오늘 셈. 관리자 변경이면 관리자 캐시 비움."""
    old, new = cmu.old_chat_member, cmu.new_chat_member
    admin = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    if old.status != new.status and (old.status in admin or new.status in admin):
        svc.perms.forget(cmu.chat.id)
    if not await get(svc.db, cmu.chat.id):
        return
    inside = (ChatMemberStatus.MEMBER, *admin)
    was, now = old.status in inside, new.status in inside
    if was != now:
        await svc.db.bump(_day(svc), cmu.chat.id, "ch_join" if now else "ch_left")


def _day(svc: Services, ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), svc.cfg.tz).strftime("%Y-%m-%d")


# ── 채널 새 글 (channel_post) ─────────────────────────────
async def record(svc: Services, chat_id: int, msg_id: int, text: str, draft: dict | None = None,
                 by: int | None = None) -> None:
    await svc.db._write(
        "INSERT INTO channel_msgs(chat_id, msg_id, ts, text, draft, by_user) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(chat_id, msg_id) DO UPDATE SET text=excluded.text, draft=COALESCE(excluded.draft, draft)",
        (chat_id, msg_id, int(time.time()), " ".join(text.split())[:500],
         None if draft is None else json.dumps(draft, ensure_ascii=False), by))


async def on_post(svc: Services, bot, msg, edited: bool = False) -> None:
    ch = await get(svc.db, msg.chat_id)
    if not ch:   # 이 기능 전부터 소담이 관리자였던 채널 (my_chat_member 를 못 받음) → 첫 글에서 등록
        chat = getattr(msg, "chat", None)
        try:
            ch = await sync(svc, bot, msg.chat_id, getattr(chat, "title", None), getattr(chat, "username", None))
        except TelegramError as e:
            log.info("channel sync %s failed: %s", msg.chat_id, e)
            return
    await record(svc, msg.chat_id, msg.message_id, msg.text or msg.caption or "")
    if not edited:
        await notify_room(svc, bot, ch, msg.message_id, msg.text or msg.caption or "")


async def notify_room(svc: Services, bot, ch, msg_id: int, text: str) -> None:
    """연결된 방에 새 글 알림 (켠 경우만, 10분에 1번 — 한 문장 UPDATE 로 차지)."""
    if not ch["notify"] or not ch["room_id"] or not await covered(svc, ch):
        return
    now = int(time.time())
    if not await svc.db.atomic(lambda c: c.execute("UPDATE channels SET notify_at=? WHERE chat_id=? AND notify_at<=?",
                                                   (now, ch["chat_id"], now - NOTIFY_GAP)).rowcount):
        return
    head = " ".join(text.split())[:120] or "(사진·영상)"
    try:
        await bot.send_message(ch["room_id"], f"📢 <b>{esc(ch['title'])}</b> 새 글\n{esc(head)}", parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                   "👀 채널에서 보기", url=post_link(ch, msg_id))]]))
    except TelegramError as e:
        log.info("channel notify %s failed: %s", ch["room_id"], e)


async def recent_posts(svc: Services, room_id: int, limit: int = 5):
    ch = await svc.db._one("SELECT * FROM channels WHERE room_id=?", (room_id,))
    if not ch:
        return None, []
    rows = await svc.db._all("SELECT * FROM channel_msgs WHERE chat_id=? AND ts>=? AND text!='' "
                             "ORDER BY ts DESC, msg_id DESC LIMIT ?",
                             (ch["chat_id"], int(time.time()) - 14 * 86400, max(1, min(limit, 10))))
    return ch, rows


# ── 가입 신청 ─────────────────────────────────────────────
async def on_join_request(svc: Services, bot, req) -> None:
    ch = await get(svc.db, req.chat.id)
    if not ch or ch["join_mode"] == "off":
        return
    if ch["join_mode"] == "puzzle" and await joinreq.verify(svc, bot, req, ch["title"]):
        return
    await svc.db._write("INSERT OR REPLACE INTO channel_joinreqs(chat_id, user_id, name, ts) VALUES(?,?,?,?)",
                        (req.chat.id, req.from_user.id, user_name(req.from_user)[:64], int(time.time())))


async def decide(svc: Services, bot, chat_id: int, uid: int, approve: bool) -> str | None:
    """✋ 직접 승인 목록의 [✅]/[❌]. 줄을 먼저 지워 차지(연타·동시 관리자에도 한 번). 실패 이유 또는 None."""
    if not await svc.db.atomic(lambda c: c.execute(
            "DELETE FROM channel_joinreqs WHERE chat_id=? AND user_id=?", (chat_id, uid)).rowcount):
        return "이미 처리된 신청이에요."
    try:
        if approve:
            await bot.approve_chat_join_request(chat_id, uid)
        else:
            await bot.decline_chat_join_request(chat_id, uid)
    except TelegramError as e:
        return f"텔레그램에서 처리하지 못했어요 (이미 처리됐거나 취소된 신청): {e.message[:60]}"
    await svc.db.log_mod(chat_id, None, uid, "join_pass" if approve else "join_decline", "채널 가입 신청 (직접)")
    return None


# ── 예약 글 ───────────────────────────────────────────────
async def post(svc: Services, bot, ch, d, by: int):
    """초안 1개를 채널에 올리고 기록 (+ 새 글 알림). TelegramError 는 부른 쪽에서."""
    sent = await composer.send(bot, ch["chat_id"], d)
    snap = {k: d[k] for k in ("body", "media_type", "media_id", "buttons", "preview")}
    await record(svc, ch["chat_id"], sent.message_id, composer.plain(d["body"]), snap, by)
    await notify_room(svc, bot, ch, sent.message_id, composer.plain(d["body"]))
    return sent


async def fire(svc: Services, bot, row, now: int) -> bool:
    """예약 1건: last_sent 를 먼저 차지(한 번만) → 권한·이용·초안 확인 → 올림. 올렸으면 True."""
    if not await svc.db.atomic(lambda c: c.execute(
            "UPDATE channel_sched SET last_sent=?, enabled=CASE WHEN kind='once' THEN 0 ELSE enabled END "
            "WHERE id=? AND enabled=1 AND last_sent IS ?", (now, row["id"], row["last_sent"])).rowcount):
        return False
    ch, d, who = await get(svc.db, row["chat_id"]), await composer.get(svc.db, row["draft_id"]), row["created_by"]
    why = None
    if not ch or not ch["active"] or not d:
        why = "채널에서 소담이 빠졌거나 글이 없어졌어요"
    elif not await is_manager(svc, bot, ch["chat_id"], who, fresh=True):
        why = "만든 분이 더는 이 채널 관리자가 아니에요"
    elif not await covered(svc, ch):
        why = "연결된 방의 이용 기간이 끝났어요"
    stop = True
    if why is None:
        try:
            await post(svc, bot, ch, d, who)
            if row["kind"] == "once":   # 한 번 예약은 올렸으면 목록에서 빠짐
                await svc.db.atomic(lambda c: (c.execute("UPDATE composer_drafts SET state='posted' WHERE id=?", (d["id"],)),
                                               c.execute("DELETE FROM channel_sched WHERE id=?", (row["id"],))))
            return True
        except TelegramError as e:
            log.warning("channel sched #%s failed: %s", row["id"], e)
            why = f"텔레그램 오류: {esc(e.message[:100])}"
            stop = isinstance(e, (Forbidden, BadRequest))   # 연결이 잠깐 끊긴 거면 반복 예약은 다음 회차에 다시
    if stop:
        await svc.db._write("UPDATE channel_sched SET enabled=0 WHERE id=?", (row["id"],))
    await _dm(bot, who, f"🗓️ 예약 글 <code>#{row['id']}</code> 을 못 올렸어요{'. 예약을 멈췄어요' if stop else ''}.\n이유: {why}",
              _open_kb(row["chat_id"]) if ch else None)
    return False


async def run_due(svc: Services, bot) -> None:
    now = int(time.time())
    for row in await svc.db._all("SELECT * FROM channel_sched WHERE enabled=1"):
        if row["kind"] == "once" and now - (row["at_ts"] or 0) >= announce.ONCE_GRACE:   # 너무 늦게 켜짐
            await svc.db._write("UPDATE channel_sched SET enabled=0 WHERE id=?", (row["id"],))
            await _dm(bot, row["created_by"], f"🗓️ 예약 글 <code>#{row['id']}</code> 은 봇이 꺼져 있던 사이 시각을 놓쳐 멈췄어요.")
        elif announce.is_due(row, now, svc.cfg.tz):
            await fire(svc, bot, row, now)


async def schedule(svc: Services, did: int, chat_id: int, uid: int, when: tuple) -> int | None:
    """초안을 예약으로 (draft → scheduled 차지와 예약 줄을 한 번에). 채널당 MAX_SCHED 개. 못 하면 None."""
    kind, at_time, interval, at_ts = when

    def run(c):
        if c.execute("SELECT COUNT(*) FROM channel_sched WHERE chat_id=? AND enabled=1", (chat_id,)).fetchone()[0] >= MAX_SCHED:
            return None
        if not c.execute("UPDATE composer_drafts SET state='scheduled' WHERE id=? AND state='draft'", (did,)).rowcount:
            return None
        return c.execute("INSERT INTO channel_sched(chat_id, draft_id, kind, at_time, interval_min, at_ts, created_by) "
                         "VALUES(?,?,?,?,?,?,?)", (chat_id, did, kind, at_time, interval, at_ts, uid)).lastrowid
    return await svc.db.atomic(run)


async def unschedule(svc: Services, chat_id: int, sid: int) -> bool:
    def run(c):
        row = c.execute("SELECT draft_id FROM channel_sched WHERE id=? AND chat_id=?", (sid, chat_id)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM channel_sched WHERE id=?", (sid,))
        c.execute("DELETE FROM composer_drafts WHERE id=?", (row[0],))
        return True
    return await svc.db.atomic(run)


# ── 구독자 수 (하루 1번) ──────────────────────────────────
async def snapshot(svc: Services, bot) -> None:
    day = _day(svc)
    rows = await svc.db._all("SELECT chat_id FROM channels WHERE active=1 AND chat_id NOT IN "
                             "(SELECT chat_id FROM channel_stats WHERE day=?)", (day,))
    for r in rows:
        cid = r["chat_id"]
        if _snap_fail.get(cid) == day:
            continue
        try:
            n = await bot.get_chat_member_count(cid)
        except TelegramError as e:
            _snap_fail[cid] = day
            log.info("channel count %s failed: %s", cid, e)
            continue
        joined = left = ids = None
        mt = getattr(svc, "mtproto", None)      # 선택: MTProto 로 전체 명단을 받을 수 있으면 새로 온·나간 사람 수
        try:
            users = await mt.participants(cid) if mt else None
        except Exception:
            log.exception("mtproto participants %s failed", cid)
            users = None
        if users is not None:
            ids = {u.id if hasattr(u, "id") else int(u) for u in users}
            prev = {x["user_id"] for x in await svc.db._all("SELECT user_id FROM channel_subs WHERE chat_id=?", (cid,))}
            joined, left = (len(ids - prev), len(prev - ids)) if prev else (None, None)
        stats = (cid, day, n, joined, left)

        def run(c, ids=ids):
            c.execute("INSERT OR IGNORE INTO channel_stats(chat_id, day, n, joined, left_n) VALUES(?,?,?,?,?)", stats)
            if ids is not None:
                c.execute("DELETE FROM channel_subs WHERE chat_id=?", (cid,))
                c.executemany("INSERT INTO channel_subs(chat_id, user_id) VALUES(?,?)", [(cid, u) for u in ids])
        await svc.db.atomic(run)
    if rows:
        await svc.db._write("DELETE FROM channel_msgs WHERE ts<?", (int(time.time()) - KEEP_MSGS,))


async def tick(svc: Services, bot) -> None:
    await run_due(svc, bot)
    await snapshot(svc, bot)


hooks.add_tick_hook(tick)
