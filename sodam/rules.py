"""알림 규칙: '누가 입금 얘기하면 나한테 알려줘' 같은 관리자 규칙 (방마다, 🔔 메뉴·AI 도구 alert_rule).

규칙은 코드가 아니라 데이터 — 정해진 부품만 조립한다 (AI 는 말 → 부품 번역만, 저장은 만든 관리자의 확인 버튼):
  언제(trig): keyword 메시지에 낱말 · user 특정 사람이 말함 · join 누가 들어옴 · quiet 방이 N시간 조용함
  누구(who):  all / newbie (들어온 지 24시간 안 된 사람, keyword 만)
  하면(action): dm 만든 관리자 1:1 · call 방에서 만든 관리자 호출 · post 방에 정해진 글
실행은 코드만 (AI 없음). 쿨다운·하루 상한은 한 문장 UPDATE 로 '먼저 차지한 알림만' 나가게, 만든 사람이 더는
관리자가 아니면 규칙을 끈다. 뮤트·밴 같은 무거운 동작은 부품에 없다 (제재는 기존 확인 버튼으로만).
폭주 방지: 규칙이 10분에 max(10, 7일 시간당 평균×5)번 넘게 걸리면(쿨다운에 막힌 것 포함) 1시간 멈추고 만든 사람에게
1:1 한 번 [▶️ 계속 실행][⏸ 오늘 중지][✏️ 규칙 보기] (panels/rules.py m:rlp, 한 번만 처리).
미리 보기 replay(): 지난 7일 기록(messages·입장)에 규칙을 코드로 대 보고 쿨다운·하루 상한까지 적용한 횟수 (AI 없음).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import TelegramError

from . import hooks
from .db import register_schema
from .util import esc, mention, user_name

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

TRIGGERS = {"keyword": "🔑 낱말이 나오면", "user": "👤 이 사람이 말하면", "join": "🚪 누가 들어오면", "quiet": "💤 방이 조용하면"}
ACTIONS = {"dm": "나한테 1:1 알림", "call": "방에서 나를 호출", "post": "방에 정해진 글"}
MAX_RULES = 20
DAILY_CAP = 30            # 규칙 하나가 하루에 울리는 최대 횟수
NEWBIE_SECONDS = 86400
KEYWORD_MIN, KEYWORD_MAX = 2, 30
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
RUNAWAY_WINDOW = 600      # 폭주 판단 창 (10분 = 분 단위 칸 10개)
RUNAWAY_MIN = 10          # 이 횟수 이하는 폭주 아님
RUNAWAY_FACTOR = 5        # 7일 시간당 평균의 이 배수를 넘으면 폭주
PAUSE_SECONDS = 3600      # 폭주 → 멈추는 시간
RESUME_GRACE = 3600       # [▶️ 계속 실행] 뒤 이 시간은 다시 멈추지 않음 (관리자가 괜찮다고 함)
REPLAY_DAYS = 7
BUSY_PER_DAY = 10         # 미리 보기에서 하루 평균이 이보다 많으면 경고

register_schema("""
CREATE TABLE IF NOT EXISTS alert_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    trig       TEXT NOT NULL,
    arg        TEXT NOT NULL DEFAULT '',      -- keyword: 낱말 · user: 사람 ID · quiet: 시간
    who        TEXT NOT NULL DEFAULT 'all',
    action     TEXT NOT NULL DEFAULT 'dm',
    text       TEXT NOT NULL DEFAULT '',      -- post 로 올릴 글
    cooldown   INTEGER NOT NULL DEFAULT 10,   -- 분
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_fired INTEGER NOT NULL DEFAULT 0,
    day        TEXT NOT NULL DEFAULT '',
    fired      INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_rules_chat ON alert_rules(chat_id);
