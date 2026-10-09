"""🚪 나간 사람 재입장 막기 화면 (그룹 허브). 동작은 sodam/leavelock.py.

m:llk:<방>          켜기/끄기 · 기간 버튼 · 마지막 실패
m:in:<방>:llkh      기간 직접 입력 ('3일'·'12시간'·'영구')
"""
from __future__ import annotations

from datetime import datetime

from telegram import Message

from .. import leavelock, menu
from ..menu import B, HubItem, PanelCtx, Screen
from ..util import esc
from .greet import _save

menu.register_preset("leave_lock", [("on", "✅ 켜기"), ("off", "❌ 끄기")], "llk")
menu.register_preset("leave_lock_hours", [("1", "1시간"), ("24", "1일"), ("168", "7일"), ("720", "30일"), ("0", "영구")], "llk")


async def s_llk(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    on = s["leave_lock"] == "on"
    lines = ["🚪 <b>나간 사람 재입장 막기</b>",
             "켜면 <b>스스로 나간 사람</b>을 나간 순간 밴해서, 정한 기간 동안 링크로도 못 들어와요.",
             "관리자가 내보낸 사람·자유 멤버(.free)는 상관없어요. 중간에 풀어 주려면 <code>.밴해제 @아이디</code>.", "",
             f"지금: <b>{'✅ 켜짐' if on else '❌ 꺼짐'}</b> · 기간: <b>{leavelock.period_label(s['leave_lock_hours'])}</b>"]
    fail = await c.svc.db.get_state(c.cid, leavelock.FAIL_KEY)
    if on and isinstance(fail, dict) and fail.get("ts"):
        when = datetime.fromtimestamp(int(fail["ts"]), c.svc.cfg.tz).strftime("%m/%d %H:%M")
        lines.append(f"⚠️ 마지막으로 못 막음 ({when}): 소담에게 <b>'사용자 차단' 권한</b>이 있는지 확인해 주세요.\n"
                     f"<i>{esc(str(fail.get('why') or ''))}</i>")
    rows = [menu._preset_row(s, c.cid, "leave_lock"),
            menu._preset_row(s, c.cid, "leave_lock_hours"),
            [B("✏️ 기간 직접 입력", f"m:in:{c.cid}:llkh")],
            menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def in_hours(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    try:
        hours = leavelock.parse_period(msg.text or "")
    except ValueError as e:
        return False, f"❌ {e}"
    await _save(c, f"leave_lock_hours={hours}", leave_lock_hours=hours)
    return True, f"✅ 재입장 막는 기간을 <b>{leavelock.period_label(hours)}</b>로 정했어요."


menu.register_hub(HubItem(24, "llk", "🚪 나간 사람 재입장 막기"))
menu.register_screen("llk", s_llk)
menu.register_screen("llkh", s_llk)
menu.register_input("llkh", (
    "🚪 나간 사람을 <b>얼마 동안</b> 못 들어오게 할까요?\n"
    "<code>3일</code> · <code>12시간</code> · <code>영구</code> (최대 365일)"), "llk", in_hours, s_llk)
