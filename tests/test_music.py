"""🎵 소담 뮤직봇 (sodam/voice/music.py · musicq.py · worker 노래 일감 · panels/music.py): python tests/run_all.py music

오너 요청 2026-10-08: '소담이가 DJ 로 — 멜론봇 같은 노래봇, 유튜브 링크도'. 서버 실측: yt-dlp 검색은 되고 다운로드는 영상에 따라
'Sign in to confirm you're not a bot' (Hetzner IP) → 쿠키 넣는 길. 네트워크·py-tgcalls·yt-dlp 없이 가짜로 (ffmpeg 풀기만 진짜).
"""
import asyncio
import os
import re
import tempfile
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner
from test_voice import MEDIA, FakeCalls, FakeClient, FakeConn, run_job

import sodam.panels  # noqa: F401
from sodam import commands, tools
from sodam.commands import CmdCtx
from sodam.menu import PanelCtx
from sodam.panels import music as M
from sodam.panels import voice as P
from sodam.permissions import Role
from sodam.voice import audio, music, musicq, store
from sodam.voice.worker import Worker

test, run_all = runner()


async def until(cond, secs=10.0):
    """서버 테스트는 2개씩 동시에 + CPU 제한 → 넉넉히 (조건이 되면 바로 끝남)."""
    t = time.monotonic()
    while not cond() and time.monotonic() - t < secs:
        await asyncio.sleep(0.01)
    assert cond()
CHAT = -100777
ADMIN, MEMBER, OTHER = fake_user(1, "대표"), fake_user(5, "멤버"), fake_user(6, "다른멤버")


def tone(ms: int, amp: int = 8000) -> bytes:
    n = audio.TG_RATE * ms // 1000
    return (np.sin(np.arange(n) / 8) * amp).astype("<i2").tobytes()


# ── 대기열 (DB) ─────────────────────────────────────────
@test
async def queue_positions_caps_and_remove_rules():
    db = await make_db()
    add = lambda t, by=5: musicq.add(db, CHAT, title=t, url="u", vid=t, duration=200, by_id=by, by_name="멤버",  # noqa: E731
                                     queue_max=3, per_user=2)
    assert (await add("a"))[1:] == (0, "ok"), "아무것도 없으면 바로 틀 차례 (#0)"
    assert (await add("b"))[1] == 1, "앞에 기다리는 곡이 있으면 #1"
    first = await musicq.start_next(db, CHAT)
    assert first["title"] == "a" and (await musicq.start_next(db, CHAT))["id"] == first["id"], "playing 은 한 곡"
    assert (await add("c"))[1] == 2 and (await add("d"))[1:] == (0, "per_user")
    assert (await add("d", by=6))[1] == 3 and (await add("e", by=7))[1:] == (0, "full")
    row, why = await musicq.remove_nth(db, CHAT, 3, 5, admin=False)
    assert why == "not_yours" and row["title"] == "d", "남의 곡은 못 뺌"
    assert (await musicq.remove_nth(db, CHAT, 1, 5, admin=False))[1] == "ok"
    assert [r["title"] for r in await musicq.waiting(db, CHAT)] == ["c", "d"]
    assert (await musicq.remove_nth(db, CHAT, 9, 1, admin=True))[1] == "none"
    assert await musicq.clear(db, CHAT) == 3 and not await musicq.current(db, CHAT)


@test
def youtube_links_and_mix_ducking():
    for u in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "https://youtu.be/dQw4w9WgXcQ?si=x",
              "https://youtube.com/shorts/dQw4w9WgXcQ", "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD"):
        assert music.link_id(u) == "dQw4w9WgXcQ", u
    assert music.link_id("아이유 밤편지") is None
    m = np.full(480, 8000, "<i2").tobytes()
    v = np.full(480, 1000, "<i2").tobytes()
    assert set(np.frombuffer(music.mix(m, None, 1.0), "<i2")) == {8000}
    assert set(np.frombuffer(music.mix(m, v, music.DUCK), "<i2")) == {3000}, "소담 목소리 + 줄인 노래"
    assert set(np.frombuffer(music.mix(m, v, 0.0), "<i2")) == {1000}, "음소거 = 목소리만"
    assert music.mix(None, None, 1.0) == music.SILENCE and len(music.SILENCE) == audio.FRAME_BYTES


@test
async def real_ffmpeg_decodes_wav_to_10ms_frames_and_seeks():
    d = tempfile.mkdtemp()
    path = f"{d}/t.wav"
    with wave.open(path, "wb") as w:            # 44.1 kHz 스테레오 1초 → 48 kHz 모노 10 ms 조각 ~100개
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(np.repeat((np.sin(np.arange(44100) / 6) * 9000).astype("<i2"), 2).tobytes())

    async def frames(start_ms):
        dec = music.Decoder(path, start_ms)
        await dec.start()
        got = []
        for _ in range(500):
            f = dec.frame()
            if f:
                got.append(f)
            elif dec.finished:
                break
            else:
                await asyncio.sleep(0.01)
        await dec.close()
        return got
    full = await frames(0)
    assert 98 <= len(full) <= 102 and all(len(f) == audio.FRAME_BYTES for f in full), len(full)
    assert audio.level(full[50]) > 3000
    half = await frames(500)
    assert 48 <= len(half) <= 52, len(half)


# ── worker (가짜 텔레그램·py-tgcalls·유튜브·풀기) ─────────
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeSource:
    def __init__(self, d):
        self.d, self.fetched, self.resolved = Path(d), [], []

    def resolve(self, query):
        self.resolved.append(query)
        if "막힘" in query:
            raise music.MusicError("blocked", "유튜브가 서버를 '봇'으로 막았어요")
        if "없는노래" in query:
            raise music.MusicError("not_found", f"'{query}' 노래를 못 찾았어요.")
        if "고르기" in query:
            raise music.MusicChoice([{"vid": "aaaaaaaaaa1", "title": "가수 - 첫째 곡", "duration": 200, "kind": ""},
                                     {"vid": "aaaaaaaaaa2", "title": "다른가수 - 첫째 곡 Cover", "duration": 210, "kind": "cover"}],
                                    "partial", query)
        vid = music.link_id(query) or query.replace(" ", "")[:11].ljust(11, "x")
        return {"title": f"{query} (MV)", "url": f"https://www.youtube.com/watch?v={vid}", "vid": vid, "duration": 180}

    def pick(self, item, req=None):
        self.resolved.append(("pick", item["vid"]))
        return {"title": item["title"], "url": f"https://www.youtube.com/watch?v={item['vid']}", "vid": item["vid"],
                "duration": item["duration"]}

    def fetch(self, vid):
        self.fetched.append(vid)
        p = self.d / "music" / f"{vid}.webm"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return str(p)


