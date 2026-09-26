"""👥 멤버 목록 (관리자 1:1 패널 · 방에선 `.멤버`).

텔레그램 봇 API 는 방 멤버 전체 목록을 주지 않는다 (관리자 목록·총인원만). 그래서
'소담이 본 사람'(말함·입장·답장·반응 등 → members 테이블) + 관리자 목록 + 텔레그램 총인원을 합쳐 보여준다.
나간 사람은 member_left 에 표시해서 뺀다.

쿼리가 터지지 않게:
- 한 쪽 15명 LIMIT/OFFSET (쪽 번호 상한), 인덱스(members(chat_id,last_seen))
- 메시지 수 정렬은 방 단위 GROUP BY 한 번 → 5분 캐시
- 텔레그램 총인원·관리자 목록은 10분·5분 캐시, 호출마다 8초 제한 (실패하면 '?' 로 표시하고 계속)
- DB 조회도 5초 제한, CSV 는 1인 1분에 1번·최대 20,000명
"""
from __future__ import annotations

import asyncio
import csv
import io
import time
from typing import TYPE_CHECKING

from telegram import InputFile
from telegram.error import TelegramError

from .. import menu
from ..db import register_schema
from ..menu import B, PanelCtx, Route, Screen, _kb, register_hub, register_input, register_route
from ..util import display_name, esc, fmt_time, to_int

if TYPE_CHECKING:
    from ..services import Services

register_schema("""
CREATE TABLE IF NOT EXISTS member_left (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_seen ON members(chat_id, last_seen);
""", migrate={"member_left": "composite"})

PAGE = 15
MAX_PAGE = 2000
SORTS = {"seen": "최근 활동", "msg": "메시지 많은", "join": "입장순"}
COUNT_TTL, ADMIN_TTL, MSG_TTL = 600, 300, 300
TG_TIMEOUT, DB_TIMEOUT = 8.0, 5.0
CSV_MAX, CSV_GAP = 20_000, 60

_count_cache: dict[int, tuple[float, int | None]] = {}
_msg_cache: dict[int, tuple[float, dict[int, int]]] = {}
_csv_last: dict[int, float] = {}


# ── 나감/다시 들어옴 표시 ─────────────────────────────────
async def mark(db, chat_id: int, user_id: int, left: bool) -> None:
    if left:
        await db._write("INSERT INTO member_left(chat_id, user_id, ts) VALUES(?,?,?) "
                        "ON CONFLICT(chat_id, user_id) DO UPDATE SET ts=excluded.ts", (chat_id, user_id, int(time.time())))
    else:
        await db._write("DELETE FROM member_left WHERE chat_id=? AND user_id=?", (chat_id, user_id))


# ── 캐시된 조회 ───────────────────────────────────────────
async def tg_member_count(bot, chat_id: int) -> int | None:
    hit = _count_cache.get(chat_id)
    if hit and time.time() - hit[0] < COUNT_TTL:
        return hit[1]
    try:
        n = await asyncio.wait_for(bot.get_chat_member_count(chat_id), TG_TIMEOUT)
    except (TelegramError, asyncio.TimeoutError):
        return hit[1] if hit else None
    _count_cache[chat_id] = (time.time(), n)
    return n


async def admin_ids(svc: Services, bot, chat_id: int) -> set[int]:
    try:
        users = await asyncio.wait_for(svc.perms.admin_users(bot, chat_id), TG_TIMEOUT)
    except (TelegramError, asyncio.TimeoutError):
        return set()
    return {u.id for u in users}


async def msg_counts(db, chat_id: int) -> dict[int, int]:
    hit = _msg_cache.get(chat_id)
    if hit and time.time() - hit[0] < MSG_TTL:
        return hit[1]
    rows = await asyncio.wait_for(db._all(
        "SELECT user_id, COUNT(*) AS n FROM messages WHERE chat_id=? AND is_bot=0 GROUP BY user_id", (chat_id,)),
        DB_TIMEOUT)
    counts = {r["user_id"]: r["n"] for r in rows}
    if len(_msg_cache) > 500:
        _msg_cache.clear()
    _msg_cache[chat_id] = (time.time(), counts)
    return counts


_BASE = ("FROM members m JOIN users u ON u.user_id=m.user_id "
         "LEFT JOIN member_left l ON l.chat_id=m.chat_id AND l.user_id=m.user_id "
         "WHERE m.chat_id=? AND u.is_bot=0 AND l.user_id IS NULL")


