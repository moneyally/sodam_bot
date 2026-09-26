"""버튼 메뉴 (그룹헬프·상마타 방식). 1:1 채팅에서만 열리고, 누를 때마다 권한을 다시 확인한다.
그룹 관리자는 텔레그램 관리자 여부로 자동 인식 (코드 불필요).

메인(/start) → [➕ 그룹에 추가] [⚙️ 내 그룹 관리] [🪪 내 ID] [❓ 도움말]
내 그룹 관리 → 방 선택 → 기능 켜기/끄기 · 말투 · 도배 기준 · 구독
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from .settings import LABELS
from .styles import STYLES
from .subscription import STATE_LABEL, chat_title, panel as sub_panel
from .util import esc, iyeyo, to_int

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

# 봇을 추가할 때 미리 체크해 둘 관리자 권한 (텔레그램이 추가 화면에서 보여줌)
ADMIN_RIGHTS = "delete_messages+restrict_members+pin_messages+invite_users"
TOGGLES = ["ai_enabled", "greet_enabled", "captcha_enabled", "cas_enabled", "link_filter",
           "injection_guard", "impersonation_guard", "games_enabled", "sports_enabled", "daily_report",
           "delete_join_message"]
FLOOD_PRESETS = {
    "loose": ("느슨", {"flood_count": 10, "flood_seconds": 8, "flood_mute_minutes": 10}),
    "normal": ("보통", {"flood_count": 6, "flood_seconds": 8, "flood_mute_minutes": 30}),
    "strict": ("엄격", {"flood_count": 4, "flood_seconds": 8, "flood_mute_minutes": 60}),
}


def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def add_to_group_url(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?startgroup=true&admin={ADMIN_RIGHTS}"


async def main_menu(svc: Services, bot: Bot, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    name = svc.cfg.bot_name
    text = (f"👋 안녕하세요, 소통방 AI 비서 <b>{iyeyo(name)}</b>\n\n"
            "<b>이런 걸 해드려요</b>\n"
            "• 🛡️ 방 관리: 입장 캡차, 도배·링크·사칭 차단, 경고·뮤트\n"
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
            [B("⚙️ 내 그룹 관리", "m:groups")],
            [B("🪪 내 ID", "m:id"), B("❓ 도움말", "m:help")]]
    return text, InlineKeyboardMarkup(rows)


async def admin_groups(svc: Services, bot: Bot, user_id: int) -> list[tuple[int, str]]:
    out = []
    for chat_id in await svc.db.all_chat_ids():
        if chat_id >= 0:
            continue
        try:
            if await svc.perms.is_admin(bot, chat_id, user_id):
                out.append((chat_id, await chat_title(svc, chat_id)))
        except TelegramError:
            continue  # 봇이 나간 방
    return out


async def groups_menu(svc: Services, bot: Bot, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = await admin_groups(svc, bot, user_id)
    rows = [[B(f"💬 {title[:30]}", f"m:g:{chat_id}")] for chat_id, title in groups[:20]]
    rows.append([InlineKeyboardButton("➕ 그룹에 추가하기", url=add_to_group_url(bot.username))])
    rows.append([B("⬅️ 처음으로", "m:home")])
    text = ("⚙️ <b>관리할 그룹을 고르세요</b>" if groups else
            "관리 중인 그룹이 없어요.\n봇을 그룹에 추가하고, 그 그룹의 관리자인 계정으로 열어주세요.")
    return text, InlineKeyboardMarkup(rows)


async def group_panel(svc: Services, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    s = await svc.db.get_settings(chat_id)
    title = esc(await chat_title(svc, chat_id))
    lines = [f"⚙️ <b>{title}</b> 설정", "버튼을 누르면 바로 켜지고 꺼져요."]
    if svc.billing and svc.billing.enabled:
        st = await svc.billing.status(chat_id)
        lines.append(f"이용: {STATE_LABEL[st.state]}")
    rows, pair = [], []
    for key in TOGGLES:
        # 누르면 될 '목표값'을 버튼에 담는다 → 두 번 눌리거나 옛 패널을 눌러도 결과가 같음
        pair.append(B(("✅ " if s[key] else "❌ ") + LABELS[key], f"m:t:{chat_id}:{key}:{0 if s[key] else 1}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    style = STYLES.get(s["style"])
    rows.append([B(f"🎭 말투: {style.label if style else s['style']}", f"m:st:{chat_id}")])
    current = next((k for k, (_, v) in FLOOD_PRESETS.items()
                    if all(s[kk] == vv for kk, vv in v.items())), None)
    rows.append([B(("● " if current == k else "") + f"도배 {label}", f"m:fl:{chat_id}:{k}")
                 for k, (label, _) in FLOOD_PRESETS.items()])
    if svc.billing and svc.billing.enabled:
        rows.append([B("💳 이용 기간·구독", f"m:sub:{chat_id}")])
    rows.append([B("🔄 새로고침", f"m:g:{chat_id}"), B("⬅️ 그룹 목록", "m:groups")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def style_menu(chat_id: int, current: str) -> InlineKeyboardMarkup:
    rows, pair = [], []
    for s in STYLES.values():
        pair.append(B(("● " if s.key == current else "") + s.label, f"m:s:{chat_id}:{s.key}"))
        if len(pair) == 3:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([B("⬅️ 뒤로", f"m:g:{chat_id}")])
    return InlineKeyboardMarkup(rows)


HELP = ("❓ <b>도움말</b>\n\n"
        "<b>그룹에서</b>\n"
        "• 소담아 … — AI에게 말 걸기 (답장·@멘션도 됨)\n"
        "• .도움말 — 명령어 전체 · .설정 — 이 메뉴를 1:1로 받기\n"
        "• .게임 · .랭킹 · .검색 · .지식 · .예약공지 · .경고/.뮤트/.밴 …\n\n"
        "<b>여기(1:1)에서</b>\n"
        "• 그냥 말하면 AI 대화 · .지식 추가 (오너: 모든 방 공통 자료)\n"
        "• /start — 이 메뉴")


async def _edit(q: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_callback(svc: Services, bot: Bot, q: CallbackQuery, parts: list[str]) -> None:
    if not q.message or q.message.chat_id != q.from_user.id:
        await q.answer("1:1 채팅에서 열어주세요.", show_alert=True)
        return
    uid = q.from_user.id
    action = parts[0] if parts else "home"

    if action == "home":
        await q.answer()
        await _edit(q, *await main_menu(svc, bot, uid))
        return
    if action == "groups":
        await q.answer()
        await _edit(q, *await groups_menu(svc, bot, uid))
        return
    if action == "id":
        await q.answer(f"내 텔레그램 ID: {uid}", show_alert=True)
        return
    if action == "help":
        await q.answer()
        await _edit(q, HELP, InlineKeyboardMarkup([[B("⬅️ 처음으로", "m:home")]]))
        return
    # 여기부터는 특정 그룹 설정: 누를 때마다 그 그룹 관리자인지 다시 확인
    chat_id = to_int(parts[1]) if len(parts) > 1 else None
    if chat_id is None or chat_id >= 0:
        await q.answer()
        return
    try:
        is_admin = await svc.perms.is_admin(bot, chat_id, uid)
    except TelegramError:
        is_admin = False
    if not is_admin:
        await q.answer("그 그룹의 관리자만 바꿀 수 있어요.", show_alert=True)
        return

    if action == "g":
        await q.answer()
        await _edit(q, *await group_panel(svc, chat_id))
    elif action == "t" and len(parts) > 2 and parts[2] in TOGGLES:
        key = parts[2]
        if len(parts) > 3 and parts[3] in ("0", "1"):
            value = parts[3] == "1"
        else:  # 예전 형식 버튼(목표값 없음)은 뒤집기
            value = not (await svc.db.get_settings(chat_id))[key]
        await svc.db.set_setting(chat_id, key, value)
        await svc.db.log_mod(chat_id, uid, None, "setting", f"{key}={value}")
        await q.answer(f"{LABELS[key]} {'켜짐' if value else '꺼짐'}")
        await _edit(q, *await group_panel(svc, chat_id))
    elif action == "st":
        await q.answer()
        await _edit(q, "🎭 이 그룹에서 봇이 쓸 기본 말투를 고르세요.\n(멤버는 각자 .말투 로 바꿀 수 있어요)",
                    style_menu(chat_id, (await svc.db.get_settings(chat_id))["style"]))
    elif action == "s" and len(parts) > 2 and parts[2] in STYLES:
        await svc.db.set_setting(chat_id, "style", parts[2])
        await svc.db.log_mod(chat_id, uid, None, "setting", f"style={parts[2]}")
        await q.answer(f"기본 말투: {STYLES[parts[2]].label}")
        await _edit(q, *await group_panel(svc, chat_id))
    elif action == "fl" and len(parts) > 2 and parts[2] in FLOOD_PRESETS:
        label, values = FLOOD_PRESETS[parts[2]]
        for k, v in values.items():
            await svc.db.set_setting(chat_id, k, v)
        await svc.db.log_mod(chat_id, uid, None, "setting", f"flood={parts[2]}")
        await q.answer(f"도배 기준: {label} ({values['flood_seconds']}초에 {values['flood_count']}개 → "
                       f"{values['flood_mute_minutes']}분 뮤트)")
        await _edit(q, *await group_panel(svc, chat_id))
    elif action == "sub" and svc.billing and svc.billing.enabled:
        await q.answer()
        text, kb = await sub_panel(svc, chat_id)
        rows = list(kb.inline_keyboard) if kb else []
        rows.append((B("⬅️ 뒤로", f"m:g:{chat_id}"),))
        await _edit(q, text, InlineKeyboardMarkup(rows))
    else:
        await q.answer()
