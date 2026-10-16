"""🤖 로봇 인증 (가입 신청 1:1 확인, sodam/joinreq.py) — 그룹 허브 버튼 + 🚪 입장·인사 화면 바로가기.

m:jv:<방ID>                    화면 (켜기·방식·자리 수·점검·오늘 숫자)
m:t:<방ID>:join_verify:<0|1>   켜기/끄기
m:n:<방ID>:join_verify_kind:<number|puzzle> · join_verify_digits:<4|5|6>
m:jvt:<방ID>                   🧪 나한테 시험으로 보내기 (맞혀도 승인 안 함)
m:jrl:<방ID>                   승인제 초대링크 만들기 (누르면 가입 신청이 들어오는 링크 → 소담이 1:1 로 확인 후 승인)
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import joinreq, menu
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..util import day_start

menu.register_toggle("join_verify", "jv")
menu.register_preset("join_verify_kind", [("number", "🔢 숫자 입력"), ("puzzle", "🖼 그림 버튼")], "jv")
menu.register_preset("join_verify_digits", [("4", "4자리"), ("5", "5자리"), ("6", "6자리")], "jv")


async def _checks(c: PanelCtx) -> list[str]:
    """텔레그램에서 바로 확인: 방이 가입 신청(승인제)인지 · 소담에게 '사용자 초대' 권한이 있는지."""
    out = []
    try:
        chat = await c.bot.get_chat(c.cid)
        if getattr(chat, "join_by_request", None):
            out.append("✅ 방이 '가입 승인' 방식이에요")
        else:
            out.append("⚠️ 방이 '가입 승인' 방식이 아니에요 → 텔레그램 방 설정 → 그룹 유형/초대 링크에서 "
                       "'가입 승인(Approve new members)'을 켜거나, 아래 🔗 승인제 초대링크를 쓰세요")
    except TelegramError:
        pass
    try:
        me = await c.bot.get_chat_member(c.cid, c.bot.id)
        if getattr(me, "status", "") == "creator" or getattr(me, "can_invite_users", False):
            out.append("✅ 소담에게 '사용자 초대' 권한이 있어요")
        else:
            out.append("⚠️ 소담에게 <b>'사용자 초대(Invite users)'</b> 권한이 없어요 → 관리자 목록에서 소담 → 권한 켜기. "
                       "<b>없으면 가입 신청이 소담에게 안 와요</b>")
    except TelegramError:
        pass
    return out


async def s_verify(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    since = day_start(c.svc.cfg.tz)
    counts = {r["action"]: r["n"] for r in await c.svc.db._all(
        "SELECT action, COUNT(*) AS n FROM mod_log WHERE chat_id=? AND ts>=? AND action IN ('join_pass','join_decline') "
        "GROUP BY action", (c.cid, since))}
    number = s["join_verify_kind"] == "number"
    how = (f"🔢 숫자 입력 — 비뚤게 그린 숫자 {s['join_verify_digits']}자리를 버튼으로 누르거나 채팅으로 보내기"
           if number else "🖼 그림 버튼 — '강아지' 같은 그림 6개 중 하나 누르기")
    lines = ["🤖 <b>로봇 인증 (가입 신청)</b>",
             "가입 신청이 오면 소담이 신청한 사람 1:1 로 확인 문제를 보내고, 맞히면 바로 승인해요. "
             "3번 틀리거나 시간이 지나면 거절해요.",
             "",
             f"상태: {'✅ 켜짐' if s['join_verify'] else '❌ 꺼짐'}",
             f"방식: {how}",
             f"제한 시간: {s['captcha_minutes']}분 (🚪 입장·인사의 캡차 시간과 같음)",
             f"오늘: 통과 {counts.get('join_pass', 0)} · 거절 {counts.get('join_decline', 0)}",
             "", *await _checks(c),
             "",
             "통과한 사람은 들어온 뒤 방 캡차·대량 입장 내보내기를 건너뛰어요. "
             "1:1 을 받을 수 없는 사람은 그대로 두니 관리자가 직접 승인해 주세요."]
    rows = menu._toggle_rows(s, c.cid, ["join_verify"])
    rows += [menu._preset_row(s, c.cid, "join_verify_kind")]
    if number:
        rows.append(menu._preset_row(s, c.cid, "join_verify_digits"))
    rows += [[B("🧪 나한테 시험으로 받아보기", f"m:jvt:{c.cid}")],
             [B("🔗 승인제 초대링크 만들기", f"m:jrl:{c.cid}")],
             menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_test(c: PanelCtx) -> Screen:
    """누른 관리자에게 실제 신청자가 받는 것과 같은 문제를 보냄 (맞혀도 승인 안 함)."""
    s = await c.svc.db.get_settings(c.cid)
    from ..subscription import chat_title
    if s["join_verify_kind"] == "number":
        ok = await joinreq.send_number(c.svc, c.bot, c.cid, c.uid, c.uid, await chat_title(c.svc, c.cid), s,
                                       s["captcha_minutes"], test=True)
        return Screen(None, toast="1:1 에 숫자 인증을 보냈어요. 아래 메시지에서 해 보세요." if ok else "1:1 로 못 보냈어요.",
                      alert=not ok)
    return Screen(None, toast="그림 버튼 방식은 시험 보내기가 없어요. 숫자 입력으로 바꾸면 해 볼 수 있어요.", alert=True)


async def r_link(c: PanelCtx) -> Screen:
    try:
        link = await c.bot.create_chat_invite_link(c.cid, name="소담 가입 확인", creates_join_request=True)
    except TelegramError as e:
        return Screen(None, toast=f"링크를 못 만들었어요: {e.message[:80]} (봇에게 '사용자 초대' 권한이 있는지 확인해주세요)",
                      alert=True)
    await c.svc.db.log_mod(c.cid, c.uid, None, "setting", "승인제 초대링크 생성")
    text = ("🔗 <b>승인제 초대링크</b>\n"
            f"<code>{link.invite_link}</code>\n\n"
            "이 링크로 들어오려는 사람은 '가입 신청'을 하게 되고, 소담이 1:1 로 로봇 인증을 보내요. "
            "맞히면 자동 승인, 틀리거나 시간이 지나면 거절해요.\n"
            "기존 초대링크는 텔레그램 방 설정 → 초대링크에서 폐기하거나 '가입 승인'을 켜 주세요. "
            "(1:1 을 받을 수 없는 사람은 관리자가 직접 승인해요)")
    return Screen(text, menu._kb([[B("⬅️ 뒤로", f"m:jv:{c.cid}")]]))


async def _join_rows(c: PanelCtx) -> list[list]:
    """🚪 입장·인사 화면 → 로봇 인증 화면 바로가기."""
    s = await c.svc.db.get_settings(c.cid)
    return [[B(f"🤖 로봇 인증 (가입 신청) {'✅' if s['join_verify'] else '❌'}", f"m:jv:{c.cid}")]]


menu.register_screen("jv", s_verify)
menu.register_hub(HubItem(25, "jv", lambda s: f"🤖 로봇 인증 {'✅' if s['join_verify'] else '❌'}"))
menu.register_screen_extra("j", _join_rows)
menu.register_route("jvt", Route(r_test, ADMIN))
menu.register_route("jrl", Route(r_link, ADMIN, fresh=True))
