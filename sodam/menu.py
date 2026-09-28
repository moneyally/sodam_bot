"""버튼 메뉴 (그룹 관리 봇 방식). 1:1 채팅에서만 열리고, 누를 때마다 권한을 다시 확인한다.
그룹 관리자는 텔레그램 관리자 여부로 자동 인식 (코드 불필요).

메인(/start) → [➕ 그룹에 추가] [⚙️ 내 그룹 관리] [🪪 내 ID] [❓ 도움말]
그룹 허브 m:g → 🧩 기능 m:f · 🚪 입장 m:j · 🛡️ 보안 m:sec(⚠️ 경고 단계 m:wl · 🚫 금지어 m:bw · 🔗 허용 도메인 m:dom)
               · 🎭 말투 m:st · 💳 구독 m:sub (텔레그램 관리자·오너만)

콜백 형식 `m:<코드>:<방ID>[:인자]` (64바이트 이하). 라우터 순서:
1:1 확인 → 레이트리밋 → 엄격 파싱(알려진 코드·방 ID 형식·DB 에 있는 방) → 권한 확인 → 핸들러가 Screen 반환 → q.answer 정확히 1번.
토글·프리셋은 목표값을 버튼에 담아서 두 번 눌리거나 옛 패널을 눌러도 결과가 같다.
버튼에 담기엔 긴 값(금지어 등)과 삭제는 서버 쪽 1회용 토큰(`m:k:<토큰>`)으로.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, TelegramError

from .db import register_schema
from .security import normalize_domain
from .services import MenuToken, PendingInput
from .settings import LABELS, coerce, render
from .styles import STYLES
from .subscription import STATE_LABEL, chat_title, panel as sub_panel
from .util import esc, human_minutes, iyeyo, josa

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

# 봇을 추가할 때 미리 체크해 둘 관리자 권한 (텔레그램이 추가 화면에서 보여줌)
ADMIN_RIGHTS = "delete_messages+restrict_members+pin_messages+invite_users"

# 권한 단계
PUBLIC, ADMIN, TG_ADMIN, OWNER = range(4)

CID_RE = re.compile(r"^-\d{5,18}$")  # SQLite INTEGER(int64) 안에 들어가게
CALLBACK_PER_MIN = 40
TOKEN_TTL = 120          # 삭제 확인 버튼
LIST_TOKEN_TTL = 600     # 목록의 항목 버튼
MAX_TOKENS = 20000
MAX_TOKENS_PER_USER = 200  # 한 사람이 목록을 계속 열어도 다른 사람의 확인 버튼은 안 밀려나게
INPUT_GRACE = 600        # 입력 시간이 지난 뒤 이 시간 안에 온 글은 '시간 지남' 안내로 받아준다
LIST_SHOW = 30
MAX_WORDS = 200
MAX_DOMAINS = 50
MAX_ITEMS_PER_INPUT = 20
CANCEL = {"취소", "cancel", "/cancel"}

# 화면별 켜기/끄기 설정
FEATURE_TOGGLES = ["ai_enabled", "games_enabled", "sports_enabled", "daily_report"]
JOIN_TOGGLES = ["greet_enabled", "captcha_enabled", "recent_account_captcha", "cas_enabled", "delete_join_message",
                "impersonation_guard"]
SEC_TOGGLES = ["link_filter", "injection_guard", "injection_warn"]
TOGGLES = FEATURE_TOGGLES + JOIN_TOGGLES + SEC_TOGGLES

# 버튼으로 고르는 값: 설정 키 → [(저장값 문자열, 버튼 글자)]. 여기 없는 값은 콜백으로 와도 무시
PRESETS: dict[str, list[tuple[str, str]]] = {
    "captcha_minutes": [(v, f"⏱ {v}분") for v in ("1", "3", "5", "10")],
    "captcha_action": [("kick", "실패→킥"), ("ban", "실패→밴"), ("mute", "실패→뮤트")],
    "dup_limit": [(v, f"반복 {v}회") for v in ("2", "3", "5")],
    "newbie_link_hours": [("0", "신규링크 제한 끔"), ("24", "24시간"), ("72", "72시간")],
    "warn_mute_at": [(v, f"뮤트 {v}회") for v in ("2", "3", "5")],
    "warn_mute_minutes": [(v, f"뮤트 {human_minutes(int(v))}") for v in ("10", "60", "1440")],
    "warn_ban_at": [(v, f"밴 {v}회") for v in ("3", "5", "7")],
}
FLOOD_PRESETS = {
    "loose": ("느슨", {"flood_count": 10, "flood_seconds": 8, "flood_mute_minutes": 10}),
    "normal": ("보통", {"flood_count": 6, "flood_seconds": 8, "flood_mute_minutes": 30}),
    "strict": ("엄격", {"flood_count": 4, "flood_seconds": 8, "flood_mute_minutes": 60}),
}
# 설정을 바꾼 뒤 다시 그릴 화면
SCREEN_OF: dict[str, str] = {
    **{k: "f" for k in FEATURE_TOGGLES}, **{k: "j" for k in JOIN_TOGGLES}, **{k: "sec" for k in SEC_TOGGLES},
    "captcha_minutes": "j", "captcha_action": "j", "dup_limit": "sec", "newbie_link_hours": "sec",
    "warn_mute_at": "wl", "warn_mute_minutes": "wl", "warn_ban_at": "wl",
}


@dataclass
class Screen:
    text: str | None                      # None 이면 화면은 그대로, 토스트만
    kb: InlineKeyboardMarkup | None = None
    toast: str | None = None
    alert: bool = False


@dataclass
class PanelCtx:
    svc: Services
    bot: Bot
    uid: int
    cid: int | None
    args: list[str]

    def arg(self, i: int) -> str:
        return self.args[i] if len(self.args) > i else ""


Handler = Callable[[PanelCtx], Awaitable[Screen]]


@dataclass(frozen=True)
class Route:
    handler: Handler
    need: int = ADMIN
    scoped: bool = True    # 두 번째 칸이 방 ID
    fresh: bool = False    # 권한을 캐시 말고 지금 상태로 확인


def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def add_to_group_url(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?startgroup=true&admin={ADMIN_RIGHTS}"


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _chunks(buttons: list[InlineKeyboardButton], n: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]


# ── 권한 · 토큰 ───────────────────────────────────────────
async def _allowed(svc: Services, bot: Bot, cid: int, uid: int, need: int, fresh: bool = False) -> bool:
    if need == PUBLIC:
        return True
    try:
        if fresh:
            svc.perms.forget(cid)
        if need == ADMIN:
            return await svc.perms.is_admin(bot, cid, uid)
        if need == TG_ADMIN:
            return await svc.perms.is_tg_admin(bot, cid, uid)
        return uid in await svc.perms.owners()
    except TelegramError:
        return False  # 봇이 나간 방 등


def _token(svc: Services, uid: int, cid: int, action: str, arg, ttl: int = TOKEN_TTL) -> str:
    tokens = svc.menu_tokens
    now = time.time()
    for key in [k for k, t in tokens.items() if t.expires < now]:
        del tokens[key]
    mine = [k for k, t in tokens.items() if t.user_id == uid]
    for k in mine[:max(0, len(mine) - MAX_TOKENS_PER_USER + 1)]:  # 넘치면 그 사람 것 중 오래된 것부터
        del tokens[k]
    while len(tokens) >= MAX_TOKENS:  # 전체 상한(비정상 상황)
        del tokens[next(iter(tokens))]
    key = secrets.token_urlsafe(6)
    tokens[key] = MenuToken(uid, cid, action, arg, now + ttl)
    return key


# ── 메인 · 그룹 목록 ──────────────────────────────────────
async def main_menu(svc: Services, bot: Bot, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    name = svc.cfg.bot_name
    text = (f"👋 안녕하세요, 소통방 AI 비서 <b>{iyeyo(name)}</b>\n\n"
            "<b>이런 걸 해드려요</b>\n"
            "• 🕵️ 이름·아이디 변경 추적: 누가 이름을 바꿨는지, 사칭인지 바로 확인 <b>(무료)</b>\n"
            "• 🛡️ 방 관리: 입장 캡차, 도배·링크·사칭 차단, 경고·뮤트 <b>(무료)</b>\n"
            "• 🤖 AI 비서: 질문 답변, 방 자료 학습, 인사, 요약\n"
            "• 🎮 게임·🗓️ 예약공지·📊 채팅 랭킹·통계\n\n"
            "<b>시작하는 방법 (방 관리자)</b>\n"
            "1️⃣ 아래 <b>➕ 내 그룹에 추가하기</b> → 방 선택 (필요한 관리자 권한이 자동으로 체크돼요)\n"
            "2️⃣ 방에서 봇이 <b>관리자</b>인지 확인\n"
            "3️⃣ <b>⚙️ 내 그룹 관리</b> 또는 방에서 <code>.설정</code> → 버튼으로 기능 설정\n\n"
            f"방에서는 <code>{esc(svc.cfg.call_names[0])} …</code> 로 부르면 돼요. "
            "여기(1:1)서는 그냥 말을 걸면 AI와 대화할 수 있어요.")
    # 일반 사용자용 메뉴. 봇 운영자(오너) 등록은 메뉴에 두지 않는다 (서버 로그의 1회용 코드로 /owner 코드)
    rows = [[InlineKeyboardButton("➕ 내 그룹에 소담 추가하기", url=add_to_group_url(bot.username))],
            [B("⚙️ 내 그룹 관리", "m:groups")]]
    owner = user_id in await svc.perms.owners()
    admin = owner or (any(n == ADMIN for *_, n in MAIN_ITEMS) and bool(await admin_groups(svc, bot, user_id)))
    extra = [B(label, f"m:{code}") for _, code, label, need in sorted(MAIN_ITEMS, key=lambda m: m[:2])
             if (await need(svc, bot, user_id) if callable(need) else
                 need == PUBLIC or (need == ADMIN and admin) or (need == OWNER and owner))]
    rows += _chunks(extra, 2)
    rows.append([B("🪪 내 ID", "m:id"), B("❓ 도움말", "m:help")])
    return text, InlineKeyboardMarkup(rows)


# 메인 메뉴에 붙는 버튼 (order, 코드, 글자, PUBLIC|ADMIN(어느 방이든 지금 관리자)|OWNER|async fn(svc, bot, uid) → 보일지).
# 패널 모듈이 추가한다
MAIN_ITEMS: list[tuple[int, str, str, int | Callable]] = []


def register_main(order: int, code: str, label: str, need: int | Callable = PUBLIC) -> None:
    MAIN_ITEMS[:] = [m for m in MAIN_ITEMS if m[1] != code] + [(order, code, label, need)]


ADMIN_GROUPS_TTL = 30   # 사람마다 목록 기억 (메뉴·도움말·복사 화면이 연달아 불러도 텔레그램에 다시 안 물음). 권한은 누를 때마다 따로 확인
ADMIN_GROUPS_PAR = 8    # 방마다 관리자 확인을 동시에 (예전: 하나씩 → 방 72개 3.7초, 감사 S1)


async def admin_groups(svc: Services, bot: Bot, user_id: int) -> list[tuple[int, str]]:
    cache = svc.__dict__.setdefault("_admin_groups", {})
    hit = cache.get(user_id)
    if hit and time.monotonic() - hit[0] < ADMIN_GROUPS_TTL:
        return list(hit[1])
    sem = asyncio.Semaphore(ADMIN_GROUPS_PAR)

    async def one(chat_id: int):
        async with sem:
            try:
                if await svc.perms.is_admin(bot, chat_id, user_id):
                    return chat_id, await chat_title(svc, chat_id)
            except TelegramError:
                pass   # 봇이 나간 방
            return None
    out = [r for r in await asyncio.gather(*(one(c) for c in await svc.perms.candidate_chats(user_id) if c < 0)) if r]
    if len(cache) > 5000:
        cache.clear()
    cache[user_id] = (time.monotonic(), out)
    return list(out)


async def groups_menu(svc: Services, bot: Bot, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = await admin_groups(svc, bot, user_id)
    rows = [[B(f"💬 {title[:30]}", f"m:g:{chat_id}")] for chat_id, title in groups[:20]]
    rows.append([InlineKeyboardButton("➕ 그룹에 추가하기", url=add_to_group_url(bot.username))])
    rows.append([B("⬅️ 처음으로", "m:home")])
    text = ("⚙️ <b>관리할 그룹을 고르세요</b>" if groups else
            "관리 중인 그룹이 없어요.\n봇을 그룹에 추가하고, 그 그룹의 관리자인 계정으로 열어주세요.")
    return text, InlineKeyboardMarkup(rows)


HELP = ("❓ <b>도움말</b>\n\n"
        "<b>그룹에서</b>\n"
        "• 소담아 … — AI에게 말 걸기 (AI 답에 답장·@멘션도 됨)\n"
        "• 사진에 답장하며 '소담아 이거 뭐야' · '소담아 ○○ 그려줘' — 사진 읽기·그림 그리기\n"
        "• 🎰 포인트 게임 !도움 · 🖼 결과표 !그림장 · 🎡 !룰렛 금액 — 버튼 베팅판\n"
        "• .도움말 — 명령어 전체 · .설정 — 이 메뉴를 1:1로 받기\n"
        "• .게임 · .랭킹 · .검색 · .지식 · .예약공지 · .경고/.뮤트/.밴 …\n\n"
        "<b>여기(1:1)에서</b>\n"
        "• 그냥 말하면 AI 대화 · .지식 추가 (오너: 모든 방 공통 자료)\n"
        "• /start — 이 메뉴")


async def r_home(c: PanelCtx) -> Screen:
    return Screen(*await main_menu(c.svc, c.bot, c.uid))


async def r_groups(c: PanelCtx) -> Screen:
    return Screen(*await groups_menu(c.svc, c.bot, c.uid))


async def r_id(c: PanelCtx) -> Screen:
    return Screen(None, toast=f"내 텔레그램 ID: {c.uid}", alert=True)


async def r_help(c: PanelCtx) -> Screen:
    return Screen(HELP, _kb([[B("⬅️ 처음으로", "m:home")]]))


# ── 그룹 화면 ─────────────────────────────────────────────
def _toggle_rows(s: dict, cid: int, keys: list[str]) -> list[list[InlineKeyboardButton]]:
    # 누르면 될 '목표값'을 버튼에 담는다 → 두 번 눌리거나 옛 패널을 눌러도 결과가 같음
    return _chunks([B(("✅ " if s[k] else "❌ ") + LABELS[k], f"m:t:{cid}:{k}:{0 if s[k] else 1}") for k in keys], 2)


def _preset_row(s: dict, cid: int, key: str) -> list[InlineKeyboardButton]:
    return [B(("● " if str(s[key]) == val else "") + label, f"m:n:{cid}:{key}:{val}") for val, label in PRESETS[key]]


def _back(cid: int, to: str = "g") -> list[InlineKeyboardButton]:
    return [B("⬅️ 뒤로", f"m:{to}:{cid}")]


@dataclass(frozen=True)
class HubItem:
    """그룹 허브 버튼. 패널 모듈이 register_hub 로 추가한다."""
    order: int
    code: str
    label: str | Callable[[dict], str]      # 설정(dict) → 글자
    need: int = ADMIN
    when: Callable[[Services], bool] | None = None
    wide: bool = False                      # 한 줄 전체


HUB_ITEMS: list[HubItem] = []


def register_hub(item: HubItem) -> None:
    HUB_ITEMS[:] = sorted([i for i in HUB_ITEMS if i.code != item.code] + [item], key=lambda i: i.order)


async def s_hub(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    lines = [f"⚙️ <b>{esc(await chat_title(svc, cid))}</b> 관리", "무엇을 설정할까요?"]
    visible = [i for i in HUB_ITEMS if (i.when is None or i.when(svc)) and await _allowed(svc, c.bot, cid, c.uid, i.need)]
    # 이용 기간·구독은 텔레그램 관리자·오너에게만 (.봇관리자 로 추가된 사람에겐 안 보임)
    if any(i.code == "sub" for i in visible):
        st = await svc.billing.status(cid)
        lines.append(f"이용: {STATE_LABEL[st.state]}")
    rows, pair = [], []
    for i in visible:
        btn = B(i.label(s) if callable(i.label) else i.label, f"m:{i.code}:{cid}")
        if i.wide:
            rows += [pair, [btn]] if pair else [[btn]]
            pair = []
            continue
        pair.append(btn)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([B("🔄 새로고침", f"m:g:{cid}"), B("⬅️ 그룹 목록", "m:groups")])
    return Screen("\n".join(lines), _kb(rows))


def _style_label(s: dict) -> str:
    style = STYLES.get(s["style"])
    return f"🎭 말투: {style.label if style else s['style']}"


def _billing_on(svc: Services) -> bool:
    return bool(svc.billing and svc.billing.enabled)


for _item in (HubItem(10, "f", "🧩 기능 켜기/끄기", wide=True), HubItem(20, "j", "🚪 입장·인사"),
              HubItem(30, "sec", "🛡️ 보안"), HubItem(80, "st", _style_label, wide=True),
              HubItem(95, "sub", "💳 이용 기간·구독", TG_ADMIN, _billing_on, wide=True)):
    register_hub(_item)


async def group_panel(svc: Services, bot: Bot, chat_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """그룹 허브 화면 (딥링크·.설정 에서 새 메시지로 보낼 때). 호출 전에 관리자 확인할 것."""
    screen = await s_hub(PanelCtx(svc, bot, user_id, chat_id, []))
    return screen.text, screen.kb


async def s_features(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    return Screen("🧩 <b>기능 켜기/끄기</b>\n버튼을 누르면 바로 바뀌어요.",
                  _kb(_toggle_rows(s, c.cid, FEATURE_TOGGLES) + await _extras("f", c) + [_back(c.cid)]))


async def s_join(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    text = ("🚪 <b>입장·인사</b>\n"
            f"캡차 제한시간: {s['captcha_minutes']}분 · 실패 시: {render('captcha_action', s['captcha_action'])}\n"
            "캡차를 통과해야 채팅할 수 있고, 통과하면 인사해요.\n"
            "'최근 만든 계정은 캡차'를 켜두면 입장 캡차가 꺼져 있어도 가입한 지 얼마 안 된 계정(사용자 ID 로 추정)은 캡차를 받아요.")
    rows = _toggle_rows(s, c.cid, JOIN_TOGGLES)
    rows += [_preset_row(s, c.cid, "captcha_minutes"), _preset_row(s, c.cid, "captcha_action"),
             *await _extras("j", c), _back(c.cid)]
    return Screen(text, _kb(rows))


# 다른 패널이 기존 화면의 '⬅️ 뒤로' 위에 버튼 줄을 붙일 때: 화면 코드 → [async fn(c) → 줄 목록]
SCREEN_EXTRAS: dict[str, list[Callable[[PanelCtx], Awaitable[list[list[InlineKeyboardButton]]]]]] = {}


def register_screen_extra(code: str, fn) -> None:
    if fn not in SCREEN_EXTRAS.setdefault(code, []):
        SCREEN_EXTRAS[code].append(fn)


async def _extras(code: str, c: PanelCtx) -> list[list[InlineKeyboardButton]]:
    rows = []
    for fn in SCREEN_EXTRAS.get(code, ()):
        rows += await fn(c)
    return rows


async def s_security(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    words = await svc.db.banned_words(cid)
    newbie = f"{s['newbie_link_hours']}시간" if s["newbie_link_hours"] else "없음"
    text = ("🛡️ <b>보안</b>\n"
            f"도배: {s['flood_seconds']}초에 {s['flood_count']}개 → {human_minutes(s['flood_mute_minutes'])} 뮤트\n"
            f"같은 말 반복: {s['dup_limit']}회면 삭제+경고\n"
            f"신규 입장자 링크 금지: {newbie}\n"
            f"경고 단계: {s['warn_mute_at']}회 뮤트({human_minutes(s['warn_mute_minutes'])}) · {s['warn_ban_at']}회 밴")
    current = next((k for k, (_, v) in FLOOD_PRESETS.items() if all(s[kk] == vv for kk, vv in v.items())), None)
    rows = [[B(("● " if current == k else "") + f"도배 {label}", f"m:fl:{cid}:{k}")
             for k, (label, _) in FLOOD_PRESETS.items()],
            _preset_row(s, cid, "dup_limit")]
    rows += _toggle_rows(s, cid, SEC_TOGGLES)
    rows += [_preset_row(s, cid, "newbie_link_hours"),
             [B(f"🔗 허용 도메인 ({len(s['whitelist_domains'])})", f"m:dom:{cid}"),
              B(f"🚫 금지어 ({len(words)})", f"m:bw:{cid}")],
             [B("⚠️ 경고 단계", f"m:wl:{cid}")],
             *await _extras("sec", c),
             _back(cid)]
    return Screen(text, _kb(rows))


async def s_warn(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    lines = ["⚠️ <b>경고 단계</b>",
             f"경고 {s['warn_mute_at']}회 → {human_minutes(s['warn_mute_minutes'])} 뮤트",
             f"경고 {s['warn_ban_at']}회 → 밴"]
    if s["warn_ban_at"] <= s["warn_mute_at"]:
        lines.append("\n⚠️ 밴 기준이 뮤트 기준보다 낮거나 같아서 뮤트 없이 바로 밴돼요.")
    rows = [_preset_row(s, c.cid, k) for k in ("warn_mute_at", "warn_mute_minutes", "warn_ban_at")]
    rows += await _extras("wl", c)
    rows.append(_back(c.cid, "sec"))
    return Screen("\n".join(lines), _kb(rows))


LIST_TEXT_CHARS = 2500  # 목록 본문 최대 글자 (텔레그램 메시지 4096자 제한 여유)


def _joined(items: list[str]) -> str:
    """'a, b, c' 로 잇되 너무 길면 '… 외 N개'."""
    out, size = [], 0
    for i, w in enumerate(items):
        if size + len(w) + 2 > LIST_TEXT_CHARS:
            return ", ".join(out) + f" … 외 {len(items) - i}개"
        out.append(esc(w))
        size += len(w) + 2
    return ", ".join(out)


async def s_words(c: PanelCtx) -> Screen:
    words = sorted(await c.svc.db.banned_words(c.cid))
    lines = ["🚫 <b>금지어</b>", "이 단어가 들어간 메시지는 지우고 경고해요. (관리자는 제외)"]
    lines.append(_joined(words) if words else "(없음)")
    if words:
        lines.append("\n단어를 누르면 삭제할 수 있어요.")
    if len(words) > LIST_SHOW:
        lines.append(f"(버튼은 앞의 {LIST_SHOW}개만. 나머지는 방에서 <code>.금지어 삭제 단어</code>)")
    btns = [B(f"🗑 {w[:20]}", f"m:k:{_token(c.svc, c.uid, c.cid, 'ask_bw', w, LIST_TOKEN_TTL)}")
            for w in words[:LIST_SHOW]]
    rows = _chunks(btns, 3) + [[B("➕ 금지어 추가", f"m:in:{c.cid}:bw")], _back(c.cid, "sec")]
    return Screen("\n".join(lines), _kb(rows))


async def s_domains(c: PanelCtx) -> Screen:
    domains = list((await c.svc.db.get_settings(c.cid))["whitelist_domains"])
    lines = ["🔗 <b>허용 도메인</b>", "링크 차단이 켜져 있어도 이 도메인(하위 도메인 포함) 링크는 허용해요."]
    lines.append(_joined(domains) if domains else "(없음)")
    if domains:
        lines.append("\n도메인을 누르면 삭제할 수 있어요.")
    btns = [B(f"🗑 {d[:24]}", f"m:k:{_token(c.svc, c.uid, c.cid, 'ask_dom', d, LIST_TOKEN_TTL)}")
            for d in domains[:LIST_SHOW]]
    rows = _chunks(btns, 2) + [[B("➕ 도메인 추가", f"m:in:{c.cid}:dom")], _back(c.cid, "sec")]
    return Screen("\n".join(lines), _kb(rows))


SCREENS: dict[str, Handler] = {"g": s_hub, "f": s_features, "j": s_join, "sec": s_security, "wl": s_warn,
                               "bw": s_words, "dom": s_domains}


def style_menu(chat_id: int, current: str) -> InlineKeyboardMarkup:
    rows = _chunks([B(("● " if s.key == current else "") + s.label, f"m:s:{chat_id}:{s.key}")
                    for s in STYLES.values()], 3)
    rows.append([B("⬅️ 뒤로", f"m:g:{chat_id}")])
    return InlineKeyboardMarkup(rows)


# ── 설정 변경 ─────────────────────────────────────────────
async def _set(c: PanelCtx, key: str, value) -> bool:
    """바뀐 경우에만 저장·기록 (같은 버튼 재전송은 기록도 안 남김)."""
    if (await c.svc.db.get_settings(c.cid))[key] == value:
        return False
    await c.svc.db.set_setting(c.cid, key, value)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"{key}={value}")
    return True


async def r_toggle(c: PanelCtx) -> Screen:
    key = c.arg(0)
    if key not in TOGGLES:
        return Screen(None)
    if c.arg(1) in ("0", "1"):
        value = c.arg(1) == "1"
    else:  # 예전 형식 버튼(목표값 없음)은 뒤집기
        value = not (await c.svc.db.get_settings(c.cid))[key]
    await _set(c, key, value)
    screen = await SCREENS[SCREEN_OF[key]](c)
    screen.toast = f"{LABELS[key]} {'켜짐' if value else '꺼짐'}"
    return screen


async def r_preset(c: PanelCtx) -> Screen:
    key, val = c.arg(0), c.arg(1)
    if key not in PRESETS or val not in {v for v, _ in PRESETS[key]}:
        return Screen(None)
    value = coerce(key, val)
    await _set(c, key, value)
    screen = await SCREENS[SCREEN_OF[key]](c)
    screen.toast = "✅ " + dict(PRESETS[key])[val]
    return screen


async def r_flood(c: PanelCtx) -> Screen:
    if c.arg(0) not in FLOOD_PRESETS:
        return Screen(None)
    label, values = FLOOD_PRESETS[c.arg(0)]
    changed = False
    for k, v in values.items():
        if (await c.svc.db.get_settings(c.cid))[k] != v:
            await c.svc.db.set_setting(c.cid, k, v)
            changed = True
    if changed:
        await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"flood={c.arg(0)}")
    screen = await s_security(c)
    screen.toast = (f"도배 기준: {label} ({values['flood_seconds']}초에 {values['flood_count']}개 → "
                    f"{values['flood_mute_minutes']}분 뮤트)")
    return screen


async def r_style_menu(c: PanelCtx) -> Screen:
    return Screen("🎭 이 그룹에서 봇이 쓸 기본 말투를 고르세요.\n(멤버는 각자 .말투 로 바꿀 수 있어요)",
                  style_menu(c.cid, (await c.svc.db.get_settings(c.cid))["style"]))


async def r_style(c: PanelCtx) -> Screen:
    if c.arg(0) not in STYLES:
        return Screen(None)
    await _set(c, "style", c.arg(0))
    screen = await s_hub(c)
    screen.toast = f"기본 말투: {STYLES[c.arg(0)].label}"
    return screen


async def r_sub(c: PanelCtx) -> Screen:
    if not (c.svc.billing and c.svc.billing.enabled):
        return Screen(None)
    text, kb = await sub_panel(c.svc, c.cid)
    rows = [list(r) for r in kb.inline_keyboard] if kb else []
    rows.append(_back(c.cid))
    return Screen(text, _kb(rows))


# ── 1회용 토큰 (목록 항목 · 삭제 확인) ────────────────────
async def _ask_delete(c: PanelCtx, what: str, item: str, action: str, back: str) -> Screen:
    tok = _token(c.svc, c.uid, c.cid, action, item)
    return Screen(f"{what} <b>{esc(item)}</b>{josa(item)[len(item):]} 삭제할까요?",
                  _kb([[B("🗑 삭제", f"m:k:{tok}"), B("취소", f"m:{back}:{c.cid}")]]))


async def t_ask_bw(c: PanelCtx, word: str) -> Screen:
    return await _ask_delete(c, "🚫 금지어", word, "del_bw", "bw")


async def t_ask_dom(c: PanelCtx, dom: str) -> Screen:
    return await _ask_delete(c, "🔗 허용 도메인", dom, "del_dom", "dom")


async def t_del_bw(c: PanelCtx, word: str) -> Screen:
    await c.svc.db.set_banned_word(c.cid, word, False)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", f"banned_word-={word}")
    screen = await s_words(c)
    screen.toast = "삭제했어요."
    return screen


async def t_del_dom(c: PanelCtx, dom: str) -> Screen:
    domains = [d for d in (await c.svc.db.get_settings(c.cid))["whitelist_domains"] if d != dom]
    await _set(c, "whitelist_domains", domains)
    screen = await s_domains(c)
    screen.toast = "삭제했어요."
    return screen


# 토큰 동작 → (핸들러, 권한을 새로 확인할지)
TOKEN_ACTIONS: dict[str, tuple[Callable[[PanelCtx, object], Awaitable[Screen]], bool]] = {
    "ask_bw": (t_ask_bw, False), "ask_dom": (t_ask_dom, False),
    "del_bw": (t_del_bw, True), "del_dom": (t_del_dom, True),
}
TOKEN_NEED: dict[str, int] = {}  # 기본 ADMIN
EXPIRED = Screen(None, toast="만료된 버튼이에요. 메뉴를 다시 열어주세요.", alert=True)


async def r_token(c: PanelCtx) -> Screen:
    t = c.svc.menu_tokens.get(c.arg(0)) or await _stored_token(c.svc, c.arg(0))   # 재시작 뒤엔 DB 에서
    if t and t.user_id != c.uid and t.expires >= time.time():   # 방에 뜬 카드를 남이 눌러도 토큰은 그대로 (주인이 누를 수 있게)
        return Screen(None, toast="요청한 사람만 누를 수 있어요.", alert=True)
    # 1회용: 메모리에서 꺼냈거나 DB 줄을 지운 쪽만 실행 (재시작 뒤 두 번 빨리 누르면 둘 다 SELECT 로 찾아 두 번 실행됐음)
    mine = c.svc.menu_tokens.pop(c.arg(0), None) is not None
    gone = await c.svc.db.atomic(lambda cn: cn.execute("DELETE FROM menu_tokens WHERE tok=?", (c.arg(0),)).rowcount)
    if not t or not (mine or gone) or t.expires < time.time() or t.action not in TOKEN_ACTIONS:
        return EXPIRED
    fn, fresh = TOKEN_ACTIONS[t.action]
    if not await _allowed(c.svc, c.bot, t.chat_id, c.uid, TOKEN_NEED.get(t.action, ADMIN), fresh):
        return Screen(None, toast="그 그룹의 관리자만 바꿀 수 있어요.", alert=True)
    c.cid = t.chat_id
    return await fn(c, t.arg)


# ── 글자 입력 (금지어·도메인 추가) ────────────────────────
INPUT_PROMPTS = {
    "bw": ("🚫 추가할 <b>금지어</b>를 보내주세요.\n여러 개면 쉼표나 줄바꿈으로 구분 (한 번에 최대 20개)", "bw"),
    "dom": ("🔗 허용할 <b>도메인</b>을 보내주세요. 예: <code>youtube.com</code>\n여러 개면 쉼표로 구분", "dom"),
}


async def r_input(c: PanelCtx) -> Screen:
    kind = c.arg(0)
    if kind not in INPUT_PROMPTS:
        return Screen(None)
    if not await _allowed(c.svc, c.bot, c.cid, c.uid, INPUT_OPTS.get(kind, (False, ADMIN))[1]):
        return Screen(None, toast="권한이 없어요.", alert=True)
    prompt, back = INPUT_PROMPTS[kind]
    c.svc.inputs[c.uid] = PendingInput(kind, c.cid, args=c.args[1:])
    if c.svc.announcer:  # 1:1 에선 입력 흐름 하나만 (예약공지 마법사와 서로 취소)
        c.svc.announcer.drafts.pop((c.uid, c.uid), None)
    return Screen(prompt + "\n\n5분 안에 보내주세요. 그만두려면 <code>취소</code>",
                  _kb([[B("❌ 취소", f"m:{back}:{c.cid}")]]))


def _split_items(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", text) if x.strip()]


async def _add_words(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    text = (msg.text or msg.caption or "").strip()
    items = list(dict.fromkeys(x.lower() for x in _split_items(text) if len(x) <= 50))[:MAX_ITEMS_PER_INPUT]
    if not items:
        return False, "금지어는 1~50자로 보내주세요."
    existing = set(await c.svc.db.banned_words(c.cid))
    new = [w for w in items if w not in existing]
    if len(existing) + len(new) > MAX_WORDS:
        return False, f"금지어는 방당 {MAX_WORDS}개까지예요. 안 쓰는 걸 먼저 지워주세요."
    for w in new:
        await c.svc.db.set_banned_word(c.cid, w, True)
    if new:
        await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "banned_word+=" + ",".join(new))
    return True, f"✅ 금지어 {len(new)}개 추가했어요." if new else "이미 있는 금지어예요."


async def _add_domains(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    text = (msg.text or msg.caption or "").strip()
    raw = _split_items(text)[:MAX_ITEMS_PER_INPUT]
    good = [d for d in (normalize_domain(x) for x in raw) if d]
    if not good:
        return False, "도메인 형식이 아니에요. 예: <code>youtube.com</code>"
    domains = list((await c.svc.db.get_settings(c.cid))["whitelist_domains"])
    merged = sorted(set(domains) | set(good))
    if len(merged) > MAX_DOMAINS:
        return False, f"허용 도메인은 방당 {MAX_DOMAINS}개까지예요."
    await _set(c, "whitelist_domains", merged)
    skipped = len(raw) - len(good)
    return True, f"✅ 도메인 {len(merged) - len(domains)}개 추가했어요." + (f" (형식이 틀린 {skipped}개는 뺐어요)" if skipped else "")


INPUT_HANDLERS: dict[str, tuple[Callable[[PanelCtx, Message], Awaitable[tuple[bool, str]]], Handler]] = {
    "bw": (_add_words, s_words), "dom": (_add_domains, s_domains),
}
INPUT_OPTS: dict[str, tuple[bool, int]] = {}  # kind → (미디어 허용, 권한). 기본 (False, ADMIN)


async def handle_input(svc: Services, bot: Bot, msg: Message) -> bool:
    """1:1 에서 메뉴가 기다리던 글자 입력이면 처리하고 True. 명령어(./)는 그대로 통과."""
    user = msg.from_user
    if not user or msg.chat_id != user.id:
        return False
    p = svc.inputs.get(user.id)
    if not p:
        return False
    text = (msg.text or msg.caption or "").strip()
    if text.startswith((".", "/")) and text not in CANCEL:
        return False
    back_kb = _kb([[B("⬅️ 메뉴로", f"m:{p.kind}:{p.chat_id}")]])
    now = time.time()
    if p.expires < now:
        svc.inputs.pop(user.id, None)
        if now - p.expires > INPUT_GRACE:
            return False  # 한참 지난 뒤의 말은 평범한 대화로
        await msg.reply_text("⌛ 입력 시간(5분)이 지나서 취소됐어요. 메뉴에서 다시 눌러주세요.", reply_markup=back_kb)
        return True
    if text in CANCEL:
        svc.inputs.pop(user.id, None)
        await msg.reply_text("취소했어요.", reply_markup=back_kb)
        return True
    media, need = INPUT_OPTS.get(p.kind, (False, ADMIN))
    # 입력을 기다리는 사이 관리자에서 내려왔을 수도 있으니 다시 확인
    if not await _allowed(svc, bot, p.chat_id, user.id, need):
        svc.inputs.pop(user.id, None)
        await msg.reply_text("그 그룹의 관리자만 바꿀 수 있어요.")
        return True
    if not text and not media:
        await msg.reply_text("글자로 보내주세요. 그만두려면 <code>취소</code>", parse_mode="HTML")
        return True
    add, screen_fn = INPUT_HANDLERS[p.kind]
    c = PanelCtx(svc, bot, user.id, p.chat_id, p.args)
    ok, result = await add(c, msg)
    if not ok:  # 형식 오류: 입력 대기는 유지하고 다시 받기
        p.expires = now + 300
        if hasattr(svc.inputs, "save"):   # DB 에도 새 기한 (sodam/persist.py InputStore)
            svc.inputs.save(user.id)
        await msg.reply_text(result + "\n다시 보내주거나 <code>취소</code>", parse_mode="HTML")
        return True
    svc.inputs.pop(user.id, None)
    screen = await screen_fn(c)
    await send_panel(svc, bot, user.id, lambda: msg.reply_text(result + ("\n\n" + screen.text if screen.text else ""), parse_mode="HTML",
                                                               reply_markup=screen.kb))
    return True


# ── 확장 등록 (sodam/panels/*.py 가 import 될 때 호출) ─────
def register_screen(code: str, fn: Handler, need: int = ADMIN, fresh: bool = False) -> None:
    """방 단위 화면 m:<code>:<방ID>[:인자]. 설정 변경 후 다시 그릴 화면 목록에도 들어간다."""
    SCREENS[code] = fn
    ROUTES[code] = Route(fn, need, fresh=fresh)


def register_route(code: str, route: Route) -> None:
    if code in ROUTES and ROUTES[code] is not route:
        log.debug("route %s overridden", code)
    ROUTES[code] = route


def register_toggle(key: str, screen: str) -> None:
    """설정 키를 목표값 토글(m:t)로 허용. screen = 바꾼 뒤 다시 그릴 화면 코드."""
    if key not in TOGGLES:
        TOGGLES.append(key)
    SCREEN_OF[key] = screen


def register_preset(key: str, options: list[tuple[str, str]], screen: str) -> None:
    """설정 키를 화이트리스트 프리셋(m:n)으로 허용. options = [(저장값 문자열, 버튼 글자)]."""
    PRESETS[key] = options
    SCREEN_OF[key] = screen


def register_input(kind: str, prompt: str, back: str,
                   handler: Callable[[PanelCtx, Message], Awaitable[tuple[bool, str]]],
                   screen: Handler, *, media: bool = False, need: int = ADMIN) -> None:
    """글자 입력 흐름. m:in:<방ID>:<kind> 로 시작. handler(c, msg) → (성공?, 안내문).
    media=True 면 사진·영상만 온 메시지도 handler 로 넘긴다 (인사 편집기 등)."""
    INPUT_PROMPTS[kind] = (prompt, back)
    INPUT_HANDLERS[kind] = (handler, screen)
    INPUT_OPTS[kind] = (media, need)


def register_token_action(action: str, fn: Callable[[PanelCtx, object], Awaitable[Screen]],
                          fresh: bool = False, need: int = ADMIN) -> None:
    TOKEN_ACTIONS[action] = (fn, fresh)
    TOKEN_NEED[action] = need


def token(svc: Services, uid: int, cid: int, action: str, arg, ttl: int = TOKEN_TTL) -> str:
    """패널 모듈용 공개 이름."""
    return _token(svc, uid, cid, action, arg, ttl)


register_schema("""
CREATE TABLE IF NOT EXISTS menu_tokens (
    tok     TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    action  TEXT NOT NULL,
    arg     TEXT NOT NULL,
    expires REAL NOT NULL
);
""", migrate={"menu_tokens": "plain"})


async def lasting_token(svc: Services, uid: int, cid: int, action: str, arg, ttl: int = 600) -> str:
    """방에 올리는 확인 카드(말로 한 예약·알림 규칙)용: DB 에도 적어 봇이 재시작돼도 버튼이 산다. arg 는 JSON 으로."""
    key = _token(svc, uid, cid, action, arg, ttl)
    now = time.time()
    await svc.db._write("DELETE FROM menu_tokens WHERE expires < ?", (now,))
    await svc.db._write("INSERT INTO menu_tokens(tok, user_id, chat_id, action, arg, expires) VALUES(?,?,?,?,?,?)",
                        (key, uid, cid, action, json.dumps(arg, ensure_ascii=False), now + ttl))
    return key


async def _stored_token(svc: Services, key: str) -> MenuToken | None:
    row = await svc.db._one("SELECT * FROM menu_tokens WHERE tok=?", (key,))
    return MenuToken(row["user_id"], row["chat_id"], row["action"], json.loads(row["arg"]), row["expires"]) if row else None


# ── 라우터 ────────────────────────────────────────────────
ROUTES: dict[str, Route] = {
    "home": Route(r_home, PUBLIC, scoped=False),
    "groups": Route(r_groups, PUBLIC, scoped=False),
    "id": Route(r_id, PUBLIC, scoped=False),
    "help": Route(r_help, PUBLIC, scoped=False),
    "k": Route(r_token, PUBLIC, scoped=False),  # 토큰 안의 방으로 권한 확인
    **{code: Route(fn) for code, fn in SCREENS.items()},
    "t": Route(r_toggle), "n": Route(r_preset), "fl": Route(r_flood),
    "st": Route(r_style_menu), "s": Route(r_style),
    "in": Route(r_input),
    "sub": Route(r_sub, TG_ADMIN, fresh=True),
}


async def send_panel(svc: Services, bot: Bot, uid: int, send: Callable[[], Awaitable[object]]) -> None:
    """메뉴를 새 메시지로 보내고, 그 전에 떠 있던 메뉴의 버튼은 없앤다 (1:1 에 메뉴 1개만 살아있게).
    send() 는 실제로 보내는 코루틴 (reply_text / send_message)."""
    sent = await send()
    new_id = getattr(sent, "message_id", None)
    old = svc.panel_msgs.get(uid)
    if new_id is not None:
        svc.panel_msgs[uid] = new_id
    if old is not None and old != new_id:
        try:
            await bot.edit_message_reply_markup(chat_id=uid, message_id=old, reply_markup=None)
        except TelegramError:
            pass  # 이미 지워졌거나 48시간 지난 메시지


async def _show(bot: Bot, q: CallbackQuery, uid: int, screen: Screen, svc: Services | None = None) -> None:
    await q.answer(screen.toast, show_alert=screen.alert)
    if screen.text is None:
        return
    try:
        await q.edit_message_text(screen.text, parse_mode="HTML", reply_markup=screen.kb)
    except BadRequest as e:
        err = str(e).lower()
        if "not modified" in err:
            return
        if "not found" in err or "can't be edited" in err:  # 지워졌거나 오래된 메시지 → 새로 보냄
            send = lambda: bot.send_message(uid, screen.text, parse_mode="HTML", reply_markup=screen.kb)  # noqa: E731
            if svc is not None:
                await send_panel(svc, bot, uid, send)
            else:
                await send()
            return
        raise
    msg_id = getattr(q.message, "message_id", None)
    if svc is not None and msg_id is not None and screen.kb is not None:
        svc.panel_msgs[uid] = msg_id  # 누른 메시지가 지금 살아있는 메뉴


async def on_callback(svc: Services, bot: Bot, q: CallbackQuery, parts: list[str]) -> None:
    # 메뉴는 1:1 전용. 단 방에 올리는 확인 카드(m:k:<토큰> — 말로 한 예약·알림 규칙·방 규칙 저장)는 방에서 누른다
    # (토큰이 만든 사람·방·만료를 확인하고, 실행 때 관리자인지 다시 봄). 예전엔 이것까지 막혀 방 카드 버튼이 안 먹혔음
    room_card = bool(parts) and parts[0] == "k"
    if not q.message or (q.message.chat_id != q.from_user.id and not room_card):
        await q.answer("1:1 채팅에서 열어주세요.", show_alert=True)
        return
    if room_card and q.message.chat_id != q.from_user.id:
        # 방에서 누르는 건 그 방에 올린 카드 토큰만 (lasting_token = DB 에 있음 + 같은 방). 1:1 메뉴 토큰(금지어 삭제 확인·
        # 기간 부여 등)을 콜백 데이터만 바꿔 방 메시지에서 누르면 그 방 봇 글이 결제 화면 등으로 바뀌던 것 (감사 P3)
        key = parts[1] if len(parts) > 1 else ""
        row = await svc.db._one("SELECT chat_id FROM menu_tokens WHERE tok=?", (key,))
        if (row and row["chat_id"] != q.message.chat_id) or (not row and key in svc.menu_tokens):
            await q.answer("이 방의 카드가 아니에요.", show_alert=True)
            return
        if not row:   # 이미 누른(지운) 카드 → 만료 안내
            await q.answer(EXPIRED.toast, show_alert=True)
            return
    uid = q.from_user.id
    if not svc.menu_limiter.allow(("menu", uid), CALLBACK_PER_MIN):
        await q.answer("너무 빨리 누르고 있어요. 잠시 후 다시 눌러주세요.")
        return
    code = parts[0] if parts else "home"
    route = ROUTES.get(code)
    if route is None:
        await q.answer()
        return
    if code != "in":
        svc.inputs.pop(uid, None)  # 다른 버튼을 누르면 글자 입력 대기는 취소
    cid, args = None, parts[1:]
    if not route.scoped and route.need == OWNER and uid not in await svc.perms.owners():
        await q.answer("오너만 쓸 수 있어요.", show_alert=True)
        return
    if not route.scoped and route.need in (ADMIN, TG_ADMIN):  # 방 없는 라우트는 PUBLIC/OWNER 만 (실수 방지)
        raise ValueError(f"unscoped route {code} needs a chat")
    if route.scoped:
        raw = parts[1] if len(parts) > 1 else ""
        if not CID_RE.fullmatch(raw) or not await svc.db.has_chat(int(raw)):
            await q.answer("없는 그룹이에요. 메뉴를 다시 열어주세요.", show_alert=True)
            return
        cid, args = int(raw), parts[2:]
        # 누를 때마다 그 그룹 관리자인지 다시 확인 (강등되면 바로 막힘)
        if not await _allowed(svc, bot, cid, uid, route.need, route.fresh):
            await q.answer("그 그룹의 관리자만 바꿀 수 있어요.", show_alert=True)
            return
    try:
        screen = await route.handler(PanelCtx(svc, bot, uid, cid, args))
    except TelegramError as e:
        log.warning("menu %s failed: %s", code, e)
        screen = Screen(None, toast="텔레그램 연결이 불안정해요. 잠시 후 다시 눌러주세요.", alert=True)
    except Exception:   # DB 잠김 등: 로딩만 돌지 않게 답은 꼭 (감사 B6)
        log.exception("menu %s failed", code)
        screen = Screen(None, toast="잠깐 문제가 생겼어요. 잠시 후 다시 눌러주세요.", alert=True)
    await _show(bot, q, uid, screen, svc)


from . import panels  # noqa: E402,F401  패널 모듈들이 위 register_* 로 화면을 추가한다
