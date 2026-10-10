"""⚡ 라이브 카드 · 자동 라이브 (설계 docs/SPORTS_ENGAGE_DESIGN.md §3 A, 2026-10-11 오너·고객 '말 안 해도 자동으로 오는 알림 + 버튼').

- 방 설정 sports_auto: off(기본) / big(주요 리그 자동) / follow(구독한 리그·팀만 카드). 콕 집은 경기 알림(sports_watch game)은 설정과 무관하게 카드.
- 경기가 시작되면 방에 카드 1장 → 몇 초마다 그 카드를 **고쳐 씀**(점수·진행·득점자). 골·종료는 alerts 가 새 글로(폰 알림).
  공개 봇 조사(Fotmob-Updates-Bot 한 경기 = 한 메시지 수정, ucl-live-bot 버튼) 반영.
- 도배 방지: 방마다 진행 중 카드 MAX_CARDS, 점수·상태가 바뀌면 바로, 시간만 바뀐 건 MINUTE_EDIT 초에 한 번, 조용한 시간엔 새 카드 X.
- 버튼 sgc:<op>:<경기 key> — dm(누른 사람 1:1 로도) · an(🧠 분석을 방에 한 번) · off(관리자: 이 경기 카드·알림 끔, sports_mute).
"""
from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton as IB
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from ..db import register_schema
from ..settings import register_setting
from ..util import esc
from . import fmt
from .leagues import LEAGUES, ko_name

log = logging.getLogger(__name__)

BIG = ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "uel", "mlb", "nba", "nhl", "kbo", "kleague")
MAX_CARDS = 4
MINUTE_EDIT = 30
AUTO = {"off": "끔", "big": "⚡ 주요 리그 자동", "follow": "🔔 구독한 것만"}

register_setting("sports_auto", "off", "스포츠 자동 라이브 카드",
                 choices={"off": "off", "끔": "off", "big": "big", "주요": "big", "자동": "big", "켜기": "big",
                          "follow": "follow", "구독": "follow"}, choice_labels=AUTO)

register_schema("""
CREATE TABLE IF NOT EXISTS sports_cards (
    chat_id  INTEGER NOT NULL,
    game     TEXT NOT NULL,
    league   TEXT NOT NULL,
    msg_id   INTEGER NOT NULL,
    body     TEXT NOT NULL,
    score    TEXT NOT NULL DEFAULT '',
    state    TEXT NOT NULL,
    updated  INTEGER NOT NULL,
    analyzed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, game)
);
CREATE TABLE IF NOT EXISTS sports_mute (
    chat_id INTEGER NOT NULL,
    game    TEXT NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (chat_id, game)
);
""", migrate={"sports_cards": "composite", "sports_mute": "composite"})


def auto_entries(mode: str) -> list[dict]:
    """자동 모드 → 구독 줄처럼 쓰는 항목 (alerts._wants 가 같이 봄)."""
    if mode == "big":
        return [{"league": c, "team": "", "auto": True} for c in BIG if c in LEAGUES]
    return []


def body(g) -> str:
    """카드 본문 (마지막 갱신 시각 빼고 — 바뀌었는지 비교용)."""
    head = {"in": "🔴 LIVE", "post": "🏁 종료"}.get(g.state, fmt.STATE_KO.get(g.state, "⏳"))
    lines = [f"{head} {fmt.tag(g)}", f"<b>{fmt.matchup(g)}</b>"]
    if g.state == "in" and g.detail:
        lines.append(f"⏱ {esc(g.detail)}")
    for clock, side, who, tag in g.goals[-6:]:
        team = ko_name(g.home if side == "home" else g.away, g.league) if side else ""
        lines.append(f"⚽ {esc(clock)} {esc(who)}{f'({esc(tag)})' if tag else ''} {esc(team)}".rstrip())
    return "\n".join(lines)


def score_of(g) -> str:
    return f"{g.home_score}-{g.away_score}" if g.scored else ""


