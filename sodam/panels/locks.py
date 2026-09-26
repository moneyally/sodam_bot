"""🔒 종류별 잠금 (m:lk) · 🚨 대량 입장 방어 (m:raid).

m:lk:<방ID>                    사진·영상·스티커… 종류마다 ✅ 허용 / 🔒 막힘 (목표값 토글 m:t) + 전달 메시지 3단계(m:n)
m:raid:<방ID>                  자동 방어 켜기/끄기 · 기준(명·초)·방어 시간 프리셋 · 지금 방어 켜기/끄기
m:rdm:<방ID>:<1|0>             방어 모드 직접 켜기/끄기 (목표값이라 두 번 눌려도 같음)
검사 자체는 moderation.LOCK_KINDS / sodam/raid.py.
"""
from __future__ import annotations

from datetime import datetime

from .. import menu, raid
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..moderation import LOCK_KINDS
from ..settings import CHOICE_LABELS
from ..util import human_minutes
from . import log as log_panel

for _key, _, _ in LOCK_KINDS:
    menu.register_toggle(_key, "lk")
menu.register_preset("forward_filter", [("off", "전달 허용"), ("newbie", "신규만 막기"), ("all", "전부 막기")], "lk")
menu.register_toggle("raid_guard", "raid")
menu.register_preset("raid_count", [(v, f"{v}명") for v in ("5", "10", "20")], "raid")
menu.register_preset("raid_seconds", [(v, f"{v}초") for v in ("30", "60", "120")], "raid")
menu.register_preset("raid_minutes", [(v, f"방어 {human_minutes(int(v))}") for v in ("10", "30", "60")], "raid")
log_panel.ACTIONS.setdefault("raid", "🚨 방어 모드")
log_panel.ACTIONS.setdefault("raid_off", "✅ 방어 모드 끝")


async def s_locks(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    hours = s["newbie_link_hours"]
    lines = ["🔒 <b>종류별 잠금</b>",
             "막아 둔 종류(🔒)를 관리자 말고 누가 올리면 바로 지워요. 안내는 한 사람에게 10분에 한 번만 해요.",
             "✅ 허용 · 🔒 막힘 — 누르면 바뀌어요.", "",
             f"📨 전달(포워드) 메시지: <b>{CHOICE_LABELS[s['forward_filter']]}</b>",
             f"('신규' = 들어온 지 {hours}시간 안 된 사람. 🛡️ 보안의 신규 입장자 링크 금지와 같은 기준)" if hours
             else "(🛡️ 보안에서 신규 입장자 링크 금지가 꺼져 있어서 '신규만 막기'는 아무도 안 막아요)"]
    btns = [B(("🔒 " if s[k] else "✅ ") + label, f"m:t:{c.cid}:{k}:{0 if s[k] else 1}") for k, label, _ in LOCK_KINDS]
    rows = menu._chunks(btns, 2)
    rows += [menu._preset_row(s, c.cid, "forward_filter"), menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def s_raid(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    end = await raid.until(c.svc, c.cid)
    now = (f"🚨 지금 방어 중 ({datetime.fromtimestamp(end, c.svc.cfg.tz):%H:%M}까지)" if end else "지금: 평소 상태")
    lines = ["🚨 <b>대량 입장 방어</b>",
             f"{s['raid_seconds']}초 안에 {s['raid_count']}명 이상 들어오면 {human_minutes(s['raid_minutes'])} 동안 "
             "방어 모드를 켜요." if s["raid_guard"] else "자동 방어가 꺼져 있어요 (직접 켜기는 가능).",
             "방어 모드 중엔 캡차 설정과 상관없이 새로 들어오는 사람 모두 스팸 확인 버튼(캡차)을 받아요. "
             "자동으로 켜지면 관리자님들께 1:1 로 알려드려요.", "", now]
    rows = [[B(("✅ " if s["raid_guard"] else "❌ ") + "자동 방어", f"m:t:{c.cid}:raid_guard:{0 if s['raid_guard'] else 1}")],
            menu._preset_row(s, c.cid, "raid_count"), menu._preset_row(s, c.cid, "raid_seconds"),
            menu._preset_row(s, c.cid, "raid_minutes"),
            [B("✅ 방어 모드 끄기", f"m:rdm:{c.cid}:0") if end else B("🚨 지금 방어 모드 켜기", f"m:rdm:{c.cid}:1")],
            menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_raid_mode(c: PanelCtx) -> Screen:
    want = c.arg(0)
    if want not in ("0", "1"):
        return Screen(None)
    on = await raid.active(c.svc, c.cid)
    if want == "1" and not on:
        minutes = (await c.svc.db.get_settings(c.cid))["raid_minutes"]
        await raid.start(c.svc, c.bot, c.cid, minutes, actor_id=c.uid)
        toast = f"🚨 방어 모드 {human_minutes(minutes)} 켰어요"
    elif want == "0" and on:
        await raid.stop(c.svc, c.bot, c.cid, actor_id=c.uid)
        toast = "✅ 방어 모드를 껐어요"
    else:
        toast = "이미 그 상태예요"
    screen = await s_raid(c)
    screen.toast = toast
    return screen


menu.register_screen("lk", s_locks)
menu.register_screen("raid", s_raid)
menu.register_route("rdm", Route(r_raid_mode))
menu.register_hub(HubItem(32, "lk", "🔒 종류별 잠금"))
menu.register_hub(HubItem(34, "raid", "🚨 대량 입장 방어"))
