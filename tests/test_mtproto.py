"""🔧 MTProto 도우미 (sodam/mtproto.py · sodam/panels/mtproto.py · tools/mtproto_login.py): python tests/run_all.py mtproto

가짜 Telethon 클라이언트(seed_mtproto.FakeClient)로만 돈다. 실제 텔레그램 연결 없음.
"""
import asyncio
import dataclasses
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot, FakeQuery, fake_user, make_db, make_svc, runner
from seed_mtproto import FakeClient, factory
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel, PeerChat, PeerUser

from sodam import __main__ as app_main
from sodam import menu, mtproto
from sodam.menu import PanelCtx
from sodam.panels import mtproto as mt_panel

test, run_all = runner()

OWNER, BOSS = fake_user(1, "오너"), fake_user(2, "방장")
CH = -1001234567890
TOKEN = "999:AAbbcc"
mtproto.MIN_GAP = mtproto.PAGE_GAP = 0   # 요청·쪽 간격은 테스트에선 안 기다림


async def world(connect=True, **kw):
    db = await make_db()
    svc = await make_svc(db, admins={BOSS.id}, telegram_token=TOKEN, mtproto_api_id=12345, mtproto_api_hash="hash")
    mt = mtproto.MTProto(svc.cfg, db, factory=factory(**kw))
    if connect:
        await mt.start()
    svc.mtproto = mt
    return svc, mt


class Sleeps:
    """mtproto._sleep 바꿔 끼우기 (진짜로 안 기다림)."""
    def __enter__(self):
        self.old, self.calls = mtproto._sleep, []

        async def fake(s):
            self.calls.append(s)
        mtproto._sleep = fake
        return self

    def __exit__(self, *a):
        mtproto._sleep = self.old


# ── 1. 설정 없으면 꺼짐 · 모든 API None ─────────────────────
@test
async def disabled_when_unconfigured():
    db = await make_db()
    svc = await make_svc(db)
    assert svc.cfg.mtproto_api_id == 0 and svc.mtproto is None
    FakeClient.made.clear()
    mt = mtproto.MTProto(svc.cfg, db, factory=factory())
    assert not mt.enabled
    await mt.start()
    mt.start_background()
    assert mt._task is None and not FakeClient.made, "설정 없으면 클라이언트도 안 만듦"
    assert await mt.participants(CH) is None and await mt.member_diff(CH) is None
    assert await mt.views(CH, [1, 2]) is None
    # 봇 시작 배선: 설정 있을 때만 svc.mtproto (딜러 봇은 안 씀)
    for kw, want in (({}, False), ({"mtproto_api_id": 5, "mtproto_api_hash": "h"}, True),
                     ({"mtproto_api_id": 5, "mtproto_api_hash": ""}, False),
                     ({"mtproto_api_id": 5, "mtproto_api_hash": "h", "bot_role": "dealer"}, False)):
        s = app_main.build_services(dataclasses.replace(svc.cfg, **kw), db)
        assert (s.mtproto is not None) is want, kw
        await s.cas.close()
        await s.billing.close()
        await s.sports.close()
    saved = {k: os.environ.get(k) for k in ("MTPROTO_API_ID", "TELEGRAM_BOT_TOKEN", "SODAM_ENV")}
    # 잘못 적어도 봇은 켜짐 (기능만 꺼짐). 진짜 .env 는 안 읽음 (SODAM_ENV = 없는 파일)
    os.environ.update(MTPROTO_API_ID="abc", TELEGRAM_BOT_TOKEN="x", SODAM_ENV=str(Path(db.path).with_name("none.env")))
    try:
        from sodam.config import load_config
        assert load_config().mtproto_api_id == 0
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# ── 2. Bot API chat_id → Telethon peer ──────────────────────
@test
def chat_id_conversion():
    assert mtproto.to_peer(CH) == PeerChannel(1234567890)
    assert mtproto.to_peer(-1000000000001) == PeerChannel(1)
    assert mtproto.to_peer(-4242) == PeerChat(4242)
    assert mtproto.to_peer(777) == PeerUser(777)
    for cid in (CH, -1009999999999, -1000000000001, -4242, -999999999999, 777):
        assert tl_utils.get_peer_id(mtproto.to_peer(cid)) == cid, cid   # Telethon 의 marked id 와 왕복


