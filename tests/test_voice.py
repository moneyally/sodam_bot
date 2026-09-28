"""📞 음성채팅 (sodam/voice · panels/voice.py). 네트워크·py-tgcalls 없이 가짜로. python tests/run_all.py voice"""
import asyncio
import base64
import tempfile
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
from fakes import FakeMsg, fake_user, make_db, make_svc, runner

from sodam import tools
from sodam.menu import PanelCtx
from sodam.panels import voice as P
from sodam.permissions import Role
from sodam.voice import audio, store
from sodam.voice.bridge import SEND_BYTES, Bridge
from sodam.voice.worker import Worker

test, run_all = runner()
CHAT = -100555
P._sleep = lambda s: asyncio.sleep(0.01)


def tone(ms: int, rate: int = audio.TG_RATE, amp: int = 8000) -> bytes:
    n = rate * ms // 1000
    return (np.sin(np.arange(n) / 8) * amp).astype("<i2").tobytes()


# ── 소리 변환 ─────────────────────────────────────────────
@test
def audio_rates_and_mix():
    a = tone(10)
    assert len(a) == audio.FRAME_BYTES == 960
    assert len(audio.down(a)) == 480 and len(audio.up(audio.down(a))) == 960
    back = np.frombuffer(audio.up(audio.down(a)), "<i2").astype(int)
    assert np.abs(back - np.frombuffer(a, "<i2")).max() < 1500           # 모양이 거의 그대로
    loud = np.full(480, 30000, "<i2").tobytes()
    assert set(np.frombuffer(audio.mix([loud, loud]), "<i2")) == {30000}  # 섞어도 안 넘침 (평균)
    assert audio.mix([]) == b"" and audio.level(bytes(960)) == 0


# ── 다리 (가짜 Realtime) ──────────────────────────────────
class FakeConn:
    def __init__(self):
        self.sent, self.q = [], asyncio.Queue()
        me = self

        class NS:
            def __init__(self, name):
                self.name = name

            def __getattr__(self, attr):
                if attr in ("item",):
                    return NS(self.name + ".item")

                async def call(**kw):
                    me.sent.append((f"{self.name}.{attr}", kw))
                return call
        self.session, self.response = NS("session"), NS("response")
        self.input_audio_buffer, self.conversation = NS("input_audio_buffer"), NS("conversation")

    def named(self, name):
        return [kw for n, kw in self.sent if n == name]

    def push(self, **ev):
        self.q.put_nowait(SimpleNamespace(**ev))

    def __aiter__(self):
        return self

    async def __anext__(self):
        ev = await self.q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


def make_bridge(**kw):
    conn, played = FakeConn(), []

    @asynccontextmanager
    async def connect():
        yield conn

    async def play(f):
        played.append(f)
    return Bridge(connect, play, instructions="너는 소담", **kw), conn, played


async def until(cond, secs=2.0):
    t = time.monotonic()
    while not cond() and time.monotonic() - t < secs:
        await asyncio.sleep(0.01)
    assert cond()


@test
async def bridge_listens_speaks_and_is_interrupted():
    b, conn, played = make_bridge(greet="인사")
    task = asyncio.create_task(b.run())
    await until(lambda: conn.named("response.create"))
    cfg = conn.named("session.update")[0]["session"]
    assert cfg["audio"]["output"]["voice"] == "marin" and cfg["audio"]["input"]["format"]["rate"] == 24000
    assert cfg["audio"]["input"]["turn_detection"]["create_response"] is True
    for _ in range(25):                                                   # 250 ms 들음 → 100 ms 씩 2번 보냄
        b.feed([tone(10), tone(10)])
    await until(lambda: len(conn.named("input_audio_buffer.append")) == 2)
    assert len(base64.b64decode(conn.named("input_audio_buffer.append")[0]["audio"])) == SEND_BYTES
    # 소담 말: 24k 500 ms → 48k 10 ms 조각 50개
    conn.push(type="response.output_audio.delta", item_id="it1", delta=base64.b64encode(tone(500, audio.AI_RATE)).decode())
    await until(lambda: b.played_ms >= 100)
    assert any(f != bytes(960) for f in played) and all(len(f) == 960 for f in played)
    conn.push(type="input_audio_buffer.speech_started", audio_start_ms=0, item_id="u2")   # 누가 끼어듦
    await until(lambda: conn.named("conversation.item.truncate"))
    tr = conn.named("conversation.item.truncate")[0]
    assert tr["item_id"] == "it1" and 0 < tr["audio_end_ms"] < 500 and not b.out
    conn.push(type="conversation.item.input_audio_transcription.completed", transcript="소담아 이제 나가도 돼", item_id="u2")
    res = await asyncio.wait_for(task, 2)
    assert res.reason == "bye" and res.user_turns == 1


