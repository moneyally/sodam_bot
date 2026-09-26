"""🔔 태그 알림 화면 (메인 메뉴). 누구나 열 수 있고, 바꾸는 건 누른 사람 자신의 그룹별 알림 설정뿐.

m:tn                     그룹 목록 (그룹마다 ✅/❌ 목표값 버튼)
m:tn:<0|1>:<방ID>        그 그룹 알림 끄기/켜기 → 목록 다시 그림
m:tn:0:<방ID>:dm         1:1 알림 메시지의 [🔕 이 그룹 알림 끄기] → 알림 글은 그대로 두고 토스트만
방 ID 를 세 번째 칸이 아니라 뒤에 두는 건 방 단위(관리자) 버튼이 아니라서. 그래도 형식·DB 존재는 확인한다.
방 관리자는 🧩 기능 화면의 '태그·답장 알림' 으로 방 전체를 끌 수 있다.
"""
from __future__ import annotations

from ..menu import (CID_RE, FEATURE_TOGGLES, PUBLIC, B, PanelCtx, Route, Screen, _kb, register_main,
                    register_route, register_toggle)
from ..subscription import chat_title
from ..tagnotify import mark_started, set_opt_out, user_groups
from ..util import esc

register_toggle("tag_notify", "f")
if "tag_notify" not in FEATURE_TOGGLES:  # 🧩 기능 켜기/끄기 화면에 버튼 추가
    FEATURE_TOGGLES.append("tag_notify")
register_main(10, "tn", "🔔 태그 알림", PUBLIC)

INTRO = ("🔔 <b>태그 알림</b>\n"
         "그룹에서 누가 나를 @태그하거나 내 메시지에 답장하면 여기(1:1)로 알려드려요.\n"
         "✅ 알림 받는 중 · ❌ 꺼짐 — 그룹을 누르면 바뀌어요.")


async def screen(c: PanelCtx) -> Screen:
    groups = await user_groups(c.svc.db, c.uid)
    lines, rows, room_off = [INTRO], [], []
    for g in groups:
        title = g["title"] or str(g["chat_id"])
        if not (await c.svc.db.get_settings(g["chat_id"])).get("tag_notify", True):
            room_off.append(title)
        on = not g["off"]
        rows.append([B(("✅ " if on else "❌ ") + title[:30], f"m:tn:{0 if on else 1}:{g['chat_id']}")])
    if not groups:
        lines.append("\n아직 소담이 있는 그룹에서 대화한 적이 없어요. 그룹에서 한 번 말하면 여기에 나와요.")
    if room_off:
        lines.append("\n⚠️ 관리자가 방 전체 태그 알림을 꺼둬서 알림이 안 오는 그룹: " + ", ".join(esc(t) for t in room_off))
    rows.append([B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines), _kb(rows))


async def r_tagnotify(c: PanelCtx) -> Screen:
    await mark_started(c.svc.db, c.uid)  # 1:1 에서 눌렀다 = 알림을 받을 수 있음 (예전 차단 표시도 풀림)
    if not c.args:
        return await screen(c)
    val, raw = c.arg(0), c.arg(1)
    if val not in ("0", "1") or not CID_RE.fullmatch(raw) or not await c.svc.db.has_chat(int(raw)):
        return Screen(None, toast="없는 그룹이에요. 메뉴를 다시 열어주세요.", alert=True)
    cid, on = int(raw), val == "1"
    await set_opt_out(c.svc.db, c.uid, cid, not on)
    title = (await chat_title(c.svc, cid))[:30]
    if c.arg(2) == "dm":
        return Screen(None, alert=True,
                      toast=f"🔕 {title} 알림을 껐어요.\n/start → 🔔 태그 알림에서 다시 켤 수 있어요.")
    s = await screen(c)
    s.toast = f"🔔 {title} 알림 켜짐" if on else f"🔕 {title} 알림 꺼짐"
    return s


register_route("tn", Route(r_tagnotify, PUBLIC, scoped=False))
