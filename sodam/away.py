"""💤 자리비움 (away): 대표/멤버가 "자러간다"고 설정하면, 그 사이 누가 @태그·답장으로 부르면 소담이 "지금 자리비움이에요: 멘트" 라고
방에 대신 알려 주고, 본인이 다시 말하면 자동 복귀하며 "자리비운 동안 N명이 찾았어요: …" 로 모아서 알려 준다.

2026-11-19 태산 대표 루디 요청 (자러갈 때 멘트 남기고, 호출하면 봇이 뜨게). 호출 대상 추출은 tagnotify.targets 재활용.
켜기 = `.자리비움 [멘트]`·`.잠수`·AI "나 잘게"(set_away). 끄기 = 본인이 방에서 다시 말하면 **자동** (또는 `.복귀`).
사람 기준(이 봇의 모든 방 공통). 부른 사람 1명당 COOLDOWN 초 1번(도배 방지). 쪽지함 = 부른 사람 모아 복귀 때 한 줄.
UI·AI 도구는 panels/away.py.
"""
from __future__ import annotations

import time

from telegram import ReplyParameters

from . import tagnotify
from .db import register_schema
from .hooks import add_group_message_hook
from .util import esc, mention, send_retry, user_name

register_schema("""
CREATE TABLE IF NOT EXISTS away (
    user_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT '',
    since   INTEGER NOT NULL,
    message TEXT NOT NULL DEFAULT ''
)""")
register_schema("""
CREATE TABLE IF NOT EXISTS away_pings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    caller_id INTEGER NOT NULL,
    caller    TEXT NOT NULL,
    ts        INTEGER NOT NULL
)""")
register_schema("CREATE INDEX IF NOT EXISTS away_pings_user ON away_pings(user_id)")

DEFAULT_MSG = "지금 자리 비움이에요. 돌아오면 확인할게요."
COOLDOWN = 600        # 부른 사람 1명당 자리비움 안내 간격(초)
MAX_PINGS = 10        # 복귀 때 보여줄 '찾은 사람' 최대 수
MAX_NOTIFY = 3        # 한 메시지가 자리비운 여러 명을 부를 때 안내 메시지 최대 수 (도배·레이트리밋 방지)
MAX_MSG = 200
CACHE_TTL = 30.0      # 자리비움 사용자 집합 캐시 (메시지마다 DB 안 치게 — 보통 비어 있어 훅이 바로 끝남)

# `.자리비움`/`.복귀` 명령은 명령 핸들러(c_away/c_back)가 처리 — 훅이 동시에(create_task) 끼어들어
# 방금 켠 걸 바로 끄거나(.자리비움) 중복 복귀(.복귀)하지 않게, 훅은 이 명령들을 건너뛴다.
_CMD_WORDS = {"자리비움", "잠수", "자러감", "away", "afk", "복귀", "돌아옴", "back"}


def _is_away_cmd(text: str) -> bool:
    t = (text or "").lstrip()
    if len(t) < 2 or t[0] not in "./":
        return False
    return t[1:].split()[0].split("@")[0].lower() in _CMD_WORDS

_away: set[int] = set()
_away_at = 0.0
_pinged: dict[tuple[int, int], float] = {}   # (부른 사람, 자리비운 사람) → monotonic ts


def _invalidate() -> None:
    global _away_at
    _away_at = 0.0


async def _ensure(db) -> set[int]:
    global _away, _away_at
    if time.monotonic() - _away_at > CACHE_TTL:
        _away_at = time.monotonic()
        _away = {r["user_id"] for r in await db._all("SELECT user_id FROM away")}
    return _away


async def set_away(db, uid: int, name: str, message: str) -> None:
    await db._write(
        "INSERT INTO away(user_id, name, since, message) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, since=excluded.since, message=excluded.message",
        (uid, name[:40], int(time.time()), (message or "")[:MAX_MSG]))
    _invalidate()


async def get_away(db, uid: int):
    return await db._one("SELECT * FROM away WHERE user_id=?", (uid,))


async def clear_away(db, uid: int) -> list[str]:
    """자리비움 해제 + 그 사이 부른 사람(중복 제거) 표시명 목록을 돌려준다."""
    # caller 는 '순수 이름'으로 저장 → 멘션은 여기서 caller_id 로 만든다 (HTML 앵커를 통째로 저장/자르지 않음)
    pings = [mention(r["caller_id"], r["caller"]) for r in await db._all(
        "SELECT caller_id, caller, MIN(ts) t FROM away_pings WHERE user_id=? GROUP BY caller_id ORDER BY t", (uid,))]
    await db._write("DELETE FROM away WHERE user_id=?", (uid,))
    await db._write("DELETE FROM away_pings WHERE user_id=?", (uid,))
    _invalidate()
    return pings