@test
async def bridge_name_mode_answers_only_when_called_and_idles_out():
    b, conn, _ = make_bridge(reply="name", idle_sec=0.3)
    task = asyncio.create_task(b.run())
    await until(lambda: conn.named("session.update"))
    assert conn.named("session.update")[0]["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
    conn.push(type="conversation.item.input_audio_transcription.completed", transcript="오늘 날씨 좋다")
    await asyncio.sleep(0.05)
    assert not conn.named("response.create"), "안 불렀으면 대답 안 함"
    conn.push(type="conversation.item.input_audio_transcription.completed", transcript="소담 너는 어때?")
    await until(lambda: conn.named("response.create"))
    res = await asyncio.wait_for(task, 3)
    assert res.reason == "idle"


@test
async def bridge_stops_at_time_limit_and_connect_error():
    b, _, _ = make_bridge(max_sec=0.2, idle_sec=99)
    assert (await asyncio.wait_for(b.run(), 3)).reason == "time"

    @asynccontextmanager
    async def bad():
        raise OSError("nope")
        yield

    async def play(f):
        pass
    res = await Bridge(bad, play, instructions="x").run()
    assert res.reason.startswith("error:")


# ── DB 일감 ───────────────────────────────────────────────
@test
async def jobs_once_and_stale_fail():
    db = await make_db()
    j = await store.add_job(db, CHAT, "start", {"a": 1}, 7)
    assert j and await store.add_job(db, CHAT, "start") is None             # 연타
    got = await store.take_jobs(db)
    assert [g["id"] for g in got] == [j] and got[0]["payload"] == {"a": 1} and not await store.take_jobs(db)
    await store.finish(db, j, True, "started")
    row = await store.job(db, j)
    assert row["status"] == "done" and row["payload"] == "{}"
    k = await store.add_job(db, CHAT, "stop")
    assert await store.expire_stale(db, now=time.time() + store.JOB_TTL + 5) == 1
    assert (await store.job(db, k))["result"] == "no_worker"


# ── worker (가짜 텔레그램·py-tgcalls) ─────────────────────
class PasswordNeeded(Exception):
    pass


PasswordNeeded.__name__ = "SessionPasswordNeededError"


class NoActiveGroupCall(Exception):
    pass


class FakeClient:
    def __init__(self, need_pw=False):
        self.need_pw, self.session = need_pw, SimpleNamespace(save=lambda: "SESSION")
        self.signed = []

    async def connect(self):
        pass

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="h")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.signed.append((phone, code, password))
        if code and self.need_pw:
            raise PasswordNeeded()

    async def get_me(self):
        return SimpleNamespace(id=4242, first_name="소담", last_name="음성", username="sodam_voice")

    async def __call__(self, req):
        return None


class FakeCalls:
    def __init__(self, client):
        self.log, self.fail = [], None

    async def start(self):
        pass

    async def play(self, chat_id, stream, config):
        if self.fail:
            raise self.fail
        self.log.append(("play", chat_id, stream, config))

    async def record(self, chat_id, stream):
        self.log.append(("record", chat_id, stream))

    async def send_frame(self, chat_id, device, data):
        self.log.append(("frame", chat_id))

    async def leave_call(self, chat_id):
        self.log.append(("leave", chat_id))