# ── 3. 참가자 매핑 ───────────────────────────────────────────
@test
async def participants_mapping():
    svc, mt = await world(members=[(10, "민지", "minji"), (11, None, None, True)])
    assert mt.bot.connected and mt.bot.me == "@sodambot"
    got = await mt.participants(CH, limit=50)
    base = {"last_name": "", "deleted": False, "min": False}
    assert got == [{"id": 10, "first_name": "민지", "username": "minji", "is_bot": False, **base},
                   {"id": 11, "first_name": "", "username": "", "is_bot": True, **base}], got
    c = mt.bot.client
    assert ("entity", PeerChannel(1234567890)) in c.calls and ("page", PeerChannel(1234567890), 0, 50) in c.calls
    assert 424242 not in [m["id"] for m in got], "users 에 섞여 온 초대한 사람은 참가자가 아님"
    # 200명씩 쪽 넘김 · 요청 수 셈 · limit
    c.members = [(1000 + i, f"u{i}", "", False, "성" if i == 0 else None) for i in range(450)]
    n0 = mt.calls_last_hour()
    got = await mt.participants(CH)
    assert len(got) == 450 and got[0]["last_name"] == "성"
    assert [x[2:] for x in c.calls if x[0] == "page"][-4:] == [(0, 200), (200, 200), (400, 200), (450, 200)]
    assert mt.calls_last_hour() - n0 == 1 + 4, "entity 1 + 쪽 4 (마지막 빈 쪽 포함)"
    assert len(await mt.participants(CH, limit=300)) == 300 and [x[2:] for x in c.calls if x[0] == "page"][-2:] == [(0, 200), (200, 100)]
    # 기본 그룹은 getFullChat 1번 (iter_participants)
    assert len(await mt.participants(-4242)) == 450 and ("participants", PeerChat(4242), 10000) in c.calls


# ── 4. member_diff: 첫 스냅샷 · 들어옴/나감 · limit 에 걸리면 나감 모름 ─
@test
async def member_diff_snapshots():
    svc, mt = await world(members=[(10, "a", "a"), (11, "b", "b")])
    d = await mt.member_diff(CH)
    assert d == {"first": True, "joined": [], "left": [], "count": 2, "partial": False}, d
    mt.bot.client.members = [(11, "b2", "b"), (12, "c", "")]
    d = await mt.member_diff(CH)
    assert not d["first"] and [m["id"] for m in d["joined"]] == [12] and [m["id"] for m in d["left"]] == [10], d
    assert d["left"][0]["first_name"] == "a"
    rows = await svc.db._all("SELECT user_id, first_name FROM mtproto_members WHERE chat_id=? ORDER BY user_id", (CH,))
    assert [(r[0], r[1]) for r in rows] == [(11, "b2"), (12, "c")]
    d = await mt.member_diff(CH)
    assert d["joined"] == [] and d["left"] == [], d
    # 다른 방은 따로 첫 스냅샷
    assert (await mt.member_diff(-100555))["first"]
    # limit 에 걸린 목록: 나감은 비우고 스냅샷에서 지우지 않음
    mt.bot.client.members = [(13, "d", ""), (14, "e", "")]
    d = await mt.member_diff(CH, limit=2)
    assert d["partial"] and d["left"] == [] and [m["id"] for m in d["joined"]] == [13, 14], d
    n = (await svc.db._one("SELECT COUNT(*) AS n FROM mtproto_members WHERE chat_id=?", (CH,)))["n"]
    assert n == 4, n
    # 조회 실패면 None + 스냅샷 그대로
    mt.bot.client.raises.append(RuntimeError("boom"))
    assert await mt.member_diff(CH) is None and "boom" in mt.bot.last_error
    assert (await svc.db._one("SELECT count FROM mtproto_snap WHERE chat_id=?", (CH,)))["count"] == 2


# ── 5. FloodWait: 짧으면 기다렸다 1번 더 · 길면 포기 + 그때까지 호출 안 함 ─
@test
async def flood_wait_short_sleep_vs_give_up():
    svc, mt = await world(members=[(10, "a", "a")])
    c = mt.bot.client
    with Sleeps() as sl:
        c.raises.append(FloodWaitError(None, capture=5))
        got = await mt.participants(CH)
        assert got and got[0]["id"] == 10 and 5 in sl.calls, sl.calls
        assert mt.bot.last_flood[1] == 5 and mt.bot.flood_until == 0
        # 두 번 연속이면 두 번째는 포기
        c.raises += [FloodWaitError(None, capture=3), FloodWaitError(None, capture=3)]
        assert await mt.participants(CH) is None and mt.bot.flood_until > time.time()
        mt.bot.flood_until = 0
        sl.calls.clear()
        c.raises.append(FloodWaitError(None, capture=mtproto.MAX_FLOOD_SLEEP + 1))
        n = len(c.calls)
        assert await mt.participants(CH) is None
        assert mtproto.MAX_FLOOD_SLEEP + 1 not in sl.calls, "긴 FloodWait 은 안 기다림"
        assert mt.bot.flood_until > time.time() + mtproto.MAX_FLOOD_SLEEP - 1 and "FloodWait" in mt.bot.last_error
        n = len(c.calls)
        assert await mt.participants(CH) is None and len(c.calls) == n, "쉬는 동안엔 텔레그램에 안 물어봄"
        assert mt.status()["bot"]["flood_until"] > 0


