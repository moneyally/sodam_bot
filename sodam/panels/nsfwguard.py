"""🔞 음란물 자동 차단 화면 (방 관리자 1:1). 감지·삭제는 sodam/nsfwguard.py.

m:ns:<방ID>                      끄기 / 알림만 / 지우고 알림 / 지우고 뮤트 / 지우고 밴 (프리셋) + 설명·최근 처리 수
m:nsx:<방ID>:<알림ID>:d|b|m|t|s  알림 버튼 [🗑 지우기] [🚫 밴] [🔇 뮤트 1일] [✅ 오탐] [🧩 스티커팩 전체 차단]
"""
from __future__ import annotations

import time

from telegram.error import TelegramError

from .. import db as dbmod
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
    nset = await svc.db._one("SELECT COUNT(*) AS n FROM nsfw_set") or {"n": 0}
    lines = ["🔞 <b>음란물 자동 차단</b>",
             "스티커·사진·GIF·영상이 올라오면 바로 검사해요. <b>관리자·일반 멤버 모두 검사</b>하고, 오너(운영자)만 빼요.", "",
             f"지금: <b>{esc(nsfwguard.MODES.get(mode, mode))}</b>",
             f"최근 24시간 처리: {today}건 · 차단한 스티커팩: {nset['n']}개", "",
             "• <b>지우고 알림 / 지우고 뮤트 / 지우고 밴</b> = 음란물이면 <b>조용히</b> 바로 지워요(방 문구·알림 없음 — "
             "도배 때 알림이 폭주하지 않게). 뮤트·밴은 확신 높을 때만 보낸 사람까지 제재. 처리 내역은 📒 기록에 남아요.",
             "• <b>알림만</b> = 안 지우고 관리자님께 1:1 알림 → [🗑][🚫밴][🔇뮤트][🧩 팩 차단][✅ 오탐] 버튼으로 직접 처리.", "",
             "조각낸 신체사진(스티커팩)은 한 팩에서 조각 2개↑ 걸리면 그 팩 전부를 즉시 지워요. 거의 살색인 조각도 잡아요. "
             "봇에게 '메시지 삭제' 권한이 있어야 지워요."]
    rows = [menu._preset_row(s, cid, "nsfw_mode"), menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


DONE = {"d": "🗑 {who} 메시지를 지웠어요.", "b": "🚫 {who} 밴했어요.",
        "m": "🔇 {who} 1일 뮤트했어요.", "t": "✅ 오탐 처리 — 이 조각은 이 방에서 다시 안 지워요."}


async def r_alert(c: PanelCtx) -> Screen:
    aid, act = to_int(c.arg(0)), c.arg(1)
    row = await c.svc.db._one("SELECT * FROM nsfw_alerts WHERE id=? AND chat_id=?", (aid, c.cid)) if aid else None
    if not row or act not in ("d", "b", "m", "t", "s"):
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    svc, bot, uid = c.svc, c.bot, row["user_id"]
    title = esc(await chat_title(svc, c.cid))

    # 🧩 스티커팩 전체 차단 — 독립(멱등), 대표조치 게이트 안 탐 (밴/뮤트와 같이 눌러도 됨)
    if act == "s":
        if not await may(svc.perms, bot, c.cid, c.uid, "delete"):
            return Screen(None, toast=no_right_text("delete"), alert=True)
        sn = row["set_name"]
        if not sn:
            return Screen(None, toast="이건 스티커가 아니라 팩 차단이 안 돼요.", alert=True)
        await svc.db._write("INSERT OR IGNORE INTO nsfw_set(set_name, ts) VALUES(?,?)", (sn, dbmod.now()))
        try:
            await bot.delete_message(c.cid, row["msg_id"])
        except TelegramError:
            pass
        await svc.db.log_mod(c.cid, c.uid, uid, "nsfw_set_block", sn[:100])
        return Screen(f"🔞 <b>{title}</b>\n🧩 스티커팩 <code>{esc(sn)}</code> 전체를 차단했어요. "
                      f"앞으로 이 팩 조각은 다 지워져요.", None, toast="팩 차단 완료")

    right = "delete" if act == "d" else "restrict"
    if act != "t" and not await may(svc.perms, bot, c.cid, c.uid, right):
        return Screen(None, toast=no_right_text(right), alert=True)
    if (row["done"] in (act, "b", "t")) or (act == "d" and row["deleted"]):
        return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
    prev = row["done"]
    if not await svc.db.atomic(lambda cn: cn.execute("UPDATE nsfw_alerts SET done=? WHERE id=? AND done IS ?",
                                                     (act, aid, prev)).rowcount):
        return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
    reason = f"음란물 (약 {int((row['score'] or 0) * 100)}%)"
    try:
        if act == "d":
            await bot.delete_message(c.cid, row["msg_id"])
            await svc.db.log_mod(c.cid, c.uid, uid, "nsfw_delete", reason[:100])
        elif act == "b":
            await svc.mod.ban(bot, c.cid, uid, c.uid, reason)
        elif act == "m":
            await svc.mod.mute(bot, c.cid, uid, nsfwguard.MUTE_MINUTES, c.uid, reason)
        else:   # t = 오탐: 그 조각(과 팩) 통과 + '나쁨'·지문 기록 지우기
            fu, sn = row["file_uid"], row["set_name"]
            if fu:
                await svc.db._write("INSERT OR IGNORE INTO nsfw_ok(chat_id, file_uid) VALUES(?,?)", (c.cid, fu))
                await svc.db._write("DELETE FROM nsfw_bad WHERE file_uid=?", (fu,))
                nsfwguard._CACHE.pop(fu, None)
            if sn:   # 팩이 잘못 걸렸던 거면 팩 차단도 풀어줌
                await svc.db._write("DELETE FROM nsfw_set WHERE set_name=?", (sn,))
                await svc.db._write("DELETE FROM nsfw_set_hits WHERE set_name=?", (sn,))
            await svc.db.log_mod(c.cid, c.uid, uid, "nsfw_ok", "오탐")
    except TelegramError as e:
        await svc.db._write("UPDATE nsfw_alerts SET done=? WHERE id=? AND done=?", (prev, aid, act))
        need = "메시지 삭제" if act == "d" else "사용자 차단"
        return Screen(None, toast=f"실패했어요: {e.message[:100]} (봇에게 '{need}' 권한 확인)", alert=True)
    who = f"{esc(row['name'])}(<code>{uid}</code>)"
    has_set = bool(row["set_name"])
    kb = None
    if act == "d":
        kb = nsfwguard.alert_kb(c.cid, aid, True, has_set)
    elif act == "m":
        extra = [B("🧩 팩 차단", f"m:nsx:{c.cid}:{aid}:s")] if has_set else []
        kb = menu._kb([[B("🚫 밴", f"m:nsx:{c.cid}:{aid}:b"), *extra, B("✅ 오탐", f"m:nsx:{c.cid}:{aid}:t")]])
    return Screen(f"🔞 <b>{title}</b>\n" + DONE[act].format(who=who), kb, toast="처리했어요.")


menu.register_hub(HubItem(34, "ns", "🔞 음란물 자동 차단"))
menu.register_screen("ns", s_ns)
menu.register_route("nsx", Route(r_alert, ADMIN, fresh=True))
