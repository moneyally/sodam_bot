"""🚪 입장·인사 화면에 붙는 '가입 신청 1:1 확인' (sodam/joinreq.py).

m:t:<방ID>:join_verify:<0|1>   켜기/끄기
m:jrl:<방ID>                   승인제 초대링크 만들기 (누르면 가입 신청이 들어오는 링크 → 소담이 1:1 로 확인 후 승인)
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import menu
from ..menu import ADMIN, B, PanelCtx, Route, Screen

menu.register_toggle("join_verify", "j")


async def _rows(c: PanelCtx) -> list[list]:
    s = await c.svc.db.get_settings(c.cid)
    rows = menu._toggle_rows(s, c.cid, ["join_verify"])
    if s["join_verify"]:
        rows.append([B("🔗 승인제 초대링크 만들기", f"m:jrl:{c.cid}")])
    return rows


async def r_link(c: PanelCtx) -> Screen:
    try:
        link = await c.bot.create_chat_invite_link(c.cid, name="소담 가입 확인", creates_join_request=True)
    except TelegramError as e:
        return Screen(None, toast=f"링크를 못 만들었어요: {e.message[:80]} (봇에게 '사용자 초대' 권한이 있는지 확인해주세요)",
                      alert=True)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "승인제 초대링크 생성")
    text = ("🔗 <b>승인제 초대링크</b>\n"
            f"<code>{link.invite_link}</code>\n\n"
            "이 링크로 들어오려는 사람은 '가입 신청'을 하게 되고, 소담이 1:1 로 그림 버튼 확인을 보내요. "
            "맞히면 자동 승인, 틀리거나 시간이 지나면 거절해요.\n"
            "기존 초대링크는 텔레그램 방 설정 → 초대링크에서 폐기하거나 '가입 승인'을 켜 주세요. "
            "(1:1 을 받을 수 없는 사람은 관리자가 직접 승인해요)")
    return Screen(text, menu._kb([[B("⬅️ 뒤로", f"m:j:{c.cid}")]]))


menu.register_screen_extra("j", _rows)
menu.register_route("jrl", Route(r_link, ADMIN, fresh=True))
