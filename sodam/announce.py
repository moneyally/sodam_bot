"""예약·반복 공지.

관리자가 단계별로 만든다 — 방 안에서(.예약공지 만들기) 또는 1:1 버튼 메뉴(🗓️ 예약공지 → ➕/✏️)에서:
  1) 제목 → 2) 내용 → 3) 사진·영상 → 4) 시간 (매일 09:00 / 매주 월 / 매월 15일 / 반복 120)
  → 5) 고정 → 6) 지난 회차 지우기/쌓기 → 7) 기간·시간대 제한(선택) → 미리보기 → 저장
Draft.ui_chat_id = 대화가 오가는 곳(방 또는 관리자 1:1), Draft.chat_id = 공지를 올릴 방.
drafts 키는 (ui_chat_id, user_id) → 1:1 에선 (uid, uid) 라서 1:1 메시지로 찾을 수 있다.
만드는 동안 오간 메시지는 저장/취소 시 지워서 채팅을 깨끗하게 둔다. 저장할 때 그 방 관리자인지 다시 확인한다.
"""
from __future__ import annotations

import calendar
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

from . import mediastore, persist
from .db import register_schema
from .settings import parse_hhmm
from .util import esc, html_plain, rich_html

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

MAX_PER_CHAT = 120            # 방당 예약공지 수 (2026-10-27 오너 '100개 넘게')
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
_WHEN_MONTHLY = re.compile(r"^(?:매월|매달|monthly)\s*(\d{1,2})\s*일?\s*(\d{1,2}:\d{2})$", re.I)
_WHEN_EVERY = re.compile(r"^(?:반복|매|every)\s*(\d+)\s*(분|m|min|시간|h)?(?:\s*마다)?$", re.I)
# 시작 시각 맞춰 반복: '00:15 반복 4시간' · '매일 00:15부터 4시간마다' · '00:15 반복 240' (다른 봇처럼 정렬 발송)
_WHEN_ANCHORED = re.compile(r"^(?:매일\s*)?(\d{1,2}:\d{2})\s*(?:부터)?\s*(?:반복\s*)?(\d+)\s*(분|m|시간|h)?(?:\s*마다)?$", re.I)


def _col(row, name: str, default):
    """예전 DB·테스트 행에 없는 컬럼을 안전하게 (마이그레이션 전/가짜 행 대비)."""
    try:
        v = row[name]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


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
    m = _WHEN_MONTHLY.match(t)                        # '매월 15일 09:00' · '매달 1일 10:00' = 매달 그 날짜
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            raise ValueError("날짜는 1일~31일 사이로 해주세요 (없는 날은 그 달 말일에 올라가요)")
        return "monthly", f"{day} {parse_hhmm(m.group(2))}", None
    m = _WHEN_ANCHORED.match(t)                       # '00:15 반복 4시간' · '매일 00:15부터 4시간마다' = 그 시각 맞춰 반복
    if m:
        minutes = int(m.group(2)) * (60 if (m.group(3) or "").lower() in ("시간", "h") else 1)
        if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
            raise ValueError(f"반복 간격은 {MIN_INTERVAL}분 ~ {MAX_INTERVAL // 1440}일 사이로 해주세요")
        return "interval", parse_hhmm(m.group(1)), minutes
    m = _WHEN_EVERY.match(t)
    if m:
        minutes = int(m.group(1)) * (60 if (m.group(2) or "").lower() in ("시간", "h") else 1)
        if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
            raise ValueError(f"반복 간격은 {MIN_INTERVAL}분 ~ {MAX_INTERVAL // 1440}일 사이로 해주세요")
        return "interval", None, minutes
    raise ValueError("한 줄에 <b>하나만</b> 보내세요 (슬래시·'또는' 같이 쓰지 말고):\n"
                     "• 매일 그 시각 한 번: <code>매일 09:00</code>\n"
                     "• 요일: <code>매주 월 10:00</code> · <code>평일 09:00</code>\n"
                     "• 매월 정해진 날: <code>매월 15일 09:00</code>\n"
                     "• 그냥 N 간격: <code>반복 120</code>(분) · <code>반복 3시간</code>\n"
                     "• <b>그 시각 맞춰 반복</b>: <code>00:15 반복 4시간</code> (0시15분·4시15분·8시15분…)")


