"""🎵 곡 사이: 다음 곡 미리 풀기 · 곡마다 소리 크기 맞추기 · 겹쳐 넘어가기 (sodam/voice/music.py Player).
2026-10-09 오너 '다음 넘어갈 때 자꾸 끊긴다' — 예전엔 앞 곡이 끝나야 다음 곡 풀기를 켜서 0.1~0.5초 빈틈 + '툭'."""
import asyncio
import json
import math
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
from fakes import make_db, runner

from sodam.voice import audio, music, musicq

test, run_all = runner()
CHAT = -100778
VAL = {"a": 1000, "b": 3000, "c": 6000, "d": 1000}             # 곡마다 다른 크기 → 보낸 조각으로 어느 곡인지 앎


class XDec:
    """곡마다 LEN 조각, 끝 EOF_AT 조각 남으면 '다 풀림'(진짜 Decoder 처럼 frames_left() 가 숫자)."""
    LEN, EOF_AT = 120, 30
    opened: list = []
    closed: list = []

    def __init__(self, path, start_ms=0):
        self.path, self.start_ms = path, start_ms
        self.name = Path(path).stem[0]
        self.rest = max(0, self.LEN - start_ms // 10)
        self.failed = ""
        XDec.opened.append(self.name)

    async def start(self):
        pass

    def frame(self):
        if self.rest <= 0:
            return None
        self.rest -= 1
        return np.full(audio.FRAME_BYTES // 2, VAL[self.name], dtype="<i2").tobytes()

    def frames_left(self):
        return self.rest if self.rest <= self.EOF_AT else None

    def ready(self):
        return True

    @property
    def finished(self):
        return self.rest <= 0

    async def close(self):
        XDec.closed.append(self.name)


class Src:
    def __init__(self, lufs=None):
        self.d, self.lufs = Path(tempfile.mkdtemp()), lufs or {}

    def fetch(self, vid):
        p = self.d / f"{vid}.webm"
        p.write_bytes(b"x")
        return str(p)

    def loudness(self, path):
        return self.lufs.get(Path(path).stem[0])


async def setup(names, *, src=None, on_send=None):
    db = await make_db()
    for n in names:
        await musicq.add(db, CHAT, title=n, url="u", vid=n * 11, duration=100, by_id=1, by_name="x")
    XDec.opened, XDec.closed = [], []
    t = [1000.0]

    async def sleep(s):
        t[0] += s
        await asyncio.sleep(0.001)
    sent = []
    holder = {}

    async def send(f):
        sent.append(np.frombuffer(f, dtype="<i2").copy())
        if on_send:
            on_send(holder["pl"], sent)
    pl = music.Player(db, CHAT, send, source=src or Src(), decoder=XDec, clock=lambda: t[0], sleep=sleep,
                      poll=0.02, idle_sec=0.3)
    holder["pl"] = pl
    return db, pl, sent


def heard(sent):
    """조각마다 들린 크기 (중간값)."""
    return [float(np.median(np.abs(f))) for f in sent]


def music_span(levels):
    on = [i for i, v in enumerate(levels) if v > 0]
    return on[0], on[-1]


@test
async def songs_overlap_with_no_gap_and_the_next_decoder_is_reused():
    db, pl, sent = await setup("ab")
    await asyncio.wait_for(pl.run(), 10)
    lv = heard(sent)
    first, last = music_span(lv)
    gap = [i for i in range(first, last) if lv[i] == 0]
    assert not gap, f"곡 사이에 무음 조각 없음 {gap[:5]}"
    assert pl.stats["xfade"] == 1, pl.stats
    assert XDec.opened == ["a", "b"], f"다음 곡은 미리 연 것을 그대로 (다시 안 엶) {XDec.opened}"
    mixed = [v for v in lv if v not in (0.0,) and abs(v - 1000) > 30 and abs(v - 3000) > 30]
    assert len(mixed) >= 10, "겹치는 동안은 두 곡이 섞인 소리"
    assert last - first + 1 <= 2 * XDec.LEN - XDec.EOF_AT + 5, "겹친 만큼 전체가 짧음 (빈틈 없음)"
    rows = await db._all("SELECT title, state FROM music_queue ORDER BY id")
    assert [(r["title"], r["state"]) for r in rows] == [("a", "done"), ("b", "done")], [dict(r) for r in rows]
    assert sorted(XDec.closed) == ["a", "b"], f"풀기는 전부 닫힘 {XDec.closed}"


@test
async def overlap_keeps_total_loudness_steady():
    db, pl, sent = await setup("ad")                      # 같은 크기 두 곡 → 겹치는 동안도 크기가 확 줄거나 늘지 않음
    await asyncio.wait_for(pl.run(), 10)
    lv = heard(sent)
    first, last = music_span(lv)
    body = lv[first + 60: last - 5]                        # 처음 키우는 구간 빼고
    assert min(body) > 1000 * 0.95 and max(body) < 1000 * 1.45, (min(body), max(body))


@test
async def skip_during_overlap_continues_the_incoming_song():
    state = {"done": False}

    def on_send(pl, sent):
        if pl._xf is not None and pl._xf["i"] > 5 and not state["done"]:
            state["done"] = True
            assert pl.skip()
    db, pl, sent = await setup("ab", on_send=on_send)
    await asyncio.wait_for(pl.run(), 10)
    assert state["done"]
    assert XDec.opened == ["a", "b"], XDec.opened
    rows = await db._all("SELECT title, state FROM music_queue ORDER BY id")
    assert [(r["title"], r["state"]) for r in rows] == [("a", "skipped"), ("b", "done")]
    lv = heard(sent)
    first, last = music_span(lv)
    assert not [i for i in range(first, last) if lv[i] == 0], "넘겨도 다음 곡이 끊기지 않고 이어짐"


@test
async def queue_change_before_the_end_is_respected():
    state = {"done": False}

    def on_send(pl, sent):
        if pl._next is not None and not state["done"]:
            state["done"] = True
            asyncio.get_running_loop().create_task(musicq.remove_nth(pl.db, CHAT, 1, 1, True))   # 미리 풀어 둔 b 를 뺌
    db, pl, sent = await setup("abc", on_send=on_send)
    await asyncio.wait_for(pl.run(), 10)
    rows = await db._all("SELECT title, state FROM music_queue ORDER BY id")
    assert [(r["title"], r["state"]) for r in rows] == [("a", "done"), ("b", "removed"), ("c", "done")], [dict(r) for r in rows]
    lv = heard(sent)
    assert not any(abs(v - 3000) < 30 for v in lv), "뺀 곡(b)은 한 조각도 안 들림"
    assert "b" in XDec.closed and "c" in XDec.opened


@test
async def loop_disables_overlap_and_replays_the_same_song():
    db, pl, sent = await setup("ab")
    pl.loop = 1
    await asyncio.wait_for(pl.run(), 10)
    assert XDec.opened.count("a") == 2, XDec.opened
    lv = heard(sent)
    first_b = next(i for i, v in enumerate(lv) if abs(v - 3000) < 30 or (v > 1000 + 30 and v != 0))
    assert sum(1 for v in lv[:first_b] if abs(v - 1000) < 30) >= 2 * (XDec.LEN - 40) - XDec.EOF_AT, "a 를 두 번 다 듣고 b"


@test
async def loop_turned_on_during_overlap_cancels_it():
    state = {"done": False}

    def on_send(pl, sent):
        if pl._xf is not None and pl._xf["i"] > 3 and not state["done"]:
            state["done"] = True
            pl.loop = 1                                    # 겹치는 도중에 '.반복'
    db, pl, sent = await setup("ab", on_send=on_send)
    await asyncio.wait_for(pl.run(), 10)
    assert state["done"] and XDec.opened.count("a") == 2, XDec.opened
    lv = heard(sent)
    loud_b = next(i for i, v in enumerate(lv) if v >= 2900)
    assert sum(1 for v in lv[:loud_b] if abs(v - 1000) < 30) >= 2 * (XDec.LEN - 40) - XDec.EOF_AT, \
        "반복이 이김 — a 를 한 번 더 다 듣기 전엔 b 가 커지지 않음"


@test
async def stopping_while_measuring_the_next_song_cancels_that_work():
    import threading
    gate = threading.Event()

    class Slow(Src):
        def loudness(self, path):
            if Path(path).stem[0] == "b":
                gate.wait(2)
            return None

    def on_send(pl, sent):
        if len(sent) == 20:
            pl.stop("end")
    db, pl, sent = await setup("ab", src=Slow(), on_send=on_send)
    t0 = time.monotonic()
    await asyncio.wait_for(pl.run(), 10)
    took = time.monotonic() - t0
    left = set(pl._bg)
    gate.set()
    assert not left and took < 1.5, f"멈추면 뒤에서 재던 일을 기다리지 않고 끊음 ({took:.1f}초)"
    assert "b" not in XDec.opened


@test
async def without_overlap_the_ready_decoder_is_still_adopted():
    old = music.XFADE
    music.XFADE = 0
    try:
        db, pl, sent = await setup("ab")
        await asyncio.wait_for(pl.run(), 10)
    finally:
        music.XFADE = old
    assert pl.stats["xfade"] == 0 and pl.stats["gapless"] == 1, pl.stats
    assert XDec.opened == ["a", "b"]


@test
async def per_song_loudness_is_matched():
    db, pl, sent = await setup("ab", src=Src({"a": -14.0, "b": -8.0}))    # b 가 6dB 큼 → 절반으로
    await asyncio.wait_for(pl.run(), 10)
    lv = heard(sent)
    tail = [v for v in lv if v > 0][-20:]
    assert all(abs(v - 1500) < 40 for v in tail), f"b(3000)는 0.5배 → 1500 {tail[:3]}"
    assert music.norm_gain(None) == 1.0 and music.norm_gain(-40) == music.NORM_MAX and music.norm_gain(5) == music.NORM_MIN
    assert abs(music.norm_gain(-17) - 10 ** (3 / 20)) < 1e-6


@test
async def stopping_with_a_ready_next_song_closes_everything():
    def on_send(pl, sent):
        if pl._next is not None:
            pl.stop("end")
    db, pl, sent = await setup("ab", on_send=on_send)
    await asyncio.wait_for(pl.run(), 10)
    assert sorted(XDec.closed) == sorted(XDec.opened) == ["a", "b"], (XDec.opened, XDec.closed)
    assert not pl._bg, "뒤에서 하던 일도 끝남"


@test
async def restart_right_after_handing_over_finishes_the_old_song():
    def on_send(pl, sent):
        if pl._carry is not None and not pl.done:
            pl.stop("restart")
    db, pl, sent = await setup("ab", on_send=on_send)
    await asyncio.wait_for(pl.run(), 10)
    rows = {r["title"]: r for r in await db._all("SELECT * FROM music_queue")}
    if pl.stats["xfade"]:                                  # 넘긴 바로 그 순간에 멈췄으면: a 는 끝, b 는 처음부터 이어 틀기
        assert rows["a"]["state"] in ("done",), dict(rows["a"])
        assert rows["b"]["state"] in ("queued", "playing"), dict(rows["b"])


@test
async def a_crashing_pacer_ends_the_session_instead_of_hanging():
    class Boom(XDec):
        def frame(self):
            raise ValueError("깨짐")
    db = await make_db()
    await musicq.add(db, CHAT, title="a", url="u", vid="a" * 11, duration=100, by_id=1, by_name="x")
    t = [0.0]

    async def sleep(s):
        t[0] += s
        await asyncio.sleep(0.001)

    async def send(f):
        pass
    pl = music.Player(db, CHAT, send, source=Src(), decoder=Boom, clock=lambda: t[0], sleep=sleep, poll=0.02, idle_sec=0.3)
    assert await asyncio.wait_for(pl.run(), 5) == "error:play", "박자 담당이 죽으면 붙잡지 말고 끝냄"


# ── 진짜 ffmpeg 로 재기 ──
def _wav(path: Path, amp: float, sec: float = 3.0) -> None:
    n = int(48000 * sec)
    x = (np.sin(np.arange(n) * 2 * math.pi * 440 / 48000) * amp * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(x.tobytes())


@test
def loudness_is_measured_once_and_cleaned_with_the_song():
    if not shutil.which("ffmpeg") and music.ffmpeg_bin() == "ffmpeg":
        return
    d = Path(tempfile.mkdtemp())
    src = music.Source(d)
    src.dir.mkdir(parents=True, exist_ok=True)
    loud, quiet = src.dir / "loud.wav", src.dir / "quiet.wav"
    _wav(loud, 0.5)
    _wav(quiet, 0.05)
    a, b = src.loudness(str(loud)), src.loudness(str(quiet))
    assert a is not None and b is not None and 18 < a - b < 22, (a, b)     # 10배 차이 = 20dB
    note = src.dir / ".loud" / "loud.wav.json"
    assert json.loads(note.read_text())["lufs"] == a
    real = subprocess.run
    subprocess.run = lambda *x, **k: (_ for _ in ()).throw(AssertionError("두 번째는 안 잼"))
    try:
        assert src.loudness(str(loud)) == a
    finally:
        subprocess.run = real
    loud.unlink()
    src.trim()
    assert not note.exists() and (src.dir / ".loud" / "quiet.wav.json").exists(), "지운 곡의 메모만 지움"


@test
async def real_decoder_reports_frames_left_only_after_the_end():
    if not shutil.which("ffmpeg") and music.ffmpeg_bin() == "ffmpeg":
        return
    d = Path(tempfile.mkdtemp())
    f = d / "t.wav"
    _wav(f, 0.3, sec=1.0)
    dec = music.Decoder(str(f))
    await dec.start()
    for _ in range(300):
        if dec.eof:
            break
        await asyncio.sleep(0.01)
    assert dec.eof and dec.frames_left() == len(dec.buf) and 95 <= dec.frames_left() <= 101, dec.frames_left()
    assert dec.ready()
    await dec.close()


if __name__ == "__main__":
    run_all()
