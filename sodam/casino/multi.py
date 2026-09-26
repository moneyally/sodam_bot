"""여러 명이 한 판에 같이 거는 방 게임: 📈 그래프(크래시) · 🏇 경마.

- 첫 베팅이 판을 연다 → 베팅 시간(그래프 15초·경마 20초) → 진행(메시지 하나를 1.5초마다 수정) → 결과판.
- 방마다 게임별로 한 판씩만. 한 사람은 한 판에 한 번만 건다.
- 베팅금은 참가할 때 take_bet 으로 바로 차감, 당첨금은 settle 로 딱 한 번 (Player.done 을 await 전에 세움).
- 판이 오류·취소로 버려지면 아직 정산 안 된 베팅금은 전부 환불(ledger 'refund:<게임>').
  봇이 판 도중에 꺼져도: 걸린 돈은 casino_open_stakes 에 적어 두고, 재시작 후 첫 명령 때 환불한다.
- 시계·sleep·난수는 모듈 변수(_clock/_sleep/_rand)라 테스트가 바꿔 끼운다 (진짜로 기다리지 않음).
- 버튼 없이 글자 명령만 쓴다 (!그래프 · !스톱 · !경마 · !라운드).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TelegramError

from ..db import now, register_schema
from ..util import esc, user_name
from . import SHUTDOWN_HOOKS, Ctx, anim, register, register_callback
from . import core
from .core import balance, credit, dealer_tail, fmt, result_line, settle, split_bet, take_bet, temp_reply
from .board import record
from .dealer import line as dealer_line

log = logging.getLogger(__name__)

register_schema("""
CREATE TABLE IF NOT EXISTS casino_open_stakes (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    game    TEXT NOT NULL,
    bet     INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id, game)
);
""", migrate={"casino_open_stakes": "composite"})

# 테스트에서 바꿔 끼우는 것들
_clock = time.monotonic
_sleep = asyncio.sleep
_rand = core.rng                 # n → 0..n-1
_crash_override = None           # 테스트: 다음 판 터지는 지점(센트, 예: 235 = 2.35x)

TICK = 3.0                       # 화면 수정 간격: 그룹은 보내기·수정 합쳐 분당 약 20개 제한 (텔레그램 FAQ)
RETRY_MAX = 30                   # 제한(429)에 걸린 마지막 화면·결과판은 최대 이만큼 기다렸다 다시
MAX_PLAYERS = 50
NAME_MAX = 12

# ── 📈 그래프 수학 (배수는 전부 정수 '센트': 100 = 1.00x) ─────
CRASH_WINDOW = 15
CRASH_CAP = 10_000               # 100x
CRASH_EDGE = 97                  # 기대 환급 97% (3% 는 1.00x 즉시 터짐)
GROWTH = math.log(100) / 58      # 58초에 100x (2x ≈ 8.7초) → 한 판 최대 약 1분
AUTO_MIN, AUTO_MAX = 101, CRASH_CAP


def crash_point(rand=None) -> int:
    """터지는 지점(센트). P(터짐 ≥ x) = 0.97/x → 어떤 지점에서 내려도 기대 환급 97%."""
    if _crash_override is not None:
        return _crash_override
    e = 1 << 52
    h = (rand or _rand)(e)
    return min(CRASH_CAP, max(100, CRASH_EDGE * e // (e - h)))


def mult_at(elapsed: float) -> int:
    return min(CRASH_CAP, int(100 * math.exp(GROWTH * max(0.0, elapsed)) + 1e-9))


def time_to(cents: int) -> float:
    return math.log(max(cents, 100) / 100) / GROWTH


def fx(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}x"


CHART_W, CHART_H = 16, 6          # 글자 차트 크기 (폰에서도 한 줄에 들어가게)
_EIGHTHS = " ▁▂▃▄▅▆▇█"


def heat(cents: int) -> str:
    return "🟢" if cents < 200 else "🟡" if cents < 500 else "🟠" if cents < 1000 else "🔴"


def chart(hist: list[int], top: int | None = None) -> str:
    """배수 기록(센트) → 막대 곡선 (한 칸 = 1/8 블록, 갈수록 가파르게 치솟음). 기록이 길면 폭에 맞게 골라 전체 비행이 보이게."""
    if len(hist) > CHART_W:
        hist = [hist[i * (len(hist) - 1) // (CHART_W - 1)] for i in range(CHART_W)]
    top = max(top or max(hist), 200)
    levels = [round((max(m, 100) - 100) / (top - 100) * CHART_H * 8) for m in hist]
    rows = []
    for r in range(CHART_H - 1, -1, -1):
        cells = "".join(_EIGHTHS[max(0, min(8, lv - r * 8))] for lv in levels)
        label = fx(top) if r == CHART_H - 1 else "1.00x" if r == 0 else ""
        rows.append(f"{label:>6}{'┤' if label else '│'}{cells}")
    return "\n".join(rows)


def parse_auto(raw: str) -> int | None:
    """'2.5' · '2.5x' · '2.5배' · '3' → 센트. 아니면 None."""
    raw = raw.strip().lower().removesuffix("x").removesuffix("배")
    try:
        v = float(raw)
    except ValueError:
        return None
    if not 0 < v <= AUTO_MAX / 100:   # round 전에 범위 검사 (inf·nan·1e308 도 여기서 걸러짐)
        return None
    return int(round(v * 100))


def crash_payout(bet: int, cash_at: int) -> int:
    return bet * cash_at // 100


# ── 🏇 경마 ──────────────────────────────────────────────
HORSE_WINDOW = 20
HORSES = 5
HORSE_PAY_X10 = 47               # ×4.7 (기대 환급 94%)
TRACK = 12
FRAMES = 6


def race_frames(winner: int, rand=None) -> list[list[int]]:
    """프레임별 말 위치(0..TRACK). 마지막 프레임에서 우승마만 결승선(TRACK)."""
    rand = rand or _rand
    paths = []
    for h in range(HORSES):
        final = TRACK if h == winner else TRACK - 1 - rand(5)
        steps = sorted(1 + rand(final) for _ in range(FRAMES - 1)) + [final]   # 첫 화면부터 움직임
        paths.append(steps)
    return [[paths[h][f] for h in range(HORSES)] for f in range(FRAMES)]


MEDALS = ("🥇", "🥈", "🥉")


def track_text(pos: list[int], title: str, picks: list[int] | None = None, final: bool = False) -> str:
    """경주 화면: 제목 → 선두·남은 칸 → <pre> 트랙 (줄 끝에 순위 메달, 결승 2칸 안이면 💨, 우승 🏆).
    picks: 말별 베팅 인원 (있으면 '👥n'). 같은 위치면 같은 순위."""
    ranks = sorted(set(pos), reverse=True)
    lead = max(pos)
    leaders = [h + 1 for h, p in enumerate(pos) if p == lead]
    if final:
        sub = ""
    elif lead == 0:
        sub = "\n출발선에 섰어요!"
    else:
        sub = f"\n선두 <b>{'·'.join(map(str, leaders))}번</b> · 결승까지 {TRACK - lead}칸" + (" 🔥" if TRACK - lead <= 2 else "")
    rows = []
    for h, p in enumerate(pos):
        r = ranks.index(p)
        mark = "🏆" if final and p == TRACK else (MEDALS[r] if r < 3 and lead > 0 else "  ")
        if not final and 0 < TRACK - p <= 2:
            mark += "💨"
        who = f" 👥{picks[h]}" if picks and picks[h] else ""
        rows.append((f"{h + 1} " + "·" * p + "🏇" + "·" * (TRACK - p) + "🏁" + mark + who).rstrip())
    return f"{title}{sub}\n<pre>" + "\n".join(rows) + "</pre>"


# ── 공통: 판 · 참가자 ─────────────────────────────────────
@dataclass
class Player:
    ctx: Ctx
    bet: int
    pick: int = 0                # 그래프: 자동 탈출(센트, 0=없음) · 경마: 말 번호(1~5)
    done: bool = False           # 정산(당첨·꽝·환불) 끝남 — await 전에 세운다
    cash_at: int = 0             # 그래프: 내린 배수
    payout: int = 0

    @property
    def uid(self) -> int:
        return self.ctx.user.id

    @property
    def name(self) -> str:
        n = user_name(self.ctx.user)
        return esc(n if len(n) <= NAME_MAX else n[:NAME_MAX - 1] + "…")


_ROUNDS: dict[tuple[int, str], "Round"] = {}
_LAST: dict[tuple[int, str], tuple[float, "Round"]] = {}   # 방금 끝난 판 (늦은 !스톱 안내용)
_TASKS: set[asyncio.Task] = set()
_PENDING: set[tuple[int, str, int]] = set()   # take_bet 도중인 (방, 게임, 사람) — 동시 두 번 베팅 막기


class Round:
    game = ""
    title = ""
    next_hint = ""
    window = 0

    def __init__(self, ctx: Ctx):
        self.svc, self.bot, self.chat_id = ctx.svc, ctx.bot, ctx.chat_id
        self.clock, self.sleep = _clock, _sleep
        self.players: dict[int, Player] = {}
        self.phase = "betting"          # betting → running → done
        self.opened = self.clock()
        self.task: asyncio.Task | None = None
        self.live = None                # 수정하는 메시지
        self.last_edit = -1e9
        self.edit_block = 0.0

    # 정산 — 전부 여기만 거친다
    async def _unstake(self, p: Player) -> None:
        await self.svc.db._write("DELETE FROM casino_open_stakes WHERE chat_id=? AND user_id=? AND game=?",
                                 (self.chat_id, p.uid, self.game))

    async def pay(self, p: Player, payout: int) -> int | None:
        """딱 한 번만. 이미 끝났으면 None. 새 잔액."""
        if p.done:
            return None
        p.done, p.payout = True, payout
        bal = await settle(p.ctx, p.bet, payout, self.game)
        await self._unstake(p)
        return bal

    async def refund(self, p: Player) -> None:
        if p.done:
            return
        p.done = True
        await credit(self.svc.db, self.chat_id, p.uid, p.bet, f"refund:{self.game}")
        await self._unstake(p)

    async def refund_all(self) -> int:
        n = 0
        for p in list(self.players.values()):
            if not p.done:
                n += 1
                try:
                    await self.refund(p)
                except Exception:
                    log.exception("refund failed %s %s", self.chat_id, p.uid)
        return n

    # 텔레그램
    async def send(self, text: str, kb=None):
        """제한(429)에 걸리면 한 번 기다렸다 다시 (결과판이 조용히 사라지지 않게)."""
        for attempt in (0, 1):
            try:
                return await self.bot.send_message(self.chat_id, text, parse_mode="HTML", reply_markup=kb)
            except RetryAfter as e:
                if attempt:
                    break
                await self.sleep(min(_secs(e), RETRY_MAX))
            except TelegramError as e:
                log.warning("%s send failed: %s", self.game, e)
                break
        return None

    async def send_anim(self, make_gif, caption: str) -> bool:
        """결과 애니메이션(GIF) 한 번 보내고 그 메시지를 live 로. 그리기·보내기 실패면 False (글자 연출로)."""
        try:
            gif = await asyncio.to_thread(make_gif)
            self.live = await self.bot.send_animation(self.chat_id, animation=gif, caption=caption, parse_mode="HTML")
        except Exception as e:
            log.warning("%s animation failed, text fallback: %r", self.game, e)
            return False
        self.last_edit = self.clock()
        return True

    async def edit(self, text: str, force: bool = False, kb=None, caption: bool = False) -> None:
        """TICK 에 한 번 이하, 오류는 건너뛴다. force(마지막 화면)는 간격·제한이 풀릴 때까지 기다렸다 고치고,
        그래도 429 면 한 번 더 기다렸다 다시."""
        if self.live is None:
            return
        if force:
            wait = max(TICK - (self.clock() - self.last_edit), self.edit_block - self.clock())
            if wait > 0:
                await self.sleep(min(wait, RETRY_MAX))
        t = self.clock()
        if not force and (t < self.edit_block or t - self.last_edit < TICK - 0.01):
            return
        self.last_edit = t
        for attempt in (0, 1):
            try:
                if caption:
                    await self.bot.edit_message_caption(chat_id=self.chat_id, message_id=self.live.message_id,
                                                        caption=text, parse_mode="HTML")
                else:
                    await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.live.message_id,
                                                     parse_mode="HTML", reply_markup=kb)   # kb 없으면 버튼 사라짐
                return
            except RetryAfter as e:
                self.edit_block = self.clock() + _secs(e)
                if not force or attempt:
                    return
                await self.sleep(min(_secs(e), RETRY_MAX))
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    log.warning("%s edit failed: %s", self.game, e)
                return
            except TelegramError as e:
                log.warning("%s edit failed: %s", self.game, e)
                return

    # 결과 봉인: 판을 열 때 결과의 지문(해시)만 보여주고, 끝나면 결과+열쇠를 공개 → 중간에 못 바꿨다는 증명
    def seal(self, result: str) -> None:
        self.sealed_result, self.salt = result, secrets.token_hex(8)
        self.sealed = hashlib.sha256(f"{result}|{self.salt}".encode()).hexdigest()

    def seal_line(self) -> str:
        return f"🔒 결과 봉인: <code>{self.sealed[:16]}</code>"

    def reveal_line(self) -> str:
        return (f"🔓 봉인 공개: <code>{self.sealed_result}|{self.salt}</code> → SHA-256 앞 16자리 "
                f"<code>{self.sealed[:16]}</code> (직접 확인 가능)")

    def left(self) -> int:
        return max(0, math.ceil(self.window - (self.clock() - self.opened)))

    # 진행
    async def run(self) -> None:
        try:
            await self.sleep(self.window)
            self.phase = "running"
            await self.play()
        except asyncio.CancelledError:
            self.phase = "done"
            await self.refund_all()
            raise
        except Exception:
            log.exception("%s round crashed in %s", self.game, self.chat_id)
            self.phase = "done"
            if await self.refund_all():
                await self.send(f"⚠️ {self.title} 진행 중 문제가 생겨 이번 판은 취소됐어요. 건 포인트는 전부 돌려드렸어요.")
        finally:
            self.phase = "done"
            if _ROUNDS.get((self.chat_id, self.game)) is self:
                del _ROUNDS[(self.chat_id, self.game)]
                _LAST[(self.chat_id, self.game)] = (self.clock(), self)
                if len(_LAST) > 1000:
                    _LAST.clear()

    async def play(self) -> None:
        raise NotImplementedError

    def status(self) -> str:
        raise NotImplementedError


def _secs(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if isinstance(ra, timedelta) else float(ra)


def current(chat_id: int, game: str) -> Round | None:
    r = _ROUNDS.get((chat_id, game))
    return r if r and r.phase != "done" else None


def _start(r: Round) -> None:
    _ROUNDS[(r.chat_id, r.game)] = r
    r.task = asyncio.create_task(r.run(), name=f"casino-{r.game}-{r.chat_id}")
    _TASKS.add(r.task)
    r.task.add_done_callback(_TASKS.discard)


async def abandon_all() -> int:
    """종료 시: 진행 중인 판 전부 취소 → 걸린 돈 환불. 취소한 판 수."""
    rounds = [r for r in list(_ROUNDS.values()) if r.task and not r.task.done()]
    for r in rounds:
        r.task.cancel()
    await asyncio.gather(*(r.task for r in rounds), return_exceptions=True)
    for r in rounds:                    # 한 번도 못 돌고 취소된 task 는 run() 의 except 를 안 탄다
        r.phase = "done"
        await r.refund_all()
        if _ROUNDS.get((r.chat_id, r.game)) is r:
            del _ROUNDS[(r.chat_id, r.game)]
    return len(rounds)


async def recover_stale(db) -> int:
    """이 프로세스가 처음 쓰는 DB 면, 남아 있는 걸린 돈(이전 실행에서 끝나지 않은 판)을 환불."""
    if getattr(db, "_multi_recovered", False):
        return 0
    db._multi_recovered = True
    rows = await db._all("SELECT chat_id, user_id, game, bet FROM casino_open_stakes")
    n = 0
    for r in rows:
        if current(r["chat_id"], r["game"]):
            continue
        cur = await db.conn.execute("DELETE FROM casino_open_stakes WHERE chat_id=? AND user_id=? AND game=?",
                                    (r["chat_id"], r["user_id"], r["game"]))
        await db.conn.commit()
        if cur.rowcount == 1:
            await credit(db, r["chat_id"], r["user_id"], r["bet"], f"refund:{r['game']}")
            n += 1
    if n:
        log.info("casino multi: refunded %d stale stakes", n)
    return n


async def _join(ctx: Ctx, cls: type[Round], amount: int | None, pick: int, pick_txt: str) -> None:
    """참가 공통: 중복·마감 확인 → take_bet → (없으면 판 열기) → 등록."""
    await recover_stale(ctx.svc.db)
    cid, uid, game = ctx.chat_id, ctx.user.id, cls.game
    r = current(cid, game)
    if r and r.phase != "betting":
        await temp_reply(ctx, f"🚫 이번 판은 이미 출발했어요. 끝나면 {cls.next_hint}")
        return
    if (r and uid in r.players) or (cid, game, uid) in _PENDING:
        await temp_reply(ctx, "이번 판엔 이미 걸었어요. 한 판에 한 번만!")
        return
    if r and len(r.players) >= MAX_PLAYERS:
        await temp_reply(ctx, f"이번 판은 꽉 찼어요 ({MAX_PLAYERS}명). 다음 판에 와요!")
        return
    _PENDING.add((cid, game, uid))
    try:
        bet = await take_bet(ctx, amount, game)
        if bet is None:
            return
        await ctx.svc.db._write("INSERT OR REPLACE INTO casino_open_stakes(chat_id, user_id, game, bet, ts) "
                                "VALUES(?,?,?,?,?)", (cid, uid, game, bet, now()))
        p = Player(ctx, bet, pick)
        r = current(cid, game)
        if r is None:                   # 첫 베팅 → 새 판
            r = cls(ctx)
            r.style = (await ctx.svc.db.get_settings(cid))["style"]  # 딜러 소담 말투 (자유분방=반말)
            r.players[uid] = p
            _start(r)
            await ctx.reply(r.open_text(p, pick_txt))
            return
        if r.phase != "betting" or uid in r.players or len(r.players) >= MAX_PLAYERS:  # take_bet 하는 사이 마감
            await r.refund(p)
            await ctx.reply(f"⏱ 아슬아슬하게 마감됐어요. {fmt(bet)} 돌려드렸어요.")
            return
        r.players[uid] = p
        # 🎫 참가 확인은 베팅이 마감될 때쯤 지워짐 (사람마다 방에 영구로 쌓이지 않게) — 참가자 목록은 결과판에
        await temp_reply(ctx, f"🎫 {p.name} {fmt(bet)}{pick_txt} · 참가 {len(r.players)}명 · {r.left()}초 남음",
                         r.left() + 3)
    finally:
        _PENDING.discard((cid, game, uid))


# ── 📈 그래프 ─────────────────────────────────────────────
class CrashRound(Round):
    game = "crash"
    title = "📈 그래프"
    next_hint = "다음 판에 타요!"
    window = CRASH_WINDOW

    def __init__(self, ctx: Ctx):
        super().__init__(ctx)
        self.crash = crash_point()
        self.seal(fx(self.crash))
        self.start = 0.0
        self.hist: list[int] = [100]   # 화면 갱신 때마다의 배수 (차트용)
        self.rid = secrets.token_urlsafe(6)   # 🛑 스톱 버튼이 이 판을 가리키는 표 (지난 판 버튼은 안 먹힘)
        self.kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 스톱 (지금 배수로 내리기)", callback_data=f"cs:cr:{self.rid}")]])

    def open_text(self, p: Player, pick_txt: str) -> str:
        return (f"📈 <b>그래프 새 판!</b> {self.window}초 동안 탑승 받아요\n"
                f"첫 탑승: {p.name} {fmt(p.bet)}{pick_txt}\n{self.seal_line()}\n\n"
                "타기: <code>!그래프 금액</code> · 자동 내리기: <code>!그래프 금액 2.5</code>\n"
                "출발하면 배수가 올라가요. 터지기 전에 <code>!스톱</code> 치면 그 배수만큼 받아요!")

    def now_mult(self) -> int:
        return mult_at(self.clock() - self.start)

    def open_players(self) -> list[Player]:
        return [p for p in self.players.values() if not p.done]

    async def cash_out(self, p: Player, at: int) -> int | None:
        p.cash_at = min(at, p.pick) if p.pick else at
        return await self.pay(p, crash_payout(p.bet, p.cash_at))

    async def try_stop(self, uid: int) -> tuple[str, Player | None, int | None]:
        """!스톱·🛑 버튼 공통. (안내, 내린 사람, 새 잔액). 내리지 못했으면 사람·잔액 None."""
        p = self.players.get(uid)
        if p is None:
            return "이번 판엔 안 타셨어요. 끝나면 다음 판에 타요!", None, None
        if self.phase == "betting":
            return f"아직 출발 전이에요 ({self.left()}초 뒤 출발).", None, None
        if p.done:
            return (f"이미 {fx(p.cash_at)}에서 내렸어요." if p.cash_at else "💥 이미 터졌어요."), None, None
        m = self.now_mult()
        if m >= self.crash:
            return "💥 한 발 늦었어요! 이미 터졌어요.", None, None
        bal = await self.cash_out(p, m)       # done 은 await 전에 세워짐 → 동시에 두 번 눌러도 한 번만
        if bal is None:
            return f"이미 {fx(p.cash_at)}에서 내렸어요.", None, None
        return f"✅ {p.name} <b>{fx(p.cash_at)}</b>에서 내림!", p, bal

    async def autos(self, upto: int) -> None:
        for p in self.open_players():
            if p.pick and p.pick <= upto:
                await self.cash_out(p, p.pick)

    def live_text(self, m: int) -> str:
        riding = len(self.open_players())
        outs = sorted((p for p in self.players.values() if p.done and p.cash_at), key=lambda p: p.cash_at)
        lines = [f"{heat(m)} <b>{fx(m)}</b> 🚀 상승 중…", f"<pre>{chart(self.hist + [m])}</pre>",
                 f"🧑‍🚀 타는 중 {riding}명 · 아래 🛑 버튼이나 <code>!스톱</code>"]
        if outs:
            shown = " · ".join(f"{p.name} {fx(p.cash_at)}" for p in outs[-5:])
            more = f" 외 {len(outs) - 5}명" if len(outs) > 5 else ""
            lines.append(f"✅ 내림: {shown}{more}")
        return "\n".join(lines)

    async def play(self) -> None:
        self.start = self.clock()
        self.live = await self.send(self.live_text(100), self.kb)
        self.last_edit = self.clock()
        t_crash = time_to(self.crash)
        while True:
            el = self.clock() - self.start
            m = mult_at(el)
            if m >= self.crash:
                break
            await self.autos(m)
            if not self.open_players():
                break
            await self.edit(self.live_text(m), kb=self.kb)
            self.hist.append(m)
            await self.sleep(max(0.01, min(TICK, t_crash - el)))
        await self.autos(self.crash)          # 목표 ≤ 터진 지점이면 성공
        if self.crash >= CRASH_CAP:           # 최대 배수 완주: 끝까지 탄 사람은 그 배수로 내려줌
            for p in self.open_players():
                await self.cash_out(p, CRASH_CAP)
        busted = self.open_players()
        for p in busted:
            p.done = True                     # 꽝: 이미 차감됨, 지급 없음
            await self._unstake(p)
        self.phase = "done"
        await record(self.svc.db, self.chat_id, "crash", str(self.crash))   # 🖼 그림장
        head = f"🏆 <b>{fx(self.crash)}</b> 완주!" if self.crash >= CRASH_CAP else f"💥 <b>{fx(self.crash)}</b>에서 터졌어요!"
        await self.edit(f"{head}\n<pre>{chart(self.hist + [self.crash])}</pre>", force=True)
        await self.send(self.board())

    def board(self) -> str:
        won = sorted((p for p in self.players.values() if p.cash_at), key=lambda p: -p.payout)
        lost = [p for p in self.players.values() if not p.cash_at]
        lines = [f"🏆 <b>그래프 {fx(self.crash)} 완주!</b>" if self.crash >= CRASH_CAP else f"💥 <b>그래프 {fx(self.crash)}에서 펑!</b>"]
        if won:
            lines.append(f"\n✅ <b>탈출 성공 {len(won)}명</b>")
            lines += [f"· {p.name} {fx(p.cash_at)}{' (자동)' if p.pick and p.cash_at == p.pick else ''}"
                      f" → <b>+{fmt(p.payout - p.bet)}</b>" for p in won]
        if lost:
            lines.append(f"\n💸 <b>같이 터짐 {len(lost)}명</b>")
            lines += [f"· {p.name} -{fmt(p.bet)}" for p in lost]
        total_in = sum(p.bet for p in self.players.values())
        total_out = sum(p.payout for p in self.players.values())
        lines.append(f"\n판돈 {fmt(total_in)} · 지급 {fmt(total_out)}")
        lines.append(self.reveal_line())
        lines.append(dealer_line(getattr(self, "style", "polite"), total_in, total_out, 10**9))
        lines.append("다음 판: <code>!그래프 금액</code>")
        return "\n".join(lines)

    def status(self) -> str:
        if self.phase == "betting":
            pot = sum(p.bet for p in self.players.values())
            return f"📈 그래프: 탑승 받는 중 ({self.left()}초 남음) · {len(self.players)}명 · 판돈 {fmt(pot)}"
        return (f"📈 그래프: 비행 중 <b>{fx(min(self.now_mult(), self.crash))}</b> · "
                f"타는 중 {len(self.open_players())}명")


async def g_crash(ctx: Ctx) -> None:
    usage = ("📈 <b>그래프</b>: <code>!그래프 1000</code> 로 탑승 → 배수가 오르다 언젠가 터져요.\n"
             "터지기 전에 <code>!스톱</code> 치면 건 돈 × 그 배수!\n"
             "자동 내리기: <code>!그래프 1000 2.5</code> (2.50x 되면 알아서 내림)")
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    if not ctx.args:
        await ctx.reply(usage + f"\n내 잔액: <b>{fmt(bal)}</b>")
        return
    amount, rest = split_bet(ctx.args, bal)
    auto = 0
    if rest:
        auto = parse_auto(rest[0]) or -1
        if not AUTO_MIN <= auto <= AUTO_MAX:
            await ctx.reply(f"자동 내리기 배수는 1.01 ~ {AUTO_MAX // 100} 사이로 적어주세요. 예: <code>!그래프 1000 2.5</code>")
            return
    await _join(ctx, CrashRound, amount, auto, f" (자동 {fx(auto)})" if auto else "")


async def g_stop(ctx: Ctx) -> None:
    r = current(ctx.chat_id, "crash")
    if not isinstance(r, CrashRound):
        t, last = _LAST.get((ctx.chat_id, "crash"), (-1e9, None))
        if last and ctx.user.id in last.players and _clock() - t < 15 and not last.players[ctx.user.id].cash_at:
            await ctx.reply(f"💥 한 발 늦었어요! 이미 {fx(last.crash)}에서 터졌어요.")
            return
        await temp_reply(ctx, "지금 날고 있는 그래프가 없어요. <code>!그래프 금액</code> 으로 새 판!")
        return
    text, p, bal = await r.try_stop(ctx.user.id)
    if p is None:
        await temp_reply(ctx, text)              # '출발 전이에요'·'이미 내렸어요' 같은 안내는 잠깐만
        return
    await ctx.reply(text + "\n" + result_line(p.bet, p.payout, bal) + await dealer_tail(ctx, p.bet, p.payout, bal))


async def cb_crash(svc, bot, q, parts: list[str]) -> None:
    """🛑 스톱 버튼: 방에 새 메시지 없이 누른 사람에게만 결과 알림 (살아 있는 차트에 '내림' 으로 보임)."""
    r = next((r for r in _ROUNDS.values() if isinstance(r, CrashRound) and r.phase != "done"
              and parts and r.rid == parts[0]), None)
    if r is None:
        await q.answer("이미 끝난 판이에요.")
        return
    text, p, bal = await r.try_stop(q.from_user.id)
    if p is not None:
        text += f" +{fmt(p.payout - p.bet)} · 잔액 {fmt(bal)}"
    await q.answer(re.sub(r"<[^>]+>", "", text), show_alert=p is not None)


# ── 🏇 경마 ──────────────────────────────────────────────
class HorseRound(Round):
    game = "horse"
    title = "🏇 경마"
    next_hint = "다음 경주에 걸어요!"
    window = HORSE_WINDOW

    def __init__(self, ctx: Ctx):
        super().__init__(ctx)
        self.winner = _rand(HORSES)             # 0..4
        self.seal(f"{self.winner + 1}번")
        self.frames = race_frames(self.winner)

    def open_text(self, p: Player, pick_txt: str) -> str:
        return (f"🏇 <b>경마 출발 {self.window}초 전!</b> 1~5번 중 1등을 맞히면 <b>×4.7</b>\n"
                f"첫 베팅: {p.name} {fmt(p.bet)}{pick_txt}\n{self.seal_line()}\n\n"
                "베팅: <code>!경마 금액 번호</code> (예: <code>!경마 1000 3</code>)")

    def picks_line(self) -> str:
        counts = self.pick_counts()
        return " · ".join(f"{h + 1}번 {c}명" for h, c in enumerate(counts) if c)

    def pick_counts(self) -> list[int]:
        counts = [0] * HORSES
        for p in self.players.values():
            counts[p.pick - 1] += 1
        return counts

    async def play(self) -> None:
        picks = self.pick_counts()
        final = track_text(self.frames[-1], f"🏁 <b>{self.winner + 1}번 우승!</b>", picks, final=True)
        if await self.send_anim(lambda: anim.race(self.frames, TRACK, self.winner),
                                "🏇 <b>출발!</b> 누가 먼저 들어올까요…"):
            await self.sleep(anim.seconds(anim.race_frame_count(self.frames)))
            await self.edit(final, force=True, caption=True)
        else:
            await self.text_race(picks)
        win_no = self.winner + 1
        for p in self.players.values():
            await self.pay(p, p.bet * HORSE_PAY_X10 // 10 if p.pick == win_no else 0)
        self.phase = "done"
        await record(self.svc.db, self.chat_id, "horse", str(win_no))
        await self.send(self.board())

    async def text_race(self, picks: list[int]) -> None:
        """애니메이션이 안 될 때: 메시지 하나를 고쳐 가며 달림."""
        self.live = await self.send(track_text([0] * HORSES, "🏇 <b>출발!</b>", picks))
        self.last_edit = self.clock()
        for f, pos in enumerate(self.frames):
            await self.sleep(TICK)
            last = f == len(self.frames) - 1
            title = (f"🏁 <b>{self.winner + 1}번 우승!</b>" if last else
                     "🏇 <b>막판 스퍼트!</b>" if f == len(self.frames) - 2 else f"🏇 <b>달리는 중…</b> ({f + 1}/{len(self.frames)})")
            await self.edit(track_text(pos, title, picks, final=last), force=last)

    def board(self) -> str:
        win_no = self.winner + 1
        won = sorted((p for p in self.players.values() if p.payout), key=lambda p: -p.payout)
        lost = [p for p in self.players.values() if not p.payout]
        lines = [f"🏁 <b>경마 결과: {win_no}번 우승!</b>"]
        if won:
            lines.append(f"\n🎉 <b>적중 {len(won)}명</b>")
            lines += [f"· {p.name} {fmt(p.bet)} → <b>+{fmt(p.payout - p.bet)}</b>" for p in won]
        else:
            lines.append("\n적중한 사람이 없어요 😭")
        if lost:
            lines.append(f"\n💸 <b>꽝 {len(lost)}명</b>")
            lines += [f"· {p.name} {p.pick}번 -{fmt(p.bet)}" for p in lost]
        total_in = sum(p.bet for p in self.players.values())
        total_out = sum(p.payout for p in self.players.values())
        lines.append("\n" + self.reveal_line())
        lines.append(dealer_line(getattr(self, "style", "polite"), total_in, total_out, 10**9))
        lines.append("다음 경주: <code>!경마 금액 번호</code>")
        return "\n".join(lines)

    def status(self) -> str:
        if self.phase == "betting":
            pot = sum(p.bet for p in self.players.values())
            return (f"🏇 경마: 베팅 받는 중 ({self.left()}초 남음) · {len(self.players)}명 · 판돈 {fmt(pot)}\n"
                    f"   {self.picks_line()}")
        return f"🏇 경마: 달리는 중 · {len(self.players)}명 참가"


async def g_horse(ctx: Ctx) -> None:
    usage = ("🏇 <b>경마</b>: <code>!경마 1000 3</code> — 1~5번 중 1등 맞히면 ×4.7\n"
             "첫 베팅 후 20초 동안 다 같이 걸고, 다 같이 달려요!")
    bal = await balance(ctx.svc.db, ctx.chat_id, ctx.user.id)
    if not ctx.args:
        await ctx.reply(usage + f"\n내 잔액: <b>{fmt(bal)}</b>")
        return
    # '!경마 3 1000' 도 되게: 1~5 한 글자는 번호로 먼저 뺀다
    horse = next((a.removesuffix("번") for a in ctx.args if a.removesuffix("번") in ("1", "2", "3", "4", "5")), None)
    if horse is None:
        await ctx.reply(usage)
        return
    rest = list(ctx.args)
    rest.remove(next(a for a in ctx.args if a.removesuffix("번") == horse))
    amount, _ = split_bet(rest, bal)
    await _join(ctx, HorseRound, amount, int(horse), f" → {horse}번")


# ── 📋 라운드 현황 ────────────────────────────────────────
async def g_rounds(ctx: Ctx) -> None:
    rs = [r for g in ("crash", "horse") if (r := current(ctx.chat_id, g))]
    if not rs:
        await ctx.reply("지금 진행 중인 판이 없어요.\n<code>!그래프 금액</code> · <code>!경마 금액 번호</code> 로 새 판을 열어보세요!")
        return
    await ctx.reply("\n".join(r.status() for r in rs))


register(("그래프", "crash", "크래시"), g_crash, usage="금액 [자동배수]",
         help="📈 다 같이 타고 터지기 전에 !스톱", group="같이 하는 게임")
register(("스톱", "멈춰", "stop"), g_stop, help="그래프에서 내리기", group="같이 하는 게임")
register_callback("cr", cb_crash)
register(("경마", "horse"), g_horse, usage="금액 번호", help="🏇 1~5번 1등 맞히면 ×4.7", group="같이 하는 게임")
register(("라운드", "round"), g_rounds, help="지금 진행 중인 판", group="같이 하는 게임", needs_account=False)


async def _shutdown(svc) -> int:
    return await abandon_all()


SHUTDOWN_HOOKS.append(_shutdown)
