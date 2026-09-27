"""도구 고르기 평가 + 토큰 측정: 실제 OpenAI 로 관리자·멤버 요청을 돌려 '맞는 도구를 골랐나'와 답 1번당 입력 토큰을 잰다.
프롬프트를 줄이거나 나눌 때 전후 비교용 (정답률은 그대로, 토큰만 줄어야 함).
    python tools/ai_eval_tools.py            # 전체
    python tools/ai_eval_tools.py 3 7        # 3·7번만
    python tools/ai_eval_tools.py --repeat 2 # 같은 상황을 여러 번 (모델 답이 매번 달라서)
    python tools/ai_eval_tools.py --route    # AI 호출 없이: 어떤 요청이 '생각하는 에이전트'로 가는지만 (AGENT_THINK=auto 기준)
    AGENT_THINK=auto python tools/ai_eval_tools.py   # 생각하는 에이전트 켜고 비교 (.env 값보다 환경변수가 우선)
그림 도구는 실제 그림을 만들지 않는다 (LLM.image 를 가짜로 — 비용 0).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import ai_live as L  # noqa: E402
from fake_llm import fast_timers, restore_timers  # noqa: E402
from fakes import close_open_dbs  # noqa: E402

from sodam import agent, costs, knowledge  # noqa: E402
from sodam.llm import LLM  # noqa: E402

USED: list[str] = []
ARGS: list[dict] = []
TOK = {"calls": 0, "in": 0, "cached": 0, "out": 0, "micro": 0}
_real_exec, _real_record = agent.execute, LLM._record


async def _exec(name, raw, ctx):
    USED.append(name)
    try:
        ARGS.append(json.loads(raw or "{}"))
    except ValueError:
        ARGS.append({})
    return await _real_exec(name, raw, ctx)


async def _record(self, usage, chat_id=None, purpose="misc", model="", *rest, **kw):
    if usage and purpose.startswith("agent"):   # chat.completions(prompt_tokens) · Responses(input_tokens) 둘 다
        p = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0
        d = getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None)
        cached = (getattr(d, "cached_tokens", 0) or 0) if d else 0
        out = (getattr(usage, "total_tokens", 0) or 0) - p
        TOK["calls"] += 1
        TOK["in"] += p
        TOK["cached"] += cached
        TOK["out"] += out
        TOK["micro"] += costs.usd_micro(model or self.cfg.model, p, cached, out, self.cfg.model)
    return await _real_record(self, usage, chat_id, purpose, model, *rest, **kw)


async def _fake_image(self, prompt, source=None, chat_id=None):
    return b"\x89PNG fake"


NONE = "(도구 없음)"
SANCTION = {"warn_member", "mute_member", "ban_member", "unmute_member"}
# (말한 사람, 요청, 맞는 도구들(하나라도 쓰면 통과) 또는 NONE, 쓰면 안 되는 도구)
CASES = [
    (L.BOSS, "소담아 매일 밤 11시에 오늘 대화 요약해서 이 방에 올려줘", {"schedule_task"}, set()),
    (L.BOSS, "소담아 내일 오전 9시에 '10시 회의 있어요' 공지 예약해줘", {"schedule_task"}, set()),
    (L.BOSS, "소담아 누가 '먹튀' 라고 쓰면 나한테 1:1로 바로 알려줘", {"alert_rule"}, set()),
    (L.BOSS, "소담아 박준호 도배해서 10분 뮤트해줘", {"mute_member"}, set()),
    (L.BOSS, "소담아 이수진 경고 한 번 줘 욕했어", {"warn_member"}, set()),
    (L.BOSS, "소담아 이 방 말투 친근하게 바꿔줘", {"change_setting"}, SANCTION),
    (L.BOSS, "소담아 게임 3시간 넘게 하는 사람 있으면 알려줘", {"game_alert"}, SANCTION),
    (L.BOSS, "소담아 김민지 어떤 사람이야?", {"member_info", "member_timeline", "analyze_member"}, SANCTION),
    (L.BOSS, "소담아 끝말잇기 하자", {"start_game"}, set()),
    (L.BOSS, "소담아 박준호 요즘 왜 계속 문제야?", {"analyze_member", "member_timeline"}, SANCTION),
    (L.BOSS, "소담아 김민지 어떤 활동 해왔는지 기록 보여줘", {"member_timeline", "analyze_member", "member_info"}, SANCTION),
    (L.BOSS, "소담아 오늘 왜 사람이 많이 나갔어?", {"room_changes"}, SANCTION),
    (L.JUNHO, "소담아 안녕~ 오늘 날씨 좋다", NONE, SANCTION),
    (L.JUNHO, "소담아 점심 뭐 먹을까 추천해줘", NONE, SANCTION),
    (L.JUNHO, "소담아 이수진 밴해줘 짜증나", NONE, SANCTION),
    (L.JUNHO, "소담아 나한테는 반말로 해줘", {"set_my_style"}, SANCTION),
    (L.JUNHO, "소담아 오늘 비트코인 시세 검색해서 알려줘", {"web_search"}, SANCTION),
    (L.JUNHO, "소담아 귀여운 고양이 그림 그려줘", {"make_image"}, SANCTION),
    (L.JUNHO, "소담아 요즘 이 방에서 제일 말 많은 사람 누구야?", {"chat_stats", "room_members"}, SANCTION),
    (L.JUNHO, "소담아 어제 누가 정기모임 얘기했었지? 찾아줘", {"search_chat", "read_chat"}, SANCTION),
]

# ── 🎞️ 사건 재현 · 🔬 설정 시뮬레이터 (sodam/replay.py) — 이 블록만 추가 ──────────
# 시뮬레이션 요청에 실제 설정 변경(change_setting)을 하면 실패 (미리 보기만 해야 함)
NO_CHANGE = SANCTION | {"change_setting"}
CASES += [
    (L.BOSS, "소담아 아까 11시쯤 박준호랑 이수진 싸운 거 무슨 일이었는지 정리해줘", {"build_incident_case"}, SANCTION),
    (L.BOSS, "소담아 링크 차단 켜면 오늘 몇 개나 걸렸을까?", {"simulate_setting_change"}, NO_CHANGE),
    (L.BOSS, "소담아 금지어에 '먹튀' 넣으면 어제부터 몇 명이 걸렸겠어?", {"simulate_setting_change"}, NO_CHANGE),
    (L.BOSS, "소담아 도배 기준 엄격으로 바꾸면 누가 걸렸을지 미리 보여줘", {"simulate_setting_change"}, NO_CHANGE),
    (L.BOSS, "소담아 신입 링크 금지를 72시간으로 늘리면 영향 얼마나 있어?", {"simulate_setting_change"}, NO_CHANGE),
]
# ── (사건 재현·시뮬레이터 블록 끝) ──────────────────────────────────────
# ── 📥 운영 인박스 · 💸 비용 예측 (sodam/opsdesk.py) ─────────────────────────────
# 방 안 장면만 (run_case 가 그룹방). owner_command_center 는 오너 1:1 전용이라 여기선 못 잼.
OPS = {"ops_inbox", "cost_forecast"}
CASES += [
    (L.BOSS, "소담아 처리할 일 보여줘", {"ops_inbox"}, SANCTION),
    (L.BOSS, "소담아 이 방에 밀린 거나 아직 확인 안 한 알림 있어?", {"ops_inbox"}, SANCTION),
    (L.BOSS, "소담아 이번 달 AI 비용 얼마 나올 것 같아?", {"cost_forecast"}, SANCTION),
    (L.BOSS, "소담아 이 속도면 이번 달 AI 사용 한도 넘을까?", {"cost_forecast"}, SANCTION),
    (L.BOSS, "소담아 최근 3시간 대화 요약해줘", {"read_chat"}, SANCTION | OPS),
]
# ── (운영 인박스 끝) ──────────────────────────────────────────────────────────
# ── 🧠 여러 단계·애매한 요청 (생각하는 에이전트 AGENT_THINK 비교용) ─────────────────────
# want 가 Steps 면 묶음마다 하나 이상 (전부 해야 통과). ASK = 제재·변경 없이 되물어야. 5번째 = 먼저 할 준비(async fn(room)),
# 6번째 = 도구 인자 확인 fn(ARGS 목록) → bool.
ASK = "(되묻기)"


class Steps(tuple):
    pass


def _said(*lines):
    async def setup(r):
        for who, text in lines:
            await L.say(r, who, text)
    return setup


async def _fee_doc(r):
    await knowledge.add_document(r.db, L.Room.CHAT, "정기모임 안내",
                                 "정기모임은 매달 셋째 주 목요일 저녁 7시, 강남역 모임공간. 회비는 1회 2만원.", "text", L.BOSS.id)


def _names(*want):
    return lambda args: any(all(w in " ".join(map(str, a.get("names") or [a.get("name", "")])) for w in want) for a in args)


NO_MUTE_BAN = {"mute_member", "ban_member", "unmute_member"}
HARD_FROM = len(CASES)
CASES += [
    (L.BOSS, "소담아 방금 먹튀 사이트 얘기한 사람 찾아서 경고 줘", {"warn_member"}, NO_MUTE_BAN,
     _said((L.SUJIN, "오늘 날씨 좋네요"), (L.JUNHO, "여기 먹튀 사이트 알려드림 ㅋㅋ 돈 금방 벌어요")), _names("박준호")),
    (L.BOSS, "소담아 이수진이랑 박준호 둘 다 10분 뮤트해줘 싸웠어", {"mute_member"}, {"warn_member", "ban_member"},
     None, _names("이수진", "박준호")),
    (L.BOSS, "소담아 매일 밤 11시에 대화 요약 올리고, 누가 '입금' 얘기하면 나한테 1:1로 알려줘",
     Steps(({"schedule_task"}, {"alert_rule"})), SANCTION),
    (L.BOSS, "소담아 이 방 말투 친근하게 바꾸고 김민지 대표님한테 인사드려", Steps(({"change_setting"}, {"greet_members"})), SANCTION),
    (L.BOSS, "소담아 자료에서 회비 확인해서 내일 오전 10시에 회비 안내 공지 예약해줘",
     Steps(({"search_knowledge", "room_rules"}, {"schedule_task"})), SANCTION, _fee_doc,
     lambda args: any("2만" in str(a.get("text", "")) or "20,000" in str(a.get("text", "")) for a in args)),
    (L.BOSS, "소담아 오늘 왜 사람 많이 나갔는지 보고, 링크 차단 켜면 영향 있을지도 알려줘",
     Steps(({"room_changes"}, {"simulate_setting_change"})), NO_CHANGE),
    (L.BOSS, "소담아 도배 기준 엄격으로 하면 누가 걸릴지 보고 괜찮으면 바꿔줘", {"simulate_setting_change"}, NO_CHANGE),
    (L.JUNHO, "소담아 이수진 경고 먹이고 나한테는 반말로 해줘", {"set_my_style"}, SANCTION),
    (L.BOSS, "소담아 걔 좀 내보내", ASK, SANCTION),
]
# ── (여러 단계 끝) ──────────────────────────────────────────────────────────


def _passed(want, used: set, forbid, out: list[str]) -> bool:
    if used & forbid:
        return False
    if want == NONE:
        return not used - {"search_knowledge", "room_rules"}
    if want == ASK:
        return any("?" in o or "누구" in o for o in out)
    if isinstance(want, Steps):
        return all(used & step for step in want)
    return bool(used & want)


async def run_case(i: int, who, text, want, forbid, setup=None, check=None) -> bool:
    r = await L.room({"captcha_enabled": False})
    if setup:
        await setup(r)
    USED.clear()
    ARGS.clear()
    out = await L.say(r, who, text)
    ok = _passed(want, set(USED), forbid, out) and (check is None or check(ARGS))
    shown = want if isinstance(want, str) else " → ".join("/".join(sorted(s)) for s in want) if isinstance(want, Steps) \
        else "/".join(sorted(want))
    print(f"  {'✅' if ok else '❌'} {i}. 도구: {', '.join(USED) or NONE}" + ("" if ok else f"  (기대: {shown})"))
    if not out and want == NONE:
        print("     (답 없음)")
    return ok


def route_report() -> int:
    """AI 호출 없이: AGENT_THINK=auto 면 어떤 요청이 생각하는 에이전트로 가는지 (여러 단계 블록은 전부 가야 함)."""
    hits = [agent.wants_thinking("auto", c[1], "call") for c in CASES]
    for i, (c, hit) in enumerate(zip(CASES, hits), 1):
        print(f"  {'🧠' if hit else '  '} {i}. {c[1]}")
    easy, hard = hits[:HARD_FROM], hits[HARD_FROM:]
    print(f"\n생각으로 감: 기존 {sum(easy)}/{len(easy)} · 여러 단계 {sum(hard)}/{len(hard)}")
    return 0


async def main() -> int:
    args = sys.argv[1:]
    if "--route" in args:
        return route_report()
    repeat = int(args[args.index("--repeat") + 1]) if "--repeat" in args else 1
    only = {int(a) for a in args if a.isdecimal() and (not repeat or a != str(repeat) or "--repeat" not in args)}
    agent.execute, LLM._record, LLM.image = _exec, _record, _fake_image
    old = fast_timers()
    t0, results = time.time(), []
    try:
        for _ in range(repeat):
            for i, case in enumerate(CASES, 1):
                if only and i not in only:
                    continue
                try:
                    results.append(await run_case(i, *case))
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    print(f"  ❌ {i}. 예외 {type(e).__name__}")
                    results.append(False)
    finally:
        agent.execute, LLM._record = _real_exec, _real_record
        restore_timers(old)
        await close_open_dbs()
    n = len(results)
    ans = max(1, n)
    print(f"\n정답 {sum(results)}/{n} · {time.time() - t0:.0f}초")
    print(f"AI 호출 {TOK['calls']}번 · 입력 {TOK['in']:,} (캐시 {TOK['cached']:,}) · 출력 {TOK['out']:,}")
    print(f"요청 1번당 입력 {TOK['in'] // ans:,} 토큰 · 호출 1번당 {TOK['in'] // max(1, TOK['calls']):,} · "
          f"요금 ${TOK['micro'] / costs.MICRO / ans:.4f}/요청 (합계 ${TOK['micro'] / costs.MICRO:.3f})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