async def _record_ping(db, uid: int, caller_id: int, caller: str) -> None:
    # caller = 순수 이름(멘션은 복귀 때 caller_id 로 만듦). 같은 (자리비운 사람, 부른 사람)은 한 줄만 →
    # 긴 자리비움 + 재시작으로 _pinged 가 비어도 DB 가 안 불어남 (복귀 때 caller_id 로 묶어 보여줌).
    await db._write(
        "INSERT INTO away_pings(user_id, caller_id, caller, ts) SELECT ?,?,?,? "
        "WHERE NOT EXISTS (SELECT 1 FROM away_pings WHERE user_id=? AND caller_id=?)",
        (uid, caller_id, (caller or "")[:40], int(time.time()), uid, caller_id))


async def on_group_message(svc, bot, msg, role) -> None:
    """자리비운 사람이 말하면 자동 복귀, 자리비운 사람을 부르면 대신 안내. 아무도 자리비움 아니면 바로 끝(비용 0)."""
    db = svc.db
    away = await _ensure(db)
    if not away:
        return
    if _is_away_cmd(getattr(msg, "text", "")):
        return                            # .자리비움/.복귀 는 명령 핸들러(c_away/c_back)가 처리 — 훅 레이스 방지
    uid = getattr(getattr(msg, "from_user", None), "id", None)
    if uid in away:                       # 자리비운 본인이 말함 → 자동 복귀
        pings = await clear_away(db, uid)
        if pings:
            shown = " ".join(pings[:MAX_PINGS]) + (f" 외 {len(pings) - MAX_PINGS}명" if len(pings) > MAX_PINGS else "")
            await send_retry(lambda: bot.send_message(
                msg.chat_id, f"👋 {mention(uid, user_name(msg.from_user))} 돌아왔어요! 자리비운 동안 "
                             f"<b>{len(pings)}명</b>이 찾았어요: {shown}", parse_mode="HTML"))
        return
    targets = await tagnotify.targets(svc, msg)   # @태그·답장 대상 (user_id, 종류)
    now = time.monotonic()
    mid = getattr(msg, "message_id", None)
    rp = ReplyParameters(mid, allow_sending_without_reply=True) if mid else None   # PTB 22.x: reply_to 는 없음
    sent = 0
    for tid, _kind in targets:
        if sent >= MAX_NOTIFY:            # 한 메시지가 여러 명 부르면 안내 수를 막음 (도배·레이트리밋)
            break
        if tid not in away or tid == uid:
            continue
        key = (uid or 0, tid)
        if now - _pinged.get(key, 0.0) < COOLDOWN:
            continue
        row = await get_away(db, tid)
        if row is None:
            continue
        _pinged[key] = now
        sent += 1
        note = esc(row["message"] or DEFAULT_MSG)
        await send_retry(lambda t=tid, r=row, n=note: bot.send_message(
            msg.chat_id, f"💤 {mention(t, r['name'] or '이 분')}님 지금 자리비움이에요: <i>{n}</i>",
            parse_mode="HTML", reply_parameters=rp))
        if uid:
            await _record_ping(db, tid, uid, user_name(msg.from_user))


add_group_message_hook(on_group_message)


# ── .자리비움 / .복귀 명령 (handlers 가 import 뒤 register_commands 부름) ──────────
async def c_away(c) -> None:
    msg_text = c.argstr.strip()
    await set_away(c.svc.db, c.user.id, user_name(c.user), msg_text)
    shown = f": <i>{esc(msg_text[:MAX_MSG])}</i>" if msg_text else ""
    await c.reply(f"💤 자리비움으로 바꿨어요{shown}\n돌아와서 방에 글 쓰면 자동으로 풀려요 (<code>.복귀</code> 로도).")


async def c_back(c) -> None:
    if await get_away(c.svc.db, c.user.id) is None:
        await c.reply("자리비움 상태가 아니에요.")
        return
    pings = await clear_away(c.svc.db, c.user.id)
    extra = (f"\n자리비운 동안 <b>{len(pings)}명</b>이 찾았어요: " + " ".join(pings[:MAX_PINGS])) if pings else ""
    await c.reply(f"👋 돌아왔어요!{extra}")


def register_commands() -> None:
    from . import commands
    from .permissions import Role
    for cmd in (
        commands.Cmd(("자리비움", "잠수", "자러감", "away", "afk"), c_away, Role.MEMBER,
                     usage="[멘트]", help="자리비움 (누가 부르면 소담이 대신 안내)", group="💤 자리비움"),
        commands.Cmd(("복귀", "돌아옴", "back"), c_back, Role.MEMBER, help="자리비움 풀기", group="💤 자리비움"),
    ):
        commands.COMMANDS.append(cmd)
        for name in cmd.names:   # _INDEX 에도 넣어야 parse 가 명령으로 인식 (빠져서 .자리비움 이 통째로 씹혔음 — 2026-11-23 일루왕)
            commands._INDEX.setdefault(name.lower(), cmd)
