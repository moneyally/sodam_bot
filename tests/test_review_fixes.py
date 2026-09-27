"""검토에서 재현된 버그 회귀 테스트: python tests/run_all.py review_fixes

① 매일 정리(prune) 뒤 모든 쓰기가 'SQL statements in progress' 로 실패  ② 자유 멤버가 메시지를 수정하면 검사를 받음
③ 가입 확인 정답을 두 번 누르면 통과 기록이 지워짐  ④ 대량 입장 동시 입장 시 '내보낸 수'를 적게 셈
⑤ 감시 스크립트를 죽이면 남은 sleep 이 잠금을 쥐어 새 감시가 못 뜸
"""
import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from fake_llm import Room
from fakes import FakeMsg, FakeQuery, fake_user, make_db, runner

from sodam import free, handlers, joinreq, raid

test, run_all = runner()
ROOT = Path(__file__).resolve().parent.parent


@test
async def writes_work_right_after_prune():
    db = await make_db()
    await db.log_message(-1, 1, 1, "안녕")
    await db.prune(int(time.time()))
    await db.log_message(-1, 1, 2, "정리 직후 기록")                 # 예전엔 여기서 실패
    assert await db._one("SELECT 1 FROM messages WHERE text='정리 직후 기록'")


@test
async def free_member_edit_not_checked():
    r = await Room().open(admins={1}, settings={"injection_guard": False})
    await r.db.set_banned_word(Room.CHAT, "먹튀", True)
    u = fake_user(40, "자유")
    await free.add(r.db, Room.CHAT, u.id, 1)
    m = FakeMsg(Room.CHAT, u, "먹튀 조심", message_id=500)
    m.chat, m.sender_chat = SimpleNamespace(id=Room.CHAT, type="supergroup"), None
    await handlers.on_group_edit(SimpleNamespace(edited_message=m), r.ctx)
    assert not m.deleted and await r.db.warning_count(Room.CHAT, u.id) == 0


@test
async def join_answer_double_press_keeps_pass():
    r = await Room().open(admins={1}, settings={"join_verify": True, "captcha_enabled": True})
    u = fake_user(9_000_000_001, "신청")
    req = SimpleNamespace(chat_join_request=SimpleNamespace(chat=SimpleNamespace(id=Room.CHAT, type="supergroup"), from_user=u,
                                                            user_chat_id=u.id))
    await handlers.on_join_request(req, r.ctx)
    ans = (await joinreq._row(r.svc, Room.CHAT, u.id))["answer"]
    approved = []
    real = r.bot.approve_chat_join_request

    async def slow_approve(chat_id, user_id):   # 텔레그램: 두 번째 승인은 '이미 처리됨' 오류
        from telegram.error import BadRequest
        if approved:
            raise BadRequest("HIDE_REQUESTER_MISSING")
        approved.append(user_id)
        await asyncio.sleep(0.05)
        await real(chat_id, user_id)
    r.bot.approve_chat_join_request = slow_approve
    qs = [FakeQuery(u.id, u, f"jr:{Room.CHAT}:{ans}") for _ in range(2)]
    await asyncio.gather(*(handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx) for q in qs))
    assert await joinreq.passed(r.svc, Room.CHAT, u.id), "통과 기록이 남아야 함"
    assert len(approved) == 1 and len(r.bot.named("approve")) == 1


@test
async def raid_kick_count_exact_under_concurrency():
    r = await Room().open(admins={1}, settings={"raid_count": 100, "raid_action": "kick", "captcha_enabled": False})
    await raid.start(r.svc, r.bot, Room.CHAT, 30)
    users = [fake_user(100_000_000 + i, f"입장{i}") for i in range(10)]
    await asyncio.gather(*(handlers.handle_new_member(r.ctx, Room.CHAT, "방", u) for u in users))
    assert len(r.bot.named("ban")) == 10
    await raid.stop(r.svc, r.bot, Room.CHAT)
    dm = [c for c in r.bot.named("send_message") if c[1] == 1 and "방어 모드가 끝났어요" in c[2]]
    assert dm and "10명을 내보냈어요" in dm[0][2], dm


@test
def supervisor_lock_released_when_killed():
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"sup{os.getpid()}"
    (tmp / "sodam").mkdir(parents=True, exist_ok=True)
    (tmp / "sodam" / "__main__.py").write_text("import time\nwhile True: open('data/heartbeat','w').write(str(int(time.time()))); time.sleep(1)\n")
    env = {**os.environ, "GRACE": "37", "CHECK": "37"}   # 37: 정리할 때 이 sleep 만 정확히 찾으려고
    py = "/usr/bin/python3"
    sup = subprocess.Popen(["bash", str(ROOT / "tools/supervise.sh"), str(tmp), py], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=False)
    try:
        time.sleep(2)
        sup.kill()
        sup.wait()
        subprocess.run(["pkill", "-f", f"^{py} -m sodam$"])
        time.sleep(0.5)
        lock = subprocess.run(["flock", "-n", str(tmp / "data/.supervise.lock"), "true"])
        assert lock.returncode == 0, "감시가 죽었는데 잠금이 남음 (남은 sleep 이 쥐고 있음)"
    finally:
        subprocess.run(["pkill", "-f", f"^{py} -m sodam$"])
        subprocess.run(["pkill", "-x", "-f", "sleep 37"])


if __name__ == "__main__":
    run_all()
