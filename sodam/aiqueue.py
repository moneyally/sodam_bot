"""AI 요청 영구 대기열 (Codex ext/queue 참고): 받은 요청을 DB 에 적고 답을 보내면 지운다.

실제 사례(2026-09-28): 컨테이너 네트워크 끊김·배포 재시작 순간 만들던 답이 3번 사라짐.
- put: 검사를 통과한 요청 (handlers.ai_reply) → 줄 하나. 이 프로세스가 처리 중이면 RUNNING 에 키.
- done: 답을 보냈거나 답할 수 없는 오류 → 지움 (보관한 답이 있는 줄은 안 지움).
- keep_answer: 답은 만들었는데 텔레그램에 확실히 못 보냄(연결 실패) → 답을 저장, sweep 이 다시 보냄 (AI 다시 안 부름).
- sweep (persist.job_sweep, 30초): 이 프로세스가 처리 중이 아닌 줄 = 재시작·끊김으로 남은 것 → 답이 있으면 보내고,
  없으면 요청을 다시 실행 (한 줄 MAX_TRIES 번까지, 메시지 STALE_SEC 넘으면 버림 — util.is_stale 과 같은 기준).
  이미 그 메시지에 답한 기록(messages)이 있으면 다시 안 함 (보낸 직후·지우기 전에 죽은 경우 중복 방지).
"""
from __future__ import annotations

import json
import logging
import time

from telegram import Message, ReplyParameters
from telegram.error import BadRequest, Forbidden, NetworkError

from .db import register_schema
from .util import STALE_SEC, sent_at, surely_unsent

log = logging.getLogger(__name__)

MAX_TRIES = 2
RUNNING: set[tuple[int, int, int]] = set()   # (bot_id, chat_id, msg_id) — 이 프로세스가 지금 처리 중

register_schema("""
CREATE TABLE IF NOT EXISTS ai_queue (
    bot_id  INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role    INTEGER NOT NULL,
    via     TEXT NOT NULL,
    request TEXT NOT NULL,
    msg     TEXT NOT NULL,
    ts      REAL NOT NULL,
    answer  TEXT,
    tries   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, chat_id, msg_id)
);
""")


def _dump(msg) -> str:
    """메시지 → JSON (재시작 뒤 Message.de_json 으로 되살림). 사진·답장 글까지 그대로."""
    if hasattr(msg, "to_dict"):
        return json.dumps(msg.to_dict(), ensure_ascii=False)
    u = msg.from_user
    return json.dumps({"message_id": msg.message_id, "date": int(sent_at(msg) or time.time()),
                       "chat": {"id": msg.chat_id, "type": "supergroup" if msg.chat_id < 0 else "private"},
                       "from": {"id": u.id, "is_bot": False, "first_name": u.first_name, "username": u.username},
                       "text": msg.text or "", **({"caption": msg.caption} if msg.caption else {})}, ensure_ascii=False)


def _key(bot, chat_id: int, msg_id: int) -> tuple[int, int, int]:
    return int(bot.id), chat_id, msg_id


async def put(db, bot, msg, role: int, via: str, request: str) -> None:
    RUNNING.add(_key(bot, msg.chat_id, msg.message_id))
    await db._write("INSERT OR IGNORE INTO ai_queue(bot_id, chat_id, msg_id, user_id, role, via, request, msg, ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (int(bot.id), msg.chat_id, msg.message_id, msg.from_user.id, int(role),
                                                  via, request[:4000], _dump(msg), sent_at(msg) or time.time()))


async def append(db, bot, chat_id: int, msg_id: int, text: str) -> None:
    """실행 중인 요청에 이어 보낸 말을 그 줄에도 덧붙임 — 그 실행이 재시작으로 끊기면 다시 돌 때 이어 보낸 말까지
    (예전: 이어 보낸 말은 메모리(Steer)에만 있어서 다시 돌면 처음 말만 처리, 조사 2026-10-09)."""
    if not text:
        return
    await db._write("UPDATE ai_queue SET request=substr(request || char(10) || ?, 1, 4000) "
                    "WHERE bot_id=? AND chat_id=? AND msg_id=? AND answer IS NULL", (text, int(bot.id), chat_id, msg_id))


