"""📥 처리할 일 · 🧭 운영센터 · 💸 비용 예측 버튼 화면 (사실 모으기·계산은 sodam/opsdesk.py, AI 호출 없음).

m:ib[:쪽]                      메인 📥 (누구나 누를 수 있지만 '지금 텔레그램 관리자인 방'만 모음 → 멤버·봇관리자는 빈 화면)
m:ibr:<방>[:쪽]                그룹 허브 📥 (TG_ADMIN) 그 방만
m:ibh:<방>:<키>:<a|r>:<쪽>     ✓ 숨기기 (TG_ADMIN · 키가 지금 그 방 항목에 있어야 함 → 위조·두 번 누름은 안내만) → a=m:ib / r=m:ibr
m:opc[:<a|n|s>[:쪽]]            오너 메인 🧭 모든 방 (전체 / 확인 필요 / 오늘 보안 이벤트)
m:opcr:<방>:<a|n|s>:<쪽>         오너: 방 하나 자세히 → 📥 그 방 처리할 일 · ⚙️ 방 설정 · 🏠 요금제
m:opf                           오너 메인 💸 이번 달 AI 요금 예측 (금액) · 많이 쓸 곳 상위 5 → m:alq 방 요금제
m:fcr:<방>                     그룹 허브 💸 (ADMIN) 이 방 이번 달 한도의 % 만 (금액 없음 — panels/agentlog 와 같은 규칙)
방 이름·항목 글은 멤버가 쓴 데이터라 전부 esc.
"""
from __future__ import annotations

import time
from functools import wraps

from .. import costs, menu, opsdesk
from ..menu import ADMIN, OWNER, PUBLIC, TG_ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..subscription import chat_title
from ..util import esc, to_int

PAGE = 6                 # 📥 한 쪽 항목 수 (항목마다 버튼 한 줄)
ROOM_PAGE = 8            # 🧭 한 쪽 방 수
REASONS_CHARS = 160
FILTER_CODE = {"a": "all", "n": "attention", "s": "security"}
NOT_OWNER = Screen(None, toast="봇 오너만 볼 수 있어요.", alert=True)


def _owner_only(fn):
    @wraps(fn)
    async def wrapped(c: PanelCtx) -> Screen:
        if c.uid not in await c.svc.perms.owners():   # 라우터 확인과 이중으로
            return NOT_OWNER
        return await fn(c)
    return wrapped


def _page(raw: str) -> int:
    return min(max(to_int(raw or "0") or 0, 0), 999)


