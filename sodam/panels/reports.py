"""📊 활동 리포트 · 🧠 AI 하루 요약 설정 화면 (방 관리자 1:1). 집계·요약 본체는 sodam/reports.py.

m:rp:<방ID>[:7|30]      최근 7일(기본)/30일 동안 소담이 한 일 + 방 기본 시각 프리셋(m:n digest_hour, -1 = 방 전체 끔)
                        + '🧠 하루 요약 받기 (나)' 토글(텔레그램 관리자·오너만) + ⏰ 내 받는 시각
m:rpme:<방ID>:<0|1>     보고 있는 관리자 본인이 이 방 요약을 받을지 (digest_prefs 방 줄)
m:dgp[:new|stop|off|h:<시>|h:d]  사람별 설정 (방 없음, 누구나 '자기 것'만): 받는 시각 9/18/21/23·방 기본·끔.
                        new = 요약 메시지의 [⏰ 받는 시각] → 요약 글은 그대로 두고 새 메시지로 화면
                        stop = 요약 메시지의 [🔕 그만 받기] → 모든 방 끔 (팝업만, 요약 글은 그대로)
"""
from __future__ import annotations

import time

from telegram.error import TelegramError

from .. import menu, reports
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..util import esc

PERIODS = {"7": 7, "30": 30}


async def _can_receive(c: PanelCtx) -> bool:
    """요약은 텔레그램 관리자(·오너)에게만 가므로 토글도 그 사람에게만 (.봇관리자 는 안 보임)."""
    try:
        return await c.svc.perms.is_tg_admin(c.bot, c.cid, c.uid)
    except TelegramError:
        return False


async def s_rp(c: PanelCtx) -> Screen:
    days_key = c.arg(0) if c.arg(0) in PERIODS else "7"   # 프리셋을 누른 뒤 다시 그릴 땐 args 가 설정 키라 기본값
    svc, now = c.svc, int(time.time())
    act = await reports.activity(svc, c.cid, now - PERIODS[days_key] * 86400, now + 1)
    title = esc(await reports.chat_title(svc, c.cid))
    s = await svc.db.get_settings(c.cid)
    hour = int(s.get("digest_hour", reports.DIGEST_OFF))
    lines = [reports.format_activity(act, title, svc.cfg.tz, heading=f"📊 최근 {PERIODS[days_key]}일 활동 리포트"),
             "",
             "🧠 <b>AI 하루 요약</b>",
             "하루 한 번 최근 24시간 대화의 주요 화제·분쟁 징후·답 못 받은 질문을 1:1 로 보내드려요 "
             "(이용 기간 중인 방만). 기본으로 <b>방을 등록한 대표님</b>이 받고, 다른 관리자는 아래 '받기 (나)'를 켜면 받아요. "
             "방이 여럿이면 한 통으로 묶어서 가요.",
             "방 기본 시각: <b>" + ("끔 (이 방은 아무도 안 받음)" if hour == reports.DIGEST_OFF else f"매일 {hour:02d}:00")
             + "</b> · 사람마다 ⏰ 에서 바꿀 수 있어요"]
    reg = (await reports.registrants(svc.db)).get(c.cid)
    if reg:
        lines.append(f"등록한 대표님: {esc(await reports.person_name(svc.db, reg))}")
        if await reports.last_status(svc.db, reg) == "forbidden":
            lines.append("⚠️ 대표님이 소담과 1:1 을 열지 않아 요약을 못 보냈어요. 방에 '1:1 열기' 안내를 가끔 올려요.")
    if not await svc.paid_features(c.cid):
        lines.append("⚠️ 이용 기간이 끝나서 지금은 요약을 보내지 않아요.")
    n_min = int(s.get("chat_min_chars") or 0)
    lines += ["", "📏 <b>채팅 집계</b> (<code>.랭킹</code>·통계·하루 리포트): "
              + (f"<b>{n_min}글자 이상</b> 쓴 글만 셈 (띄어쓰기 빼고)" if n_min else "<b>모든 글</b>을 셈")]
    period_row = [B(("● " if k == days_key else "") + f"최근 {v}일", f"m:rp:{c.cid}:{k}") for k, v in PERIODS.items()]
    rows = [period_row, menu._preset_row(s, c.cid, "digest_hour"), menu._preset_row(s, c.cid, "chat_min_chars")]
    if await _can_receive(c):
        me = await reports.receives(svc, c.uid, c.cid)
        rows.append([B(("✅" if me else "❌") + " 🧠 하루 요약 받기 (나)", f"m:rpme:{c.cid}:{0 if me else 1}")])
        rows.append([B("⏰ 내 요약 받는 시각", "m:dgp")])
    rows.append(menu._back(c.cid))
    return Screen("\n".join(lines), menu._kb(rows))


