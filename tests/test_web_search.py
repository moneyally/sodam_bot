"""🌐 소담 웹 검색을 클로드처럼 (sodam/llm.py web_search·_search·search_actions, agent._final_check gave_up, tools.t_sports 힌트).
2026-10-13 #4595 오너 '검색해 주고 분석 자료 토대로 다시 말해 줘' → 작은 모델이 한 번 찾고 'KOVO 에서 직접 확인하세요' → 소담 포기.
OpenAI 문서: 같은 검색 도구가 추론 모델이면 페이지 열기(open_page)·페이지 안 찾기까지 하고 더 찾을지 스스로 정함."""
import dataclasses
from types import SimpleNamespace

import httpx
from fakes import cfg, make_db, runner
from openai import BadRequestError

from sodam import agent, costs, tools
from sodam.llm import LLM, search_actions

test, run_all = runner()


def usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=100, total_tokens=1100,
                           input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0))


def resp(text="흥국생명 10/10 3-1 승", actions=()):
    out = [SimpleNamespace(type="web_search_call", action=a) for a in actions]
    return SimpleNamespace(output=out, output_text=text, usage=usage())


def act(kind="search", urls=()):
    return SimpleNamespace(type=kind, sources=[{"type": "url", "url": u} for u in urls])


class Client:
    def __init__(self, answers, fail=()):
        self.kw, self.answers, self.fail = [], list(answers), list(fail)

        async def create(**kw):
            self.kw.append(kw)
            if self.fail:
                raise self.fail.pop(0)
            return self.answers.pop(0)
        self.responses = SimpleNamespace(create=create)


async def llm_with(answers, fail=(), model="gpt-6-sol"):
    db = await make_db()
    llm = LLM(dataclasses.replace(cfg(db.path), model=model), db)
    llm.client = Client(answers, fail)
    return llm, db


@test
async def search_uses_main_reasoning_model_with_pages_and_sources():
    llm, _ = await llm_with([resp()])
    await llm.web_search("흥국생명 최근 경기 결과")
    kw = llm.client.kw[0]
    assert kw["model"] == "gpt-6-sol", "작은 모델(guard)이 아니라 큰 모델 — 추론 모델이어야 페이지를 열어 봄"
    assert kw["reasoning"] == {"effort": "low"}
    assert kw["include"] == ["web_search_call.action.sources"]
    tool = kw["tools"][0]
    assert tool["type"] == "web_search" and tool["search_context_size"] == "high"
    assert tool["user_location"]["country"] == "KR" and tool["user_location"]["timezone"] == "Asia/Seoul"
    assert kw["max_output_tokens"] >= 4000                     # 생각 토큰까지 (800 이면 생각만 하다 잘림)
    assert "여러 번" in kw["instructions"] and "열어서" in kw["instructions"] and "직접 확인하세요" in kw["instructions"]


@test
async def sources_listed_and_each_search_billed():
    a = [act("search", ["https://www.kovo.co.kr/a", "https://sports.news.naver.com/b"]),
         act("open_page", ["https://www.kovo.co.kr/game/1"]), act("search", ["https://namu.wiki/w/x"])]
    llm, db = await llm_with([resp(actions=a)])
    out = await llm.web_search("흥국생명 IBK 상대 전적", -100)
    assert out.endswith("(찾아본 곳: kovo.co.kr, sports.news.naver.com, namu.wiki)"), out
    day = llm._today()
    assert await db.counter(day, 0, "web_search_calls") == 2              # 페이지 열기는 검색 요금 아님
    tok = costs.usd_micro("gpt-6-sol", 1000, 0, 100)
    assert await db.counter(day, -100, costs.ROOM_USD) == tok + 2 * round(costs.WEB_SEARCH_PER_CALL * costs.MICRO)
    assert search_actions(resp()) == {"searches": 0, "domains": []}
    llm2, _ = await llm_with([resp(text="", actions=a)])
    assert await llm2.web_search("x") == ""                               # 답 없으면 출처만 붙이지 않음


