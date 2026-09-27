"""🧭 이상징후 감지 화면 (방 관리자 1:1). 감지 자체는 sodam/anomaly.py.

m:anm:<방ID>                     켜기/끄기 · 민감도 · 보안 강화 시간 프리셋 · 보안 강화 상태 · 최근 알림 목록
m:anmx:<방ID>:<알림ID>:d         🔍 상세 보기 (들어온 사람·반복된 링크(도메인만, 안 눌리게)·시간대별)
m:anmx:<방ID>:<알림ID>:h|H       🛡️ 보안 강화 확인 화면 → 켜기  ('사용자 차단' 권한을 누를 때마다 다시 확인)
m:anmx:<방ID>:<알림ID>:i         🙈 무시 (처리 표시 + 관리 기록)
m:anmu:<방ID>:<0|1>              🔓 보안 강화 지금 끄기 (0 = 확인 화면, 1 = 끄기)
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import anomaly, menu, raid
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..subscription import chat_title
from ..util import esc, fmt_time, mention, to_int
from . import log as log_panel

menu.register_preset("anomaly_mode", [("notify", "🔔 알림 켬"), ("off", "❌ 끔")], "anm")
menu.register_preset("anomaly_level", [(k, v.label) for k, v in anomaly.LEVELS.items()], "anm")
menu.register_preset("anomaly_harden_hours", [(v, f"🛡️ {v}시간") for v in ("1", "3", "6")], "anm")
log_panel.ACTIONS.setdefault("anomaly", "🧭 이상징후 알림")
log_panel.ACTIONS.setdefault("anomaly_ignore", "🙈 이상징후 무시")
log_panel.ACTIONS.setdefault("anomaly_harden", "🛡️ 보안 강화")
log_panel.ACTIONS.setdefault("anomaly_harden_off", "🔓 보안 강화 끝")

STATUS = {None: "확인 전", "ignored": "🙈 무시함", "hardened": "🛡️ 보안 강화함"}
MAX_TEXT = 3800


def _hhmm(svc, ts: int) -> str:
    return datetime.fromtimestamp(ts, svc.cfg.tz).strftime("%H:%M")


async def s_anm(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    await anomaly.maybe_revert(svc, c.bot, cid)
    s = await svc.db.get_settings(cid)
    on = s["anomaly_mode"] != "off"
    lv = anomaly.LEVELS.get(s["anomaly_level"], anomaly.LEVELS["normal"])
    lines = ["🧭 <b>이상징후 감지</b>",
             f"최근 {anomaly.WINDOW // 60}분 동안 입장 몰림·비슷한 이름·같은 링크 반복·새 계정 비율·신규 멤버 메시지 몰림을 "
             "함께 보고, 위험해 보이면 '사용자 차단' 권한이 있는 관리자님께 1:1 로 알려드려요.",
             "자동 제재는 하지 않아요 · AI 비용 없음 · 관리자·자유 멤버는 세지 않아요.", "",
             f"지금: <b>{anomaly.MODES.get(s['anomaly_mode'], s['anomaly_mode'])}</b>",
             f"민감도: <b>{lv.label}</b> — 입장 {lv.joins}명↑(평소의 {lv.factor:g}배↑) · 비슷한 이름 {lv.names}명 · "
             f"같은 링크 {lv.links}회 · 신규 메시지 {lv.flood}개 · 새 계정 {int(lv.recent * 100)}%",
             f"알림: {anomaly.COOLDOWN // 60}분에 1번 · 하루 {anomaly.DAILY_CAP}번까지",
             f"🛡️ 보안 강화(알림의 버튼): {s['anomaly_harden_hours']}시간 동안 대량 입장 방어 모드 + 신규 입장자 링크·전달 제한, "
             "끝나면 자동으로 원래대로"]
    until = await anomaly.hardened_until(svc, cid)
    if until:
        lines.append(f"🛡️ <b>지금 보안 강화 중</b> ({_hhmm(svc, until)}까지)")
    elif await raid.active(svc, cid):
        lines.append("🚨 지금 대량 입장 방어 중 — 그동안 입장 몰림은 알림에서 빼요.")
    alerts = await anomaly.recent_alerts(svc.db, cid)
    if alerts:
        lines += ["", "최근 알림:"]
        lines += [f"<code>{fmt_time(a['ts'], svc.cfg.tz)}</code> {esc(a['summary'])} · {STATUS.get(a['status'], '')}"
                  for a in alerts]
    rows = [menu._preset_row(s, cid, "anomaly_mode"), menu._preset_row(s, cid, "anomaly_level"),
            menu._preset_row(s, cid, "anomaly_harden_hours")]
    if until:
        rows.append([B("🔓 보안 강화 지금 끄기", f"m:anmu:{cid}:0")])
    rows += menu._chunks([B(f"🔍 {fmt_time(a['ts'], svc.cfg.tz)}", f"m:anmx:{cid}:{a['id']}:d") for a in alerts], 2)
    rows.append(menu._back(cid))
    if not on:
        lines.insert(4, "(꺼져 있어서 지금은 세지 않아요)")
    return Screen("\n".join(lines), menu._kb(rows))


def detail_text(svc, title: str, row) -> str:
    d = json.loads(row["detail"] or "{}")
    lines = [f"🔍 <b>이상징후 상세</b> · <b>{esc(title)}</b>",
             f"<code>{fmt_time(row['ts'], svc.cfg.tz)}</code> 위험 점수 {row['score']} · {STATUS.get(row['status'], '')}",
             esc(row["summary"])]
    lines += [f"• {esc(r)}" for r in d.get("reasons", [])]
    if d.get("raid"):
        lines.append("🚨 그때 대량 입장 방어 중이었어요.")
    tail = []
    links = d.get("links") or []
    if links:
        tail += ["", "🔗 <b>올라온 링크</b> (도메인만, 눌리지 않게 표시)"]
        tail += [f"• <code>{esc(anomaly.defang(k))}</code> {n}회 · {u}명" for k, n, u in links]
    timeline = d.get("timeline") or []
    if timeline:
        tail += ["", "⏱️ <b>시간대별</b> (입장 · 링크 · 신규 메시지)"]
        tail += [f"<code>{_hhmm(svc, ts)}</code> {j} · {lk} · {m}" for ts, j, lk, m in timeline]
    users = d.get("users") or []
    head = ["", f"👥 <b>들어온 사람</b> {d.get('joins', 0)}명" + (" (🔁 비슷한 이름 · 🆕 새 계정)" if users else "")]
    body = []
    for uid, name, username, recent, clustered in users:
        body.append("• " + ("🔁" if clustered else "") + ("🆕" if recent else "") + mention(uid, name[:32])
                    + (f" @{esc(username)}" if username else "") + f" <code>{uid}</code>")
    more = d.get("more", 0)
    text = "\n".join(lines + tail)
    # 텔레그램 4096자 제한: 사람 목록을 줄여서 맞춤
    while body and len(text) + len("\n".join(head + body)) + 40 > MAX_TEXT:
        body.pop()
        more += 1
    if body:
        text = "\n".join(lines + head + body + ([f"… 외 {more}명"] if more else []) + tail)
    return text


async def _alert_screen(c: PanelCtx, row, toast: str | None = None) -> Screen:
    title = await chat_title(c.svc, c.cid)
    rows = []
    if row["status"] is None:
        cb = f"m:anmx:{c.cid}:{row['id']}:"
        rows.append([B("🛡️ 보안 강화", cb + "h"), B("🙈 무시", cb + "i")])
    rows.append([B("⬅️ 이상징후 감지", f"m:anm:{c.cid}")])
    return Screen(detail_text(c.svc, title, row), menu._kb(rows), toast=toast)


async def r_alert(c: PanelCtx) -> Screen:
    aid, act = to_int(c.arg(0)), c.arg(1)
    row = await anomaly.get_alert(c.svc.db, c.cid, aid) if aid else None
    if not row or act not in ("d", "h", "H", "i"):
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    if act == "d":
        return await _alert_screen(c, row)
    if not await may(c.svc.perms, c.bot, c.cid, c.uid, "restrict"):   # 누를 때마다 지금 권한으로
        return Screen(None, toast=no_right_text("restrict"), alert=True)
    if row["status"] is not None:
        return Screen(None, toast=f"이미 처리된 알림이에요 ({STATUS.get(row['status'], '')}).", alert=True)
    svc = c.svc
    hours = (await svc.db.get_settings(c.cid))["anomaly_harden_hours"]
    if act == "h":
        s = await svc.db.get_settings(c.cid)
        plan = anomaly.planned_changes(s)
        lines = [f"🛡️ <b>보안 강화</b> — {hours}시간 동안",
                 "• 대량 입장 방어 모드: " + ("새로 들어오는 사람을 안내 없이 바로 내보내요"
                                        if s["raid_action"] == "kick" else "새로 들어오는 사람 모두 캡차를 받아요")]
        if "newbie_link_hours" in plan:
            lines.append(f"• 신규 입장자 링크 금지: {anomaly.NEWBIE_LINK_HOURS}시간 (지금 {s['newbie_link_hours']}시간)")
        if "forward_filter" in plan:
            lines.append("• 신규 입장자 전달(포워드) 막기")
        lines += ["방에 방어 모드 안내가 한 번 올라가요. 시간이 지나면 자동으로 원래대로 돌려요 "
                  "(그 사이 직접 바꾼 설정은 그대로 둬요).", "켤까요?"]
        cb = f"m:anmx:{c.cid}:{aid}:"
        return Screen("\n".join(lines), menu._kb([[B("✅ 보안 강화 켜기", cb + "H"), B("취소", cb + "d")]]))
    if act == "H":
        if not await anomaly.mark(svc.db, aid, "hardened", c.uid):
            return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
        until = await anomaly.harden(svc, c.bot, c.cid, c.uid, hours)
        row = await anomaly.get_alert(svc.db, c.cid, aid)
        return await _alert_screen(c, row, toast=f"🛡️ {_hhmm(svc, until)}까지 보안 강화했어요")
    if not await anomaly.mark(svc.db, aid, "ignored", c.uid):
        return Screen(None, toast="이미 처리된 알림이에요.", alert=True)
    await svc.db.audit(c.cid, c.uid, None, "anomaly_ignore", row["summary"])
    row = await anomaly.get_alert(svc.db, c.cid, aid)
    return await _alert_screen(c, row, toast="🙈 무시했어요 (관리 기록에 남겼어요)")


async def r_unharden(c: PanelCtx) -> Screen:
    if not await may(c.svc.perms, c.bot, c.cid, c.uid, "restrict"):
        return Screen(None, toast=no_right_text("restrict"), alert=True)
    if not await anomaly.hardened_until(c.svc, c.cid):
        screen = await s_anm(c)
        screen.toast = "보안 강화 중이 아니에요"
        return screen
    if c.arg(0) != "1":
        return Screen("🔓 보안 강화를 지금 끌까요? 바꿨던 설정을 원래대로 돌리고, 같이 켠 대량 입장 방어 모드도 꺼요.",
                      menu._kb([[B("🔓 끄기", f"m:anmu:{c.cid}:1"), B("취소", f"m:anm:{c.cid}")]]))
    await anomaly.revert(c.svc, c.bot, c.cid, actor_id=c.uid)
    screen = await s_anm(c)
    screen.toast = "🔓 보안 강화를 껐어요"
    return screen


menu.register_hub(HubItem(35, "anm", "🧭 이상징후 감지"))
menu.register_screen("anm", s_anm)
menu.register_route("anmx", Route(r_alert, ADMIN, fresh=True))
menu.register_route("anmu", Route(r_unharden, ADMIN, fresh=True))
