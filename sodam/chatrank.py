"""🏆 채팅 순위 틀 (2026-10-12 고객 'FOX 이벤트 봇처럼, 하드코딩 없이 방장이 꾸미게').

숫자(누가 몇 번)는 소담이 기록에서 셈, 모양은 전부 방장 값:
  기간(오늘·어제·최근 7일·최근 30일) · 몇 등까지(3~10) · 등수별 상품 글 · 줄 모양('{등수} {이름} — {횟수}회 {상품}') ·
  등수 그림(이모지·움직이는 이모지) · 머리말·꼬리말('{기간}·{날짜}', 움직이는 이모지·굵게 OK) · 이름 보이기(아이디·이름·가리기) ·
  관리자 빼기 · 어제 당첨자 칸 · 시상식 시각(그 시각에 '어제 하루' 순위를 방에 올리고 당첨 기록 + 관리자 1:1 지급 완료 버튼).
집계 규칙(최소 글자·반복 빼기)은 📊 화면 설정(chat_min_chars·chat_no_repeat) 그대로 — stats.min_chars.
틀은 텔레그램 HTML 로 보관 (util.rich_html — 붙여 넣은 움직이는 이모지가 그대로). 값은 칸 이름만 바꿈(코드 실행 X).
화면·명령·시상식은 panels/chatrank.py, 테스트 tests/test_chatrank.py.
"""
from __future__ import annotations

import itertools
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db import register_schema
from .util import display_name, esc, html_balanced, html_plain, period_range

register_schema("""
CREATE TABLE IF NOT EXISTS rank_rooms (
    chat_id    INTEGER PRIMARY KEY,
    cfg        TEXT NOT NULL,
    updated    INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER
);
CREATE TABLE IF NOT EXISTS rank_awards (
    chat_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,
    rank     INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    n        INTEGER NOT NULL DEFAULT 0,
    prize    TEXT NOT NULL DEFAULT '',
    paid_by  INTEGER,
    paid_at  INTEGER,
    PRIMARY KEY (chat_id, day, rank)
);
""")

PERIODS = {"today": ("오늘", "오늘"), "yesterday": ("어제", "어제"), "thisweek": ("이번주", "이번 주"), "week": ("주간", "최근 7일"),
           "month": ("월간", "최근 30일"), "lastweek": ("지난주", "지난 주")}
PERIOD_CHOICES = ("today", "yesterday", "thisweek", "week", "month")    # 방장이 고르는 것 (lastweek 는 시상식·.순위 지난주 용)
WEEKLY = 100                                 # 시상식 '매주 월요일 0시' (지난 주 월~일 순위) — 고객 2026-10-12 '추첨은 월요일 자정'
AWARD_LABEL = {-1: "끔", 0: "매일 0시", 9: "매일 9시", 12: "매일 12시", WEEKLY: "매주 월요일 0시"}
NAME_MODES = {"user": "아이디 (없으면 이름)", "name": "이름", "mask": "가리기 (sin***)"}
AWARD_HOURS = (-1, 0, 9, 12, WEEKLY)         # 시상식 (한국 시각, -1 = 끔, WEEKLY = 매주 월요일 0시)
TOP_MIN, TOP_MAX = 3, 10
PRIZE_MAX, LINE_MAX, HEAD_MAX, FOOT_MAX, ICON_MAX = 30, 120, 300, 600, 8
LINE_FIELDS = ("등수", "이름", "횟수", "상품")
HEAD_FIELDS = ("기간", "날짜")
ME_FIELDS = ("이름", "등수", "횟수", "기간")
ICONS = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
DEFAULT = {"period": "today", "top": 5, "prizes": [], "line": "{등수} {이름} — {횟수}회 {상품}", "icons": [],
           "head": "🏆 <b>{기간} 채팅 순위</b>\n📅 {날짜}", "foot": "", "names": "user", "no_admin": False,
           "yday": True, "award": -1, "me": "📊 {이름} 님 — {기간} 채팅 {횟수}회 · {등수}위"}
CACHE_SEC = 30
_CACHE: dict = {}
_SEQ = itertools.count(1)
_FIELD = re.compile(r"\{([^{}\n]{1,8})\}")
_EMOJI_TAG = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", re.S)


