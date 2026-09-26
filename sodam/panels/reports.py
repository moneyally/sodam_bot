"""📊 활동 리포트 · 🧠 AI 하루 요약 설정 화면 (방 관리자 1:1). 집계·요약 본체는 sodam/reports.py.

m:rp:<방ID>[:7|30]      최근 7일(기본)/30일 동안 소담이 한 일 + 하루 요약 시각 프리셋(m:n digest_hour)
"""
from __future__ import annotations

import time

from .. import menu, reports
from ..menu import B, HubItem, PanelCtx, Screen
from ..util import esc

PERIODS = {"7": 7, "30": 30}


async def s_rp(c: PanelCtx) -> Screen:
    days_key = c.arg(0) if c.arg(0) in PERIODS else "7"   # 프리셋을 누른 뒤 다시 그릴 땐 args 가 설정 키라 기본값
    svc, now = c.svc, int(time.time())
    act = await reports.activity(svc, c.cid, now - PERIODS[days_key] * 86400, now + 1)
    title = esc(await reports.chat_title(svc, c.cid))
    s = await svc.db.get_settings(c.cid)
    hour = int(s.get("digest_hour", reports.DIGEST_OFF))
    lines = [reports.format_activity(act, title, svc.cfg.tz, heading=f"📊 최근 {PERIODS[days_key]}일 활동 리포트"),
             "",
             "🧠 <b>관리자 AI 하루 요약</b>",
             "매일 정한 시각에 최근 24시간 대화의 주요 화제·분쟁 징후·답 못 받은 질문을 텔레그램 관리자님들 1:1 로 "
             "보내드려요 (이용 기간 중인 방만, 하루 1번).",
             "지금: <b>" + ("끔" if hour == reports.DIGEST_OFF else f"매일 {hour:02d}:00") + "</b>"]
    if not await svc.paid_features(c.cid):
        lines.append("⚠️ 이용 기간이 끝나서 지금은 요약을 보내지 않아요.")
    period_row = [B(("● " if k == days_key else "") + f"최근 {v}일", f"m:rp:{c.cid}:{k}") for k, v in PERIODS.items()]
    rows = [period_row, menu._preset_row(s, c.cid, "digest_hour"), menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


menu.register_hub(HubItem(8, "rp", "📊 활동 리포트 · AI 하루 요약", wide=True))
menu.register_screen("rp", s_rp)
menu.register_preset("digest_hour", reports.DIGEST_PRESETS, "rp")