def _pages(n: int, size: int) -> int:
    return max(1, -(-n // size))


def _nav(prefix: str, page: int, pages: int) -> list:
    row = []
    if page > 0:
        row.append(B("◀ 이전", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        row.append(B("다음 ▶", f"{prefix}:{page + 1}"))
    return row


# 방 전체를 다시 세는 화면(📥·🧭)은 쪽 넘김·필터마다 같은 계산(방 72개 = DB 1천 번↑)을 반복했음 → 사람마다 CACHE_TTL 초 기억 (감사 S2).
# 숨기기는 그 사람 캐시를 비워 바로 반영. 버튼·권한은 누를 때마다 그대로 확인 (캐시는 보여 줄 목록만).
CACHE_TTL = 30


async def _cached(c: PanelCtx, key: tuple, make, reuse: bool = True):
    """reuse=False(화면을 처음 열 때) 면 새로 계산해 담고, 쪽 넘김·필터에서만 담아 둔 걸 씀 → 새 일은 바로 보임."""
    cache = c.svc.__dict__.setdefault("_ops_cache", {})
    now, hit = time.monotonic(), cache.get(key)
    if reuse and hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    val = await make()
    if len(cache) > 500:
        cache.clear()
    cache[key] = (now, val)
    return val


def _forget(c: PanelCtx) -> None:
    cache = c.svc.__dict__.get("_ops_cache", {})
    for k in [k for k in cache if k[0] == c.uid]:
        del cache[k]


# ── 📥 처리할 일 ─────────────────────────────────────────
async def _inbox(c: PanelCtx, rooms: list[tuple[int, str]], scope: str, page: int) -> Screen:
    svc = c.svc
    room = c.cid if scope == "r" else None
    items = await _cached(c, (c.uid, "ib", tuple(r for r, _ in rooms)), lambda: opsdesk.inbox(svc, c.bot, c.uid, rooms),
                          reuse=page > 0)
    pages = _pages(len(items), PAGE)
    page = min(page, pages - 1)
    head = "📥 <b>처리할 일</b>" + (f" · <b>{esc(rooms[0][1])}</b>" if room else "")
    here = f"m:ibr:{room}" if room else "m:ib"
    back = menu._back(room) if room else [B("⬅️ 처음으로", "m:home")]
    if not rooms:
        return Screen(head + "\n\n지금 텔레그램 관리자인 방이 없어요.\n소담이 있는 방의 관리자 계정으로 열어주세요.",
                      menu._kb([back]))
    lines = [head + (f" ({page + 1}/{pages}쪽 · {len(items)}개)" if pages > 1 else ""),
             "지금 상태 먼저, 그다음 기록을 최신순으로 보여줘요 (우선순위 점수는 매기지 않아요).",
             "✓ 숨기기는 나에게만 적용돼요. 새 일이 생기면 다시 보여요.", ""]
    rows = []
    if not items:
        lines.append(f"처리할 일이 없어요 ✨ ({len(rooms)}개 방 확인)")
    shown = items[page * PAGE:(page + 1) * PAGE]
    for n, it in enumerate(shown, page * PAGE + 1):
        lines.append(f"{n}. {esc(opsdesk.item_line(it, svc, room is None))}")
        btns = [B(f"🔎 {n} 열기", it.open)] if it.open else []
        btns.append(B(f"✓ {n} 숨기기", f"m:ibh:{it.chat_id}:{it.key}:{scope}:{page}"))
        rows.append(btns)
    if nav := _nav(here, page, pages):
        rows.append(nav)
    rows.append([B("🔄 새로고침", f"{here}:{page}")] + back)
    return Screen("\n".join(lines), menu._kb(rows))


async def s_inbox(c: PanelCtx) -> Screen:
    return await _inbox(c, await opsdesk.tg_admin_rooms(c.svc, c.bot, c.uid), "a", _page(c.arg(0)))


async def s_room_inbox(c: PanelCtx) -> Screen:
    return await _inbox(c, [(c.cid, await chat_title(c.svc, c.cid))], "r", _page(c.arg(0)))


async def r_hide(c: PanelCtx) -> Screen:
    key, scope, page = c.arg(0), c.arg(1), _page(c.arg(2))
    if not opsdesk.KEY_RE.fullmatch(key) or scope not in ("a", "r"):
        return Screen(None, toast="만료된 버튼이에요. 메뉴를 다시 열어주세요.", alert=True)
    keys = {it.key for it in await opsdesk.room_items(c.svc, c.bot, c.cid, c.uid)}
    if key not in keys:                       # 이미 처리됐거나 위조된 키 → 숨길 것 없음 (DB 안 씀)
        toast = "이미 처리됐거나 없는 항목이에요."
    else:
        toast = "✓ 숨겼어요." if await opsdesk.hide(c.svc.db, c.uid, c.cid, key) else "이미 숨긴 항목이에요."
        _forget(c)
    screen = await (s_room_inbox if scope == "r" else s_inbox)(PanelCtx(c.svc, c.bot, c.uid, c.cid, [str(page)]))
    screen.toast = toast
    return screen


# ── 🧭 운영센터 (오너) ────────────────────────────────────
def _filter(raw: str) -> str:
    return raw if raw in FILTER_CODE else "a"


@_owner_only
async def s_center(c: PanelCtx) -> Screen:
    svc, f = c.svc, _filter(c.arg(0))
    statuses, tot = await _cached(c, (c.uid, "opc"), lambda: opsdesk.command_center(svc, c.bot, c.uid), reuse=bool(c.args))
    shown = opsdesk.narrow(statuses, FILTER_CODE[f])
    pages = _pages(len(shown), ROOM_PAGE)
    page = min(_page(c.arg(1)), pages - 1)
    att, sec = sum(1 for s in statuses if s.attention), sum(1 for s in statuses if s.security)
    subs = f" · 구독 {tot['paid']} · 체험 {tot['trial']}" if tot["billing"] else ""
    lines = ["🧭 <b>운영센터</b>" + (f" ({page + 1}/{pages}쪽)" if pages > 1 else ""),
             f"방 {tot['rooms']} · ⚠️ 확인 필요 {att} · 🚨 오늘 보안 이벤트 {sec}",
             f"💵 오늘 AI 요금 {costs.fmt_usd(tot['usd'])}{subs}",
             "기록된 사실로만 나눠요 (확인 전 알림·실패한 예약·AI 사용량 80%↑·봇 권한·이용 기간·급증 / 오늘 이상징후·사기 의심·CAS 차단).",
             ""]
    rows = [[B(("● " if f == k else "") + label, f"m:opc:{k}:0")
             for k, label in (("a", f"전체 {len(statuses)}"), ("n", f"⚠️ {att}"), ("s", f"🚨 {sec}"))]]
    if not statuses:
        lines.append("봇이 들어간 그룹이 아직 없어요.")
    elif not shown:
        lines.append("해당하는 방이 없어요 ✅")
    btns = []
    for n, s in enumerate(shown[page * ROOM_PAGE:(page + 1) * ROOM_PAGE], page * ROOM_PAGE + 1):
        why = " · ".join(s.security + s.attention)
        if len(why) > REASONS_CHARS:
            why = why[:REASONS_CHARS - 1] + "…"
        lines.append(f"{n}. <b>{esc(s.title[:30])}</b> {s.label}" + (f"\n    {esc(why)}" if why else ""))
        btns.append(B(f"{n}. {s.title[:20]}", f"m:opcr:{s.chat_id}:{f}:{page}"))
    rows += menu._chunks(btns, 2)
    if nav := _nav(f"m:opc:{f}", page, pages):
        rows.append(nav)
    rows += [[B("💸 비용 예측", "m:opf"), B("📒 AI 비용", "m:al")],
             [B("🔄 새로고침", f"m:opc:{f}:{page}"), B("⬅️ 처음으로", "m:home")]]
    return Screen("\n".join(lines), menu._kb(rows))


@_owner_only
async def s_center_room(c: PanelCtx) -> Screen:
    raw = c.arg(0)
    if not menu.CID_RE.fullmatch(raw or "") or not await c.svc.db.has_chat(int(raw)):
        return Screen(None, toast="없는 그룹이에요. 목록을 다시 열어주세요.", alert=True)
    cid, f, page = int(raw), _filter(c.arg(1)), _page(c.arg(2))
    title = await chat_title(c.svc, cid)
    s = await opsdesk.room_status(c.svc, c.bot, cid, title, c.uid, int(time.time()))
    lines = [f"🧭 <b>{esc(title)}</b> (<code>{cid}</code>)", s.label, ""]
    if s.security:
        lines += ["<b>🚨 오늘 보안 이벤트</b>"] + [f"• {esc(x)}" for x in s.security] + [""]
    if s.attention:
        lines += ["<b>⚠️ 확인 필요</b>"] + [f"• {esc(x)}" for x in s.attention[:30]]
    if not (s.security or s.attention):
        lines.append("특이사항 없어요 ✅")
    rows = [[B("📥 처리할 일", f"m:ibr:{cid}"), B("⚙️ 방 설정", f"m:g:{cid}")],
            [B("🏠 요금제", f"m:alq:{cid}"), B("⬅️ 운영센터", f"m:opc:{f}:{page}")]]
    return Screen("\n".join(lines), menu._kb(rows))


# ── 💸 비용 예측 ─────────────────────────────────────────
@_owner_only
async def s_forecast(c: PanelCtx) -> Screen:
    svc = c.svc
    f = await opsdesk.forecast(svc)
    lines = ["💸 <b>이번 달 AI 요금 예측</b>"] + [esc(x) for x in opsdesk.forecast_lines(f)]
    top = (await opsdesk.room_forecasts(svc))[:5]
    btns = []
    if top:
        titles = {r["chat_id"]: r["title"] for r in await svc.db._all("SELECT chat_id, title FROM chats")}
        lines += ["", f"<b>많이 쓸 곳 상위 {len(top)}</b> (이 속도면 이번 달 · 방별 기록은 {opsdesk.USD_SINCE}부터)"]
        for n, r in enumerate(top, 1):
            name = titles.get(r.chat_id) or (f"1:1 {r.chat_id}" if r.chat_id > 0 else str(r.chat_id))
            cap = f" · 방 한도 합계 {costs.fmt_usd(r.cap_month)}의 {r.pct}%" if r.chat_id < 0 else ""
            lines.append(f"{n}. {esc(name[:24])} 지금까지 {costs.fmt_usd(r.mtd)} → 약 {costs.fmt_usd(r.projected)}{cap}")
            if r.chat_id < 0:
                btns.append(B(f"🏠 {name[:18]}", f"m:alq:{r.chat_id}"))
    elif not f.mtd[1]:
        lines += ["", "이번 달은 아직 AI 요금 기록이 없어요."]
    lines.append("\n요금은 모델별 요금표(sodam/costs.py)로 계산한 추정치예요.")
    rows = menu._chunks(btns, 2) + [[B("🧭 운영센터", "m:opc"), B("📒 AI 비용", "m:al")],
                                    [B("🔄 새로고침", "m:opf"), B("⬅️ 처음으로", "m:home")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def s_room_forecast(c: PanelCtx) -> Screen:
    """방 관리자: 금액 없이 % 만 (오너도 이 화면에선 같은 %, 금액은 💸 메인 화면)."""
    [rf] = await opsdesk.room_forecasts(c.svc, only=c.cid)
    f = await opsdesk.forecast(c.svc)
    left = f.days_in_month - f.day_no
    lines = ["💸 <b>이번 달 AI 사용 예측</b>", f"<b>{esc(await chat_title(c.svc, c.cid))}</b> · {f.month} ({f.day_no}/{f.days_in_month}일째)",
             f"지금까지: 이번 달 한도(하루 한도 × {f.days_in_month}일)의 <b>{rf.mtd_pct}%</b>",
             f"이 속도면 이번 달: 약 <b>{rf.pct}%</b> (지금까지 + 최근 하루 평균 × 남은 {left}일)"]
    if not rf.avg_days:
        lines.append("(지난 기록이 없어서 오늘 사용량을 하루치로 봤어요)")
    if rf.pct >= 100:
        lines.append("⚠️ 이 속도면 한도를 다 쓰는 날이 생겨요 — 그날은 자정까지 AI 가 쉬어요 (관리·게임은 그대로).")
    lines.append(f"\n방별 기록은 {opsdesk.USD_SINCE}부터예요. 한도 % 는 📒 AI 작업 기록에서 줄일 수 있어요.")
    return Screen("\n".join(lines), menu._kb([[B("📒 AI 작업 기록", f"m:alg:{c.cid}")], menu._back(c.cid)]))


# ── 등록 ──────────────────────────────────────────────────
menu.register_main(15, "ib", "📥 처리할 일", ADMIN)   # 관리하는 방이 없는 사람에겐 빈 화면이라 숨김
menu.register_main(91, "opc", "🧭 운영센터", OWNER)
menu.register_main(93, "opf", "💸 비용 예측", OWNER)
menu.register_route("ib", Route(s_inbox, PUBLIC, scoped=False))
menu.register_hub(HubItem(9, "ibr", "📥 처리할 일", TG_ADMIN))
menu.register_screen("ibr", s_room_inbox, TG_ADMIN)
menu.register_route("ibh", Route(r_hide, TG_ADMIN))
for _code, _fn in (("opc", s_center), ("opcr", s_center_room), ("opf", s_forecast)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_hub(HubItem(47, "fcr", "💸 AI 사용 예측"))
menu.register_screen("fcr", s_room_forecast)
