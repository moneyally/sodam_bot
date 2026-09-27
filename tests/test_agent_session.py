"""Codex 식 실행 세션: 도구 결과 앞+뒤 자르기(TruncationPolicy) · 실행 중 이어 보낸 말 넣기(inject_if_running).
python tests/run_all.py agent_session"""
import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import tools, util  # noqa: E402

test, run_all = runner()
BOSS, JUNHO = fake_user(1, "방장", "boss"), fake_user(20, "박준호", "junho")


async def _room(script, think="off"):
    r = Room()
    r.llm = ScriptedLLM(script)
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    r.svc.cfg = dataclasses.replace(r.svc.cfg, agent_think=think)
    for u in (BOSS, JUNHO):
        await r.join(u)
    return r


# ── 1. 도구 결과 앞+뒤 자르기 ─────────────────────────────
@test
def clip_mid_keeps_head_and_tail():
    assert util.clip_mid("짧은 글", 4000) == "짧은 글"
    text = "머리\n" + "가" * 6000 + "\n합계: 12명 3건"
    out = util.clip_mid(text, 4000)
    assert len(out) <= 4000 and out.startswith("머리\n") and out.endswith("합계: 12명 3건")
    assert "…(중간 " in out and "자 생략)…" in out
    cut = int(out.split("…(중간 ")[1].split("자")[0])
    assert len(out) - len(f"\n…(중간 {cut}자 생략)…\n") + cut == len(text)      # 생략한 글자 수가 정확
    assert out.index("…(중간") >= 2600 - 5 and len(out) - out.index("자 생략)…") <= 1200 + 10   # 앞 ~2600 · 뒤 ~1200
    assert util.clip_mid("x" * 4000, 4000) == "x" * 4000                                     # 딱 맞으면 그대로


@test
async def long_tool_result_keeps_totals_line_at_the_end():
    old = fast_timers()
    try:
        r = await _room([tool_call("room_rules"), reply("합계는 12명이에요.")])
        await r.db.set_setting(r.CHAT, "rules", "규칙\n" + "\n".join(f"{i}. 긴 줄 " + "나" * 40 for i in range(200))
                               + "\n합계: 12명")
        await r.say(BOSS, "소담아 규칙 합계 알려줘")
        tool_msg = [m for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"][0]["content"]
        assert "합계: 12명" in tool_msg and "…(중간 " in tool_msg, tool_msg[-300:]
        assert len(tool_msg) < 4000 + 200                                               # nonce 태그 몫만 더
    finally:
        restore_timers(old)


_ = (asyncio, tools, JUNHO)   # 2단계 테스트에서 씀
