"""📡 노래 끊김 블랙박스 (sodam/voice/netwatch.py · glitch.py · panels/music c_glitch `.끊김`).
2026-10-13 일루왕 '4초 멈췄다 2배속, 한 곡에 3번' → 손으로 잰 패킷: 오너가 들은 14:08:02 에 텔레그램 서버 → 우리 1.64초 끊김, 우리 보냄은 빈틈 0."""
import ipaddress
import json
import tempfile
import time
from pathlib import Path

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner

from sodam import commands
from sodam.commands import CmdCtx
from sodam.permissions import Role
from sodam.voice import glitch, netwatch

test, run_all = runner()
CHAT, OTHER = -100555, -100666
TG_A, TG_B = "2001:b28:f23f:f105:2:0:866:907", "2001:b28:f23f:f105:1:0:861:908"
USER = fake_user(7, "듣는사람")


def feed(w, start, secs, peer=TG_A, rin=11, rout=18, in_gap=None, out_gap=None):
    """secs 초 동안 들어옴 rin·나감 rout 개/초 (gap=(시작, 끝) 동안은 그쪽 안 보냄). 0.05초마다 check. 사건 목록."""
    evs, t, end = [], start, start + secs
    nin = nout = 0.0
    while t < end:
        nin += rin * 0.01
        nout += rout * 0.01
        if nin >= 1:
            nin -= 1
            if not (in_gap and in_gap[0] <= t - start < in_gap[1]):
                evs += w.packet(t, peer, False)
        if nout >= 1:
            nout -= 1
            if not (out_gap and out_gap[0] <= t - start < out_gap[1]):
                evs += w.packet(t, peer, True)
        if round((t - start) * 100) % 5 == 0:
            w.check(t)
        t = round(t + 0.01, 4)
    return evs


@test
async def telegram_addresses_only():
    assert netwatch.is_tg(ipaddress.ip_address(TG_A).packed)
    assert netwatch.is_tg(ipaddress.ip_address("149.154.167.51").packed)
    assert not netwatch.is_tg(ipaddress.ip_address("162.159.192.1").packed)        # 우회 길(Cloudflare)
    assert not netwatch.is_tg(ipaddress.ip_address("2a01:4f8:c2c:67c9::1").packed)  # 우리 서버


@test
async def parse_udp_headers_only():
    src, dst = ipaddress.ip_address(TG_A).packed, ipaddress.ip_address("2a01:4f8::1").packed
    v6 = bytes([0x60, 0, 0, 0, 0, 20, 17, 64]) + src + dst
    assert netwatch.parse(v6, netwatch.ETH_P_IPV6) == (src, dst)
    assert netwatch.parse(v6[:6] + bytes([6]) + v6[7:], netwatch.ETH_P_IPV6) is None    # TCP
    v4 = bytes([0x45, 0, 0, 28, 0, 0, 0, 0, 64, 17, 0, 0]) + bytes([149, 154, 167, 51]) + bytes([10, 0, 0, 1])
    assert netwatch.parse(v4, netwatch.ETH_P_IP) == (bytes([149, 154, 167, 51]), bytes([10, 0, 0, 1]))


@test
async def telegram_stall_while_we_keep_sending():
    w = netwatch.Watch()
    evs = feed(w, 1000.0, 20, in_gap=(10, 11.64))
    assert [e["kind"] for e in evs] == ["tg_stall"], evs
    assert 1.5 < evs[0]["dur"] < 1.8 and evs[0]["peer"] == TG_A


@test
async def our_stall_while_telegram_keeps_sending():
    w = netwatch.Watch()
    evs = feed(w, 1000.0, 20, out_gap=(8, 9))
    assert [e["kind"] for e in evs] == ["our_stall"], evs
    assert 0.9 < evs[0]["dur"] < 1.2


@test
async def short_gap_or_call_end_is_not_stall():
    w = netwatch.Watch()
    assert feed(w, 1000.0, 20, in_gap=(10, 10.5)) == []                    # 0.5초 = 평소 흔들림
    w = netwatch.Watch()
    assert feed(w, 1000.0, 20, in_gap=(10, 20), out_gap=(10, 20)) == []     # 둘 다 멈춤 = 통화 끝
    w = netwatch.Watch()
    assert feed(w, 1000.0, 20, rin=2, in_gap=(10, 12)) == []                # 드문드문 오가던 주소


@test
async def stall_then_call_end_is_dropped():
    # 텔레그램 쪽이 먼저 멈추고(멈춤 시작) 우리도 통화를 끝냄 → 나중에 다시 오가도 10초짜리 가짜 멈춤을 남기면 안 됨
    w = netwatch.Watch()
    assert feed(w, 1000.0, 30, in_gap=(10, 20), out_gap=(11.5, 20)) == []


@test
async def quiet_peer_is_forgotten():
    w = netwatch.Watch()
    feed(w, 1000.0, 5)
    w.check(1005.0 + netwatch.FORGET + 1)
    assert TG_A not in w.peers and w.snapshot(1070.0)["peers"] == {}