@dataclass
class Cfg:
    period: str = "today"
    top: int = 5
    prizes: list = field(default_factory=list)
    line: str = DEFAULT["line"]
    icons: list = field(default_factory=list)
    head: str = DEFAULT["head"]
    foot: str = ""
    names: str = "user"
    no_admin: bool = False
    yday: bool = True
    award: int = -1
    me: str = DEFAULT["me"]
    reset_at: int = 0      # 채팅 수 초기화 시각 — 이 뒤 글만 셈 (글은 안 지움: AI 기억·검색·사칭 확인 기록 그대로). 0 = 안 함

    def icon(self, i: int) -> str:
        return self.icons[i] if i < len(self.icons) else ICONS[i]

    def prize(self, i: int) -> str:
        return self.prizes[i] if i < len(self.prizes) else ""

    def dump(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def load(raw: str | None) -> Cfg:
    """저장된 값 → Cfg. 옛 값·깨진 값은 기본으로 (하나가 틀려도 나머지는 살림)."""
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        d = {}
    c = Cfg()
    if d.get("period") in PERIOD_CHOICES:
        c.period = d["period"]
    if isinstance(d.get("top"), int) and TOP_MIN <= d["top"] <= TOP_MAX:
        c.top = d["top"]
    if isinstance(d.get("prizes"), list):
        c.prizes = [str(p)[:PRIZE_MAX] for p in d["prizes"][:TOP_MAX]]
    for k in ("line", "head", "foot", "me"):
        if isinstance(d.get(k), str) and html_balanced(d[k]):
            setattr(c, k, d[k])
    if isinstance(d.get("icons"), list):
        c.icons = [str(i) for i in d["icons"][:TOP_MAX]]
    if d.get("names") in NAME_MODES:
        c.names = d["names"]
    c.no_admin, c.yday = bool(d.get("no_admin", False)), bool(d.get("yday", True))
    if d.get("award") in AWARD_HOURS:
        c.award = d["award"]
    if isinstance(d.get("reset_at"), int) and d["reset_at"] > 0:
        c.reset_at = d["reset_at"]
    return c


async def get(db, chat_id: int) -> Cfg:
    r = await db._one("SELECT cfg FROM rank_rooms WHERE chat_id=?", (chat_id,))
    return load(r["cfg"] if r else None)


async def save(db, chat_id: int, cfg: Cfg, by: int | None) -> None:
    await db._write("INSERT INTO rank_rooms(chat_id, cfg, updated, updated_by) VALUES(?,?,?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET cfg=excluded.cfg, updated=excluded.updated, updated_by=excluded.updated_by",
                    (chat_id, cfg.dump(), int(time.time()), by))


async def award_rooms(db) -> list[tuple[int, Cfg]]:
    out = []
    for r in await db._all("SELECT chat_id, cfg FROM rank_rooms"):
        c = load(r["cfg"])
        if c.award >= 0:
            out.append((r["chat_id"], c))
    return out


# ── 방장 글 검사 ──────────────────────────────────────────
def clean_tpl(html: str, fields: tuple[str, ...], limit: int, need: tuple[str, ...] = ()) -> str:
    """방장이 보낸 틀(텔레그램 HTML) 검사. 모르는 칸·필요한 칸 없음·짝 안 맞는 태그·너무 긺·링크면 ValueError."""
    html = (html or "").strip()
    plain = html_plain(html)
    if not plain.strip():
        raise ValueError("글이 비었어요")
    if len(plain) > limit:
        raise ValueError(f"{limit}자 안으로 줄여 주세요 (지금 {len(plain)}자)")
    if not html_balanced(html):
        raise ValueError("서식이 깨졌어요. 다시 보내 주세요")
    if "<a " in html or re.search(r"https?://|t\.me/", plain, re.I):
        raise ValueError("링크는 넣을 수 없어요 (아래 버튼으로 넣어요)")
    bad = [f for f in _FIELD.findall(plain) if f not in fields]
    if bad:
        raise ValueError(f"모르는 칸 {{{bad[0]}}} — 쓸 수 있는 칸: " + " ".join("{" + f + "}" for f in fields))
    miss = [f for f in need if "{" + f + "}" not in plain]
    if miss:
        raise ValueError(f"{{{miss[0]}}} 칸은 꼭 넣어 주세요")
    return html


def clean_prizes(text: str) -> list[str]:
    """한 줄에 하나 (1등부터). '1등 5만원' 처럼 앞 등수 글자는 뗌."""
    out = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"^\s*\d{1,2}\s*(등|위|\.|\))\s*[:：-]?\s*", "", ln).strip()
        if not ln:
            continue
        if re.search(r"https?://|t\.me/|@\w{4,}", ln, re.I):
            raise ValueError("상품 글에 링크·@아이디는 넣을 수 없어요")
        if len(ln) > PRIZE_MAX:
            raise ValueError(f"상품 글은 하나에 {PRIZE_MAX}자 안 ('{ln[:12]}…')")
        out.append(ln)
    if len(out) > TOP_MAX:
        raise ValueError(f"{TOP_MAX}등까지만 돼요")
    return out


