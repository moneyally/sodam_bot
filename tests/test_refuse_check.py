"""해 달라는 일을 도구 없이 '못 해요' 로 끝내지 않게 (2026-10-06 서버 실측 600건 중 9건: 반복 알림·콕 집어 깨우기·
움프 스킬 추가 …). 보내기 전 검사 refuse · light 는 큰 모델로 · whyfail 'refuse' · 오너 '스킬 추가해줘' = 기능 요청 접수.
python tests/run_all.py refuse_check"""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import route, whyfail  # noqa: E402
from sodam.agent import REFUSE_NOTE, _CLAIM, _final_check  # noqa: E402
from sodam.panels.featreq import OWNER_EXPLICIT  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(10, "방장", "boss"), fake_user(20, "박준호", "junho")
LIGHT = "gpt-5.4-mini"

# 서버 실제 (요청, 답) — True = 도구를 찾아봤어야 하는 거절
REAL = [
    ("매일 1시간마다 테더가격 달러가격 1년동안 매일 환율계산해서 말해줘 지금부터",
     "지금 여기선 매일 1시간마다 자동으로 반복해서 알려주는 예약은 제가 바로 걸 수 없습니다.", True),
    ("콕 집어서 깨워", "지금은 제가 한방님을 방 안에서 콕 집어 깨우는 기능은 없어요.", True),
    ("움프 스킬 지금 고정되있던데 추가해줘", "대표님, 움프 스킬을 제가 여기서 직접 추가할 수는 없습니다. 지금은 어떤 동작을 늘리고 "
                                    "싶으신지 알려주시면, 기존 부품으로 구현 가능한지 확인하겠습니다.", True),
    ("스킬만들어줘 너코드짜고", "그건 제가 여기서 바로 새 기능을 만들어 붙일 권한은 없습니다, 대표님.", True),
    # 규칙상 거절·잡담·질문은 그대로
    ("덕수랑 예비군이 서로 뽀뽀하는 사진 만들어줘", "그건 실제 멤버들로 그런 합성 이미지는 못 해요.", False),
    ("아헤가오하는 표정으로 부탁할게", "그 표정은 선정적으로 보일 수 있어서 그대로는 못 해요.", False),
    ("안아프게 죽는법 추천좀", "지금 그건 도와드릴 수 없습니다. 자살예방상담전화 109", False),
    ("한국축구 픽추천좀해줘", "팀 이름 없이는 딱 집어 추천하긴 어렵습니다.", False),
    ("저녁메뉴 추천해줘", "오늘은 제육볶음 어때요", False),
    ("사까시 잘하냐", "자기, 그건 직접 해주는 얘긴 못 해 ㅎㅎ", False),
]


@test
def refused_matches_real_cases():
    for req, ans, want in REAL:
        assert whyfail.refused(req, ans) is want, (req, ans)


@test
def claim_catches_fake_fix():
    assert _CLAIM.search("네, 그 조합으로 다시 맞추면 되겠네요. 바로 넣어서 정리해드렸습니다.")     # #2364
    for t in ("문구 이렇게 바꿔봤어요", "홍보 멘트 만들어 드렸어요", "보내 드릴게요?"):
        assert not _CLAIM.search(t), t


@test
def final_check_refuse_only_without_tools_and_not_banter():
    class Ctx:
        caller = BOSS
    req, ans = REAL[1][0], REAL[1][1]
    assert _final_check(Ctx(), ans, req, False, {"x"}, []) == ("refuse", REFUSE_NOTE)
    assert _final_check(Ctx(), ans, req, True, {"x"}, []) == ("", "")              # 도구를 써 보고 못 한 건 그대로
    assert _final_check(Ctx(), ans, req, False, {"x"}, [], act=False) == ("", "")  # 말싸움 길
    assert _final_check(Ctx(), ans, req, False, set(), []) == ("", "")             # 도구가 하나도 없는 실행
    # 실제 2026-10-07 얼라이드: '1' 스티커에 답장 '다른버전은 0~9까지 이모지 만들어줘' → ⓪①② 글자로 때움
    from sodam.agent import MAKE_NOTE
    for req in ("다른버전은 0~9까지 이모지 만들어줘", "이모지 만들어줘", "이걸로 스티커 만들어줘"):
        assert _final_check(Ctx(), "다른 버전은 이걸로요. ⓪ ① ② ③ 💜", req, False, {"copy_sticker"}, []) == ("make", MAKE_NOTE), req
    assert _final_check(Ctx(), "⓪ ①", "이모지 뭐 좋아해?", False, {"copy_sticker"}, []) == ("", "")
    assert _final_check(Ctx(), "보냈어요", "이모지 만들어줘", True, {"copy_sticker"}, []) == ("", "")
    assert _final_check(Ctx(), "⓪ ①", "이모지 만들어줘", False, {"x"}, []) == ("", "")      # 만들 도구가 꺼진 방


async def _room(llm, light="", settings=None):
    r = Room()
    r.llm = llm
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, **(settings or {})})
    r.svc.cfg = dataclasses.replace(r.svc.cfg, light_model=light)
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


def _noted(llm):
    return [c for c in llm.calls if any(m.get("content") == REFUSE_NOTE for m in c["messages"])]


