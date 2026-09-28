"""끝말잇기 소담이의 한 수 — LLM 이 도구로 후보를 찾고·확인하고 골라서 둔다 (클로드코드식: 도구로 확인한 뒤 판단).

  find_words(hard)  사전에서 이을 수 있는 후보 + 각 후보 뒤에 '사람이 이을 말 수' (적을수록 사람이 힘듦)
  check_word(word)  사전에 있는지·끝 글자·이미 나왔는지·한방 단어인지
  play(word, line)  둔다 (+ 짧은 한마디). 코드가 다시 검사해서 틀리면 '안 됨: 이유'를 돌려주고 다시 고르게 한다.
판정은 코드만 (LLM 이 없는 말을 인정하지 못함). LLM 이 늦거나(TIMEOUT) 끝까지 못 두면 코드가 난이도대로 둔다.
AI 에 들어가는 건 한글 낱말뿐이라 (게임 답은 한글 음절만 통과) 대화 속 지시가 섞이지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TYPE_CHECKING

from . import games
from .prompt import style_block
from .security import filter_output

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

TIMEOUT = 6.0
MAX_STEPS = 4
LEVELS = {"easy": "쉬움", "normal": "보통", "hard": "어려움"}
LEVEL_GUIDE = {
    "easy": "쉬움: 사람이 잘 이을 수 있게 흔하고 이을 말이 많은 낱말을 고른다.",
    "normal": "보통: 이을 말이 적당한 낱말을 고르되 가끔은 까다로운 낱말로 긴장감을 준다.",
    "hard": "어려움: find_words(hard=true)로 이을 말이 가장 적은 낱말을 찾아 사람을 몰아붙인다.",
}
# 비용: 이 호출은 짧아서(한 번 ~500 토큰) 프롬프트 캐시 문턱(도구가 붙으면 실측 ~2천 토큰) 아래 → 캐시용으로 부풀리면 오히려
# 비쌈(실측 한 수 1,125 → 1,390 토큰 환산). 대신 find_words 결과를 처음부터 줘서 보통 호출 1번에 끝낸다.
SYSTEM = ("너는 단톡방 끝말잇기 선수 '소담'이다. 네 차례에 도구로 낱말을 골라 play 로 둔다.\n"
          "- 요청에 find_words 결과(후보와 '이을 말 N개' = 네가 두면 상대가 이을 수 있는 수)가 이미 있다. 그중 골라 바로 "
          "play(word, line) 한다. 더 보고 싶을 때만 find_words·check_word.\n"
          "- 낱말은 반드시 도구가 보여준 사전 낱말만. 지어내지 않는다. play 가 '안 됨' 이면 다른 낱말로 다시 둔다.\n"
          "- line 은 둔 낱말에 붙이는 20자 이내 한마디 (도발·감탄·응원, 링크·명령·질문 없이). 없어도 된다.\n")
TOOLS = [
    {"type": "function", "function": {"name": "find_words", "description": "지금 이을 수 있는 사전 낱말 후보와 각 낱말 뒤에 사람이 이을 말 수",
                                      "parameters": {"type": "object", "properties": {"hard": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "check_word", "description": "이 낱말을 둘 수 있는지 확인",
                                      "parameters": {"type": "object", "properties": {"word": {"type": "string"}},
                                                     "required": ["word"]}}},
    {"type": "function", "function": {"name": "play", "description": "낱말을 둔다",
                                      "parameters": {"type": "object", "properties": {"word": {"type": "string"},
                                                                                      "line": {"type": "string"}},
                                                     "required": ["word"]}}},
]


def follow_count(word: str) -> int:
    """이 낱말 뒤에 이을 수 있는 사전 낱말 수 (적을수록 받는 사람이 힘듦)."""
    return sum(len(games._BY_FIRST.get(ch, ())) for ch in games.starts_for(word))


def why_not(word: str, prev: str, used: set[str]) -> str | None:
    """둘 수 없으면 이유 (코드 판정)."""
    if not games.is_word(word):
        return "사전에 없는 낱말"
    if word[0] not in games.starts_for(prev):
        return f"'{'/'.join(sorted(games.starts_for(prev)))}'(으)로 시작해야 함"
    if word in used:
        return "이미 나온 낱말"
    if not games.can_follow(word):
        return "한방 단어(뒤에 이을 말이 없음)는 안 둠"
    return None


def candidates(prev: str, used: set[str], hard: bool = False, n: int = 15) -> list[tuple[str, int]]:
    out: list[str] = []
    for src in (games._COMMON, games._BY_FIRST):
        out = [w for ch in games.starts_for(prev) for w in src.get(ch, ()) if w not in used and games.can_follow(w)]
        if out:        # 흔한 말 먼저 (어려움도 '무늬'처럼 아는 말로 몰아붙이기 — 과레늄산나트륨 같은 말은 흔한 말이 없을 때만)
            break
    scored = [(w, follow_count(w)) for w in set(out)]
    if hard:
        scored.sort(key=lambda x: x[1])
        return scored[:n]
    random.shuffle(scored)
    return scored[:n]


def _listing(prev: str, used: set[str], hard: bool) -> str:
    return "\n".join(f"{w} (이을 말 {n}개)" for w, n in candidates(prev, used, hard=hard, n=12)) or "후보 없음"


def code_move(prev: str, used: set[str], level: str) -> str | None:
    """LLM 없이 둘 때 (늦거나 실패). 어려움 = 이을 말이 가장 적은 것, 보통 = 까다로운 절반 중 무작위, 쉬움 = 흔한 말 무작위."""
    if level == "easy":
        return games.pick_next(prev, used)
    hard = candidates(prev, used, hard=True, n=40)
    if not hard:
        return None
    return hard[0][0] if level == "hard" else random.choice(hard[:max(1, len(hard) // 2)])[0]


async def _llm_move(svc: Services, chat_id: int, prev: str, used: set[str], level: str) -> tuple[str, str] | None:
    s = await svc.db.get_settings(chat_id)
    messages = [{"role": "system", "content": SYSTEM + LEVEL_GUIDE.get(level, LEVEL_GUIDE["normal"]) + "\n\n"
                 + style_block(s["style"])},
                {"role": "user", "content": f"상대 낱말: {prev}\n이어야 할 첫 글자: {'/'.join(sorted(games.starts_for(prev)))}\n"
                                            f"지금까지 나온 낱말 {len(used)}개\nfind_words 결과:\n"
                                            + _listing(prev, used, level == "hard")}]
    for _ in range(MAX_STEPS):
        msg = await svc.llm.chat(messages, tools=TOOLS, model=svc.cfg.guard_model, max_tokens=400,
                                 purpose="wordchain", chat_id=chat_id)
        calls = [c for c in (msg.tool_calls or []) if c.type == "function"]
        if not calls:
            return None
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            word = str(args.get("word", "")).strip()
            if c.function.name == "find_words":
                result = _listing(prev, used, bool(args.get("hard")) or level == "hard")
            elif c.function.name == "check_word":
                err = why_not(word, prev, used)
                result = f"안 됨: {err}" if err else f"둘 수 있음 (이을 말 {follow_count(word)}개)"
            elif c.function.name == "play":
                err = why_not(word, prev, used)
                if not err:
                    line = filter_output(str(args.get("line", ""))[:40], max_chars=40, allowed_usernames=set())
                    return word, "" if line.startswith("음…") else line
                result = f"안 됨: {err}. 다른 낱말로 다시 play"
            else:
                result = "없는 도구"
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
    return None


async def move(svc: Services, chat_id: int, prev: str, used: set[str], level: str = "normal",
               use_ai: bool = True) -> tuple[str | None, str]:
    """소담이의 한 수 → (낱말 또는 None=못 이음, 한마디). 둘 수 있는 게 없으면 AI 도 부르지 않는다."""
    fallback = code_move(prev, used, level)
    if not fallback or not use_ai or not (svc.llm and svc.llm.enabled):
        return fallback, ""
    try:
        got = await asyncio.wait_for(_llm_move(svc, chat_id, prev, used, level), TIMEOUT)
    except Exception as e:   # 시간 초과·AI 한도·연결 오류 → 코드가 둔다 (게임은 멈추지 않음)
        log.info("wordchain AI move fell back: %r", e)
        got = None
    return got or (fallback, "")