def keyboard(g, ended: bool) -> InlineKeyboardMarkup | None:
    base = f"sgc:{{}}:{g.key}"
    if len(base.format("an").encode()) > 64:
        return None
    row = [IB("🧠 분석", base.format("an"))]
    if not ended:
        row = [IB("📩 1:1로도", base.format("dm"))] + row + [IB("🔕 끄기", base.format("off"))]
    return InlineKeyboardMarkup([row])


def stamp(now: float, ended: bool) -> str:
    from datetime import datetime

    from .providers import KST
    t = datetime.fromtimestamp(now, KST).strftime("%H:%M:%S")
    return f"\n<i>{'최종' if ended else '자동 갱신'} {t}</i>"


async def muted(db, chat_id: int) -> set[str]:
    return {r["game"] for r in await db._all("SELECT game FROM sports_mute WHERE chat_id=?", (chat_id,))}


async def update(db, bot, chat_id: int, games: list, now: float, quiet: bool, limiter=None) -> int:
    """이 방에 보여 줄 경기들(진행 중·막 끝난 것) → 카드 새로 띄우기/고치기. 고친·띄운 수."""
    rows = {r["game"]: r for r in await db._all("SELECT * FROM sports_cards WHERE chat_id=?", (chat_id,))}
    live_cards = sum(1 for r in rows.values() if r["state"] == "in")
    done = 0
    for g in sorted(games, key=lambda g: (BIG.index(g.league) if g.league in BIG else 99, g.start)):
        row = rows.get(g.key)
        ended = g.state not in ("in", "pre", "suspended")
        text = body(g)
        if row is None:
            if g.state != "in" or quiet or live_cards >= MAX_CARDS:
                continue
            try:
                m = await bot.send_message(chat_id, text + stamp(now, False), parse_mode="HTML",
                                           reply_markup=keyboard(g, False), disable_notification=True)
            except TelegramError as e:
                log.warning("라이브 카드 보내기 실패 %s: %s", chat_id, e)
                continue
            await db._write("INSERT OR REPLACE INTO sports_cards(chat_id, game, league, msg_id, body, score, state, updated) "
                            "VALUES(?,?,?,?,?,?,?,?)", (chat_id, g.key, g.league, m.message_id, text, score_of(g), g.state, int(now)))
            live_cards += 1
            done += 1
            continue
        if text == row["body"] and g.state == row["state"]:
            continue
        important = score_of(g) != row["score"] or g.state != row["state"]
        if not important and now - row["updated"] < MINUTE_EDIT:
            continue
        try:
            await bot.edit_message_text(text + stamp(now, ended), chat_id=chat_id, message_id=row["msg_id"], parse_mode="HTML",
                                        reply_markup=keyboard(g, ended))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.info("라이브 카드 고치기 실패 %s %s: %s — 카드 버림", chat_id, g.key, e)
                await db._write("DELETE FROM sports_cards WHERE chat_id=? AND game=?", (chat_id, g.key))
                continue
        except TelegramError as e:
            log.warning("라이브 카드 고치기 실패 %s: %s", chat_id, e)
            continue
        await db._write("UPDATE sports_cards SET body=?, score=?, state=?, updated=? WHERE chat_id=? AND game=?",
                        (text, score_of(g), g.state, int(now), chat_id, g.key))
        done += 1
    return done


async def prune(db, now: float) -> None:
    await db._write("DELETE FROM sports_cards WHERE state != 'in' AND updated < ?", (int(now) - 86400,))
    await db._write("DELETE FROM sports_cards WHERE updated < ?", (int(now) - 2 * 86400,))
    await db._write("DELETE FROM sports_mute WHERE created < ?", (int(now) - 2 * 86400,))