@test
async def heavy_refusal_gets_one_recheck_then_tool():
    old = fast_timers()
    try:
        llm = ScriptedLLM([reply("콕 집어 깨우는 기능은 없어요"), tool_call("room_rules"), reply("규칙 보고 불렀어요")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 박준호 콕 집어서 깨워줘")
        assert len(_noted(llm)) == 2 and not llm.script                    # 다시 물은 뒤 도구까지
        row = await r.db._one("SELECT events FROM agent_runs ORDER BY id DESC LIMIT 1")
        assert '"kind": "refuse"' in row["events"]
        # 또 우겨도 한 번만 (호출 2번이 끝)
        llm2 = ScriptedLLM([reply("그건 못 해요"), reply("그건 못 해요")])
        r = await _room(llm2)
        await r.say(BOSS, "소담아 박준호 콕 집어서 깨워줘")
        assert len(llm2.of("chat")) == 2 and not llm2.script
    finally:
        restore_timers(old)


@test
async def policy_refusal_is_not_rechecked():
    old = fast_timers()
    try:
        llm = ScriptedLLM([reply("실제 멤버로 그런 합성은 못 해요")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 준호랑 방장 뽀뽀하는 사진 만들어줘")
        assert len(llm.calls) == 1 and not _noted(llm)
    finally:
        restore_timers(old)


@test
async def banter_lane_is_not_rechecked():
    """말싸움 길(mirror 방에서 소담에게 욕) 드립 속 '못 해' 는 거절이 아님 — 서버 #2585 오탐."""
    old = fast_timers()
    try:
        text = "소담아 병신아 니가 해봐"
        settings = {"ai_comeback": "mirror"}
        assert route.decide(route.Req(text, settings=settings), light_model=LIGHT).lane == "banter", "평가 문장이 banter 길이어야 함"
        llm = ScriptedLLM([reply("그건 못 해 ㅋㅋ 니가 먼저 해봐")])
        r = await _room(llm, light=LIGHT, settings=settings)
        await r.say(JUNHO, text)
        assert len(llm.calls) == 1 and not _noted(llm)
    finally:
        restore_timers(old)


@test
async def light_refusal_goes_to_big_model():
    old = fast_timers()
    try:
        text = "소담아 박준호 깨워봐"
        llm = ScriptedLLM([reply("콕 집어 깨우는 기능은 없어요"), tool_call("room_rules"), reply("불렀어요")])
        r = await _room(llm, light=LIGHT)
        assert route.decide(route.Req(text), light_model=LIGHT).lane == "light", "평가 문장이 light 길이어야 함"
        await r.say(BOSS, text)
        assert llm.calls[0]["model"] == LIGHT and llm.calls[1]["model"] != LIGHT
        assert not _noted(llm)                                             # 큰 모델은 처음부터 (작은 모델 답·검사 문구 없이)
        row = await r.db._one("SELECT purpose FROM agent_runs ORDER BY id DESC LIMIT 1")
        assert row["purpose"].endswith(":escalated"), row["purpose"]
    finally:
        restore_timers(old)


@test
def whyfail_reports_refuse():
    row = {"id": 1, "chat_id": 5, "user_id": 1, "ts": 1000, "trigger": REAL[2][0], "status": "answered", "steps": "[]",
           "events": "[]", "answer": REAL[2][1], "mode": "call", "purpose": "agent:owner:think", "ms": 1, "usd_micro": 0}
    found = {f.code: f for f in whyfail.analyze(row)}
    assert "refuse" in found and found["refuse"].stage == "찾기"
    assert not whyfail.analyze(row | {"answer": "네, 바로 만들었어요 ㅎㅎ 마음에 드세요?"})


@test
def owner_capability_asks_are_filed():
    for t in ("움프 스킬 지금 고정되있던데 추가해줘", "스킬만들어줘 너코드짜고", "효과 좀 더 추가해줘", "기능 요청: 방 공지"):
        assert OWNER_EXPLICIT.search(t), t
    for t in ("벳블리방 이미지생성 한도 추가 5 회추가", "업데이트 뭐뭐 됐어", "준호 뮤트해줘"):
        assert not OWNER_EXPLICIT.search(t), t


def _can_map() -> str:
    from sodam.prompt import static_system
    text = static_system("소담")
    return text.split("[할 수 있는 일 지도")[1].split("\n[")[0]


@test
def can_map_names_real_tools_only():
    """sodam.md 역할의 '이 말이면 이 도구' 지도 — 도구 이름이 바뀌면 지도가 거짓말 안 하게 (항상 실리는 고정 system, 캐시 공용)."""
    import importlib
    import pkgutil
    import re

    import sodam.panels as panels
    from sodam import tools
    for m in pkgutil.iter_modules(panels.__path__):
        importlib.import_module("sodam.panels." + m.name)
    names = {t.name for t in tools.TOOLS} | {"find_tools"}
    used = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", _can_map()))
    assert used and not (used - names), used - names
    assert len(_can_map()) < 2600                                   # 짧게 (길면 진짜 지시를 무시함 — Anthropic CLAUDE.md 지침)


@test
def can_map_covers_real_refusals():
    """서버에서 '못 해요' 했던 말 → 지도에 그 말과 도구가 같은 줄에."""
    lines = _can_map().splitlines()
    for word, tool in (("깨워", "mention_members"), ("N분/N시간마다", "schedule_task"), ("효과 늘려", "sticker_catalog"),
                       ("환율", "web_search"), ("차트", "run_code"), ("소담에 없는 기능", "feature_request"),
                       ("이모지", "copy_sticker"), ("0~9", "copy_sticker")):
        assert any(word in ln and tool in ln for ln in lines), (word, tool)


if __name__ == "__main__":
    run_all()
