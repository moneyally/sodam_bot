"""🧾 멤버 타임라인 화면 (👥 멤버 목록 → 🧾 번호) + AI 도구 등록 (sodam/insight.py 를 불러오면 register_tool).

m:mbt:<방>:<사람> — 방 관리자만 (Route 기본 ADMIN, 누를 때마다 그 방 관리자인지 다시 확인),
그 방 멤버 기록이 있는 사람만. 사실(📋)과 AI 기억 메모(🧠, 확인 안 됨)는 칸을 나눠 보여준다. AI 호출 없음.
"""
from __future__ import annotations

import asyncio

from .. import insight  # noqa: F401  AI 도구 member_timeline·analyze_member·room_changes·owner_room_insight 등록
from ..menu import B, PanelCtx, Route, Screen, _kb, register_route
from ..util import to_int


async def s_timeline(c: PanelCtx) -> Screen:
    uid = to_int(c.arg(0))
    if not uid or uid <= 0:
        return Screen(None, toast="없는 멤버예요.", alert=True)
    try:
        f = await asyncio.wait_for(insight.member_facts(c.svc, c.bot, c.cid, uid, days=30), 8)
        m = await insight.memo(c.svc.db, c.cid, uid) if f else None
    except asyncio.TimeoutError:
        return Screen(None, toast="조회가 오래 걸려요. 잠시 후 다시 눌러주세요.", alert=True)
    if f is None:
        return Screen(None, toast="이 방에서 본 적 없는 멤버예요.", alert=True)
    back = c.arg(1) if c.arg(1) in ("seen", "msg", "join") else "seen"
    return Screen(insight.timeline_html(f, c.svc.cfg.tz, m), _kb([
        [B("🔄 새로고침", f"m:mbt:{c.cid}:{uid}:{back}")],
        [B("⬅️ 멤버 목록", f"m:mb:{c.cid}:{back}:0")]]))


register_route("mbt", Route(s_timeline))
