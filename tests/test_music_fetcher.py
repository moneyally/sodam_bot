"""🎵 노래 찾기·받기를 다른 프로그램에서 (sodam/voice/fetcher.py).
서버 실측 2026-10-09: 같은 프로그램 스레드에서 받으면 소리 박자가 1분에 6~10번 0.03초↑ 밀림(처음 0.49초) → 다른 프로그램이면 0번."""
import asyncio
import os
import tempfile
import time
from pathlib import Path

from fakes import runner

from sodam.voice import fetcher, music

ROOT = Path(__file__).resolve().parents[1]
test, run_all = runner()

ENV = {"PYTHONPATH": str(ROOT / "tests")}


def src(**k):
    return fetcher.ProcSource(Path(tempfile.mkdtemp()), factory="fetcher_fake:Fake", env=ENV, **k)


@test
def results_and_errors_cross_the_process_boundary():
    s = src(timeout=10)
    try:
        got = s.resolve("아이유 밤편지")
        assert got["title"] == "아이유 밤편지" and got["vid"] == "bbbbbbbbbbb", got
        assert s._slots[0].proc.pid != os.getpid(), "다른 프로그램에서 돎"
        try:
            s.resolve("고르기")
            raise AssertionError("고르기")
        except music.MusicChoice as e:
            assert e.items[0]["vid"] == "aaaaaaaaaaa" and e.query == "고르기"
        try:
            s.resolve("터짐")
            raise AssertionError("터짐")
        except RuntimeError as e:
            assert "이상한 오류" in str(e)
        assert s.resolve("또")["calls"] >= 2, "자식은 계속 살아서 상태를 들고 있음 (다시 안 띄움)"
    finally:
        s.close()


@test
def blocked_state_comes_back_so_primary_ok_works_in_the_parent():
    s = src()
    try:
        assert s.primary_ok()
        try:
            s.resolve("막힘")
        except music.MusicError as e:
            assert e.code == "blocked"
        assert not s.primary_ok(), "자식이 막힌 걸 부모도 앎 (이벤트 루프에서 바로 물어봄)"
    finally:
        s.close()


@test
def a_stuck_job_is_killed_and_the_next_one_still_works():
    s = src(timeout=1.0)
    try:
        s.resolve("미리 띄우기")                          # 켜지는 시간은 일 시간(1초)에 안 들어감 — 서버가 느려도
        t = time.monotonic()
        try:
            s.resolve("느림")
            raise AssertionError("시간 초과여야")
        except music.MusicError as e:
            assert e.code == "download" and time.monotonic() - t < 3
        assert s.resolve("다음")["title"] == "다음", "죽인 자식 대신 새로 띄움"
    finally:
        s.close()


@test
def slow_start_does_not_count_against_the_job_time():
    """2026-10-10 서버 시험(힘 60%): 자식이 켜지는 데 1초↑ → 1초 일 제한에 걸려 실패하던 것. 켜짐은 START_TIMEOUT 으로 따로."""
    s = fetcher.ProcSource(Path(tempfile.mkdtemp()), factory="fetcher_fake:SlowStart", env=ENV, timeout=1.0)
    try:
        assert s.resolve("다음")["title"] == "다음"
    finally:
        s.close()


@test
async def two_jobs_run_at_the_same_time_and_the_event_loop_stays_free():
    s = src()
    try:
        s.resolve("미리 띄우기")
        s._slots[1].call("resolve", ("미리",), {}, 0.0, 10)
        ticks = []

        async def beat():                            # 10ms 박자가 밀리지 않는지
            nxt = time.monotonic()
            for _ in range(60):
                nxt += 0.01
                await asyncio.sleep(max(0, nxt - time.monotonic()))
                ticks.append(time.monotonic() - nxt)
        t0 = time.monotonic()
        a, b, _ = await asyncio.gather(asyncio.to_thread(s.fetch, "x1"), asyncio.to_thread(s.fetch, "x2"), beat())
        assert a.endswith("x1.webm") and b.endswith("x2.webm")
        assert time.monotonic() - t0 < 0.95, "둘이 동시에 (자식 2개)"
        assert max(ticks) < 0.05, ("받는 동안 박자가 밀림", max(ticks))
    finally:
        s.close()


@test
def worker_uses_the_process_source_by_default():
    from types import SimpleNamespace

    from sodam.voice import worker
    w = worker.Worker(SimpleNamespace(db_path=tempfile.mkdtemp() + "/sodam.db"), None)
    assert type(w.music_source).__name__ == "ProcSource"
    os.environ["MUSIC_PROC"] = "0"
    try:
        w2 = worker.Worker(SimpleNamespace(db_path=tempfile.mkdtemp() + "/sodam.db"), None)
        assert isinstance(w2.music_source, music.Source)
    finally:
        os.environ.pop("MUSIC_PROC")


if __name__ == "__main__":
    run_all()
