"""예약·반복 공지.

관리자가 단계별로 만든다 — 방 안에서(.예약공지 만들기) 또는 1:1 버튼 메뉴(🗓️ 예약공지 → ➕/✏️)에서:
  1) 제목 → 2) 내용 (글 또는 사진·영상·GIF·파일 + 설명) → 3) 시간 (매일 09:00 / 반복 120)
  → 4) 고정 여부 버튼 → 미리보기 → 저장
Draft.ui_chat_id = 대화가 오가는 곳(방 또는 관리자 1:1), Draft.chat_id = 공지를 올릴 방.
drafts 키는 (ui_chat_id, user_id) → 1:1 에선 (uid, uid) 라서 1:1 메시지로 찾을 수 있다.
만드는 동안 오간 메시지는 저장/취소 시 지워서 채팅을 깨끗하게 둔다. 저장할 때 그 방 관리자인지 다시 확인한다.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, TelegramError

from . import persist
from .db import register_schema
from .settings import parse_hhmm
from .util import esc, html_plain, rich_html

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

MAX_PER_CHAT = 20
WIZARD_TTL = 600
DAILY_GRACE = 15 * 60          # 정각을 놓쳤을 때(재시작 등) 이 시간 안이면 늦게라도 보냄
MIN_INTERVAL, MAX_INTERVAL = 30, 7 * 1440
CAPTION_LIMIT, TEXT_LIMIT = 1024, 4096
MEDIA_LABEL = {"photo": "사진", "video": "영상", "animation": "GIF", "document": "파일"}
KEEP = {"그대로", "유지", "="}
CANCEL = {"취소", ".취소", "/cancel"}


# ── 순수 함수 (테스트 대상) ───────────────────────────────
def extract_media(msg: Message) -> tuple[str | None, str | None]:
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.animation:  # GIF 는 document 도 같이 채워져 오므로 먼저 확인
        return "animation", msg.animation.file_id
    if msg.document:
        return "document", msg.document.file_id
    return None, None


_WHEN_DAILY = re.compile(r"^(?:매일|daily)?\s*(\d{1,2}:\d{2})$", re.I)
DAYS = "월화수목금토일"                  # datetime.weekday() 순서
_WHEN_WEEKLY = re.compile(r"^(?:매주\s*((?:[월화수목금토일](?:요일)?[,·\s]*)+)|(평일|주말))\s*(\d{1,2}:\d{2})$")
_WHEN_EVERY = re.compile(r"^(?:반복|매|every)\s*(\d+)\s*(분|m|min|시간|h)?(?:\s*마다)?$", re.I)


def parse_when(text: str) -> tuple[str, str | None, int | None]:
    """'매일 09:00' → ('daily','09:00',None) / '반복 120' '반복 2시간' → ('interval',None,120)
    '매주 월 10:00' '매주 월,수,금 10:00' '매주 월요일 10:00' '평일 09:00' '주말 11:00' → ('weekly','월수금 10:00',None)"""
    t = " ".join(text.split())
    m = _WHEN_DAILY.match(t)
    if m:
        return "daily", parse_hhmm(m.group(1)), None
    m = _WHEN_WEEKLY.match(t)
    if m:
        days = {"평일": "월화수목금", "주말": "토일"}.get(m.group(2) or "") or "".join(
            d for d in DAYS if d in (m.group(1) or "").replace("요일", ""))
        return "weekly", f"{days} {parse_hhmm(m.group(3))}", None
    m = _WHEN_EVERY.match(t)
    if m:
        minutes = int(m.group(1)) * (60 if (m.group(2) or "").lower() in ("시간", "h") else 1)
        if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
            raise ValueError(f"반복 간격은 {MIN_INTERVAL}분 ~ {MAX_INTERVAL // 1440}일 사이로 해주세요")
        return "interval", None, minutes
    raise ValueError("형식: <code>매일 09:00</code> · <code>매주 월 10:00</code> · <code>평일 09:00</code> · "
                     "<code>반복 120</code> (분) / <code>반복 3시간</code>")


_WHEN_AFTER = re.compile(r"^(\d+)\s*(분|시간)\s*(?:뒤|후)(?:에)?$")
_WHEN_ONCE = re.compile(r"^(?:(오늘|내일|모레)|(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2}))\s*(\d{1,2}:\d{2})$")
ONCE_GRACE = 6 * 3600          # 한 번 예약을 놓쳤을 때(재시작 등) 이 안이면 늦게라도 실행


def parse_time(text: str, tz, now_ts: int | None = None) -> tuple[str, str | None, int | None, int | None]:
    """parse_when + 한 번: '30분 뒤' '2시간 후' '오늘 21:00' '내일 09:00' '09-28 09:00' '2026-09-28 09:00'
    → ('once', 'MM-DD HH:MM'(표시용), None, 실행 시각). 나머지는 parse_when (+ None)."""
    t = " ".join(text.split())
    now = datetime.fromtimestamp(now_ts or time.time(), tz)
    m = _WHEN_AFTER.match(t)
    if m:
        minutes = int(m.group(1)) * (60 if m.group(2) == "시간" else 1)
        if not 1 <= minutes <= 30 * 1440:
            raise ValueError("1분 ~ 30일 뒤까지 예약할 수 있어요")
        at = now + timedelta(minutes=minutes)
    elif m := _WHEN_ONCE.match(t):
        hh, mm = map(int, parse_hhmm(m.group(5)).split(":"))
        if m.group(1):
            at = (now + timedelta(days=("오늘", "내일", "모레").index(m.group(1)))).replace(hour=hh, minute=mm)
        else:
            try:
                at = now.replace(year=int(m.group(2) or now.year), month=int(m.group(3)), day=int(m.group(4)),
                                 hour=hh, minute=mm)
            except ValueError:
                raise ValueError("없는 날짜예요") from None
            if not m.group(2) and at <= now:          # 연도 없이 지난 날짜 = 내년
                at = at.replace(year=at.year + 1)
        at = at.replace(second=0, microsecond=0)
        if not now.timestamp() < at.timestamp() <= now.timestamp() + 366 * 86400:
            raise ValueError("지금보다 뒤의 시각(1년 안)으로 해주세요")
    else:
        return (*parse_when(t), None)
    return "once", at.strftime("%m-%d %H:%M"), None, int(at.timestamp())


def describe_when(kind: str, at_time: str | None, interval_min: int | None) -> str:
    if kind == "once":
        return f"{at_time} 한 번"
    if kind == "daily":
        return f"매일 {at_time}"
    if kind == "weekly":
        days, hhmm = (at_time or " ").split(" ", 1)
        label = {"월화수목금": "평일", "토일": "주말", DAYS: "매일"}.get(days) or "매주 " + "·".join(days)
        return f"{label} {hhmm}"
    if interval_min and interval_min % 60 == 0:
        return f"{interval_min // 60}시간마다"
    return f"{interval_min}분마다"


def is_due(row, now_ts: int, tz) -> bool:
    if not row["enabled"]:
        return False
    if row["kind"] == "once":
        return row["last_sent"] is None and 0 <= now_ts - (row["at_ts"] or 0) < ONCE_GRACE
    if row["kind"] in ("daily", "weekly"):
        days, _, hhmm = row["at_time"].rpartition(" ")          # weekly: '월수금 10:00' · daily: '10:00'
        hh, mm = map(int, hhmm.split(":"))
        now = datetime.fromtimestamp(now_ts, tz)
        if days and DAYS[now.weekday()] not in days:
            return False
        sched = int(now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
        if not 0 <= now_ts - sched < DAILY_GRACE:
            return False
        return row["last_sent"] is None or row["last_sent"] < sched
    return now_ts - (row["last_sent"] or 0) >= (row["interval_min"] or MAX_INTERVAL) * 60


def render(title: str, text: str, *, has_media: bool, rules: str, tz, fmt: str = "") -> str:
    """HTML 본문. 사용자 입력은 전부 escape 하고 자리표시자만 치환.
    길이는 escape '전' 글자를 줄여서 맞춘다 (escape 후에 자르면 &amp; 같은 기호가 반쯤 잘려 발송이 실패함).
    fmt='html': 제목·내용이 이미 텔레그램 HTML (움직이는 이모지·굵게 보관) → escape 안 함. 넘치면 서식 빼고 글자로."""
    rules_txt = rules or "(등록된 규칙이 없어요)"
    today = datetime.now(tz).strftime("%Y-%m-%d (%a)")
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT
    if fmt == "html":
        body = text.replace("{규칙}", esc(rules_txt)).replace("{날짜}", today)
        out = (f"📢 <b>{title}</b>\n\n{body}" if title else f"📢 {body}").strip()
        if len(html_plain(out)) <= limit:
            return out
        title, text = html_plain(title), html_plain(text)
    raw = text.replace("{규칙}", rules_txt)
    raw = raw.replace("{날짜}", today)

    def build(body_raw: str) -> str:
        body = esc(body_raw)
        return (f"📢 <b>{esc(title)}</b>\n\n{body}" if title else f"📢 {body}").strip()

    out = build(raw)
    while len(out) > limit and raw:
        raw = raw[: max(0, len(raw) - (len(out) - limit) - 1)]
        out = build(raw + "…")
    return out


# ── 만들기 마법사 상태 ────────────────────────────────────
@dataclass
class Draft:
    chat_id: int                  # 공지를 올릴 방
    user_id: int
    edit_id: int | None = None
    step: str = "title"
    title: str = ""
    text: str = ""
    media_type: str | None = None
    media_id: str | None = None
    fmt: str = ""                 # 'html' = 제목·내용이 텔레그램 HTML (움직이는 이모지·서식 보관)
    kind: str | None = None
    at_time: str | None = None
    interval_min: int | None = None
    pin: bool = False
    token: str = field(default_factory=lambda: secrets.token_hex(3))
    expires: float = field(default_factory=lambda: time.time() + WIZARD_TTL)
    cleanup: list[int] = field(default_factory=list)
    ui_chat_id: int | None = None  # 대화가 오가는 곳 (없으면 chat_id = 방 안에서 만드는 중)

    def __post_init__(self):
        if self.ui_chat_id is None:
            self.ui_chat_id = self.chat_id

    @property
    def key(self) -> tuple[int, int]:
        return self.ui_chat_id, self.user_id

    @property
    def in_dm(self) -> bool:
        return self.ui_chat_id == self.user_id


CLOSE_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 닫기", callback_data="an:x")]])


register_schema("""
CREATE TABLE IF NOT EXISTS announce_drafts (
    ui_chat_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,   -- 공지를 올릴 방 (그룹 전환 때 버림)
    data       TEXT NOT NULL,
    expires    REAL NOT NULL,
    PRIMARY KEY (ui_chat_id, user_id)
);
""", migrate={"announce_drafts": "drop"})


class Announcer:
    def __init__(self, svc: Services):
        self.svc = svc
        db = svc.db

        async def write(key, d: Draft) -> None:
            await db._write("INSERT OR REPLACE INTO announce_drafts(ui_chat_id, user_id, chat_id, data, expires) "
                            "VALUES(?,?,?,?,?)", (*key, d.chat_id, json.dumps(asdict(d), ensure_ascii=False), d.expires))

        async def drop(key) -> None:
            await db._write("DELETE FROM announce_drafts WHERE ui_chat_id=? AND user_id=?", key)
        # 만드는 중인 마법사도 DB 에 (배포 재시작 뒤 다음 말이 마법사로 이어지게). 넣기·빼기는 자동, 단계 진행은 save
        self.drafts: dict[tuple[int, int], Draft] = persist.MirrorDict(write, drop) if db is not None else {}

    async def restore(self) -> int:
        """봇 시작 때: 기한 안 지난 마법사를 메모리로."""
        rows = await self.svc.db._all("SELECT data FROM announce_drafts WHERE expires > ?", (time.time(),))
        n = 0
        for r in rows:
            try:
                d = Draft(**json.loads(r["data"]))
            except (ValueError, TypeError) as e:
                log.warning("announce draft unreadable: %r", e)
                continue
            dict.__setitem__(self.drafts, d.key, d)
            n += 1
        await self.svc.db._write("DELETE FROM announce_drafts WHERE expires <= ?", (time.time(),))
        return n

    def _saved(self, key) -> None:
        if isinstance(self.drafts, persist.MirrorDict):
            self.drafts.save(key)

    # ── 발송 ──────────────────────────────────────────────
    async def _rules(self, chat_id: int) -> str:
        return (await self.svc.db.get_settings(chat_id))["rules"]

    async def send(self, bot: Bot, chat_id: int, *, title: str, text: str, media_type: str | None,
                   media_id: str | None, reply_markup=None, rules_chat: int | None = None, fmt: str = "") -> Message:
        """chat_id 로 보낸다. {규칙} 은 rules_chat(기본 chat_id) 방의 규칙 (1:1 미리보기용).
        서식(fmt=html)이 거절되면(움직이는 이모지를 못 쓰는 봇 등) 글자만으로 한 번 더 — 공지가 안 올라가는 것보단 낫다."""
        rules = await self._rules(rules_chat if rules_chat is not None else chat_id)
        html = render(title, text, has_media=bool(media_id), rules=rules, tz=self.svc.cfg.tz, fmt=fmt)
        try:
            return await self._send_html(bot, chat_id, html, media_type, media_id, reply_markup)
        except BadRequest as e:
            if fmt != "html":
                raise
            log.warning("announce rich send refused, plain retry: %s", e)
            plain = render(html_plain(title), html_plain(text), has_media=bool(media_id), rules=rules, tz=self.svc.cfg.tz)
            return await self._send_html(bot, chat_id, plain, media_type, media_id, reply_markup)

    @staticmethod
    async def _send_html(bot: Bot, chat_id: int, html: str, media_type: str | None, media_id: str | None,
                         reply_markup) -> Message:
        kw = {"parse_mode": "HTML", "reply_markup": reply_markup}
        if media_type == "photo":
            return await bot.send_photo(chat_id, media_id, caption=html, **kw)
        if media_type == "video":
            return await bot.send_video(chat_id, media_id, caption=html, **kw)
        if media_type == "animation":
            return await bot.send_animation(chat_id, media_id, caption=html, **kw)
        if media_type == "document":
            return await bot.send_document(chat_id, media_id, caption=html, **kw)
        return await bot.send_message(chat_id, html, **kw)

    async def publish(self, bot: Bot, row) -> None:
        """예약 공지 1건 올리기: 지난 회차 삭제 → 발송 → (옵션) 고정 → 기록. 알람·AI 작업은 cron.fire."""
        now_ts = int(time.time())
        if row["kind"] == "once":
            await self.svc.db._write("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
        if row["action"] != "post":
            from . import cron   # 늦게 import (순환 방지)
            await self.svc.db.mark_schedule_sent(row["id"], now_ts, None)   # 먼저 기록: AI 가 느려도 다음 틱에 또 안 돌게
            await cron.fire(self.svc, bot, row)
            return
        if row["last_msg_id"]:
            try:
                await bot.delete_message(row["chat_id"], row["last_msg_id"])
            except TelegramError:
                pass
        msg_id = None
        try:
            sent = await self.send(bot, row["chat_id"], title=row["title"], text=row["text"],
                                   media_type=row["media_type"], media_id=row["media_id"], fmt=row["fmt"] or "")
            msg_id = sent.message_id
            await self.svc.db.bump(datetime.now(self.svc.cfg.tz).strftime("%Y-%m-%d"), row["chat_id"], "rep_announce")
            if row["pin"]:
                try:
                    await bot.pin_chat_message(row["chat_id"], msg_id, disable_notification=True)
                except TelegramError as e:
                    log.warning("announce pin failed: %s", e)
        except TelegramError as e:
            log.warning("announce #%s send failed: %s", row["id"], e)
            from . import opsdesk   # 📥 운영 인박스용 기록 (sodam/opsdesk.py, 늦게 import — 순환 방지)
            await opsdesk.schedule_failed(self.svc, row, "send", e.message)
        # 실패해도 기록: 안 그러면 다음 틱마다 계속 재시도하며 에러를 쏟아냄
        await self.svc.db.mark_schedule_sent(row["id"], now_ts, msg_id)

    async def run_due(self, bot: Bot) -> None:
        now_ts = int(time.time())
        for row in await self.svc.db.schedules():
            if row["kind"] == "once" and now_ts - (row["at_ts"] or 0) >= ONCE_GRACE:    # 너무 늦게 켜짐: 안 하고 끔
                await self.svc.db._write("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
                from . import opsdesk   # 📥 운영 인박스용 기록 (sodam/opsdesk.py)
                await opsdesk.schedule_failed(self.svc, row, "missed")
            elif is_due(row, now_ts, self.svc.cfg.tz) and await self.svc.paid_features(row["chat_id"]):
                await self.publish(bot, row)

    # ── 목록 ──────────────────────────────────────────────
    async def list_text(self, chat_id: int) -> str:
        rows = await self.svc.db.schedules(chat_id)
        if not rows:
            return ("등록된 예약공지가 없어요.\n<code>.예약공지 만들기</code> 로 제목·내용·사진/영상·시간을 "
                    "차례로 설정할 수 있어요.")
        lines = ["🗓️ <b>예약공지</b>"]
        for r in rows:
            flags = ("📌" if r["pin"] else "") + (f"[{MEDIA_LABEL[r['media_type']]}]" if r["media_type"] else "")
            state = "" if r["enabled"] else " (꺼짐)"
            title, body = (html_plain(r["title"]), html_plain(r["text"])) if r["fmt"] == "html" else (r["title"], r["text"])
            name = title or (body[:20] + ("…" if len(body) > 20 else "")) or "(내용 없음)"
            lines.append(f"<code>#{r['id']}</code> {describe_when(r['kind'], r['at_time'], r['interval_min'])} "
                         f"{flags} {esc(name)}{state}")
        lines.append("\n수정 <code>.예약공지 수정 ID</code> · 미리보기 <code>.예약공지 미리보기 ID</code> · "
                     "지금 올리기 <code>.예약공지 지금 ID</code> · <code>켜기/끄기/삭제 ID</code>")
        return "\n".join(lines)

    # ── 마법사 ────────────────────────────────────────────
    def _get(self, ui_chat_id: int, user_id: int) -> Draft | None:
        draft = self.drafts.get((ui_chat_id, user_id))
        if draft and draft.expires < time.time():
            del self.drafts[(ui_chat_id, user_id)]
            return None
        return draft

    def active(self, ui_chat_id: int, user_id: int) -> bool:
        return self._get(ui_chat_id, user_id) is not None

    async def _say(self, bot: Bot, draft: Draft, text: str, markup=None) -> None:
        sent = await bot.send_message(draft.ui_chat_id, text, parse_mode="HTML", reply_markup=markup)
        draft.cleanup.append(sent.message_id)

    async def start(self, bot: Bot, msg: Message, edit_row=None) -> None:
        """방 안에서 .예약공지 만들기/수정 (대화도 그 방에서)."""
        chat_id = msg.chat_id
        if edit_row is None and await self.svc.db.count_schedules(chat_id) >= MAX_PER_CHAT:
            await msg.reply_text(f"예약공지는 방당 {MAX_PER_CHAT}개까지예요. 안 쓰는 걸 지워주세요.")
            return
        await self._begin(bot, Draft(chat_id, msg.from_user.id), edit_row, trigger_msg_id=msg.message_id)

    async def start_dm(self, bot: Bot, user_id: int, chat_id: int, edit_row=None) -> str | None:
        """1:1 버튼 메뉴에서 시작: 대화는 관리자 1:1, 공지는 chat_id 방에. 못 하면 이유(문자열)."""
        if edit_row is None and await self.svc.db.count_schedules(chat_id) >= MAX_PER_CHAT:
            return f"예약공지는 방당 {MAX_PER_CHAT}개까지예요. 안 쓰는 걸 지워주세요."
        from .subscription import chat_title  # 순환 import 방지
        await self._begin(bot, Draft(chat_id, user_id, ui_chat_id=user_id), edit_row,
                          where=await chat_title(self.svc, chat_id))
        return None

    async def _begin(self, bot: Bot, draft: Draft, edit_row=None, *, trigger_msg_id: int | None = None,
                     where: str | None = None) -> None:
        if edit_row is not None:
            draft.edit_id = edit_row["id"]
            for k in ("title", "text", "media_type", "media_id", "kind", "at_time", "interval_min"):
                setattr(draft, k, edit_row[k])
            draft.pin = bool(edit_row["pin"])
            draft.fmt = edit_row["fmt"] or ""
        if trigger_msg_id is not None:
            draft.cleanup.append(trigger_msg_id)
        if draft.in_dm:  # 1:1 에선 입력 흐름을 하나만 (메뉴 글자 입력과 서로 취소)
            self.svc.inputs.pop(draft.user_id, None)
        old = self.drafts.pop(draft.key, None)
        if old and old.cleanup:  # 같은 곳에서 하던 마법사는 버리고 새로 시작
            await self._cleanup(bot, old)
        self.drafts[draft.key] = draft
        head = f"✏️ 예약공지 #{draft.edit_id} 수정" if draft.edit_id else "🗓️ 예약공지 만들기"
        room = f"\n💬 올릴 방: <b>{esc(where)}</b>" if where else ""
        now_title = draft.title if draft.fmt == "html" else esc(draft.title)
        keep = f"\n(지금: {now_title or '없음'} · 그대로 두려면 <code>그대로</code>)" if draft.edit_id else ""
        await self._say(bot, draft, f"{head} (언제든 <code>취소</code>){room}\n\n"
                                    f"<b>1/4 제목</b>을 보내주세요. 제목 없이 하려면 <code>없음</code>{keep}")
        self._saved(draft.key)

    async def handle_message(self, bot: Bot, msg: Message) -> bool:
        """마법사 진행 중인 관리자의 메시지면 처리하고 True."""
        if not msg.from_user or (msg.chat_id, msg.from_user.id) not in self.drafts:
            return False      # 대부분의 메시지: 메모리만 보고 끝 (DB 안 읽음)
        try:
            return await self._handle_message(bot, msg)
        finally:
            self._saved((msg.chat_id, msg.from_user.id))

    @staticmethod
    def _take(draft: Draft, text: str, rich: str | None, field_name: str) -> str:
        """입력 한 칸(제목/내용) 보관. 서식(움직이는 이모지 등)이 있으면 HTML 로 — 그때 이미 받은 다른 칸도 HTML 로 바꿔
        제목·내용이 같은 형식이 되게 (fmt 하나로)."""
        if rich is not None and draft.fmt != "html":
            other = "text" if field_name == "title" else "title"
            setattr(draft, other, esc(getattr(draft, other)))
            draft.fmt = "html"
        if draft.fmt == "html":
            if rich is not None and field_name == "title" and len(html_plain(rich)) > 100:
                return esc(text)         # 제목이 넘치면 서식 빼고 (태그 중간을 자르지 않게)
            return (rich or esc(text)).strip()
        return text

    async def _handle_message(self, bot: Bot, msg: Message) -> bool:
        draft = self._get(msg.chat_id, msg.from_user.id)
        if not draft or draft.step not in ("title", "body", "when"):
            return False
        text = (msg.text or msg.caption or "").strip()
        if text and text[0] in "./" and text not in CANCEL:
            return False  # 다른 명령어는 그대로 통과
        draft.cleanup.append(msg.message_id)
        draft.expires = time.time() + WIZARD_TTL
        if text in CANCEL:
            await self._finish(bot, draft, "예약공지 만들기를 취소했어요.")
            return True
        editing = draft.edit_id is not None

        if draft.step == "title":
            if not text:
                await self._say(bot, draft, "제목은 글자로 보내주세요. (없으면 <code>없음</code>)")
                return True
            if not (editing and text in KEEP):
                draft.title = "" if text in ("없음", "-") else self._take(draft, text[:100], rich_html(msg), "title")
            draft.step = "body"
            keep = " · 그대로 두려면 <code>그대로</code>" if editing else ""
            await self._say(bot, draft,
                            "<b>2/4 내용</b>을 보내주세요.\n"
                            "• 글만 보내도 되고, 사진·영상·GIF·파일에 설명을 붙여 보내도 돼요\n"
                            "• <code>{규칙}</code> 을 쓰면 방 규칙이, <code>{날짜}</code> 를 쓰면 오늘 날짜가 들어가요"
                            + keep)
            return True

        if draft.step == "body":
            if editing and text in KEEP:
                pass
            else:
                media_type, media_id = extract_media(msg)
                if not text and not media_id:
                    await self._say(bot, draft, "글이나 사진·영상·GIF·파일을 보내주세요.")
                    return True
                limit = CAPTION_LIMIT - 150 if media_id else TEXT_LIMIT - 150
                if len(text) > limit:   # 보이는 글자 수 (서식 태그는 안 셈)
                    await self._say(bot, draft, f"내용이 너무 길어요. {limit}자 이내로 줄여주세요"
                                                f"{' (사진·영상 설명은 텔레그램 제한이 1024자예요)' if media_id else ''}.")
                    return True
                draft.text = self._take(draft, text, rich_html(msg), "text")
                draft.media_type, draft.media_id = media_type, media_id
            draft.step = "when"
            now = (f"\n(지금: {describe_when(draft.kind, draft.at_time, draft.interval_min)} · "
                   f"<code>그대로</code>)") if editing else ""
            await self._say(bot, draft, "<b>3/4 언제</b> 올릴까요?\n"
                                        "• 매일 정해진 시각: <code>매일 09:00</code>\n"
                                        "• 일정 간격: <code>반복 120</code> (분) / <code>반복 3시간</code>" + now)
            return True

        # step == "when"
        if not (editing and text in KEEP):
            try:
                draft.kind, draft.at_time, draft.interval_min = parse_when(text)
            except ValueError as e:
                await self._say(bot, draft, f"❌ {e}")
                return True
        draft.step = "pin"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📌 고정해서 올리기", callback_data=f"an:{draft.token}:pin1"),
            InlineKeyboardButton("고정 안 함", callback_data=f"an:{draft.token}:pin0"),
        ]])
        await self._say(bot, draft, "<b>4/4 고정</b>할까요? (고정하면 지난 회차 공지는 지우고 새로 고정해요)", kb)
        return True

    async def on_callback(self, bot: Bot, query: CallbackQuery, parts: list[str]) -> None:
        token = (parts + [""])[0]
        draft = next((d for d in self.drafts.values() if d.token == token), None)
        try:
            await self._on_callback(bot, query, parts)
        finally:
            if draft is not None:
                self._saved(draft.key)

    async def _on_callback(self, bot: Bot, query: CallbackQuery, parts: list[str]) -> None:
        token, action = (parts + ["", ""])[:2]
        if token == "x":  # 1:1 미리보기의 [🗑 닫기]
            if not query.message or query.message.chat_id != query.from_user.id:
                await query.answer()  # 그룹 메시지에 위조 콜백을 붙여 봇 메시지를 지우는 것 방지
                return
            await query.answer()
            try:
                await bot.delete_message(query.message.chat_id, query.message.message_id)
            except TelegramError:
                pass
            return
        draft = next((d for d in self.drafts.values() if d.token == token), None)
        if not draft or draft.expires < time.time():
            await query.answer("만료됐어요. 다시 만들어주세요.")
            return
        if query.from_user.id != draft.user_id:
            await query.answer("만들고 있는 관리자만 누를 수 있어요.", show_alert=True)
            return
        draft.expires = time.time() + WIZARD_TTL
        if action in ("pin1", "pin0") and draft.step == "pin":
            draft.pin = action == "pin1"
            draft.step = "confirm"
            await query.answer()
            await self._say(bot, draft, "👀 <b>미리보기</b> — "
                                        f"{describe_when(draft.kind, draft.at_time, draft.interval_min)}"
                                        f"{' · 📌 고정' if draft.pin else ''}")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 저장", callback_data=f"an:{draft.token}:save"),
                InlineKeyboardButton("❌ 취소", callback_data=f"an:{draft.token}:cancel"),
            ]])
            try:
                preview = await self.send(bot, draft.ui_chat_id, title=draft.title, text=draft.text,
                                          media_type=draft.media_type, media_id=draft.media_id, reply_markup=kb,
                                          rules_chat=draft.chat_id, fmt=draft.fmt)
                draft.cleanup.append(preview.message_id)
            except TelegramError as e:
                again = "메뉴에서 다시 눌러주세요" if draft.in_dm else "다시 <code>.예약공지 만들기</code> 해주세요"
                await self._say(bot, draft, f"미리보기 실패: {esc(e.message)}\n{again}.")
                self.drafts.pop(draft.key, None)
            return
        if action == "cancel":
            await query.answer()
            await self._finish(bot, draft, "예약공지 만들기를 취소했어요.")
            return
        if action == "save" and draft.step == "confirm":
            draft.step = "saving"  # 두 번 눌러도 한 번만 (await 전에 바꿔야 동시에 온 두 번째가 막힘)
            await query.answer("저장했어요!")
            await self._save(bot, draft)
            return
        await query.answer()

    async def _save(self, bot: Bot, draft: Draft) -> None:
        db, perms = self.svc.db, self.svc.perms
        # 만드는 사이 관리자에서 내려왔을 수도 있으니 저장 직전에 캐시 말고 지금 상태로 확인
        try:
            perms.forget(draft.chat_id)
            allowed = await perms.is_admin(bot, draft.chat_id, draft.user_id)
        except TelegramError:
            allowed = False  # 봇이 나간 방 등
        if not allowed:
            await self._finish(bot, draft, "그 그룹의 관리자만 예약공지를 저장할 수 있어요. 저장하지 않았어요.")
            return
        fields = dict(kind=draft.kind, at_time=draft.at_time, interval_min=draft.interval_min,
                      title=draft.title, text=draft.text, media_type=draft.media_type,
                      media_id=draft.media_id, pin=int(draft.pin), fmt=draft.fmt)
        if draft.edit_id:
            if not await db.update_schedule(draft.chat_id, draft.edit_id, **fields):
                await self._finish(bot, draft, f"예약공지 #{draft.edit_id} 는 그사이 삭제됐어요. 저장하지 않았어요.")
                return
            sid = draft.edit_id
        else:
            if await db.count_schedules(draft.chat_id) >= MAX_PER_CHAT:
                await self._finish(bot, draft, f"예약공지는 방당 {MAX_PER_CHAT}개까지예요. 저장하지 않았어요.")
                return
            sid = await db.add_schedule(draft.chat_id, created_by=draft.user_id, **fields)
        await db.log_mod(draft.chat_id, draft.user_id, None, "schedule",
                         f"#{sid} {html_plain(draft.title) if draft.fmt == 'html' else draft.title}")
        when = describe_when(draft.kind, draft.at_time, draft.interval_min)
        await self._finish(bot, draft, f"✅ 예약공지 <code>#{sid}</code> {'수정' if draft.edit_id else '저장'}: "
                                       f"{when}{' · 📌' if draft.pin else ''} "
                                       f"{draft.title if draft.fmt == 'html' else esc(draft.title)}")

    async def _cleanup(self, bot: Bot, draft: Draft) -> None:
        if draft.cleanup:
            try:
                await bot.delete_messages(draft.ui_chat_id, draft.cleanup[:100])
            except TelegramError:
                pass

    async def _finish(self, bot: Bot, draft: Draft, text: str) -> None:
        if self.drafts.get(draft.key) is draft:
            del self.drafts[draft.key]
        await self._cleanup(bot, draft)
        # 1:1 에서 만들었으면 예약공지 목록(버튼 메뉴)으로 돌아가는 버튼
        kb = (InlineKeyboardMarkup([[InlineKeyboardButton("🗓️ 예약공지 목록", callback_data=f"m:sc:{draft.chat_id}")]])
              if draft.in_dm and draft.chat_id != draft.user_id else None)
        await bot.send_message(draft.ui_chat_id, text, parse_mode="HTML", reply_markup=kb)
