"""👍 멤버 평판(karma) — 글에 '+1'·👍 답장으로 상대 추천. 2026-11-22 경쟁조사 ⑦ (KarmaBot·reputation-bot).

포인트 게임(casino)과 완전 분리 — 순수 추천/인정. 돈·게임 아님.
- 글에 답장으로 '+1'·'👍'·'추천'·'따봉' → 그 사람 평판 +1
- 자기 자신 금지, 같은 상대에게 하루 1번, 봇 제외
- .평판 (내/남 점수·순위) · .평판순위
명령 등록은 handlers 가 import 뒤 register_commands()."""
import re
import time

from .db import KST_SEC, register_schema

register_schema("""
CREATE TABLE IF NOT EXISTS karma (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    points  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
)""")
register_schema("""
CREATE TABLE IF NOT EXISTS karma_given (
    chat_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,
    to_id   INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    PRIMARY KEY (chat_id, from_id, to_id, day)
)""")

# 글 전체가 이것만일 때만 (대화 중 '+1분' 같은 건 안 걸림)
PLUS = re.compile(r"\s*(\+1|\+\+|👍|추천|따봉|굿잡|goodjob)\s*", re.I)


def _day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + KST_SEC))


async def give(db, chat_id: int, from_id: int, to_id: int) -> int | None:
    """추천 1점. 준 뒤 상대의 총점 반환. 자기 자신·하루 중복이면 None (무시)."""
    if from_id == to_id:
        return None
    day = _day()

    def fn(cn):
        if cn.execute("SELECT 1 FROM karma_given WHERE chat_id=? AND from_id=? AND to_id=? AND day=?",
                      (chat_id, from_id, to_id, day)).fetchone():
            return None                      # 오늘 이미 이 사람에게 줬음
        cn.execute("INSERT INTO karma_given(chat_id, from_id, to_id, day) VALUES(?,?,?,?)",
                   (chat_id, from_id, to_id, day))
        cn.execute("INSERT INTO karma(chat_id, user_id, points) VALUES(?,?,1) "
                   "ON CONFLICT(chat_id, user_id) DO UPDATE SET points=points+1", (chat_id, to_id))
        return cn.execute("SELECT points FROM karma WHERE chat_id=? AND user_id=?", (chat_id, to_id)).fetchone()[0]
    return await db.atomic(fn)


async def points(db, chat_id: int, uid: int) -> int:
    row = await db._one("SELECT points FROM karma WHERE chat_id=? AND user_id=?", (chat_id, uid))
    return row["points"] if row else 0


async def rank(db, chat_id: int, uid: int) -> tuple[int, int]:
    """(순위, 점수). 점수 0이면 (0, 0) — 순위 없음."""
    pts = await points(db, chat_id, uid)
    if pts <= 0:
        return 0, 0
    row = await db._one("SELECT COUNT(*) n FROM karma WHERE chat_id=? AND points>?", (chat_id, pts))
    return (row["n"] + 1 if row else 1), pts


async def top(db, chat_id: int, n: int = 10) -> list:
    return await db._all("SELECT user_id, points FROM karma WHERE chat_id=? AND points>0 "
                         "ORDER BY points DESC, user_id LIMIT ?", (chat_id, n))


async def handle(svc, bot, msg) -> bool:
    """답장으로 '+1'·👍 → 평판 +1. 처리했으면 True(게임·AI 안 탐), 아니면 False."""
    reply = getattr(msg, "reply_to_message", None)
    text = getattr(msg, "text", None) or ""
    if reply is None or not PLUS.fullmatch(text):
        return False
    to = getattr(reply, "from_user", None)
    if to is None or getattr(to, "is_bot", False):
        return False
    pts = await give(svc.db, msg.chat_id, msg.from_user.id, to.id)
    if pts is None:
        return True                          # 자기 자신·하루 중복 → 조용히 무시 (도배 방지)
    from .util import mention
    name = getattr(to, "first_name", None) or "그분"
    try:
        await bot.send_message(msg.chat_id, f"👍 {mention(to.id, name)}님 평판 +1 (총 {pts}점)", parse_mode="HTML")
    except Exception:
        pass
    return True


def register_commands() -> None:
    from .panels import karma as panel
    panel.register_commands()
