"""포인트 지갑 · 가입 · 채굴 · 출석 · 파산 구제 · 베팅 공통 처리.

돈 관련 규칙 (바꾸지 말 것): 포인트는 충전·환전·이체가 없다. 단위는 'P' (원·₩ 금지).
잔액 변경은 전부 `debit`/`credit` 두 함수만 거치고, 모든 변동은 casino_ledger 에 남는다.
차감은 SQL 한 문장(`points >= ?` 조건)이라 동시에 두 번 베팅해도 잔액이 음수가 되지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from .. import persist
from ..db import now, register_schema
from ..settings import register_setting
from ..util import esc, mention, user_name
from . import Ctx, register

if TYPE_CHECKING:
    from . import CasinoCmd

register_schema("""
CREATE TABLE IF NOT EXISTS casino_accounts (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    joined_at    INTEGER NOT NULL,
    last_mine    INTEGER NOT NULL DEFAULT 0,
    mine_streak  INTEGER NOT NULL DEFAULT 0,
    last_daily   TEXT NOT NULL DEFAULT '',
    last_bailout TEXT NOT NULL DEFAULT '',
    wagered      INTEGER NOT NULL DEFAULT 0,
    won          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS casino_ledger (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    delta   INTEGER NOT NULL,
    reason  TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casino_ledger ON casino_ledger(chat_id, user_id, id);
CREATE TABLE IF NOT EXISTS casino_open (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount  INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
""", migrate={"casino_accounts": "composite", "casino_ledger": "plain", "casino_open": "composite"})

register_setting("casino_enabled", True, "포인트 게임(! 명령)")
register_setting("casino_max_bet", 100_000, "포인트 게임 최대 베팅", range_=(100, 100_000_000))

START_POINTS = 10_000
MIN_BET = 100
MINE_COOLDOWN = 600            # 채굴 10분마다
MINE_MIN, MINE_MAX = 200, 1_000
MINE_JACKPOT_ODDS = 50         # 1/50 확률 💎 대박 광맥 ×10
MINE_STREAK_MAX = 10           # 연속 채굴(1시간 안에 다시) 보너스 최대 +10%×10
DAILY_POINTS = 5_000
BAILOUT_POINTS = 3_000         # 잔액 MIN_BET 미만일 때 하루 1번
BET_GAP = 2.0                  # 같은 사람 연속 베팅 최소 간격(초) — 방 도배 방지
_last_bet: dict[tuple[int, int], float] = {}
GATE_QUIET = 600               # 꺼진 방·이용 기간 아닌 방 안내는 방당 10분에 한 번 (나머지 ! 명령은 조용히)
TEMP_SECS = 20                 # 잠깐 보이고 지워지는 답장 (참가 확인 등)
temp_sleep = asyncio.sleep     # 테스트에서 바꿔 끼움
_TEMP: set[asyncio.Task] = set()
log = logging.getLogger(__name__)


def fmt(n: int) -> str:
    return f"{n:,}P"


def today(svc) -> str:
    return datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")


# ── 계정 · 잔액 ───────────────────────────────────────────
async def account(db, chat_id: int, user_id: int):
    return await db._one("SELECT * FROM casino_accounts WHERE chat_id=? AND user_id=?", (chat_id, user_id))


async def balance(db, chat_id: int, user_id: int) -> int:
    row = await db._one("SELECT points FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return row["points"] if row else 0


def _open(c, chat_id: int, user_id: int, delta: int, ts: int) -> None:
    """열린 베팅(casino_open) 합계를 delta 만큼. 0 이하가 되면 지운다. debit/credit 의 db.atomic 안에서만."""
    c.execute("INSERT INTO casino_open(chat_id, user_id, amount, ts) VALUES(?,?,?,?) "
              "ON CONFLICT(chat_id, user_id) DO UPDATE SET amount=amount+excluded.amount, ts=excluded.ts",
              (chat_id, user_id, delta, ts))
    c.execute("DELETE FROM casino_open WHERE chat_id=? AND user_id=? AND amount<=0", (chat_id, user_id))


async def credit(db, chat_id: int, user_id: int, amount: int, reason: str,
                 cond: tuple[str, tuple] | None = None, close: int = 0) -> int | None:
    """포인트 지급. 새 잔액. cond=(UPDATE 문, 인자) 를 주면 그 문장이 한 줄을 바꿀 때만 같이 지급 (쿨타임 표시와 지급을
    한 번에 — 한쪽만 저장되지 않게), 못 바꾸면 None. 'win:' 은 누적 당첨도 같이.
    close = 이 지급으로 끝나는 베팅 금액 (정산·환불) → 열린 베팅에서 같이 뺀다 (지급과 '끝남' 표시가 한 번에)."""
    if amount <= 0 and not cond and not close:
        return await balance(db, chat_id, user_id)
    ts = now()

    def run(c) -> bool:   # 잔액과 원장을 함께 (한쪽만 저장되지 않게, db.atomic)
        if cond and c.execute(*cond).rowcount != 1:
            return False
        if close:
            _open(c, chat_id, user_id, -close, ts)
        if amount > 0:
            c.execute("INSERT INTO members(chat_id, user_id, points, last_seen) VALUES(?, ?, ?, ?) "
                      "ON CONFLICT(chat_id, user_id) DO UPDATE SET points=points+excluded.points",
                      (chat_id, user_id, amount, ts))
            c.execute("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                      (chat_id, user_id, amount, reason[:40], ts))
            if reason.startswith("win:"):
                c.execute("UPDATE casino_accounts SET won=won+? WHERE chat_id=? AND user_id=?", (amount, chat_id, user_id))
        return True
    if not await db.atomic(run):
        return None
    return await balance(db, chat_id, user_id)


async def debit(db, chat_id: int, user_id: int, amount: int, reason: str) -> bool:
    """잔액이 충분할 때만 차감 (한 문장이라 동시 베팅에도 음수 불가). 성공하면 True. 'bet:' 은 누적 베팅도 같이."""
    if amount <= 0:
        return False
    ts = now()

    def run(c) -> bool:   # 차감과 원장을 함께 (차감만 되고 기록이 빠지지 않게, db.atomic)
        if c.execute("UPDATE members SET points=points-? WHERE chat_id=? AND user_id=? AND points>=?",
                     (amount, chat_id, user_id, amount)).rowcount != 1:
            return False
        c.execute("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                  (chat_id, user_id, -amount, reason[:40], ts))
        if reason.startswith("bet:"):
            c.execute("UPDATE casino_accounts SET wagered=wagered+? WHERE chat_id=? AND user_id=?", (amount, chat_id, user_id))
            _open(c, chat_id, user_id, amount, ts)     # 정산·환불(credit close=)될 때까지 '열린 베팅'
        return True
    return await db.atomic(run)


# ── 열린 베팅 (정산 전 오류 환불 · 파산 구제 악용 막기 · 강제 종료 뒤 환불) ──────────────
# DB casino_open = 'bet:' 차감 ~ 정산·환불(credit close=) 사이의 돈 (같은 트랜잭션에서 더하고 뺌). 봇이 kill -9·컨테이너 회수로
# 정산 없이 죽어도 다음 시작 때 recover_open 이 돌려준다. _STAKED 는 명령 하나 안의 오류 환불용 (메모리).
_STAKED: dict[tuple[int, int], int] = {}   # (방, 사람) → 명령·버튼 안에서 차감했지만 아직 정산 안 된 베팅
OPEN_CHECKS: list[Callable[[int, int], bool]] = []   # 명령 밖에서 이어지는 판(카드·멀티)이 열려 있나 — 각 모듈이 등록
SWEEPS: list = []                          # async fn() — 시간 지난 판 정리 (파산 구제 전에 불러 정산부터)


async def recover_open(db) -> int:
    """봇 시작 때 (판이 하나도 없을 때): 지난 실행에서 정산 못 한 베팅을 환불. 환불한 사람 수."""
    ts = now()

    def run(c) -> int:
        rows = c.execute("SELECT chat_id, user_id, amount FROM casino_open").fetchall()
        n = 0
        for cid, uid, amount in rows:
            if amount > 0:
                c.execute("UPDATE members SET points=points+? WHERE chat_id=? AND user_id=?", (amount, cid, uid))
                c.execute("INSERT INTO casino_ledger(chat_id, user_id, delta, reason, ts) VALUES(?,?,?,?,?)",
                          (cid, uid, amount, "refund:restart", ts))
                # 방에 '봇이 다시 시작돼서 게임을 끝냈어요' (persist.announce_ended, 강제 종료로 알림을 못 남긴 경우)
                c.execute("INSERT OR IGNORE INTO live_games(chat_id, kind, title, ts) VALUES(?, 'casino', '포인트 게임', ?)",
                          (cid, ts))
                n += 1
        c.execute("DELETE FROM casino_open")
        return n
    n = await db.atomic(run)
    if n:
        log.warning("casino: 지난 실행에서 정산 못 한 베팅 %d건 환불", n)
    return n


def _stake(ctx: Ctx, delta: int) -> None:
    ctx.staked += delta
    key = (ctx.chat_id, ctx.user.id)
    n = _STAKED.get(key, 0) + delta
    if n > 0:
        _STAKED[key] = n
    else:
        _STAKED.pop(key, None)


def handoff(ctx: Ctx, bet: int) -> None:
    """판(카드 핸드·멀티 참가)이 이 베팅의 정산·환불을 맡음 → 명령 쪽 오류 환불에서 뺀다."""
    _stake(ctx, -min(bet, ctx.staked))


async def has_open_bet(db, chat_id: int, user_id: int) -> bool:
    if _STAKED.get((chat_id, user_id)) or any(f(chat_id, user_id) for f in OPEN_CHECKS):
        return True
    return bool(await db._one("SELECT 1 FROM casino_open WHERE chat_id=? AND user_id=? AND amount>0", (chat_id, user_id)))


async def guarded(ctx: Ctx, fn) -> None:
    """명령·버튼 실행: 베팅을 뺀 뒤 정산 전에 오류·취소가 나면 그 베팅을 돌려준다 (돈만 사라지지 않게)."""
    try:
        await fn(ctx)
    except BaseException:
        if ctx.staked > 0:
            await credit(ctx.svc.db, ctx.chat_id, ctx.user.id, ctx.staked, f"refund:{ctx.game}", close=ctx.staked)
            log.warning("casino %s failed after bet: refunded %d", ctx.game, ctx.staked)
        raise
    finally:
        _stake(ctx, -ctx.staked)


# ── 베팅 금액 ─────────────────────────────────────────────
_AMOUNT = re.compile(r"^(\d{1,12})(만|천|k|K)?$")


def parse_amount(raw: str, bal: int) -> int | None:
    """'1000', '5천', '3만', '1k', '올인', '반', '반띵' → 포인트. 형식이 아니면 None."""
    raw = raw.strip().replace(",", "")
    if raw in ("올인", "allin", "all", "다"):
        return bal
    if raw in ("반", "반띵", "half"):
        return bal // 2
    m = _AMOUNT.match(raw)
    if not m:
        return None
    n = int(m.group(1)) * {"만": 10_000, "천": 1_000, "k": 1_000, "K": 1_000}.get(m.group(2) or "", 1)
    return n


def split_bet(args: list[str], bal: int) -> tuple[int | None, list[str]]:
    """인자에서 금액 하나를 찾고 나머지(선택지)를 돌려준다. 순서 자유: '!홀짝 홀 1000' · '!주사위 6 1000'.
    최소 베팅 이상인 첫 값이 금액 (선택지 숫자 0~36·배수는 그보다 작음). 없으면 처음 나온 금액 형식 (→ 최소 베팅 안내)."""
    found = [(i, n) for i, a in enumerate(args) if (n := parse_amount(a, bal)) is not None]
    if not found:
        return None, args
    i, n = next(((i, n) for i, n in found if n >= MIN_BET), found[0])
    return n, args[:i] + args[i + 1:]


async def take_bet(ctx: Ctx, amount: int | None, game: str) -> int | None:
    """베팅 검사 + 차감. 실패하면 안내하고 None."""
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    key, t = (cid, uid), time.monotonic()
    prev = _last_bet.get(key, 0)
    if t - prev < BET_GAP:
        await ctx.reply("⏳ 조금만 천천히! (2초에 한 번)")
        return None
    _last_bet[key] = t                     # await 전에 (동시에 온 두 판이 둘 다 간격 검사를 지나지 않게)
    if len(_last_bet) > 5000:
        _last_bet.clear()
    bal = await balance(db, cid, uid)
    max_bet = (await db.get_settings(cid))["casino_max_bet"]
    if amount is None:
        err = (f"베팅 금액을 적어주세요. 예: <code>1000</code> · <code>5천</code> · <code>1만</code> · <code>올인</code>\n"
               f"내 잔액: <b>{fmt(bal)}</b>")
    elif amount < MIN_BET:
        err = (f"최소 베팅은 {fmt(MIN_BET)}예요. 잔액: <b>{fmt(bal)}</b>"
               + ("\n포인트가 모자라면 <code>!채굴</code> · <code>!출석</code> · <code>!파산</code>" if bal < MIN_BET else ""))
    elif amount > max_bet:
        err = f"이 방 최대 베팅은 {fmt(max_bet)}예요."
    else:
        ctx.game = game
        _stake(ctx, amount)                # 차감 전에 표시 (그 사이 !파산 이 '열린 베팅 없음'으로 보지 않게)
        try:
            ok = await debit(db, cid, uid, amount, f"bet:{game}")
        except BaseException:              # 차감 자체가 실패(되돌려짐) → 오류 환불 대상 아님 (안 뺀 돈을 돌려주던 것)
            _stake(ctx, -amount)
            raise
        if ok:
            return amount
        _stake(ctx, -amount)
        err = f"잔액이 모자라요. 잔액: <b>{fmt(await balance(db, cid, uid))}</b>"
    if _last_bet.get(key) == t:            # 거절된 시도는 간격에 안 셈
        _last_bet[key] = prev
    await ctx.reply(err)
    return None


async def settle(ctx: Ctx, bet: int, payout: int, game: str) -> int:
    """결과 정산: payout(원금 포함 받을 돈, 지면 0) 지급. 새 잔액."""
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    handoff(ctx, bet)                      # 정산이 시작되면 오류 환불 대상 아님 (지급이 반쯤 되고 환불까지 되는 일 없게)
    return await credit(db, cid, uid, payout, f"win:{game}", close=bet)   # 지급과 '열린 베팅 끝' 을 한 번에


async def settle_text(ctx: Ctx, game: str, bet: int, payout: int) -> tuple[int, str]:
    """정산 + 꼬리 두 줄 (손익·잔액 / 딜러 소담 한마디). (새 잔액, 꼬리)."""
    from .dealer import line
    bal = await settle(ctx, bet, payout, game)
    style = (await ctx.svc.db.get_settings(ctx.chat_id))["style"]
    return bal, f"{result_line(bet, payout, bal)}\n{line(style, bet, payout, bal)}"


async def finish(ctx: Ctx, game: str, bet: int, payout: int, body: str, **reply_kw) -> int:
    """정산 + 결과 메시지 (게임 결과 줄 → 손익·잔액 → 딜러 소담 한마디). 새 잔액."""
    bal, tail = await settle_text(ctx, game, bet, payout)
    await ctx.reply(f"{body}\n{tail}", **reply_kw)
    return bal


async def dealer_tail(ctx: Ctx, bet: int, payout: int, bal: int) -> str:
    """결과 문구 뒤에 붙일 딜러 소담 한마디 (방 말투 반영)."""
    from .dealer import line
    return "\n" + line((await ctx.svc.db.get_settings(ctx.chat_id))["style"], bet, payout, bal)


def result_line(bet: int, payout: int, bal: int) -> str:
    if payout > bet:
        return f"🎉 <b>+{fmt(payout - bet)}</b> 획득! · 잔액 {fmt(bal)}"
    if payout == bet:
        return f"🤝 본전 · 잔액 {fmt(bal)}"
    if payout > 0:
        return f"😅 {fmt(payout)} 돌려받음 · 잔액 {fmt(bal)}"
    return f"💸 -{fmt(bet)} · 잔액 {fmt(bal)}"


def rng(n: int) -> int:
    """0 ~ n-1 (암호학적 난수: 예측 불가)."""
    return secrets.randbelow(n)


# ── 잠깐 보이는 답장 ──────────────────────────────────────
async def _delete_later(bot, chat_id: int, message_id: int, secs: float, db=None) -> None:
    try:
        if db is not None:                      # 재시작돼도 지우게 DB 에도 (sodam/persist.py sweep)
            await persist.remember_delete(db, bot, chat_id, message_id, secs)
        await temp_sleep(secs)
        await persist.delete_now(db, bot, chat_id, message_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:                      # 이미 지워짐·권한 없음·DB 닫힘: 그냥 남김
        log.debug("temp delete failed: %r", e)


def delete_later(bot, chat_id: int, message_id: int | None, secs: float = TEMP_SECS, db=None) -> None:
    """secs 초 뒤 그 메시지를 지운다 (asyncio 태스크 + DB 기록: 봇이 꺼지면 다음 실행의 persist.sweep 이 지움)."""
    if message_id is None:
        return
    db = db if db is not None else persist.db_of(bot)
    task = asyncio.create_task(_delete_later(bot, chat_id, message_id, secs, db))
    _TEMP.add(task)
    task.add_done_callback(_TEMP.discard)


async def temp_reply(ctx: Ctx, text: str, secs: float = TEMP_SECS):
    """방에 쌓이지 않는 답장: 보내고 secs 초 뒤 삭제 (🎫 참가 확인·'이미 걸었어요' 같은 안내)."""
    sent = await ctx.reply(text)
    delete_later(ctx.bot, ctx.chat_id, getattr(sent, "message_id", None), secs, ctx.svc.db)
    return sent


# ── 사용 가능 여부 ────────────────────────────────────────
ROOM_OFF = "이 방은 포인트 게임이 꺼져 있어요. (이 안내는 10분에 한 번만 나와요)"
ROOM_UNPAID = "포인트 게임은 이용 기간 중인 방에서 쓸 수 있어요. (이 안내는 10분에 한 번만 나와요)"
_room_noticed: dict[int, float] = {}


def room_notice_due(chat_id: int, text: str) -> bool:
    """방 전체가 막힌 이유(게임 꺼짐·이용 기간 아님)는 방당 GATE_QUIET 초에 한 번만 방에 보이게. 다른 안내는 항상 True."""
    if text not in (ROOM_OFF, ROOM_UNPAID):
        return True
    t = time.monotonic()
    if t - _room_noticed.get(chat_id, -1e18) < GATE_QUIET:
        return False
    _room_noticed[chat_id] = t
    return True


async def gate(ctx: Ctx, cmd: CasinoCmd) -> str | None:
    if ctx.chat_id > 0:
        return "포인트 게임은 그룹방에서 해요. 봇을 넣은 방에서 <code>!가입</code>"
    s = await ctx.svc.db.get_settings(ctx.chat_id)
    if not (s["casino_enabled"] and s["games_enabled"]):
        return ROOM_OFF
    if not await ctx.svc.paid_features(ctx.chat_id):
        return ROOM_UNPAID
    if cmd.needs_account and not await account(ctx.svc.db, ctx.chat_id, ctx.user.id):
        return f"{mention(ctx.user.id, user_name(ctx.user))}님, 먼저 <code>!가입</code> 해주세요! (가입하면 {fmt(START_POINTS)} 지급)"
    return None


# ── 명령 ──────────────────────────────────────────────────
async def c_join(ctx: Ctx) -> None:
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    if await account(db, cid, uid):
        await ctx.reply(f"이미 가입했어요. 잔액: <b>{fmt(await balance(db, cid, uid))}</b> · <code>!도움</code>")
        return
    cur = await db.conn.execute("INSERT OR IGNORE INTO casino_accounts(chat_id, user_id, joined_at) VALUES(?,?,?)",
                                (cid, uid, now()))
    await db.conn.commit()
    if cur.rowcount != 1:  # 동시에 두 번 누른 경우
        return
    await db.touch_member(cid, uid)
    bal = await credit(db, cid, uid, START_POINTS, "join")
    n = (await db._one("SELECT COUNT(*) AS n FROM casino_accounts WHERE chat_id=?", (cid,)))["n"]
    await ctx.reply(f"🎉 {mention(uid, user_name(ctx.user))}님 가입 완료! (이 방 {n}번째)\n"
                    f"가입 선물 <b>{fmt(START_POINTS)}</b> · 잔액 {fmt(bal)}\n\n"
                    "⛏ <code>!채굴</code> 10분마다 포인트 캐기 · 📅 <code>!출석</code> 하루 한 번\n"
                    "🎰 게임 목록은 <code>!도움</code>")


async def c_mine(ctx: Ctx) -> None:
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    acc = await account(db, cid, uid)
    t = now()
    left = acc["last_mine"] + MINE_COOLDOWN - t
    if left > 0:
        await ctx.reply(f"⛏ 곡괭이 식는 중… {left // 60}분 {left % 60}초 뒤에 다시 캘 수 있어요.")
        return
    streak = acc["mine_streak"] + 1 if t - acc["last_mine"] < 3600 else 1
    base = MINE_MIN + rng(MINE_MAX - MINE_MIN + 1)
    jackpot = rng(MINE_JACKPOT_ODDS) == 0
    bonus_pct = min(streak - 1, MINE_STREAK_MAX) * 10
    amount = base * (10 if jackpot else 1)
    amount += amount * bonus_pct // 100
    bal = await credit(db, cid, uid, amount, "mine", (   # 쿨타임 확인+갱신과 지급을 한 번에 (연타해도 한 번만)
        "UPDATE casino_accounts SET last_mine=?, mine_streak=? WHERE chat_id=? AND user_id=? AND last_mine=?",
        (t, streak, cid, uid, acc["last_mine"])))
    if bal is None:
        return
    head = "💎 <b>대박 광맥 발견!!</b> ×10\n" if jackpot else ""
    streak_txt = f" (연속 {streak}회 +{bonus_pct}%)" if bonus_pct else ""
    await ctx.reply(f"{head}⛏ {esc(user_name(ctx.user))}님 채굴 성공: <b>+{fmt(amount)}</b>{streak_txt}\n잔액 {fmt(bal)} · 10분 뒤 다시")


async def c_daily(ctx: Ctx) -> None:
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    day = today(ctx.svc)
    bal = await credit(db, cid, uid, DAILY_POINTS, "daily", (
        "UPDATE casino_accounts SET last_daily=? WHERE chat_id=? AND user_id=? AND last_daily<>?", (day, cid, uid, day)))
    if bal is None:
        await ctx.reply("📅 오늘은 이미 출석했어요. 내일 또 와요!")
        return
    await ctx.reply(f"📅 출석 완료! <b>+{fmt(DAILY_POINTS)}</b> · 잔액 {fmt(bal)}")


async def c_bailout(ctx: Ctx) -> None:
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    if await balance(db, cid, uid) >= MIN_BET:
        await ctx.reply(f"파산 구제는 잔액이 {fmt(MIN_BET)} 미만일 때만 돼요. <code>!채굴</code> 해보세요!")
        return
    for sweep in SWEEPS:                   # 시간 지난 판은 먼저 정산 (자리 비운 판이 구제를 영영 막지 않게)
        await sweep()
    if await has_open_bet(db, cid, uid):   # 올인 결과를 기다리는 중 구제받고 이기면 둘 다 챙기는 것 막기
        await ctx.reply("🆘 진행 중인 판이 끝난 뒤에 해주세요.")
        return
    day = today(ctx.svc)
    bal = await credit(db, cid, uid, BAILOUT_POINTS, "bailout", (   # 잔액·열린 베팅·하루 1번을 지급과 한 문장에서 다시 확인
        "UPDATE casino_accounts SET last_bailout=? WHERE chat_id=? AND user_id=? AND last_bailout<>? "   # (위 확인과 지급 사이에
        "AND (SELECT COALESCE(MAX(points), 0) FROM members WHERE chat_id=? AND user_id=?) < ? "          #  당첨금이 들어오면 거절)
        "AND NOT EXISTS (SELECT 1 FROM casino_open WHERE chat_id=? AND user_id=? AND amount>0)",
        (day, cid, uid, day, cid, uid, MIN_BET, cid, uid)))
    if bal is None:
        if (await account(db, cid, uid))["last_bailout"] == day:
            await ctx.reply("🆘 파산 구제는 하루 한 번이에요. <code>!채굴</code> 로 다시 일어서요!")
        elif await balance(db, cid, uid) >= MIN_BET:
            await ctx.reply(f"파산 구제는 잔액이 {fmt(MIN_BET)} 미만일 때만 돼요. 방금 포인트가 들어왔어요!")
        else:
            await ctx.reply("🆘 진행 중인 판이 끝난 뒤에 해주세요.")
        return
    await ctx.reply(f"🆘 파산 구제 지원금 <b>+{fmt(BAILOUT_POINTS)}</b> · 잔액 {fmt(bal)}\n다시 가보자고요 💪")


async def c_wallet(ctx: Ctx) -> None:
    db, cid, uid = ctx.svc.db, ctx.chat_id, ctx.user.id
    acc = await account(db, cid, uid)
    bal = await balance(db, cid, uid)
    rank = (await db._one("SELECT COUNT(*) AS n FROM members m JOIN casino_accounts a USING(chat_id, user_id) "
                          "WHERE m.chat_id=? AND m.points>?", (cid, bal)))["n"] + 1
    left = max(0, acc["last_mine"] + MINE_COOLDOWN - now())
    mine = "지금 가능" if not left else f"{left // 60}분 {left % 60}초 뒤"
    daily = "완료" if acc["last_daily"] == today(ctx.svc) else "가능"
    await ctx.reply(f"💰 <b>{esc(user_name(ctx.user))}</b>님 지갑\n"
                    f"잔액 <b>{fmt(bal)}</b> · 방 순위 {rank}위\n"
                    f"누적 베팅 {fmt(acc['wagered'])} · 누적 당첨 {fmt(acc['won'])}\n"
                    f"⛏ 채굴 {mine} · 📅 출석 {daily}")


async def c_rank(ctx: Ctx) -> None:
    rows = await ctx.svc.db._all(
        "SELECT m.user_id, m.points, u.first_name, u.last_name, u.username FROM members m "
        "JOIN casino_accounts a USING(chat_id, user_id) JOIN users u ON u.user_id=m.user_id "
        "WHERE m.chat_id=? ORDER BY m.points DESC LIMIT 10", (ctx.chat_id,))
    if not rows:
        await ctx.reply("아직 가입한 사람이 없어요. <code>!가입</code>")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>포인트 부자 순위</b>"]
    for i, r in enumerate(rows):
        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or (r["username"] and "@" + r["username"]) or "?"
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(name)} — <b>{fmt(r['points'])}</b>")
    await ctx.reply("\n".join(lines))


async def c_help(ctx: Ctx) -> None:
    from . import help_text
    await ctx.reply(help_text())


register(("가입", "join"), c_join, help=f"게임 가입 ({fmt(START_POINTS)} 지급)", group="시작", needs_account=False)
register(("채굴", "mine", "캐기"), c_mine, help="10분마다 포인트 캐기 (가끔 💎 ×10)", group="시작")
register(("출석", "daily"), c_daily, help=f"하루 한 번 {fmt(DAILY_POINTS)}", group="시작")
register(("지갑", "잔액", "돈", "wallet"), c_wallet, help="내 포인트·순위", group="시작")
register(("순위", "랭킹", "부자", "rank"), c_rank, help="방 포인트 부자 순위", group="시작", needs_account=False)
register(("파산", "구제"), c_bailout, help=f"잔액 바닥이면 하루 한 번 {fmt(BAILOUT_POINTS)}", group="시작")
register(("도움", "게임", "help", "명령어"), c_help, help="이 목록", group="시작", needs_account=False)