# ── 6. 조회수: 채널마다 30분 1번 · 그 사이 캐시 · 동시 호출도 1번 ─
@test
async def views_rate_limit_and_cache():
    svc, mt = await world(members=[])
    assert mt.user.client is None and await mt.views(CH, [1]) is None, "사용자 세션 없으면 None (봇은 조회수 못 봄)"
    mtproto.write_session(mt.user.session_path, "USER")
    await mt._start_user()
    assert mt.user.connected
    u = mt.user.client
    u.view_counts = {5: 100, 6: 7}
    views = lambda: [x for x in u.calls if x[0] == "call"]  # noqa: E731
    got = await mt.views(CH, [6, 5, 5, -1])
    assert got == {5: 100, 6: 7}, got
    assert views() == [("call", "GetMessagesViewsRequest", [5, 6], False)], "increment=False (조회수 안 올림)"
    u.view_counts = {5: 999, 6: 999}
    assert await mt.views(CH, [5, 6, 7]) == {5: 100, 6: 7} and len(views()) == 1, "30분 안엔 캐시"
    other = await mt.views(-100777, [5])
    assert other == {5: 999} and len(views()) == 2, "다른 채널은 따로"
    # 30분 지나면 다시
    await svc.db._write("UPDATE mtproto_view_calls SET ts=ts-? WHERE chat_id=?", (mtproto.VIEWS_GAP, CH))
    assert await mt.views(CH, [5]) == {5: 999} and len(views()) == 3
    # 동시에 두 번 → 실제 호출 1번
    await svc.db._write("UPDATE mtproto_view_calls SET ts=0 WHERE chat_id=?", (CH,))
    a, b = await asyncio.gather(mt.views(CH, [6]), mt.views(CH, [6]))
    assert len(views()) == 4 and {6: 999} in (a, b), (a, b)
    # 실패는 None (다음 호출도 30분 안이면 캐시)
    await svc.db._write("UPDATE mtproto_view_calls SET ts=0 WHERE chat_id=?", (CH,))
    u.raises.append(RuntimeError("CHANNEL_PRIVATE"))
    assert await mt.views(CH, [5]) is None and "CHANNEL_PRIVATE" in mt.user.last_error
    assert await mt.views(CH, []) == {}


# ── 7. 세션 파일 권한 0600 ────────────────────────────────────
@test
async def session_file_perms():
    svc, mt = await world(members=[])
    p = mt.bot.session_path
    assert p.parent == Path(svc.cfg.db_path).resolve().parent and p.read_text() == "S:999"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600, oct(p.stat().st_mode)
    old = os.umask(0)
    try:
        q = p.with_name("mtproto_user.session")
        mtproto.write_session(q, "secret")
        assert stat.S_IMODE(q.stat().st_mode) == 0o600 and q.read_text() == "secret"
        os.chmod(q, 0o644)
        assert mtproto.read_session(q) == "secret" and stat.S_IMODE(q.stat().st_mode) == 0o600, "넓은 권한은 읽을 때 고침"
    finally:
        os.umask(old)
    assert mtproto.read_session(p.with_name("none.session")) == ""
    # 저장된 세션으로 재시작하면 다시 로그인 안 함 (봇 로그인 반복 = FloodWait)
    FakeClient.made.clear()
    await mt.stop()
    await mt.start()
    assert FakeClient.made[0].session_in == "S:999" and not [x for x in FakeClient.made[0].calls if x[0] == "sign_in"]


# ── 8. 업데이트는 안 받음 (PTB 폴링과 분리) ───────────────────
@test
async def receive_updates_disabled():
    client = mtproto.make_client("", 12345, "hash")
    try:
        assert client._no_updates is True, "receive_updates=False → invokeWithoutUpdates, 업데이트 루프 요청 안 함"
        assert client.flood_sleep_threshold == 0, "FloodWait 은 우리가 처리 (라이브러리가 몰래 오래 자지 않게)"
        assert not client.is_connected()
    finally:
        client.session.close()