async def known_count(db, chat_id: int) -> int:
    row = await asyncio.wait_for(db._one(f"SELECT COUNT(*) AS n {_BASE}", (chat_id,)), DB_TIMEOUT)
    return row["n"]


async def page_rows(db, chat_id: int, sort: str, page: int) -> list:
    cols = "u.user_id, u.first_name, u.last_name, u.username, m.last_seen, m.joined_at"
    if sort == "msg":
        counts = await msg_counts(db, chat_id)
        rows = await asyncio.wait_for(db._all(f"SELECT {cols} {_BASE}", (chat_id,)), DB_TIMEOUT)
        rows = sorted(rows, key=lambda r: (-counts.get(r["user_id"], 0), -(r["last_seen"] or 0)))
        return rows[page * PAGE:(page + 1) * PAGE]
    order = "m.last_seen DESC" if sort == "seen" else "COALESCE(m.joined_at, m.last_seen) ASC"
    return await asyncio.wait_for(db._all(
        f"SELECT {cols} {_BASE} ORDER BY {order}, u.user_id LIMIT ? OFFSET ?",
        (chat_id, PAGE, page * PAGE)), DB_TIMEOUT)


async def search_rows(db, chat_id: int, q: str) -> list:
    q = q.strip().lstrip("@")
    uid = to_int(q)
    like = f"%{q}%"
    return await asyncio.wait_for(db._all(
        f"SELECT u.user_id, u.first_name, u.last_name, u.username, m.last_seen, m.joined_at {_BASE} AND "
        "(u.user_id=? OR u.first_name LIKE ? OR COALESCE(u.last_name,'') LIKE ? OR COALESCE(u.username,'') LIKE ?) "
        "ORDER BY m.last_seen DESC LIMIT ?", (chat_id, uid or -1, like, like, like, PAGE)), DB_TIMEOUT)


# ── 화면 ─────────────────────────────────────────────────
def _line(i: int, r, tz, admins: set[int], counts: dict[int, int] | None) -> str:
    name = esc(display_name(r["first_name"], r["last_name"], None))
    uname = f" @{esc(r['username'])}" if r["username"] else ""
    crown = " 👑" if r["user_id"] in admins else ""
    seen = fmt_time(r["last_seen"], tz, "%m/%d %H:%M") if r["last_seen"] else "-"
    msgs = f" · 💬{counts.get(r['user_id'], 0)}" if counts is not None else ""
    return f"{i}. <b>{name}</b>{uname}{crown}\n    <code>{r['user_id']}</code> · 최근 {seen}{msgs}"


