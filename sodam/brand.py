"""🎁 방 전용 1:1 메뉴 — 데이터·방 고르기 (설계: claude.ai 설계 페이지 '방 전용 1:1 메뉴', 2026-10-12 고객 요청 'AI 픽스터처럼').

소담은 봇 하나를 여러 방이 같이 씀 → 1:1 에서 "지금 어느 방 손님인지"를 먼저 정하고, 출석·복권·운세·픽·알림을 전부 그 방 기준으로.
- 방마다 brand_rooms 한 줄: 켜짐·이름·색(THEMES)·아래쪽 메뉴 버튼(부품 PARTS + 값)·복권 안내·당첨 연락처·딥링크 코드.
- 들어오는 길 = 방 전용 링크 t.me/<봇>?start=b_<코드> (코드는 무작위 — 방 ID 를 몰라도 되고, 링크를 바꾸면 옛 링크는 끝).
- 고른 방은 brand_session(사람 → 방). 쓸 때마다 '그 방이 아직 켜져 있고 이용 기간이고 이 사람이 멤버인지' 다시 확인 (current).
- 기록은 원래 표 그대로 (방, 사람) 키 — A방 출석이 B방 복권으로 넘어갈 길이 없음.
- 버튼은 고정 목록이 아니라 부품 + 값 (CLAUDE.md '프레임워크로'): 방장이 부품을 골라 이름·색·값만 넣음, 최대 MAX_BUTTONS.
테스트 tests/test_brand.py.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field

from .db import register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS brand_rooms (
    chat_id  INTEGER PRIMARY KEY,
    code     TEXT NOT NULL UNIQUE,
    enabled  INTEGER NOT NULL DEFAULT 0,
    name     TEXT NOT NULL DEFAULT '',
    theme    TEXT NOT NULL DEFAULT 'navy',
    buttons  TEXT NOT NULL DEFAULT '[]',
    notice   TEXT NOT NULL DEFAULT '',
    contact  TEXT NOT NULL DEFAULT '',
    updated  INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER
);
CREATE TABLE IF NOT EXISTS brand_session (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS brand_images (
    chat_id INTEGER NOT NULL,
    slot    TEXT NOT NULL,
    file    TEXT NOT NULL,            -- data/brand 안 파일 이름 (<chat>_<slot>_<sha8>.jpg)
    src     TEXT NOT NULL,            -- upload / ai
    sticker TEXT,                     -- 당첨 스티커 file_id (slot=win 일 때, 이 봇 것)
    updated INTEGER NOT NULL,
    PRIMARY KEY (chat_id, slot)
);
""", migrate={"brand_rooms": "composite", "brand_session": "plain", "brand_images": "composite"})

MAX_BUTTONS = 8
LABEL_MAX = 20
NOTICE_MAX = 120
TEXT_MAX = 600
NAME_MAX = 24
SLOTS = {"bg": "카드 배경", "win": "당첨 그림"}
STYLES = {"": "기본", "primary": "파랑", "success": "초록", "danger": "빨강"}

# 색 묶음: 카드·미니앱이 같이 씀 (bg1→bg2 그라데이션, 강조색, 글자색). 방장은 이름만 고름
THEMES: dict[str, dict] = {
    "navy":   {"label": "남색·금색", "bg1": (13, 22, 51), "bg2": (44, 63, 134), "accent": (242, 193, 78), "fg": (255, 255, 255)},
    "red":    {"label": "빨강·금색", "bg1": (60, 8, 14), "bg2": (150, 22, 36), "accent": (255, 205, 90), "fg": (255, 255, 255)},
    "black":  {"label": "검정·네온", "bg1": (8, 8, 12), "bg2": (34, 30, 52), "accent": (80, 255, 200), "fg": (255, 255, 255)},
    "green":  {"label": "초록·금색", "bg1": (6, 40, 28), "bg2": (18, 104, 70), "accent": (250, 210, 100), "fg": (255, 255, 255)},
    "purple": {"label": "보라·분홍", "bg1": (30, 10, 60), "bg2": (96, 36, 150), "accent": (255, 140, 210), "fg": (255, 255, 255)},
}


@dataclass(frozen=True)
class Part:
    kind: str
    label: str            # 기본 버튼 글자
    desc: str
    value: str = ""       # 값 종류: "" 없음 / url / contact / text


PARTS: dict[str, Part] = {p.kind: p for p in (
    Part("picks", "🌟 오늘의 추천픽", "주요 리그 오늘 경기 + 많이 고른 쪽 (AI 분석은 누를 때만)"),
    Part("alerts", "📡 스코어 알림 받기", "경기·팀을 골라 골·결과 알림을 1:1 로"),
    Part("checkin", "📅 출석체크", "하루 한 번 출석 + 복권 1장 (방 복권이 켜져 있을 때)"),
    Part("lotto", "🎟 행운의 복권", "내 복권 긁기 (미니앱이면 손가락으로)"),
    Part("fortune", "🔮 오늘의 운세", "하루 동안 같은 운세 카드"),
    Part("app", "✨ 혜택 화면", "출석·복권·운세·픽을 한 화면에 (미니앱)"),
    Part("link", "🔗 바로가기", "누르면 사이트 열기 버튼", "url"),
    Part("contact", "💬 문의하기", "누르면 그 사람 1:1 열기 버튼", "contact"),
    Part("text", "📢 안내", "방장이 쓴 안내 글 보여 주기", "text"),
)}
DEFAULT_BUTTONS = [{"k": "picks", "s": "danger", "w": 2}, {"k": "alerts"}, {"k": "fortune"},
                   {"k": "checkin"}, {"k": "lotto"}]