CREATE TABLE IF NOT EXISTS alert_rule_hits (      -- 규칙이 걸린 수 (분 단위 칸, 쿨다운에 막힌 것 포함, 8일 보관)
    rule_id INTEGER NOT NULL,
    minute  INTEGER NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rule_id, minute)
);
CREATE TABLE IF NOT EXISTS alert_rule_pauses (    -- 폭주로 멈춤 (멈출 때마다 한 줄)
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    until   INTEGER NOT NULL,                     -- 이 시각까지 안 울림 (0 = 풀림)
    hits    INTEGER NOT NULL,
    status  TEXT,                                 -- NULL 안 누름 / resume 계속 실행 / today 오늘 중지
    by_id   INTEGER,
    done_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alert_rule_pauses ON alert_rule_pauses(rule_id, until);
""", migrate={"alert_rules": "plain", "alert_rule_pauses": "plain"})


def norm(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def validate(trig: str, arg: str, action: str, text: str) -> str | None:
    """저장 전 검사. 문제면 이유."""
    if trig not in TRIGGERS or action not in ACTIONS:
        return "종류가 잘못됐어요."
    if trig == "keyword" and not KEYWORD_MIN <= len(norm(arg)) <= KEYWORD_MAX:
        return f"낱말은 {KEYWORD_MIN}~{KEYWORD_MAX}자로 해주세요."
    if trig == "user" and not arg.isdecimal():
        return "사람을 찾지 못했어요."
    if trig == "quiet" and not (arg.isdecimal() and 1 <= int(arg) <= 72):
        return "조용한 시간은 1~72시간으로 해주세요."
    if action == "post" and not text.strip():
        return "방에 올릴 글을 적어주세요."
    return None


async def describe(svc: Services, r) -> str:
    """사람 말로 한 줄 (카드·목록용)."""
    if r["trig"] == "keyword":
        when = f"'{r['arg']}' 이(가) 나오면" + (" (신규 입장자만)" if r["who"] == "newbie" else "")
    elif r["trig"] == "user":
        when = f"{await svc.db.first_name(int(r['arg'])) or r['arg']}님이 말하면"
    elif r["trig"] == "join":
        when = "누가 들어오면"
    else:
        when = f"방이 {r['arg']}시간 조용하면"
    what = ACTIONS[r["action"]] + (f" ('{r['text'][:30]}')" if r["action"] == "post" else "")
    return f"{when} → {what}"


async def add(svc: Services, chat_id: int, uid: int, spec: dict) -> int:
    rid = await svc.db._write(
        "INSERT INTO alert_rules(chat_id, created_by, trig, arg, who, action, text, cooldown, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (chat_id, uid, spec["trig"], str(spec.get("arg", ""))[:KEYWORD_MAX * 2], spec.get("who", "all"),
         spec["action"], str(spec.get("text", ""))[:300], max(1, min(int(spec.get("cooldown") or 10), 1440)),
         int(time.time())))
    forget(svc.db, chat_id)
    await svc.db.log_mod(chat_id, uid, None, "setting", f"알림 규칙 #{rid} {spec['trig']}:{spec.get('arg', '')}")
    return rid


async def room_rules(db, chat_id: int, enabled_only: bool = False) -> list:
    async def load():
        return await db._all("SELECT * FROM alert_rules WHERE chat_id=?" + (" AND enabled=1" if enabled_only else "")
                             + " ORDER BY id", (chat_id,))
    # 켜진 규칙은 메시지·입장마다 읽음 → 캐시 (규칙을 바꾸는 곳은 forget). 울린 시각·횟수는 _claim 이 DB 에서 봄
    return await db.cached(("alert_rules", chat_id), load) if enabled_only else await load()


def forget(db, chat_id: int) -> None:
    db.uncache(("alert_rules", chat_id))


async def _claim(db, rid: int, now: int, day: str) -> bool:
    """쿨다운·하루 상한 안이고 멈춤이 아니면 이번 알림을 차지 (한 문장이라 동시에 와도 한 번만)."""
    def run(c):
        return c.execute(
            "UPDATE alert_rules SET last_fired=?, fired=CASE WHEN day=? THEN fired+1 ELSE 1 END, day=? "
            "WHERE id=? AND enabled=1 AND ?-last_fired >= cooldown*60 AND NOT (day=? AND fired>=?) "
            "AND NOT EXISTS (SELECT 1 FROM alert_rule_pauses p WHERE p.rule_id=alert_rules.id AND p.until>?)",
            (now, day, day, rid, now, day, DAILY_CAP, now)).rowcount
    return bool(await db.atomic(run))


# ── 폭주 방지 ─────────────────────────────────────────────
async def _hit(db, r, now: int) -> tuple[int, int] | None:
    """걸린 수를 세고, 폭주면 멈춤 한 줄을 넣어 (멈춤 ID, 10분 수). 한 번의 atomic → 동시에 와도 멈춤·알림 한 번."""
    rid, minute, span = r["id"], now // 60, RUNAWAY_WINDOW // 60

    def run(c):
        c.execute("INSERT INTO alert_rule_hits(rule_id, minute, n) VALUES(?, ?, 1) "
                  "ON CONFLICT(rule_id, minute) DO UPDATE SET n=n+1", (rid, minute))
        if minute % 60 == 0:
            c.execute("DELETE FROM alert_rule_hits WHERE rule_id=? AND minute<?", (rid, minute - 8 * 1440))
        recent = c.execute("SELECT COALESCE(SUM(n), 0) FROM alert_rule_hits WHERE rule_id=? AND minute>?",
                           (rid, minute - span)).fetchone()[0]
        if recent <= RUNAWAY_MIN:
            return None
        if c.execute("SELECT 1 FROM alert_rule_pauses WHERE rule_id=? AND (until>? OR (status='resume' AND done_ts>?))",
                     (rid, now, now - RESUME_GRACE)).fetchone():
            return None   # 이미 멈춤 · 방금 관리자가 [▶️ 계속 실행]
        base = c.execute("SELECT COALESCE(SUM(n), 0) FROM alert_rule_hits WHERE rule_id=? AND minute>? AND minute<=?",
                         (rid, minute - 7 * 1440, minute - span)).fetchone()[0]   # 지금 10분은 빼고 7일
        if recent <= max(RUNAWAY_MIN, RUNAWAY_FACTOR * base / (7 * 24)):
            return None
        pid = c.execute("INSERT INTO alert_rule_pauses(rule_id, chat_id, ts, until, hits) VALUES(?, ?, ?, ?, ?)",
                        (rid, r["chat_id"], now, now + PAUSE_SECONDS, recent)).lastrowid
        return pid, recent
    return await db.atomic(run)


async def active_pause(db, rid: int, now: int | None = None):
    """지금 걸려 있는 멈춤 (없으면 None)."""
    return await db._one("SELECT * FROM alert_rule_pauses WHERE rule_id=? AND until>? ORDER BY id DESC LIMIT 1",
                         (rid, now or int(time.time())))


def pause_buttons(chat_id: int, pid: int) -> list[InlineKeyboardButton]:
    cb = f"m:rlp:{chat_id}:{pid}:"
    return [InlineKeyboardButton("▶️ 계속 실행", callback_data=cb + "r"),
            InlineKeyboardButton("⏸ 오늘 중지", callback_data=cb + "d")]


def midnight(svc: Services, now: int) -> int:
    """내일 0시 (봇 시간대)."""
    d = datetime.fromtimestamp(now, svc.cfg.tz).date() + timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, tzinfo=svc.cfg.tz).timestamp())


async def resolve_pause(svc: Services, pid: int, act: str, by_id: int) -> bool:
    """[▶️ 계속 실행](resume) · [⏸ 오늘 중지](today) — 멈춤 하나에 한 번만 (한 문장 UPDATE)."""
    now = int(time.time())
    until = 0 if act == "resume" else midnight(svc, now)

    def run(c):
        return c.execute("UPDATE alert_rule_pauses SET status=?, by_id=?, done_ts=?, until=? WHERE id=? AND status IS NULL",
                         (act, by_id, now, until, pid)).rowcount
    return bool(await svc.db.atomic(run))


async def drop_stats(db, rid: int) -> None:
    """규칙을 지울 때 걸린 수·멈춤 기록도."""
    def run(c):
        c.execute("DELETE FROM alert_rule_hits WHERE rule_id=?", (rid,))
        c.execute("DELETE FROM alert_rule_pauses WHERE rule_id=?", (rid,))
    await db.atomic(run)


async def _notify_pause(svc: Services, bot: Bot, r, pid: int, hits: int) -> None:
    """멈춤 한 번에 만든 사람 1:1 한 번 (멈춤 줄은 _hit 이 atomic 으로 한 번만 넣음)."""
    from .subscription import chat_title  # 늦게 import (순환 방지)
    cid, rid = r["chat_id"], r["id"]
    await svc.db.log_mod(cid, None, None, "setting", f"알림 규칙 #{rid} 10분에 {hits}번 → 1시간 멈춤")
    text = (f"⚠️ 규칙 '{esc((await describe(svc, r))[:40])}' 10분에 {hits}번 → 1시간 멈춤\n"
            f"방: <b>{esc(await chat_title(svc, cid))}</b> · 규칙 #{rid}\n"
            "평소보다 너무 자주 걸려서 잠깐 멈췄어요. 그냥 두면 1시간 뒤 다시 울려요.")
    kb = InlineKeyboardMarkup([pause_buttons(cid, pid),
                               [InlineKeyboardButton("✏️ 규칙 보기", callback_data=f"m:rli:{cid}:{rid}")]])
    try:
        await bot.send_message(r["created_by"], text, parse_mode="HTML", reply_markup=kb, link_preview_options=NO_PREVIEW)
    except TelegramError as e:
        log.info("alert rule #%s pause notice failed: %s", rid, e)   # 1:1 을 시작 안 한 관리자


def _link(chat_id: int, msg_id: int | None) -> str:
    s = str(chat_id)
    return f"\n🔗 https://t.me/c/{s[4:]}/{msg_id}" if msg_id and s.startswith("-100") else ""


async def fire(svc: Services, bot: Bot, r, what: str, msg_id: int | None = None) -> bool:
    cid, creator = r["chat_id"], r["created_by"]
    if not await svc.paid_features(cid):
        return False
    if not await svc.perms.is_admin(bot, cid, creator):          # 권한은 울릴 때 다시 확인
        await svc.db._write("UPDATE alert_rules SET enabled=0 WHERE id=?", (r["id"],))
        forget(svc.db, cid)
        return False
    now = int(time.time())
    trip = await _hit(svc.db, r, now)       # 쿨다운에 막히는 것까지 셈 → 폭주면 멈추고 만든 사람에게 한 번
    if trip:
        await _notify_pause(svc, bot, r, *trip)
        return False
    if not await _claim(svc.db, r["id"], now, datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")):
        return False
    from .subscription import chat_title  # 늦게 import (순환 방지)
    name = await svc.db.first_name(creator) or "관리자"
    try:
        if r["action"] == "dm":
            await bot.send_message(creator, f"🔔 <b>{esc(await chat_title(svc, cid))}</b> · 규칙 #{r['id']}\n{what}"
                                   + _link(cid, msg_id), parse_mode="HTML", link_preview_options=NO_PREVIEW)
        elif r["action"] == "call":
            await bot.send_message(cid, f"🔔 {mention(creator, name)}님, {what}", parse_mode="HTML",
                                   link_preview_options=NO_PREVIEW)
        else:
            await bot.send_message(cid, esc(r["text"]), parse_mode="HTML", link_preview_options=NO_PREVIEW)
        return True
    except TelegramError as e:
        log.info("alert rule #%s send failed: %s", r["id"], e)   # 1:1 을 시작 안 한 관리자 등
        return False


async def on_message(svc: Services, bot: Bot, msg, role) -> None:
    rules = await room_rules(svc.db, msg.chat_id, enabled_only=True)
    if not rules:
        return
    user, text = msg.from_user, msg.text or msg.caption or ""
    body, who = norm(text), esc(user_name(user))
    preview = esc(" ".join(text.split())[:100])
    newbie = None
    for r in rules:
        if user.id == r["created_by"] or user.is_bot:
            continue
        if r["trig"] == "keyword" and norm(r["arg"]) in body:
            if r["who"] == "newbie":
                if newbie is None:
                    m = await svc.db.get_member(msg.chat_id, user.id)
                    newbie = bool(m and m["joined_at"] and time.time() - m["joined_at"] < NEWBIE_SECONDS)
                if not newbie:
                    continue
            await fire(svc, bot, r, f"'{esc(r['arg'])}' 나옴 — {who}: {preview}", msg.message_id)
        elif r["trig"] == "user" and r["arg"] == str(user.id):
            await fire(svc, bot, r, f"{who}님이 말했어요: {preview}", msg.message_id)


async def on_join(svc: Services, bot: Bot, chat_id: int, user) -> bool:
    for r in await room_rules(svc.db, chat_id, enabled_only=True):
        if r["trig"] == "join":
            await fire(svc, bot, r, f"{esc(user_name(user))}님이 들어왔어요 (ID <code>{user.id}</code>)")
    return False   # 입장은 막지 않음


async def check_quiet(svc: Services, bot: Bot, now: int | None = None) -> int:
    """10분마다: 조용함 규칙. 마지막 대화 뒤 N시간이 지났고 그 조용함에 아직 안 울렸으면 한 번."""
    now = now or int(time.time())
    n = 0
    for r in await svc.db._all("SELECT * FROM alert_rules WHERE trig='quiet' AND enabled=1"):
        row = await svc.db._one("SELECT MAX(ts) AS t FROM messages WHERE chat_id=? AND is_bot=0", (r["chat_id"],))
        last = (row and row["t"]) or 0
        if last and now - last >= int(r["arg"]) * 3600 and r["last_fired"] < last:
            n += await fire(svc, bot, r, f"방이 {r['arg']}시간째 조용해요")
    return n


# ── 미리 보기 (지난 7일 기록에 대 보기) ──────────────────
@dataclass
class Replay:
    total: int = 0
    max_day: int = 0
    days: int = REPLAY_DAYS
    per_day: dict = field(default_factory=dict)

    @property
    def avg(self) -> float:
        return self.total / self.days if self.days else 0.0


async def _join_times(db, chat_id: int, since: int) -> dict[int, list[int]]:
    """사람별 입장 시각: members.joined_at(마지막 입장) + 입장 기록(log_join, 캡차 통과 뒤). 15분 안의 둘은 한 번."""
    got: dict[int, list[int]] = {}
    for row in await db._all("SELECT user_id, joined_at AS ts FROM members WHERE chat_id=? AND joined_at>=?",
                             (chat_id, since)):
        got.setdefault(row["user_id"], []).append(row["ts"])
    for row in await db._all("SELECT user_id, ts FROM messages WHERE chat_id=? AND is_bot=1 AND ts>=? "
                             "AND text LIKE '%님이 방에 들어옴'", (chat_id, since)):
        got.setdefault(row["user_id"], []).append(row["ts"])
    out = {}
    for uid, ts in got.items():
        merged: list[int] = []
        for t in sorted(ts):
            if not merged or t - merged[-1] > 900:
                merged.append(t)
        out[uid] = merged
    return out


async def _events(svc: Services, chat_id: int, spec: dict, since: int, now: int) -> list[int]:
    """규칙이 걸렸을 시각들 (쿨다운·상한 적용 전). on_message·on_join·check_quiet 와 같은 조건."""
    db, trig, arg = svc.db, spec["trig"], str(spec.get("arg", ""))
    creator = spec.get("created_by")
    if trig in ("keyword", "user"):
        rows = await db._all("SELECT user_id, text, ts FROM messages WHERE chat_id=? AND is_bot=0 AND ts>=? AND ts<=? "
                             "ORDER BY ts LIMIT 200000", (chat_id, since, now))
        rows = [m for m in rows if m["user_id"] != creator]      # 만든 사람 말은 안 울림
        if trig == "user":
            return [m["ts"] for m in rows if str(m["user_id"]) == arg]
        word, newbie = norm(arg), spec.get("who") == "newbie"
        joins = await _join_times(db, chat_id, since - NEWBIE_SECONDS) if newbie else {}
        return [m["ts"] for m in rows if word in norm(m["text"]) and
                (not newbie or any(j <= m["ts"] < j + NEWBIE_SECONDS for j in joins.get(m["user_id"], ())))]
    if trig == "join":
        return [t for ts in (await _join_times(db, chat_id, since)).values() for t in ts if t <= now]
    if trig == "quiet" and arg.isdecimal():
        gap = int(arg) * 3600
        prev = await db._one("SELECT MAX(ts) AS t FROM messages WHERE chat_id=? AND is_bot=0 AND ts<?", (chat_id, since))
        ts = [prev["t"]] if prev and prev["t"] else []
        ts += [m["ts"] for m in await db._all("SELECT ts FROM messages WHERE chat_id=? AND is_bot=0 AND ts>=? AND ts<=? "
                                              "ORDER BY ts", (chat_id, since, now))]
        # 대화 사이 gap 이상 빈 곳마다 한 번 (마지막 대화 + gap 시각) — check_quiet 처럼 조용함 하나에 한 번
        return [a + gap for a, b in zip(ts, ts[1:] + [now + 1]) if b - a >= gap and since <= a + gap <= now]
    return []


async def replay(svc: Services, chat_id: int, spec: dict, days: int = REPLAY_DAYS, now: int | None = None) -> Replay:
    """이 규칙이 지난 days 일 동안 있었다면 몇 번 울렸을지 — 기록된 대화·입장에 코드로 대 보고 쿨다운·하루 상한 적용
    (AI 없음. 봇이 기록하기 전·지운 기록은 모름)."""
    now = now or int(time.time())
    cooldown = max(1, min(int(spec.get("cooldown") or 10), 1440)) * 60
    out, last = Replay(days=days), None
    for t in sorted(await _events(svc, chat_id, spec, now - days * 86400, now)):
        day = datetime.fromtimestamp(t, svc.cfg.tz).strftime("%Y-%m-%d")
        if (last is not None and t - last < cooldown) or out.per_day.get(day, 0) >= DAILY_CAP:
            continue
        last = t
        out.per_day[day] = out.per_day.get(day, 0) + 1
    out.total = sum(out.per_day.values())
    out.max_day = max(out.per_day.values(), default=0)
    return out


async def preview_text(svc: Services, chat_id: int, spec: dict) -> str:
    """확인 카드·🔎 미리 보기용 한두 줄."""
    rp = await replay(svc, chat_id, spec)
    text = f"🔎 지난 {rp.days}일이면 {rp.total}번 · 하루 최대 {rp.max_day}번"
    if rp.avg > BUSY_PER_DAY:
        text += f"\n⚠️ 하루 평균 {rp.avg:.0f}번 — 너무 자주 울릴 수 있어요. 낱말을 좁히거나 쿨다운을 늘려 보세요."
    return text


hooks.add_group_message_hook(on_message)
hooks.add_member_join_hook(on_join)