# ── 9. 로그인 실패해도 봇은 안 죽음 ───────────────────────────
@test
async def login_failure_never_crashes():
    for kw, where in (({"connect_error": OSError("network down")}, "network down"),
                      ({"sign_in_error": RuntimeError("ACCESS_TOKEN_INVALID")}, "ACCESS_TOKEN_INVALID")):
        svc, mt = await world(connect=False, **kw)
        mt.start_background()
        await mt._task
        assert mt.bot.client is None and where in mt.bot.last_error and mt.bot.last_error_ts
        assert await mt.participants(CH) is None and await mt.member_diff(CH) is None
        st = mt.status()["bot"]
        assert st["configured"] and not st["connected"]
    # 팩토리 자체가 터져도 (telethon 없음 등)
    svc, mt = await world(connect=False)
    mt.factory = lambda *a: (_ for _ in ()).throw(ImportError("no telethon"))
    await mt.start()
    assert "no telethon" in mt.bot.last_error
    # 사용자 세션: 만료·봇 계정이면 끄고 오류만
    svc, mt = await world(connect=False, authorized=False)
    mtproto.write_session(mt.user.session_path, "OLD")
    await mt._start_user()
    assert mt.user.configured and mt.user.client is None and "만료" in mt.user.last_error
    svc, mt = await world(connect=False)
    mt.factory = factory(authorized=True, is_user=False)   # get_me 가 봇
    mtproto.write_session(mt.user.session_path, "X")
    await mt._start_user()
    assert mt.user.client is None and "봇 계정" in mt.user.last_error
    # 토큰이 바뀌면 옛 세션 버리고 새 봇 토큰으로 로그인
    svc, mt = await world(connect=False, bot_id=555)
    mtproto.write_session(mt.bot.session_path, "S:555")
    FakeClient.made.clear()
    mt.factory = lambda s, *a: FakeClient(s, *a, bot_id=555 if s else 999)
    await mt._start_bot()
    assert mt.bot.me == "@sodambot" and FakeClient.made[1].session_in == "" and \
        ("sign_in", TOKEN) in FakeClient.made[1].calls and "disconnect" in FakeClient.made[0].calls
    # 종료: 연결 끊고 다시 켜기 연타는 막힘
    await mt.stop()
    assert mt.bot.client is None and "disconnect" in FakeClient.made[1].calls
    assert mt.restart() and not mt.restart(), "30초 안 두 번째는 거절"
    await mt._task
    assert mt.bot.connected
    await mt.stop()


# ── 10. 오너 화면: 오너만 · 상태·안내 · 다시 연결 ──────────────
async def press(svc, bot, user, data):
    q = FakeQuery(user.id, user, data)
    svc.menu_limiter._hits.clear()
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


@test
async def owner_only_screen():
    svc, mt = await world(members=[])
    bot = FakeBot()
    svc.perms.owner_ids = {OWNER.id}
    _, kb = await menu.main_menu(svc, bot, OWNER.id)
    assert "m:mt" in [b.callback_data for row in kb.inline_keyboard for b in row]
    _, kb = await menu.main_menu(svc, bot, BOSS.id)
    assert "m:mt" not in [b.callback_data for row in kb.inline_keyboard for b in row], "방 관리자 메인엔 없음"
    for who in (BOSS, fake_user(3, "멤버")):
        for data in ("m:mt", "m:mtr"):
            q = await press(svc, bot, who, data)
            assert not q.edits and q.answers[0][1], (who.id, data)
    assert mt._task is None or mt._task.done()
    for fn in (mt_panel.s_status, mt_panel.r_restart):   # 라우트와 별개로 함수도 스스로 확인
        s = await fn(PanelCtx(svc, bot, BOSS.id, None, []))
        assert s.text is None and "오너" in s.toast
    assert all(menu.ROUTES[c].need == menu.OWNER and not menu.ROUTES[c].scoped for c in ("mt", "mtr"))
    mt.bot.last_flood = (int(time.time()), 12)
    mt.user.fail("x", "<b>bad</b> & worse")
    mt.user.configured = True
    q = await press(svc, bot, OWNER, "m:mt")
    body = q.edits[-1]
    assert "① 봇 세션" in body and "✅ @sodambot" in body and "FloodWait" in body and "12초" in body
    assert "&lt;b&gt;bad&lt;/b&gt; &amp; worse" in body and "<b>bad" not in body
    q = await press(svc, bot, OWNER, "m:mtr")
    assert q.edits and "다시 연결" in q.answers[0][0]
    await mt._task
    q = await press(svc, bot, OWNER, "m:mtr")
    assert not q.edits and "30초" in q.answers[0][0]
    # 설정 없으면 켜는 법
    svc.mtproto = None
    q = await press(svc, bot, OWNER, "m:mt")
    body = q.edits[-1]
    assert "my.telegram.org" in body and "MTPROTO_API_ID" in body and "tools/mtproto_login.py" in body
    assert "터미널" in body and "무효화" in body
    assert "m:mtr" not in [b.callback_data for row in q.kb.inline_keyboard for b in row]
    q = await press(svc, bot, OWNER, "m:mtr")
    assert not q.edits and q.answers[0][1]
    await mt.stop()


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