@test
async def snapshot_has_per_second_counts():
    w = netwatch.Watch()
    feed(w, 1000.0, 10)
    snap = w.snapshot(1010.0)
    p = snap["peers"][TG_A]
    assert len(p["out"]) == netwatch.RING and p["out"][-2] == 18 and p["in"][-2] == 11


def ring(now, peers):
    return {"ts": now, "peers": {r: {"first": f, "in": [11] * 60, "out": [18] * 60} for r, f in peers.items()}}


@test
async def verdict_kinds():
    now = 5000.0
    sess = [(CHAT, 4000.0)]
    r = ring(now, {TG_A: 4003.0})
    ev = {"kind": "tg_stall", "peer": TG_A, "ts": now - 12, "end": now - 10.4, "dur": 1.6}
    assert glitch.verdict(now, CHAT, sess, r, [ev])["kind"] == "telegram"
    assert glitch.verdict(now, CHAT, sess, r, [dict(ev, ts=now - 300, end=now - 299)])["kind"] == "clean"   # 오래전 것
    ours = {"kind": "our_stall", "peer": TG_A, "ts": now - 5, "end": now - 4, "dur": 1.0}
    assert glitch.verdict(now, CHAT, sess, r, [ev, ours])["kind"] == "ours"
    assert glitch.verdict(now, CHAT, sess, ring(now - 60, {TG_A: 4003.0}), [ev])["kind"] == "off"     # 블랙박스 멈춤
    assert glitch.verdict(now, CHAT, sess, None, [ev])["kind"] == "off"


@test
async def two_calls_matched_by_start_time():
    now = 5000.0
    sess = [(OTHER, 1000.0), (CHAT, 4000.0)]
    r = ring(now, {TG_A: 1004.0, TG_B: 4002.0})
    assert glitch.peer_for(CHAT, sess, r) == TG_B and glitch.peer_for(OTHER, sess, r) == TG_A
    other_ev = {"kind": "tg_stall", "peer": TG_A, "ts": now - 10, "end": now - 9, "dur": 1.0}
    v = glitch.verdict(now, CHAT, sess, r, [other_ev])
    assert v["kind"] == "clean" and v["sure"], v                                # 다른 방 통화 끊김은 이 방 탓 아님


@test
async def read_files_and_text():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "ring.json").write_text(json.dumps(ring(100.0, {TG_A: 1.0})))
        (p / "events.jsonl").write_text('{"kind":"tg_stall","peer":"x","ts":1,"end":2,"dur":1}\nnot json\n'
                                        '{"kind":"other"}\n')
        r, evs = glitch.read(p)
        assert r["peers"][TG_A]["first"] == 1.0 and len(evs) == 1
    from zoneinfo import ZoneInfo
    t = glitch.text({"kind": "telegram", "sure": True, "events": [{"kind": "tg_stall", "ts": 0, "dur": 1.64}]},
                    ZoneInfo("Asia/Seoul"))
    assert "텔레그램 음성 서버" in t and "1.6초" in t and "09:00:00" in t


@test
async def glitch_command_reports_and_tells_owner():
    from sodam.panels import music
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "일루왕")
    bot = FakeBot()
    with tempfile.TemporaryDirectory() as d:
        net = Path(d) / "netwatch"
        net.mkdir()
        now = time.time()
        await db._write("INSERT INTO music_sessions(chat_id, start_ts) VALUES(?,?)", (CHAT, int(now - 100)))
        (net / "ring.json").write_text(json.dumps(ring(now, {TG_A: now - 98})))
        (net / "events.jsonl").write_text(json.dumps({"kind": "tg_stall", "peer": TG_A, "ts": now - 8,
                                                      "end": now - 6.4, "dur": 1.6}) + "\n")
        old = music.data_dir
        music.data_dir = lambda s: Path(d)
        try:
            async def say(text):
                cmd_, args, argstr = commands.parse(text, "sodambot")
                msg = FakeMsg(CHAT, USER, text)
                await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, USER, Role.MEMBER, args, argstr), cmd_)
                return msg
            m = await say(".끊김")
            assert m.replies and "텔레그램 음성 서버" in m.replies[0], m.replies
            row = await db._one("SELECT * FROM music_glitch WHERE chat_id=?", (CHAT,))
            assert row["kind"] == "telegram" and row["user_id"] == USER.id
            owner_msgs = [c for c in bot.named("send_message") if c[1] == 1]
            assert owner_msgs and "끊김 신고" in owner_msgs[0][2] and "일루왕" in owner_msgs[0][2]
            m2 = await say(".렉")                                                    # 1분 안 다시 → 판정 안 함
            assert "1분 뒤" in m2.replies[0]
            assert len(await db._all("SELECT id FROM music_glitch")) == 1
        finally:
            music.data_dir = old


@test
async def glitch_command_without_music():
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot()
    cmd_, args, argstr = commands.parse(".끊김", "sodambot")
    msg = FakeMsg(OTHER, USER, ".끊김")
    await commands.dispatch(CmdCtx(svc, bot, msg, OTHER, USER, Role.MEMBER, args, argstr), cmd_)
    assert "안 나오고" in msg.replies[0]
    assert not await db._all("SELECT id FROM music_glitch")


if __name__ == "__main__":
    run_all()
