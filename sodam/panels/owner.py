"""👑 오너 메뉴 (봇 운영자 1:1 전용). 금액·결제 내역이 나오는 유일한 메뉴라 누를 때마다 오너인지 다시 확인한다.

m:o                      오너 메뉴 (요약)
m:ol:<쪽>                🌐 전체 방 현황 (10개씩, 쪽 번호는 0~999 만)
m:oc:<방ID>:<쪽>         방 상세 (상태·만료·최근 결제) + 기간 부여 버튼
m:og:<방ID>:<일수>:<쪽>  기간 부여 확인 화면 (7/30/90일만). 실제 부여는 1회용 토큰 m:k:<토큰> (새로 권한 확인)
m:os                     💰 매출·결제 (이번달/지난달, 최근 결제, 청구서와 안 맞는 입금)

라우트는 방 단위가 아니라서(scoped=False) 라우터가 권한을 안 본다 → 핸들러마다 _owner_only 로 확인.
조회는 이 모듈 안에서 db._all/_one 로만 (쓰기는 기간 부여 = db.extend_paid 한 문장뿐).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from functools import wraps

from .. import menu
from ..billing import fmt_usdt
from ..menu import OWNER, B, PanelCtx, Route, Screen
from ..util import esc, fmt_time

PAGE_SIZE = 10
PAGE_RE = re.compile(r"^\d{1,3}$")
GRANT_DAYS = (7, 30, 90)
RECENT = 10
NOT_OWNER = Screen(None, toast="봇 오너만 볼 수 있어요.", alert=True)
STATE_ICON = {"paid": "✅", "trial": "🎁", "expired": "⛔", "free": "🆓", "none": "❔"}
STATE_NAME = {"paid": "구독", "trial": "체험", "expired": "만료", "free": "무료", "none": "기록 없음"}

# 결제로 청구서가 처리된 입금 = 매출. 나머지(청구서 없음·금액 틀림·처리 경합)는 '확인 필요 입금'
_MATCHED = "EXISTS (SELECT 1 FROM invoices i WHERE i.tx_id=p.tx_id AND i.status='paid')"
_TS = "COALESCE(p.block_ts, p.seen_at)"


def _owner_only(fn):
    @wraps(fn)
    async def wrapped(c: PanelCtx) -> Screen:
        if c.uid not in await c.svc.perms.owners():  # 누를 때마다 확인 (위조 콜백·오너 해제 대비)
            return NOT_OWNER
        return await fn(c)
    return wrapped


def _page(raw: str) -> int | None:
    return int(raw) if PAGE_RE.fullmatch(raw or "0") else None


async def _room_cid(c: PanelCtx, raw: str) -> int | None:
    """콜백의 방 ID: 형식이 맞고 DB 에 있는 그룹만."""
    if not menu.CID_RE.fullmatch(raw or "") or not await c.svc.db.has_chat(int(raw)):
        return None
    return int(raw)


def _state(c: PanelCtx, row, now: int) -> str:
    """billing.status 와 같은 판단. 단 목록 조회라 체험 기록을 새로 만들지 않는다."""
    if not (c.svc.billing and c.svc.billing.enabled):
        return "free"
    if row["paid_until"] is None and row["trial_until"] is None:
        return "none"
    if (row["paid_until"] or 0) > now:
        return "paid"
    if (row["trial_until"] or 0) > now:
        return "trial"
    return "expired"


def _date(c: PanelCtx, ts: int | None, fmt: str = "%Y-%m-%d") -> str:
    return fmt_time(ts, c.svc.cfg.tz, fmt) if ts else "-"


def _short(addr: str | None, keep: int = 6) -> str:
    addr = addr or "?"
    return addr if len(addr) <= keep * 2 + 1 else f"{addr[:keep]}…{addr[-keep:]}"


def _month_start(c: PanelCtx, back: int = 0) -> tuple[int, str]:
    """이번 달(back=0)·지난 달(back=1) 1일 0시 (봇 시간대) 와 'N월'."""
    now = datetime.now(c.svc.cfg.tz)
    y, m = now.year, now.month - back
    if m < 1:
        y, m = y - 1, m + 12
    return int(now.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()), f"{m}월"


# ── 조회 ──────────────────────────────────────────────────
_ROOMS_SQL = (
    "SELECT c.chat_id, c.title, s.trial_until, s.paid_until, s.added_by, "
    "MAX(COALESCE(s.paid_until, 0), COALESCE(s.trial_until, 0)) AS until, "
    "(SELECT MAX(COALESCE(block_ts, seen_at)) FROM payments p WHERE p.chat_id=c.chat_id AND " + _MATCHED + ") AS last_paid "
    "FROM chats c LEFT JOIN subscriptions s ON s.chat_id=c.chat_id WHERE c.chat_id<0 ")


async def all_rooms(c: PanelCtx, page: int) -> list:
    # 이용 중인 방은 만료 임박 순, 그 뒤에 만료된 방은 최근 만료 순
    return await c.svc.db._all(
        _ROOMS_SQL + "ORDER BY (until <= ?), CASE WHEN until > ? THEN until ELSE -until END, c.chat_id "
        "LIMIT ? OFFSET ?", (int(time.time()),) * 2 + (PAGE_SIZE, page * PAGE_SIZE))


async def room(c: PanelCtx, cid: int):
    return await c.svc.db._one(_ROOMS_SQL + "AND c.chat_id=?", (cid,))


async def room_counts(c: PanelCtx) -> dict[str, int]:
    now = int(time.time())
    counts = dict.fromkeys(STATE_NAME, 0)
    for r in await c.svc.db._all(
            "SELECT s.trial_until, s.paid_until FROM chats c "
            "LEFT JOIN subscriptions s ON s.chat_id=c.chat_id WHERE c.chat_id<0"):
        counts[_state(c, r, now)] += 1
    return counts


async def payments_summary(c: PanelCtx, start: int, end: int | None = None) -> tuple[int, int]:
    """[start, end) 사이 매출 (합계 단위, 건수). 청구서와 맞은 입금만."""
    row = await c.svc.db._one(
        f"SELECT COALESCE(SUM(p.amount_units), 0) AS total, COUNT(*) AS n FROM payments p "
        f"WHERE {_MATCHED} AND {_TS} >= ? AND {_TS} < ?", (start, end if end is not None else 2**62))
    return row["total"], row["n"]


async def recent_payments(c: PanelCtx, limit: int = RECENT) -> list:
    return await c.svc.db._all(
        f"SELECT p.*, {_TS} AS ts, ch.title FROM payments p LEFT JOIN chats ch ON ch.chat_id=p.chat_id "
        f"WHERE {_MATCHED} ORDER BY ts DESC LIMIT ?", (limit,))


async def unmatched_payments(c: PanelCtx, limit: int = RECENT) -> tuple[list, int, int]:
    """청구서와 안 맞는 입금 (최근 limit 건, 전체 건수, 전체 합계)."""
    rows = await c.svc.db._all(
        f"SELECT p.*, {_TS} AS ts FROM payments p WHERE NOT {_MATCHED} ORDER BY ts DESC LIMIT ?", (limit,))
    agg = await c.svc.db._one(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(amount_units), 0) AS total FROM payments p WHERE NOT {_MATCHED}")
    return rows, agg["n"], agg["total"]


# ── 화면 ──────────────────────────────────────────────────
@_owner_only
async def s_owner(c: PanelCtx) -> Screen:
    counts = await room_counts(c)
    total, n = await payments_summary(c, _month_start(c)[0])
    _, bad, _ = await unmatched_payments(c, 0)
    lines = ["👑 <b>오너 메뉴</b>",
             f"방 {sum(counts.values())}개 · " + " · ".join(f"{STATE_ICON[k]} {STATE_NAME[k]} {v}"
                                                         for k, v in counts.items() if v),
             f"이번 달 매출: <b>{fmt_usdt(total)} USDT</b> ({n}건)"]
    if bad:
        lines.append(f"⚠️ 확인이 필요한 입금 {bad}건 — 💰 매출·결제에서 확인하세요.")
    rows = [[B("🌐 전체 방 현황", "m:ol:0"), B("💰 매출·결제", "m:os")],
            [B("⬅️ 처음으로", "m:home")]]
    return Screen("\n".join(lines), menu._kb(rows))


@_owner_only
async def s_rooms(c: PanelCtx) -> Screen:
    page = _page(c.arg(0))
    if page is None:
        return Screen(None)
    total = (await c.svc.db._one("SELECT COUNT(*) AS n FROM chats WHERE chat_id<0"))["n"]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages - 1)  # 방이 줄어서 옛 버튼의 쪽이 없어졌으면 마지막 쪽
    now = int(time.time())
    lines = [f"🌐 <b>전체 방 현황</b> ({total}개 · {page + 1}/{pages}쪽)",
             "방을 누르면 상세를 보고 이용 기간을 부여할 수 있어요.\n"]
    btns = []
    for i, r in enumerate(await all_rooms(c, page), page * PAGE_SIZE + 1):
        st = _state(c, r, now)
        title = r["title"] or str(r["chat_id"])
        if st in ("paid", "trial"):
            when = f"{STATE_NAME[st]} {_date(c, r['until'])}까지"
        elif st == "expired":
            when = f"만료 {_date(c, r['until'])}"
        else:
            when = STATE_NAME[st]
        paid = f"최근 결제 {_date(c, r['last_paid'])}" if r["last_paid"] else "결제 없음"
        lines.append(f"{i}. {STATE_ICON[st]} <b>{esc(title[:30])}</b>\n    {when} · {paid}")
        btns.append([B(f"{STATE_ICON[st]} {title[:28]}", f"m:oc:{r['chat_id']}:{page}")])
    if not total:
        lines.append("\n봇이 들어간 그룹이 아직 없어요.")
    nav = []
    if page > 0:
        nav.append(B("◀️ 이전", f"m:ol:{page - 1}"))
    if page < pages - 1:
        nav.append(B("다음 ▶️", f"m:ol:{page + 1}"))
    rows = btns + ([nav] if nav else []) + [[B("⬅️ 오너 메뉴", "m:o")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def _room_screen(c: PanelCtx, cid: int, page: int) -> Screen:
    r = await room(c, cid)
    st = _state(c, r, int(time.time()))
    pays = await c.svc.db._all(
        f"SELECT p.amount_units, {_TS} AS ts FROM payments p WHERE p.chat_id=? AND {_MATCHED} "
        "ORDER BY ts DESC LIMIT 3", (cid,))
    full = "%Y-%m-%d %H:%M"
    lines = ["🌐 <b>방 상세</b>",
             f"<b>{esc(r['title'] or str(cid))}</b> (<code>{cid}</code>)",
             f"상태: {STATE_ICON[st]} {STATE_NAME[st]}",
             f"구독 만료: {_date(c, r['paid_until'], full) if r['paid_until'] else '구독한 적 없음'}",
             f"체험 만료: {_date(c, r['trial_until'], full)}"]
    if r["added_by"]:
        lines.append(f"봇 추가한 사람: <code>{r['added_by']}</code>")
    lines.append("\n<b>최근 결제</b>")
    lines += [f"• {_date(c, p['ts'])} · {fmt_usdt(p['amount_units'])} USDT" for p in pays] or ["(없음)"]
    lines.append("\n기간 부여는 남은 기간 뒤로 이어서 붙어요 (결제 없이).")
    rows = [[B(f"➕ {d}일", f"m:og:{cid}:{d}:{page}") for d in GRANT_DAYS],
            [B("⬅️ 방 목록", f"m:ol:{page}"), B("👑 오너 메뉴", "m:o")]]
    return Screen("\n".join(lines), menu._kb(rows))


@_owner_only
async def s_room(c: PanelCtx) -> Screen:
    cid, page = await _room_cid(c, c.arg(0)), _page(c.arg(1))
    if cid is None or page is None:
        return Screen(None, toast="없는 그룹이에요. 목록을 다시 열어주세요.", alert=True)
    return await _room_screen(c, cid, page)


@_owner_only
async def s_grant_ask(c: PanelCtx) -> Screen:
    cid, page = await _room_cid(c, c.arg(0)), _page(c.arg(2))
    days = int(c.arg(1)) if c.arg(1) in {str(d) for d in GRANT_DAYS} else None
    if cid is None or page is None or days is None:
        return Screen(None, toast="없는 그룹이에요. 목록을 다시 열어주세요.", alert=True)
    r = await room(c, cid)
    base = max(int(time.time()), r["until"] or 0)
    tok = menu.token(c.svc, c.uid, cid, "owner_grant", (days, page))
    text = ("➕ <b>이용 기간 부여</b>\n"
            f"<b>{esc(r['title'] or str(cid))}</b> 이용 기간을 <b>{days}일</b> 늘릴까요?\n"
            f"지금 만료: {_date(c, r['until'] or None, '%Y-%m-%d %H:%M')}\n"
            f"부여 후: {_date(c, base + days * 86400, '%Y-%m-%d %H:%M')} 쯤\n\n"
            "결제 없이 부여돼요. 2분 안에 눌러주세요.")
    return Screen(text, menu._kb([[B(f"✅ {days}일 부여", f"m:k:{tok}"), B("취소", f"m:oc:{cid}:{page}")]]))


async def t_grant(c: PanelCtx, arg) -> Screen:
    """1회용 토큰으로만 도착 (오너·새 권한 확인은 r_token 이 TOKEN_NEED=OWNER 로 이미 함)."""
    days, page = arg
    if days not in GRANT_DAYS or not await c.svc.db.has_chat(c.cid):
        return Screen(None, toast="없는 그룹이에요.", alert=True)
    # 남은 기간(유료·체험 중 늦은 쪽) 뒤로 이어서. SQL 한 문장이라 결제 연장과 동시에 돌아도 안 덮어씀
    until = await c.svc.db.extend_paid(c.cid, days * 86400, int(time.time()))
    title = (await room(c, c.cid))["title"] or str(c.cid)
    await c.svc.db.log_mod(c.cid, c.uid, None, "sub_grant", f"{days}일 (오너 메뉴)")
    await c.svc.mod.report(c.bot, f"[기간 부여] 오너 메뉴에서 결제 없이\n{esc(title)} (<code>{c.cid}</code>) "
                                  f"+{days}일 → {_date(c, until, '%Y-%m-%d %H:%M')} 까지")
    screen = await _room_screen(c, c.cid, page)
    screen.toast = f"✅ {days}일 부여 → {_date(c, until)} 까지"
    return screen


@_owner_only
async def s_sales(c: PanelCtx) -> Screen:
    this_start, this_name = _month_start(c)
    last_start, last_name = _month_start(c, 1)
    this_total, this_n = await payments_summary(c, this_start)
    last_total, last_n = await payments_summary(c, last_start, this_start)
    lines = ["💰 <b>매출·결제</b>",
             f"이번 달({this_name}): <b>{fmt_usdt(this_total)} USDT</b> · {this_n}건",
             f"지난 달({last_name}): <b>{fmt_usdt(last_total)} USDT</b> · {last_n}건",
             "(청구서와 맞아서 구독이 연장된 입금만 합산)",
             f"\n<b>최근 결제</b>"]
    pays = await recent_payments(c)
    lines += [f"• {_date(c, p['ts'], '%m/%d %H:%M')} · {fmt_usdt(p['amount_units'])} · "
              f"{esc((p['title'] or str(p['chat_id']))[:20])}" for p in pays] or ["(아직 없어요)"]
    bad, n_bad, bad_total = await unmatched_payments(c)
    if n_bad:
        lines.append(f"\n⚠️ <b>청구서와 안 맞는 입금</b> {n_bad}건 · {fmt_usdt(bad_total)} USDT")
        lines += [f"• {_date(c, u['ts'], '%m/%d %H:%M')} · {fmt_usdt(u['amount_units'])} · "
                  f"보낸 주소 <code>{esc(_short(u['from_addr']))}</code> · tx <code>{esc(_short(u['tx_id'], 8))}</code>"
                  for u in bad]
        lines.append("금액을 잘못 보냈거나 청구서가 끝난 뒤 들어온 돈이에요. "
                     "보낸 사람을 확인한 뒤 🌐 전체 방 현황에서 그 방에 기간을 부여하세요.")
    else:
        lines.append("\n청구서와 안 맞는 입금: 없음")
    rows = [[B("🔄 새로고침", "m:os"), B("⬅️ 오너 메뉴", "m:o")]]
    return Screen("\n".join(lines), menu._kb(rows))


# ── 등록 ──────────────────────────────────────────────────
menu.register_main(90, "o", "👑 오너 메뉴", need=OWNER)
for _code, _fn in (("o", s_owner), ("ol", s_rooms), ("oc", s_room), ("og", s_grant_ask), ("os", s_sales)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_token_action("owner_grant", t_grant, fresh=True, need=OWNER)
