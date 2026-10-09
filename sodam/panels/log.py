"""🗂️ 관리 기록 (m:log:<방ID>[:쪽]). 이 방의 mod_log 를 10개씩 최신순으로."""
from __future__ import annotations

from .. import menu
from ..db import NOT_AUDIT
from ..menu import B, HubItem, PanelCtx, Screen
from ..settings import CHOICES, LABELS, choice_label
from ..styles import STYLES
from ..util import display_name, esc, fmt_time, to_int

PAGE = 10
DETAIL_CHARS = 80

ACTIONS = {
    "setting": "⚙️ 설정", "warn": "⚠️ 경고", "unwarn": "↩️ 경고 취소", "resetwarns": "↩️ 경고 초기화",
    "mute": "🔇 뮤트", "unmute": "🔊 뮤트 해제", "ban": "⛔ 밴", "unban": "↩️ 밴 해제", "kick": "👢 내보내기",
    "lock": "🔒 채팅 잠금", "unlock": "🔓 잠금 해제", "purge": "🧹 메시지 정리",
    "captcha": "🧩 캡차 실패", "captcha_pass": "🧩 캡차 통과",
    "schedule": "🗓️ 예약공지 등록", "schedule_del": "🗓️ 예약공지 삭제",
    "knowledge_add": "📚 자료 추가", "knowledge_del": "📚 자료 삭제",
    "sub_grant": "📅 이용 기간 부여", "rules": "📜 규칙", "bot_admin": "🛠️ 봇 관리자", "free": "🕊️ 자유 멤버", "join_pass": "🚪 가입 확인 통과", "join_decline": "🚪 가입 신청 거절",
    "leave_lock": "🚪 나가서 재입장 막음",
    "auto_reply": "💬 자동 답글 등록", "auto_reply_del": "💬 자동 답글 지움",
}


def _value(key: str, raw: str) -> str:
    if raw in ("True", "False", "[]"):
        return {"True": "켜짐", "False": "꺼짐", "[]": "(없음)"}[raw]
    if key == "style" and raw in STYLES:
        return STYLES[raw].label
    if key in CHOICES:
        return choice_label(key, raw)
    return raw


def describe(action: str, detail: str) -> str:
    """기록 한 줄의 설명 (esc 전 평문)."""
    detail = detail or ""
    if action == "setting":
        if detail.startswith("banned_word+="):
            return "금지어 추가: " + detail.split("=", 1)[1]
        if detail.startswith("banned_word-="):
            return "금지어 삭제: " + detail.split("=", 1)[1]
        if detail.startswith("flood="):
            return "도배 기준: " + {"loose": "느슨", "normal": "보통", "strict": "엄격"}.get(detail[6:], detail[6:])
        key, sep, value = detail.partition("=")
        if sep and key in LABELS:
            return f"{LABELS[key]} → {_value(key, value)}"
    return detail


async def _page(svc, cid: int, page: int) -> tuple[list, int, int, int]:
    total = (await svc.db._one(f"SELECT COUNT(*) AS n FROM mod_log l WHERE chat_id=? AND {NOT_AUDIT}", (cid,)))["n"]
    pages = max(1, -(-total // PAGE))
    page = min(max(page, 0), pages - 1)
    rows = await svc.db._all(
        "SELECT l.*, a.first_name AS a_first, a.last_name AS a_last, a.username AS a_user, "
        "t.first_name AS t_first, t.last_name AS t_last, t.username AS t_user FROM mod_log l "
        "LEFT JOIN users a ON a.user_id=l.actor_id LEFT JOIN users t ON t.user_id=l.target_id "
        f"WHERE l.chat_id=? AND {NOT_AUDIT} ORDER BY l.id DESC LIMIT ? OFFSET ?", (cid, PAGE, page * PAGE))
    return rows, page, pages, total


def _who(uid, first, last, user, bot_name: str) -> str:
    if uid is None:
        return bot_name
    if first or last or user:
        return display_name(first, last, user)
    return f"ID {uid}"


async def s_log(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    rows, page, pages, total = await _page(svc, cid, to_int(c.arg(0)) or 0)
    head = "🗂️ <b>관리 기록</b>" + (f" ({page + 1}/{pages}쪽 · 총 {total}건)" if pages > 1 else "")
    if not rows:
        return Screen(head + "\n\n아직 기록이 없어요. 경고·뮤트·밴·설정 변경이 여기에 남아요.",
                      menu._kb([menu._back(cid)]))
    lines = [head, "최근 것부터 보여줘요.", ""]
    bot_name = f"🤖 {svc.cfg.bot_name}"
    for r in rows:
        actor = _who(r["actor_id"], r["a_first"], r["a_last"], r["a_user"], bot_name)
        line = f"<code>{fmt_time(r['ts'], svc.cfg.tz)}</code> {ACTIONS.get(r['action'], esc(r['action']))} · {esc(actor)}"
        if r["target_id"] is not None:
            line += " → " + esc(_who(r["target_id"], r["t_first"], r["t_last"], r["t_user"], bot_name))
        desc = describe(r["action"], r["detail"])
        if desc:
            desc = desc if len(desc) <= DETAIL_CHARS else desc[:DETAIL_CHARS] + "…"
            line += f"\n    {esc(desc)}"
        lines.append(line)
    nav = []
    if page > 0:
        nav.append(B("◀ 최근 기록", f"m:log:{cid}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("지난 기록 ▶", f"m:log:{cid}:{page + 1}"))
    rows_kb = ([nav] if nav else []) + [[B("🔄 새로고침", f"m:log:{cid}"), *menu._back(cid)]]
    return Screen("\n".join(lines), menu._kb(rows_kb))


menu.register_hub(HubItem(70, "log", "🗂️ 관리 기록"))
menu.register_screen("log", s_log)
