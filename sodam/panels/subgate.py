"""📢 채널 구독 필수 화면 (그룹 허브). 동작은 sodam/subgate.py.

m:sgt:<방>        켜기/끄기 · 채널 · 소담이 그 채널 관리자인지
m:in:<방>:sgc     채널 입력 (@아이디 · t.me/아이디 · -100… ID)
"""
from __future__ import annotations

from telegram import Message

from .. import menu, subgate
from ..menu import B, HubItem, PanelCtx, Screen
from ..util import esc
from .greet import _save

menu.register_preset("subgate_mode", [("on", "✅ 켜기"), ("off", "❌ 끄기")], "sgt")


async def s_sgt(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    ch = s["subgate_channel"]
    lines = ["📢 <b>채널 구독 필수</b>",
             "켜면 채널을 구독 안 한 사람은 <b>채팅 금지</b> + 방에 [📢 채널 들어가기][✅ 구독 확인] 안내.",
             "구독하고 [구독 확인]을 누르면 소담이 실제로 확인해서 풀어줘요. 관리자·자유 멤버는 제외.", "",
             f"지금: <b>{'✅ 켜짐' if s['subgate_mode'] == 'on' else '❌ 꺼짐'}</b>",
             f"채널: <b>{esc(ch)}</b>" if ch else "채널: 아직 없음 — 먼저 채널을 정해 주세요"]
    if ch:
        ok = await subgate.subscribed(c.bot, ch, c.bot.id)
        lines.append("✅ 소담이 채널 관리자라 구독 여부를 확인할 수 있어요." if ok else
                     "⚠️ 소담이 그 채널 <b>관리자</b>가 아니라 구독 여부를 못 봐요 — 채널에 소담을 관리자로 넣어 주세요. "
                     "(그 전엔 아무도 막지 않아요)")
    rows = [menu._preset_row(s, c.cid, "subgate_mode"),
            [B("📢 채널 바꾸기" if ch else "📢 채널 정하기", f"m:in:{c.cid}:sgc")],
            menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def in_channel(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    try:
        ch = subgate._channel(msg.text or "")
    except ValueError as e:
        return False, f"❌ {e}"
    if not ch:
        return False, "채널 @아이디를 보내 주세요."
    await _save(c, f"subgate_channel={ch}", subgate_channel=ch)
    ok = await subgate.subscribed(c.bot, ch, c.bot.id)
    return True, f"✅ 채널을 {esc(ch)} 로 정했어요." + ("" if ok else "\n⚠️ 소담을 그 채널 관리자로 넣어야 구독 확인이 돼요.")


menu.register_hub(HubItem(23, "sgt", "📢 채널 가입 필수"))
menu.register_screen("sgt", s_sgt)
menu.register_screen("sgc", s_sgt)
menu.register_input("sgc", (
    "📢 구독해야 하는 <b>채널</b>을 보내 주세요.\n"
    "<code>@채널아이디</code> · <code>https://t.me/채널아이디</code> · 비공개 채널은 <code>-100…</code> 숫자 ID\n"
    "소담이 그 채널의 관리자여야 구독 여부를 확인할 수 있어요."), "sgt", in_channel, s_sgt)
