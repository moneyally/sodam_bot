"""🧭 하이브리드 라우팅 (sodam/route.py + agent._attempt): 코드 판정 평가표 · light 작은 모델 · 쓰기 도구/ask_senior 면 heavy 로 한 번 ·
오너 방 모드 · 끄기. python tests/run_all.py route"""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import route  # noqa: E402
from sodam.permissions import Role  # noqa: E402

test, run_all = runner()
BOSS, JUNHO, OWNER = fake_user(10, "방장", "boss"), fake_user(20, "박준호", "junho"), fake_user(1, "오너", "owner")
LIGHT = "gpt-5.4-mini"

# 실제 방 말투 평가표 (멤버, 그룹방, 설정 없음): 기대 길
TABLE = [
    # 잡담·인사·장난·짧은 질문 → light
    ("안녕~ 오늘 날씨 좋다", "light"), ("밥 먹고 왔어", "light"), ("ㅋㅋㅋㅋ 개웃기네", "light"), ("뭐해?", "light"),
    ("오늘 기분 어때", "light"), ("나 심심해", "light"), ("잘자~", "light"), ("좋은 아침", "light"),
    ("너 몇 살이야?", "light"), ("소담이 귀엽다", "light"), ("오늘 불금이다", "light"), ("배고프다", "light"),
    ("점심 뭐 먹지", "light"), ("ㅎㅇ", "light"), ("반가워요 대표님들", "light"), ("오늘 비트코인 얼마야?", "light"),
    ("내 포인트 몇 점이야", "light"), ("이 방 규칙이 뭐야?", "light"), ("오늘 경기 결과 알려줘", "light"),
    ("너 누가 만들었어?", "light"), ("사랑해", "light"), ("피곤하다", "light"), ("ㅇㅈ", "light"),
    ("오늘 몇 명 들어왔어?", "light"), ("노래 추천 좀", "light"),
    # 일·분석·여러 단계 → heavy
    ("박준호 10분 뮤트해줘", "heavy"), ("도배 기준 엄격으로 바꿔줘", "heavy"), ("공지 올려줘", "heavy"),
    ("매일 밤 11시에 요약 올려줘", "heavy"), ("고양이 그림 그려줘", "heavy"), ("내 프사로 움프 만들어줘", "heavy"),
    ("이 사람 왜 계속 도배해?", "heavy"), ("오늘 대화 요약해줘", "heavy"), ("링크 올린 사람 찾아서 경고 줘", "heavy"),
    ("영상 만들어줘 바닷가 노을", "heavy"), ("금지어에 먹튀 추가해줘", "heavy"), ("말투 친근하게 바꿔줘", "heavy"),
    ("이거 영어로 번역해줘", "heavy"), ("음성방 들어와", "heavy"), ("캡차 켜줘", "heavy"),
    ("A랑 B 둘 다 비교해서 뭐가 나은지 판단해줘", "heavy"),
    ("우리 방 이벤트를 다음 주에 하려고 하는데 참여율 높이려면 어떤 방식이 좋을지, 보상은 얼마가 적당한지, "
     "공지는 언제 올리는 게 좋은지 한 번에 정리해서 알려줄 수 있을까? 작년엔 사람이 적게 왔거든 그래서 고민이야 진짜로",
     "heavy"),
]


@test
def evaluation_table():
    wrong = []
    for text, want in TABLE:
        got = route.decide(route.Req(text), light_model=LIGHT).lane
        if got != want:
            wrong.append((text, want, got))
    assert len(TABLE) >= 40
    assert not wrong, wrong


@test
def signals_media_dm_banter_modes_and_off():
    d = lambda text="안녕", **kw: route.decide(route.Req(text, **{k: v for k, v in kw.items() if k != "m"}),  # noqa: E731
                                              mode=kw.get("m", "hybrid"), light_model=LIGHT)
    assert d(has_media=True).lane == "heavy"                                   # 사진·영상 = heavy
    assert d(in_dm=True, role=Role.ADMIN).lane == "heavy"                      # 1:1 관리자·오너 = 운영
    assert d(in_dm=True, role=Role.MEMBER).lane == "light"
    assert d(role=Role.ADMIN).lane == "light"                                  # 관리자 잡담도 작은 모델 (추론 X)
    mirror = {"ai_comeback": "mirror"}
    assert d("소담이 병신아", settings=mirror).lane == "banter"                 # 욕 받아치기 방 = 큰 모델 말맛
    assert d("소담이 병신아", settings={}).lane == "light"                      # 받아치기 꺼진 방 = 그냥 잡담
    assert d("소담이 병신아", settings=mirror, m="saver").lane == "light"       # 절약 모드
    assert d("섹스", settings={"ai_spicy": 1}).lane == "banter"
    assert d("안녕", m="best").lane == "heavy"                                 # 최고 모드
    assert route.decide(route.Req("안녕"), light_model="").lane == "heavy"     # 작은 모델 비면 끔
    assert d("안녕", mode="chime").lane == "light"                             # 끼어들기


