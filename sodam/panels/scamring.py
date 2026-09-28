"""🕸️ 사기 무리 (sodam/scamring.py) 화면·AI 도구·밴 알림.

그룹 허브 🕸️ m:rgl:<방> 목록 → m:rng:<방>:<사람> 자세히 → m:rngb 확인 → m:rngx 이 방에서 밴 (텔레그램 관리자 화면,
밴은 누를 때 '사용자 차단' 권한·관리자·자유 멤버를 다시 확인, 무리당 60초 차지로 한 번). 다른 방 이름은 숫자로만.
AI 도구 scam_ring(name) 읽기 전용(tainted — 글 조각이 멤버가 쓴 것). 밴 알림은 hooks tick (새 mod_log ban → 무리).
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .. import free, hooks, incidents, menu, persist, scamring as SR, tools
from ..menu import TG_ADMIN, B, HubItem, PanelCtx, Route, Screen, _back, _kb
from ..permissions import Role, may
from ..util import esc, mention, to_int

log = logging.getLogger(__name__)
MAX_LIST = 8


async def _names(db, uids) -> list[str]:
    return [mention(u, (await db.first_name(u)) or str(u)) for u in uids]


async def describe(svc, chat_id: int, ring: SR.Ring, skip: int | None = None) -> tuple[str, list[int]]:
    """무리 설명 (이 방 사람은 이름, 다른 방은 숫자만) + 이 방에 지금 있는 사람."""
    others = ring.members - {skip} if skip else ring.members
    here = await SR.present(svc.db, chat_id, others)
    rooms = await SR.rooms_of(svc.db, others)
    elsewhere = len([c for c in rooms if c != chat_id])
    lines = [f"🕸️ <b>{'강한 ' if ring.strong else ''}연결 무리</b> · {len(ring.members)}명 · {esc(ring.kinds())}"]
    lines += [f"• {esc(SR.KIND_LABEL[k])} {esc(v)} — {len(us)}명" for k, v, us in ring.evidence[:6]]
    if ring.flagged:
        lines.append(f"🚫 밴·공동 차단 기록 있는 사람 {len(ring.flagged)}명")
    lines.append(f"\n<b>이 방에 지금 {len(here)}명</b>: " + (", ".join(await _names(svc.db, here)) or "없음"))
    if elsewhere:
        lines.append(f"(다른 방 {elsewhere}곳에도 있어요)")
    lines.append(f"<i>지난 {SR.DAYS}일 기록 기준 · 추정이에요 — 같은 사람이 여러 계정인지, 같은 글을 퍼 나른 건지 확인해 주세요.</i>")
    return "\n".join(lines), here


async def targets(c: PanelCtx, ring: SR.Ring) -> list[int]:
    """이 방에서 밴할 수 있는 사람: 지금 있음 · 누른 사람·관리자·봇·자유 멤버 아님."""
    out = []
    for u in await SR.present(c.svc.db, c.cid, ring.members):
        if u != c.uid and not await c.svc.perms.protected(c.bot, c.cid, u) and not await free.is_free(c.svc.db, c.cid, u):
            out.append(u)
    return out


# ── 화면 ─────────────────────────────────────────────────
async def s_rgl(c: PanelCtx) -> Screen:
    rings = await SR.rings_in(c.svc.db, c.cid, await c.svc.perms.owners())
    lines = ["🕸️ <b>사기 무리</b>", f"같은 지갑·초대링크·똑같은 긴 글로 이어진 계정 묶음 (지난 {SR.DAYS}일, 우리 봇이 있는 방 전체).",
             "자동 제재는 안 해요. 밴이 생기면 같은 무리가 남아 있는 방 관리자에게 알려드려요.", ""]
    rows = []
    for i, ring in enumerate(rings[:MAX_LIST], 1):
        here = await SR.present(c.svc.db, c.cid, ring.members)
        lines.append(f"{i}. {len(ring.members)}명 (이 방 {len(here)}명) · {esc(ring.kinds())}"
                     + (" · 🚫 기록" if ring.flagged else ""))
        rows.append([B(f"🔍 {i}", f"m:rng:{c.cid}:{here[0]}")])
    if not rings:
        lines.append("지금은 이 방에 걸린 무리가 없어요.")
    return Screen("\n".join(lines), _kb([*rows, _back(c.cid)]))


async def _ring(c: PanelCtx) -> tuple[int | None, SR.Ring | None]:
    uid = to_int(c.arg(0))
    g = await SR.graph(c.svc.db, await c.svc.perms.owners())
    return uid, (g.get(uid) if uid is not None else None)


async def s_rng(c: PanelCtx) -> Screen:
    uid, ring = await _ring(c)
    if ring is None:
        return Screen("🕸️ 이 사람은 지금 무리 기록이 없어요 (지난 기록 기준).", _kb([[B("⬅️ 목록", f"m:rgl:{c.cid}")]]))
    text, _ = await describe(c.svc, c.cid, ring)
    can = await targets(c, ring)
    rows = [[B(f"🚫 이 방에서 {len(can)}명 밴", f"m:rngb:{c.cid}:{uid}")]] if can else []
    return Screen(text, _kb([*rows, [B("⬅️ 목록", f"m:rgl:{c.cid}")]]))


async def s_rngb(c: PanelCtx) -> Screen:
    uid, ring = await _ring(c)
    here = await targets(c, ring) if ring else []
    if not here:
        return await s_rng(c)
    return Screen(f"🚫 이 방에서 {len(here)}명을 밴할까요?\n" + ", ".join(await _names(c.svc.db, here)) +
                  "\n(관리자·자유 멤버는 빼고, 누를 때 권한을 다시 확인해요)",
                  _kb([[B("🚫 밴", f"m:rngx:{c.cid}:{uid}"), B("❌ 취소", f"m:rng:{c.cid}:{uid}")]]))


async def r_rngx(c: PanelCtx) -> Screen:
    svc = c.svc
    if not await may(svc.perms, c.bot, c.cid, c.uid, "restrict"):
        return Screen(None, toast="'사용자 차단' 권한이 있는 관리자만 할 수 있어요.", alert=True)
    uid = to_int(c.arg(0))
    ring = (await SR.graph(svc.db, await svc.perms.owners(), fresh=True)).get(uid) if uid is not None else None
    if ring is None or not await persist.claim(svc.db, f"rngx:{c.cid}:{ring.root}", 60):
        return Screen(None, toast="이미 처리 중이거나 무리 기록이 없어요.")
    done = []
    for u in await targets(c, ring):
        try:
            await svc.mod.ban(c.bot, c.cid, u, c.uid, "🕸️ 사기 무리")
            done.append(u)
        except Exception as e:   # 권한 없음·이미 나감 등 → 다른 사람은 계속
            log.warning("ring ban failed %s/%s: %s", c.cid, u, e)
    SR.invalidate(svc.db)
    screen = await s_rng(c)
    screen.toast = f"{len(done)}명 밴했어요." if done else "밴할 사람이 없었어요."
    return screen


# ── 밴 → 무리 알림 ────────────────────────────────────────
async def on_tick(svc, bot) -> None:
    bans = await SR.new_bans(svc.db)
    if not bans:
        return
    g = await SR.graph(svc.db, await svc.perms.owners(), fresh=True)
    done = set()
    for b in bans:
        ring = g.get(b["target_id"])
        if ring is None or not ring.alertable or ring.root in done:
            continue
        done.add(ring.root)
        for cid in await SR.rooms_of(svc.db, ring.members - {b["target_id"]}):
            try:
                await alert(svc, bot, cid, ring, b["target_id"])
            except Exception:
                log.exception("ring alert failed chat=%s", cid)


async def alert(svc, bot, chat_id: int, ring: SR.Ring, banned: int) -> None:
    from ..anomaly import recipients                       # 늦게 import (순환 방지)
    from ..subscription import chat_title
    body, here = await describe(svc, chat_id, ring, skip=banned)
    if not here:
        return
    who = (await svc.db.first_name(banned)) or str(banned)
    text = (f"🕸️ <b>{esc(await chat_title(svc, chat_id))}</b>\n방금 밴된 {mention(banned, who)} 과(와) 이어진 계정이 이 방에 있어요.\n\n"
            + body + "\n자동으로 제재하지 않았어요.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚫 이 방에서 {len(here)}명 밴", callback_data=f"m:rngb:{chat_id}:{here[0]}")],
                               [InlineKeyboardButton("🔍 자세히", callback_data=f"m:rng:{chat_id}:{here[0]}")]])
    await incidents.open_or_bump(svc, bot, chat_id, "ring", ring.root, text, kb, await recipients(svc, bot, chat_id))
    await svc.db.audit(chat_id, None, banned, "scamring_alert", f"무리 {len(ring.members)}명 · 이 방 {len(here)}명")


# ── AI 도구 ──────────────────────────────────────────────
async def t_scam_ring(ctx: tools.ToolCtx, a: dict) -> str:
    row, err = await tools._resolve(ctx, str(a.get("name", "")))
    if err:
        return err
    ring = (await SR.graph(ctx.svc.db, await ctx.svc.perms.owners())).get(row["user_id"])
    if ring is None:
        return f"{row['first_name']} 님은 지난 {SR.DAYS}일 기록에서 다른 계정과 이어진 무리가 없음 (같은 지갑·초대링크·똑같은 긴 글 기준)."
    ctx.tainted = True   # 가린 글 조각도 멤버가 쓴 데이터
    here = await SR.present(ctx.svc.db, ctx.chat_id, ring.members - {row["user_id"]})
    rooms = await SR.rooms_of(ctx.svc.db, ring.members)
    names = [(await ctx.svc.db.first_name(u)) or str(u) for u in here]
    ev = "; ".join(f"{SR.KIND_LABEL[k]} {v} ({len(us)}명)" for k, v, us in ring.evidence[:5])
    return (f"무리 {len(ring.members)}명 ({'강한 연결' if ring.strong else '약한 연결'}) · 증거: {ev} · "
            f"이 방에 같이 있는 사람: {', '.join(names) or '없음'} · 다른 방 {len([c for c in rooms if c != ctx.chat_id])}곳 · "
            f"밴·공동 차단 기록 {len(ring.flagged)}명. 추정이라 단정하지 말 것. 제재는 관리자가 원하면 기존 확인 버튼으로, "
            f"한 번에 보려면 관리자 1:1 메뉴 🕸️ 사기 무리.")


menu.register_hub(HubItem(43, "rgl", "🕸️ 사기 무리", TG_ADMIN))
menu.register_screen("rgl", s_rgl, need=TG_ADMIN)
menu.register_route("rng", Route(s_rng, TG_ADMIN))
menu.register_route("rngb", Route(s_rngb, TG_ADMIN))
menu.register_route("rngx", Route(r_rngx, TG_ADMIN, fresh=True))
hooks.add_tick_hook(on_tick)
tools.register_tool(tools.Tool(
    "scam_ring",
    "[관리자] 멤버 한 사람이 다른 계정들과 같은 지갑·초대링크·똑같은 긴 글로 이어진 '무리'인지 본다 (우리 봇이 있는 방 전체, "
    "지난 30일). '이 사람 다른 계정 있어?', '같은 패거리 누구야' 같은 요청. 읽기만 함.",
    {"name": {"type": "string", "description": "멤버 이름·@아이디·ID"}}, ["name"], t_scam_ring, Role.ADMIN, where="room"),
    read_only=True)