async def done(db, bot, chat_id: int, msg_id: int) -> None:
    RUNNING.discard(_key(bot, chat_id, msg_id))
    await db._write("DELETE FROM ai_queue WHERE bot_id=? AND chat_id=? AND msg_id=? AND answer IS NULL",
                    (int(bot.id), chat_id, msg_id))


async def keep_answer(db, bot, chat_id: int, msg_id: int, body: str) -> None:
    await db._write("UPDATE ai_queue SET answer=? WHERE bot_id=? AND chat_id=? AND msg_id=?",
                    (body, int(bot.id), chat_id, msg_id))
    RUNNING.discard(_key(bot, chat_id, msg_id))


async def _answered(db, chat_id: int, msg_id: int) -> bool:
    return bool(await db._one("SELECT 1 FROM messages WHERE chat_id=? AND reply_to_msg_id=? AND is_bot=1 LIMIT 1",
                              (chat_id, msg_id)))


async def sweep(context, now: float | None = None) -> int:
    """남은 줄 처리 (위 설명). 다시 보냈거나 다시 실행한 수."""
    from . import handlers, security   # handlers 가 이 모듈을 씀 (순환 import 피함)
    svc, bot = context.bot_data["svc"], context.bot
    db, bid, now = svc.db, int(bot.id), time.time() if now is None else now
    await db._write("DELETE FROM ai_queue WHERE bot_id=? AND (ts<? OR tries>=?)", (bid, now - STALE_SEC, MAX_TRIES))
    n = 0
    for r in await db._all("SELECT * FROM ai_queue WHERE bot_id=? ORDER BY ts", (bid,)):
        key = (bid, r["chat_id"], r["msg_id"])
        if key in RUNNING:
            continue
        await db._write("UPDATE ai_queue SET tries=tries+1 WHERE bot_id=? AND chat_id=? AND msg_id=?", key)
        if r["answer"] is None and await _answered(db, r["chat_id"], r["msg_id"]):
            await db._write("DELETE FROM ai_queue WHERE bot_id=? AND chat_id=? AND msg_id=?", key)
            continue
        n += 1
        if r["answer"] is not None:
            group = r["chat_id"] < 0
            quote = group and (await db.get_settings(r["chat_id"]))["ai_quote"]   # 방 설정 ai_quote (handlers 와 같게)
            reply = ReplyParameters(r["msg_id"], allow_sending_without_reply=True) if quote else None
            try:
                sent = await bot.send_message(r["chat_id"], r["answer"], parse_mode="HTML", reply_parameters=reply,
                                              link_preview_options=security.NO_PREVIEW)
            except NetworkError as e:
                if surely_unsent(e):
                    continue          # 아직 끊김 → 다음 sweep (tries 까지)
            except (BadRequest, Forbidden):
                pass
            else:   # 실제 답 글로 기록 + 대화 기록 (예전: '(다시 보낸 답)' 글자만 → 그 답에 이어 말하면 무슨 말 했는지 몰랐음)
                from . import memory
                from .util import html_plain
                plain = html_plain(r["answer"])
                await db.log_message(r["chat_id"], bot.id, sent.message_id, plain, is_bot=True,
                                     reply_to_msg_id=r["msg_id"] if group else None,
                                     reply_to_user=r["user_id"] if group else None)
                try:
                    await memory.record_turn(db, r["chat_id"], r["user_id"], r["via"], r["request"], plain, sent.message_id)
                except Exception as e:
                    log.debug("record turn failed: %r", e)
            await db._write("DELETE FROM ai_queue WHERE bot_id=? AND chat_id=? AND msg_id=?", key)
            continue
        msg = Message.de_json(json.loads(r["msg"]), bot)
        log.info("대기열에 남은 AI 요청 다시 실행 chat=%s msg=%s", r["chat_id"], r["msg_id"])
        RUNNING.add(key)
        try:
            await handlers.ai_reply(context, msg, handlers.Role(r["role"]), r["request"], security.scan(r["request"]),
                                    r["via"], followup=True)
        finally:
            RUNNING.discard(key)
    return n