@dataclass
class Room:
    chat_id: int
    code: str
    enabled: bool = False
    name: str = ""
    theme: str = "navy"
    buttons: list = field(default_factory=list)
    notice: str = ""
    contact: str = ""

    @property
    def colors(self) -> dict:
        return THEMES.get(self.theme, THEMES["navy"])


def _row(r) -> Room:
    try:
        buttons = clean_buttons(json.loads(r["buttons"] or "[]"))
    except (ValueError, TypeError):
        buttons = []
    return Room(r["chat_id"], r["code"], bool(r["enabled"]), r["name"] or "", r["theme"] if r["theme"] in THEMES else "navy",
                buttons, r["notice"] or "", r["contact"] or "")


# ── 값 검사 (저장 전에 늘 여기로) ─────────────────────────
_URL = re.compile(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d{2,5})?(/[^\s<>\"'\\]*)?$")
_USER = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_TME = re.compile(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?$")
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")


def clean_label(raw: str) -> str:
    v = " ".join(_CTRL.sub("", str(raw or "")).split())
    if not v:
        raise ValueError("버튼 글자가 비었어요")
    if len(v) > LABEL_MAX:
        raise ValueError(f"버튼 글자는 {LABEL_MAX}자 안으로")
    return v


def clean_url(raw: str) -> str:
    v = str(raw or "").strip()
    if not _URL.fullmatch(v) or len(v) > 300:
        raise ValueError("주소는 https:// 로 시작하는 한 줄로 (예: https://example.com)")
    return v


def clean_contact(raw: str) -> str:
    """@아이디 또는 t.me/아이디 → 아이디만 (사람·채널 1:1 열기용)."""
    v = str(raw or "").strip()
    m = _USER.fullmatch(v) or _TME.fullmatch(v)
    if not m:
        raise ValueError("문의는 @아이디 로 (예: @sodam_help)")
    return m.group(1)


def clean_text(raw: str, limit: int = TEXT_MAX) -> str:
    v = _CTRL.sub("", str(raw or "").replace("\r", "")).strip()
    v = re.sub(r"\n{3,}", "\n\n", v)
    if not v:
        raise ValueError("글이 비었어요")
    if len(v) > limit:
        raise ValueError(f"{limit}자 안으로 줄여 주세요")
    return v


def clean_name(raw: str) -> str:
    v = " ".join(_CTRL.sub("", str(raw or "")).split())
    if not v or len(v) > NAME_MAX:
        raise ValueError(f"이름은 1~{NAME_MAX}자로")
    if re.search(r"https?://|t\.me/|@\w", v, re.I):
        raise ValueError("이름엔 링크·@아이디를 못 넣어요")
    return v


def clean_button(b: dict) -> dict | None:
    """저장된 버튼 하나 검사 — 틀린 건 None (조용히 뺌, 화면이 깨지지 않게)."""
    if not isinstance(b, dict) or b.get("k") not in PARTS:
        return None
    part = PARTS[b["k"]]
    out = {"k": part.kind}
    try:
        out["t"] = clean_label(b.get("t") or part.label)
        if part.value == "url":
            out["v"] = clean_url(b.get("v", ""))
        elif part.value == "contact":
            out["v"] = clean_contact(b.get("v", ""))
        elif part.value == "text":
            out["v"] = clean_text(b.get("v", ""))
    except ValueError:
        return None
    if b.get("s") in STYLES and b.get("s"):
        out["s"] = b["s"]
    if b.get("w") == 2:
        out["w"] = 2
    return out


def clean_buttons(items) -> list[dict]:
    out, seen = [], set()
    for b in items if isinstance(items, list) else []:
        c = clean_button(b)
        if c is None or c["t"] in seen:      # 같은 글자 둘이면 눌렀을 때 어느 쪽인지 모름
            continue
        seen.add(c["t"])
        out.append(c)
        if len(out) >= MAX_BUTTONS:
            break
    return out


# ── 읽기·쓰기 ─────────────────────────────────────────────
async def get(db, chat_id: int) -> Room | None:
    r = await db._one("SELECT * FROM brand_rooms WHERE chat_id=?", (chat_id,))
    return _row(r) if r else None


async def ensure(db, chat_id: int, title: str = "") -> Room:
    """없으면 기본값으로 만듦 (꺼진 상태). 코드는 무작위 10자."""
    got = await get(db, chat_id)
    if got:
        return got
    name = (" ".join(_CTRL.sub("", title or "").split()) or "혜택 메뉴")[:NAME_MAX]
    await db._write("INSERT OR IGNORE INTO brand_rooms(chat_id, code, name, buttons, updated) VALUES(?,?,?,?,?)",
                    (chat_id, secrets.token_hex(5), name, json.dumps(DEFAULT_BUTTONS), int(time.time())))
    return await get(db, chat_id)


async def by_code(db, code: str) -> Room | None:
    if not re.fullmatch(r"[0-9a-f]{10}", code or ""):
        return None
    r = await db._one("SELECT * FROM brand_rooms WHERE code=?", (code,))
    return _row(r) if r else None


async def update(db, chat_id: int, by: int | None = None, **fields) -> None:
    """정해진 칸만 (값은 부른 쪽이 clean_* 로 검사)."""
    allowed = {"enabled", "name", "theme", "buttons", "notice", "contact", "code"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "buttons" in sets:
        sets["buttons"] = json.dumps(clean_buttons(sets["buttons"]), ensure_ascii=False)
    if "theme" in sets and sets["theme"] not in THEMES:
        raise ValueError("없는 색")
    if "enabled" in sets:
        sets["enabled"] = 1 if sets["enabled"] else 0
    cols = ", ".join(f"{k}=?" for k in sets)
    await db._write(f"UPDATE brand_rooms SET {cols}, updated=?, updated_by=? WHERE chat_id=?",
                    (*sets.values(), int(time.time()), by, chat_id))


async def new_code(db, chat_id: int, by: int | None = None) -> str:
    """링크 바꾸기 — 옛 링크는 바로 끝."""
    code = secrets.token_hex(5)
    await update(db, chat_id, by, code=code)
    return code


def link(bot_username: str, room: Room) -> str:
    return f"https://t.me/{bot_username}?start=b_{room.code}"


# ── 방 고르기 ─────────────────────────────────────────────
async def is_member(db, chat_id: int, user_id: int) -> bool:
    """소담이 본 멤버 기록 + 나간 기록 없음 (DB 만 — 텔레그램 확인은 부른 쪽이 한 번 더)."""
    r = await db._one("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    if not r:
        return False
    try:
        left = await db._one("SELECT 1 FROM member_left WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    except Exception:          # 나감 표가 없는 DB (테스트)
        left = None
    return left is None


async def rooms_for(db, user_id: int) -> list[tuple[int, str]]:
    """이 사람이 들어가 있고 메뉴를 켠 방 (최근 말한 순). 이용 기간은 부른 쪽이 거름."""
    rows = await db._all("SELECT b.chat_id, b.name FROM brand_rooms b JOIN members m ON m.chat_id=b.chat_id AND m.user_id=? "
                         "WHERE b.enabled=1 ORDER BY m.last_seen DESC LIMIT 20", (user_id,))
    out = []
    for r in rows:
        if await is_member(db, r["chat_id"], user_id):
            out.append((r["chat_id"], r["name"]))
    return out


async def select(db, user_id: int, chat_id: int) -> None:
    await db._write("INSERT INTO brand_session(user_id, chat_id, ts) VALUES(?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, ts=excluded.ts",
                    (user_id, chat_id, int(time.time())))


async def forget(db, user_id: int) -> None:
    await db._write("DELETE FROM brand_session WHERE user_id=?", (user_id,))


async def selected(db, user_id: int) -> int | None:
    r = await db._one("SELECT chat_id FROM brand_session WHERE user_id=?", (user_id,))
    return r["chat_id"] if r else None


async def usable(svc, chat_id: int, user_id: int) -> Room | None:
    """지금 이 사람에게 이 방 메뉴를 보여 줘도 되는지 — 켜짐·이용 기간·멤버. 아니면 None."""
    room = await get(svc.db, chat_id)
    if room is None or not room.enabled:
        return None
    if not await svc.paid_features(chat_id):
        return None
    if not await is_member(svc.db, chat_id, user_id):
        return None
    return room


async def current(svc, user_id: int) -> Room | None:
    """고른 방 (아직 쓸 수 있으면). 못 쓰게 됐으면 기억을 지우고, 남은 방이 하나뿐이면 그 방으로."""
    cid = await selected(svc.db, user_id)
    if cid is not None:
        room = await usable(svc, cid, user_id)
        if room:
            return room
        await forget(svc.db, user_id)
    left = [c for c, _ in await rooms_for(svc.db, user_id)]
    ok = [c for c in left if await svc.paid_features(c)]
    if len(ok) == 1:
        await select(svc.db, user_id, ok[0])
        return await get(svc.db, ok[0])
    return None


def button_for(room: Room, text: str) -> dict | None:
    """아래쪽 메뉴에서 누른 글자 → 그 버튼 (정확히 같을 때만 — 평범한 대화를 버튼으로 오해하지 않게)."""
    t = " ".join((text or "").split())
    for b in room.buttons:
        if b["t"] == t:
            return b
    return None


async def prune(db, now: float) -> None:
    await db._write("DELETE FROM brand_session WHERE ts < ?", (int(now) - 180 * 86400,))