# ── 버튼 ──────────────────────────────────────────────
async def on_button(svc, bot, q, parts) -> None:
    """sgc:<op>:<경기 key>"""
    from ..permissions import Role
    from . import analysis
    if svc.sports is None or len(parts) < 2 or not q.message:
        await q.answer()
        return
    op, key = parts[0], ":".join(parts[1:])
    chat_id = q.message.chat.id
    row = await svc.db._one("SELECT * FROM sports_cards WHERE chat_id=? AND game=?", (chat_id, key))
    if row is None:
        await q.answer("끝났거나 정리된 경기예요.")
        return
    g = _find(svc, row["league"], key)
    if op == "off":
        if await svc.perms.role(bot, chat_id, q.from_user.id) < Role.ADMIN:
            await q.answer("이 경기 끄기는 관리자만 할 수 있어요. 본인만 안 받으려면 📩 대신 그냥 두면 돼요.", show_alert=True)
            return
        await svc.db._write("INSERT OR IGNORE INTO sports_mute VALUES(?,?,?)", (chat_id, key, int(time.time())))
        await svc.db._write("DELETE FROM sports_cards WHERE chat_id=? AND game=?", (chat_id, key))
        await svc.db._write("DELETE FROM sports_watch WHERE chat_id=? AND game=?", (chat_id, key))
        try:
            await q.edit_message_text(row["body"] + "\n<i>🔕 이 경기 알림을 껐어요</i>", parse_mode="HTML")
        except TelegramError:
            pass
        await q.answer("이 경기 알림을 껐어요.")
        return
    if op == "dm":
        if g is None:
            await q.answer("경기 정보를 다시 받는 중이에요. 잠시 뒤에 눌러 주세요.")
            return
        try:
            await bot.send_chat_action(q.from_user.id, "typing")
        except TelegramError:
            await q.answer(f"소담 1:1 을 한 번 열어 주세요 → @{bot.username} 시작 누른 뒤 다시 🔔", show_alert=True)
            return
        from .alerts import WATCH_KEEP
        label = f"{fmt.tag(g)} {fmt.matchup(g, score=False)}"
        from ..util import html_plain
        r = await svc.sports.alerts.watch(q.from_user.id, q.from_user.id, g.league, "", g.key, html_plain(label)[:80], "goals",
                                          expires=int(g.start) + WATCH_KEEP)
        await q.answer({"added": "📩 이 경기 골·결과를 1:1 로도 보낼게요", "exists": "이미 1:1 로 받고 있어요",
                        "full": "1:1 알림이 꽉 찼어요 (1:1 [⚽ 스포츠]에서 정리)"}[r])
        return
    if op == "an":
        if row["analyzed"]:
            await q.answer("이 경기 분석은 위에 올려 뒀어요.")
            return
        if g is None:
            await q.answer("경기 정보를 다시 받는 중이에요.")
            return
        claimed = await svc.db.atomic(lambda c: c.execute(
            "UPDATE sports_cards SET analyzed=1 WHERE chat_id=? AND game=? AND analyzed=0", (chat_id, key)).rowcount)
        if not claimed:
            await q.answer("이 경기 분석은 위에 올려 뒀어요.")
            return
        await q.answer("🧠 분석 중…")
        text = await analysis.analyze(svc, g)
        try:
            await bot.send_message(chat_id, text[:3900], parse_mode="HTML", reply_to_message_id=row["msg_id"])
        except TelegramError as e:
            log.warning("분석 보내기 실패 %s: %s", chat_id, e)
        return
    await q.answer()


def _find(svc, league: str, key: str):
    """캐시에 있는 그 경기 (카드는 진행 중 경기라 알림 폴링이 캐시를 채워 둠)."""
    from datetime import datetime, timedelta

    from .providers import KST
    lg = LEAGUES.get(league)
    if lg is None:
        return None
    today = datetime.fromtimestamp(svc.sports.feed.clock(), KST).date()
    for d in (today, today - timedelta(days=1), today + timedelta(days=1)):
        for g in svc.sports.feed.cached_day(lg, d) or []:
            if g.key == key:
                return g
    return None
