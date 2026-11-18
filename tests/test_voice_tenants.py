"""🏷 클론봇도 노래 (sodam/voice/tenants.py): python tests/run_all.py voice_tenants

2026-11-17 오너 '클론도 자이 계정으로 노래': 음성 담당 하나가 본체 + 클론 장부의 노래 일감을 같이 받음.
- 도우미 계정(client·calls)은 hub 하나만 (같은 세션을 두 곳에서 열면 텔레그램이 세션을 무효로 만듦)
- 클론 방 대기열·카드는 클론 장부·클론 봇으로 · 클론은 도우미 로그인·로그아웃·AI 통화 X · 같은 방 DJ 두 명 X
"""
import asyncio

from fakes import FakeBot, make_db, runner
from test_music import CHAT, FakeDecoder, frames, make_worker, stop_all, until
from test_voice import run_job

from sodam.voice import musicq, store
from sodam.voice.tenants import Spec, Tenants, TenantWorker

test, run_all = runner()
CHAT2 = -100888


async def with_tenant():
    db, w, src, clock = await make_worker()
    db2, bot2 = await make_db(), FakeBot()
    opened = []

    async def opener(spec):
        opened.append(spec.slug)
        return type("C", (), {"db_path": "/tmp/clone/sodam.db", "tz": None, "owner_ids": frozenset({9})})(), db2, bot2
    w.tenants = Tenants(w, "/tmp", opener,
                        discover_fn=lambda d: {"prestige": Spec("prestige", "t", "/tmp/clone/sodam.db", frozenset({9}), "프레스티지")})
    await w.tenants.start()
    return db, w, db2, bot2, opened


async def clone_job(w, db2, kind, payload, chat=CHAT2):
    jid = await store.add_job(db2, chat, kind, payload, 7)
    await w.step()
    await w.drain()
    return await store.job(db2, jid)


@test
async def clone_music_uses_shared_assistant_and_own_ledger():
    db, w, db2, bot2, opened = await with_tenant()
    tw = w.tenants.workers["prestige"]
    try:
        assert opened == ["prestige"] and isinstance(tw, TenantWorker)
        assert tw.client is w.client and tw.calls is w.calls and tw.players is w.players, "도우미·DJ 표는 hub 하나"
        await w.step()
        assert await store.assistant(db2) and await store.worker_alive(db2), "클론 장부에도 '도우미 연결됨·음성 담당 살아 있음'"
        row = await clone_job(w, db2, "music_play", {"query": "클론 노래", "by_name": "멤버", "status_msg": 55, "by": 5})
        assert row["status"] == "done" and row["result"] == "playing", dict(row)
        assert any(x[0] == "play" and x[1] == CHAT2 for x in w.calls.log), "hub 의 도우미 계정으로 음성채팅"
        await until(lambda: any(x[0] == "frame" and x[1] == CHAT2 for x in frames(w)))
        assert await db2._one("SELECT 1 FROM music_queue WHERE chat_id=?", (CHAT2,)), "대기열은 클론 장부"
        assert not await db._one("SELECT 1 FROM music_queue WHERE chat_id=?", (CHAT2,)), "본체 장부엔 안 섞임"
        await until(lambda: any("재생 시작" in x[2] for x in bot2.named("edit_text")))
        assert not any("클론 노래" in str(x) for x in w.bot.calls), "재생 카드는 클론 봇이 보냄 (본체 봇 X)"
        assert (await clone_job(w, db2, "music_skip", {}))["result"] == "skipped", "넘기기도 클론 장부 일감으로"
    finally:
        await stop_all(w)


@test
async def clone_cannot_touch_assistant_login_or_ai_call():
    db, w, db2, bot2, _ = await with_tenant()
    client = w.client
    for kind in ("logout", "login_phone", "login_code"):
        row = await clone_job(w, db2, kind, {"phone": "+82", "code": "1"}, chat=0)
        assert row["status"] == "failed" and row["result"] == "clone_no_login", (kind, dict(row))
    assert w.client is client and await store.assistant(db), "클론 일감으로 자이 계정이 끊기면 안 됨"
    row = await clone_job(w, db2, "start", {})
    assert row["result"] == "clone_voice_off" and not w.bridges, "AI 음성 대화는 클론 X (노래만)"


@test
async def same_room_two_bots_one_dj():
    db, w, db2, bot2, _ = await with_tenant()
    FakeDecoder.LEN = 3000
    try:
        jid = await store.add_job(db, CHAT, "music_play", {"query": "본체 곡", "by_name": "멤버", "by": 5}, 7)
        await w.step()
        await w.drain()
        assert (await store.job(db, jid))["result"] == "playing"
        row = await clone_job(w, db2, "music_play", {"query": "클론 곡", "by_name": "멤버", "by": 5}, chat=CHAT)
        assert row["status"] == "failed" and row["result"] == "busy", "같은 방 DJ 는 하나 (대기열 갈라짐 방지)"
        assert len([p for p in w.players.values() if not p.done]) == 1
    finally:
        FakeDecoder.LEN = 40
        await stop_all(w)


@test
async def assistant_gone_is_mirrored_to_clone():
    db, w, db2, bot2, _ = await with_tenant()
    await w.step()
    assert await store.assistant(db2)
    w.client = w.calls = None                       # 도우미 세션이 풀림 (health 가 하는 일)
    await w.step()
    assert not await store.assistant(db2), "클론도 '도우미 연결 안 됨' 으로 안내하게"
    assert w.tenants.workers["prestige"].client is None


@test
async def drain_returns_when_job_finished_but_cleanup_not_run_yet():
    """2026-11-18 서버 시험 2시간 멈춤: 일감이 끝났는데 목록 정리(done 콜백)가 아직 안 돈 순간 drain 을 부르면,
    gather 가 쉬지 않고 바로 돌아와 정리가 영영 못 돌고 무한 반복 → 쉬지 않는 반복이라 wait_for 도 못 끊어서 알람으로 끊음."""
    import signal
    db, w, src, clock = await make_worker()

    async def quick():
        return None

    t = asyncio.create_task(quick())
    w.jobs.add(t)
    t.add_done_callback(w.jobs.discard)
    await asyncio.sleep(0)                  # 일감은 끝남, 정리 콜백은 아직 줄에서 대기
    assert t.done() and t in w.jobs, "재현 조건: 끝났는데 아직 목록에 있음"

    def boom(*_):
        raise TimeoutError("drain 무한 반복")
    old = signal.signal(signal.SIGALRM, boom)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        await w.drain()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    assert not w.jobs


if __name__ == "__main__":
    asyncio.run(run_all())
