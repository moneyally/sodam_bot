"""📞 GPT-Live 다리 (sodam/voice/live.py, VOICE_ENGINE=live). 가짜 Live 연결 — 이벤트 이름·필드는 openai-python 3.19
`types/live` 그대로. 기존 Realtime 경로(bridge.py)는 test_voice.py. python tests/run_all.py voice_live"""
import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace as N

import numpy as np
from fakes import make_db, runner

from sodam.voice import audio, store
from sodam.voice.live import BARGE_SEC, LiveBridge, live_config

test, run_all = runner()
CHAT = -100556


def tone(ms: int, rate: int = audio.TG_RATE, amp: int = 8000) -> bytes:
    n = rate * ms // 1000
    return (np.sin(np.arange(n) / 8) * amp).astype("<i2").tobytes()


class Path_:
    """conn.session.input_audio.append(**kw) 처럼 몇 단계든 — 부르면 (점 이름, kw) 기록."""

    def __init__(self, conn, name):
        self._conn, self._name = conn, name

    def __getattr__(self, attr):
        return Path_(self._conn, f"{self._name}.{attr}" if self._name else attr)

    async def __call__(self, **kw):
        self._conn.sent.append((self._name, kw))
        if self._name == "session.close" and self._conn.answer_close:   # 진짜 서버처럼 마지막 사용량과 함께 closed
            self._conn.push(type="session.closed", reason="close_requested", usage=N(seconds=self._conn.final_sec), session=N())


class FakeLive:
    def __init__(self):
        self.sent, self.q = [], asyncio.Queue()
        self.session, self.response = Path_(self, "session"), Path_(self, "response")
        self.answer_close, self.final_sec = True, 0.0

    def named(self, name):
        return [kw for n, kw in self.sent if n == name]

    def push(self, **ev):
        self.q.put_nowait(N(**ev))

    def __aiter__(self):
        return self

    async def __anext__(self):
        ev = await self.q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


def make(**kw):
    conns, played = [], []

    @asynccontextmanager
    async def connect():
        c = FakeLive()
        conns.append(c)
        yield c

    async def play(f):
        played.append(f)
    b = LiveBridge(connect, play, instructions="너는 소담", **kw)
    return b, conns, played


async def until(cond, secs=2.0):
    t = time.monotonic()
    while not cond() and time.monotonic() - t < secs:
        await asyncio.sleep(0.01)
    assert cond()


def started(c):
    c.push(type="session.started", session=N(id="sess_1"))


