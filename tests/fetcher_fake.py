"""tests/test_music_fetcher.py 가 자식 프로그램에서 쓰는 가짜 Source (진짜 받기 없음)."""
import time

from sodam.voice import music


class Fake:
    def __init__(self, data_dir):
        self.blocked_at = 0.0
        self.calls = 0

    def resolve(self, q):
        self.calls += 1
        if q == "막힘":
            self.blocked_at = time.time()
            raise music.MusicError("blocked", "음원 서버가 잠깐 막혔어요.")
        if q == "고르기":
            raise music.MusicChoice([{"vid": "aaaaaaaaaaa", "title": "가수 - 노래"}], "버전 골라", q)
        if q == "느림":
            time.sleep(5)
        if q == "터짐":
            raise ValueError("이상한 오류")
        print("받기 라이브러리가 표준 출력에 뭘 찍어도 통로가 안 깨짐")
        return {"title": q, "vid": "bbbbbbbbbbb", "calls": self.calls}

    def fetch(self, vid):
        time.sleep(0.5)
        return f"/tmp/{vid}.webm"


class SlowStart(Fake):
    """켜지는 데 오래 걸리는 자식 (서버가 바쁠 때 import 가 느린 것 흉내)."""

    def __init__(self, data_dir):
        time.sleep(1.5)
        super().__init__(data_dir)