class FakeDecoder:
    """곡마다 LEN 조각 (진짜 ffmpeg 대신). 어디서부터 풀었는지 기록."""
    LEN = 40
    opened: list = []

    def __init__(self, path, start_ms=0):
        self.path, self.start_ms = path, start_ms
        self.left = max(0, self.LEN - start_ms // 10)
        self.failed = ""
        FakeDecoder.opened.append((Path(path).stem, start_ms))

    async def start(self):
        pass

    def frame(self):
        if self.left <= 0:
            return None
        self.left -= 1
        return tone(10)

    @property
    def finished(self):
        return self.left <= 0

    async def close(self):
        pass


async def make_worker(bot=None, clock=None):
    db = await make_db()
    d = tempfile.mkdtemp()
    cfg = SimpleNamespace(db_path=f"{d}/sodam.db", mtproto_api_id=1, mtproto_api_hash="x")
    clock = clock or Clock()

    async def sleep(s):
        clock.t += s
        await asyncio.sleep(0.001)
    src = FakeSource(d)
    FakeDecoder.opened = []
    w = Worker(cfg, db, client_factory=lambda *a: Client(), calls_factory=FakeCalls,
               realtime_connect=lambda m: None, media=MEDIA, bot=bot or FakeBot(), music_source=src,
               music_decoder=FakeDecoder, music_opts={"clock": clock, "sleep": sleep, "poll": 0.02, "idle_sec": 5})
    await run_job(db, w, "login_phone", {"phone": "+821000000000"})
    await run_job(db, w, "login_code", {"code": "12345"})
    return db, w, src, clock


class Client(FakeClient):
    async def is_user_authorized(self):
        return True

    async def get_dialogs(self, limit=0):
        return []

    def is_connected(self):
        return True


async def stop_all(w):
    """테스트 끝: 남은 DJ 를 끝내고 기다림 (DB 가 닫힌 뒤 뒤늦게 쓰지 않게)."""
    for chat_id, pl in list(w.players.items()):
        pl.stop("end")
    await asyncio.gather(*list(w.ptasks.values()), return_exceptions=True)


async def play(db, w, query, by=5, status=None):
    return await run_job(db, w, "music_play", {"query": query, "by_name": "멤버", "status_msg": status, "by": by}, CHAT)


def frames(w):
    return [x for x in w.calls.log if x[0] == "frame"]


@test
async def play_queue_skip_and_cards():
    db, w, src, _ = await make_worker()
    bot = w.bot
    row = await play(db, w, "아이유 밤편지", status=77)
    assert row["status"] == "done" and row["result"] == "playing", dict(row)
    p = next(x for x in w.calls.log if x[0] == "play")
    assert p[1] == CHAT and p[2] == ("media", 1, (48000, 1)) and p[3] == {"auto_start": True}, "소리 줄 하나 (외부 오디오)"
    assert not any(x[0] == "record" for x in w.calls.log), "노래만이면 받아쓰기 안 함"
    await until(lambda: any("재생 시작" in x[2] for x in bot.named("edit_text")))
    assert "아이유 밤편지 (MV)" in bot.named("edit_text")[0][2], "'찾는 중' 글을 재생 카드로"
    await until(lambda: len(frames(w)) >= 5)
    assert await musicq.active_session(db, CHAT)
    row = await play(db, w, "두번째 곡", status=78)
    assert row["result"] == "queued"
    assert any("대기열 추가</b> (#1)" in x[2] for x in bot.named("edit_text")), bot.named("edit_text")
    assert (await run_job(db, w, "music_skip", {}, CHAT))["result"] == "skipped"
    await until(lambda: any("두번째 곡" in x[2] and "재생 시작" in x[2] for x in bot.named("send_message")))
    assert bot.named("edit_markup"), "지난 곡 카드 버튼은 뗌"
    first = await db._one("SELECT state FROM music_queue WHERE title LIKE '아이유%'")
    assert first["state"] == "skipped"
    assert src.fetched[:2] == ["아이유밤편지".ljust(11, "x"), "두번째곡".ljust(11, "x")], src.fetched
    await stop_all(w)


@test
async def pause_resume_seek_volume_mute_loop():
    db, w, _, _ = await make_worker()
    FakeDecoder.LEN = 3000
    try:
        await play(db, w, "긴 노래")
        await until(lambda: w.players.get(CHAT) and w.players[CHAT].pos_ms >= 100)
        pl = w.players[CHAT]
        assert (await run_job(db, w, "music_pause", {}, CHAT))["result"] == "paused"
        pos = pl.pos_ms
        await asyncio.sleep(0.05)
        assert pl.pos_ms == pos, "멈추면 위치 그대로"
        assert (await run_job(db, w, "music_pause", {}, CHAT))["status"] == "failed", "이미 멈춤"
        assert (await run_job(db, w, "music_resume", {}, CHAT))["result"] == "resumed"
        await until(lambda: pl.pos_ms > pos)
        assert (await run_job(db, w, "music_seek", {"value": 90}, CHAT))["result"] == "seek:90"
        assert FakeDecoder.opened[-1][1] == 90_000 and pl.pos_ms >= 90_000
        assert (await run_job(db, w, "music_seek", {"value": 10, "relative": True}, CHAT))["result"].startswith("seek:10")
        assert (await run_job(db, w, "music_seek", {"value": 999}, CHAT))["status"] == "failed", "곡 길이(180초) 넘음"
        assert (await run_job(db, w, "music_volume", {"value": 500}, CHAT))["result"] == "volume:200"
        await run_job(db, w, "music_mute", {}, CHAT)
        n = len(frames(w))
        await until(lambda: len(frames(w)) > n + 3)
        assert pl.muted and (await run_job(db, w, "music_loop", {"value": 3}, CHAT))["result"] == "loop:3"
    finally:
        FakeDecoder.LEN = 40
        await stop_all(w)


class Const(FakeDecoder):
    """늘 8000 인 소리 (크기 확인용)."""

    def frame(self):
        if self.left <= 0:
            return None
        self.left -= 1
        return np.full(480, 8000, "<i2").tobytes()


@test
async def player_mute_and_ducking_change_what_is_sent():
    db = await make_db()
    clock = Clock()
    sent = []

    async def send(f):
        sent.append(int(np.frombuffer(f, "<i2")[0]))

    async def sleep(s):
        clock.t += s
        await asyncio.sleep(0.001)
    Const.LEN = 100000
    d = tempfile.mkdtemp()
    pl = music.Player(db, CHAT, send, source=FakeSource(d), decoder=Const, clock=clock, sleep=sleep, poll=0.02)
    await musicq.add(db, CHAT, title="t", url="u", vid="vvvvvvvvvvv", duration=999, by_id=1, by_name="a")
    task = asyncio.create_task(pl.run())
    await until(lambda: 8000 in sent)
    for _ in range(20):
        pl.voice_frame(np.full(480, 1000, "<i2").tobytes())
    await until(lambda: 3000 in sent), "소담 목소리 나오는 동안 노래는 DUCK 배"
    i = sent.index(3000)
    assert all(3000 <= x <= 9000 for x in sent[sent.index(8000):i]), "줄이는 중간 값을 거침 (한 번에 1→0.25 X)"
    assert len(set(sent[sent.index(8000):i])) >= 3, ("50ms 에 걸쳐 줄임", sent[-30:])
    await until(lambda: sent[-1] == 8000)
    pl.volume = 50
    await until(lambda: sent[-1] == 4000)
    pl.muted = True
    await until(lambda: sent[-1] == 0)
    pl.stop("end")
    await task


@test
async def loop_replays_then_end_clears_and_leaves():
    db, w, _, _ = await make_worker()
    await play(db, w, "반복곡")
    await until(lambda: CHAT in w.players)
    await run_job(db, w, "music_loop", {"value": 1}, CHAT)
    await until(lambda: len([o for o in FakeDecoder.opened if o[0].startswith("반복곡")]) >= 2)
    await play(db, w, "다음곡")
    assert (await run_job(db, w, "music_end", {}, CHAT))["result"] == "ended"
    assert ("leave", CHAT) in w.calls.log and CHAT not in w.players
    assert not await musicq.waiting(db, CHAT) and not await musicq.current(db, CHAT)
    s = await db._one("SELECT * FROM music_sessions WHERE chat_id=?", (CHAT,))
    assert s["reason"] == "end" and s["end_ts"]
    assert (await run_job(db, w, "music_skip", {}, CHAT))["result"] == "no_music"


@test
async def idle_after_queue_runs_out_leaves_once():
    db, w, _, _ = await make_worker()
    await play(db, w, "짧은곡")
    await until(lambda: ("leave", CHAT) in w.calls.log)    # 끝 정리(대기열·세션 기록) 뒤에 나감
    assert CHAT not in w.players
    s = await db._one("SELECT * FROM music_sessions WHERE chat_id=?", (CHAT,))
    assert s["reason"] == "idle" and s["tracks"] == 1
    assert (await db._one("SELECT state FROM music_queue"))["state"] == "done"


@test
async def blocked_youtube_edits_status_and_records_health():
    db, w, _, _ = await make_worker()
    row = await play(db, w, "막힘 테스트", status=55)
    assert row["status"] == "failed" and row["result"] == "music:blocked"
    assert any("봇" in x[2] for x in w.bot.named("edit_text"))
    assert (await db.get_state(0, musicq.HEALTH_KEY))["err"]
    assert not w.players and not any(x[0] == "play" for x in w.calls.log), "못 찾으면 음성채팅에 안 들어감"
    assert (await play(db, w, "없는노래"))["result"] == "music:not_found"


@test
async def ai_voice_and_music_share_one_call_and_voice_ducks_music():
    db, w, _, _ = await make_worker()
    FakeDecoder.LEN = 3000
    try:
        await play(db, w, "배경음악")
        await until(lambda: CHAT in w.players)
        conn = FakeConn()
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def rt(model):
            yield conn
        w.realtime_connect = rt
        row = await run_job(db, w, "start", {"instructions": "x", "max_sec": 30}, CHAT)
        assert row["result"] == "started", row["result"]
        assert len([x for x in w.calls.log if x[0] == "play"]) == 1, "AI 대화가 소리 줄을 다시 열지 않음"
        assert any(x[0] == "record" for x in w.calls.log)
        pl = w.players[CHAT]
        got, real = [], pl.voice_frame
        pl.voice_frame = lambda f: (got.append(f), real(f))
        n = len(frames(w))
        await w._send(CHAT, tone(10))                    # 소담 AI 목소리 한 조각 → 바로 보내지 않고 DJ 가 섞음
        assert got == [tone(10)]
        await w._send(CHAT, bytes(audio.FRAME_BYTES))   # 무음은 섞을 것에 안 넣음
        assert all(f != music.SILENCE for f in pl.voice)
        pl.voice_frame = real
        await run_job(db, w, "stop", {}, CHAT)
        await until(lambda: CHAT not in w.bridges)
        assert ("leave", CHAT) not in w.calls.log and CHAT in w.players, "AI 가 나가도 노래는 계속"
        await run_job(db, w, "music_end", {}, CHAT)
        assert ("leave", CHAT) in w.calls.log
    finally:
        FakeDecoder.LEN = 40
        await stop_all(w)
        for b in list(w.bridges.values()):
            b.stop("end")
        await asyncio.gather(*list(w.tasks.values()), return_exceptions=True)


@test
async def restart_saves_position_and_resumes_from_there():
    db, w, _, _ = await make_worker()
    FakeDecoder.LEN = 3000
    try:
        await play(db, w, "이어틀기")
        await until(lambda: CHAT in w.players and w.players[CHAT].pos_ms >= 200)
        await play(db, w, "그다음")
        await w.shutdown()
        cur = await musicq.current(db, CHAT)
        assert cur["title"].startswith("이어틀기") and cur["pos_ms"] >= 200, "재시작: 곡은 playing 그대로 + 위치"
        assert len(await musicq.waiting(db, CHAT)) == 1
        w2 = Worker(w.cfg, db, client_factory=lambda *a: Client(), calls_factory=FakeCalls, realtime_connect=None,
                    media=MEDIA, bot=w.bot, music_source=w.music_source, music_decoder=FakeDecoder, music_opts=w.music_opts)
        from sodam import mtproto
        from sodam.voice.worker import session_path
        mtproto.write_session(session_path(w.cfg), "SESSION")
        t = asyncio.create_task(w2.run())
        await until(lambda: CHAT in w2.players)
        await until(lambda: FakeDecoder.opened[-1][0].startswith("이어틀기") and FakeDecoder.opened[-1][1] >= 200)
        s = await db._all("SELECT reason FROM music_sessions ORDER BY id")
        assert s[0]["reason"] == "restart"
        t.cancel()
        await stop_all(w2)
    finally:
        FakeDecoder.LEN = 40


@test
async def telegram_file_path_must_stay_in_music_dir():
    db, w, _, _ = await make_worker()
    row = await run_job(db, w, "music_play", {"path": "/etc/passwd", "title": "x"}, CHAT)
    assert row["result"] == "music:not_found", "data/music 밖 파일은 안 염"
    ok = Path(w.cfg.db_path).parent / "music" / "tg_abc.mp3"
    ok.parent.mkdir(parents=True, exist_ok=True)
    ok.write_bytes(b"x")
    row = await run_job(db, w, "music_play", {"path": str(ok), "title": "내 파일", "duration": 61}, CHAT)
    assert row["result"] == "playing" and FakeDecoder.opened[-1][0] == "tg_abc"
    await run_job(db, w, "music_end", {}, CHAT)


# ── 봇 쪽 ────────────────────────────────────────────────
async def world(**settings):
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN.id,))
    await db.ensure_chat(CHAT, "노래방")
    for k, v in settings.items():
        await db.set_setting(CHAT, k, v)
    bot = FakeBot()

    async def promote(chat_id, user_id, **kw):
        bot.calls.append(("promote", chat_id, user_id, kw))
    bot.promote_chat_member = promote
    return db, svc, bot


