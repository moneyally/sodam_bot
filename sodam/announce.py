"""예약·반복 공지.

관리자가 방 안에서 단계별로 만든다 (.예약공지 만들기):
  1) 제목 → 2) 내용 (글 또는 사진·영상·GIF·파일 + 설명) → 3) 시간 (매일 09:00 / 반복 120)
  → 4) 고정 여부 버튼 → 미리보기 → 저장
만드는 동안 오간 메시지는 저장/취소 시 지워서 방을 깨끗하게 둔다.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import TelegramError

from .settings import parse_hhmm
from .util import esc

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
_WHEN_EVERY = re.compile(r"^(?:반복|매|every)\s*(\d+)\s*(분|m|min|시간|h)?(?:\s*마다)?$", re.I)


def parse_when(text: str) -> tuple[str, str | None, int | None]:
    """'매일 09:00' → ('daily','09:00',None) / '반복 120' '반복 2시간' → ('interval',None,120)"""
    t = text.strip()
    m = _WHEN_DAILY.match(t)
    if m:
        return "daily", parse_hhmm(m.group(1)), None
    m = _WHEN_EVERY.match(t)
    if m:
        minutes = int(m.group(1)) * (60 if (m.group(2) or "").lower() in ("시간", "h") else 1)
        if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
            raise ValueError(f"반복 간격은 {MIN_INTERVAL}분 ~ {MAX_INTERVAL // 1440}일 사이로 해주세요")
        return "interval", None, minutes
    raise ValueError("형식: <code>매일 09:00</code> 또는 <code>반복 120</code> (분) / <code>반복 3시간</code>")


def describe_when(kind: str, at_time: str | None, interval_min: int | None) -> str:
    if kind == "daily":
        return f"매일 {at_time}"
    if interval_min and interval_min % 60 == 0:
        return f"{interval_min // 60}시간마다"
    return f"{interval_min}분마다"


def is_due(row, now_ts: int, tz) -> bool:
    if not row["enabled"]:
        return False
    if row["kind"] == "daily":
        hh, mm = map(int, row["at_time"].split(":"))
        now = datetime.fromtimestamp(now_ts, tz)
        sched = int(now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
        if not 0 <= now_ts - sched < DAILY_GRACE:
            return False
        return row["last_sent"] is None or row["last_sent"] < sched
    return now_ts - (row["last_sent"] or 0) >= (row["interval_min"] or MAX_INTERVAL) * 60


def render(title: str, text: str, *, has_media: bool, rules: str, tz) -> str:
    """HTML 본문. 사용자 입력은 전부 escape 하고 자리표시자만 치환.
    길이는 escape '전' 글자를 줄여서 맞춘다 (escape 후에 자르면 &amp; 같은 기호가 반쯤 잘려 발송이 실패함)."""
    raw = text.replace("{규칙}", rules or "(등록된 규칙이 없어요)")
    raw = raw.replace("{날짜}", datetime.now(tz).strftime("%Y-%m-%d (%a)"))
    limit = CAPTION_LIMIT if has_media else TEXT_LIMIT

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
    chat_id: int
    user_id: int
    edit_id: int | None = None
    step: str = "title"
    title: str = ""
    text: str = ""
    media_type: str | None = None
    media_id: str | None = None
    kind: str | None = None
    at_time: str | None = None
    interval_min: int | None = None
    pin: bool = False
    token: str = field(default_factory=lambda: secrets.token_hex(3))
    expires: float = field(default_factory=lambda: time.time() + WIZARD_TTL)
    cleanup: list[int] = field(default_factory=list)


class Announcer:
    def __init__(self, svc: Services):
        self.svc = svc
        self.drafts: dict[tuple[int, int], Draft] = {}

    # ── 발송 ──────────────────────────────────────────────
    async def _rules(self, chat_id: int) -> str:
        return (await self.svc.db.get_settings(chat_id))["rules"]

    async def send(self, bot: Bot, chat_id: int, *, title: str, text: str, media_type: str | None,
                   media_id: str | None, reply_markup=None) -> Message:
        html = render(title, text, has_media=bool(media_id), rules=await self._rules(chat_id), tz=self.svc.cfg.tz)
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
        """예약 공지 1건 올리기: 지난 회차 삭제 → 발송 → (옵션) 고정 → 기록."""
        now_ts = int(time.time())
        if row["last_msg_id"]:
            try:
                await bot.delete_message(row["chat_id"], row["last_msg_id"])
            except TelegramError:
                pass
        msg_id = None
        try:
            sent = await self.send(bot, row["chat_id"], title=row["title"], text=row["text"],
                                   media_type=row["media_type"], media_id=row["media_id"])
            msg_id = sent.message_id
            if row["pin"]:
                try:
                    await bot.pin_chat_message(row["chat_id"], msg_id, disable_notification=True)
                except TelegramError as e:
                    log.warning("announce pin failed: %s", e)
        except TelegramError as e:
            log.warning("announce #%s send failed: %s", row["id"], e)
        # 실패해도 기록: 안 그러면 다음 틱마다 계속 재시도하며 에러를 쏟아냄
        await self.svc.db.mark_schedule_sent(row["id"], now_ts, msg_id)

    async def run_due(self, bot: Bot) -> None:
        now_ts = int(time.time())
        for row in await self.svc.db.schedules():
            if is_due(row, now_ts, self.svc.cfg.tz) and await self.svc.paid_features(row["chat_id"]):
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
            name = r["title"] or (r["text"][:20] + ("…" if len(r["text"]) > 20 else "")) or "(내용 없음)"
            lines.append(f"<code>#{r['id']}</code> {describe_when(r['kind'], r['at_time'], r['interval_min'])} "
                         f"{flags} {esc(name)}{state}")
        lines.append("\n수정 <code>.예약공지 수정 ID</code> · 미리보기 <code>.예약공지 미리보기 ID</code> · "
                     "지금 올리기 <code>.예약공지 지금 ID</code> · <code>켜기/끄기/삭제 ID</code>")
        return "\n".join(lines)

    # ── 마법사 ────────────────────────────────────────────
    def _get(self, chat_id: int, user_id: int) -> Draft | None:
        draft = self.drafts.get((chat_id, user_id))
        if draft and draft.expires < time.time():
            del self.drafts[(chat_id, user_id)]
            return None
        return draft

    def active(self, chat_id: int, user_id: int) -> bool:
        return self._get(chat_id, user_id) is not None

    async def _say(self, bot: Bot, draft: Draft, text: str, markup=None) -> None:
        sent = await bot.send_message(draft.chat_id, text, parse_mode="HTML", reply_markup=markup)
        draft.cleanup.append(sent.message_id)

    async def start(self, bot: Bot, msg: Message, edit_row=None) -> None:
        chat_id, user_id = msg.chat_id, msg.from_user.id
        if edit_row is None and await self.svc.db.count_schedules(chat_id) >= MAX_PER_CHAT:
            await msg.reply_text(f"예약공지는 방당 {MAX_PER_CHAT}개까지예요. 안 쓰는 걸 지워주세요.")
            return
        draft = Draft(chat_id, user_id)
        if edit_row is not None:
            draft.edit_id = edit_row["id"]
            for k in ("title", "text", "media_type", "media_id", "kind", "at_time", "interval_min"):
                setattr(draft, k, edit_row[k])
            draft.pin = bool(edit_row["pin"])
        draft.cleanup.append(msg.message_id)
        if chat_id == user_id:  # 1:1 에선 입력 흐름을 하나만 (메뉴 글자 입력과 서로 취소)
            self.svc.inputs.pop(user_id, None)
        self.drafts[(chat_id, user_id)] = draft
        head = f"✏️ 예약공지 #{draft.edit_id} 수정" if draft.edit_id else "🗓️ 예약공지 만들기"
        keep = f"\n(지금: {esc(draft.title) or '없음'} · 그대로 두려면 <code>그대로</code>)" if draft.edit_id else ""
        await self._say(bot, draft, f"{head} (언제든 <code>취소</code>)\n\n"
                                    f"<b>1/4 제목</b>을 보내주세요. 제목 없이 하려면 <code>없음</code>{keep}")

    async def handle_message(self, bot: Bot, msg: Message) -> bool:
        """마법사 진행 중인 관리자의 메시지면 처리하고 True."""
        if not msg.from_user:
            return False
        draft = self._get(msg.chat_id, msg.from_user.id)
        if not draft or draft.step not in ("title", "body", "when"):
            return False
        text = (msg.text or msg.caption or "").strip()
        if text[:1] in "./" and text not in CANCEL:
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
                draft.title = "" if text in ("없음", "-") else text[:100]
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
                if len(text) > limit:
                    await self._say(bot, draft, f"내용이 너무 길어요. {limit}자 이내로 줄여주세요"
                                                f"{' (사진·영상 설명은 텔레그램 제한이 1024자예요)' if media_id else ''}.")
                    return True
                draft.text, draft.media_type, draft.media_id = text, media_type, media_id
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
        token, action = (parts + ["", ""])[:2]
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
                preview = await self.send(bot, draft.chat_id, title=draft.title, text=draft.text,
                                          media_type=draft.media_type, media_id=draft.media_id, reply_markup=kb)
                draft.cleanup.append(preview.message_id)
            except TelegramError as e:
                await self._say(bot, draft, f"미리보기 실패: {esc(e.message)}\n다시 <code>.예약공지 만들기</code> 해주세요.")
                self.drafts.pop((draft.chat_id, draft.user_id), None)
            return
        if action == "cancel":
            await query.answer()
            await self._finish(bot, draft, "예약공지 만들기를 취소했어요.")
            return
        if action == "save" and draft.step == "confirm":
            await query.answer("저장했어요!")
            await self._save(bot, draft)
            return
        await query.answer()

    async def _save(self, bot: Bot, draft: Draft) -> None:
        db = self.svc.db
        fields = dict(kind=draft.kind, at_time=draft.at_time, interval_min=draft.interval_min,
                      title=draft.title, text=draft.text, media_type=draft.media_type,
                      media_id=draft.media_id, pin=int(draft.pin))
        if draft.edit_id:
            await db.update_schedule(draft.chat_id, draft.edit_id, **fields)
            sid = draft.edit_id
        else:
            sid = await db.add_schedule(draft.chat_id, created_by=draft.user_id, **fields)
        await db.log_mod(draft.chat_id, draft.user_id, None, "schedule", f"#{sid} {draft.title}")
        when = describe_when(draft.kind, draft.at_time, draft.interval_min)
        await self._finish(bot, draft, f"✅ 예약공지 <code>#{sid}</code> {'수정' if draft.edit_id else '저장'}: "
                                       f"{when}{' · 📌' if draft.pin else ''} {esc(draft.title)}")

    async def _finish(self, bot: Bot, draft: Draft, text: str) -> None:
        self.drafts.pop((draft.chat_id, draft.user_id), None)
        if draft.cleanup:
            try:
                await bot.delete_messages(draft.chat_id, draft.cleanup[:100])
            except TelegramError:
                pass
        await bot.send_message(draft.chat_id, text, parse_mode="HTML")