@test
async def rejected_options_fall_back_to_plain_search():
    bad = BadRequestError("Unknown parameter: include", response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                          body={"error": {"code": "unknown_parameter", "message": "Unknown parameter: include"}})
    llm, _ = await llm_with([resp()], fail=[bad])
    assert await llm.web_search("날씨") == "흥국생명 10/10 3-1 승"
    first, second = llm.client.kw
    assert "include" in first and "include" not in second and "user_location" not in second["tools"][0]
    assert llm.search_opts_off and second["model"] == "gpt-6-sol"


def ctx():
    return SimpleNamespace(caller=SimpleNamespace(first_name="자이", last_name=None, username=None), room_people=None)


@test
async def one_search_then_giving_up_is_asked_again():
    allowed = {"web_search", "sports"}
    text = "자기, 다시 찾아봤는데 오늘 경기의 최근 전력·선발 자료가 확인되지 않았어."
    kind, note = agent._final_check(ctx(), text, "검색해주고 다시 말해줘", True, allowed, [], True, searches=1)
    assert kind == "gave_up" and "web_search" in note and "낱말" in note
    assert agent._final_check(ctx(), "KOVO 에서 직접 확인해 보세요", "x", True, allowed, [], True, searches=1)[0] == "gave_up"
    assert agent._final_check(ctx(), text, "x", True, allowed, [], True, searches=2) == ("", "")   # 이미 다시 찾음
    assert agent._final_check(ctx(), text, "x", True, {"sports"}, [], True, searches=1) == ("", "")   # 검색 꺼진 방
    assert agent._final_check(ctx(), "흥국생명이 10일 3-1 로 이겼어.", "x", True, allowed, [], True, searches=1) == ("", "")


@test
async def sports_analysis_without_records_points_to_search():
    from sodam.sports import analysis
    from sodam.sports import ui as sports_ui
    old_find, old_an = sports_ui.UI.find_games, analysis.analyze

    async def find_games(self, q, translate=None):
        return ["경기"]

    async def analyze(svc, g):
        return "🧠 흥국생명 vs IBK기업은행\n(이 대회는 팀 기록을 더 못 가져옴)"
    sports_ui.UI.find_games, analysis.analyze = find_games, analyze
    try:
        c = SimpleNamespace(svc=SimpleNamespace(sports=SimpleNamespace(feed=None)), chat_id=-100, caller=SimpleNamespace(id=1))
        out = await tools.t_sports(c, {"action": "analysis", "query": "흥국생명 IBK기업은행"})
    finally:
        sports_ui.UI.find_games, analysis.analyze = old_find, old_an
    assert "web_search" in out and "최근 경기 결과" in out, out


@test
async def agent_searches_again_after_giving_up_once():
    from fake_llm import Room, ScriptedLLM, fast_timers, restore_timers, tool_call
    from fakes import fake_user
    boss = fake_user(1, "자이", "boss")
    old = fast_timers()
    try:
        r = Room()
        r.llm = ScriptedLLM([tool_call("web_search", {"query": "흥국생명 IBK기업은행 2026 10월 13일 핸디캡 최근 전력"}),
                             "오늘 경기의 최근 전력 자료가 확인되지 않았어.",
                             tool_call("web_search", {"query": "흥국생명 최근 경기 결과"}),
                             "흥국생명은 10일 3-1 로 이겼어."])
        searched = []

        async def web_search(query, chat_id=None):
            searched.append(query)
            return "KOVO 에서 직접 확인하세요" if len(searched) == 1 else "흥국생명 10/10 3-1 승"
        r.llm.web_search = web_search
        await r.open(admins=(boss.id,), settings={"captcha_enabled": False})
        await r.join(boss)
        await r.say(boss, "소담아 검색해주고 분석자료 토대로 다시 말해줘")
        noted = [c for c in r.llm.of("chat") if any(m.get("content") == agent.GAVE_UP_NOTE for m in c["messages"])]
        assert len(searched) == 2 and noted and not r.llm.script, (searched, r.llm.script)
    finally:
        restore_timers(old)


if __name__ == "__main__":
    run_all()