MEDIA = SimpleNamespace(Device=SimpleNamespace(MICROPHONE="mic"), AudioParameters=lambda r, c: (r, c),
                        MediaStream=lambda src, p: ("media", src, p), ExternalMedia=SimpleNamespace(AUDIO="ext"),
                        GroupCallConfig=lambda auto_start: {"auto_start": auto_start},
                        RecordStream=lambda a, p: ("record", a, p))


async def make_worker(need_pw=False):
    db = await make_db()
    d = tempfile.mkdtemp()
    cfg = SimpleNamespace(db_path=f"{d}/sodam.db", mtproto_api_id=1, mtproto_api_hash="x")
    client = FakeClient(need_pw)
    conn = FakeConn()

    @asynccontextmanager
    async def rt(model):
        yield conn
    w = Worker(cfg, db, client_factory=lambda *a: client, calls_factory=FakeCalls, realtime_connect=rt, media=MEDIA)
    return db, w, client, conn


async def run_job(db, w, kind, payload, chat=0):
    jid = await store.add_job(db, chat, kind, payload, 7)
    await w.step()
    return await store.job(db, jid)


@test
async def worker_login_with_password_writes_session_and_assistant():
    db, w, client, _ = await make_worker(need_pw=True)
    assert (await run_job(db, w, "login_phone", {"phone": "+821000000000"}))["result"] == "code_sent"
    assert (await run_job(db, w, "login_code", {"code": "12345"}))["result"] == "need_password"
    row = await run_job(db, w, "login_pw", {"password": "pw"})
    assert row["status"] == "done" and row["payload"] == "{}", "비밀번호는 처리하고 지움"
    assert (await store.assistant(db))["id"] == 4242 and await store.worker_alive(db)
    from sodam.voice.worker import session_path
    assert session_path(w.cfg).read_text().strip() == "SESSION" and oct(session_path(w.cfg).stat().st_mode)[-3:] == "600"


@test
async def worker_start_stop_records_call():
    db, w, _, conn = await make_worker()
    await run_job(db, w, "login_phone", {"phone": "+821000000000"})
    await run_job(db, w, "login_code", {"code": "12345"})
    row = await run_job(db, w, "start", {"instructions": "x", "max_sec": 30}, CHAT)
    assert row["result"] == "started", row["result"]
    play = next(x for x in w.calls.log if x[0] == "play")
    assert play[3] == {"auto_start": True} and play[2] == ("media", "ext", (48000, 1))    # 음성채팅 없으면 켜기
    assert any(x[0] == "record" for x in w.calls.log) and await store.active_call(db, CHAT)
    assert (await run_job(db, w, "start", {}, CHAT))["result"] == "already"
    await until(lambda: any(x[0] == "frame" for x in w.calls.log))
    assert (await run_job(db, w, "stop", {}, CHAT))["result"] == "stopped"
    assert not await store.active_call(db, CHAT) and ("leave", CHAT) in w.calls.log
    [call] = await store.ended_unnotified(db)
    assert call["reason"] == "admin"


@test
async def worker_no_voice_chat_is_reported():
    db, w, _, _ = await make_worker()
    await run_job(db, w, "login_phone", {"phone": "+821000000000"})
    await run_job(db, w, "login_code", {"code": "12345"})
    w.calls.fail = NoActiveGroupCall()
    assert (await run_job(db, w, "start", {}, CHAT))["result"] == "no_voice_chat"
    assert not w.bridges and not await store.active_call(db, CHAT)
    w2 = Worker(w.cfg, db)
    assert (await run_job(db, w2, "start", {}, CHAT))["result"] == "no_assistant"


# ── 봇 쪽 ────────────────────────────────────────────────
async def world(who="admin"):
    db = await make_db()
    svc = await make_svc(db, admins=(1,))
    await db.ensure_chat(CHAT, "테스트방")
    await db.set_setting(CHAT, "voice_who", who)
    from fakes import FakeBot
    bot = FakeBot()
    promoted = []

    async def promote(chat_id, user_id, **kw):
        promoted.append((chat_id, user_id, kw))
    bot.promote_chat_member = promote
    bot.promoted = promoted
    return db, svc, bot


