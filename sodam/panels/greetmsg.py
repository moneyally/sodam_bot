"""🌅 인사멘트 패널 (그룹 허브 → m:gm).

아침·점심·저녁 같은 시각마다 인사 멘트를 자동으로 올린다. 멘트를 여러 개 넣으면 그때마다
'돌려가며 하나씩'(무반복 순환) — 매번 같은 글이 아니라 자연스럽다 (AI 가 아니라 방장이 쓴 멘트라 티 0).

예약공지(제휴 공지)와 자리만 나누고, 발송·수정·삭제·미리보기 엔진은 그대로 재사용한다
(schedules.category='greet' · variants 멘트 풀 · 발송은 announce.publish 가 variant_cursor 로 순환).
마법사는 announce.Announcer 의 greet 모드(제목 없이 멘트 쌓기 → 🌅🌞🌙 시각).

m:gm:<방>    목록 (🟢/⏸ · 언제 · 멘트 N개) + ➕ 새 인사멘트
m:gmn:<방>   새로 만들기 → announce 마법사(greet 모드) 1:1
항목·수정·삭제·미리보기·켜기끄기는 예약공지 패널(m:sci·m:sce·m:scd·m:scp·m:sct)을 그대로 쓴다
 (그 화면들이 category='greet' 를 알아보고 🌅 로 보여 주고 '뒤로'를 이 목록으로 보낸다).
"""
from __future__ import annotations

from . import announce as ann
from .. import menu
from ..announce import MAX_PER_CHAT, describe_when, row_variants
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..db import register_columns

# schedules 에 인사멘트용 칸 (예전 DB 는 _migrate 가 추가). category 로 예약공지와 목록을 나눈다.
register_columns("schedules", {
    "category": "TEXT NOT NULL DEFAULT 'announce'",   # 'announce' 예약공지(제휴) / 'greet' 인사멘트
    "variants": "TEXT",                               # 멘트 풀 JSON [멘트, …]
    "variant_cursor": "INTEGER NOT NULL DEFAULT 0",   # 무반복 순환 위치 (그 시각마다 다음 멘트)
})


async def s_list(c: PanelCtx) -> Screen:
    rows = await c.svc.db.schedules(c.cid, "greet")
    lines = [f"🌅 <b>인사멘트</b> ({len(rows)}/{MAX_PER_CHAT})",
             "정한 시각마다 인사 멘트를 자동으로 올려요. 멘트를 <b>여러 개</b> 넣으면 그때마다 "
             "<b>돌려가며 하나씩</b> — 매번 같은 글이 아니라 자연스러워요 (AI 아니라 방장님 멘트라 티 0)."]
    if rows:
        lines.append("눌러서 미리보기·수정·켜기/끄기·삭제할 수 있어요. 🟢 켜짐 · ⏸ 꺼짐")
    else:
        lines.append("\n아직 없어요. <b>➕ 새 인사멘트</b> → 🌅아침·🌞점심·🌙저녁 시각 고르고 멘트를 "
                     "여러 개 넣으면 돼요. 아침·점심·저녁 따로 만들어도 돼요.")
    if not await c.svc.paid_features(c.cid):
        lines.append("\n⚠️ 이용 기간이 끝나서 지금은 안 올라가요.")
    btns = [[B(f"{'🟢' if r['enabled'] else '⏸'} "
               f"{describe_when(r['kind'], r['at_time'], r['interval_min'])} · 멘트 {len(row_variants(r)) or 1}개",
               f"m:sci:{c.cid}:{r['id']}")] for r in rows]
    btns.append([B("➕ 새 인사멘트", f"m:gmn:{c.cid}")])
    btns.append([B("⬅️ 뒤로", f"m:g:{c.cid}")])
    return Screen("\n".join(lines), menu._kb(btns))


async def r_new(c: PanelCtx) -> Screen:
    return await ann._wizard(c, category="greet")   # announce 마법사 greet 모드 (제목 없이 멘트 쌓기)


menu.register_hub(HubItem(51, "gm", "🌅 인사멘트"))   # 예약공지(50) 바로 옆
menu.register_screen("gm", s_list)
menu.register_route("gmn", Route(r_new))
