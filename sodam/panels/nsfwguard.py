"""🔞 음란물 자동 차단 화면 (방 관리자 1:1). 감지·삭제는 sodam/nsfwguard.py.

m:ns:<방ID>                   끄기 / 지우고 알림 / 알림만 (프리셋) + 설명·최근 처리 수
m:nsx:<방ID>:<알림ID>:d|b|m|t 관리자 1:1 알림의 [🗑 지우기] [🚫 밴] [🔇 뮤트 1일] [✅ 오탐(이 조각 통과)]
"""
from __future__ import annotations

import time

from telegram.error import TelegramError

from .. import menu, nsfwguard
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..subscription import chat_title
from ..util import esc, to_int

menu.register_preset("nsfw_mode", list(nsfwguard.MODES.items()), "ns")


async def s_ns(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    mode = str(s.get("nsfw_mode", "off"))
    row = await svc.db._one(
        "SELECT COUNT(*) AS n FROM nsfw_alerts WHERE chat_id=? AND ts>?", (cid, int(time.time()) - 86400))
    today = row["n"] if row else 0
    lines = ["🔞 <b>음란물 자동 차단</b>",
             "스티커·사진·GIF·영상이 올라오면 바로 검사해서 음란물이면 "
             "지우거나 관리자님께 알려드려요. <b>관리자·일반 멤버 모두 검사</b>하고, 오너(운영자)만 빼요.",
             "같은 음란 스티커를 도배하면 두 번째부터는 즉시 지워요.", "",
             f"지금: <b>{esc(nsfwguard.MODES.get(mode, mode))}</b>",
             f"최근 24시간 처리: {today}건", "",
             "• <b>지우고 알림</b> = 음란물로 판단되면 봇이 바로 지우고 관리자님께 1:1 알림 "
             "(봇에게 '메시지 삭제' 권한이 있어야 지워요)",
             "• <b>알림만</b> = 안 지우고 관리자님께만 알림 → 알림의 버튼으로 직접 처리",
             "• 오탐이면 알림의 [✅ 오탐] 으로 그 조각을 이 방에서 통과시켜요. 자동 밴은 안 해요."]
    rows = [menu._preset_row(s, cid, "nsfw_mode"), menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


DONE = {"d": "🗑 {who} 메시지를 지웠어요.", "b": "🚫 {who} 밴했어요.",
        "m": "🔇 {who} 1일 뮤트했어요.", "t": "✅ 오탐 처리 — 이 조각은 이 방에서 다시 안 지워요."}


async def r_alert(c: PanelCtx) -> Screen:
    aid, act = to_int(c.arg(0)), c.arg(1)
    row = await c.svc.db._one("SELECT * FROM nsfw_alerts WHERE id=? AND chat_id=?", (aid, c.cid)) if aid else None
    if not row or act not in DONE:
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    right = "delete" if act == "d" else "restrict"
    if act != "t" and not await may(c.svc.perms, c.bot, c.cid, c.uid, right):
        return Screen(None, toast=no_right_text(right), alert=True)
    if (row["done"] in (act, "b", "t")) or (act == "d" and row["deleted"]):
        return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
    prev = row["done"]
    if not await c.svc.db.atomic(lambda cn: cn.execute("UPDATE nsfw_alerts SET done=? WHERE id=? AND done IS ?",
                                                       (act, aid, prev)).rowcount):
        return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
    uid, svc, bot = row["user_id"], c.svc, c.bot
    reason = f"음란물 (약 {int((row['score'] or 0) * 100)}%)"
    try:
        if act == "d":
            await bot.delete_message(c.cid, row["msg_id"])
            await svc.db.log_mod(c.cid, c.uid, uid, "nsfw_delete", reason[:100])
        elif act == "b":
            await svc.mod.ban(bot, c.cid, uid, c.uid, reason)
        elif act == "m":
            await svc.mod.mute(bot, c.cid, uid, nsfwguard.MUTE_MINUTES, c.uid, reason)
        else:   # t = 오탐: 그 조각 통과 목록 + '나쁨' 기록·캐시 지우기
            fu = row["file_uid"]
            if fu:
                await svc.db._write("INSERT OR IGNORE INTO nsfw_ok(chat_id, file_uid) VALUES(?,?)", (c.cid, fu))
                await svc.db._write("DELETE FROM nsfw_bad WHERE file_uid=?", (fu,))
                nsfwguard._CACHE.pop(fu, None)
            await svc.db.log_mod(c.cid, c.uid, uid, "nsfw_ok", "오탐")
    except TelegramError as e:
        await svc.db._write("UPDATE nsfw_alerts SET done=? WHERE id=? AND done=?", (prev, aid, act))
        need = "메시지 삭제" if act == "d" else "사용자 차단"
        return Screen(None, toast=f"실패했어요: {e.message[:100]} (봇에게 '{need}' 권한 확인)", alert=True)
    who = f"{esc(row['name'])}(<code>{uid}</code>)"
    title = esc(await chat_title(svc, c.cid))
    kb = None
    if act == "d":
        kb = nsfwguard.alert_kb(c.cid, aid, True)
    elif act == "m":
        kb = menu._kb([[B("🚫 밴", f"m:nsx:{c.cid}:{aid}:b"), B("✅ 오탐", f"m:nsx:{c.cid}:{aid}:t")]])
    return Screen(f"🔞 <b>{title}</b>\n" + DONE[act].format(who=who), kb, toast="처리했어요.")


menu.register_hub(HubItem(34, "ns", "🔞 음란물 자동 차단"))
menu.register_screen("ns", s_ns)
menu.register_route("nsx", Route(r_alert, ADMIN, fresh=True))