async def ready(db):
    await db.set_state(0, store.ASSISTANT_KEY, {"id": 4242, "name": "소담 노래", "username": None})
    await db.set_state(0, store.WORKER_BEAT, time.time())


async def cmd(svc, bot, text, user=MEMBER, role=Role.MEMBER, reply_to=None):
    cmd_, args, argstr = commands.parse(text, "sodambot")
    msg = FakeMsg(CHAT, user, text, reply_to=reply_to)
    await commands.dispatch(CmdCtx(svc, bot, msg, CHAT, user, role, args, argstr), cmd_)
    return msg


async def worker_answers(db, answers):
    for _ in range(400):
        for j in await store.take_jobs(db):
            res = answers.get(j["kind"], "done")
            await store.finish(db, j["id"], not res.startswith("!"), res.lstrip("!"))
        await asyncio.sleep(0.01)


@test
async def play_command_posts_searching_and_sends_job():
    db, svc, bot = await world()
    await ready(db)
    bot.member_status = {(CHAT, 4242): "member"}
    t = asyncio.create_task(worker_answers(db, {"music_play": "playing"}))
    msg = await cmd(svc, bot, ".노래 아이유 밤편지")
    assert msg.replies and "찾는 중" in msg.replies[0], msg.replies
    job = None
    for _ in range(200):
        job = await db._one("SELECT * FROM voice_jobs WHERE kind='music_play'")
        if job and job["status"] == "done":
            break
        await asyncio.sleep(0.01)
    t.cancel()
    assert job and job["by_user"] == MEMBER.id and job["status"] == "done"
    assert bot.named("promote") and bot.named("promote")[0][3] == {"can_manage_video_chats": True}


@test
async def bare_english_slash_only_when_room_allows_it():
    db, svc, bot = await world()
    await ready(db)
    msg = await cmd(svc, bot, "/play 밤편지")
    assert not msg.replies, "다른 음악봇과 같이 있는 방: @ 없는 /play 는 무시"
    msg = await cmd(svc, bot, "/play@sodambot 밤편지")
    assert msg.replies and "찾는 중" in msg.replies[0]
    await db.set_setting(CHAT, "music_bare", True)
    msg = await cmd(svc, bot, "/skip")
    assert msg.replies, "켠 방은 받음"
    msg = await cmd(svc, bot, "/mute@sodambot")
    assert commands.parse("/mute", "sodambot")[0].names[0] == "뮤트", "/mute 는 멤버 뮤트 그대로"


@test
async def precheck_paths():
    db, svc, bot = await world()
    assert "연결 안 됐" in (await cmd(svc, bot, ".노래 밤편지")).replies[0]
    await ready(db)
    await db.set_setting(CHAT, "music_who", "admin")
    assert "관리자만" in (await cmd(svc, bot, ".노래 밤편지")).replies[0]
    await db.set_setting(CHAT, "music_enabled", False)
    assert "꺼져" in (await cmd(svc, bot, ".노래 밤편지", ADMIN, Role.ADMIN)).replies[0]
    await db.set_setting(CHAT, "music_enabled", True)
    assert "제목" in (await cmd(svc, bot, ".노래", ADMIN, Role.ADMIN)).replies[0]
    await db.set_setting(CHAT, "music_who", "all")
    assert "찾는 중" in (await cmd(svc, bot, ".노래 첫곡", OTHER)).replies[0]
    assert "초에 한 번" in (await cmd(svc, bot, ".노래 둘째곡", OTHER)).replies[0], "연타 = 유튜브 검색만 늘어남"


@test
async def control_permissions_requester_admin_and_end():
    db, svc, bot = await world()
    sid = await musicq.session_start(db, CHAT, 5)
    rid, _, _ = await musicq.add(db, CHAT, title="멤버곡", url="u", vid="v", duration=100, by_id=MEMBER.id, by_name="멤버")
    await musicq.start_next(db, CHAT)
    t = asyncio.create_task(worker_answers(db, {"music_skip": "skipped", "music_end": "ended", "music_pause": "paused"}))
    assert "신청한 분만" in (await cmd(svc, bot, ".스킵", OTHER)).replies[0]
    assert "넘겼어요" in (await cmd(svc, bot, ".스킵", MEMBER)).replies[0], "그 곡 신청자는 됨"
    assert "일시정지" in (await cmd(svc, bot, ".일시정지", ADMIN, Role.ADMIN)).replies[0]
    assert "관리자만" in (await cmd(svc, bot, ".노래끝", OTHER)).replies[0]
    assert "나왔어요" in (await cmd(svc, bot, ".노래끝", MEMBER)).replies[0], "남은 곡이 전부 내 곡이면 끝낼 수 있음"
    await db.set_setting(CHAT, "music_ctrl", "all")
    assert "넘겼어요" in (await cmd(svc, bot, ".스킵", OTHER)).replies[0]
    t.cancel()
    await musicq.session_end(db, sid, "end", 1)
    assert "없어요" in (await cmd(svc, bot, ".스킵", ADMIN, Role.ADMIN)).replies[0]


@test
async def seek_volume_args_parse():
    db, svc, bot = await world()
    await musicq.session_start(db, CHAT, 1)
    seen = []

    async def answers():
        for _ in range(300):
            for j in await store.take_jobs(db):
                import json
                seen.append((j["kind"], j["payload"]))
                await store.finish(db, j["id"], True, {"music_seek": "seek:90", "music_volume": "volume:50"}[j["kind"]])
            await asyncio.sleep(0.01)
    t = asyncio.create_task(answers())
    assert "1:30" in (await cmd(svc, bot, ".이동 1:30", ADMIN, Role.ADMIN)).replies[0]
    await cmd(svc, bot, ".이동 -15", ADMIN, Role.ADMIN)
    assert "50%" in (await cmd(svc, bot, ".볼륨 50", ADMIN, Role.ADMIN)).replies[0]
    assert "초로" in (await cmd(svc, bot, ".이동 앞으로", ADMIN, Role.ADMIN)).replies[0]
    t.cancel()
    assert seen[0] == ("music_seek", {"value": 90, "relative": False})
    assert seen[1] == ("music_seek", {"value": -15, "relative": True})
    assert seen[2][1]["value"] == 50


