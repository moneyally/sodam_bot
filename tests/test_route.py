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
    ("벤츠 샀어", "light"), ("밴드 공연 가자", "light"), ("영상 재밌더라", "light"), ("통화 중이야", "light"),
    ("안내 고마워", "light"), ("그림 잘 그리네", "light"), ("기분 틀어졌어", "light"), ("저장해둘게", "light"),
    # 일·분석·여러 단계 → heavy
    ("박준호 10분 뮤트해줘", "heavy"), ("도배 기준 엄격으로 바꿔줘", "heavy"), ("공지 올려줘", "heavy"),
    ("매일 밤 11시에 요약 올려줘", "heavy"), ("고양이 그림 그려줘", "heavy"), ("내 프사로 움프 만들어줘", "heavy"),
    ("이 사람 왜 계속 도배해?", "heavy"), ("오늘 대화 요약해줘", "heavy"), ("링크 올린 사람 찾아서 경고 줘", "heavy"),
    ("영상 만들어줘 바닷가 노을", "heavy"), ("금지어에 먹튀 추가해줘", "heavy"), ("말투 친근하게 바꿔줘", "heavy"),
    ("이거 영어로 번역해줘", "heavy"), ("음성방 들어와", "heavy"), ("캡차 켜줘", "heavy"),
    ("A랑 B 둘 다 비교해서 뭐가 나은지 판단해줘", "heavy"),
    ("조용히 시켜", "heavy"), ("스팸 막아줘", "heavy"), ("광고 금지해줘", "heavy"), ("ban 해", "heavy"), ("캡챠 활성화", "heavy"),
    ("입장 인사 켜봐", "heavy"), ("쫓아내", "heavy"), ("킥해", "heavy"), ("짭리 벤해줘", "heavy"), ("노래 틀어줘", "heavy"),
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
async def escalation_resets_light_reads_and_reports_done_writes():
    old = fast_timers()
    try:
        # light 가 사람 찾기(tainted 켜짐) 뒤 설정 바꾸기 → heavy 의 설정 바꾸기는 '방 기록 읽음' 으로 막히면 안 됨 (리뷰 재현)
        llm = ScriptedLLM([tool_call("lookup_user", {"who": "박준호"}),
                           tool_call("change_setting", {"key": "warn_ban_at", "value": "3"}, "c2"),
                           tool_call("change_setting", {"key": "warn_ban_at", "value": "3"}, "c3"), reply("바꿨어요")])
        r = await _room(llm)
        await r.join(JUNHO)
        await r.say(BOSS, "소담아 ㅋㅋ 요즘 방 분위기 어때")
        heavy = llm.of("chat", "agent:admin")
        tool_out = [m["content"] for m in heavy[-1]["messages"] if m.get("role") == "tool"]
        assert tool_out and "보안" not in tool_out[-1] and "읽은 답변" not in tool_out[-1], tool_out
        assert (await r.db.get_settings(r.CHAT))["warn_ban_at"] == 3                  # heavy 의 설정 바꾸기가 실제로 됨
        steps = (await _last_run(r))["steps"] or ""
        assert "lookup_user" in steps                                                  # light 가 실제로 찾기를 했음 (경로 확인)

        # light 가 인사(가벼운 쓰기)를 한 뒤 올려 보내면 heavy 에 '이미 한 일' 이 들어감 (두 번 인사 X)
        llm = ScriptedLLM([tool_call("greet_members", {"names": ["박준호"]}),
                           tool_call("change_setting", {"key": "flood_limit", "value": "3"}, "c2"), reply("네")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 ㅋㅋ 준호 왔네")
        heavy = llm.of("chat", "agent:admin")[0]["messages"]
        assert any(m["role"] == "system" and m["content"].startswith("(이미 한 일)") and "greet_members" in m["content"] for m in heavy)
    finally:
        restore_timers(old)


@test
async def light_rounds_capped_and_continuation_goes_heavy():
    old = fast_timers()
    try:
        from sodam import agent
        # 작은 모델이 도구 라운드 LIGHT_MAX_STEPS 번을 다 쓰고도 더 찾으면 = 여러 단계 일 → 큰 모델로 (대충 마무리 X)
        script = [tool_call("room_rules", {}, f"c{i}") for i in range(agent.LIGHT_MAX_STEPS)] + [reply("큰 모델 답")]
        llm = ScriptedLLM(script)
        r = await _room(llm)
        await r.say(JUNHO, "소담아 규칙이 뭐였지")
        assert len(llm.of("chat", "agent:member:light")) == agent.LIGHT_MAX_STEPS
        assert len(llm.of("chat", "agent:member")) == 1 and (await _last_run(r))["purpose"] == "agent:member:escalated"

        # 끼어들기(chime)엔 ask_senior 를 안 붙임
        from sodam import route as rt
        assert rt.decide(rt.Req("안녕", mode="chime"), light_model=LIGHT).lane == "light"

        # 5분 안 큰 모델 일의 이어짐('하나 더') = 처음부터 큰 모델 (작은 모델 → 올려 보내기 두 번 호출 방지)
        llm = ScriptedLLM([reply("공지 올릴게요"), reply("하나 더 올릴게요")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 공지 올려줘")
        await r.say(BOSS, "소담아 하나 더")
        assert len(llm.of("chat", "agent:admin")) == 2 and not llm.of("chat", "agent:admin:light")
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
async def tool_list_is_stable_across_room_settings_for_cache():
    old = fast_timers()
    try:
        keys, tools = [], []
        for settings in ({}, {"image_daily": 0, "games_enabled": False, "sports_enabled": False}):
            llm = ScriptedLLM([reply("네")])
            r = await _room(llm, light="", settings=settings)
            await r.say(BOSS, "소담아 공지 올려줘")
            call = llm.of("chat", "agent:admin")[0]
            keys.append(call["cache_key"])
            tools.append([t["function"]["name"] for t in call["tools"]])
            if settings:
                assert call["allowed"] is not None and "make_image" not in call["allowed"]
                assert "start_game" not in call["allowed"] and "warn_member" in call["allowed"]
            else:
                assert call["allowed"] is None                                   # 전부 부를 수 있으면 제한 없음
        assert tools[0] == tools[1] and keys[0] == keys[1]                        # 방 설정이 달라도 같은 앞부분·같은 캐시 키

        # 꺼진 도구를 모델이 불러도 실행 안 됨 + '꺼져 있는 기능' 안내
        llm = ScriptedLLM([tool_call("make_image", {"prompt": "고양이", "mode": "new"}), reply("이 방은 그림이 꺼져 있어요")])
        r = await _room(llm, light="", settings={"image_daily": 0})
        await r.say(BOSS, "소담아 고양이 그려줘")
        second = llm.of("chat", "agent:admin")[1]["messages"]
        assert any(m.get("role") == "tool" and "꺼져 있는 기능" in m["content"] for m in second)
    finally:
        restore_timers(old)


@test
async def llm_sends_allowed_tools_and_cache_key():
    from types import SimpleNamespace

    from fakes import cfg, make_db

    from sodam.llm import LLM
    db = await make_db()
    llm = LLM(cfg(db.path), db)
    sent = []
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=0), input_tokens=10, output_tokens=2,
                            input_tokens_details=SimpleNamespace(cached_tokens=0))

    async def chat_create(**kw):
        sent.append(("chat", kw))
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="네", tool_calls=None))])

    async def resp_create(**kw):
        sent.append(("resp", kw))
        return SimpleNamespace(usage=usage, output=[], output_text="네")
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
                                 responses=SimpleNamespace(create=resp_create))
    schema = [{"type": "function", "function": {"name": n, "description": "d", "parameters": {"type": "object"}}}
              for n in ("read_chat", "make_image")]
    await llm.chat([{"role": "user", "content": "x"}], tools=schema, purpose="agent:admin", model="gpt-5.4-mini",
                   allowed=["read_chat"], cache_key="agent:abc")
    await llm.think([{"role": "user", "content": "x"}], tools=schema, purpose="agent:admin:think", allowed=["read_chat"],
                    cache_key="agent:abc:think", model="gpt-5.4-mini")
    await llm.chat([{"role": "user", "content": "x"}], tools=schema, purpose="agent:admin", allowed=[])
    await llm.chat([{"role": "user", "content": "x"}], tools=schema, tool_choice="none", purpose="agent:admin",
                   allowed=["read_chat"])
    (_, c1), (_, t1), (_, c2), (_, c3) = sent
    assert c1["model"] == "gpt-5.4-mini" and c1["prompt_cache_key"] == "sodam:agent:abc"
    assert c1["tool_choice"] == {"type": "allowed_tools", "allowed_tools": {
        "mode": "auto", "tools": [{"type": "function", "function": {"name": "read_chat"}}]}}
    assert len(c1["tools"]) == 2                                                  # 목록은 그대로
    assert t1["model"] == "gpt-5.4-mini" and t1["prompt_cache_key"] == "sodam:agent:abc:think"
    assert t1["tool_choice"] == {"type": "allowed_tools", "mode": "auto", "tools": [{"type": "function", "name": "read_chat"}]}
    assert c2["tool_choice"] == "none"                                            # 부를 수 있는 게 없으면 도구 안 씀
    assert c3["tool_choice"] == "none"                                            # 마무리 답은 그대로 none

    # API 가 allowed_tools 를 거절하면: 그 요청은 목록을 줄여 다시, 이후엔 처음부터 목록 줄이기 (AI 답이 멈추면 안 됨)
    import httpx
    from openai import BadRequestError
    sent.clear()
    fails = [1]

    async def picky(**kw):
        if isinstance(kw.get("tool_choice"), dict) and fails:
            fails.pop()
            raise BadRequestError("bad tool_choice", response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                                  body=None)
        return await chat_create(**kw)
    llm.client.chat.completions.create = picky
    msg = await llm.chat([{"role": "user", "content": "x"}], tools=schema, purpose="agent:admin", allowed=["read_chat"])
    assert msg.content == "네" and llm.allowed_off
    # 원격 점검에서 확인할 수 있게 받음/거절을 셈 (앞의 성공 호출 2번 = ok, 이번 거절 1번 = rejected)
    assert await db.counter(llm._today(), 0, "allowed_tools_ok:gpt-5.4-mini") == 2
    assert await db.counter(llm._today(), 0, "allowed_tools_rejected:gpt-5.4") == 1
    retry = [k for _, k in sent if k.get("tool_choice") == "auto"]
    assert [t["function"]["name"] for t in retry[-1]["tools"]] == ["read_chat"]
    sent.clear()
    await llm.chat([{"role": "user", "content": "x"}], tools=schema, purpose="agent:admin", allowed=["read_chat"])
    assert len(sent) == 1 and [t["function"]["name"] for t in sent[0][1]["tools"]] == ["read_chat"]

    # 도구와 무관한 400(길이 초과 등)은 예전 방식으로 바꾸지 않음 (캐시 효과 유지)
    llm.allowed_off = False

    async def too_long(**kw):
        raise BadRequestError("context_length_exceeded", response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                              body=None)
    llm.client.chat.completions.create = too_long
    try:
        await llm.chat([{"role": "user", "content": "x"}], tools=schema, purpose="agent:admin", allowed=["read_chat"])
        raise AssertionError("다시 던져야 함")
    except BadRequestError:
        pass
    assert not llm.allowed_off

    # .env OPENAI_REASONING_EFFORT 가 비어도 gpt-5 + 도구 = 'none' (작은 모델 기본 medium → 400 방지)
    import dataclasses as dc
    llm.cfg = dc.replace(llm.cfg, reasoning_effort="")
    assert llm._extra("gpt-5.4-mini", has_tools=True) == {"reasoning_effort": "none"}
    assert llm._extra("gpt-5.4-mini", has_tools=False) == {}


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