_WHEN_AFTER = re.compile(r"^(\d+)\s*(분|시간)\s*(?:뒤|후)(?:에)?$")
_WHEN_ONCE = re.compile(r"^(?:(오늘|내일|모레)|(?:(\d{4})[-./])?(\d{1,2})[-./](\d{1,2}))\s*(\d{1,2}:\d{2})$")
# 날짜 없이 시각만 ('11시55분', '23:55', '오후 3시 반', '내일 아침 8시') → 한 번, 지금 이후 가장 가까운 그 시각.
# 오전/오후를 안 말한 12시간제(1~12시)면 h 와 h+12 중 지금 이후 가장 가까운 쪽 (실제 '11시55분에 나한테 보내줘').
_WHEN_AT = re.compile(r"^(?:(오늘|내일|모레)\s*)?(오전|오후|아침|새벽|저녁|밤|낮)?\s*(\d{1,2})\s*(?::|시)\s*(?:(\d{1,2})\s*분?|(반))?\s*(?:에)?$")
_PM = ("오후", "저녁", "밤", "낮")
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
    elif m := _WHEN_AT.match(t):
        day, ampm, hh = m.group(1), m.group(2), int(m.group(3))
        mm = 30 if m.group(5) else int(m.group(4) or 0)
        if hh > 23 or mm > 59:
            raise ValueError("없는 시각이에요")
        if ampm in _PM and hh < 12:
            hh += 12
        elif ampm in ("오전", "새벽", "아침") and hh == 12:
            hh = 0
        hours = [hh] if ampm or hh == 0 or hh > 12 else [hh % 12, hh % 12 + 12]   # 오전/오후 없으면 둘 다 후보
        days = [("오늘", "내일", "모레").index(day)] if day else [0, 1]
        cands = [(now + timedelta(days=d)).replace(hour=h, minute=mm, second=0, microsecond=0) for d in days for h in hours]
        later = sorted(c for c in cands if c > now)
        if not later:
            raise ValueError("지금보다 뒤의 시각으로 해주세요")
        at = later[0]
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
    if kind == "monthly":
        day, hhmm = (at_time or " ").split(" ", 1)
        return f"매월 {day}일 {hhmm}"
    every = f"{interval_min // 60}시간마다" if interval_min and interval_min % 60 == 0 else f"{interval_min}분마다"
    return f"매일 {at_time}부터 {every}" if at_time else every


def describe_limits(start_ts, end_ts, active_from, active_to, tz) -> str:
    """시작/종료 날짜·시간대 제한을 사람이 읽는 한 줄로 (없으면 '')."""
    parts = []
    if start_ts or end_ts:
        a = datetime.fromtimestamp(start_ts, tz).strftime("%m-%d") if start_ts else "지금"
        b = datetime.fromtimestamp(end_ts, tz).strftime("%m-%d") if end_ts else "계속"
        parts.append(f"📅 {a}~{b}")
    if active_from is not None and active_to is not None:
        parts.append(f"⏰ {active_from}~{active_to}시만")
    return " · ".join(parts)


_LIM_DATE = re.compile(r"(\d{1,2})[-/.](\d{1,2})\s*[~\-]\s*(\d{1,2})[-/.](\d{1,2})")   # 10-01~10-31
_LIM_ONE = re.compile(r"(시작|종료|부터|까지)\s*(\d{1,2})[-/.](\d{1,2})")                # 시작 10-01 / 종료 10-31
_LIM_HOUR = re.compile(r"(\d{1,2})\s*시\s*[-~]\s*(\d{1,2})\s*시?|(\d{1,2})\s*~\s*(\d{1,2})")  # 9시~18시 / 9~18 ('-'는 날짜라 시엔 ~만)


