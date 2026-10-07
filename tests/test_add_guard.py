"""🚫 강제 추가 막기 (sodam/addguard.py). 실제 2026-10-07 백악관: 관리자 아닌 누군가가 연락처 사람들을 5명씩 같은 초에 추가 →
'스팸 신고'로 방이 막힘(Chat_restricted), 누가 추가했는지 기록이 없었음. python tests/run_all.py add_guard"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import addguard, handlers  # noqa: E402

test, run_all = runner()
BOSS = fake_user(10, "대표", "boss")
BAD = fake_user(20, "추가범", "adder")
PEOPLE = [fake_user(100 + i, f"연락처{i}", None) for i in range(5)]


async def join(r, users, by):
    m = SimpleNamespace(chat_id=r.CHAT, chat=SimpleNamespace(id=r.CHAT, title="백악관", type="supergroup"),
                        new_chat_members=list(users), from_user=by, delete=None)

    async def delete():
        return True
    m.delete = delete
    await handlers.on_join(SimpleNamespace(message=m), r.ctx)
    await asyncio.sleep(0)


async def room(**settings):
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False, "recent_account_captcha": False,
                                                      "greet_enabled": False, **settings})
    await r.join(BAD)
    return r


@test
async def member_adding_strangers_gets_them_kicked_logged_and_muted():
    r = await room()
    await join(r, PEOPLE, BAD)
    kicked = [c[2] for c in r.bot.calls if c[0] == "ban"]
    assert sorted(kicked) == [p.id for p in PEOPLE], r.bot.calls
    rows = await r.db._all("SELECT actor_id, target_id FROM mod_log WHERE action='forced_add' ORDER BY id")
    assert [(x["actor_id"], x["target_id"]) for x in rows] == [(BAD.id, p.id) for p in PEOPLE]   # 다음엔 DB 에서 바로 범인
    muted = [c for c in r.bot.calls if c[0] == "restrict" and c[2] == BAD.id]
    assert len(muted) == 1, "3명째에서 한 번만 뮤트"
    assert any("강제 추가 감지" in str(c) for c in r.bot.calls if c[0] in ("send_message", "edit_text")), "관리자·오너 알림"


@test
async def own_join_admin_add_and_off_mode_pass():
    r = await room()
    await join(r, [PEOPLE[0]], PEOPLE[0])                     # 링크·신청으로 직접 들어옴 (추가한 사람 = 본인)
    await join(r, [PEOPLE[1]], BOSS)                          # 관리자가 추가·승인
    assert not [c for c in r.bot.calls if c[0] == "ban"]
    assert not await r.db._all("SELECT 1 FROM mod_log WHERE action='forced_add'")
    r = await room(add_guard="notify")                        # 알림만: 내보내지 않고 기록
    await join(r, PEOPLE[:2], BAD)
    assert not [c for c in r.bot.calls if c[0] == "ban"]
    assert len(await r.db._all("SELECT 1 FROM mod_log WHERE action='forced_add'")) == 2


@test
def setting_registered_with_kick_default():
    from sodam.settings import DEFAULTS
    assert DEFAULTS["add_guard"] == "kick" and addguard.BURST == 3


if __name__ == "__main__":
    run_all()
