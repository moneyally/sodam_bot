"""📢 채널 광고 자동순환 화면 (그룹 허브). 동작은 sodam/adrotate.py.

m:adr:<방>          켜기/끄기 · 간격 · 개수 · 순서 · 지우기 · 미리보기
m:in:<방>:adrm      간격 직접 입력(분)
AI 도구 ad_rotate(action on/off/status, …)
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import adrotate, channel, menu, tools
from ..menu import B, HubItem, PanelCtx, Route, Screen
from ..permissions import Role
from ..util import esc, to_int


def _dot(on: bool) -> str:
    return "• " if on else ""


async def s_adr(c: PanelCtx) -> Screen:
    r = await adrotate.get(c.svc, c.cid)
    ch = await adrotate.linked_channel(c.svc, c.cid)
    n_ads = len(await adrotate.ad_ids(c.svc, ch["chat_id"])) if ch else 0
    on = bool(r and r["enabled"])
    iv = r["interval_min"] if r else adrotate.DEFAULT_MIN
    per = r["per_tick"] if r else 1
    order = r["order_mode"] if r else "seq"
    delp = bool(r and r["del_prev"])

    lines = ["📢 <b>채널 광고 자동순환</b>",
             "연결된 채널 글을 방에 <b>정한 간격마다 자동으로</b> 올려요 (광고 돌리기).",
             "채널에 올리기만 하면 돼요 — 소담이 채널 관리자로 <b>들어온 뒤</b> 올라온 글만 돌아가요.", ""]
    if not ch:
        lines.append("⚠️ <b>이 방에 연결된 채널이 없어요.</b>\n"
                     "먼저 1:1 → 📢 내 채널 → 🔗 연결된 방 에서 이 방을 연결해 주세요.")
    else:
        lines.append(f"채널: <b>{esc(ch['title'] or '-')}</b> · 돌릴 글: <b>{n_ads}개</b> 감지")
        if n_ads == 0:
            lines.append("<i>채널에 광고를 올리면 여기 잡혀요 (이미 있던 과거 글은 한 번 다시 올려야 보여요).</i>")
    lines += ["", f"지금: <b>{'✅ 켜짐' if on else '❌ 꺼짐'}</b> · 간격 <b>{iv}분</b> · 한 번에 <b>{per}개</b> · "
              f"<b>{'순서대로' if order == 'seq' else '랜덤'}</b> · {'🗑 지우고 새로' if delp else '📚 쌓기'}"]

    cid = c.cid
    rows = [
        [B("❌ 끄기" if on else "✅ 켜기", f"m:adrx:{cid}:{0 if on else 1}")],
        [B(f"{_dot(iv == 15)}15분", f"m:adri:{cid}:15"), B(f"{_dot(iv == 30)}30분", f"m:adri:{cid}:30"),
         B(f"{_dot(iv == 60)}1시간", f"m:adri:{cid}:60"), B(f"{_dot(iv == 120)}2시간", f"m:adri:{cid}:120")],
        [B("✏️ 간격 직접 입력", f"m:in:{cid}:adrm")],
        [B(f"{_dot(per == 1)}1개", f"m:adrp:{cid}:1"), B(f"{_dot(per == 2)}2개", f"m:adrp:{cid}:2"),
         B(f"{_dot(per == 3)}3개", f"m:adrp:{cid}:3")],
        [B(f"{_dot(order == 'seq')}순서대로", f"m:adro:{cid}:seq"),
         B(f"{_dot(order == 'random')}랜덤", f"m:adro:{cid}:random")],
        [B(f"{_dot(delp)}🗑 지우고 새로", f"m:adrd:{cid}:1"), B(f"{_dot(not delp)}📚 쌓기", f"m:adrd:{cid}:0")],
        [B("👀 지금 하나 보내보기", f"m:adrv:{cid}")],
        menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


async def r_toggle(c: PanelCtx) -> Screen:
    if to_int(c.arg(0)) and not await adrotate.linked_channel(c.svc, c.cid):
        return await s_adr(c)                 # 연결 채널 없으면 못 켬 (화면이 안내)
    await adrotate.save(c.svc, c.cid, enabled=1 if to_int(c.arg(0)) else 0)
    return await s_adr(c)


async def r_interval(c: PanelCtx) -> Screen:
    v = to_int(c.arg(0)) or adrotate.DEFAULT_MIN
    await adrotate.save(c.svc, c.cid, interval_min=max(adrotate.MIN_MIN, min(v, adrotate.MAX_MIN)))
    return await s_adr(c)


async def r_per(c: PanelCtx) -> Screen:
    await adrotate.save(c.svc, c.cid, per_tick=max(1, min(to_int(c.arg(0)) or 1, adrotate.MAX_PER)))
    return await s_adr(c)


async def r_order(c: PanelCtx) -> Screen:
    await adrotate.save(c.svc, c.cid, order_mode="random" if c.arg(0) == "random" else "seq")
    return await s_adr(c)


async def r_delprev(c: PanelCtx) -> Screen:
    await adrotate.save(c.svc, c.cid, del_prev=1 if to_int(c.arg(0)) else 0)
    return await s_adr(c)


async def r_preview(c: PanelCtx) -> Screen:
    """👀 지금 하나 보내보기 — 상태는 안 바꾸고(순환 안 건너뛰게) 다음 광고 하나를 방에 forward."""
    ch = await adrotate.linked_channel(c.svc, c.cid)
    if not ch:
        return Screen(None, toast="연결된 채널이 없어요", alert=True)
    ids = await adrotate.ad_ids(c.svc, ch["chat_id"])
    if not ids:
        return Screen(None, toast="채널에서 잡힌 글이 없어요", alert=True)
    r = await adrotate.get(c.svc, c.cid)
    batch = adrotate.pick(ids, r["last_ad"] if r else None, 1, r["order_mode"] if r else "seq")
    try:
        await c.bot.forward_message(chat_id=c.cid, from_chat_id=ch["chat_id"], message_id=batch[0])
    except TelegramError:
        return Screen(None, toast="못 보냈어요 (채널 글·소담 권한 확인)", alert=True)
    return Screen(None, toast="✅ 하나 보냈어요")


async def in_interval(c: PanelCtx, msg) -> tuple[bool, str]:
    v = to_int((msg.text or "").strip().replace("분", ""))
    if v is None or not (adrotate.MIN_MIN <= v <= adrotate.MAX_MIN):
        return False, f"❌ {adrotate.MIN_MIN}~{adrotate.MAX_MIN} 사이 분(分)으로 보내 주세요. 예: <code>30</code>"
    await adrotate.save(c.svc, c.cid, interval_min=v)
    return True, f"✅ 자동순환 간격을 <b>{v}분</b>으로 정했어요."


# ── AI 도구 ───────────────────────────────────────────────
async def t_ad_rotate(ctx: tools.ToolCtx, a: dict) -> str:
    action = (a.get("action") or "status").lower()
    cid = ctx.chat_id
    ch = await adrotate.linked_channel(ctx.svc, cid)
    r = await adrotate.get(ctx.svc, cid)
    if action in ("on", "start", "켜기", "켬"):
        if not ch:
            return ("이 방에 연결된 채널이 없어서 못 켬. '1:1 → 📢 내 채널 → 🔗 연결된 방 에서 이 방을 먼저 연결해 달라'고 안내.")
        f: dict = {"enabled": 1}
        if a.get("interval_min"):
            f["interval_min"] = max(adrotate.MIN_MIN, min(int(a["interval_min"]), adrotate.MAX_MIN))
        if a.get("per_tick"):
            f["per_tick"] = max(1, min(int(a["per_tick"]), adrotate.MAX_PER))
        if a.get("order") in ("seq", "random"):
            f["order_mode"] = a["order"]
        if "del_prev" in a:
            f["del_prev"] = 1 if a["del_prev"] else 0
        await adrotate.save(ctx.svc, cid, **f)
        n = len(await adrotate.ad_ids(ctx.svc, ch["chat_id"]))
        iv = f.get("interval_min", r["interval_min"] if r else adrotate.DEFAULT_MIN)
        per = f.get("per_tick", r["per_tick"] if r else 1)
        extra = " 채널에 올라온 글이 아직 없어서, 올리면 그때부터 돌아간다고 덧붙여." if n == 0 else ""
        return f"채널 광고 자동순환 켰음 ({iv}분마다 {per}개씩, 채널 글 {n}개 감지).{extra} 짧게 알려."
    if action in ("off", "stop", "끄기", "끔"):
        await adrotate.save(ctx.svc, cid, enabled=0)
        return "채널 광고 자동순환 껐음. 짧게 알려."
    if not r or not r["enabled"]:
        return "채널 광고 자동순환은 지금 꺼져 있음. '켜줘' 하면 켤 수 있다고 짧게 안내."
    n = len(await adrotate.ad_ids(ctx.svc, ch["chat_id"])) if ch else 0
    return (f"채널 광고 자동순환 켜짐: {r['interval_min']}분마다 {r['per_tick']}개씩 "
            f"{'순서대로' if r['order_mode'] == 'seq' else '랜덤'}, 감지된 채널 글 {n}개. 짧게 요약.")


menu.register_hub(HubItem(26, "adr", "📢 채널 광고 자동순환"))
menu.register_screen("adr", s_adr)
menu.register_route("adrx", Route(r_toggle))
menu.register_route("adri", Route(r_interval))
menu.register_route("adrp", Route(r_per))
menu.register_route("adro", Route(r_order))
menu.register_route("adrd", Route(r_delprev))
menu.register_route("adrv", Route(r_preview, fresh=True))
menu.register_input("adrm", "📢 광고를 <b>몇 분마다</b> 자동으로 올릴까요? (분) 예: <code>30</code> "
                    f"({adrotate.MIN_MIN}~{adrotate.MAX_MIN})", "adr", in_interval, s_adr)
tools.register_tool(tools.Tool(
    "ad_rotate",
    "[관리자] 연결된 채널의 광고(공지) 글을 방에 정한 간격마다 자동으로 하나씩 순환 홍보한다 (채널 광고 자동순환). "
    "'채널 광고 30분마다 자동으로 돌려줘'·'공지 자동홍보 켜줘'·'광고 순환 꺼줘'·'자동순환 상태' 처럼. "
    "채널에 올리기만 하면 자동(소담이 그 채널 관리자로 들어온 뒤 글만). 그룹방에서만.",
    {"action": {"type": "string", "enum": ["on", "off", "status"]},
     "interval_min": {"type": "integer", "description": "간격(분, 5~1440, 기본 30)"},
     "per_tick": {"type": "integer", "description": "한 번에 올릴 광고 수(1~5, 기본 1)"},
     "order": {"type": "string", "enum": ["seq", "random"], "description": "seq=순서대로, random=무작위"},
     "del_prev": {"type": "boolean", "description": "true면 새로 올릴 때 지난 광고 삭제(도배 방지)"}},
    ["action"], t_ad_rotate, Role.ADMIN, where="room"))
