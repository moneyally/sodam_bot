"""🕵️ 이름 기록 (1:1 메인 메뉴): 내 이름·아이디 변경 기록 + 다른 사람 조회."""
from __future__ import annotations

from telegram import Message

from .. import namehist
from ..menu import (PUBLIC, B, PanelCtx, Route, Screen, _kb, _toggle_rows, register_input, register_main, register_route,
                    register_screen_extra, register_toggle)
from ..services import PendingInput

HOW = ("\n\n<b>조회 방법</b>\n"
       "• 🔎 버튼 → @아이디(예전 아이디도 됨)·숫자 ID 보내기\n"
       "• 그 사람 메시지를 여기로 <b>전달</b>하기\n"
       "• 그룹에서 답장하고 <code>.기록</code> · <code>.전체기록</code> · <code>.이름조회</code> · <code>.아이디조회</code>")


def _kb_main():
    return _kb([[B("🔎 다른 사람 조회", "m:nhq")], [B("⬅️ 처음으로", "m:home")]])


async def s_mine(c: PanelCtx) -> Screen:
    text = await namehist.history_text(c.svc.db, c.uid, c.svc.cfg.tz, title="내 이름 기록")
    return Screen(text + HOW, _kb_main())


async def r_query(c: PanelCtx) -> Screen:
    c.svc.inputs[c.uid] = PendingInput("nh", 0)
    if c.svc.announcer:
        c.svc.announcer.drafts.pop((c.uid, c.uid), None)
    return Screen("🔎 조회할 사람의 <b>@아이디</b>(예전 아이디도 돼요)나 <b>숫자 ID</b>를 보내주세요.\n"
                  "그 사람 메시지를 전달해도 돼요.\n\n그만두려면 <code>취소</code>",
                  _kb([[B("❌ 취소", "m:nh")]]))


async def _lookup(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    remote = False
    if getattr(msg, "forward_origin", None) is not None:
        uid, err = namehist.forwarded_user(msg)
        if uid is None:
            return True, err
    else:
        uid = await namehist.resolve(c.svc.db, (msg.text or "").strip())
        if uid is None:   # 기록에 없는 @아이디 → MTProto 도우미 (켜져 있을 때만)
            uid, remote = await namehist.resolve_remote(c.svc, (msg.text or "").strip(), c.uid)
        if uid is None:
            return False, "그 아이디는 기록에 없어요. @아이디 또는 숫자 ID로 보내주세요."
    owner = c.uid in await c.svc.perms.owners()
    if not await namehist.can_view(c.svc.db, c.uid, uid, 0, owner):
        return True, "🔒 볼 수 없는 기록이에요."
    return True, await namehist.history_text(c.svc.db, uid, c.svc.cfg.tz, note=namehist.REMOTE_NOTE if remote else None)


async def s_after(c: PanelCtx) -> Screen:
    return Screen("", _kb_main())


register_main(30, "nh", "🕵️ 이름 기록")
register_route("nh", Route(s_mine, PUBLIC, scoped=False))
register_route("nhq", Route(r_query, PUBLIC, scoped=False))
register_input("nh", "", "nh", _lookup, s_after, media=True, need=PUBLIC)


# 🛡️ 보안 화면에 '이름 변경 알림' 켜기/끄기
register_toggle("name_change_notice", "sec")


async def _sec_row(c: PanelCtx) -> list[list]:
    s = await c.svc.db.get_settings(c.cid)
    return _toggle_rows(s, c.cid, ["name_change_notice"])


register_screen_extra("sec", _sec_row)