async def _room(llm, *, think="off", light=LIGHT, settings=None):
    r = Room()
    r.llm = llm
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, **(settings or {})})
    r.svc.cfg = dataclasses.replace(r.svc.cfg, agent_think=think, light_model=light)
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


async def _last_run(r):
    return await r.db._one("SELECT purpose, status, steps FROM agent_runs ORDER BY id DESC LIMIT 1")


@test
async def chat_goes_light_with_small_model_and_escalate_tool():
    old = fast_timers()
    try:
        llm = ScriptedLLM([reply("ㅎㅇㅎㅇ 반가워요!")])
        r = await _room(llm)
        await r.say(JUNHO, "소담아 안녕")
        calls = llm.of("chat", "agent:member:light")
        assert len(calls) == 1 and calls[0]["model"] == LIGHT
        assert route.ESCALATE_TOOL in {t["function"]["name"] for t in calls[0]["tools"]}
        assert (await _last_run(r))["purpose"] == "agent:member:light"
    finally:
        restore_timers(old)


@test
async def admin_chat_is_light_without_thinking():
    old = fast_timers()
    try:
        llm = ScriptedLLM([reply("ㅋㅋ 네 대표님")])
        llm.think = None   # 추론을 부르면 터짐
        r = await _room(llm, think="auto")
        await r.say(BOSS, "소담아 ㅋㅋㅋ 오늘 불금이다")
        assert len(llm.of("chat", "agent:admin:light")) == 1
        assert (await _last_run(r))["purpose"] == "agent:admin:light"
    finally:
        restore_timers(old)


@test
async def write_tool_in_light_is_not_run_and_escalates_once():
    old = fast_timers()
    try:
        # light 가 설정 바꾸기를 부름 → 실행 안 하고 heavy 로 처음부터. heavy 는 그냥 답 (설정은 그대로여야 함)
        llm = ScriptedLLM([tool_call("change_setting", {"key": "flood_limit", "value": "3"}), reply("어떤 값으로 바꿀까요?")])
        r = await _room(llm)
        before = (await r.db.get_settings(r.CHAT)).get("flood_limit")
        await r.say(BOSS, "소담아 ㅋㅋ 오늘 좀 조용하네")
        assert len(llm.of("chat", "agent:admin:light")) == 1
        heavy = llm.of("chat", "agent:admin")
        assert len(heavy) == 1 and heavy[0].get("model") is None                   # 큰 모델(cfg.model)
        assert route.ESCALATE_TOOL not in {t["function"]["name"] for t in heavy[0]["tools"] or []}
        assert not any(m.get("role") == "tool" for m in heavy[0]["messages"])       # light 의 도구 흔적 없이 처음부터
        assert (await r.db.get_settings(r.CHAT)).get("flood_limit") == before     # light 가 부른 도구는 실행 안 됨
        run = await _last_run(r)
        assert run["purpose"] == "agent:admin:escalated" and route.ESCALATE_TOOL in (run["steps"] or "")
    finally:
        restore_timers(old)


