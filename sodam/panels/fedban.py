"""🛡️ 공동 차단 명단 화면 (방 관리자 1:1). 핵심 동작은 sodam/fedban.py.

m:fb:<방ID>              이 방 공동 차단 설정 (끔 / 알림 / 자동 밴 프리셋 m:n)
m:fbl:<방ID>             명단 최근 항목 (이 방이 올린 항목은 '이 방 표시 빼기', 오너는 '완전 삭제' — 1회용 토큰)
m:fbx:<방ID>:<ID>:b|i    관리자 1:1 알림의 [🚫 밴] [무시]
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import fedban, menu
from ..menu import ADMIN, OWNER, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..subscription import chat_title
from ..util import esc, fmt_time, to_int

MODE_PRESETS = [("off", "끔"), ("alert", "🔔 알림"), ("ban", "🚫 자동 밴")]
LIST_N = 10


async def s_fb(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    paid = await c.svc.paid_features(c.cid)
    mode = s.get("fedban_mode", "alert")
    lines = ["🛡️ <b>공동 차단 명단</b>",
             "소담을 쓰는 여러 방이 사기·스팸 계정을 함께 올리는 명단이에요.",
             "명단에 있는 사람이 이 방에 들어오거나 처음 말하면:",
             "• 끔: 아무것도 안 해요",
             "• 🔔 알림: 텔레그램 관리자님들 1:1 로 알려드려요 ([밴] [무시] 버튼)",
             "• 🚫 자동 밴: 바로 내보내고 방에 잠깐 안내해요 (이용 기간 중인 방만)",
             "",
             f"지금: <b>{esc(fedban.MODES.get(mode, mode))}</b>"]
    if mode == "ban" and not paid:
        lines.append("⚠️ 이용 기간이 끝나서 지금은 자동 밴 대신 알림으로 동작해요.")
    lines += ["", f"명단 전체: {await fedban.total(c.svc.db)}명",
              "올리기: 방에서 <code>.공동차단 @아이디 사유</code> (이 방에서도 내보냄"
              + (")" if paid else ", 이용 기간 중인 방만)")]
    rows = [menu._preset_row(s, c.cid, "fedban_mode"),
            [B("📋 명단 최근 항목", f"m:fbl:{c.cid}")],
            menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def s_fbl(c: PanelCtx) -> Screen:
    db = c.svc.db
    owner = c.uid in await c.svc.perms.owners()
    items = await fedban.recent(db, LIST_N)
    lines = [f"📋 <b>공동 차단 명단</b> (최근 {LIST_N}명)", ""]
    btns = []
    for r in items:
        mine = await fedban.room_reported(db, c.cid, r["user_id"])
        lines.append(f"• {esc(r['name'][:30])} (<code>{r['user_id']}</code>) · 방 {r['rooms']}곳 · "
                     f"{fmt_time(r['last_ts'], c.svc.cfg.tz)}\n  사유: {esc((r['reason'] or '')[:60])}"
                     + (" · <i>이 방이 올림</i>" if mine else ""))
        label = r["name"][:16]
        if owner:
            btns.append(B(f"🗑 완전 삭제: {label}",
                          f"m:k:{menu.token(c.svc, c.uid, c.cid, 'fb_ask', r['user_id'], menu.LIST_TOKEN_TTL)}"))
        elif mine:
            btns.append(B(f"↩️ 이 방 표시 빼기: {label}",
                          f"m:k:{menu.token(c.svc, c.uid, c.cid, 'fb_unmark', r['user_id'], menu.LIST_TOKEN_TTL)}"))
    if not items:
        lines.append("(아직 없어요)")
    rows = [[b] for b in btns] + [[B("🔄 새로고침", f"m:fbl:{c.cid}"), B("⬅️ 뒤로", f"m:fb:{c.cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def t_unmark(c: PanelCtx, uid) -> Screen:
    if not await may(c.svc.perms, c.bot, c.cid, c.uid):
        return Screen(None, toast=no_right_text(), alert=True)
    msg = await fedban.remove(c.svc, c.cid, int(uid), c.uid, owner=False)
    screen = await s_fbl(c)
    screen.toast = _plain(msg)
    return screen


async def t_ask_delete(c: PanelCtx, uid) -> Screen:
    info = await fedban.lookup(c.svc.db, int(uid))
    if not info:
        screen = await s_fbl(c)
        screen.toast = "이미 명단에 없어요."
        return screen
    tok = menu.token(c.svc, c.uid, c.cid, "fb_del", int(uid))
    return Screen(f"🗑 <b>{esc(info['name'])}</b>(<code>{uid}</code>)님을 공동 차단 명단에서 완전히 뺄까요?\n"
                  f"올린 방 {info['rooms']}곳의 표시가 모두 사라져요.",
                  menu._kb([[B("🗑 완전 삭제", f"m:k:{tok}"), B("취소", f"m:fbl:{c.cid}")]]))


async def t_delete(c: PanelCtx, uid) -> Screen:
    msg = await fedban.remove(c.svc, c.cid, int(uid), c.uid, owner=True)
    screen = await s_fbl(c)
    screen.toast = _plain(msg)
    return screen


def _plain(text: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text)).split("\n")[0][:190]


async def r_alert(c: PanelCtx) -> Screen:
    """알림 메시지의 [🚫 밴] [무시]. 알림을 보낸 기록(fedban_seen)이 있는 사람만."""
    uid, act = to_int(c.arg(0)), c.arg(1)
    if not uid or act not in ("b", "i") or not await c.svc.db._one(
            "SELECT 1 FROM fedban_seen WHERE chat_id=? AND user_id=?", (c.cid, uid)):
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    title = esc(await chat_title(c.svc, c.cid))
    if act == "i":
        return Screen(f"🛡️ {title}: ID <code>{uid}</code> 알림을 무시했어요.\n"
                      "올린 방이 늘어나면 다시 알려드려요.")
    if not await may(c.svc.perms, c.bot, c.cid, c.uid):
        return Screen(None, toast=no_right_text(), alert=True)
    info = await fedban.lookup(c.svc.db, uid)
    reason = "공동 차단 명단: " + (info["reasons"][0][:100] if info else "관리자 판단")
    try:
        await c.svc.mod.ban(c.bot, c.cid, uid, c.uid, reason)
    except TelegramError as e:
        return Screen(None, toast=f"실패했어요: {e.message[:100]} (봇에게 '사용자 차단' 권한이 있는지 확인해주세요)",
                      alert=True)
    return Screen(f"🚫 {title}에서 ID <code>{uid}</code>님을 내보냈어요.", toast="내보냈어요.")


menu.register_hub(HubItem(57, "fb", "🚷 공동 차단"))
menu.register_screen("fb", s_fb)
menu.register_screen("fbl", s_fbl)
menu.register_preset("fedban_mode", MODE_PRESETS, "fb")
menu.register_route("fbx", Route(r_alert, ADMIN))
menu.register_token_action("fb_unmark", t_unmark, fresh=True)
menu.register_token_action("fb_ask", t_ask_delete, need=OWNER)
menu.register_token_action("fb_del", t_delete, fresh=True, need=OWNER)
