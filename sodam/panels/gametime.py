"""🎮 장시간 게임 알림 화면 (방 관리자 1:1). 동작은 sodam/gametime.py.

m:gt:<방ID>                    켜기/끄기 · 기준 시간 · 쉬면 초기화 · 조치 · 뮤트 시간 · 알릴 사람 · 게임 명령
m:gtme:<방ID>                  알릴 사람을 나로
m:gtp:<방ID>                   지금 오래 게임 중인 사람
m:gtx:<방ID>[:<대상방ID>|all]   이 설정을 내 다른 방에 적용 (대상 방마다 관리자인지 다시 확인)
m:gtb:<방ID>:<사람ID>:m|u|ok   관리자 1:1 알림의 [🔇 뮤트] [🔊 풀기] [👌 괜찮음]
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import gametime, menu
from ..menu import ADMIN, CID_RE, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..settings import choice_label
from ..subscription import chat_title
from ..util import esc, to_int

menu.register_toggle("gt_enabled", "gt")
menu.register_preset("gt_hours", [(str(h), f"{h}시간") for h in (6, 8, 12, 24)], "gt")
menu.register_preset("gt_gap", [(str(m), f"{m}분") for m in (15, 30, 60)], "gt")
menu.register_preset("gt_action", list(gametime.ACTIONS.items()), "gt")
menu.register_preset("gt_mute_hours", [(str(h), f"{h}시간") for h in (1, 3, 6, 12)], "gt")
menu.register_preset("gt_notify", [("setter", "지정한 관리자"), ("admins", "모든 관리자")], "gt")


async def s_gt(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    if not s["gt_setter"]:           # 처음 연 관리자가 알림 받을 사람 (🙋 로 바꿀 수 있음)
        await svc.db.set_setting(cid, "gt_setter", c.uid)
        s["gt_setter"] = c.uid
    on = s["gt_enabled"]
    who = esc(await svc.db.first_name(s["gt_setter"]) or str(s["gt_setter"]))
    lines = ["🎮 <b>장시간 게임 알림</b>",
             "멤버가 게임 명령을 계속 보내면 연속 시간을 세고, 기준을 넘으면 방에 알리고 관리자 1:1 로 알려드려요.",
             f"지금: <b>{'켜짐' if on else '꺼짐'}</b>"]
    if on and not await svc.paid_features(cid):
        lines.append("⚠️ 이용 기간(구독·체험) 중인 방에서만 동작해요. 지금은 쉬고 있어요.")
    lines += [f"⏱ 기준: 연속 <b>{s['gt_hours']}시간</b> · ☕ {s['gt_gap']}분 쉬면 다시 셈",
              f"🚨 조치: <b>{choice_label('gt_action', s['gt_action'])}</b>"
              + (f" ({s['gt_mute_hours']}시간)" if s["gt_action"] != "notify" else ""),
              f"📣 알릴 사람: <b>{who if s['gt_notify'] == 'setter' else '모든 관리자'}</b>",
              "🎯 게임 명령: " + (f"<code>{esc(s['gt_cmds'])}</code>" if s["gt_cmds"] else "/ 나 ! 로 시작하는 명령·🎲 전부")]
    if s["gt_action"] != "notify" and not await svc.perms.bot_can_moderate(c.bot, cid):
        lines.append("⚠️ 봇에게 '사용자 차단' 권한이 없어서 뮤트는 안 돼요 (알림만 가요).")
    rows = [[B(("✅ " if on else "❌ ") + "장시간 게임 알림", f"m:t:{cid}:gt_enabled:{0 if on else 1}")],
            menu._preset_row(s, cid, "gt_hours") + [B("✏️", f"m:in:{cid}:gth")],
            menu._preset_row(s, cid, "gt_gap"), menu._preset_row(s, cid, "gt_action")]
    if s["gt_action"] != "notify":
        rows.append(menu._preset_row(s, cid, "gt_mute_hours"))
    rows += [menu._preset_row(s, cid, "gt_notify"),
             [B("🙋 알림은 나한테", f"m:gtme:{cid}"), B("🎯 게임 명령 정하기", f"m:in:{cid}:gtc")],
             [B("📋 지금 오래 하는 사람", f"m:gtp:{cid}"), B("📤 다른 방에도 적용", f"m:gtx:{cid}")],
             menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_me(c: PanelCtx) -> Screen:
    await c.svc.db.set_setting(c.cid, "gt_setter", c.uid)
    await c.svc.db.set_setting(c.cid, "gt_notify", "setter")
    screen = await s_gt(c)
    screen.toast = "🙋 알림은 이제 나한테 와요"
    return screen


async def r_playing(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    rows = await gametime.playing(c.svc.db, c.cid, s["gt_gap"])
    lines = ["📋 <b>지금 게임 중</b> (오래한 순)"] + [
        f"{i}. {esc(' '.join(x for x in (r['first_name'], r['last_name']) if x) or str(r['user_id']))} — "
        f"{gametime.fmt_dur(r['last'] - r['start'])}" + (" 🔔" if r["alerted"] else "")
        for i, r in enumerate(rows, 1)] or ["(없음)"]
    if not s["gt_enabled"]:
        lines.append("\n알림이 꺼져 있으면 기록도 안 해요.")
    return Screen("\n".join(lines), menu._kb([[B("🔄 새로고침", f"m:gtp:{c.cid}"), B("⬅️ 뒤로", f"m:gt:{c.cid}")]]))


async def r_copy(c: PanelCtx) -> Screen:
    """이 방 설정을 내가 관리자인 다른 방에. 대상마다 관리자 확인 (버튼 조작으로 남의 방 못 바꾸게)."""
    groups = [(g, t) for g, t in await menu.admin_groups(c.svc, c.bot, c.uid) if g != c.cid]
    target = c.arg(0)
    if not target:
        rows = [[B(f"➡️ {t[:28]}", f"m:gtx:{c.cid}:{g}")] for g, t in groups[:15]]
        rows += ([[B(f"📤 전부 ({len(groups)}개 방)", f"m:gtx:{c.cid}:all")]] if len(groups) > 1 else [])
        return Screen("📤 <b>이 방의 게임 알림 설정을 어느 방에 적용할까요?</b>" if groups else "관리 중인 다른 방이 없어요.",
                      menu._kb(rows + [[B("⬅️ 뒤로", f"m:gt:{c.cid}")]]))
    ids = [g for g, _ in groups] if target == "all" else [int(target)] if CID_RE.fullmatch(target) else []
    ids = [g for g in ids if g in {g for g, _ in groups}]
    if not ids:
        return Screen(None, toast="관리 중인 방이 아니에요.", alert=True)
    s = await c.svc.db.get_settings(c.cid)
    for g in ids:
        for k in gametime.KEYS:
            await c.svc.db.set_setting(g, k, s[k])
        await c.svc.db.set_setting(g, "gt_setter", c.uid)
        await c.svc.db.log_mod(g, c.uid, None, "setting", f"게임 알림 설정 복사 ← {c.cid}")
    screen = await s_gt(c)
    screen.toast = f"📤 {len(ids)}개 방에 적용했어요"
    return screen


async def r_button(c: PanelCtx) -> Screen:
    uid, act = to_int(c.arg(0)), c.arg(1)
    if not uid or act not in ("m", "u", "ok"):
        return Screen(None)
    who = f"<code>{uid}</code>"
    title = esc(await chat_title(c.svc, c.cid))
    if act == "ok":
        return Screen(f"🎮 <b>{title}</b>\n👌 {who} 괜찮음으로 처리했어요.", toast="처리했어요.")
    if not await may(c.svc.perms, c.bot, c.cid, c.uid):
        return Screen(None, toast=no_right_text(), alert=True)
    if await c.svc.perms.protected(c.bot, c.cid, uid):
        return Screen(None, toast="관리자는 뮤트할 수 없어요.", alert=True)
    hours = (await c.svc.db.get_settings(c.cid))["gt_mute_hours"]
    try:
        if act == "m":
            await c.svc.mod.mute(c.bot, c.cid, uid, hours * 60, c.uid, "장시간 게임")
        else:
            await c.svc.mod.unmute(c.bot, c.cid, uid, c.uid)
    except TelegramError as e:
        return Screen(None, toast=f"실패했어요: {e.message[:100]} (봇에게 '사용자 차단' 권한이 있는지 확인해주세요)", alert=True)
    done = f"🔇 {who} {hours}시간 뮤트했어요." if act == "m" else f"🔊 {who} 뮤트를 풀었어요."
    return Screen(f"🎮 <b>{title}</b>\n{done}", gametime.button_kb(c.cid, uid, act == "m", hours), toast="처리했어요.")


async def i_hours(c: PanelCtx, msg) -> tuple[bool, str]:
    h = to_int((msg.text or "").strip().removesuffix("시간").strip())
    if not h or not 1 <= h <= 48:
        return False, "1~48 사이 숫자로 보내주세요. 예: <code>10</code>"
    await menu._set(c, "gt_hours", h)
    return True, f"✅ 기준 {h}시간으로 바꿨어요."


async def i_cmds(c: PanelCtx, msg) -> tuple[bool, str]:
    raw = (msg.text or "").strip()
    cmds = "" if raw in ("전부", "기본", "all") else " ".join(w for w in raw.split() if 1 < len(w) <= 32)[:300]
    await menu._set(c, "gt_cmds", cmds)
    return True, f"✅ 게임 명령: {esc(cmds) if cmds else '/ 나 ! 로 시작하는 명령·🎲 전부'}"


menu.register_hub(HubItem(36, "gt", "🎮 장시간 게임 알림"))
menu.register_screen("gt", s_gt)
menu.register_route("gtme", Route(r_me, ADMIN))
menu.register_route("gtp", Route(r_playing, ADMIN))
menu.register_route("gtx", Route(r_copy, ADMIN))
menu.register_route("gtb", Route(r_button, ADMIN, fresh=True))   # 뮤트·풀기: 지금 권한으로
menu.register_input("gth", "⏱ 기준 시간을 숫자로 보내주세요 (1~48). 예: <code>10</code>", "gt", i_hours, s_gt)
menu.register_input("gtc", "🎯 게임으로 칠 명령을 띄어서 보내주세요. 예: <code>/ㅅㅌㅊ /ㄱㄹㅈ /ㄷㄹ</code>\n"
                    "기본(/ 나 ! 로 시작하는 명령·🎲 전부)으로 돌리려면 <code>전부</code>", "gt", i_cmds, s_gt)
menu.register_route("gth", Route(s_gt, ADMIN))       # 입력 화면의 [⬅️ 메뉴로] (m:<입력종류>:<방>)
menu.register_route("gtc", Route(s_gt, ADMIN))