async def fake_worker(db, answers):
    """worker 대신 일감에 답함."""
    for _ in range(300):
        for j in await store.take_jobs(db):
            res = answers.get(j["kind"], "done")
            if j["kind"] == "start" and res == "started":
                await store.call_started(db, j["chat_id"], j["by"])
            await store.finish(db, j["id"], True, res)
        await asyncio.sleep(0.01)


def ctx(svc, bot, uid, role):
    return tools.ToolCtx(svc, bot, CHAT, fake_user(uid, "대표"), role, {})


@test
async def tool_refuses_member_and_without_assistant():
    db, svc, bot = await world("admin")
    out = await P.t_voice_call(ctx(svc, bot, 5, Role.MEMBER), {"action": "start"})
    assert "관리자만" in out
    out = await P.t_voice_call(ctx(svc, bot, 1, Role.ADMIN), {"action": "start"})
    assert "준비가 아직 안 됐" in out and "방송 키" in out
    await db.set_state(0, store.ASSISTANT_KEY, {"id": 4242, "name": "소담 음성", "username": None})
    out = await P.t_voice_call(ctx(svc, bot, 1, Role.ADMIN), {"action": "start"})
    assert "꺼져" in out, "worker 가 안 돌면"
    assert tools._BY_NAME["voice_call"].where == "room"


@test
async def start_invites_assistant_promotes_and_posts_result():
    db, svc, bot = await world("all")
    await db.set_state(0, store.ASSISTANT_KEY, {"id": 4242, "name": "소담 음성", "username": None})
    await db.set_state(0, store.WORKER_BEAT, time.time())
    bot.member_status = {(CHAT, 4242): "left"}
    w = asyncio.create_task(fake_worker(db, {"join": "joined", "start": "started"}))
    c = ctx(svc, bot, 5, Role.MEMBER)
    out = await P.t_voice_call(c, {"action": "start"})
    assert c.quiet and "들어가는 중" in out
    await until(lambda: any("들어왔어요" in x[2] for x in bot.named("send_message")), 5)
    w.cancel()
    inv = bot.named("invite_link")[0][2]
    assert inv["member_limit"] == 1 and inv["expire_date"] > time.time()
    assert bot.promoted and bot.promoted[0][2] == {"can_manage_video_chats": True}
    start = await db._one("SELECT * FROM voice_jobs WHERE kind='start'")
    assert start["payload"] == "{}", "끝난 일은 지움"
    assert "여자" in P.PERSONA and P.VOICE == "marin"
    out = await P.t_voice_call(ctx(svc, bot, 5, Role.MEMBER), {"action": "start"})
    assert "이미" in out


@test
async def ended_call_notice_once_and_month_cap():
    db, svc, bot = await world("admin")
    cid = await store.call_started(db, CHAT, 1)
    await store.call_ended(db, cid, P.MONTH_MIN * 60, "idle")
    await P.tick(svc, bot)
    await P.tick(svc, bot)
    notes = [x for x in bot.named("send_message") if "나왔어요" in x[2]]
    assert len(notes) == 1 and "조용해서" in notes[0][2]
    await db.set_state(0, store.ASSISTANT_KEY, {"id": 4242, "name": "a"})
    await db.set_state(0, store.WORKER_BEAT, time.time())
    assert "다 썼어요" in await P.precheck(svc, CHAT, 1, Role.ADMIN)