@test
async def escalation_after_read_round_starts_clean():
    old = fast_timers()
    try:
        # light: 읽기 도구 한 라운드 → 다음 라운드에 쓰기 도구 → heavy 는 light 의 도구 기록 없이 처음부터
        llm = ScriptedLLM([tool_call("room_rules", {}), tool_call("change_setting", {"key": "flood_limit", "value": "3"}),
                           reply("큰 모델 답")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 ㅋㅋ 요즘 방 분위기 어때")
        heavy = llm.of("chat", "agent:admin")
        assert len(heavy) == 1
        assert not any(m.get("role") in ("tool", "assistant") for m in heavy[0]["messages"])
        assert (await _last_run(r))["purpose"] == "agent:admin:escalated"
    finally:
        restore_timers(old)


@test
async def ask_senior_escalates_and_read_tool_stays_light():
    old = fast_timers()
    try:
        llm = ScriptedLLM([tool_call(route.ESCALATE_TOOL, {"reason": "애매함"}), reply("큰 모델 답")])
        r = await _room(llm)
        await r.say(JUNHO, "소담아 그거 어때")
        assert (await _last_run(r))["purpose"] == "agent:member:escalated"
        assert r.bot.texts()[-1].endswith("큰 모델 답") if hasattr(r.bot, "texts") else True

        llm2 = ScriptedLLM([tool_call("room_rules", {}), reply("규칙은 이래요")])
        r2 = await _room(llm2)
        await r2.say(JUNHO, "소담아 이 방 규칙 뭐야")
        assert len(llm2.of("chat", "agent:member:light")) == 2                     # 읽기 도구는 light 에서 그대로
        run = await _last_run(r2)
        assert run["purpose"] == "agent:member:light" and "room_rules" in (run["steps"] or "")
    finally:
        restore_timers(old)


@test
async def task_banter_best_and_off_paths():
    old = fast_timers()
    try:
        llm = ScriptedLLM([reply("네")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 공지 올려줘")                                    # 일 → heavy
        assert len(llm.of("chat", "agent:admin")) == 1 and llm.of("chat", "agent:admin")[0].get("model") is None

        llm = ScriptedLLM([reply("니가 더 병신")])
        r = await _room(llm, settings={"ai_comeback": "mirror"})
        await r.say(JUNHO, "소담이 병신아")
        banter = llm.of("chat", "agent:member:banter")
        assert len(banter) == 1 and banter[0].get("model") is None                  # 말싸움 = 큰 모델
        assert (await _last_run(r))["purpose"] == "agent:member:banter"

        llm = ScriptedLLM([reply("안녕")])
        r = await _room(llm)
        await r.db.set_state(r.CHAT, route.ROUTE_KEY, "best")                       # 오너 '최고' = 전부 큰 모델
        await r.say(JUNHO, "소담아 안녕")
        assert len(llm.of("chat", "agent:member")) == 1

        llm = ScriptedLLM([reply("안녕")])
        r = await _room(llm, light="")                                              # 기능 끔 = 예전 그대로
        await r.say(JUNHO, "소담아 안녕")
        assert len(llm.of("chat", "agent:member")) == 1 and not llm.of("chat", "agent:member:light")
    finally:
        restore_timers(old)


@test
async def owner_command_sets_room_mode_admin_cannot():
    old = fast_timers()
    try:
        r = await _room(ScriptedLLM([]))
        r.svc.perms.owner_ids.add(OWNER.id)
        await r.join(OWNER)
        await r.say(BOSS, ".AI모델 절약")                                           # 방 관리자 = 오너 명령 아님
        assert await route.room_mode(r.db, r.CHAT) == "hybrid"
        await r.say(OWNER, ".AI모델 절약")
        assert await route.room_mode(r.db, r.CHAT) == "saver"
        await r.say(OWNER, ".AI모델 나눠")
        assert await route.room_mode(r.db, r.CHAT) == "hybrid" and await r.db.get_state(r.CHAT, route.ROUTE_KEY) is None
    finally:
        restore_timers(old)


@test
def diag_ai_config_never_reads_secrets():
    import tempfile

    from sodam import diag
    f = Path(tempfile.mktemp())
    f.write_text("OPENAI_API_KEY=sk-secret123456789012345\nTELEGRAM_BOT_TOKEN=1:abc\nAGENT_LIGHT_MODEL=gpt-5.4-mini\n"
                 "OPENAI_CACHE_RETENTION=\n")
    out = diag.ai_config(f)
    assert out["AGENT_LIGHT_MODEL"] == "gpt-5.4-mini" and out["OPENAI_CACHE_RETENTION"] == "(기본)"
    assert "secret" not in str(out) and "abc" not in str(out) and "OPENAI_API_KEY" not in out
    assert diag.ai_config(Path("/nonexistent/.env")) is None


if __name__ == "__main__":
    run_all()