def clean_icons(html: str) -> list[str]:
    """띄어 쓴 이모지(움직이는 이모지 포함) → 등수 그림 목록. 하나에 보이는 글자 ICON_MAX 안."""
    toks = re.findall(r"<tg-emoji\b[^>]*>.*?</tg-emoji>|[^\s<]+", html or "", re.S)
    out = []
    for t in toks:
        if not t.startswith("<tg-emoji"):
            if re.search(r"[<>]|https?:|@\w", t):
                raise ValueError("이모지·숫자만 띄어서 보내 주세요")
        if len(html_plain(t)) > ICON_MAX:
            raise ValueError(f"그림 하나가 너무 길어요 ('{html_plain(t)[:8]}…') — 띄어서 보내 주세요")
        out.append(t)
    if not out:
        raise ValueError("이모지를 띄어서 보내 주세요")
    return out[:TOP_MAX]


def strip_custom(html: str) -> str:
    """움직이는 이모지 → 보통 이모지 (봇 주인이 프리미엄이 아니라 거절될 때 다시 보내기용)."""
    return _EMOJI_TAG.sub(lambda m: m.group(1), html or "")


# ── 글 만들기 ─────────────────────────────────────────────
def fill(tpl: str, vals: dict) -> str:
    """{칸} 을 값으로 (값은 이미 HTML 이스케이프한 것). 모르는 칸은 그대로 둠 — 검사 단계에서 막았음."""
    return _FIELD.sub(lambda m: vals.get(m.group(1), m.group(0)), tpl or "")