@test
async def owner_login_flow_deletes_secrets_and_is_owner_only():
    db, svc, bot = await world()
    svc.perms.owner_ids = {7}
    s = await P.s_owner(PanelCtx(svc, bot, 5, None, []))
    assert s.text is None and "오너" in s.toast
    c = PanelCtx(svc, bot, 7, 0, [])
    assert "연결 안 됨" in (await P.s_owner(c)).text
    w = asyncio.create_task(fake_worker(db, {"login_phone": "code_sent", "login_code": "need_password", "login_pw": "ok:소담"}))
    m = FakeMsg(7, fake_user(7), "+82 10-1234-5678")
    ok, text = await P.i_phone(c, m)
    assert ok and m.deleted and "띄어서" in text
    await P.s_after(c)
    assert svc.inputs[7].kind == "vcc"
    m = FakeMsg(7, fake_user(7), "1 2 3 4 5")
    ok, text = await P.i_code(c, m)
    assert m.deleted and "비밀번호" in text
    await P.s_after(c)
    assert svc.inputs[7].kind == "vcpw"
    m = FakeMsg(7, fake_user(7), "secret")
    ok, text = await P.i_password(c, m)
    w.cancel()
    assert m.deleted and "✅" in text
    rows = await db._all("SELECT payload FROM voice_jobs")
    assert all(r["payload"] == "{}" for r in rows) and "secret" not in str([dict(r) for r in rows])
    ok, text = await P.i_code(c, FakeMsg(7, fake_user(7), "12"))
    assert not ok


@test
async def call_cost_goes_into_daily_budget():
    db = await make_db()
    from sodam import costs
    micro = await store.record_cost(db, None, CHAT, 120)
    day = time.strftime("%Y-%m-%d")
    assert micro == int(2 * store.USD_PER_MIN * 1e6)
    assert await db.counter(day, 0, costs.USD) == micro and await db.counter(day, CHAT, costs.ROOM_USD) == micro


# ── 📡 방송 모드 (계정 없이 RTMP) ───────────────────────────
import os
import shutil
import subprocess

from sodam import hooks
from sodam.voice import radio as R


@test
async def radio_real_ffmpeg_writes_audio_and_video():
    if not shutil.which("ffmpeg"):
        return
    out = os.path.join(tempfile.mkdtemp(), "live.flv")
    r = R.Radio(out, max_sec=1.5, idle_sec=99)
    await r.start()
    r.say(tone(600, R.RATE))
    assert (await asyncio.wait_for(r.run(), 10)) == "time"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                            "-of", "csv=p=0", out], capture_output=True, text=True).stdout
    assert "aac,audio" in probe and "h264,video" in probe, probe
    assert 0.5 < r.spoken_sec < 0.7


@test
def rtmp_target_joins_url_and_key():
    assert R.target_of("rtmps://dc4-1.rtmp.t.me/s/", "123:abc") == "rtmps://dc4-1.rtmp.t.me/s/123:abc"
    assert R.target_of("rtmps://dc4-1.rtmp.t.me/s", " 123:abc ") == "rtmps://dc4-1.rtmp.t.me/s/123:abc"


class FakeRadio:
    made = []

    def __init__(self, target, max_sec=0, idle_sec=0):
        self.target, self.said, self.spoken_sec = target, [], 0.0
        self.ev = asyncio.Event()
        FakeRadio.made.append(self)

    async def start(self):
        pass

    def say(self, pcm):
        self.said.append(pcm)
        self.spoken_sec += len(pcm) / 48000

    def stop(self, reason):
        self.reason = reason
        self.ev.set()

    async def run(self):
        await self.ev.wait()
        return self.reason


@test
async def worker_radio_needs_no_account():
    db = await make_db()
    cfg = SimpleNamespace(db_path=f"{tempfile.mkdtemp()}/s.db", mtproto_api_id=0, mtproto_api_hash="")

    async def tts(text):
        return b"\1\0" * 24000                                     # 1초
    w = Worker(cfg, db, tts=tts, radio_factory=FakeRadio)          # 도우미 계정·py-tgcalls 없음
    row = await run_job(db, w, "radio_start", {"url": "rtmps://dc4-1.rtmp.t.me/s/", "key": "1:k", "greet": "안녕"}, CHAT)
    assert row["result"] == "radio_started" and FakeRadio.made[-1].target.endswith("/s/1:k")
    assert row["payload"] == "{}", "방송 키도 처리 뒤 지움"
    await run_job(db, w, "radio_say", {"text": "반가워요"}, CHAT)
    assert len(FakeRadio.made[-1].said) == 2
    assert (await run_job(db, w, "stop", {}, CHAT))["result"] == "stopped"
    [call] = await store.ended_unnotified(db)
    assert call["seconds"] == 2 and call["reason"] == "admin", dict(call)     # 한 달 한도엔 말한 시간만
    assert (await run_job(db, w, "radio_say", {"text": "x"}, CHAT))["result"] == "not_live"