async def s_members(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    sort = c.arg(0) if c.arg(0) in SORTS else "seen"
    page = to_int(c.arg(1)) or 0
    try:
        total_known = await known_count(svc.db, cid)
        pages = max(1, (total_known + PAGE - 1) // PAGE)
        page = max(0, min(page, pages - 1, MAX_PAGE))
        rows = await page_rows(svc.db, cid, sort, page)
        counts = await msg_counts(svc.db, cid) if sort == "msg" else None
    except asyncio.TimeoutError:
        return Screen(None, toast="조회가 오래 걸려요. 잠시 후 다시 눌러주세요.", alert=True)
    tg_total, admins = await tg_member_count(c.bot, cid), await admin_ids(svc, c.bot, cid)
    head = [f"👥 <b>멤버 목록</b> · {SORTS[sort]} ({page + 1}/{pages}쪽)",
            f"텔레그램 기준 <b>{tg_total if tg_total is not None else '?'}명</b> · 소담이 본 사람 <b>{total_known}명</b>"]
    if tg_total and total_known < tg_total:
        head.append("<i>말하거나 들어온 적 없는 사람은 텔레그램이 봇에게 알려주지 않아서 목록에 없어요.</i>")
    lines = [_line(page * PAGE + i + 1, r, svc.cfg.tz, admins, counts) for i, r in enumerate(rows)] or ["(아직 본 사람이 없어요)"]
    sort_row = [B(("● " if k == sort else "") + v, f"m:mb:{cid}:{k}:0") for k, v in SORTS.items()]
    nav = []
    if page > 0:
        nav.append(B("◀ 이전", f"m:mb:{cid}:{sort}:{page - 1}"))
    if page < pages - 1:
        nav.append(B("다음 ▶", f"m:mb:{cid}:{sort}:{page + 1}"))
    rows_kb = [sort_row] + ([nav] if nav else []) + [
        [B("🔎 검색", f"m:in:{cid}:mbq"), B("📄 CSV 받기", f"m:mbx:{cid}")],
        [B("🔄 새로고침", f"m:mbr:{cid}:{sort}"), B("⬅️ 뒤로", f"m:g:{cid}")]]
    return Screen("\n".join(head) + "\n\n" + "\n".join(lines), _kb(rows_kb))


async def r_refresh(c: PanelCtx) -> Screen:
    _count_cache.pop(c.cid, None)
    _msg_cache.pop(c.cid, None)
    screen = await s_members(PanelCtx(c.svc, c.bot, c.uid, c.cid, [c.arg(0), "0"]))
    screen.toast = "새로 불러왔어요."
    return screen


async def r_export(c: PanelCtx) -> Screen:
    now = time.time()
    if now - _csv_last.get(c.uid, 0) < CSV_GAP:
        return Screen(None, toast="CSV 는 1분에 한 번만 받을 수 있어요.", alert=True)
    _csv_last[c.uid] = now
    try:
        rows = await asyncio.wait_for(c.svc.db._all(
            f"SELECT u.user_id, u.first_name, u.last_name, u.username, m.joined_at, m.last_seen {_BASE} "
            "ORDER BY m.last_seen DESC LIMIT ?", (c.cid, CSV_MAX)), DB_TIMEOUT * 2)
        counts = await msg_counts(c.svc.db, c.cid)
    except asyncio.TimeoutError:
        return Screen(None, toast="조회가 오래 걸려요. 잠시 후 다시 눌러주세요.", alert=True)
    admins = await admin_ids(c.svc, c.bot, c.cid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "username", "admin", "joined", "last_seen", "messages_90d"])
    tz = c.svc.cfg.tz
    for r in rows:
        w.writerow([r["user_id"], display_name(r["first_name"], r["last_name"], None), r["username"] or "",
                    "Y" if r["user_id"] in admins else "", fmt_time(r["joined_at"], tz, "%Y-%m-%d") if r["joined_at"] else "",
                    fmt_time(r["last_seen"], tz, "%Y-%m-%d %H:%M") if r["last_seen"] else "", counts.get(r["user_id"], 0)])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # 엑셀에서 한글 안 깨지게 BOM
    await c.bot.send_document(c.uid, InputFile(io.BytesIO(data), filename=f"members_{c.cid}.csv"),
                              caption=f"👥 멤버 {len(rows)}명 (소담이 본 사람, 나간 사람 제외)")
    await c.svc.db.log_mod(c.cid, c.uid, None, "export", f"members csv {len(rows)}")
    return Screen(None, toast=f"1:1 로 CSV 를 보냈어요 ({len(rows)}명).")


async def _search(c: PanelCtx, msg) -> tuple[bool, str]:
    q = (msg.text or "").strip()
    if not 1 <= len(q) <= 40:
        return False, "이름·@아이디·숫자 ID 를 1~40자로 보내주세요."
    try:
        rows = await search_rows(c.svc.db, c.cid, q)
    except asyncio.TimeoutError:
        return True, "조회가 오래 걸려요. 잠시 후 다시 해주세요."
    admins = await admin_ids(c.svc, c.bot, c.cid)
    if not rows:
        return True, f"🔎 '{esc(q)}' 멤버가 없어요."
    return True, f"🔎 '{esc(q)}' {len(rows)}명\n\n" + "\n".join(
        _line(i + 1, r, c.svc.cfg.tz, admins, None) for i, r in enumerate(rows))


async def s_after_search(c: PanelCtx) -> Screen:
    return Screen("", _kb([[B("👥 멤버 목록으로", f"m:mb:{c.cid}")]]))


register_hub(menu.HubItem(70, "mb", "👥 멤버 목록"))
register_route("mb", Route(s_members))
register_route("mbr", Route(r_refresh))
register_route("mbx", Route(r_export, fresh=True))
register_input("mbq", "🔎 찾을 멤버의 <b>이름·@아이디·숫자 ID</b>를 보내주세요.", "mb", _search, s_after_search)
menu.SCREENS["mb"] = s_members