def parse_limits(text: str, tz, now_ts: int | None = None):
    """'10-01~10-31' 기간 · '9시~18시' 시간대 (줄·쉼표로 여럿) → (start_ts, end_ts, active_from, active_to).
    날짜 범위와 시간대 둘 다 넣을 수 있다. 하나도 못 읽으면 ValueError."""
    now = datetime.fromtimestamp(now_ts or time.time(), tz)
    start_ts = end_ts = active_from = active_to = None
    found = False

    def md_ts(mon: int, day: int, *, end: bool):
        if not (1 <= mon <= 12 and 1 <= day <= 31):
            raise ValueError("없는 날짜예요 (월·일을 확인해 주세요)")
        year = now.year if (mon, day) >= (now.month, now.day) else now.year + 1   # 지난 날짜 = 내년
        day = min(day, calendar.monthrange(year, mon)[1])
        t = now.replace(year=year, month=mon, day=day, hour=23 if end else 0,
                        minute=59 if end else 0, second=59 if end else 0, microsecond=0)
        return int(t.timestamp())

    for part in re.split(r"[\n,]+", text):
        part = part.strip()
        if not part or part in KEEP:
            continue
        if m := _LIM_DATE.search(part):
            start_ts = md_ts(int(m.group(1)), int(m.group(2)), end=False)
            end_ts = md_ts(int(m.group(3)), int(m.group(4)), end=True)
            found = True
        elif m := _LIM_ONE.search(part):
            ts = md_ts(int(m.group(2)), int(m.group(3)), end=m.group(1) in ("종료", "까지"))
            if m.group(1) in ("종료", "까지"):
                end_ts = ts
            else:
                start_ts = ts
            found = True
        elif m := _LIM_HOUR.search(part):
            a, b = int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))
            if not (0 <= a <= 23 and 0 <= b <= 24) or a == b:
                raise ValueError("시간대는 0~23시 사이·시작≠끝 으로 해주세요 (예: 9시~18시)")
            active_from, active_to = a, b % 24
            found = True
        else:
            raise ValueError("기간은 <code>10-01~10-31</code>, 시간대는 <code>9시~18시</code> 모양으로 보내주세요.")
    if not found:
        raise ValueError("기간·시간대를 못 읽었어요. <code>10-01~10-31</code> 또는 <code>9시~18시</code>")
    if start_ts and end_ts and end_ts <= start_ts:
        raise ValueError("종료일이 시작일보다 빨라요.")
    return start_ts, end_ts, active_from, active_to


def _within_limits(row, now_ts: int, tz) -> bool:
    """유효 기간(start_ts~end_ts)·하루 시간대(active_from~active_to) 안인지. 없으면 늘 True."""
    s, e = _col(row, "start_ts", None), _col(row, "end_ts", None)
    if (s and now_ts < s) or (e and now_ts > e):
        return False
    af, at = _col(row, "active_from", None), _col(row, "active_to", None)
    if af is not None and at is not None:
        hr = datetime.fromtimestamp(now_ts, tz).hour
        inside = af <= hr < at if af < at else (hr >= af or hr < at)   # 자정 넘김 (22~6) 지원
        if not inside:
            return False
    return True