@test
async def queue_and_remove_commands():
    db, svc, bot = await world()
    for t, by in (("지금곡", 5), ("둘째", 5), ("셋째", 6)):
        await musicq.add(db, CHAT, title=t, url="u", vid=t, duration=95, by_id=by, by_name=f"사람{by}")
    await musicq.start_next(db, CHAT)
    out = (await cmd(svc, bot, ".대기열")).replies[0]
    assert "🎶 지금: <b>지금곡</b> (1:35)" in out and "1. 둘째" in out and "2. 셋째" in out, out
    assert "내가 신청한 곡만" in (await cmd(svc, bot, ".빼기 2", MEMBER)).replies[0]
    assert "뺐어요" in (await cmd(svc, bot, ".빼기 2", ADMIN, Role.ADMIN)).replies[0]
    assert "없어요" in (await cmd(svc, bot, ".빼기 5", ADMIN, Role.ADMIN)).replies[0]


@test
async def card_buttons_and_session_end_notice():
    db, svc, bot = await world()
    sid = await musicq.session_start(db, CHAT, 5)
    await musicq.add(db, CHAT, title="곡", url="u", vid="v", duration=100, by_id=MEMBER.id, by_name="멤버")
    await musicq.start_next(db, CHAT)
    t = asyncio.create_task(worker_answers(db, {"music_skip": "skipped"}))
    q = FakeQuery(CHAT, OTHER, "mu:skip")
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append((text, show_alert))
    q.answer = answer
    await M.on_button(svc, bot, q, ["skip"])
    assert answers[-1][1] and "신청한 분만" in answers[-1][0], "남의 곡 카드 버튼은 거절"
    q.from_user = MEMBER
    await M.on_button(svc, bot, q, ["skip"])
    assert "넘겼어요" in answers[-1][0] and any("넘겼어요" in x[2] for x in bot.named("send_message"))
    t.cancel()
    await musicq.session_end(db, sid, "idle", 3)
    await M.tick(svc, bot)
    await M.tick(svc, bot)
    notes = [x for x in bot.named("send_message") if "나왔어요" in x[2]]
    assert len(notes) == 1 and "틀 노래가 없어서" in notes[0][2] and "3곡" in notes[0][2]
    sid2 = await musicq.session_start(db, CHAT, 5)
    await musicq.session_end(db, sid2, "end", 1)
    await M.tick(svc, bot)
    assert len([x for x in bot.named("send_message") if "나왔어요" in x[2]]) == 1, "끝내기는 이미 답했음 → 안내 없음"


@test
async def ai_tool_play_is_quiet_and_core():
    db, svc, bot = await world()
    await ready(db)
    t = tools._BY_NAME["music"]
    assert t.where == "room" and "music" in tools.CORE_TOOLS and not t.name in tools.READ_ONLY
    c = tools.ToolCtx(svc, bot, CHAT, MEMBER, Role.MEMBER, {})
    out = await M.t_music(c, {"action": "play", "query": "뉴진스 하입보이"})
    assert c.quiet and "접수" in out
    assert any("찾는 중" in x[2] for x in bot.named("send_message"))
    out = await M.t_music(tools.ToolCtx(svc, bot, CHAT, MEMBER, Role.MEMBER, {}), {"action": "now"})
    assert "없음" in out


@test
async def telegram_audio_reply_is_downloaded_into_music_dir():
    db, svc, bot = await world()
    await ready(db)
    audio_file = SimpleNamespace(file_id="F1", file_unique_id="U1", file_size=1000, duration=61, title="내 노래",
                                 performer="가수", file_name="song.mp3")
    src = SimpleNamespace(audio=audio_file, voice=None, document=None)
    saved = []

    async def get_file(fid):
        async def download_to_drive(path):
            Path(path).write_bytes(b"ID3")
            saved.append(path)
        return SimpleNamespace(download_to_drive=download_to_drive)
    bot.get_file = get_file
    t = asyncio.create_task(worker_answers(db, {"music_play": "playing"}))
    bot.member_status = {(CHAT, 4242): "member"}
    msg = await cmd(svc, bot, ".노래", reply_to=src)
    assert "가수 - 내 노래" in msg.replies[0], msg.replies
    job = None
    for _ in range(200):
        job = await db._one("SELECT * FROM voice_jobs WHERE kind='music_play'")
        if job:
            break
        await asyncio.sleep(0.01)
    t.cancel()
    assert saved and saved[0].endswith("music/tg_U1.mp3") and Path(saved[0]).parent == Path(svc.cfg.db_path).parent / "music"


@test
async def owner_cookie_upload_validates_and_is_private():
    db, svc, bot = await world()
    svc.perms.owner_ids = {7}
    s = await M.s_owner(PanelCtx(svc, bot, 5, None, []))
    assert "오너만" in s.text
    good = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc\n"
    assert M.valid_cookies("# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tHSID\tx\n"), \
        "크롬 확장이 쓰는 HttpOnly 줄"
    assert M.valid_cookies(good) and not M.valid_cookies("hello") and not M.valid_cookies(".example.com\tTRUE\t/\tx\t1\ta\tb")
    bot.files["DOC"] = good.encode()
    msg = FakeMsg(7, fake_user(7, "오너"), document=SimpleNamespace(file_id="DOC", file_size=len(good)))
    M.cookie_dir(svc).mkdir(parents=True, exist_ok=True)
    (M.cookie_dir(svc) / "cookies_1.txt").write_text(good)    # 기한 끝난 옛 쿠키
    checked, real_check = [], M.COOKIE_CHECK
    M.COOKIE_CHECK = lambda path: checked.append(path) or True
    try:
        ok, text = await M.i_cookie(PanelCtx(svc, bot, 7, 0, []), msg)
    finally:
        M.COOKIE_CHECK = real_check
    assert ok and msg.deleted and "확인" in text and checked, text
    files = list(M.cookie_dir(svc).glob("*.txt"))
    assert files[0].name != "cookies_1.txt", "새 쿠키를 넣으면 옛 쿠키는 지움"
    assert len(files) == 1 and oct(files[0].stat().st_mode)[-3:] == "600"
    src = music.Source(Path(svc.cfg.db_path).parent)
    assert src.cookies() == [str(files[0])], "음성 담당이 같은 곳에서 읽음"
    bot.files["BAD"] = b"not cookies"
    ok, text = await M.i_cookie(PanelCtx(svc, bot, 7, 0, []), FakeMsg(7, fake_user(7, "오너"),
                                                                       document=SimpleNamespace(file_id="BAD", file_size=11)))
    assert not ok and "아니에요" in text
    M.COOKIE_CHECK = lambda path: False
    msg = FakeMsg(7, fake_user(7, "오너"), document=SimpleNamespace(file_id="DOC", file_size=len(good)))
    try:
        ok, text = await M.i_cookie(PanelCtx(svc, bot, 7, 0, []), msg)
    finally:
        M.COOKIE_CHECK = real_check
    assert ok and "막혀요" in text and "robots.txt" in text, ("넣자마자 막히면 바로 알려 줌", text)


@test
def youtube_retries_next_cookie_only_when_blocked():
    class DownloadError(Exception):
        pass
    calls = []

    class YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import sys
    fake = SimpleNamespace(YoutubeDL=YDL, utils=SimpleNamespace(DownloadError=DownloadError))
    old = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = fake
    try:
        d = tempfile.mkdtemp()
        y = music.Source(Path(d))
        (Path(d) / "music_auth").mkdir()
        (Path(d) / "music_auth" / "a.txt").write_text("x")

        (Path(d) / "music_auth" / "b.txt").write_text("x")

        def blocked(ydl):
            calls.append(ydl.opts.get("cookiefile"))
            if ydl.opts.get("cookiefile"):          # 쿠키 둘 다 막힘 → 마지막에 쿠키 없이
                raise DownloadError("ERROR: [youtube] x: Sign in to confirm you're not a bot")
            return "ok"
        assert y._run(blocked) == "ok" and calls[-1] is None and len(calls) == 3
        assert set(calls[:2]) == {str(Path(d) / "music_auth" / n) for n in ("a.txt", "b.txt")}, "쿠키부터"
        assert os.environ["DENO_DIR"].startswith(d) or "DENO_DIR" in os.environ
        calls.clear()

        seen = []                                    # 우회 길(WARP)이 있으면 쿠키 없이 그 길 먼저, 죽었으면 예전 순서
        os.environ["MUSIC_PROXY"] = "socks5://127.0.0.1:40000"
        try:
            assert y._run(lambda ydl: seen.append((ydl.opts.get("proxy"), ydl.opts.get("cookiefile"))) or "ok") == "ok"
            assert seen == [("socks5://127.0.0.1:40000", None)], ("쿠키 안 쓰고 우회 길로", seen)
            seen.clear()

            def dead_proxy(ydl):
                seen.append((ydl.opts.get("proxy"), ydl.opts.get("cookiefile")))
                if ydl.opts.get("proxy"):
                    raise DownloadError("ERROR: Unable to connect to proxy")
                return "ok"
            assert y._run(dead_proxy) == "ok" and seen[0][0] and not seen[1][0] and seen[1][1], ("길이 죽으면 쿠키로", seen)
        finally:
            os.environ.pop("MUSIC_PROXY", None)

        def always(ydl):
            calls.append(1)
            raise DownloadError("ERROR: Sign in to confirm you're not a bot")
        try:
            y._run(always)
            raise AssertionError("실패해야 함")
        except music.MusicError as e:
            assert e.code == "blocked" and len(calls) == 3
        calls.clear()

        def gone(ydl):
            calls.append(1)
            raise DownloadError("ERROR: Video unavailable")
        try:
            y._run(gone)
            raise AssertionError("실패해야 함")
        except music.MusicError as e:
            assert e.code == "download" and calls == [1], "막힌 게 아니면 다른 쿠키로 다시 안 함"
    finally:
        if old is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = old