async def r_rpme(c: PanelCtx) -> Screen:
    if c.arg(0) not in ("0", "1"):
        return Screen(None)
    on = c.arg(0) == "1"
    await reports.set_pref(c.svc.db, c.uid, c.cid, enabled=int(on))
    if on and (await reports.my_prefs(c.svc.db, c.uid))["enabled"] == 0:   # 🔕 전체 끔이었으면 다시 받기로
        await reports.set_pref(c.svc.db, c.uid, reports.ALL_ROOMS, enabled=None)
    screen = await s_rp(PanelCtx(c.svc, c.bot, c.uid, c.cid, []))
    screen.toast = "🧠 이 방 하루 요약을 받아요" if on else "이 방 하루 요약을 안 받아요"
    return screen


def _hour_label(p: dict) -> str:
    if p["enabled"] == 0:
        return "🔕 안 받음"
    return f"매일 {p['hour']:02d}:00" if p["hour"] is not None else "방 기본 시각 (보통 21:00)"


async def s_dgp(c: PanelCtx) -> Screen:
    p = await reports.my_prefs(c.svc.db, c.uid)
    text = "\n".join([
        "⏰ <b>내 AI 하루 요약</b>",
        "하루 한 번, 내가 받는 방들의 요약을 한 통으로 보내드려요 (방이 여럿이면 묶어서).",
        "받는 방: 내가 등록한 방 + 📊 활동 리포트에서 '받기 (나)'를 켠 방 (텔레그램 관리자인 방만).",
        f"지금: <b>{_hour_label(p)}</b>"])
    on = p["enabled"] != 0
    hours = [B(("● " if on and p["hour"] == h else "") + f"{h:02d}:00", f"m:dgp:h:{h}") for h in reports.HOUR_CHOICES]
    rows = [hours[:2], hours[2:],
            [B(("● " if on and p["hour"] is None else "") + "↩️ 방 기본 시각", "m:dgp:h:d"),
             B(("● " if not on else "") + "🔕 끔", "m:dgp:off")],
            [B("⬅️ 처음으로", "m:home")]]
    return Screen(text, menu._kb(rows))


async def r_dgp(c: PanelCtx) -> Screen:
    """본인 설정만 바꾼다 (방 없음 → 누구나 눌러도 자기 줄만)."""
    db, action = c.svc.db, c.arg(0)
    if action == "new":          # 요약 메시지는 그대로 두고 설정 화면을 새 메시지로
        screen = await s_dgp(c)
        await menu.send_panel(c.svc, c.bot, c.uid, lambda: c.bot.send_message(
            c.uid, screen.text, parse_mode="HTML", reply_markup=screen.kb))
        return Screen(None)
    if action == "stop":
        await reports.set_pref(db, c.uid, reports.ALL_ROOMS, enabled=0)
        return Screen(None, toast="🔕 이제 하루 요약을 안 보내요. 다시 받으려면 [⏰ 받는 시각] 에서 시각을 고르세요.",
                      alert=True)
    if action == "off":
        await reports.set_pref(db, c.uid, reports.ALL_ROOMS, enabled=0)
        toast = "🔕 하루 요약 끔"
    elif action == "h" and c.arg(1) == "d":
        await reports.set_pref(db, c.uid, reports.ALL_ROOMS, enabled=None, hour=None)
        toast = "✅ 방 기본 시각에 받아요"
    elif action == "h" and c.arg(1).isdecimal() and int(c.arg(1)) in reports.HOUR_CHOICES:
        await reports.set_pref(db, c.uid, reports.ALL_ROOMS, enabled=None, hour=int(c.arg(1)))
        toast = f"✅ 매일 {int(c.arg(1)):02d}:00 에 받아요"
    elif action == "":
        return await s_dgp(c)
    else:
        return Screen(None)
    screen = await s_dgp(c)
    screen.toast = toast
    return screen


menu.register_hub(HubItem(8, "rp", "📊 활동 리포트 · AI 하루 요약", wide=True))
menu.register_screen("rp", s_rp)
menu.register_preset("digest_hour", reports.DIGEST_PRESETS, "rp")
menu.register_preset("chat_min_chars", [("0", "📏 전부"), ("2", "2자↑"), ("3", "3자↑"), ("5", "5자↑"), ("10", "10자↑")], "rp")
menu.register_route("rpme", Route(r_rpme, menu.TG_ADMIN))
menu.register_route("dgp", Route(r_dgp, menu.PUBLIC, scoped=False))