def mask(name: str) -> str:
    keep = max(1, min(3, len(name) // 3))
    return name[:keep] + "***"


def show_name(row, mode: str) -> str:
    uname, first = (row["username"] or "").strip(), row["first_name"] or ""
    if mode == "name":
        name = display_name(first, None, None) or uname or "멤버"
    else:
        name = uname or display_name(first, None, None) or "멤버"
    return mask(name) if mode == "mask" else name


def line_html(cfg: Cfg, i: int, name: str, n: int | None, prize: str | None = None) -> str:
    vals = {"등수": cfg.icon(i), "이름": esc(name), "횟수": f"{n:,}" if n is not None else "",
            "상품": esc(cfg.prize(i) if prize is None else prize)}
    out = fill(cfg.line, vals)
    out = re.sub(r"[ \t]+$", "", out)
    return re.sub(r"\s*[—\-·:]\s*$", "", out) if not vals["상품"] and "{상품}" in cfg.line else out


def render(cfg: Cfg, label: str, date: str, rows: list, yday: list | None = None, yday_date: str = "",
           me: tuple[int, int] | None = None, yday_label: str = "어제 당첨자") -> str:
    """rows = [(이름, 횟수)] 등수 순. yday = [(등수-1, 이름, 횟수, 상품)]. me = (내 등수, 횟수) — 1:1 만."""
    head = fill(cfg.head, {"기간": esc(label), "날짜": esc(date)})
    out = [head, ""] if head.strip() else []
    if rows:
        out += [line_html(cfg, i, name, n) for i, (name, n) in enumerate(rows)]
    else:
        out.append(f"아직 {esc(label)} 기록이 없어요.")
    if me:
        out += ["", f"👤 나 — {me[0]}위 · {me[1]:,}회" if me[0] else "👤 나 — 아직 기록 없음"]
    if yday:
        out += ["", f"🏆 <b>{esc(yday_label)}</b> ({esc(yday_date)})"]
        out += [line_html(cfg, i, name, n, prize) for i, name, n, prize in yday]
    if cfg.foot.strip():
        out += ["", cfg.foot]
    return "\n".join(out).strip()


def me_html(cfg: Cfg, name: str, label: str, rank: int, n: int) -> str:
    """.나 한 줄 (방장 틀). 기록이 없으면 정해진 한 줄."""
    if not n:
        return f"📊 {esc(name)} 님 — {esc(label)} 채팅 기록이 아직 없어요."
    return fill(cfg.me, {"이름": esc(name), "등수": str(rank), "횟수": f"{n:,}", "기간": esc(label)})


# ── 숫자 ──────────────────────────────────────────────────
def day_of(tz, days_ago: int = 0, now: float | None = None) -> str:
    return (datetime.fromtimestamp(now or time.time(), tz).date() - timedelta(days=days_ago)).isoformat()


def short_date(day: str) -> str:
    return f"{int(day[5:7])}/{int(day[8:10])}"


def monday(tz, weeks_ago: int = 0, now: float | None = None):
    d = datetime.fromtimestamp(now or time.time(), tz).date()
    return d - timedelta(days=d.weekday() + 7 * weeks_ago)


def span(key: str, tz, now: float | None = None) -> tuple[int, int | None, str]:
    """기간 → (시작, 끝, 날짜 글). 이번 주 = 이번 월요일 0시부터 (매주 월요일 0시에 0부터 다시), 지난 주 = 지난 월~일."""
    if key in ("thisweek", "lastweek"):
        m = monday(tz, 1 if key == "lastweek" else 0, now)
        start = datetime(m.year, m.month, m.day, tzinfo=tz)
        until = int((start + timedelta(days=7)).timestamp()) if key == "lastweek" else None
        return int(start.timestamp()), until, f"{short_date(m.isoformat())}~{short_date((m + timedelta(days=6)).isoformat())}"
    since, until, _ = period_range(PERIODS[key][0], tz)
    if key in ("week", "month"):
        date = f"{short_date(day_of(tz, 6 if key == 'week' else 29))}~{short_date(day_of(tz))}"
    else:
        date = short_date(day_of(tz, 1 if key == "yesterday" else 0))
    return since, until, date


def weekly(cfg: "Cfg") -> bool:
    """주 단위 방 (이번 주 순위 또는 매주 시상식) → '지난주 당첨자'."""
    return cfg.period == "thisweek" or cfg.award == WEEKLY


def award_key(tz, cfg: "Cfg", now: float | None = None) -> tuple[str, str]:
    """(기록 열쇠, 순위 기간) — 매일 = 어제 날짜·yesterday, 매주 = 'w'+지난 월요일·lastweek (버튼 글에 ':' 없게)."""
    if cfg.award == WEEKLY or (cfg.award < 0 and weekly(cfg)):
        return "w" + monday(tz, 1, now).isoformat(), "lastweek"
    return day_of(tz, 1, now), "yesterday"


async def standings(svc, chat_id: int, cfg: Cfg, period: str | None = None, bot=None) -> tuple[str, str, list]:
    """(표시 이름, 날짜, [(row, 횟수)]) — 관리자 빼기면 텔레그램 관리자 목록으로 거름(봇이 모르면 안 거름)."""
    from . import stats
    key = period or cfg.period
    since, until, date = span(key, svc.cfg.tz)
    since = max(since, cfg.reset_at)                   # 🔄 채팅 수 초기화 뒤 글만
    rule = await stats.min_chars(svc.db, chat_id)
    extra = 10 if cfg.no_admin else 0
    ck = (svc.db.__dict__.setdefault("_rank_tok", next(_SEQ)), chat_id, key, since, cfg.top + extra, rule)   # id() 는 시험에서 재사용됨
    hit = _CACHE.get(ck)
    if hit and time.monotonic() - hit[0] < CACHE_SEC:   # 미니앱을 여는 사람마다 하루치 글을 다시 세지 않게 (바쁜 방 하루 1만 개↑)
        rows = hit[1]
    else:
        rows = await svc.db.top_chatters(chat_id, since, cfg.top + extra, until, rule)
        if len(_CACHE) > 500:
            _CACHE.clear()
        _CACHE[ck] = (time.monotonic(), rows)
    if cfg.no_admin and bot is not None:
        try:
            admins = {u.id for u in await svc.perms.admin_users(bot, chat_id)}
        except Exception:
            admins = set()
        rows = [r for r in rows if r["user_id"] not in admins]
    rows = rows[:cfg.top]
    return PERIODS[key][1], date, rows


async def my_place(svc, chat_id: int, user_id: int, cfg: Cfg, period: str | None = None) -> tuple[int, int]:
    """(내 등수, 횟수). 기록 없으면 (0, 0). 관리자 빼기는 등수에 반영 안 함 (대략)."""
    from . import stats
    from .db import count_filter
    since, until, _ = span(period or cfg.period, svc.cfg.tz)
    since = max(since, cfg.reset_at)
    end = until if until is not None else 1 << 62
    m = await stats.min_chars(svc.db, chat_id)
    mine = await svc.db._one("SELECT COUNT(*) n FROM messages WHERE chat_id=? AND user_id=? AND ts>=? AND ts<? AND is_bot=0 "
                             + count_filter(), (chat_id, user_id, since, end, *m.args))
    n = int(mine["n"] if mine else 0)
    if not n:
        return 0, 0
    above = await svc.db._one("SELECT COUNT(*) n FROM (SELECT user_id, COUNT(*) c FROM messages WHERE chat_id=? AND ts>=? AND ts<? "
                              "AND is_bot=0 " + count_filter() + " GROUP BY user_id) WHERE c>?", (chat_id, since, end, *m.args, n))
    return int(above["n"]) + 1, n


def winners_label(cfg: Cfg) -> str:
    return "지난주 당첨자" if weekly(cfg) else "어제 당첨자"


async def yday_winners(svc, chat_id: int, cfg: Cfg, bot=None) -> tuple[str, list]:
    """어제(주 단위 방은 지난주) 당첨자: 시상식 기록이 있으면 그것, 없으면 그 기간 순위를 바로 셈 (상품은 지금 값)."""
    key, period = award_key(svc.cfg.tz, cfg)
    _, _, date = span(period, svc.cfg.tz)
    rows = await svc.db._all("SELECT rank, name, n, prize FROM rank_awards WHERE chat_id=? AND day=? ORDER BY rank", (chat_id, key))
    if rows:
        # 기록엔 아이디로 남김(관리자 지급용) → 방에 보일 땐 지금 '가리기' 설정을 따름 (리뷰 2026-10-12)
        return date, [(r["rank"] - 1, mask(r["name"]) if cfg.names == "mask" else r["name"], r["n"], r["prize"]) for r in rows]
    _, _, live = await standings(svc, chat_id, cfg, period, bot)
    return date, [(i, show_name(r, cfg.names), r["n"], cfg.prize(i)) for i, r in enumerate(live)]


async def record_awards(db, chat_id: int, day: str, cfg: Cfg, rows: list) -> int:
    """시상식 기록 (같은 날 두 번 안 들어감). 넣은 줄 수."""
    def go(c):
        n = 0
        for i, r in enumerate(rows):
            n += c.execute("INSERT OR IGNORE INTO rank_awards(chat_id, day, rank, user_id, name, n, prize) VALUES(?,?,?,?,?,?,?)",
                           (chat_id, day, i + 1, r["user_id"], show_name(r, "user"), r["n"], cfg.prize(i))).rowcount
        return n
    return await db.atomic(go)


async def mark_paid(db, chat_id: int, day: str, rank: int, by: int) -> bool:
    return bool(await db.atomic(lambda c: c.execute(
        "UPDATE rank_awards SET paid_by=?, paid_at=? WHERE chat_id=? AND day=? AND rank=? AND paid_at IS NULL",
        (by, int(time.time()), chat_id, day, rank)).rowcount))