class FakeYDL:
    """yt_dlp 가짜: 검색·정보·받기를 표대로 (서버 실측 모양: 막힌 IP = 유튜브 받기 'Sign in', SoundCloud 공식 음원 = DRM)."""
    SEARCH: dict = {}
    DRM: set = set()
    YT_BLOCKED = True
    log: list = []

    class DownloadError(Exception):
        pass

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.log.append(url)
        m = re.match(r"(yt|sc)search\d+:(.*)", url, re.S)
        if m:
            return {"entries": FakeYDL.SEARCH.get((m.group(1), m.group(2)), [])}
        if "soundcloud" in url and url.rsplit("A", 1)[-1] in FakeYDL.DRM:
            raise FakeYDL.DownloadError("ERROR: [soundcloud] 1: This video is DRM protected")
        if "youtube.com/watch" in url and FakeYDL.YT_BLOCKED:
            raise FakeYDL.DownloadError("ERROR: [youtube] x: Sign in to confirm you're not a bot")
        return {"id": "x", "title": "t", "duration": 200}

    def download(self, urls):
        self.extract_info(urls[0])
        out = self.opts["outtmpl"].replace("%(id)s", urls[0].rsplit("=", 1)[-1]).replace("%(ext)s", "m4a")
        Path(out).write_bytes(b"a")


def sc(i, title, dur):
    return {"id": str(i), "title": title, "duration": dur}


def with_fake_ydl(fn):
    import sys
    old = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = SimpleNamespace(YoutubeDL=FakeYDL, utils=SimpleNamespace(DownloadError=FakeYDL.DownloadError))
    try:
        return fn()
    finally:
        if old is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = old


@test
def blocked_primary_falls_back_to_the_same_song_or_says_not_found():
    FakeYDL.SEARCH = {
        ("yt", "아이유 밤편지"): [{"id": "EjMTw4xLcBI", "title": "아이유(IU) - 밤편지 [가사/Lyrics]", "duration": 254}],
        ("sc", "아이유(IU) - 밤편지"): [sc(5, "아이유 - 밤편지", 600), sc(1, "HANNI (하니) - 밤편지 아이유 cover", 254),
                                     sc(2, "밤편지 - 아이유 (Live)", 260), sc(6, "밤편지 아이유 하니 듣기 모음", 252),
                                     sc(3, "밤편지 - 아이유", 252), sc(4, "아이유 밤편지 1시간 듣기", 3600)],
        ("yt", "BTS dynamite"): [{"id": "gdZLi9oWNZg", "title": "BTS (방탄소년단) 'Dynamite' Official MV", "duration": 229}],
        ("sc", "BTS (방탄소년단) 'Dynamite'"): [sc(10, "BTS Butter", 655), sc(11, "BTS (방탄소년단) - Dynamite", 199),
                                              sc(12, "BTS Dynamite", 237)],
        ("sc", "BTS dynamite"): [],
        ("yt", "뉴진스 하입보이"): [{"id": "11cta61wi0g", "title": "NewJeans (뉴진스) 'Hype Boy' Official MV", "duration": 178},
                               {"id": "hypeboylyr1", "title": "NewJeans (뉴진스) 'Hype Boy (하입보이)' 가사 (Color Coded Lyrics)",
                                "duration": 179}],   # 서버 실측 2026-10-08 순서
        ("sc", "NewJeans (뉴진스) 'Hype Boy (하입보이)'"): [sc(21, "NewJeans - Hype Boy (Remix)", 175), sc(20, "NewJeans - Hype Boy", 179),
                                               sc(22, "윈터 - Hype Boy Acoustic Ver", 175)],
        ("sc", "뉴진스 하입보이"): [],
    }
    FakeYDL.DRM = {"11"}                             # 공식 음원은 잠김

    def run():
        d = tempfile.mkdtemp()
        y = music.Source(Path(d))
        got = y.resolve("아이유 밤편지")
        assert got["vid"] == "EjMTw4xLcBI" and y.primary_ok(), "검색만 했을 땐 아직 막힌 줄 모름 → 유튜브 곡"
        try:
            y.fetch("EjMTw4xLcBI")
            raise AssertionError("막혀야 함")
        except music.MusicError as e:
            assert e.code == "blocked" and not y.primary_ok(), "받기가 막히면 그 뒤로 SoundCloud 먼저"
        got = y.resolve("아이유 밤편지")
        assert got["vid"] == "sc3", ("커버·라이브·1시간 듣기 말고 원곡", got)
        got = y.resolve("BTS dynamite")
        assert got["vid"] == "sc12" and got["url"].endswith("A12"), ("버터(다른 곡·11분)·DRM 공식 빼고", got)
        FakeYDL.DRM = {"11", "20"}
        try:
            y.resolve("뉴진스 하입보이")
            raise AssertionError("원곡이 잠겼으면 리믹스·커버를 틀지 않음")
        except music.MusicError as e:
            assert e.code == "not_found" and "쿠키" in str(e)
        FakeYDL.DRM = {"11"}
        got = y.resolve("뉴진스 하입보이")
        assert got["vid"] == "sc20", ("한글 신청 ↔ 영어 제목 (기준 곡 제목의 가수·노래로)", got)
        assert y.fetch("sc12").endswith("sc12.m4a") and y.fetch("sc12").endswith("sc12.m4a")
        alt = y.fallback("아이유(IU) - 밤편지 [가사/Lyrics]", 254)
        assert alt["vid"] == "sc3"
        FakeYDL.SEARCH[("sc", "BTS (방탄소년단) 'Dynamite'")] = [sc(30, "BTS - Butter", 229), sc(31, "방탄 노래 모음", 229)]
        try:
            y.fallback("BTS (방탄소년단) 'Dynamite' Official MV", 229)
            raise AssertionError("길이가 같아도 다른 곡이면 안 틂")
        except music.MusicError as e:
            assert e.code == "not_found"
    with_fake_ydl(run)
    assert music.clean_title("[MV] Paul Kim(폴킴) _ Me After You(너를 만나)") == "Paul Kim(폴킴) Me After You(너를 만나)"
    assert music.clean_title("NewJeans (뉴진스) 'Hype Boy' Official MV") == "NewJeans (뉴진스) 'Hype Boy'"


@test
async def player_swaps_blocked_track_for_the_same_song_elsewhere():
    db = await make_db()
    clock = Clock()
    d = tempfile.mkdtemp()

    class Src(FakeSource):
        def fetch(self, vid):
            if not vid.startswith("sc"):
                raise music.MusicError("blocked", "막힘")
            return super().fetch(vid)

        def fallback(self, title, ref_sec=0):
            self.asked = (title, ref_sec)
            return {"title": "밤편지 - 아이유", "url": music.ALT_TRACK.format(3), "vid": "sc3", "duration": 252}
    said = []

    async def say(kind, chat_id, row, why=""):
        said.append((kind, dict(row)))

    async def send(f):
        pass

    async def sleep(s):
        clock.t += s
        await asyncio.sleep(0.001)
    src = Src(d)
    pl = music.Player(db, CHAT, send, source=src, announce=say, decoder=FakeDecoder, clock=clock, sleep=sleep, poll=0.02)
    await musicq.add(db, CHAT, title="아이유(IU) - 밤편지 [가사/Lyrics]", url="u", vid="EjMTw4xLcBI", duration=254,
                     by_id=1, by_name="a")
    task = asyncio.create_task(pl.run())
    await until(lambda: said)
    assert said[0][0] == "now" and said[0][1]["vid"] == "sc3" and "soundcloud" in said[0][1]["url"]
    assert "soundcloud" not in musicq.card_text("now", said[0][1]).lower(), "출처는 안 보임"
    assert src.asked == ("아이유(IU) - 밤편지 [가사/Lyrics]", 254)
    row = await db._one("SELECT * FROM music_queue")
    assert row["vid"] == "sc3" and row["title"] == "밤편지 - 아이유"
    pl.stop("end")
    await task


