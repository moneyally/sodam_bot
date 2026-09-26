"""🕵️ 이름 기록 (1:1 메인 메뉴): 내 이름·아이디 변경 기록 + 다른 사람 조회 (SangMata 방식)."""
from __future__ import annotations

from telegram import Message

from .. import namehist
from ..menu import (PUBLIC, B, PanelCtx, Route, Screen, _kb, _toggle_rows, register_input, register_main, register_route,
                    register_screen_extra, register_toggle)
from ..services import PendingInput
from ..util import to_int


def _kb_main():
    return _kb([[B("🔎 다른 사람 조회", "m:nhq")], [B("⬅️ 처음으로", "m:home")]])


async def s_mine(c: PanelCtx) -> Screen:
    text = await namehist.history_text(c.svc.db, c.uid, c.svc.cfg.tz, title="내 이름 기록")
    return Screen(text + "\n\n그룹에서는 <code>.이름기록 @아이디</code> 나 답장으로 다른 멤버 기록을 볼 수 있어요.",
                  _kb_main())


async def r_query(c: PanelCtx) -> Screen:
    c.svc.inputs[c.uid] = PendingInput("nh", 0)
    if c.svc.announcer:
        c.svc.announcer.drafts.pop((c.uid, c.uid), None)
    return Screen("🔎 조회할 사람의 <b>@아이디</b>(예전 아이디도 돼요) 또는 <b>숫자 ID</b>를 보내주세요.\n"
                  "나와 같은 그룹에 있는 사람만 볼 수 있어요.\n\n그만두려면 <code>취소</code>",
                  _kb([[B("❌ 취소", "m:nh")]]))


async def resolve(db, raw: str) -> int | None:
    raw = raw.strip()
    uid = to_int(raw)
    if uid is not None:
        return uid if uid > 0 else None
    name = raw.lstrip("@")
    if not name or " " in name:
        return None
    row = await db._one("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (name,))
    return row["user_id"] if row else await namehist.find_by_old_username(db, None, name)


async def _lookup(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    uid = await resolve(c.svc.db, raw)
    if uid is None:
        return False, "그 아이디는 기록에 없어요. @아이디 또는 숫자 ID로 보내주세요."
    owner = c.uid in await c.svc.perms.owners()
    if uid != c.uid and not owner and not await namehist.shares_group(c.svc.db, c.uid, uid):
        return True, "🔒 나와 같은 그룹에 있는 사람만 조회할 수 있어요."
    return True, await namehist.history_text(c.svc.db, uid, c.svc.cfg.tz)


async def s_after(c: PanelCtx) -> Screen:
    return Screen("", _kb_main())


register_main(30, "nh", "🕵️ 이름 기록")
register_route("nh", Route(s_mine, PUBLIC, scoped=False))
register_route("nhq", Route(r_query, PUBLIC, scoped=False))
register_input("nh", "", "nh", _lookup, s_after, need=PUBLIC)


# 🛡️ 보안 화면에 '이름 변경 알림' 켜기/끄기
register_toggle("name_change_notice", "sec")


async def _sec_row(c: PanelCtx) -> list[list]:
    s = await c.svc.db.get_settings(c.cid)
    return _toggle_rows(s, c.cid, ["name_change_notice"])


register_screen_extra("sec", _sec_row)
