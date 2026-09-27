"""📒 AI 작업 기록 (에이전트 실행 1번 = 1줄, 화면은 panels/agentlog.py).

무엇을 요청받았고(trigger — 멤버 글이라 데이터로만 저장·표시) → 어떤 도구를 어떤 인자로 불러 무엇을 받았고(steps) →
결과(status) · 토큰(입력·캐시·출력) · 요금(costs.usd_micro, 정수 마이크로달러).
토큰·요금은 llm._record 가 ContextVar `current` 로 지금 실행에 더한다 → 도구 안에서 부른 AI(웹검색·요약 등)도 같은 실행에 잡힘.
기록 실패는 답에 영향 없음(삼킴). 보관 14일: 넣을 때 한 시간에 한 번 오래된 줄을 같은 db.atomic 안에서 지움.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from .db import register_schema

log = logging.getLogger(__name__)

KEEP_DAYS = 14
TRIGGER_CHARS = 200
ARGS_CHARS = 120
RESULT_CHARS = 120
MAX_STEPS_KEPT = 12       # 도구 라운드는 최대 8번(agent.MAX_STEPS), 보통 1~3번
PRUNE_EVERY = 3600
STATUS = {"answered": "✅ 답함", "tool_only": "🛠️ 도구만", "empty": "💤 빈 답", "error": "❌ 오류", "budget": "⛔ 한도"}

register_schema("""
CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER,
    ts          INTEGER NOT NULL,
    ms          INTEGER NOT NULL DEFAULT 0,
    mode        TEXT NOT NULL DEFAULT '',
    purpose     TEXT NOT NULL DEFAULT '',
    trigger     TEXT NOT NULL DEFAULT '',
    models      TEXT NOT NULL DEFAULT '',
    steps       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,
    tok_in      INTEGER NOT NULL DEFAULT 0,
    tok_cached  INTEGER NOT NULL DEFAULT 0,
    tok_out     INTEGER NOT NULL DEFAULT 0,
    usd_micro   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_chat ON agent_runs(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs(ts);
""", migrate={"agent_runs": "plain"})

# 기록에 남기면 안 되는 값: 인자 이름이 비밀값 같거나, 값이 API 키·봇 토큰 모양이면 가린다
_SECRET_NAME = re.compile(r"key|token|secret|passw|seed|private|mnemonic", re.I)
_SECRET_VALUE = re.compile(r"sk-[A-Za-z0-9_-]{16,}|\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def clip(text: str, n: int) -> str:
    """한 줄로 줄이고 n 자에서 자름 + 비밀값 모양 가리기."""
    text = _SECRET_VALUE.sub("[가림]", " ".join(str(text or "").split()))
    return text if len(text) <= n else text[:n - 1] + "…"


def summarize_args(raw: str | None) -> str:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return clip(raw or "", ARGS_CHARS)
    if not isinstance(data, dict):
        return clip(json.dumps(data, ensure_ascii=False), ARGS_CHARS)
    parts = []
    for k, v in data.items():
        if _SECRET_NAME.search(str(k)):
            v = "***"
        elif not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={clip(v, 60)}")
    return clip(", ".join(parts), ARGS_CHARS)


@dataclass
class Run:
    chat_id: int
    user_id: int | None
    mode: str
    trigger: str
    purpose: str = ""
    started: float = field(default_factory=time.monotonic)
    steps: list[dict] = field(default_factory=list)
    models: dict[str, int] = field(default_factory=dict)   # 모델 → 호출 수
    tok_in: int = 0
    tok_cached: int = 0
    tok_out: int = 0
    usd_micro: int = 0
    closed: bool = False

    def add(self, model: str, inp: int, cached: int, out: int, micro: int) -> None:
        if self.closed:   # 끝난 뒤 뒤늦게 온 사용량(도구가 띄운 백그라운드 작업)은 이 실행에 안 넣음
            return
        if model:
            self.models[model] = self.models.get(model, 0) + 1
        self.tok_in += inp
        self.tok_cached += cached
        self.tok_out += out
        self.usd_micro += micro

    def step(self, name: str, args: str | None, result: str) -> None:
        if len(self.steps) < MAX_STEPS_KEPT:
            self.steps.append({"tool": clip(name, 40), "args": summarize_args(args), "result": clip(result, RESULT_CHARS)})


current: ContextVar[Run | None] = ContextVar("sodam_agent_run", default=None)


def add_usage(model: str, inp: int, cached: int, out: int, micro: int) -> None:
    """llm._record 가 부른다. 에이전트 실행 중이 아니면 아무것도 안 함."""
    run = current.get()
    if run is not None:
        run.add(model, inp, cached, out, micro)


def start(chat_id: int, user_id: int | None, mode: str, trigger: str) -> tuple[Run, Token]:
    run = Run(chat_id, user_id, mode, clip(trigger, TRIGGER_CHARS))
    return run, current.set(run)


async def finish(db, run: Run, token: Token, status: str) -> None:
    """실행 끝: ContextVar 되돌리고(바깥 실행이 있으면 사용량을 거기에도 더함) 한 줄 저장. 실패해도 예외 안 냄."""
    run.closed = True
    try:
        current.reset(token)
    except ValueError:   # 다른 컨텍스트에서 끝난 경우 (정상 경로에선 없음)
        current.set(None)
    parent = current.get()
    if parent is not None:
        parent.add("", run.tok_in, run.tok_cached, run.tok_out, run.usd_micro)
        for m, n in run.models.items():
            parent.models[m] = parent.models.get(m, 0) + n
    try:
        await save(db, run, status)
    except Exception:
        log.exception("AI 작업 기록 실패 (답은 그대로)")


_last_prune = float("-inf")


async def save(db, run: Run, status: str) -> int:
    global _last_prune
    now = int(time.time())
    prune = time.monotonic() - _last_prune > PRUNE_EVERY
    models = ",".join(f"{m}×{n}" if n > 1 else m for m, n in run.models.items())
    row = (run.chat_id, run.user_id, now, int((time.monotonic() - run.started) * 1000), clip(run.mode, 20),
           clip(run.purpose, 40), run.trigger, clip(models, 120), json.dumps(run.steps, ensure_ascii=False),
           status if status in STATUS else "error", run.tok_in, run.tok_cached, run.tok_out, run.usd_micro)

    def work(c) -> int:
        cur = c.execute("INSERT INTO agent_runs(chat_id, user_id, ts, ms, mode, purpose, trigger, models, steps, status, "
                        "tok_in, tok_cached, tok_out, usd_micro) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        if prune:
            c.execute("DELETE FROM agent_runs WHERE ts<?", (now - KEEP_DAYS * 86400,))
        return cur.lastrowid
    rid = await db.atomic(work)
    if prune:
        _last_prune = time.monotonic()
    return rid


# ── 조회 (화면용). chat_id 를 주면 그 방 것만 ───────────────
async def recent(db, chat_id: int | None, limit: int, offset: int = 0) -> list:
    where, params = ("WHERE chat_id=?", (chat_id,)) if chat_id is not None else ("", ())
    return await db._all(f"SELECT * FROM agent_runs {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))


async def count(db, chat_id: int | None) -> int:
    where, params = ("WHERE chat_id=?", (chat_id,)) if chat_id is not None else ("", ())
    return (await db._one(f"SELECT COUNT(*) AS n FROM agent_runs {where}", params))["n"]


async def get(db, run_id: int, chat_id: int | None) -> object | None:
    if chat_id is None:
        return await db._one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
    return await db._one("SELECT * FROM agent_runs WHERE id=? AND chat_id=?", (run_id, chat_id))


def steps_of(row) -> list[dict]:
    try:
        steps = json.loads(row["steps"] or "[]")
    except ValueError:
        return []
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []
