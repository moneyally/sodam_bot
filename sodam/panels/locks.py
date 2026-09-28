"""🔒 종류별 잠금 (m:lk) · 🚨 대량 입장 방어 (m:raid).

m:lk:<방ID>                    사진·영상·스티커… 종류마다 ✅ 허용 / 🔒 막힘 (목표값 토글 m:t) + 전달 메시지 3단계(m:n)
m:raid:<방ID>                  자동 방어 켜기/끄기 · 기준(명·초)·방어 시간 프리셋 · 지금 방어 켜기/끄기
m:rdm:<방ID>:<1|0>             방어 모드 직접 켜기/끄기 (목표값이라 두 번 눌려도 같음)
검사 자체는 moderation.LOCK_KINDS / sodam/raid.py.
"""
from __future__ import annotations

from datetime import datetime

from .. import menu, persist, raid
from ..permissions import may
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..moderation import LOCK_KINDS
from ..settings import choice_label
from ..util import human_minutes
from . import log as log_panel

for _key, _, _ in LOCK_KINDS:
    menu.register_toggle(_key, "lk")
menu.register_toggle("promo_mentions", "lk")
menu.register_preset("forward_filter", [("off", "전달 허용"), ("newbie", "신규만 막기"), ("all", "전부 막기")], "lk")
menu.register_toggle("raid_guard", "raid")
menu.register_preset("raid_count", [(v, f"{v}명") for v in ("5", "10", "20")], "raid")
menu.register_preset("raid_seconds", [(v, f"{v}초") for v in ("30", "60", "120")], "raid")
menu.register_preset("raid_action", [("captcha", "새 입장자 캡차"), ("kick", "새 입장자 바로 내보내기")], "raid")
menu.register_preset("raid_minutes", [(v, f"방어 {human_minutes(int(v))}") for v in ("10", "30", "60")], "raid")
log_panel.ACTIONS.setdefault("raid", "🚨 방어 모드")
log_panel.ACTIONS.setdefault("raid_off", "✅ 방어 모드 끝")


async def s_locks(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    hours = s["newbie_link_hours"]
    lines = ["🔒 <b>종류별 잠금</b>",
             "막아 둔 종류(🔒)를 관리자 말고 누가 올리면 바로 지워요. 안내는 한 사람에게 10분에 한 번만 해요.",
             "✅ 허용 · 🔒 막힘 — 누르면 바뀌어요.", "",
             "🔗 홍보 @아이디: 다른 채널·그룹·봇을 알리는 @아이디 (사람 태그는 안 막아요). 허락한 홍보가 있으면 ✅ 로 두세요.",
             f"📨 전달(포워드) 메시지: <b>{choice_label('forward_filter', s['forward_filter'])}</b>",
             f"('신규' = 들어온 지 {hours}시간 안 된 사람. 🛡️ 보안의 신규 입장자 링크 금지와 같은 기준)" if hours
             else "(🛡️ 보안에서 신규 입장자 링크 금지가 꺼져 있어서 '신규만 막기'는 아무도 안 막아요)"]
    btns = [B(("🔒 " if s[k] else "✅ ") + label, f"m:t:{c.cid}:{k}:{0 if s[k] else 1}") for k, label, _ in LOCK_KINDS]
    rows = menu._chunks(btns, 2)
    rows += [[B(("🔒 " if s["promo_mentions"] else "✅ ") + "홍보 @아이디 (채널·그룹·봇)",
                f"m:t:{c.cid}:promo_mentions:{0 if s['promo_mentions'] else 1}")],
             menu._preset_row(s, c.cid, "forward_filter"), menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def s_raid(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    end = await raid.until(c.svc, c.cid)
    now = (f"🚨 지금 방어 중 ({datetime.fromtimestamp(end, c.svc.cfg.tz):%H:%M}까지)" if end else "지금: 평소 상태")
    lines = ["🚨 <b>대량 입장 방어</b>",
             f"{s['raid_seconds']}초 안에 {s['raid_count']}명 이상 들어오면 {human_minutes(s['raid_minutes'])} 동안 "
             "방어 모드를 켜요." if s["raid_guard"] else "자동 방어가 꺼져 있어요 (직접 켜기는 가능).",
             ("방어 모드 중엔 새로 들어오는 사람을 안내 없이 바로 내보내요 (다시 들어올 수는 있어요). 끝나면 몇 명 내보냈는지 알려드려요."
              if s["raid_action"] == "kick" else
              "방어 모드 중엔 캡차 설정과 상관없이 새로 들어오는 사람 모두 스팸 확인 버튼(캡차)을 받아요.")
             + " 자동으로 켜지면 관리자님들께 1:1 로 알려드려요.", "", now]
    rows = [[B(("✅ " if s["raid_guard"] else "❌ ") + "자동 방어", f"m:t:{c.cid}:raid_guard:{0 if s['raid_guard'] else 1}")],
            menu._preset_row(s, c.cid, "raid_count"), menu._preset_row(s, c.cid, "raid_seconds"),
            menu._preset_row(s, c.cid, "raid_minutes"), menu._preset_row(s, c.cid, "raid_action"),
            [B("✅ 방어 모드 끄기", f"m:rdm:{c.cid}:0") if end else B("🚨 지금 방어 모드 켜기", f"m:rdm:{c.cid}:1")],
            menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_raid_mode(c: PanelCtx) -> Screen:
    want = c.arg(0)
    if want not in ("0", "1"):
        return Screen(None)
    if not await may(c.svc.perms, c.bot, c.cid, c.uid):   # 방어 모드 = 전원 캡차·내보내기 → 제재와 같은 '사용자 차단' 권한 (🧭 보안 강화와 같게)
        return Screen(None, toast="'사용자 차단' 권한이 있는 관리자만 방어 모드를 켜고 끌 수 있어요.", alert=True)
    if not await persist.claim(c.svc.db, f"raid_btn:{c.cid}:{want}", 5):   # 두 관리자가 동시에 → 방 안내 2개·기록 2줄 (감사 B2)
        return Screen(None, toast="방금 처리했어요.")
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
menu.register_route("rdm", Route(r_raid_mode, fresh=True))
menu.register_hub(HubItem(32, "lk", "🔒 종류별 잠금"))
menu.register_hub(HubItem(34, "raid", "🚨 대량 입장 방어"))