def is_due(row, now_ts: int, tz) -> bool:
    if not row["enabled"]:
        return False
    if row["kind"] == "once":
        return row["last_sent"] is None and 0 <= now_ts - (row["at_ts"] or 0) < ONCE_GRACE
    if not _within_limits(row, now_ts, tz):                   # 기간·시간대 밖 (once 는 at_ts 로 이미 정해져 제외)
        return False
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
    if row["kind"] == "monthly":
        day_s, _, hhmm = row["at_time"].rpartition(" ")         # '15 09:00'
        target, (hh, mm) = int(day_s), map(int, hhmm.split(":"))
        now = datetime.fromtimestamp(now_ts, tz)
        last_day = calendar.monthrange(now.year, now.month)[1]
        if now.day != min(target, last_day):                    # 31일 지정 + 30일 달 = 말일에
            return False
        sched = int(now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
        if not 0 <= now_ts - sched < DAILY_GRACE:
            return False
        return row["last_sent"] is None or row["last_sent"] < sched
    interval_min = row["interval_min"] or MAX_INTERVAL
    anchor = row["at_time"]                                    # interval + 시작 시각 = 그 시각 맞춰 정렬 발송 (00:15 → 00:15·04:15·…)
    if anchor:
        hh, mm = map(int, anchor.split(":"))
        now = datetime.fromtimestamp(now_ts, tz)
        base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if base.timestamp() > now_ts:                         # 오늘 그 시각 아직 안 지남 → 어제 시각부터 세어 칸 맞춤
            base -= timedelta(days=1)
        step = interval_min * 60
        slot = int(base.timestamp()) + ((now_ts - int(base.timestamp())) // step) * step
        if not 0 <= now_ts - slot < DAILY_GRACE:
            return False
        return row["last_sent"] is None or row["last_sent"] < slot
    return now_ts - (row["last_sent"] or 0) >= interval_min * 60


_EMOJI_TAG = re.compile(r'<tg-emoji emoji-id="([^"]+)">(.*?)</tg-emoji>', re.S)


async def _emoji_sendable(bot: Bot, chat_id: int, html: str) -> bool:
    """html 한 덩이를 조용히 보냈다 지워서 '봇이 보낼 수 있는지' 확인 (못 보내는 커스텀 이모지 탐지용)."""
    try:
        m = await bot.send_message(chat_id, html, parse_mode="HTML", disable_notification=True)
    except BadRequest:
        return False                       # Document_invalid 등 = 못 보냄
    except TelegramError:
        return True                        # 네트워크·1:1 막힘 등 = 판정 불가 → 통과로 봄
    try:
        await bot.delete_message(chat_id, m.message_id)
    except TelegramError:
        pass
    return True


async def unsendable_emojis(bot: Bot, test_chat: int | None, text_html: str) -> list[tuple[str, int]]:
    """공지 글에서 '봇이 못 올리는' 커스텀 이모지를 (보이는 글자, 줄번호) 목록으로. 없으면 [].
    getCustomEmojiStickers 로는 못 거른다(삭제·특수 이모지도 조회는 '살아있음' 으로 나옴, 2026-10-27 실측:
    죽은 💎 가 조회엔 있는데 send 는 Document_invalid) → 관리자 1:1 로 실제 보냈다 지워서 확인."""
    pairs = _EMOJI_TAG.findall(text_html or "")
    if not pairs or not test_chat:
        return []
    uniq: dict[str, str] = {}
    for eid, ch in pairs:
        uniq.setdefault(eid, ch)
    whole = "".join(f'<tg-emoji emoji-id="{i}">{c}</tg-emoji>' for i, c in uniq.items())
    if await _emoji_sendable(bot, test_chat, whole):
        return []                          # 통째로 OK → 문제 없음 (대부분 여기서 끝, 호출 1번)
    bad = {i for i, c in uniq.items()
           if not await _emoji_sendable(bot, test_chat, f'<tg-emoji emoji-id="{i}">{c}</tg-emoji>')}
    if not bad:
        return []                          # 개별론 다 되는데 통째 안 됨(개수 등) → 여기선 못 집음
    lines = html_plain(text_html).split("\n")
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for eid in bad:
        ch = uniq[eid]
        if ch in seen:
            continue
        seen.add(ch)
        ln = next((n for n, line in enumerate(lines, 1) if ch and ch in line), 0)
        out.append((ch, ln))
    out.sort(key=lambda x: x[1])
    return out


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
    del_prev: bool = True         # True = 새 회차 올릴 때 지난 회차 지움(기본) / False = 그대로 쌓기
    start_ts: int | None = None   # 유효 기간 시작/끝 (None=무제한)
    end_ts: int | None = None
    active_from: int | None = None  # 하루 중 올릴 시간대 [from,to) 한국 시 (None=종일)
    active_to: int | None = None
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
        except mediastore.MediaLost:
            raise
        except BadRequest as e:
            if fmt != "html":
                raise
            log.warning("announce rich send refused, plain retry: %s", e)
            plain = render(html_plain(title), html_plain(text), has_media=bool(media_id), rules=rules, tz=self.svc.cfg.tz)
            return await self._send_html(bot, chat_id, plain, media_type, media_id, reply_markup)

    async def _send_html(self, bot: Bot, chat_id: int, html: str, media_type: str | None, media_id: str | None,
                         reply_markup) -> Message:
        """미디어는 mediastore 로 (봇이 바뀌어 file_id 가 안 먹으면 보관 원본으로 다시 올림)."""
        kw = {"parse_mode": "HTML", "reply_markup": reply_markup}
        if media_type in mediastore.KINDS and media_id:
            return await mediastore.send(bot, self.svc.db, media_type, chat_id, media_id, caption=html, **kw)
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
        if row["last_msg_id"] and _col(row, "del_prev", 1):   # del_prev=0 이면 지난 회차 글을 안 지우고 쌓음 (泰山 루디)
            try:
                await bot.delete_message(row["chat_id"], row["last_msg_id"])
            except TelegramError:
                pass
        msg_id = None
        try:
            try:
                sent = await self.send(bot, row["chat_id"], title=row["title"], text=row["text"],
                                       media_type=row["media_type"], media_id=row["media_id"], fmt=row["fmt"] or "")
            except mediastore.MediaLost:
                # 봇이 바뀌어 미디어가 안 먹고 원본도 없음 → 공지는 글만이라도 올리고 만든 관리자에게 하루 한 번 알림
                sent = await self.send(bot, row["chat_id"], title=row["title"], text=row["text"],
                                       media_type=None, media_id=None, fmt=row["fmt"] or "")
                await self._media_lost(bot, row)
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

    async def _media_lost(self, bot: Bot, row) -> None:
        from . import opsdesk   # 늦게 import (순환 방지)
        await opsdesk.schedule_failed(self.svc, row, "media")
        what = {"photo": "사진", "video": "영상", "animation": "GIF"}.get(row["media_type"], "파일")
        name = html_plain(row["title"] or "") or html_plain(row["text"] or "")[:20] or "(제목 없음)"
        await mediastore.notify_lost(
            bot, self.svc.db, f"s{row['id']}", row["created_by"],
            f"🗓️ 예약공지 <b>#{row['id']}</b> {esc(name)} — {what}이 사라져서 <b>글만</b> 올렸어요.\n"
            f"소담 1:1 → 🗓️ 예약공지에서 그 공지를 눌러 {what}을 다시 넣어 주세요.")

    async def run_due(self, bot: Bot) -> None:
        now_ts = int(time.time())
        for row in await self.svc.db.schedules():
            if row["kind"] == "once" and now_ts - (row["at_ts"] or 0) >= ONCE_GRACE:    # 너무 늦게 켜짐: 안 하고 끔
                await self.svc.db._write("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
                from . import opsdesk   # 📥 운영 인박스용 기록 (sodam/opsdesk.py)
                await opsdesk.schedule_failed(self.svc, row, "missed")
            elif row["enabled"] and row["kind"] != "once" and _col(row, "end_ts", None) \
                    and now_ts > row["end_ts"]:                                          # 종료일 지남 → 깔끔하게 끔
                await self.svc.db._write("UPDATE schedules SET enabled=0 WHERE id=?", (row["id"],))
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

    async def _warn_bad_emoji(self, bot: Bot, draft: Draft) -> None:
        """내용에 '봇이 못 올리는 커스텀 이모지'(삭제됐거나 특수 이모지)가 있으면 어느 이모지·몇 번째 줄인지
        콕 집어 알려 준다 (2026-10-27 오너: 예약공지가 Document_invalid 로 조용히 씹히던 걸, 원인을 사람 말로).
        막지는 않음 — 발송 땐 글자로 폴백되니, 경고만 하고 다음 단계로 진행. 경고는 지우지 않고 남긴다."""
        if draft.fmt != "html":
            return
        try:
            bad = await unsendable_emojis(bot, draft.user_id, draft.text)
        except TelegramError:
            return
        if not bad:
            return
        lst = "\n".join(f"• {ch} — {ln}번째 줄" if ln else f"• {ch}" for ch, ln in bad)
        await bot.send_message(draft.ui_chat_id,
                               "⚠️ <b>텔레그램이 못 올리는 이모지</b>가 있어요 (삭제됐거나 봇이 못 쓰는 특수 이모지):\n"
                               f"{lst}\n\n이대로 올리면 그 이모지는 <b>글자로 바뀌어</b> 나가요. 원래 모양으로 올리려면 "
                               "그 이모지를 빼거나 키보드 이모지로 바꿔서 <b>내용을 다시 보내주세요</b>.", parse_mode="HTML")

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
            draft.del_prev = bool(_col(edit_row, "del_prev", 1))
            for k in ("start_ts", "end_ts", "active_from", "active_to"):
                setattr(draft, k, _col(edit_row, k, None))
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
                                    f"<b>1/7 제목</b>을 보내주세요. 제목 없이 하려면 <code>없음</code>{keep}")
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
        if not draft or draft.step not in ("title", "body", "media", "when"):
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
                            "<b>2/7 내용</b>을 보내주세요.\n"
                            "• 글만 보내면 다음 단계에서 사진·영상을 따로 받아요 (사진에 설명을 붙여 한 번에 보내도 돼요)\n"
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
                await self._warn_bad_emoji(bot, draft)   # 못 올리는 커스텀 이모지면 어느 줄인지 콕 집어 알림 (막진 않음)
                if media_id:   # 사진에 설명을 붙여 한 번에 → 사진 단계 건너뜀 (글만이면 지금 붙은 사진은 다음 단계에서 정함)
                    draft.media_type, draft.media_id = media_type, media_id
                    mediastore.remember_soon(bot, self.svc.db, media_type, media_id)   # 봇이 바뀌어도 다시 올릴 원본
                    await self._ask_when(bot, draft)
                    return True
            await self._ask_media(bot, draft)
            return True

        if draft.step == "media":
            media_type, media_id = extract_media(msg)
            if media_id:
                if len(html_plain(draft.text) if draft.fmt == "html" else draft.text) > CAPTION_LIMIT - 150:
                    await self._say(bot, draft, f"내용이 길어서 사진·영상 설명으로 못 붙여요 (텔레그램 제한 1024자 → "
                                                f"{CAPTION_LIMIT - 150}자 이내). 사진 없이 하려면 <code>없음</code>")
                    return True
                draft.media_type, draft.media_id = media_type, media_id
                mediastore.remember_soon(bot, self.svc.db, media_type, media_id)   # 봇이 바뀌어도 다시 올릴 원본
            elif text in ("없음", "-", "건너뛰기", "패스"):
                draft.media_type = draft.media_id = None
            elif text not in KEEP:   # 그대로 = 지금 상태 (붙은 게 있으면 그것, 없으면 없이)
                await self._say(bot, draft, "사진·영상·GIF·파일을 보내주세요. 사진 없이 하려면 <code>없음</code>")
                return True
            await self._ask_when(bot, draft)
            return True

        if draft.step == "limits":
            if text in ("없음", "-", "건너뛰기", "패스") or text in KEEP:
                draft.start_ts = draft.end_ts = draft.active_from = draft.active_to = None
            else:
                try:
                    draft.start_ts, draft.end_ts, draft.active_from, draft.active_to = \
                        parse_limits(text, self.svc.cfg.tz)
                except ValueError as e:
                    await self._say(bot, draft, f"❌ {e}")
                    return True
            await self._ask_confirm(bot, draft)
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
        await self._say(bot, draft, "<b>5/7 고정</b>할까요? (고정하면 지난 회차 공지는 지우고 새로 고정해요)", kb)
        return True

    async def _ask_keep(self, bot: Bot, draft: Draft) -> None:
        """6/7 반복 공지: 새 회차 올릴 때 지난 회차 글을 지울지(기본) 그대로 쌓을지."""
        draft.step = "keep"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 지우고 새로 (기본)", callback_data=f"an:{draft.token}:keep1"),
            InlineKeyboardButton("📚 그대로 쌓기", callback_data=f"an:{draft.token}:keep0"),
        ]])
        await self._say(bot, draft, "<b>6/7 지난 회차 글</b>을 어떻게 할까요? (반복 공지일 때)\n"
                                    "• <b>지우고 새로</b> = 새 글 올릴 때 이전 회차 글을 지워요 (하나만 남음)\n"
                                    "• <b>그대로 쌓기</b> = 이전 글을 안 지워요 (글이 계속 쌓여요)", kb)

    async def _ask_limits(self, bot: Bot, draft: Draft) -> None:
        """7/7 (선택) 유효 기간·하루 시간대 제한. 안 쓰면 [⏭ 제한 없음]."""
        draft.step = "limits"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ 제한 없음 (계속·종일)", callback_data=f"an:{draft.token}:nolimit")]])
        await self._say(bot, draft, "<b>7/7 기간·시간대 제한</b> (선택)\n"
                                    "특정 기간·시간대에만 올리고 싶으면 보내주세요 (둘 다 가능, 한 줄에 하나):\n"
                                    "• 기간: <code>10-01~10-31</code> (이 기간만 올림)\n"
                                    "• 하루 시간대: <code>9시~18시</code> (그 시간대에만)\n"
                                    "• 없으면 아래 버튼 또는 <code>없음</code>", kb)

    async def _ask_confirm(self, bot: Bot, draft: Draft) -> None:
        draft.step = "confirm"
        keep = "" if draft.del_prev else " · 📚 이전 글 쌓기"
        lim = describe_limits(draft.start_ts, draft.end_ts, draft.active_from, draft.active_to, self.svc.cfg.tz)
        await self._say(bot, draft, "👀 <b>미리보기</b> — "
                                    f"{describe_when(draft.kind, draft.at_time, draft.interval_min)}"
                                    f"{' · 📌 고정' if draft.pin else ''}{keep}"
                                    f"{' · ' + lim if lim else ''}")
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

    async def _ask_media(self, bot: Bot, draft: Draft) -> None:
        """3/7 사진·영상 (선택) — 글을 먼저 쓰고 사진을 따로 올리는 관리자가 많음 (오너 요청 2026-10-05)."""
        draft.step = "media"
        what = {"photo": "사진", "video": "영상", "animation": "GIF", "document": "파일"}.get(draft.media_type or "", "")
        rows = [[InlineKeyboardButton("⏭ 사진 없이", callback_data=f"an:{draft.token}:nomedia")]]
        now = ""
        if draft.media_id:
            rows[0].insert(0, InlineKeyboardButton(f"✅ 지금 {what} 그대로", callback_data=f"an:{draft.token}:keepmedia"))
            now = f"\n(지금 붙은 {what}이 있어요 · 그대로 두려면 <code>그대로</code>)"
        await self._say(bot, draft, "<b>3/7 사진·영상</b>을 같이 올릴까요?\n"
                                    "• 사진·영상·GIF·파일을 보내주세요 (공지 글이 그 설명으로 붙어요)\n"
                                    "• 없으면 <code>없음</code> 또는 아래 버튼" + now, InlineKeyboardMarkup(rows))

    async def _ask_when(self, bot: Bot, draft: Draft) -> None:
        draft.step = "when"
        now = (f"\n(지금: {describe_when(draft.kind, draft.at_time, draft.interval_min)} · "
               f"<code>그대로</code>)") if draft.edit_id is not None else ""
        await self._say(bot, draft, "<b>4/7 언제</b> 올릴까요? (한 줄에 하나만)\n"
                                    "• 매일 정해진 시각: <code>매일 09:00</code>\n"
                                    "• 요일: <code>매주 월 10:00</code> · <code>평일 09:00</code>\n"
                                    "• <b>매월 정해진 날</b>: <code>매월 15일 09:00</code> (없는 날은 말일)\n"
                                    "• 일정 간격: <code>반복 120</code>(분) · <code>반복 3시간</code>\n"
                                    "• 그 시각 맞춰 반복: <code>00:15 반복 4시간</code> (0시15분·4시15분·…)" + now)

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
        if action in ("nomedia", "keepmedia") and draft.step == "media":
            await query.answer()
            if action == "nomedia":
                draft.media_type = draft.media_id = None
            await self._ask_when(bot, draft)
            return
        if action in ("pin1", "pin0") and draft.step == "pin":
            draft.pin = action == "pin1"
            await query.answer()
            await self._ask_keep(bot, draft)   # 예약공지 마법사는 반복만 (once 는 parse_when 거부) → 늘 '쌓기' 물음
            return
        if action in ("keep1", "keep0") and draft.step == "keep":
            draft.del_prev = action == "keep1"
            await query.answer()
            await self._ask_limits(bot, draft)
            return
        if action == "nolimit" and draft.step == "limits":
            draft.start_ts = draft.end_ts = draft.active_from = draft.active_to = None
            await query.answer()
            await self._ask_confirm(bot, draft)
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
                      media_id=draft.media_id, pin=int(draft.pin), del_prev=int(draft.del_prev),
                      start_ts=draft.start_ts, end_ts=draft.end_ts,
                      active_from=draft.active_from, active_to=draft.active_to, fmt=draft.fmt)
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
