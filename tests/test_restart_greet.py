"""재시작 중 쌓인 업데이트·기록에 없는 이름 인사·도구 로그·남의 말투: python tests/test_restart_greet.py"""
import asyncio
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fake_llm import Room, reply, tool_call
from fakes import fake_user, runner

from sodam import addressee
from sodam.util import is_stale

test, run_all = runner()
ROOT = Path(__file__).resolve().parents[1]
BOSS = fake_user(1, "방장", "boss")


@test
def polling_keeps_pending_updates():
    src = (ROOT / "sodam" / "__main__.py").read_text(encoding="utf-8")
    assert re.search(r"drop_pending_updates\s*=\s*False", src) and "drop_pending_updates=True" not in src


@test
def stale_check():
    class M:
        date = None
    assert not is_stale(M())                                    # 날짜 모르면 정상 처리
    M.date = datetime.now(timezone.utc)
    assert not is_stale(M())
    M.date = datetime.fromtimestamp(time.time() - 600, timezone.utc)
    assert is_stale(M())


@test
async def old_message_after_restart_gets_no_ai_reply():
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    await r.join(BOSS)
    m = r.msg(BOSS, "소담아 안녕")
    m.date = datetime.fromtimestamp(time.time() - 900, timezone.utc)
    from types import SimpleNamespace

    from sodam import handlers
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)   # 대본 비어 있음 → 부르면 AssertionError
    await r.settle()
    assert not r.llm.of("chat") and not m.replies


@test
async def unknown_name_hint_says_use_name_as_is():
    r = await Room().open(admins=(BOSS.id,))
    m = r.msg(BOSS, "소담아 Major님 입장 인사드려")
    hints = await addressee.collect(r.svc, r.bot, m, r.CHAT, BOSS, "Major님 입장 인사드려")
    assert hints[0].startswith("후보 없음") and "이름 그대로" in hints[0]


@test
async def greet_tool_missing_name_logged_and_guided():
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    await r.join(BOSS)
    seen = {}

    def final(messages):
        seen["tool"] = messages[-1]["content"]
        return "Major님 반갑습니다! 편하게 이야기 나눠요 😊"
    r.llm.script = [tool_call("greet_members", {"names": ["Major"]}), final]
    logs = []

    class H(logging.Handler):
        def emit(self, rec):
            logs.append(rec.getMessage())
    h = H()
    lg = logging.getLogger("sodam.agent")
    level = lg.level
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        m = await r.say(BOSS, "소담아 Major님 입장 인사드려")
    finally:
        lg.removeHandler(h)
        lg.setLevel(level)
    assert "못 찾은 이름: Major" in seen["tool"] and "이름 그대로" in seen["tool"]
    assert "멘션이 자동으로" not in seen["tool"]
    assert any("greet_members" in x and "Major" in x for x in logs), logs
    assert "Major님" in m.replies[-1] and "tg://user" not in m.replies[-1]



@test
async def style_command_with_target_goes_to_ai_and_sets_that_member():
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    luffy = fake_user(301, "루피", "LF2030")
    for u in (BOSS, luffy):
        await r.join(u)
    r.llm.script = [tool_call("set_member_style", {"name": "@LF2030", "style": "여친"}), "앞으로 이렇게 말할게요"]
    m = await r.say(BOSS, ".말투 여친 소담아 @LF2030 한테 이제 앞으로 말투 바꿔서 사용")
    assert (await r.db.get_member(r.CHAT, luffy.id))["style"] == "girlfriend"
    assert not (await r.db.get_member(r.CHAT, BOSS.id))["style"]
    assert "tg://user?id=301" in m.replies[-1]
    # 한 단어는 예전처럼 본인 말투 (AI 안 부름)
    n = len(r.llm.of("chat"))
    await r.say(BOSS, ".말투 츤데레")
    assert (await r.db.get_member(r.CHAT, BOSS.id))["style"] == "tsundere" and len(r.llm.of("chat")) == n


@test
async def member_cannot_set_someone_elses_style():
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    luffy, kim = fake_user(301, "루피", "LF2030"), fake_user(302, "김철수", "kimcs")
    for u in (BOSS, luffy, kim):
        await r.join(u)
    seen = {}

    def final(messages):
        seen["tool"] = messages[-1]["content"]
        return "그건 관리자만 바꿀 수 있어요"
    r.llm.script = [tool_call("set_member_style", {"name": "루피", "style": "여친"}), final]
    await r.say(kim, ".말투 여친 루피한테")
    assert "사용할 수 없음" in seen["tool"] and not (await r.db.get_member(r.CHAT, luffy.id))["style"]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