# ── 리뷰로 찾은 버그 (2026-10-08 검사 담당 재현) ─────────────────
async def fast_player(db, src=None, **kw):
    clock = Clock()

    async def sleep(s):
        clock.t += s
        await asyncio.sleep(0.001)
    sent = []

    async def send(f):
        sent.append(f)
    said = []

    async def say(kind, chat_id, row, why=""):
        said.append(kind)
    pl = music.Player(db, CHAT, send, source=src or FakeSource(tempfile.mkdtemp()), announce=say, decoder=kw.pop("decoder", FakeDecoder),
                      clock=clock, sleep=sleep, poll=0.02, **kw)
    return pl, said


@test
async def stop_while_taking_next_song_or_fetching_does_not_hang():
    db = await make_db()
    await musicq.add(db, CHAT, title="a", url="u", vid="aaaaaaaaaaa", duration=100, by_id=1, by_name="x")
    pl, said = await fast_player(db)
    real = musicq.start_next

    async def stop_in_between(db_, chat):
        row = await real(db_, chat)
        pl.stop("end")                                  # 다음 곡을 꺼내는 그 순간 '노래끝'
        return row
    musicq.start_next = stop_in_between
    try:
        await asyncio.wait_for(pl.run(), 3)
    finally:
        musicq.start_next = real
    assert pl.done and not said and pl.dec is None, "멈춤 신호를 지워 영원히 기다리던 것"

    await musicq.clear(db, CHAT)
    await musicq.add(db, CHAT, title="b", url="u", vid="bbbbbbbbbbb", duration=100, by_id=1, by_name="x")

    class Slow(FakeSource):
        def fetch(self, vid):
            pl2.stop("restart")                          # 받는 사이 배포 재시작
            return super().fetch(vid)
    pl2, said2 = await fast_player(db, Slow(tempfile.mkdtemp()))
    await asyncio.wait_for(pl2.run(), 3)
    assert not said2, "끝난 뒤에 '재생 시작' 카드를 올리지 않음"


@test
async def stalled_decoder_moves_on_instead_of_silence_forever():
    db = await make_db()

    class Stuck(FakeDecoder):
        def frame(self):
            return None

        @property
        def finished(self):
            return False
    await musicq.add(db, CHAT, title="깨진곡", url="u", vid="ccccccccccc", duration=1100, by_id=1, by_name="x")
    old = music.STALL_FRAMES
    music.STALL_FRAMES = 50
    try:
        pl, said = await fast_player(db, decoder=Stuck, idle_sec=0.5)
        await asyncio.wait_for(pl.run(), 5)
    finally:
        music.STALL_FRAMES = old
    row = await db._one("SELECT state FROM music_queue")
    assert row["state"] == "failed" and "failed" in said, row["state"]


@test
async def volume_zero_is_zero_and_status_message_kept_on_join_failure():
    db, w, _, _ = await make_worker()
    FakeDecoder.LEN = 3000
    try:
        await play(db, w, "곡")
        await until(lambda: CHAT in w.players)
        assert (await run_job(db, w, "music_volume", {"value": 0}, CHAT))["result"] == "volume:0"
        assert w.players[CHAT].volume == 0
        assert (await run_job(db, w, "music_loop", {"value": 0}, CHAT))["result"] == "loop:0"
    finally:
        FakeDecoder.LEN = 40
        await stop_all(w)
    await run_job(db, w, "music_end", {}, CHAT)
    w.calls.fail = type("NoActiveGroupCall", (Exception,), {})()
    row = await play(db, w, "실패곡", status=91)
    assert row["result"] == "no_voice_chat" and not w.bot.named("delete"), "봇이 이유로 고칠 수 있게 '찾는 중' 글을 안 지움"


@test
async def restart_keeps_whole_queue_of_restarted_room_and_drops_others():
    db = await make_db()
    old = int(time.time()) - 7200
    for t in ("지금", "q1", "q2"):
        rid, _, _ = await musicq.add(db, CHAT, title=t, url="u", vid=t, duration=100, by_id=1, by_name="x")
        await db._write("UPDATE music_queue SET ts=? WHERE id=?", (old, rid))   # 2시간 전에 신청한 긴 대기열
    await musicq.start_next(db, CHAT)
    sid = await musicq.session_start(db, CHAT, 1)
    await musicq.session_end(db, sid, "restart", 3)
    await musicq.add(db, -200, title="딴방", url="u", vid="z", duration=100, by_id=1, by_name="x")
    assert await musicq.restart_chats(db, int(time.time()) - 1800) == [CHAT]
    await musicq.drop_except(db, [CHAT])
    rows = await db._all("SELECT chat_id, title, state FROM music_queue ORDER BY id")
    assert [(r["title"], r["state"]) for r in rows] == [("지금", "playing"), ("q1", "queued"), ("q2", "queued"), ("딴방", "removed")]


@test
async def old_dj_cleanup_does_not_wipe_a_new_request():
    db, w, _, _ = await make_worker()
    FakeDecoder.LEN = 3000
    try:
        await play(db, w, "옛곡")
        await until(lambda: CHAT in w.players)
        old_pl, old_task = w.players[CHAT], w.ptasks[CHAT]
        cleared = []
        real = musicq.clear

        async def spy(db_, chat, reason="removed"):
            cleared.append(chat)
            return await real(db_, chat, reason)
        musicq.clear = spy
        try:
            new = SimpleNamespace(done=False, stop=lambda r="": None, tracks=0)
            old_pl.stop("idle")
            w.players[CHAT] = new                            # 끝나는 사이 새 신청으로 새 DJ
            await old_task
        finally:
            musicq.clear = real
        assert w.players.get(CHAT) is new and not cleared and ("leave", CHAT) not in w.calls.log
        w.players.pop(CHAT)
    finally:
        FakeDecoder.LEN = 40


