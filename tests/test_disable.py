"""🚫 방별 명령 끄기 (sodam/disable.py · panels/disable.py). python tests/run_all.py disable

2026-11-22 경쟁조사 ③. 끄기·켜기·별칭→대표이름·보호명령 거절·캐시."""
from fakes import make_db, runner

from sodam import commands, disable
import sodam.handlers  # noqa: F401  (명령 등록 — disable.register_commands 가 돎)

test, run_all = runner()
CHAT = -1005555


def cmd_by(name):
    return commands._INDEX[name]


@test
async def disable_enable_and_is_off():
    db = await make_db()
    disable._CACHE.clear()
    rate = cmd_by("시세")                               # 끌 수 있는 일반 명령
    assert not await disable.is_off(db, CHAT, rate)
    out = await disable.disable_cmd(db, CHAT, "환율")    # 별칭으로 꺼도 대표이름으로 저장
    assert "껐어" in out
    assert await disable.is_off(db, CHAT, rate)          # 대표이름(시세)으로 is_off True
    assert "시세" in await disable.off_list(db, CHAT)
    # 다시 켜기
    assert "다시 켰어" in await disable.enable_cmd(db, CHAT, "시세")
    assert not await disable.is_off(db, CHAT, rate)


@test
async def protected_commands_cannot_be_disabled():
    db = await make_db()
    disable._CACHE.clear()
    for raw in ("설정", "명령끄기", "도움말"):
        out = await disable.disable_cmd(db, CHAT, raw)
        assert "끌 수 없어" in out, (raw, out)
    assert await disable.off_list(db, CHAT) == []        # 하나도 저장 안 됨


@test
async def unknown_command_rejected():
    db = await make_db()
    disable._CACHE.clear()
    assert "없어" in await disable.disable_cmd(db, CHAT, "없는명령어xyz")


@test
async def only_affects_target_room():
    db = await make_db()
    disable._CACHE.clear()
    rate = cmd_by("시세")
    await disable.disable_cmd(db, CHAT, "시세")
    assert await disable.is_off(db, CHAT, rate)
    assert not await disable.is_off(db, -1009999, rate)  # 다른 방은 영향 없음


@test
async def cache_busts_on_change():
    db = await make_db()
    disable._CACHE.clear()
    rate = cmd_by("시세")
    assert await disable.off_set(db, CHAT) == frozenset()  # 캐시에 빈 집합
    await disable.disable_cmd(db, CHAT, "시세")             # bust 해야 캐시가 새 값 봄
    assert "시세" in await disable.off_set(db, CHAT)