@test
async def rtmp_key_input_checks_and_hides():
    db, svc, bot = await world()
    c = PanelCtx(svc, bot, 1, CHAT, [])
    m = FakeMsg(1, fake_user(1), "https://evil.example/s/\n123:abc")
    ok, _ = await P.i_rtmp(c, m)
    assert not ok and m.deleted and not await db.get_state(CHAT, store.RTMP_KEY)
    m = FakeMsg(1, fake_user(1), "rtmps://dc4-1.rtmp.t.me/s/\n1234567:AbCdEfGh")
    ok, _ = await P.i_rtmp(c, m)
    assert ok and m.deleted and (await db.get_state(CHAT, store.RTMP_KEY))["key"] == "1234567:AbCdEfGh"
    screen = await P.s_room(c)
    assert "등록됨" in screen.text and "AbCdEfGh" not in screen.text, "키는 화면에 안 보임"


@test
async def no_account_but_key_starts_radio_speaks_answers_and_hears_voice():
    db, svc, bot = await world("all")
    await db.set_state(0, store.WORKER_BEAT, time.time())
    assert "방송 키 등록" in await P.precheck(svc, CHAT, 5, Role.MEMBER), "둘 다 없으면 두 길 안내"
    await db.set_state(CHAT, store.RTMP_KEY, {"url": "rtmps://dc4-1.rtmp.t.me/s/", "key": "1:k"})
    w = asyncio.create_task(fake_worker(db, {"radio_start": "radio_started", "radio_say": "said"}))
    orig = fake_worker
    c = ctx(svc, bot, 5, Role.MEMBER)
    assert "들어가는 중" in await P.t_voice_call(c, {"action": "start"})
    await until(lambda: any("방송 준비" in x[2] for x in bot.named("send_message")), 5)
    w.cancel()
    await store.call_started(db, CHAT, 5)                                # (가짜 worker 대신) 방송 중 표시
    assert await db.get_state(CHAT, store.MODE_KEY) == "radio"
    await P.on_ai_answer(svc, bot, CHAT, "첫째 답")
    await P.on_ai_answer(svc, bot, CHAT, "둘째 답")
    says = await db._all("SELECT id FROM voice_jobs WHERE kind='radio_say'")
    assert len(says) == 2, "말하기는 줄 세움 (연타 막기 없음)"

    class Tr:
        async def create(self, **kw):
            assert kw["model"] == P.STT_MODEL and kw["language"] == "ko"
            return SimpleNamespace(text="오늘 날씨 어때")
    recorded = []

    async def _check(chat_id):
        pass

    async def _record(*a, **k):
        recorded.append(k)
    svc.llm = SimpleNamespace(client=SimpleNamespace(audio=SimpleNamespace(transcriptions=Tr())),
                              _check_budget=_check, _record=_record)

    async def get_file(fid):
        async def dl():
            return bytearray(b"OggS...")
        return SimpleNamespace(download_as_bytearray=dl)
    bot.get_file = get_file
    vm = FakeMsg(CHAT, fake_user(5), "")
    vm.voice, vm.video_note = SimpleNamespace(file_id="f", duration=4), None
    assert await hooks.voice_text(svc, bot, vm) == "소담아 오늘 날씨 어때"
    assert recorded and recorded[0]["extra_micro"] > 0, "받아쓰기 요금도 예산에"
    vm.voice = SimpleNamespace(file_id="f", duration=90)
    assert await hooks.voice_text(svc, bot, vm) is None, "60초 넘는 음성은 안 받아씀"
    await db.set_state(CHAT, store.MODE_KEY, None)
    vm.voice = SimpleNamespace(file_id="f", duration=4)
    assert await hooks.voice_text(svc, bot, vm) is None, "방송 중이 아니면 음성메시지는 그대로(비용 0)"
