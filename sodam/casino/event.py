"""🎉 포인트 이벤트 (2026-10-17 블랙컴퍼니 '바카라 포인트 이벤트 — 토끼가 퀴즈 내고 정답자에게 리필, 12~3시 제일 많이 딴 사람').

- `!진행자 @아이디|답장` (텔레그램 '사용자 차단' 권한 관리자) = 이벤트 진행자 켜고 끄기, `!진행자` = 목록.
- `!지급 @아이디|답장 금액` (진행자·관리자) = **자기 잔액에서** 상대에게 옮김 (새 포인트를 만들지 않음 — 진행자가 받은 만큼만 나눠 줌).
  아무나 쓰는 송금은 아님 (멤버끼리 주고받기 = 포인트 현금 거래 길이라 안 만듦, casino/__init__ 머리말).
- `!이벤트 00:00 03:00 10억` (관리자) = 이벤트 시간·목표, `!이벤트 끄기`. 끝 시각이 시작보다 이르면 다음 날.
- `!이벤트순위` = 그 시간 안 게임 순수익(베팅·당첨·환불만, 지급·리필·채굴 제외) 순위. 목표 이상만 수상 후보.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from .. import setkeys
from ..permissions import may
from ..settings import register_setting
from ..util import esc, mention, user_name
from . import Ctx, register
from .core import balance, fmt, parse_amount

register_setting("casino_hosts", "", "포인트 이벤트 진행자 (ID 목록)")
register_setting("casino_event", "", "포인트 이벤트 시간·목표")
setkeys.EXCLUDED["casino_hosts"] = "이벤트 진행자는 방에서 관리자가 !진행자 @아이디 로 정함"
setkeys.EXCLUDED["casino_event"] = "이벤트 시간·목표는 방에서 관리자가 !이벤트 00:00 03:00 10억 으로 정함"

GAME_REASONS = "(reason LIKE 'bet:%' OR reason LIKE 'win:%' OR reason LIKE 'refund%')"
TOP = 10
GAMES = {"바카라": ("baccarat", "bactable")}   # !이벤트 … 바카라 → 그 게임만 셈 (없으면 모든 게임)


def _hosts(s: dict) -> set[int]:
    return {int(x) for x in str(s.get("casino_hosts") or "").split(",") if x.strip().isdecimal()}


def big(n: int) -> str:
    """10억 2,500만 같은 읽기 쉬운 금액 (P)."""
    sign, n = ("-" if n < 0 else ""), abs(n)
    eok, man = divmod(n, 100_000_000)
    man, rest = divmod(man, 10_000)
    parts = [f"{eok:,}억"] if eok else []
    if man:
        parts.append(f"{man:,}만")
    if rest or not parts:
        parts.append(f"{rest:,}")
    return sign + " ".join(parts) + "P"


async def _admin(ctx: Ctx) -> bool:
    return await may(ctx.svc.perms, ctx.bot, ctx.chat_id, ctx.user.id)


async def _target(ctx: Ctx, args: list[str]) -> tuple[dict | None, list[str], str | None]:
    """답장 대상 또는 첫 인자(@아이디·숫자 ID) → 이 방 멤버. (사람, 남은 인자, 오류)."""
    db, rep = ctx.svc.db, ctx.msg.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        u = rep.from_user
        return {"user_id": u.id, "name": user_name(u)}, args, None
    if not args:
        return None, args, "누구에게? 그 사람 글에 답장하거나 <code>@아이디</code> 를 적어주세요."
    q, rest = args[0].lstrip("@"), args[1:]
    if q.isdecimal():
        row = await db._one("SELECT u.user_id, u.first_name, u.last_name, u.username FROM members m JOIN users u USING(user_id) "
                            "WHERE m.chat_id=? AND m.user_id=?", (ctx.chat_id, int(q)))
    else:
        row = await db._one("SELECT u.user_id, u.first_name, u.last_name, u.username FROM members m JOIN users u USING(user_id) "
                            "WHERE m.chat_id=? AND u.username=? COLLATE NOCASE", (ctx.chat_id, q))
    if not row:
        return None, rest, f"이 방에서 {esc(args[0])} 를 못 찾았어요. 그 사람 글에 답장해서 해주세요."
    name = " ".join(x for x in (row["first_name"], row["last_name"]) if x) or (row["username"] and "@" + row["username"]) or "?"
    return {"user_id": row["user_id"], "name": name}, rest, None


# ── 진행자 ─────────────────────────────────────────────────
async def c_host(ctx: Ctx) -> None:
    db = ctx.svc.db
    s = await db.get_settings(ctx.chat_id)
    hosts = _hosts(s)
    if not ctx.args and not ctx.msg.reply_to_message:
        if not hosts:
            await ctx.reply("🎤 이벤트 진행자가 없어요. 관리자가 <code>!진행자 @아이디</code> 로 정해요.")
            return
        rows = await db._all(f"SELECT user_id, first_name, username FROM users WHERE user_id IN ({','.join('?' * len(hosts))})",
                             tuple(hosts))
        names = ", ".join(mention(r["user_id"], r["first_name"] or r["username"] or "?") for r in rows)
        await ctx.reply(f"🎤 이벤트 진행자: {names}\n진행자는 <code>!지급 @아이디 금액</code> 으로 자기 포인트를 나눠 줄 수 있어요.")
        return
    if not await _admin(ctx):
        await ctx.reply("진행자는 '사용자 차단' 권한이 있는 관리자만 정할 수 있어요.")
        return
    who, _, err = await _target(ctx, ctx.args)
    if err:
        await ctx.reply(err)
        return
    uid = who["user_id"]
    on = uid not in hosts
    hosts = (hosts | {uid}) if on else (hosts - {uid})
    await db.set_setting(ctx.chat_id, "casino_hosts", ",".join(map(str, sorted(hosts))))
    await db.log_mod(ctx.chat_id, ctx.user.id, uid, "event_host", "지정" if on else "해제")
    await ctx.reply(f"🎤 {mention(uid, who['name'])}님을 이벤트 진행자로 {'정했어요' if on else '뺐어요'}."
                    + ("\n<code>!지급 @아이디 금액</code> 으로 자기 포인트를 나눠 줄 수 있어요." if on else ""))


# ── 지급 (진행자 잔액 → 상대) ─────────────────────────────────
async def c_pay(ctx: Ctx) -> None:
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    s = await db.get_settings(cid)
    if uid not in _hosts(s) and not await _admin(ctx):
        await ctx.reply("🎤 <code>!지급</code> 은 이벤트 진행자·관리자만 써요.")
        return
    who, rest, err = await _target(ctx, ctx.args)
    if err:
        await ctx.reply(err)
        return
    to = who["user_id"]
    if to == uid:
        await ctx.reply("자기 자신에게는 못 보내요.")
        return
    bal = await balance(db, cid, uid)
    amount = next((n for a in rest if (n := parse_amount(a, bal)) is not None), None)
    if not amount or amount <= 0:
        await ctx.reply("금액을 적어주세요. 예: <code>!지급 @아이디 5000만</code> · <code>1억</code> · <code>3천만</code>")
        return
    ts = int(time.time())

    def run(c) -> bool:   # 빼기·넣기·원장 둘을 한 번에 (한쪽만 되지 않게)
        if c.execute("UPDATE members SET points=points-? WHERE chat_id=? AND user_id=? AND points>=?",
                     (amount, cid, uid, amount)).rowcount != 1:
            return False
        c.execute("INSERT OR IGNORE INTO casino_accounts(chat_id, user_id, joined_at) VALUES(?,?,?)", (cid, to, ts))
        c.execute("INSERT INTO members(chat_id, user_id, points, last_seen) VALUES(?,?,?,?) "
                  "ON CONFLICT(chat_id, user_id) DO UPDATE SET points=points+excluded.points", (cid, to, amount, ts))
        c.execute("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                  (cid, uid, -amount, f"pay_out:{to}", ts))
        c.execute("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                  (cid, to, amount, f"pay_in:{uid}", ts))
        return True
    if not await db.atomic(run):
        await ctx.reply(f"잔액이 모자라요. 지금 {big(bal)}")
        return
    await db.audit(cid, uid, to, "points_pay", f"{amount}")
    left, got = await balance(db, cid, uid), await balance(db, cid, to)
    await ctx.reply(f"🎁 {mention(to, who['name'])}님에게 <b>{big(amount)}</b> 지급!\n"
                    f"받은 사람 잔액 {big(got)} · 진행자 남은 {big(left)}")


# ── 이벤트 시간·순위 ─────────────────────────────────────────
_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?(?:시)?$")


def _at(now: datetime, raw: str) -> datetime | None:
    m = _TIME.match(raw.strip())
    if not m or int(m.group(1)) > 24 or int(m.group(2) or 0) > 59:
        return None
    h, mi = int(m.group(1)) % 24, int(m.group(2) or 0)
    return now.replace(hour=h, minute=mi, second=0, microsecond=0)


def plan(now: datetime, start_raw: str, end_raw: str) -> tuple[int, int] | None:
    """'00:00' '03:00' → (시작, 끝) 유닉스 초. 시작이 이미 지났고 끝도 지났으면 다음 날로, 끝이 시작보다 이르면 끝은 다음 날."""
    s, e = _at(now, start_raw), _at(now, end_raw)
    if not s or not e:
        return None
    if e <= s:
        e += timedelta(days=1)
    if e <= now:
        s, e = s + timedelta(days=1), e + timedelta(days=1)
    return int(s.timestamp()), int(e.timestamp())


def _event(s: dict) -> tuple[int, int, int, str] | None:
    parts = str(s.get("casino_event") or "").split(",")
    if len(parts) not in (3, 4) or not all(p.strip().lstrip("-").isdecimal() for p in parts[:3]):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2]), (parts[3].strip() if len(parts) == 4 else "")


def _amount_text(raw: str) -> int | None:
    raw = raw.replace(",", "").strip()
    m = re.fullmatch(r"(\d+)(억|천만|만|천)?", raw)
    if not m:
        return None
    return int(m.group(1)) * {"억": 100_000_000, "천만": 10_000_000, "만": 10_000, "천": 1_000}.get(m.group(2) or "", 1)


async def standings(db, cid: int, start: int, end: int, limit: int = TOP, games: str = "") -> list:
    names = GAMES.get(games, ())
    only = (" AND substr(l.reason, instr(l.reason, ':') + 1) IN (" + ",".join("?" * len(names)) + ")") if names else ""
    return await db._all(
        f"SELECT l.user_id, SUM(l.delta) AS net, u.first_name, u.last_name, u.username FROM casino_ledger l "
        f"LEFT JOIN users u ON u.user_id=l.user_id WHERE l.chat_id=? AND l.ts>=? AND l.ts<? AND {GAME_REASONS}{only} "
        f"GROUP BY l.user_id HAVING net > 0 ORDER BY net DESC LIMIT ?", (cid, start, end, *names, limit))


def _when(svc, ts: int) -> str:
    d = datetime.fromtimestamp(ts, svc.cfg.tz)
    return d.strftime("%m/%d %H:%M")


async def c_event(ctx: Ctx) -> None:
    db, cid = ctx.svc.db, ctx.chat_id
    if ctx.args:
        if not await _admin(ctx):
            await ctx.reply("이벤트 시간은 '사용자 차단' 권한이 있는 관리자만 정해요. 순위는 <code>!이벤트순위</code>")
            return
        if ctx.args[0] in ("끄기", "끝", "off", "취소"):
            await db.set_setting(cid, "casino_event", "")
            await ctx.reply("🎉 이벤트를 껐어요.")
            return
        if len(ctx.args) < 3 or not (p := plan(datetime.now(ctx.svc.cfg.tz), ctx.args[0], ctx.args[1])) \
                or (goal := _amount_text(ctx.args[2])) is None:
            await ctx.reply("사용법: <code>!이벤트 00:00 03:00 10억 바카라</code> (시작 끝 목표 순이익 [게임]) · <code>!이벤트 끄기</code>")
            return
        games = next((a for a in ctx.args[3:] if a in GAMES), "")
        await db.set_setting(cid, "casino_event", f"{p[0]},{p[1]},{goal}" + (f",{games}" if games else ""))
        await db.log_mod(cid, ctx.user.id, None, "event_set", f"{_when(ctx.svc, p[0])}~{_when(ctx.svc, p[1])} 목표 {goal}")
        await ctx.reply(f"🎉 이벤트: <b>{_when(ctx.svc, p[0])} ~ {_when(ctx.svc, p[1])}</b>\n"
                        f"이 시간에 {games or '게임'}으로 번 순수익 기준 · 목표 <b>{big(goal)}</b> 이상만 수상 후보\n"
                        "순위: <code>!이벤트순위</code>")
        return
    await c_rank(ctx)


async def c_rank(ctx: Ctx) -> None:
    db, cid = ctx.svc.db, ctx.chat_id
    ev = _event(await db.get_settings(cid))
    if not ev:
        await ctx.reply("진행 중인 이벤트가 없어요. 관리자가 <code>!이벤트 00:00 03:00 10억</code> 으로 정해요.")
        return
    start, end, goal, games = ev
    now = int(time.time())
    head = f"🎉 <b>{games + ' ' if games else ''}이벤트 순위</b> ({_when(ctx.svc, start)} ~ {_when(ctx.svc, end)}) · 목표 {big(goal)}"
    if now < start:
        left = start - now
        await ctx.reply(f"{head}\n⏳ 시작까지 {left // 3600}시간 {left % 3600 // 60}분")
        return
    rows = await standings(db, cid, start, min(now, end) + 1, games=games)
    state = "🏁 <b>최종 결과</b>" if now >= end else f"⏱ 진행 중 · 남은 시간 {(end - now) // 60}분"
    if not rows:
        await ctx.reply(f"{head}\n{state}\n아직 순수익이 난 사람이 없어요.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines, rank = [head, state, ""], 0
    for r in rows:
        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or (r["username"] and "@" + r["username"]) or "?"
        ok = r["net"] >= goal
        if ok:
            rank += 1
        mark = (medals[rank - 1] if rank <= 3 else f"{rank}위") if ok else "·"
        lines.append(f"{mark} {esc(name)} — <b>{big(r['net'])}</b>" + ("" if ok else " (목표 미달)"))
    lines.append(f"\n순수익 = 이 시간 {games or '게임'} 베팅·당첨만 (지급·리필·채굴 제외)")
    await ctx.reply("\n".join(lines))


register(("진행자", "이벤트진행자"), c_host, help="이벤트 진행자 보기·정하기 (관리자)", group="이벤트", needs_account=False)
register(("지급", "상금"), c_pay, help="진행자가 자기 포인트를 나눠 줌 (!지급 @아이디 금액)", group="이벤트", needs_account=False)
register(("이벤트",), c_event, help="이벤트 순위 · 관리자: !이벤트 00:00 03:00 10억", group="이벤트", needs_account=False)
register(("이벤트순위", "이순위"), c_rank, help="이벤트 순위 (그 시간 게임 순수익)", group="이벤트", needs_account=False)
