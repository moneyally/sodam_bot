"""📒 AI 작업 기록 · 💵 AI 비용 (sodam/agentlog.py 기록 + sodam/costs.py 예산·방 한도).

오너 (메인 메뉴 📒, 방 없는 라우트 → 핸들러마다 _owner_only):
  m:al                   오늘 요금·하루 예산·많이 쓴 방
  m:alr:<쪽>             최근 AI 작업 (모든 방) → m:alv:<id>:<쪽> 상세 (요금 포함)
  m:alp:<쪽>             방 요금제 목록 → m:alq:<방ID> 방 요금제 화면 → m:alqs:<방ID>:<센트> (정해진 값만, 목표값이라 두 번 눌려도 같음)
방 관리자 (그룹 허브 📒, 방 단위 → 라우터가 관리자 확인):
  m:alg:<방ID>[:쪽]      이 방 AI 사용량(한도의 %만, 금액 없음) · 사용 한도 % 프리셋(m:n, 줄이기만) · 이 방 작업 목록
  m:agv:<방ID>:<id>:<쪽> 상세 (이 방 것만, 금액 없음)
저장된 요청·도구 결과는 멤버 글이라 전부 esc 해서 보여준다.
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from .. import agentlog, costs, menu, mistakes  # noqa: F401 (mistakes = 🧠 오너 하루 실수 보고 tick 등록)
from ..ai_settings import ROOM_TOKENS_MAX
from ..llm import ROOM_TOKENS
from ..menu import OWNER, B, HubItem, PanelCtx, Route, Screen
from ..util import esc, fmt_time, to_int

PAGE = 8
STEPS_SHOWN = 8
NOT_OWNER = Screen(None, toast="봇 오너만 볼 수 있어요.", alert=True)

menu.register_preset(costs.PCT_KEY, [(v, f"한도 {v}%") for v in ("30", "50", "70", "100")], "alg")


def _owner_only(fn):
    @wraps(fn)
    async def wrapped(c: PanelCtx) -> Screen:
        if c.uid not in await c.svc.perms.owners():   # 누를 때마다 확인 (라우터 확인과 이중으로)
            return NOT_OWNER
        return await fn(c)
    return wrapped


def _page(raw: str) -> int:
    n = to_int(raw or "0")
    return min(max(n or 0, 0), 999)


def _today(svc) -> str:
    return datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")


def _pct(used: int, cap: int) -> int:
    return used * 100 // max(cap, 1)


async def _titles(svc, ids) -> dict[int, str]:
    ids = sorted({i for i in ids if i is not None})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = await svc.db._all(f"SELECT chat_id, title FROM chats WHERE chat_id IN ({marks})", tuple(ids))
    return {r["chat_id"]: r["title"] or str(r["chat_id"]) for r in rows}


def _where(titles: dict[int, str], cid: int) -> str:
    return titles.get(cid) or (f"1:1 {cid}" if cid > 0 else str(cid))


def _page_row(prefix: str, page: int, pages: int) -> list:
    row = []
    if page > 0:
        row.append(B("◀ 이전", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        row.append(B("다음 ▶", f"{prefix}:{page + 1}"))
    return row


def _run_line(svc, r, titles: dict[int, str] | None, money: bool) -> str:
    where = f" · {esc(_where(titles, r['chat_id'])[:18])}" if titles is not None else ""
    cost = f" · {costs.fmt_usd(r['usd_micro'], 4)}" if money else ""
    tools = len(agentlog.steps_of(r))
    return (f"<code>#{r['id']}</code> {fmt_time(r['ts'], svc.cfg.tz)}{where} · {agentlog.STATUS.get(r['status'], r['status'])}"
            f"{f' · 도구 {tools}' if tools else ''}{cost}\n    {esc(r['trigger'][:50]) or '(내용 없음)'}")


async def _detail(c: PanelCtx, r, money: bool) -> str:
    svc = c.svc
    title = (await _titles(svc, [r["chat_id"]])).get(r["chat_id"])
    who = await svc.db.first_name(r["user_id"]) if r["user_id"] else None
    lines = [f"📒 <b>AI 작업</b> <code>#{r['id']}</code>",
             f"곳: {esc(title or _where({}, r['chat_id']))} · {fmt_time(r['ts'], svc.cfg.tz, '%m/%d %H:%M:%S')} · "
             f"{r['ms'] / 1000:.1f}초",
             f"요청한 사람: {esc(who or '?')} (<code>{r['user_id']}</code>)",
             f"방식: {esc(r['mode'])} · {esc(r['purpose'] or '-')}",
             f"결과: {agentlog.STATUS.get(r['status'], esc(r['status']))}",
             "", "<b>요청</b> (멤버가 쓴 글 그대로, 앞 200자)", f"<blockquote>{esc(r['trigger']) or '(내용 없음)'}</blockquote>"]
    steps = agentlog.steps_of(r)
    if steps:
        lines.append(f"<b>도구 {len(steps)}번</b>")
        for i, s in enumerate(steps[:STEPS_SHOWN], 1):
            lines.append(f"{i}. <code>{esc(str(s.get('tool', '?')))}</code>({esc(str(s.get('args', '')))})\n"
                         f"    → {esc(str(s.get('result', '')))}")
        if len(steps) > STEPS_SHOWN:
            lines.append(f"… 외 {len(steps) - STEPS_SHOWN}번")
    else:
        lines.append("도구 안 씀")
    lines += ["", f"토큰: 입력 {r['tok_in']:,} (캐시 {r['tok_cached']:,}) · 출력 {r['tok_out']:,}"]
    if money:
        lines.append(f"모델: {esc(r['models'] or '-')}")
        lines.append(f"요금: <b>{costs.fmt_usd(r['usd_micro'], 4)}</b>")
    return "\n".join(lines)


# ── 오너 ─────────────────────────────────────────────────
@_owner_only
async def s_overview(c: PanelCtx) -> Screen:
    svc = c.svc
    u = await costs.usage(svc.db, _today(svc))
    budget = getattr(svc.llm, "usd_budget", costs.DEFAULT_USD_BUDGET)
    lines = [f"📒 <b>AI 비용·작업 기록</b> (오늘 {_today(svc)})"]
    if budget > 0:
        cap = int(budget * costs.MICRO)
        full = " ⛔ 예산 다 씀 → 자정까지 AI 멈춤" if u["usd"] >= cap else ""
        lines.append(f"💵 오늘 요금: <b>{costs.fmt_usd(u['usd'])}</b> / 하루 예산 {costs.fmt_usd(cap)} "
                     f"({_pct(u['usd'], cap)}%){full}")
    else:
        lines.append(f"💵 오늘 요금: <b>{costs.fmt_usd(u['usd'])}</b> (하루 달러 예산 꺼짐)")
    tok_budget = getattr(svc.llm, "token_budget", 0)
    lines.append(f"🔢 토큰 {u['tokens']:,}" + (f" / 토큰 예산 {tok_budget:,} ({_pct(u['tokens'], tok_budget)}%)"
                                               if tok_budget else ""))
    btns = []
    if u["rooms"]:
        titles = await _titles(svc, [cid for cid, _, _ in u["rooms"]])
        lines += ["", "<b>많이 쓴 곳</b> (오늘 / 하루 한도)"]
        for i, (cid, used, cap) in enumerate(u["rooms"], 1):
            name = _where(titles, cid)
            lines.append(f"{i}. {esc(name[:24])} {costs.fmt_usd(used)} / {costs.fmt_usd(cap)} ({_pct(used, cap)}%)")
            if cid < 0:
                btns.append(B(f"💬 {name[:20]} {costs.fmt_usd(used)}", f"m:alq:{cid}"))
    else:
        lines.append("\n오늘은 아직 AI 요금 기록이 없어요.")
    lines.append("\n요금은 모델별 요금표(sodam/costs.py)로 계산한 추정치예요. 캐시로 읽은 입력은 10% 값으로 셉니다.")
    rows = menu._chunks(btns, 2) + [[B("📒 최근 AI 작업", "m:alr:0"), B("🏠 방 요금제", "m:alp:0")],
                                    [B("🔄 새로고침", "m:al"), B("⬅️ 처음으로", "m:home")]]
    return Screen("\n".join(lines), menu._kb(rows))


@_owner_only
async def s_runs(c: PanelCtx) -> Screen:
    svc = c.svc
    total = await agentlog.count(svc.db, None)
    pages = max(1, -(-total // PAGE))
    page = min(_page(c.arg(0)), pages - 1)
    rows_db = await agentlog.recent(svc.db, None, PAGE, page * PAGE)
    titles = await _titles(svc, [r["chat_id"] for r in rows_db])
    lines = [f"📒 <b>최근 AI 작업</b> (모든 방 · {total}건" + (f" · {page + 1}/{pages}쪽)" if pages > 1 else ")"),
             f"최근 {agentlog.KEEP_DAYS}일만 보관해요.", ""]
    lines += [_run_line(svc, r, titles, True) for r in rows_db] or ["아직 기록이 없어요."]
    rows = menu._chunks([B(f"#{r['id']} {_where(titles, r['chat_id'])[:14]}", f"m:alv:{r['id']}:{page}")
                         for r in rows_db], 2)
    if pr := _page_row("m:alr", page, pages):
        rows.append(pr)
    rows.append([B("⬅️ AI 비용", "m:al")])
    return Screen("\n".join(lines), menu._kb(rows))


@_owner_only
async def s_run(c: PanelCtx) -> Screen:
    rid = to_int(c.arg(0))
    r = await agentlog.get(c.svc.db, rid, None) if rid else None
    if not r:
        return Screen(None, toast="없는 기록이에요 (14일 지나면 지워져요).", alert=True)
    return Screen(await _detail(c, r, True), menu._kb([[B("⬅️ 목록", f"m:alr:{_page(c.arg(1))}")]]))


@_owner_only
async def s_plans(c: PanelCtx) -> Screen:
    svc = c.svc
    total = (await svc.db._one("SELECT COUNT(*) AS n FROM chats WHERE chat_id<0"))["n"]
    pages = max(1, -(-total // PAGE))
    page = min(_page(c.arg(0)), pages - 1)
    rows_db = await svc.db._all("SELECT chat_id, title FROM chats WHERE chat_id<0 ORDER BY chat_id LIMIT ? OFFSET ?",
                                (PAGE, page * PAGE))
    day = _today(svc)
    lines = [f"🏠 <b>방 요금제</b> (방마다 하루 AI 요금 한도 · 기본 {costs.plan_label(costs.DEFAULT_PLAN_CENTS)})"
             + (f" {page + 1}/{pages}쪽" if pages > 1 else ""),
             "방 관리자는 이 한도 안에서 %로 줄이기만 할 수 있어요.", ""]
    btns = []
    for r in rows_db:
        cid = r["chat_id"]
        plan = await costs.room_plan_cents(svc.db, cid)
        used = await svc.db.counter(day, cid, costs.ROOM_USD)
        name = r["title"] or str(cid)
        lines.append(f"• {esc(name[:24])} · 요금제 {costs.plan_label(plan)} · 오늘 {costs.fmt_usd(used)}")
        btns.append(B(f"💬 {name[:20]} {costs.plan_label(plan)}", f"m:alq:{cid}"))
    if not rows_db:
        lines.append("봇이 들어간 그룹이 아직 없어요.")
    rows = menu._chunks(btns, 2)
    if pr := _page_row("m:alp", page, pages):
        rows.append(pr)
    rows.append([B("⬅️ AI 비용", "m:al")])
    return Screen("\n".join(lines), menu._kb(rows))


async def _room_cid(c: PanelCtx, raw: str) -> int | None:
    if not menu.CID_RE.fullmatch(raw or "") or not await c.svc.db.has_chat(int(raw)):
        return None
    return int(raw)


async def _plan_screen(c: PanelCtx, cid: int) -> Screen:
    svc = c.svc
    s = await svc.db.get_settings(cid)
    plan = await costs.room_plan_cents(svc.db, cid)
    cap = await costs.room_cap_micro(svc.db, cid, s)
    used = await svc.db.counter(_today(svc), cid, costs.ROOM_USD)
    title = (await _titles(svc, [cid])).get(cid, str(cid))
    lines = ["🏠 <b>방 요금제</b>",
             f"<b>{esc(title)}</b> (<code>{cid}</code>)",
             f"요금제(하루): <b>{costs.plan_label(plan)}</b>",
             f"방 관리자 설정: 요금제의 {s.get(costs.PCT_KEY, 100)}% → 실제 한도 {costs.fmt_usd(cap)}",
             f"오늘 사용: {costs.fmt_usd(used)} ({_pct(used, cap)}%)",
             "\n한도를 넘으면 그 방은 자정까지 AI 가 쉬어요 (관리·게임은 그대로)."]
    presets = [B(("● " if plan == v else "") + costs.plan_label(v), f"m:alqs:{cid}:{v}") for v in costs.PLAN_CENTS]
    rows = menu._chunks(presets, 4) + [[B("⬅️ 방 요금제", "m:alp:0"), B("📒 AI 비용", "m:al")]]
    return Screen("\n".join(lines), menu._kb(rows))


@_owner_only
async def s_plan(c: PanelCtx) -> Screen:
    cid = await _room_cid(c, c.arg(0))
    if cid is None:
        return Screen(None, toast="없는 그룹이에요. 목록을 다시 열어주세요.", alert=True)
    return await _plan_screen(c, cid)


async def set_plan(svc, cid: int, uid: int, cents: int, source: str) -> bool:
    """방 하루 요금제 저장 (오너 버튼·AI 1:1 확인 카드 공통 — 부르는 쪽이 오너·값을 확인한 뒤). 바뀌었으면 True.
    같은 값이면 기록 안 남김 (같은 버튼 재전송)."""
    if await svc.db.get_state(cid, costs.PLAN_KEY) == cents:
        return False
    await svc.db.set_state(cid, costs.PLAN_KEY, cents)
    await svc.db.log_mod(cid, uid, None, "setting", f"AI 하루 요금제 → {costs.plan_label(cents)} ({source})")
    return True


@_owner_only
async def r_set_plan(c: PanelCtx) -> Screen:
    cid = await _room_cid(c, c.arg(0))
    cents = to_int(c.arg(1))
    if cid is None or cents not in costs.PLAN_CENTS:   # 정해진 값만 (위조 콜백 방지)
        return Screen(None, toast="없는 그룹이나 값이에요.", alert=True)
    await set_plan(c.svc, cid, c.uid, cents, "오너")
    screen = await _plan_screen(c, cid)
    screen.toast = f"✅ 하루 요금제 {costs.plan_label(cents)}"
    return screen


# ── 방 관리자 ─────────────────────────────────────────────
async def usage_pct(svc, cid: int, s: dict | None = None) -> int:
    """오늘 이 방 AI 사용량 = 달러 한도·토큰 한도 중 더 많이 찬 쪽의 % (방 관리자에겐 금액을 안 보여줌)."""
    s = s if s is not None else await svc.db.get_settings(cid)
    day = _today(svc)
    usd = _pct(await svc.db.counter(day, cid, costs.ROOM_USD), await costs.room_cap_micro(svc.db, cid, s))
    cap = min(s.get("ai_room_daily_tokens") or ROOM_TOKENS_MAX, ROOM_TOKENS_MAX)
    return max(usd, _pct(await svc.db.counter(day, cid, ROOM_TOKENS), cap))


async def s_room_runs(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    total = await agentlog.count(svc.db, cid)
    pages = max(1, -(-total // PAGE))
    page = min(_page(c.arg(0)), pages - 1)
    rows_db = await agentlog.recent(svc.db, cid, PAGE, page * PAGE)
    pct = await usage_pct(svc, cid, s)
    full = " · 오늘 몫을 다 써서 자정까지 쉬어요" if pct >= 100 else ""
    lines = ["📒 <b>AI 작업 기록</b>" + (f" ({page + 1}/{pages}쪽 · 총 {total}건)" if pages > 1 else ""),
             f"🔋 오늘 AI 사용량: 하루 한도의 {min(pct, 100)}%{full}",
             f"사용 한도: 기본 한도의 {s.get(costs.PCT_KEY, 100)}% (낮추면 AI 를 덜 써요)",
             f"AI 가 무엇을 요청받고 어떤 도구를 썼는지 최근 {agentlog.KEEP_DAYS}일 기록이에요.", ""]
    lines += [_run_line(svc, r, None, False) for r in rows_db] or ["아직 기록이 없어요."]
    rows = [menu._preset_row(s, cid, costs.PCT_KEY)]
    rows += menu._chunks([B(f"#{r['id']} {fmt_time(r['ts'], svc.cfg.tz)}", f"m:agv:{cid}:{r['id']}:{page}")
                          for r in rows_db], 2)
    if pr := _page_row(f"m:alg:{cid}", page, pages):
        rows.append(pr)
    rows.append(menu._back(cid))
    return Screen("\n".join(lines), menu._kb(rows))


async def s_room_run(c: PanelCtx) -> Screen:
    rid = to_int(c.arg(0))
    r = await agentlog.get(c.svc.db, rid, c.cid) if rid else None   # 이 방 기록만
    if not r:
        return Screen(None, toast="이 방 기록이 아니거나 지워졌어요.", alert=True)
    return Screen(await _detail(c, r, False), menu._kb([[B("⬅️ 목록", f"m:alg:{c.cid}:{_page(c.arg(1))}")]]))


# ── 등록 ──────────────────────────────────────────────────
menu.register_main(92, "al", "📒 AI 비용·기록", OWNER)
for _code, _fn in (("al", s_overview), ("alr", s_runs), ("alv", s_run), ("alp", s_plans), ("alq", s_plan),
                   ("alqs", r_set_plan)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_hub(HubItem(46, "alg", "📒 AI 작업 기록"))
menu.register_screen("alg", s_room_runs)
menu.register_screen("agv", s_room_run)
