"""❓ 되묻기 버튼 (AI 도구 ask_choice): 답에 따라 할 일이 달라질 때만 짧은 질문 + 보기 버튼 2~4개.

흐름: AI 가 ask_choice(question, options) → 요청한 사람의 메시지에 답장으로 질문 + [보기…] + [✏️ 직접 입력]
(menu.lasting_token = 재시작 뒤에도 버튼 유효, 요청한 사람만) → ctx.quiet (이번 실행의 AI 글은 안 보냄).
누르면: 질문 메시지를 '✅ 고른 것'으로 고치고 버튼을 없앰(DB 차지 한 번 → 연타·다른 보기 동시 눌러도 1번) →
같은 사람으로 에이전트를 다시 돌림 (요청 '(선택: 보기) 질문', 질문에 답장, 지난 대화 이어서, 보통 AI 답처럼 기록).
제재 같은 도구는 다시 돌린 실행에서도 원래대로 확인 카드를 거친다 (여기서 바로 실행하는 것 없음).

막는 것: 기록을 읽은 답변(ctx.tainted — 읽은 글 속 지시로 버튼을 못 만들게) · 한 실행에 1번 · 먼저 끼어들기/아침 인사
(도구 목록에 없음 + 여기서 한 번 더) · 다시 돌린 실행에서 또 묻기(무한 되묻기 방지).
질문은 10분 뒤 닫힘 (눌러도 '시간 지남' 으로 버튼만 없앰). 질문 메시지에 답장으로 직접 쓰면 보통 AI 호출
(ai_turns 에 질문을 AI 답으로 기록 → handlers 가 답장을 호출로 봄) + 방에선 버튼을 닫음 (그룹 메시지 훅).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from openai import OpenAIError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, User
from telegram.error import TelegramError

from .. import agentlog, hooks, memory, menu, security, tools
from ..db import register_schema
from ..menu import PanelCtx, Screen
from ..util import esc, mention, user_name

log = logging.getLogger(__name__)

QUESTION_CHARS = 120
LABEL_CHARS = 20
MIN_OPTS, MAX_OPTS = 2, 4
ASK_TTL = 600                 # 질문이 열려 있는 시간 (10분)
TOKEN_TTL = ASK_TTL + 3000    # 버튼 토큰은 조금 더 길게 → 10분 넘어 눌러도 '시간 지남'으로 버튼을 정리해 줌
KEEP = 86400                  # 표 정리
SENT = "질문을 보냄 — 답을 기다림 (다른 말 하지 말 것)"
_USED = "_askchoice_used"     # ToolCtx 에 붙이는 표시 (한 실행에 1번)
TASKS: set[asyncio.Task] = set()   # 누른 뒤 다시 돌리는 에이전트 (테스트가 기다림)

register_schema("""
CREATE TABLE IF NOT EXISTS ask_choices (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    username TEXT,
    question TEXT NOT NULL,
    options  TEXT NOT NULL,           -- JSON [보기]
    toks     TEXT NOT NULL DEFAULT '[]',  -- JSON [보기 토큰] (직접 입력을 누른 뒤 버튼 다시 그리기)
    request  TEXT NOT NULL DEFAULT '',
    msg_id   INTEGER,
    created  REAL NOT NULL,
    expires  REAL NOT NULL,
    status   TEXT,                    -- NULL 열림 · chosen · typed · expired
    chosen   TEXT
);
CREATE INDEX IF NOT EXISTS ask_choices_msg ON ask_choices(chat_id, msg_id);
""", migrate={"ask_choices": "plain"})


# ── AI 도구 ───────────────────────────────────────────────
def _clean(q, opts) -> tuple[str, list[str], str | None]:
    question = " ".join(str(q or "").split())
    if not question:
        return "", [], "question 이 비었음."
    if len(question) > QUESTION_CHARS:
        return "", [], f"question 은 {QUESTION_CHARS}자 이내로 줄여서 다시."
    if not isinstance(opts, list):
        return "", [], "options 는 보기 글 목록이어야 함."
    labels = list(dict.fromkeys(" ".join(str(o or "").split()) for o in opts))
    if any(not x for x in labels):
        return "", [], "빈 보기가 있음."
    if any(len(x) > LABEL_CHARS for x in labels):
        return "", [], f"보기는 각각 {LABEL_CHARS}자 이내로 줄여서 다시."
    if not MIN_OPTS <= len(labels) <= MAX_OPTS:
        return "", [], f"보기는 서로 다른 것 {MIN_OPTS}~{MAX_OPTS}개."
    return question, labels, None


async def _request_msg(ctx: tools.ToolCtx) -> int | None:
    """요청한 사람의 가장 최근 메시지 (지금 답하는 요청 — 연달아 보낸 말도 마지막 것에 답함)."""
    row = await ctx.svc.db._one("SELECT msg_id FROM messages WHERE chat_id=? AND user_id=? AND is_bot=0 "
                                "AND msg_id IS NOT NULL ORDER BY id DESC LIMIT 1", (ctx.chat_id, ctx.caller.id))
    return row["msg_id"] if row else None


def _keyboard(labels: list[str], toks: list[str], free: str | None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(x, callback_data=f"m:k:{t}")] for x, t in zip(labels, toks)]
    if free:
        rows.append([InlineKeyboardButton("✏️ 직접 입력", callback_data=f"m:k:{free}")])
    return InlineKeyboardMarkup(rows)


def _question_html(question: str, name: str) -> str:
    return f"❓ {esc(question)}\n<i>({esc(name)}님만 누를 수 있어요)</i>"


async def t_ask_choice(ctx: tools.ToolCtx, a: dict) -> str:
    if ctx.tainted:   # execute 도 막지만 한 번 더 (읽은 글 속 지시로 버튼·다시 실행이 이어지지 않게)
        return "기록을 읽은 답변에서는 되묻기 버튼을 못 씀. 필요하면 글로 물어볼 것."
    run = agentlog.current.get()
    if run is not None and run.mode in ("chime", "morning"):
        return "이 도구는 지금 사용할 수 없음."
    if getattr(ctx, _USED, False):
        return "이번 답변에서는 이미 질문을 보냈음 (한 번만). 다른 말 하지 말 것."
    question, labels, err = _clean(a.get("question"), a.get("options"))
    if err:
        return err
    if any(security.scan(x).blocked for x in (question, *labels)):
        return "지시문 같은 문장이라 버튼을 만들지 않았음. 글로 짧게 물어볼 것."
    svc, uid, cid = ctx.svc, ctx.caller.id, ctx.chat_id
    now = time.time()
    request = run.trigger if run is not None else ""
    name = user_name(ctx.caller)
    await svc.db._write("DELETE FROM ask_choices WHERE created < ?", (now - KEEP,))
    ask_id = await svc.db._write(
        "INSERT INTO ask_choices(chat_id, user_id, name, username, question, options, request, created, expires) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (cid, uid, name[:40], getattr(ctx.caller, "username", None), question,
                                      json.dumps(labels, ensure_ascii=False), request[:500], now, now + ASK_TTL))
    toks = [await menu.lasting_token(svc, uid, cid, "ask_pick", {"a": ask_id, "i": i}, TOKEN_TTL) for i in range(len(labels))]
    free = await menu.lasting_token(svc, uid, cid, "ask_free", {"a": ask_id}, TOKEN_TTL)
    await svc.db._write("UPDATE ask_choices SET toks=? WHERE id=?", (json.dumps(toks), ask_id))
    req_msg = await _request_msg(ctx)
    try:
        sent = await ctx.bot.send_message(
            cid, _question_html(question, name), parse_mode="HTML", reply_markup=_keyboard(labels, toks, free),
            reply_parameters=ReplyParameters(req_msg, allow_sending_without_reply=True) if req_msg else None)
    except TelegramError as e:
        log.warning("ask_choice send failed chat=%s: %s", cid, e)
        await svc.db._write("UPDATE ask_choices SET status='failed' WHERE id=?", (ask_id,))
        return "질문을 보내지 못했음. 글로 짧게 물어볼 것."
    await svc.db._write("UPDATE ask_choices SET msg_id=? WHERE id=?", (sent.message_id, ask_id))
    plain = question + " [" + " / ".join(labels) + "]"
    try:   # 대화 기록 + AI 답으로 기록 → 질문에 답장으로 직접 쓰면 보통 AI 호출, 다음 실행에 지난 대화로 보임
        await svc.db.log_message(cid, ctx.bot.id, sent.message_id, plain, is_bot=True,
                                 reply_to_msg_id=req_msg, reply_to_user=uid if req_msg else None)
        await memory.record_turn(svc.db, cid, uid, "call", request or question, plain, sent.message_id)
    except Exception:   # 기록 실패해도 버튼은 산다
        log.exception("ask_choice record failed")
    setattr(ctx, _USED, True)
    ctx.quiet = True
    return SENT


# ── 버튼 ──────────────────────────────────────────────────
async def _claim(svc, ask_id: int, status: str, chosen: str | None = None) -> bool:
    """열린 질문을 한 번만 닫음 (연타·보기 둘 동시에 눌러도 1번). 시간 지난 건 못 차지."""
    now = time.time()
    return bool(await svc.db.atomic(lambda cn: cn.execute(
        "UPDATE ask_choices SET status=?, chosen=? WHERE id=? AND status IS NULL AND expires>=?",
        (status, chosen, ask_id, now)).rowcount))


async def _closed(c: PanelCtx, row) -> Screen:
    """이미 닫힌 질문: 시간 지남이면 버튼을 없애고 안내, 이미 고른 거면 토스트만."""
    if row is None:
        return menu.EXPIRED
    if row["status"] in (None, "expired") and row["expires"] < time.time():
        await c.svc.db._write("UPDATE ask_choices SET status='expired' WHERE id=? AND status IS NULL", (row["id"],))
        return Screen(f"❓ {esc(row['question'])}\n⌛ 10분이 지나 닫았어요. 필요하면 다시 말해 주세요.", None,
                      toast="시간이 지났어요.")
    return Screen(None, toast="이미 골랐어요.")


async def t_pick(c: PanelCtx, spec) -> Screen:
    spec = spec if isinstance(spec, dict) else {}
    row = await c.svc.db._one("SELECT * FROM ask_choices WHERE id=?", (int(spec.get("a", 0) or 0),))
    labels = json.loads(row["options"]) if row else []
    i = spec.get("i")
    if row is None or row["user_id"] != c.uid or not isinstance(i, int) or not 0 <= i < len(labels):
        return menu.EXPIRED
    label = labels[i]
    if not await _claim(c.svc, row["id"], "chosen", label):
        return await _closed(c, row)
    task = asyncio.create_task(_rerun(c.svc, c.bot, row, label))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return Screen(f"❓ {esc(row['question'])}\n→ ✅ <b>{esc(label)}</b>", None, toast=f"'{label}' 골랐어요")


async def t_free(c: PanelCtx, spec) -> Screen:
    """[✏️ 직접 입력]: 답장으로 쓰라고 안내하고 보기 버튼은 그대로 (이 버튼만 빠짐)."""
    spec = spec if isinstance(spec, dict) else {}
    row = await c.svc.db._one("SELECT * FROM ask_choices WHERE id=?", (int(spec.get("a", 0) or 0),))
    if row is None or row["user_id"] != c.uid:
        return menu.EXPIRED
    if row["status"] is not None or row["expires"] < time.time():
        return await _closed(c, row)
    labels, toks = json.loads(row["options"]), json.loads(row["toks"])
    return Screen(_question_html(row["question"], row["name"]) + "\n✏️ 이 메시지에 <b>답장</b>으로 직접 써 주세요.",
                  _keyboard(labels, toks, None), toast="이 질문에 답장으로 써 주세요.")


async def _rerun(svc, bot, row, label: str) -> None:
    """고른 보기로 같은 사람의 에이전트를 다시 돌려 질문에 답장 (handlers.ai_reply 의 뒷부분과 같은 순서)."""
    from .. import handlers   # 순환 import 방지 (handlers → menu → panels)
    from ..agent import run_agent
    from ..llm import BudgetExceeded
    cid, uid, qmsg = row["chat_id"], row["user_id"], row["msg_id"]
    try:
        s = await svc.db.get_settings(cid)
        if not s["ai_enabled"] or svc.llm is None or not getattr(svc.llm, "enabled", False):
            return
        caller = User(uid, row["name"] or "알 수 없음", False, username=row["username"])
        role = await svc.perms.role(bot, cid, uid)   # 누를 때 권한으로 (그 사이 관리자에서 내려왔을 수 있음)
        member = await svc.db.get_member(cid, uid)
        style = (member["style"] if member else None) or s["style"]
        notes = json.loads(member["notes"]) if member and member["notes"] else {}
        history = await svc.db.recent_messages(cid, handlers.HISTORY_LIMIT,
                                               since=int(time.time()) - handlers.HISTORY_HOURS * 3600)
        request = f"(선택: {label}) {row['question']}"
        ctx = tools.ToolCtx(svc, bot, cid, caller, role, s)
        setattr(ctx, _USED, True)   # 고른 뒤 또 되묻지 않게
        try:
            answer = await run_agent(ctx, style_key=style, notes=notes, history=history,
                                     reply_to=f"{svc.cfg.bot_name}(봇): {row['question']}", request=request, mode="call")
        except BudgetExceeded:
            answer = "오늘 AI 사용량을 다 써서 내일 다시 불러주세요 🙏"
        except OpenAIError as e:
            log.warning("ask_choice rerun openai error: %s", e)
            answer = "AI 연결이 잠깐 불안정해요. 잠시 후 다시 불러주세요."
        if ctx.quiet:
            return
        usernames = {r["username"].lower() for r in await svc.db.member_names(cid) if r["username"]}
        out = security.filter_output(answer, max_chars=s["reply_max_chars"], allowed_usernames=usernames)
        if not out:
            return
        body = esc(out)
        if ctx.mentions:
            body = " ".join(mention(m, n) for m, n in dict(ctx.mentions).items()) + " " + body
        sent = await bot.send_message(cid, body, parse_mode="HTML", link_preview_options=security.NO_PREVIEW,
                                      reply_parameters=ReplyParameters(qmsg, allow_sending_without_reply=True) if qmsg else None)
        await svc.db.log_message(cid, bot.id, sent.message_id, out, is_bot=True,
                                 reply_to_msg_id=qmsg, reply_to_user=uid if qmsg else None)
        await memory.record_turn(svc.db, cid, uid, "call", request, out, sent.message_id)
    except Exception:   # 뒤에서 도는 작업: 실패해도 봇은 계속
        log.exception("ask_choice rerun failed chat=%s", cid)


# ── 직접 답장하면 버튼 닫기 (그룹방) ───────────────────────
async def on_group_message(svc, bot, msg, role) -> None:
    r = getattr(msg, "reply_to_message", None)
    if not r or not getattr(r, "from_user", None) or r.from_user.id != bot.id or not msg.from_user:
        return   # 대부분의 메시지는 여기서 끝 (DB 안 봄)
    row = await svc.db._one("SELECT id, user_id FROM ask_choices WHERE chat_id=? AND msg_id=? AND status IS NULL",
                            (msg.chat_id, r.message_id))
    if row is None or row["user_id"] != msg.from_user.id or not await _claim(svc, row["id"], "typed"):
        return
    try:
        await bot.edit_message_reply_markup(chat_id=msg.chat_id, message_id=r.message_id, reply_markup=None)
    except TelegramError:
        pass


TOOL = tools.Tool(
    "ask_choice",
    "답에 따라 할 일이 확실히 달라질 때만 되묻는 버튼 (보기 2~4개, 추천을 맨 앞). 예: 같은 이름 두 사람 중 누구, "
    "어느 방, 뮤트 1시간/1일, 어느 음악 봇. 그 밖엔 글로 묻거나 바로 할 것. 보낸 뒤엔 다른 말 하지 말 것.",
    {"question": {"type": "string", "maxLength": QUESTION_CHARS, "description": f"짧은 질문 ({QUESTION_CHARS}자 이내)"},
     "options": {"type": "array", "minItems": MIN_OPTS, "maxItems": MAX_OPTS,
                 "items": {"type": "string", "maxLength": LABEL_CHARS},
                 "description": f"보기 글 ({LABEL_CHARS}자 이내, 추천 먼저)"}},
    ["question", "options"], t_ask_choice)
tools.register_tool(TOOL)
menu.register_token_action("ask_pick", t_pick, need=menu.PUBLIC)
menu.register_token_action("ask_free", t_free, need=menu.PUBLIC)
hooks.add_group_message_hook(on_group_message)