@test
def same_song_is_downloaded_once_even_when_asked_twice_at_once():
    import threading as th
    d = tempfile.mkdtemp()
    y = music.Source(Path(d))
    n = []

    def slow_fetch(vid):
        n.append(vid)
        time.sleep(0.2)
        p = Path(d) / "music" / f"{vid}.m4a"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"a")
        return str(p)
    real = y._fetch
    y._fetch = lambda vid: real(vid) if y._cached(vid) else slow_fetch(vid)
    ts = [th.Thread(target=y.fetch, args=("sc5",)) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert n == ["sc5"], n
    (Path(d) / "music" / "gone.m4a.part").write_bytes(b"x" * 10)
    y.cache_mb = 0
    y.trim()
    assert (Path(d) / "music" / "gone.m4a.part").exists(), "받는 중인 파일은 안 지움"


@test
def alt_source_words_any_script_and_variant_word_boundaries():
    FakeYDL.SEARCH = {
        ("sc", "残酷な天使のテーゼ"): [sc(40, "Totally Different Song", 240), sc(41, "残酷な天使のテーゼ - 高橋洋子", 241)],
        ("sc", "未知の歌"): [sc(42, "Totally Different Song", 240)],
        ("sc", "drivers license"): [sc(43, "Olivia Rodrigo - drivers license (Deluxe Edition)", 242),
                                    sc(44, "drivers license (Remix)", 240)],
        ("sc", "beyonce halo"): [sc(45, "Beyoncé - Halo", 261)]}
    FakeYDL.DRM = set()

    def run():
        y = music.Source(Path(tempfile.mkdtemp()))
        alt = lambda q: y.alt_for(None, music.mm.parse(q))     # 기준 곡 없음(검색 실패) → 신청 낱말 전부
        assert alt("残酷な天使のテーゼ")["vid"] == "sc41", "일본어 제목도 낱말로 비교"
        try:
            alt("未知の歌")
            raise AssertionError("낱말이 하나도 안 맞으면 아무 곡이나 틀지 않음")
        except music.MusicError as e:
            assert e.code == "not_found"
        assert alt("drivers license")["vid"] == "sc43", "'edition' 은 'edit' 변형이 아님"
        assert alt("beyonce halo")["vid"] == "sc45", "악센트 무시"
        assert music._words("아이유 Beyoncé") == ["아이유", "beyonce"], "한글은 자모로 안 쪼갬 (NFKD 뒤 NFC)"
        FakeYDL.SEARCH[("sc", "♪ 노")] = [sc(46, "Totally Different Song", 240)]
        try:
            alt("♪ 노")
            raise AssertionError("비교할 낱말이 없으면(기호·한 글자) 아무 곡이나 통과시키지 않음")
        except music.MusicError as e:
            assert e.code == "not_found"
    with_fake_ydl(run)


@test
async def slow_result_leaves_the_status_message_to_the_worker():
    db, svc, bot = await world()
    await ready(db)
    bot.member_status = {(CHAT, 4242): "member"}
    real = P._wait

    async def slow(db_, jid, timeout=0):
        return "failed", "slow"
    P._wait = slow
    try:
        await M.request(svc, bot, CHAT, MEMBER, query="밤편지", status_msg=55)
    finally:
        P._wait = real
    assert not bot.named("edit_text") and not [x for x in bot.named("send_message") if "느" in x[2]]


@test
def music_text_people_see_never_names_the_source():
    """오너 결정 2026-10-08: 방에 보이는 글·안내서·도구 설명·명령 설명에 음원 사이트 이름을 안 씀."""
    assert music._hide_src("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable https://www.youtube.com/watch?v=x") == "Video unavailable"
    assert "soundcloud" not in music._hide_src("ERROR: [soundcloud] 12: This video is DRM protected").lower()
    assert music._hide_src("ERROR: [youtube:tab] PLx: YouTube said: The playlist does not exist.") == "The playlist does not exist."
    root = Path(__file__).resolve().parent.parent
    bad = ("유튜브", "youtube", "soundcloud", "사클")
    guide = (root / "sodam/guide/music.md").read_text().lower()
    body = guide.split("---", 2)[2]                      # tags 의 '유튜브링크' 는 찾기용 낱말 (본문엔 없음)
    assert not [w for w in bad if w in body], "안내서 본문에 출처"
    t = tools._BY_NAME["music"]
    desc = t.description + str(t.params)
    assert not [w for w in bad if w in desc.lower()], desc
    card = musicq.card_text("now", {"title": "밤편지", "duration": 250, "url": music.ALT_TRACK.format(3)})
    assert not [w for w in bad if w in card.lower()], card
    for f in ("sodam/voice/music.py", "sodam/panels/music.py", "sodam/voice/musicq.py"):
        said = re.findall(r"MusicError\([^)]*\)|return Screen\([^)]*\)|ctx\.reply\([^)]*\)", (root / f).read_text())
        assert not [x for x in said if any(w in x.lower() for w in bad)], (f, said)


@test
async def no_exact_song_posts_choice_buttons_only_the_requester_picks_once():
    """'김연지 별이될께' 처럼 딱 맞는 곡이 없으면 엉뚱한 곡을 틀지 않고 신청 글을 고르기 버튼으로 (서버 실측 2026-10-08)."""
    bot = FakeBot()
    db, w, src, _ = await make_worker(bot)
    row = await play(db, w, "고르기 첫째", status=77)
    assert row["result"] == "music:choice" and not await musicq.current(db, CHAT), "틀지 않음"
    edit = [c for c in bot.named("edit_text") if c[1] == CHAT][-1]
    assert "딱 맞는 곡" in edit[2] and edit[3].get("reply_markup"), edit
    datas = [b.callback_data for r in edit[3]["reply_markup"].inline_keyboard for b in r]
    assert datas[-1].endswith(":x") and all(len(d.encode()) <= 64 for d in datas), datas
    ch = await db._one("SELECT * FROM music_choices")
    assert ch["msg_id"] == 77 and ch["by_id"] == 5

    _, svc, pbot = await world()
    svc.db = db
    await ready(db)
    pbot.member_status = {(CHAT, 4242): "member"}
    from sodam.panels import music as Mp

    def q(user, data):
        x = FakeQuery(CHAT, user, data)
        x.message.message_id = 77
        return x
    other = q(OTHER, datas[0])
    await Mp.on_button(svc, pbot, other, datas[0].split(":")[1:])
    assert other.answers[0][1] and "신청한 분" in other.answers[0][0] and not (await musicq.get_choice(db, ch["id"]))["used"]
    stale = FakeQuery(CHAT, MEMBER, datas[0])
    stale.message.message_id = 999
    await Mp.on_button(svc, pbot, stale, datas[0].split(":")[1:])
    assert "지난 버튼" in stale.answers[0][0], "다른 글의 버튼(옛 카드)"
    import json
    taken, real_take = [], store.take_jobs

    async def take_jobs(*a, **k):                         # 가져가면 payload 를 지우므로 (서버가 빠르면 먼저 가져감) 가져간 걸 기록
        got = await real_take(*a, **k)
        taken.extend({"kind": j["kind"], "payload": json.dumps(j["payload"], ensure_ascii=False), "by_user": j["by"]} for j in got)
        return got
    store.take_jobs = take_jobs
    try:
        t = asyncio.create_task(worker_answers(db, {"music_play": "playing"}))
        mine = q(MEMBER, datas[1])
        await Mp.on_button(svc, pbot, mine, datas[1].split(":")[1:])
        again = q(MEMBER, datas[0])
        await Mp.on_button(svc, pbot, again, datas[0].split(":")[1:])
        job = None
        for _ in range(1000):
            job = next((j for j in taken if j["kind"] == "music_play" and "pick" in (j.get("payload") or "")), None) \
                or await db._one("SELECT * FROM voice_jobs WHERE kind='music_play' AND payload LIKE '%pick%'")
            if job:
                break
            await asyncio.sleep(0.01)
        t.cancel()
    finally:
        store.take_jobs = real_take
    assert "준비 중" in mine.edits[0] and "이미 골랐" in again.answers[0][0], (mine.edits, again.answers)
    import json
    pl = json.loads(job["payload"])
    assert pl["pick"]["vid"] == "aaaaaaaaaa2" and job["by_user"] == MEMBER.id and pl["status_msg"] == 77
    await stop_all(w)


@test
async def picked_song_is_checked_again_by_the_worker_and_queued():
    bot = FakeBot()
    db, w, src, _ = await make_worker(bot)
    bad = await run_job(db, w, "music_play", {"pick": {"vid": "../../etc/x", "title": "x"}, "by": 5, "by_name": "멤버"}, CHAT)
    assert bad["result"] == "music:not_found" and not [x for x in src.resolved if x[0] == "pick"], "이상한 ID 는 안 받음"
    ok = await run_job(db, w, "music_play", {"pick": {"vid": "aaaaaaaaaa1", "title": "가수 - 첫째 곡", "duration": 200},
                                             "by": 5, "by_name": "멤버"}, CHAT)
    assert ok["result"] in ("playing", "queued") and ("pick", "aaaaaaaaaa1") in src.resolved
    await stop_all(w)


@test
async def choice_expires_and_cancel_works():
    db, svc, bot = await world()
    from sodam.panels import music as Mp
    cid = await musicq.save_choice(db, CHAT, MEMBER.id, "멤버", "블루문", "partial",
                                   [{"vid": "aaaaaaaaaa1", "title": "엔플라잉 - Blue Moon", "duration": 216, "kind": ""}])
    await musicq.set_choice_msg(db, cid, 5)
    x = FakeQuery(CHAT, MEMBER, f"mu:pk:{cid}:x")
    x.message.message_id = 5
    await Mp.on_button(svc, bot, x, ["pk", str(cid), "x"])
    assert "취소" in x.edits[0] and (await musicq.get_choice(db, cid))["used"]
    assert not await musicq.claim_choice(db, cid), "두 번째 차지는 실패 (동시에 눌러도 한 번만)"
    cid = await musicq.save_choice(db, CHAT, MEMBER.id, "멤버", "블루문", "partial",
                                   [{"vid": "aaaaaaaaaa1", "title": "엔플라잉 - Blue Moon", "duration": 216, "kind": ""}])
    await db._write("UPDATE music_choices SET ts=ts-? WHERE id=?", (musicq.CHOICE_SEC + 5, cid))
    y = FakeQuery(CHAT, MEMBER, f"mu:pk:{cid}:0")
    y.message.message_id = 5
    await Mp.on_button(svc, bot, y, ["pk", str(cid), "0"])
    assert "10분" in y.answers[0][0] and not (await musicq.get_choice(db, cid))["used"]
    z = FakeQuery(CHAT, MEMBER, f"mu:pk:{cid}:9")
    z.message.message_id = 5
    await db._write("UPDATE music_choices SET ts=? WHERE id=?", (int(time.time()), cid))
    await Mp.on_button(svc, bot, z, ["pk", str(cid), "9"])
    assert "지난 버튼" in z.answers[0][0] and not (await musicq.get_choice(db, cid))["used"], "없는 번호는 안 씀"


@test
async def same_song_twice_is_not_queued_again():
    db = await make_db()
    a = await musicq.add(db, CHAT, title="아로하", url="u", vid="sc62", duration=243, by_id=1, by_name="a")
    b = await musicq.add(db, CHAT, title="아로하", url="u", vid="sc62", duration=243, by_id=2, by_name="b")
    assert a[0] and b == (None, 0, "dup"), b
    await musicq.finish(db, a[0], "done")
    c = await musicq.add(db, CHAT, title="아로하", url="u", vid="sc62", duration=243, by_id=2, by_name="b")
    assert c[0], "끝난 곡은 다시 신청 가능"
    f1 = await musicq.add(db, CHAT, title="파일", url="", vid=None, duration=10, by_id=1, by_name="a", path="x")
    f2 = await musicq.add(db, CHAT, title="파일", url="", vid=None, duration=10, by_id=1, by_name="a", path="x")
    assert f1[0] and f2[0], "음악 파일(vid 없음)은 막지 않음"


@test
def ai_tool_says_play_again_means_resume_not_a_new_request():
    d = tools._BY_NAME["music"].description
    assert "resume" in d and "고르기 버튼" in d and "커버" in d


@test
def resolve_with_real_search_results_end_to_end():
    """서버 실측 검색 결과로 Source.resolve 전체 (기본 음원 막힘 → 대체 음원 '같은 노래')."""
    import json
    data = json.loads((Path(__file__).parent / "fixtures/music/search_20261008.json").read_text())
    FakeYDL.SEARCH = {("yt", q): v for q, v in data["yt"].items()}
    FakeYDL.SEARCH.update({("sc", q): v for q, v in data["sc"].items()})
    FakeYDL.DRM = set()

    def run():
        y = music.Source(Path(tempfile.mkdtemp()))
        assert y.resolve("아이유 밤편지 틀어줘")["title"].startswith("아이유(IU) - 밤편지"), "막히기 전엔 기본 음원 원곡"
        y.blocked_at = time.time()
        got = y.resolve("조정석 아로하")
        assert "CRAVITY" not in got["title"] and got["vid"].startswith("sc") and "조정석" in got["title"], got
        for q in ("좋은 발라드", "최유리 노래모음", "잔잔한 플리 하나 틀어줘"):
            try:
                y.resolve(q)
                raise AssertionError(q)
            except music.MusicError as e:
                assert e.code == "mix", (q, e)            # 분위기·모음 → 워커가 여러 곡 (예전: '못 틀어요')
        try:
            y.resolve("노래 틀어줘")
            raise AssertionError("아무 말도 없으면 되묻기")
        except music.MusicError as e:
            assert e.code == "not_found" and "분위기" in str(e), e
        try:
            y.resolve("김연지 별이될께")
            raise AssertionError("맨 위 '미친 사랑의 노래'(다른 곡)를 틀면 안 됨")
        except music.MusicChoice as c:
            assert "별이될께" in c.items[0]["title"] and len(c.items) <= 4, c.items
    with_fake_ydl(run)


@test
def alt_source_refuses_other_versions_and_singerless_titles():
    """대체 음원은 '같은 노래' 만 — 기준 곡과 낱말이 다 맞아도 라이브·괄호 버전·가수 없는 같은 제목은 안 틂."""
    FakeYDL.SEARCH = {
        ("sc", "아이유(IU) - 밤편지"): [sc(90, "아이유 - 밤편지 (Live)", 255)],
        ("sc", "아이유 밤편지"): [],
        ("sc", "말달리자 -- 크라잉 넛"): [sc(91, "말달리자 (Run your horse)", 190)],
        ("sc", "말달리자 크라잉 넛"): [], ("sc", "말 달리자"): [sc(91, "말달리자 (Run your horse)", 190)],
        ("sc", "잔나비 - 주저하는 연인들을 위해"): [sc(92, "잔나비 - 주저하는 연인들을 위해 (강희선성우ver)", 266)],
        ("sc", "잔나비 주저하는 연인들을 위해"): [],
    }
    FakeYDL.DRM = set()

    def run():
        y = music.Source(Path(tempfile.mkdtemp()))
        for ref, req, dur in (("아이유(IU) - 밤편지 [가사/Lyrics]", "아이유 밤편지", 254),
                              ("말달리자 -- 크라잉 넛", "말 달리자", 188),
                              ("잔나비 - 주저하는 연인들을 위해", "잔나비 주저하는 연인들을 위해", 270)):
            try:
                got = y.alt_for(ref, music.mm.parse(req), dur)
                raise AssertionError((req, got))
            except music.MusicError as e:
                assert e.code == "not_found", e
        assert y.alt_for("아이유(IU) - 밤편지 [가사/Lyrics]", music.mm.parse("아이유 라이브 밤편지"), 254)["vid"] == "sc90", \
            "라이브를 원하면 라이브도"
    with_fake_ydl(run)
    assert music.mm.kind("설윤 (SULLYOON) - 밤편지 (Original Song by IU (아이유))", known="아이유(IU) - 밤편지") == "cover", \
        "기준 곡 가수 이름이 'by' 뒤에 있어도 'Original Song by' 는 다른 가수"
    assert music.mm.kind("린&찬혁이 부르는 AKMU의 '어떻게 이별까지 사랑하겠어'") == "cover"


if __name__ == "__main__":
    run_all()


@test
def loud_mix_is_softly_limited_and_gain_glides_both_ways():
    """'사운드가 튀네요' (2026-10-08): 크게 녹음된 곡 + 목소리 + 볼륨 200% → 딱딱 잘림(지직), 목소리 끝나면 한 번에 커짐(쿵)."""
    out = np.frombuffer(music.mix(np.full(480, 20000, "<i2").tobytes(), np.full(480, 10000, "<i2").tobytes(), 1.0), "<i2")
    assert music.LIMIT < int(out[0]) < 30000, ("한계 넘는 소리는 부드럽게 눌림", int(out[0]))
    quiet = np.frombuffer(music.mix(np.full(480, 8000, "<i2").tobytes(), None, 1.0), "<i2")
    assert int(quiet[0]) == 8000, "작은 소리는 그대로"
    g, steps = music.DUCK, 0
    while g < 1.0:
        g = music.glide(g, 1.0)
        steps += 1
    assert steps >= 15, ("다시 키울 땐 천천히 (0.15초↑)", steps)
    assert music.glide(1.0, music.DUCK) > music.DUCK, "줄일 때도 한 조각에 다 X"
    ramp = np.frombuffer(music.mix(np.full(480, 8000, "<i2").tobytes(), None, 0.5, 1.0), "<i2")
    assert ramp[0] > ramp[-1] and abs(int(ramp[-1]) - 4000) < 50, "조각 안에서 매끄럽게"


@test
async def short_gaps_between_voice_frames_keep_music_ducked():
    db = await make_db()
    clock = Clock()
    sent = []

    async def send(f):
        sent.append(int(np.frombuffer(f, "<i2")[-1]))

    async def sleep(s):
        clock.t += s
        await asyncio.sleep(0.001)
    Const.LEN = 100000
    pl = music.Player(db, CHAT, send, source=FakeSource(tempfile.mkdtemp()), decoder=Const, clock=clock, sleep=sleep, poll=0.02)
    await musicq.add(db, CHAT, title="t", url="u", vid="vvvvvvvvvvv", duration=999, by_id=1, by_name="a")
    task = asyncio.create_task(pl.run())
    await until(lambda: 8000 in sent)
    for _ in range(10):
        pl.voice_frame(np.full(480, 1000, "<i2").tobytes())
    await until(lambda: not pl.voice)
    n = len(sent)
    await until(lambda: len(sent) >= n + 15)
    assert all(x <= 2100 for x in sent[n + 1:n + 15]), ("말 사이 0.15초 틈엔 노래를 다시 안 키움", sent[n:n + 15])
    pl.stop("end")
    await task


class Gappy(Const):
    """20 조각마다 3 조각 비는 소리 (풀기가 잠깐 밀림)."""
    n = 0

    def frame(self):
        Gappy.n += 1
        if Gappy.n % 23 in (0, 1, 2):
            return None
        return super().frame()


@test
async def underrun_fades_out_instead_of_cutting_and_fades_back_in():
    db = await make_db()
    clock = Clock()
    frames = []

    async def send(f):
        frames.append(np.frombuffer(f, "<i2").copy())

    async def sleep(s):
        clock.t += s
        await asyncio.sleep(0.001)
    Const.LEN = 100000
    pl = music.Player(db, CHAT, send, source=FakeSource(tempfile.mkdtemp()), decoder=Gappy, clock=clock, sleep=sleep, poll=0.02)
    await musicq.add(db, CHAT, title="t", url="u", vid="vvvvvvvvvvv", duration=999, by_id=1, by_name="a")
    task = asyncio.create_task(pl.run())
    await until(lambda: pl.stats["underrun"] >= 3 and len(frames) > 80)
    pl.stop("end")
    await task
    for a, b in zip(frames, frames[1:]):
        jump = abs(int(b[0]) - int(a[-1]))
        assert jump <= 1500, ("조각 사이에 소리가 한 번에 크게 바뀌지 않음 ('딱')", int(a[-1]), int(b[0]))