@test
def config_matches_sdk_shape():
    cfg = live_config("너는 소담", "cedar", reply="name", tools=[{"type": "function", "name": "web_search"}])
    assert cfg["model"] == "gpt-live-1" and cfg["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert cfg["audio"]["output"]["voice"] == "cedar"
    d = cfg["delegation"]
    assert d["type"] == "responses" and d["responses"]["model"] == "gpt-6-luna"
    assert d["responses"]["tools"][0]["name"] == "web_search" and d["responses"]["parallel_tool_calls"] is False
    assert d["responses"]["tools"][0]["strict"] is False, "선택 인자가 있는 우리 스키마 — strict 면 거절될 수 있음"
    assert "Selected requests" in cfg["instructions"] and "Delegation policy" in cfg["instructions"]
    assert "Selected requests" not in live_config("x", "marin")["instructions"], "기본은 늘 대답"
    for k in ("turn_detection", "temperature", "max_output_tokens", "tools"):   # Live session 에 없는 필드
        assert k not in cfg and k not in cfg["audio"]


@test
async def audio_waits_for_started_then_flows_and_greets():
    b, conns, played = make(greet="한 문장 인사")
    task = asyncio.create_task(b.run())
    await until(lambda: conns and conns[0].named("session.start"))
    c = conns[0]
    for _ in range(25):
        b.feed([tone(10)])
    await asyncio.sleep(0.05)
    assert not c.named("session.input_audio.append"), "session.started 전엔 소리 안 보냄 (문서)"
    assert not c.named("response.create"), "Realtime 식 인사 없음"
    started(c)
    await until(lambda: len(c.named("session.input_audio.append")) == 2)
    assert c.named("session.instructions.append")[0]["content"].startswith("한 문장 인사")
    assert c.named("session.instructions.append")[0]["delegation_id"] is None
    c.push(type="session.output_audio.delta", delta=base64.b64encode(tone(300, audio.AI_RATE)).decode())
    await until(lambda: any(f != bytes(960) for f in played))
    b.stop("admin")
    res = await asyncio.wait_for(task, 2)
    assert res.reason == "admin" and res.stats["engine"] == "live"


@test
async def transcript_deltas_become_lines_and_bye_ends():
    lines = []
    b, conns, _ = make(on_line=lambda who, text, ssrc: lines.append((who, text, ssrc)))
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    for ssrc in (7,) * 30:                                   # ssrc 7 이 말함
        b.feed([(ssrc, tone(10)), (8, bytes(960))])
    c.push(type="session.output_transcript.delta", delta="안녕하세요", start_ms=0, end_ms=50)
    for d in ("오늘 ", "날씨 ", "어때?"):
        c.push(type="session.input_transcript.delta", delta=d, start_ms=0, end_ms=100)
    c.push(type="session.output_transcript.delta", delta="맑아요", start_ms=200, end_ms=300)
    await until(lambda: ("user", "오늘 날씨 어때?", 7) in lines)
    c.push(type="session.input_transcript.delta", delta="소담아 이제 나가", start_ms=400, end_ms=500)
    res = await asyncio.wait_for(task, 2)
    assert res.reason == "bye" and res.user_turns == 2 and res.bot_turns == 2
    assert ("sodam", "맑아요", None) in lines
    assert [w for w, *_ in lines][:3] == ["sodam", "user", "sodam"], lines   # 말이 바뀌면 앞 줄을 끝냄 (순서 그대로)


@test
async def barge_in_drops_only_local_queue():
    b, conns, _ = make()
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    c.push(type="session.output_audio.delta", delta=base64.b64encode(tone(3000, audio.AI_RATE)).decode())
    await until(lambda: len(b.out) > 100)
    for _ in range(int(BARGE_SEC * 100) + 5):                # 사람이 0.3초 넘게 크게 말함
        b.feed([(9, tone(10, amp=20000))])
    assert not b.out and b.stats["interrupts"] == 1
    assert not c.named("conversation.item.truncate"), "Live 엔 truncate 없음"
    b.out.extend([("live", bytes(960))] * 10)
    b.feed([(9, tone(10, amp=20000))])                       # 짧은 소리 한 번은 안 끊음
    assert len(b.out) == 10
    b.out.extend([("live", bytes(960))] * 1000)
    for i in range(80):                                      # 실제 말처럼 사이사이 조용한 조각 (10개 중 3개)
        b.feed([(9, tone(10, amp=20000) if i % 10 >= 3 else bytes(960))])
    assert b.stats["interrupts"] == 2, "말 사이 짧은 정적이 있어도 끊김 (리뷰)"
    b.stop("admin")
    await asyncio.wait_for(task, 2)


@test
async def delegated_tool_runs_with_speaker_and_returns_to_backend():
    seen = []

    async def room_stats(args, meta):
        seen.append((args, meta))
        return "<tool_result>대화 120개</tool_result>"
    room_stats.wants_meta = True
    b, conns, _ = make(tools={"chat_stats": room_stats}, tool_specs=[{"type": "function", "name": "chat_stats"}])
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    for _ in range(20):
        b.feed([(42, tone(10))])
    c.push(type="session.input_transcript.delta", delta="오늘 대화 몇 개야", start_ms=0, end_ms=900)
    c.push(type="session.delegation.created", offset_ms=950,
           delegation=N(id="del_1", target="responses", type="delegation", response_id="resp_1"))
    c.push(type="response.event", delegation_id="del_1", event={
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": "call_9", "name": "chat_stats", "arguments": json.dumps({"period": "today"})}})
    await until(lambda: c.named("response.create"))
    assert seen == [({"period": "today"}, {"ssrc": 42, "response_id": "resp_1"})], seen
    out = c.named("response.item.create")[0]["item"]
    assert out == {"type": "function_call_output", "call_id": "call_9", "output": "<tool_result>대화 120개</tool_result>"}
    sent = [n for n, _ in c.sent]
    assert sent.index("response.item.create") < sent.index("response.create"), "결과 먼저, 그다음 이어가기 (가이드)"
    c.push(type="response.event", delegation_id="del_1", event={
        "type": "response.completed", "response": {"model": "gpt-6-luna-2026-09-01",
                                                   "usage": {"input_tokens": 900, "input_tokens_details": {"cached_tokens": 600},
                                                             "output_tokens": 40}, "output": []}})
    c.push(type="session.usage.updated", usage=N(seconds=30.0))
    c.push(type="session.usage.updated", usage=N(seconds=61.5))      # 누적값 — 더하지 않음
    await until(lambda: b.result.usage.get("live_seconds") == 61.5)
    b.stop("admin")
    res = await asyncio.wait_for(task, 2)
    u = res.usage
    assert (u["backend_in"], u["backend_cached"], u["backend_out"], u["tool_calls"]) == (900, 600, 40, 1), u
    micro, how = store.cost_micro("gpt-live-1", u, res.seconds)
    want = 61.5 / 60 * 0.05 + (300 * 0.10 + 600 * 0.01 + 40 * 0.50) / 1e6
    assert how == "live" and micro == int(want * 1e6), (micro, want)


@test
async def stop_closes_session_and_takes_final_seconds():
    b, conns, _ = make()
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    c.push(type="session.usage.updated", usage=N(seconds=50.0))
    await until(lambda: b.result.usage.get("live_seconds") == 50.0)
    c.final_sec = 57.5                                       # 마지막 갱신 뒤 7.5초 더 — closed 에만 있음
    b.stop("idle")
    res = await asyncio.wait_for(task, 3)
    assert c.named("session.close"), "끝낼 때 session.close (정산 확인)"
    assert res.usage["live_seconds"] == 57.5, res.usage


@test
async def silent_server_on_close_does_not_hang():
    import sodam.voice.live as L
    old = L.CLOSE_WAIT
    L.CLOSE_WAIT = 0.2
    try:
        b, conns, _ = make()
        task = asyncio.create_task(b.run())
        await until(lambda: conns)
        conns[0].answer_close = False
        started(conns[0])
        await until(lambda: b._started.is_set())
        b.stop("admin")
        res = await asyncio.wait_for(task, 2)
        assert res.reason == "admin"
    finally:
        L.CLOSE_WAIT = old


@test
async def speaker_is_taken_from_the_line_window_only():
    lines = []
    b, conns, _ = make(on_line=lambda who, text, ssrc: lines.append((who, text, ssrc)))
    clock = {"t": 1000.0}
    b.clock = lambda: clock["t"]
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    await until(lambda: b._started.is_set())
    for _ in range(300):                                     # 관리자(1)가 오래 크게 웃음 (소담이 말하던 때)
        b.feed([(1, tone(10, amp=20000))])
    clock["t"] += 10                                         # 10초 뒤 멤버(2)가 짧게 말함
    for _ in range(40):
        b.feed([(2, tone(10))])
    c.push(type="session.input_transcript.delta", delta="그 사람 밴해", start_ms=0, end_ms=400)
    await until(lambda: b._line_in)
    c.push(type="session.delegation.created", offset_ms=500,
           delegation=N(id="d9", target="responses", type="delegation", response_id="r9"))
    await until(lambda: "d9" in b._deleg)
    assert b._deleg["d9"]["ssrc"] == 2, "앞서 웃은 관리자 소리는 안 셈 (리뷰: 권한 오인)"
    b.stop("admin")
    await asyncio.wait_for(task, 3)
    assert ("user", "그 사람 밴해", 2) in lines, lines


@test
async def null_delegation_tool_has_no_speaker():
    seen = []

    async def warn(args, meta):
        seen.append(meta)
        return "ok"
    warn.wants_meta = True
    b, conns, _ = make(tools={"warn_member": warn})
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    b.speaker = 77                                            # 나중에 말한 사람이 있어도
    c.push(type="response.event", delegation_id=None, event={
        "type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c", "name": "warn_member", "arguments": "{}"}})
    await until(lambda: seen)
    assert seen[0]["ssrc"] is None, "어느 위임인지 모르면 말한 사람도 모름 → 쓰기 도구는 거절 쪽"
    b.stop("admin")
    await asyncio.wait_for(task, 3)


@test
async def unknown_tool_and_failures_do_not_break_call():
    b, conns, _ = make(tools={})
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    c = conns[0]
    started(c)
    c.push(type="response.event", delegation_id=None, event={
        "type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c1", "name": "nope", "arguments": "{"}})
    c.push(type="response.event", delegation_id=None, event={"type": "response.output_text.delta", "delta": "x"})
    c.push(type="error", error=N(code="immutable_field_update", message="no", param=None))
    await until(lambda: c.named("response.item.create"))
    assert "도구" in c.named("response.item.create")[0]["item"]["output"]
    assert not b.done and b.stats["rt_errors"].get("immutable_field_update") == 1
    for _ in range(6):                                       # 무해한 오류는 여러 번이어도 통화 계속
        c.push(type="error", error=N(code="immutable_field_update", message="no", param=None))
    await asyncio.sleep(0.1)
    assert not b.done
    b.stop("admin")
    await asyncio.wait_for(task, 2)


@test
async def connection_lost_reconnects_once_with_new_session():
    b, conns, _ = make(greet="인사")
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    started(conns[0])
    await until(lambda: conns[0].named("session.instructions.append"))
    conns[0].push(type="session.usage.updated", usage=N(seconds=40.0))
    conns[0].push(type="session.closed", reason="connection_lost", usage=N(seconds=41.0), session=N())
    await until(lambda: len(conns) == 2 and conns[1].named("session.start"))
    assert b.stats["reconnects"] == 1 and not b.done
    for _ in range(25):
        b.feed([tone(10)])
    await asyncio.sleep(0.05)
    assert not conns[1].named("session.input_audio.append"), "새 세션도 started 전엔 소리 안 보냄"
    started(conns[1])
    await until(lambda: conns[1].named("session.input_audio.append"))
    assert not conns[1].named("session.instructions.append"), "다시 연결해도 인사는 한 번만"
    conns[1].push(type="session.usage.updated", usage=N(seconds=5.0))
    await until(lambda: b.result.usage.get("live_seconds") == 46.0)     # 옛 세션 41 + 새 세션 5
    conns[1].push(type="session.closed", reason="connection_lost", usage=N(seconds=6.0), session=N())
    res = await asyncio.wait_for(task, 2)
    assert res.reason == "ws_closed" and len(conns) == 2, "두 번째 끊김은 끝"


@test
async def closed_reasons_map_to_end_reasons():
    for reason, want in (("expired", "time"), ("content", "content"), ("remote_hangup", "closed")):
        b, conns, _ = make()
        task = asyncio.create_task(b.run())
        await until(lambda: conns)
        started(conns[0])
        conns[0].push(type="session.closed", reason=reason, usage=N(seconds=3.0), session=N())
        res = await asyncio.wait_for(task, 2)
        assert res.reason == want, (reason, res.reason)


@test
async def start_timeout_ends_call():
    import sodam.voice.live as L
    old = L.START_TIMEOUT
    L.START_TIMEOUT = 0.1
    try:
        b, conns, _ = make()
        res = await asyncio.wait_for(b.run(), 2)
        assert res.reason == "error:start_timeout"
    finally:
        L.START_TIMEOUT = old


@test
async def idle_ends_live_call_even_with_no_vad():
    b, conns, _ = make(idle_sec=0.3)
    task = asyncio.create_task(b.run())
    await until(lambda: conns)
    started(conns[0])
    res = await asyncio.wait_for(task, 4)
    assert res.reason == "idle", "Live 는 조용해도 과금 → idle 로 끊어야"


@test
async def worker_picks_engine_and_charges_live_price():
    from sodam.voice import worker as W
    db = await make_db()
    w = W.Worker(SimpleCfg(), db, engine="live")
    assert w.engine == "live" and w.model == "gpt-live-1"
    assert W.Worker(SimpleCfg(), db).engine == "realtime", "기본은 지금 그대로"
    assert W.Worker(SimpleCfg(), db, engine="weird").engine == "realtime"
    micro = await store.record_cost(db, None, CHAT, 120, "gpt-live-1", {"live_seconds": 120.0})
    assert micro == int(120 / 60 * 0.05 * 1e6)
    assert store.cost_micro("gpt-live-1-2026-09-10", {"live_seconds": 60.0}, 60) == (int(0.05 * 1e6), "live")
    old_live = W.LIVE
    W.LIVE = "gpt-realtime-2.1"                              # live 엔진에 Live 아닌 모델 → realtime 으로 (연결 짝 안 맞음 방지)
    try:
        w = W.Worker(SimpleCfg(), db, engine="live")
        assert (w.engine, w.model) == ("realtime", W.MODEL)
    finally:
        W.LIVE = old_live


class SimpleCfg:
    tz = None
    openai_api_key = "x"


if __name__ == "__main__":
    asyncio.run(run_all())
