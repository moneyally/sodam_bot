"""🎵 소담 뮤직봇 — 음성 담당 프로세스(worker) 안에서 노래를 찾고·받고·풀어서 음성채팅에 틀어 준다.

구조:
  찾기   Source: 링크면 ID 그대로, 아니면 기본 음원 검색으로 길이 맞는 첫 곡 (키 없음)
  받기   bestaudio → data/music/<id>.<ext> (같은 곡은 다시 안 받음, MUSIC_CACHE_MB 넘으면 오래된 것부터 지움).
         JS 풀이(deno)가 필요 → pip 'deno' 의 실행 파일, 서버 IP 가 막히면
         data/music_auth/*.txt 인증 쿠키 (오너가 1:1 🎵 화면에서 넣음, 여러 개면 돌아가며) → 그래도 막히면 대체 음원
  풀기   ffmpeg(imageio-ffmpeg 정적 바이너리) → 48 kHz 모노 s16le 10 ms 조각, -ss 로 되감기·건너뛰기
  틀기   Player 가 10 ms 마다 한 조각을 send_frame (통화 = ExternalMedia 소리 줄 하나).
         소담 AI 목소리(Bridge)가 같은 방에 있으면 그 조각을 받아 섞음 — 소담이 말하는 동안 노래를 DUCK 배로 줄임 (DJ).
py-tgcalls 에는 seek 가 없어 우리가 직접 풀기 때문에 일시정지·되감기·음량·음소거가 전부 이 파일 몫.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import random
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import audio, musicmatch as mm, musicq

log = logging.getLogger("sodam.voice.music")

MAX_SEC = int(os.getenv("MUSIC_MAX_SEC", "1200"))          # 한 곡 최대 길이 (20분 — 라이브·몇 시간짜리 믹스 막기)
CACHE_MB = int(os.getenv("MUSIC_CACHE_MB", "600"))
IDLE_SEC = float(os.getenv("MUSIC_IDLE_SEC", "180"))       # 틀 곡 없음·일시정지가 이만큼이면 음성채팅에서 나감
PAUSE_MAX = float(os.getenv("MUSIC_PAUSE_MAX", "900"))     # 일시정지는 15분까지 기다림
STALL_FRAMES = 1500                                        # 15초 동안 풀린 조각이 없으면 곡을 끝냄
DUCK = 0.25                                                # 소담이 말하는 동안 노래 크기
VOICE_HOLD = 30                                            # 목소리 조각이 끊겨도 0.3초는 줄인 채 (말 사이마다 노래가 '쿵' 커지지 않게)
RESYNC = 0.03                                              # 이만큼(3조각) 넘게 밀리면 따라잡지 않고 박자 다시 맞춤
PREBUFFER = 50                                             # 곡 시작 전 미리 풀어 둘 조각 (0.5초)
AHEAD = 300                                                # 미리 풀어 두는 조각 (3초) — 디스크·CPU 가 잠깐 늦어도 안 끊김
XFADE = max(0.0, float(os.getenv("MUSIC_XFADE", "3")))     # 곡 사이 겹쳐 넘어가는 초 (0 = 겹치지 않고 바로 이어 붙이기만)
NORMALIZE = os.getenv("MUSIC_NORMALIZE", "1") != "0"       # 곡마다 소리 크기 맞추기
TARGET_LUFS = -14.0                                        # 맞출 크기 (음원 사이트들이 쓰는 기준)
NORM_MIN, NORM_MAX = 0.35, 1.6                             # 크기 맞춤 배수 범위 (작은 곡을 너무 키우면 찌그러짐)
ANNOUNCE_TIMEOUT = 20.0     # 재생 카드·음성채팅 제목 — 텔레그램이 답을 안 줘도 이만큼만 기다림 (2026-10-10 DC 4 내부 오류 때 무한 대기)
STOP_GRACE = 5.0            # 멈춘 뒤 진행 담당이 정리할 시간 — 넘으면 끊고 끝냄
RESUME_REASONS = ("restart", "chat_closed")               # 이렇게 끝나면 곡 위치를 남김 (재시작 / 음성채팅이 다시 열리면 이어서)
NORM_STEP = 0.005                                          # 재는 게 늦게 끝나면 조각마다 이만큼씩 (1초에 0.5) 따라감
VOICE_KEEP = 60                                            # 섞을 소담 목소리 조각 (0.6초 넘게 밀리면 오래된 것부터 버림)
SILENCE = bytes(audio.FRAME_BYTES)
LINK_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/|music\.youtube\.com/watch\?(?:[^#\s]*&)?v=)"
                   r"([A-Za-z0-9_-]{11})")
URL = re.compile(r"https?://\S+")
WATCH = "https://www.youtube.com/watch?v={}"
OEMBED = "https://www.youtube.com/oembed?format=json&url="


class MusicChoice(Exception):
    """딱 맞는 곡이 없거나 버전(커버·라이브)을 골라야 함 → 신청한 사람이 버튼으로 고름."""

    def __init__(self, items: list[dict], reason: str, query: str):
        super().__init__(reason)
        self.code, self.items, self.reason, self.query = "choice", items, reason, query

    def __reduce__(self):                                   # 다른 프로그램(fetcher)에서 건너올 수 있게
        return (MusicChoice, (self.items, self.reason, self.query))


class MusicError(Exception):
    """방에 그대로 보일 이유 (code = blocked·not_found·too_long·live·download·decode)."""

    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code, self.text = code, text

    def __reduce__(self):
        return (MusicError, (self.code, self.text))


PLAYLIST = re.compile(r"(?:youtube\.com|youtu\.be)/\S*?[?&]list=([A-Za-z0-9_-]{10,64})")
PLAYLIST_URL = "https://www.youtube.com/playlist?list={}"
PLAYLIST_MAX = 15                                       # 재생목록 링크 한 번에 넣는 곡
LYRICS_API = "https://lrclib.net/api/search"             # 공개 가사 DB (키 없음, 2026-10-08)


MIX_MIN = 4                                             # 분위기 플리로 넣을 곡 수 (이보다 적게 찾으면 다음 목록도 봄)
MIX_JUNK = re.compile(r"(?i)하루\s*종일|듣기\s*좋은|광고\s*없는|playlist|플레이리스트|플리|연속\s*재생|베스트\s*\d+|top\s*\d+|\d+\s*곡")   # 모음 영상 제목
MIX_MAX_SEC = 480                                       # 플리 곡 하나 최대 8분 (모음 영상 빼기)
SEARCH_PLAYLISTS = "https://www.youtube.com/results?search_query={}&sp=EgIQAw%253D%253D"   # 검색 '재생목록만'


FILLER = {"노래", "음악", "곡", "노래들", "한곡", "하나", "아무", "아무거나", "아무노래", "아무곡", "다른", "다른노래",
          "song", "songs", "music"}


def wants_mix(req) -> bool:
    """가수·제목 대신 분위기·모음을 신청 ('잔잔한 플리'·'최유리 노래모음'·'신나는 노래') → 여러 곡."""
    mood = [w for w in req.must if w not in FILLER]
    if not mood and not req.compilation:
        return False                                    # '노래 틀어줘'·'아무 노래' — 분위기 말이 없음 → 되묻기
    return req.compilation or req.generic


def playlist_id(text: str) -> str | None:
    """'…/playlist?list=PL…' 처럼 곡 ID(v=) 없이 목록만 있는 링크 → 목록 ID. 곡 링크에 붙은 list= 는 그 곡만 (공유 링크 흔함)."""
    m = PLAYLIST.search(text or "")
    if not m or LINK_ID.search(text or ""):
        return None
    return m.group(1)


def link_id(text: str) -> str | None:
    m = LINK_ID.search(text or "")
    return m.group(1) if m else None


# ── 찾기·받기 (yt-dlp) ─────────────────────────────────────
def _deno() -> str | None:
    try:
        import deno            # pip 'deno' (denoland 공식 배포) — 서버에 따로 설치 안 해도 됨
        return deno.find_deno_bin()
    except Exception:
        return None


def _blocked(msg: str) -> bool:
    m = msg.lower()
    return "sign in to confirm" in m or "not a bot" in m or "http error 429" in m or "po token" in m


ALT_TRACK = "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A{}"
SEARCH_N = 8                     # 기본 음원 검색 후보 수
ALT_TRY = 4                      # 실제로 받아지는지 확인해 볼 후보 수 (DRM 잠긴 공식 음원 건너뛰기)
ALT_MIN_SEC = 45                 # 대체 음원 미리듣기(30초) 조각은 건너뜀
PROXY_RETRY = 300                                          # 우회 길이 있을 때 막힌 뒤 쉬는 시간
PRIMARY_RETRY = int(os.getenv("MUSIC_RETRY_SEC", "3600"))   # 기본 음원이 막힌 뒤 이만큼은 대체 음원 먼저 (쿠키가 바뀌면 바로 다시)
_NOISE = re.compile(r"[\[(【](?:[^\])】]*?(?:mv|m/v|official|lyrics?|가사|audio|video|live|4k|hd|remaster)[^\])】]*)[\])】]", re.I)


_WORDS = re.compile(r"(?i)\b(?:official\s*(?:music\s*)?(?:video|mv|audio)|m/?v|lyrics?|live\s*clip|lyric\s*video)\b")


_norm = mm.norm
_words = mm.words


def clean_title(title: str) -> str:
    """'[MV] Paul Kim(폴킴) _ Me After You(너를 만나) [가사/Lyrics]' → 다른 곳에서 찾을 검색어 (괄호 속 MV·가사 같은 것 뺌)."""
    t = _NOISE.sub(" ", title or "")
    t = _WORDS.sub(" ", t)
    t = re.sub(r"가사(?:\s*첨부)?", " ", t)
    t = re.sub(r"[_|/]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:100]


class Source:
    """yt-dlp 로 찾기·받기 (기본 음원 → 막히면 대체 음원). 전부 스레드에서 (이벤트 루프를 안 막게).
    실측 2026-10-08 서버 IP: 기본 음원 검색은 되고 받기는 15곡 중 15곡 '봇이냐?' (쿠키 필요) ·
    대체 음원은 키·쿠키 없이 검색·받기 됨 (한국 노래도 사용자 업로드로 꽤 있음)."""

    def __init__(self, data_dir: Path, max_sec: int = MAX_SEC, cache_mb: int = CACHE_MB):
        self.dir = Path(data_dir) / "music"
        self.cookie_dir = Path(data_dir) / "music_auth"
        self.cache_dir = Path(data_dir) / "cache"
        self.max_sec, self.cache_mb = max_sec, cache_mb
        self.blocked_at = 0.0           # 기본 음원이 마지막으로 막은 시각
        self._way = 0                   # 여러 우회 길 중 지난번에 된 길 (거기부터)
        self._locks: dict[str, threading.Lock] = {}   # 같은 곡을 두 곳(미리 받기·두 방)에서 동시에 받지 않게
        self._locks_guard = threading.Lock()
        self._sig: tuple = ()           # 쿠키 파일 모양 (바뀌면 기본 음원 다시 시도)

    def _env(self) -> None:
        """음성 담당 서비스는 data/ 에만 쓸 수 있음 (ProtectSystem=strict·ProtectHome) → yt-dlp·deno 캐시도 그 안으로."""
        os.environ.setdefault("XDG_CACHE_HOME", str(self.cache_dir))
        os.environ.setdefault("DENO_DIR", str(self.cache_dir / "deno"))

    def cookies(self) -> list[str]:
        try:
            return sorted(str(p) for p in self.cookie_dir.glob("*.txt") if p.is_file() and p.stat().st_size > 0)
        except OSError:
            return []

    def primary_ok(self) -> bool:
        """기본 음원으로 받아 볼 만한지: 최근에 막혔으면 PRIMARY_RETRY 동안은 아님 (쿠키가 새로 들어오면 바로 다시)."""
        try:
            sig = tuple((p, os.stat(p).st_mtime) for p in self.cookies())
        except OSError:
            sig = ()
        if sig != self._sig:
            self._sig, self.blocked_at = sig, 0.0
        wait = PRIMARY_RETRY
        if proxies():                              # 우회 길(WARP) 막힘은 잠깐씩 왔다 감 (서버 실측 2026-10-09) → 1시간 대신 5분
            wait = min(wait, PROXY_RETRY)
        return time.time() - self.blocked_at >= wait

    def _opts(self, cookie: str | None, proxy: str | None = None, **extra) -> dict:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 15, "retries": 2,
                "logger": _Quiet(), "noprogress": True}
        deno_bin = _deno()
        if deno_bin:
            opts["js_runtimes"] = {"deno": {"path": deno_bin}}
        if cookie:
            opts["cookiefile"] = cookie
        if proxy:
            opts["proxy"] = proxy
            # 우회 길(WARP)은 IPv4·IPv6 둘 다 나가서, 곡 정보는 한쪽·소리 받기는 다른 쪽이면 403 (소리 주소가 IP 에 묶임 — 서버 실측 2026-10-09).
            # IPv4 로만 → 5곡 다 받아짐.
            opts["source_address"] = "0.0.0.0"
        opts.update(extra)
        return opts

    def _run(self, fn: Callable[[dict], Any], *, cookies: bool = True, where: str = "기본 음원", **extra):
        """쿠키가 있으면 쿠키부터(여러 개면 무작위 순서), '봇이냐?'로 막히면 다음 쿠키 → 마지막에 쿠키 없이.
        (쿠키 없이 먼저 두드리면 막힌 IP 로 요청만 늘어남)"""
        import yt_dlp
        self._env()
        jar = self.cookies() if cookies else []
        ways = proxies()
        # 나가는 길(MUSIC_PROXY, 예: WARP socks5://127.0.0.1:40000 — 쉼표로 여러 개)이 있으면 쿠키 없이 그 길로 먼저 — 서버 IP 는
        # '봇이냐?'로 막혀도 그 길은 됨 (서버 실측 2026-10-08: 쿠키 없이 3곡 다 받음). 여러 길이면 지난번에 된 길부터, 안 되면 다음 길.
        # 길이 다 죽었거나 막히면 예전처럼 쿠키 → 쿠키 없이.
        start = self._way if self._way < len(ways) else 0
        tries: list[tuple[str | None, str | None]] = [(None, ways[(start + i) % len(ways)]) for i in range(len(ways))] + \
            [(c, None) for c in random.sample(jar, len(jar))] + [(None, None)]
        last = ""
        for cookie, via in tries:
            try:
                with yt_dlp.YoutubeDL(self._opts(cookie, via, **extra)) as ydl:
                    got = fn(ydl)
                if via:
                    self._way = ways.index(via)
                return got
            except yt_dlp.utils.DownloadError as e:
                last = str(e)
                if via:
                    log.info("%s 우회 길 실패 → 서버 IP 로: %s", where, _short(last, 200))
                    continue
                if not _blocked(last):
                    break
        if _blocked(last):
            self.blocked_at = time.time()
            raise MusicError("blocked", "음원 서버가 잠깐 막혔어요. 잠시 뒤 다시 신청해 주세요.")   # 운영자 안내는 오너 1:1 로만 (worker)
        log.info("%s 못 가져옴: %s", where, _short(last, 300))
        raise MusicError("download", f"노래를 못 가져왔어요: {_short(_hide_src(last))}")

    def _info(self, ydl, url: str) -> dict:
        info = ydl.extract_info(url, download=False)
        return info or {}

    def resolve(self, query: str) -> dict:
        """검색어·링크 → {title, url, vid, duration}. 딱 맞는 곡이 없거나 버전을 골라야 하면 MusicChoice.
        신청 낱말을 전부 담은 원곡만 바로 틂 (musicmatch.choose) — 맨 위 곡을 그냥 믿지 않음."""
        vid = link_id(query)
        if vid:
            try:
                return self._check(self._run(lambda y: self._info(y, WATCH.format(vid))))
            except MusicError as e:
                if e.code != "blocked":
                    raise
                title = self._oembed_title(vid)      # 제목은 막힌 IP 에서도 oEmbed 로 됨
                if not title:
                    raise
                return self.alt_for(title, mm.parse(""), 0, fallback_of=e)
        if URL.search(query):
            raise MusicError("not_found", "노래 링크나 제목만 돼요.")
        req = mm.parse(query)
        if wants_mix(req):                         # '잔잔한 플리'·'최유리 노래모음'·'신나는 노래' → 워커가 mix_for 로 여러 곡
            raise MusicError("mix", "분위기 신청")
        if req.generic:
            raise MusicError("not_found", "🎧 어떤 노래로 틀까요? 가수·제목이나 분위기를 같이 써 주세요. "
                                          "예: <code>.노래 아이유 밤편지</code> · <code>.노래 잔잔한 플리</code>")
        try:                                      # 검색은 막힌 IP 에서도 됨 (실측)
            res = self._run(lambda y: y.extract_info(f"ytsearch{SEARCH_N}:{req.text}", download=False), extract_flat="in_playlist")
        except MusicError as e:
            log.info("기본 음원 검색 실패: %s", e)
            res = None
        cands = [e for e in (res or {}).get("entries") or [] if e]
        if not cands:
            return self.alt_for(None, req, 0)
        got = mm.choose(req, cands, self.max_sec)
        if got.item:
            return self.pick(got.item, req)
        if got.choices:
            raise MusicChoice(got.choices, got.reason, req.text)
        raise MusicError("not_found", f"'{_short(query, 40)}' 노래를 못 찾았어요.")

    def pick(self, item: dict, req: mm.Req | None = None) -> dict:
        """고른 곡(기본 음원 후보) → 틀 곡. 기본 음원이 막혀 있으면 대체 음원에서 '같은 노래'만."""
        info = {"title": str(item.get("title") or "?")[:200], "url": WATCH.format(item["vid"]), "vid": item["vid"],
                "duration": int(item.get("duration") or 0)}
        if self.primary_ok():
            return info
        try:
            return self.alt_for(info["title"], req or mm.parse(""), info["duration"])
        except MusicError:
            if self.cookies():
                return info                           # 쿠키가 있음 → 기본 음원으로 한 번 더 (막히면 Player 가 안내)
            raise

    def alt_for(self, ref_title: str | None, req: mm.Req, ref_sec: int = 0,
                fallback_of: MusicError | None = None) -> dict:
        """대체 음원에서 기준 곡(ref_title)과 '같은 노래' 한 곡 — 가수·노래 두 쪽 다 맞아야 (musicmatch.same_song).
        기준이 없으면(검색 실패) 신청 낱말 전부. 원하지 않은 버전(커버·라이브…)·길이 다른 것·DRM 잠긴 것은 건너뜀.
        못 찾으면 엉뚱한 곡을 트느니 '못 찾았어요'. vid = 'sc<번호>'."""
        allowed = req.allowed
        if ref_title and mm.kind(ref_title, req.allowed):
            allowed += " " + mm.norm(ref_title)          # 사람이 커버를 골랐으면 그 커버의 말은 허용
        queries = []
        if ref_title:
            queries += [clean_title(ref_title), " ".join(w for main, _ in mm.split(ref_title) for w in main)]
        if req.must:
            queries.append(req.text)
        seen = set()
        for q in queries:
            if not q or q in seen:
                continue
            seen.add(q)
            res = self._run(lambda y: y.extract_info(f"scsearch10:{q}", download=False), cookies=False,
                            where="대체 음원", extract_flat="in_playlist")
            ok = []
            for n, e in enumerate((res or {}).get("entries") or []):
                if not e or not str(e.get("id") or "").isdecimal():
                    continue
                dur = int(e.get("duration") or 0)
                if dur < ALT_MIN_SEC or dur > self.max_sec:
                    continue
                if ref_sec and abs(dur - ref_sec) > max(30, ref_sec * 0.2):
                    continue                              # 원곡과 길이가 다름 (믹스·조각·다른 곡)
                title = str(e.get("title") or "")
                k = mm.kind(title, allowed, ref_title or "")
                if (k and k != req.intent) or (not req.intent and mm.odd_version(title, ref_title or "")):
                    continue
                same, score = mm.same_song(title, ref_title, req) if ref_title else (False, 0)
                if not same and score == 0 and (not ref_title or mm.touches_all_parts(title, ref_title)):
                    same, score = mm.alt_ok(title, req)   # 기준 제목이 지저분해도 신청 낱말 전부 (-1 = 다른 가수 이름 → 안 살림)
                if not same:
                    continue
                extra = mm.extra_words(title, f"{ref_title or ''} {req.text}")
                ok.append(((-score, extra, abs(dur - ref_sec) if ref_sec else 0, n),
                           {"title": title or q, "url": ALT_TRACK.format(e["id"]), "vid": f"sc{e['id']}", "duration": dur}))
            ok.sort(key=lambda x: x[0])
            for _, t in ok[:ALT_TRY]:
                try:
                    self._run(lambda y: y.extract_info(t["url"], download=False), cookies=False, where="대체 음원")
                    return t
                except MusicError as e:
                    log.info("대체 음원 후보 건너뜀 %s: %s", t["vid"], e)
        if fallback_of is not None:
            raise fallback_of
        raise MusicError("not_found", f"'{_short(ref_title or req.text, 40)}' — 지금은 이 노래를 못 가져왔어요. "
                         "잠시 뒤 다시 신청해 주세요.")

    def fallback(self, title: str, ref_sec: int = 0) -> dict:
        """받으려던 곡이 막힘 → 대체 음원에서 같은 노래 (Player 가 부름)."""
        return self.alt_for(title, mm.parse(""), ref_sec)

    def _entries(self, url: str) -> list[dict]:
        res = self._run(lambda y: y.extract_info(url, download=False), extract_flat="in_playlist", noplaylist=False,
                        playlistend=50)
        return [e for e in (res or {}).get("entries") or [] if e and e.get("id")]

    def _playable(self, e: dict) -> bool:
        dur = int(e.get("duration") or 0)
        title = str(e.get("title") or "")
        return (bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", str(e.get("id"))))
                and e.get("live_status") not in ("is_live", "is_upcoming")
                and 30 <= dur <= self.max_sec and title not in ("[Private video]", "[Deleted video]"))

    def playlist(self, list_id: str, limit: int = PLAYLIST_MAX) -> list[dict]:
        """재생목록 → 틀 수 있는 곡 [{title, url, vid, duration}] (최대 limit, 라이브·20분 넘는 것·지운 영상 빼고).
        곡은 넣어 두기만 — 받기는 차례가 오면 (막혔으면 그때 대체 음원)."""
        out = []
        for e in self._entries(PLAYLIST_URL.format(list_id)):
            if self._playable(e):
                out.append({"title": str(e.get("title"))[:200], "url": WATCH.format(e["id"]), "vid": e["id"],
                            "duration": int(e.get("duration") or 0)})
            if len(out) >= limit:
                break
        if not out:
            raise MusicError("not_found", "재생목록에서 틀 수 있는 곡을 못 찾았어요.")
        return out

    def mix_for(self, text: str, limit: int = PLAYLIST_MAX) -> list[dict]:
        """분위기·모음 신청 → 재생목록 검색 → 앞 목록부터 곡을 골라 limit 곡까지.
        모음 영상(8분 넘음)·커버·라이브·리믹스 빼고, 가수 이름을 썼으면 그 가수 곡만, 같은 곡은 한 번 (서버 실측 2026-10-08:
        첫 목록이 1시간짜리 모음 영상뿐인 경우가 있어 다음 목록으로 넘어감)."""
        import urllib.parse
        req = mm.parse(text)
        words = [w for w in req.must if w not in FILLER and w not in ("소담", "소담아", "소담이")]   # '소담아 잔잔한 플리 하나' → '잔잔한'
        base = " ".join(words) or "인기"
        queries = [f"{base} 노래", f"{base} 플레이리스트"]   # 실측: '잔잔한 플레이리스트' 는 1시간 모음 영상 목록만, '잔잔한 노래' 는 곡 102개
        names = [w for w in words if w not in mm.GENERIC]   # 가수 이름일 수도('최유리'), 분위기 말일 수도('잔잔한')
        out, named, seen = [], [], set()
        lists = []
        for q in queries:
            lists += [pl for pl in self._entries(SEARCH_PLAYLISTS.format(urllib.parse.quote(q)))[:8] if pl not in lists]
        for pl in lists[:14]:                             # 검색 순서가 매번 달라 앞 몇 개가 모음 영상 목록뿐일 때가 있음 (실측: 앞 8개 전부 1시간 영상)
            try:
                items = self._entries(PLAYLIST_URL.format(pl["id"]))
            except MusicError as e:
                log.info("플리 목록 못 가져옴 %s: %s", pl.get("id"), e)
                continue
            for e in items:
                title = str(e.get("title") or "")
                key = mm.song_key(title) or title
                if not self._mix_ok(e, req, seen):
                    continue
                seen.add(key)
                item = {"title": title[:200], "url": WATCH.format(e["id"]), "vid": e["id"], "duration": int(e.get("duration") or 0)}
                out.append(item)
                if names and mm.Title(f"{title} {e.get('channel') or e.get('uploader') or ''}").hits(names):
                    named.append(item)
            if len(named if names else out) >= limit:
                break
        if names and len(named) >= MIX_MIN:
            out = named                                  # 그 말이 곡 제목에 자주 나오면 가수 이름 → 그 가수 곡만
        if 0 < len(out) < MIX_MIN:                       # 몇 곡뿐 → 그 곡의 믹스(비슷한 노래)로 채움 (검색 순서가 매번 달라서)
            for e in self._mix_entries(out[0]["vid"]):
                title = str(e.get("title") or "")
                key = mm.song_key(title) or title
                if not self._mix_ok(e, req, seen):
                    continue
                seen.add(key)
                out.append({"title": title[:200], "url": WATCH.format(e["id"]), "vid": e["id"],
                            "duration": int(e.get("duration") or 0)})
                if len(out) >= limit:
                    break
        if not out:
            raise MusicError("not_found", f"🎧 '{_short(' '.join(words) or req.text, 30)}' 분위기 곡을 못 찾았어요. 다른 말로 다시 해 주세요 "
                                          "(예: <code>.노래 잔잔한 발라드 플리</code>).")
        return out[:limit]

    def _mix_ok(self, e: dict, req, seen: set) -> bool:
        """플리에 넣을 곡: 8분 안·원곡·같은 곡 아님·'하루종일 듣기 좋은 …' 같은 모음 영상 제목 아님."""
        title = str(e.get("title") or "")
        return (bool(title) and self._playable(e) and int(e.get("duration") or 0) <= MIX_MAX_SEC
                and not mm.kind(title, req.allowed) and not MIX_JUNK.search(title)
                and (mm.song_key(title) or title) not in seen)

    def _mix_entries(self, seed_vid: str) -> list[dict]:
        try:
            return self._entries(WATCH.format(seed_vid) + "&list=RD" + seed_vid)
        except MusicError as e:
            log.info("믹스 못 가져옴 %s: %s", seed_vid, e)
            return []

    def related(self, seed_vid: str, seen_vids: set[str], seen_titles: set[str]) -> dict | None:
        """자동 재생: 기준 곡의 믹스(비슷한 노래 목록)에서 이 방이 최근에 안 튼 원곡 하나. 없으면 None."""
        try:
            entries = self._entries(WATCH.format(seed_vid) + "&list=RD" + seed_vid)   # 믹스는 곡 링크로만 열림 (실측: playlist?list=RD… 는 'unviewable')
        except MusicError as e:
            log.info("자동 재생 목록 못 가져옴 %s: %s", seed_vid, e)
            return None
        for e in entries:
            title = str(e.get("title") or "")
            if e["id"] == seed_vid or e["id"] in seen_vids or mm.song_key(title) in seen_titles:
                continue
            if not self._playable(e) or mm.kind(title):          # 커버·라이브·모음·리믹스 말고 원곡만
                continue
            return {"title": title[:200], "url": WATCH.format(e["id"]), "vid": e["id"], "duration": int(e.get("duration") or 0)}
        return None

    def lyrics(self, title: str, duration: int = 0) -> dict | None:
        """공개 가사 DB 에서 같은 곡 가사 → {track, artist, plain} — 길이 ±5초·가수/노래 둘 다 맞을 때만 (엉뚱한 가사 X)."""
        import json
        import urllib.parse
        import urllib.request
        q = clean_title(title)
        if not q:
            return None
        req = urllib.request.Request(LYRICS_API + "?" + urllib.parse.urlencode({"q": q}),
                                     headers={"User-Agent": "sodam-music/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                items = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
        except Exception as e:
            log.info("가사 못 가져옴 %s: %r", q[:40], e)
            return None
        best = None
        for it in items if isinstance(items, list) else []:
            plain = str(it.get("plainLyrics") or "").strip()
            if not plain or it.get("instrumental"):
                continue
            d = int(float(it.get("duration") or 0))
            if duration and d and abs(d - duration) > 5:
                continue
            cand = f"{it.get('artistName') or ''} - {it.get('trackName') or ''}"
            same, score = mm.same_song(cand, title)
            if not same:
                continue
            if best is None or score > best[0]:
                best = (score, {"track": str(it.get("trackName") or ""), "artist": str(it.get("artistName") or ""), "plain": plain})
        return best[1] if best else None

    def _oembed_title(self, vid: str) -> str | None:
        import json
        import urllib.parse
        import urllib.request
        url = OEMBED + urllib.parse.quote(WATCH.format(vid))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
                return str(json.load(r).get("title") or "")[:200] or None
        except Exception as e:
            log.info("oEmbed 제목 실패 %s: %r", vid, e)
            return None

    def _check(self, info: dict) -> dict:
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise MusicError("live", "라이브 방송은 못 틀어요.")
        dur = int(info.get("duration") or 0)
        if dur > self.max_sec:
            raise MusicError("too_long", f"{self.max_sec // 60}분 넘는 영상은 못 틀어요.")
        return {"title": info.get("title") or "?", "url": info.get("webpage_url") or WATCH.format(info.get('id')),
                "vid": info.get("id"), "duration": dur}

    def fetch(self, vid: str) -> str:
        """오디오 파일 경로 (이미 받았으면 그대로). vid 'sc…' = 대체 음원. 같은 곡은 한 번에 하나만 받음."""
        with self._locks_guard:
            lock = self._locks.setdefault(vid, threading.Lock())
        with lock:
            return self._fetch(vid)

    def _fetch(self, vid: str) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        old = self._cached(vid)
        if old:
            os.utime(old)                                   # 오래된 것부터 지울 때 '최근에 씀'
            return old
        sc = vid.startswith("sc") and vid[2:].isdecimal()
        url = ALT_TRACK.format(vid[2:]) if sc else WATCH.format(vid)
        self._run(lambda y: y.download([url]), cookies=not sc, where="대체 음원" if sc else "기본 음원",
                  format="bestaudio/best", outtmpl=str(self.dir / (f"{vid}.%(ext)s" if sc else "%(id)s.%(ext)s")),
                  overwrites=False, max_filesize=80 * 1024 * 1024, match_filter=self._too_long)
        got = self._cached(vid)
        if not got:
            raise MusicError("download", "노래 파일을 못 받았어요.")
        self.trim()
        return got

    def _too_long(self, info, *, incomplete=False):
        dur = info.get("duration")
        return "too long" if dur and dur > self.max_sec else None

    def _cached(self, vid: str) -> str | None:
        for p in self.dir.glob(f"{glob_escape(vid)}.*"):
            if p.suffix not in (".part", ".ytdl", ".tmp") and p.stat().st_size > 0:
                return str(p)
        return None

    def loudness(self, path: str) -> float | None:
        """곡 전체 소리 크기(LUFS, ebur128) — 한 번 재서 .loud/<파일>.json 에 남김 (곡마다 크기를 맞춰 틀려고).
        무거운 일(곡 전체 풀기 ~1~2초)이라 fetcher 자식 프로그램이 함. 못 재면 None (그 곡은 크기 그대로)."""
        p = Path(path)
        note = p.parent / ".loud" / (p.name + ".json")
        try:
            return float(json.loads(note.read_text())["lufs"])
        except Exception:
            pass
        try:
            r = subprocess.run([ffmpeg_bin(), "-nostdin", "-hide_banner", "-i", str(p), "-vn", "-af", "ebur128=framelog=quiet",
                                "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        except Exception as e:
            log.info("소리 크기 못 잼 %s: %r", p.name, e)
            return None
        m = re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", r.stderr or "")
        if not m:
            return None
        lufs = float(m[-1])
        if not -70 < lufs < 5:                              # 무음·이상한 값은 안 맞춤
            return None
        with contextlib.suppress(OSError):
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(json.dumps({"lufs": lufs}))
        return lufs

    def trim(self) -> None:
        """오래 안 쓴 것부터 지움. 받는 중(.part)은 건드리지 않고, 그 사이 사라진 파일은 건너뜀."""
        files = []
        for p in self.dir.glob("*"):
            try:
                if p.is_file() and p.suffix not in (".part", ".ytdl"):
                    st = p.stat()
                    files.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue
        files.sort()
        total = sum(f[1] for f in files)
        limit = self.cache_mb * 1024 * 1024
        for _, size, p in files:
            if total <= limit:
                break
            total -= size
            with contextlib.suppress(OSError):
                p.unlink()
        for note in (self.dir / ".loud").glob("*.json"):   # 지워진 곡의 소리 크기 메모도
            if not (self.dir / note.name[:-5]).exists():
                with contextlib.suppress(OSError):
                    note.unlink()


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


class _Quiet:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        log.debug("yt-dlp: %s", msg)


def _hide_src(msg: str) -> str:
    """방에 보일 오류 글에서 음원 출처(추출기 이름·주소)를 뺌."""
    msg = re.sub(r"^(?:ERROR:\s*)?(?:\[[^\]]*\]\s*)+", "", msg or "")
    msg = re.sub(r"^[\w-]+:\s+", "", msg)                     # 'abc123def45: ' 곡 ID
    msg = re.sub(r"(?i)\b(?:youtube|you\s*tube|soundcloud|yt-dlp)\b(?:\s+said)?:?", "", msg)   # 'YouTube said: …' (실측)
    return URL.sub("", msg).strip()


def proxies() -> list[str]:
    """노래 받기 우회 길 (MUSIC_PROXY, 쉼표로 여러 개 — 서버는 WARP 두 개)."""
    return [p.strip() for p in os.getenv("MUSIC_PROXY", "").split(",") if p.strip()]


def _short(s: str, n: int = 120) -> str:
    s = re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", str(s or ""))).replace("ERROR: ", "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ── 풀기 (ffmpeg) ─────────────────────────────────────────
def ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


class Decoder:
    """파일 → 48 kHz 모노 10 ms 조각. 뒤에서 AHEAD 조각까지 미리 풀어 둠 (pacer 는 기다리지 않고 꺼내기만)."""

    def __init__(self, path: str, start_ms: int = 0):
        self.path, self.start_ms = path, max(0, int(start_ms))
        self.buf: deque[bytes] = deque()
        self.eof = False
        self.failed = ""
        self._proc = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-ss", f"{self.start_ms / 1000:.2f}", "-i", self.path,
            "-vn", "-ac", "1", "-ar", str(audio.TG_RATE), "-f", "s16le", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)   # 오류 출력을 PIPE 로 두고 안 읽으면 ~128KB 에서 ffmpeg 가 멈춤
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        out = self._proc.stdout
        try:
            while True:
                if len(self.buf) >= AHEAD:
                    await asyncio.sleep(0.02)
                    continue
                try:
                    chunk = await out.readexactly(audio.FRAME_BYTES)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        self.buf.append(e.partial.ljust(audio.FRAME_BYTES, b"\0"))
                    break
                self.buf.append(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.failed = str(e)
        finally:
            self.eof = True
        rc = await self._proc.wait()
        if rc not in (0, None) and not self.buf:
            self.failed = self.failed or f"ffmpeg {rc}"

    def frame(self) -> bytes | None:
        return self.buf.popleft() if self.buf else None

    def frames_left(self) -> int | None:
        """끝까지 남은 조각 수 — 다 풀었을 때만 (그 전엔 None). 곡 끝 겹쳐 넘어가기 시점을 잴 때."""
        return len(self.buf) if self.eof else None

    def ready(self) -> bool:
        return len(self.buf) >= PREBUFFER or self.eof

    @property
    def finished(self) -> bool:
        return self.eof and not self.buf

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()


LIMIT = 0.85 * 32767                                       # 이보다 큰 소리는 부드럽게 눌러 줌 (딱딱 잘리면 '지직·튐')
DUCK_DOWN = 0.15                                           # 한 조각(10ms)에 줄이는 크기 → 1→0.25 가 50ms
DUCK_UP = 0.03                                             # 다시 키울 때 → 0.25→1 이 250ms (한 번에 바뀌면 '딱' 소리)


def soft_limit(x: np.ndarray) -> np.ndarray:
    """LIMIT 넘는 부분만 tanh 로 눌러서 32767 안에 (노래 + 목소리 + 볼륨 200% 가 겹쳐도 딱딱 잘리지 않게)."""
    over = np.abs(x) > LIMIT
    if over.any():
        room = 32767 - LIMIT
        a = np.abs(x[over]) - LIMIT
        x[over] = np.sign(x[over]) * (LIMIT + room * np.tanh(a / room))
    return x


def glide(cur: float, target: float) -> float:
    """조각마다 크기를 조금씩 (줄일 땐 빨리, 키울 땐 천천히)."""
    if target < cur:
        return max(target, cur - DUCK_DOWN)
    return min(target, cur + DUCK_UP)


def pcm(b: bytes) -> np.ndarray:
    return np.frombuffer(b[: audio.FRAME_BYTES], dtype="<i2").astype(np.float32)


def ramp(g0: float, g1: float, n: int):
    """조각 안에서 g0 → g1 (같으면 숫자 하나)."""
    return g1 if g0 == g1 else np.linspace(g0, g1, n, dtype=np.float32)


def norm_gain(lufs: float | None) -> float:
    """곡 크기(LUFS) → 맞춤 배수. 못 쟀으면 1."""
    if lufs is None:
        return 1.0
    return max(NORM_MIN, min(NORM_MAX, 10 ** ((TARGET_LUFS - float(lufs)) / 20)))


def _has(m) -> bool:
    return m is not None and (len(m) > 0 if isinstance(m, np.ndarray) else bool(m))


def mix(music, voice: bytes | None, gain: float, start: float | None = None) -> bytes:
    """노래(gain 배, start 가 있으면 조각 안에서 start→gain 으로 매끄럽게) + 소담 목소리. 둘 다 없으면 무음.
    노래는 조각(bytes) 또는 크기 맞춤·겹치기를 이미 한 실수 배열 (잘리지 않게 끝에서 한 번만 누름)."""
    if not _has(music) and not voice:
        return SILENCE
    out = np.zeros(audio.FRAME_BYTES // 2, dtype=np.float32)
    if _has(music) and (gain > 0 or (start or 0) > 0):
        m = music[: len(out)] if isinstance(music, np.ndarray) else pcm(music)
        g = gain if start is None or start == gain else np.linspace(start, gain, len(m), dtype=np.float32)
        out[: len(m)] += m * g
    if voice:
        v = np.frombuffer(voice[: audio.FRAME_BYTES], dtype="<i2").astype(np.float32)
        out[: len(v)] += v
    return np.clip(soft_limit(out), -32768, 32767).astype("<i2").tobytes()


class _Stopped(Exception):
    """받는 중에 DJ 가 멈춤."""


def _ignore(t: asyncio.Future) -> None:
    if not t.cancelled():
        t.exception()                                      # 뒤에서 끝난 일의 오류는 버림 ('never retrieved' 경고 없이)


# ── 틀기 ─────────────────────────────────────────────────
class Player:
    """방 하나의 DJ. worker 가 만들고 run() 을 task 로. 곡 순서는 DB(musicq) — 재시작해도 이어서."""

    def __init__(self, db, chat_id: int, send: Callable[[bytes], Any], *, source: Any, announce: Callable | None = None,
                 decoder: Callable[[str, int], Any] = Decoder, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep, has_voice: Callable[[], bool] = lambda: False,
                 idle_sec: float = IDLE_SEC, poll: float = 1.0):
        self.db, self.chat_id, self._send = db, chat_id, send
        self.source, self.announce = source, announce
        self._decoder, self.clock, self.sleep, self.has_voice = decoder, clock, sleep, has_voice
        self.idle_sec, self.poll = idle_sec, poll
        self.row = None                     # 지금 곡 (music_queue 줄)
        self.dec = None
        self.pos_ms = 0
        self.paused = False
        self.muted = False
        self.volume = 100                   # 0~200 %
        self.loop = 0                       # 남은 반복 횟수
        self.voice: deque[bytes] = deque(maxlen=VOICE_KEEP)
        self.tracks = 0
        self.reason = ""
        self._done = asyncio.Event()
        self._end = asyncio.Event()         # 지금 곡 끝 (다 틀었음·건너뛰기)
        self._end_state = "done"
        self._wake = asyncio.Event()        # 새 곡이 들어옴
        self._quiet_since = clock()
        self._paused_at: float | None = None
        self.stats = {"frames": 0, "late": 0, "underrun": 0, "jitter": 0, "max_late_ms": 0,
                      "send_slow": 0, "send_max_ms": 0, "xfade": 0, "gapless": 0}
        self._next: dict | None = None      # 미리 풀어 둔 다음 곡 {id, path, pos, dec, checked}
        self._xf: dict | None = None        # 겹쳐 넘어가는 중 {n 전체 조각, i 지난 조각, got 다음 곡에서 꺼낸 조각}
        self._carry: dict | None = None     # 겹쳐 넘어가서 이미 틀고 있는 다음 곡 (다음 _play 가 이어받음)
        self._prev_dec = None               # 넘어가며 끝난 앞 곡 풀기 (그 곡의 _play 가 닫음)
        self._ncur = 1.0                    # 지금 곡에 걸린 크기 맞춤 배수 (조각마다 dec.norm 쪽으로 조금씩)
        self._arm_lock = asyncio.Lock()
        self._bg: set = set()
        self.win_max_late = 0               # 끊김 감시가 1분마다 읽고 0 으로 (그 1분 동안 가장 크게 밀린 ms)
        self._voice_tail = 0
        self._last: bytes | None = None     # 마지막으로 꺼낸 노래 조각 (비었을 때 줄이며 끝내기용)
        self._gain = 1.0                    # 지금 노래 크기 (목소리 줄이기·볼륨을 조각마다 조금씩 따라감)
        self._starved = 0
        self._auto_tried = False            # 대기열이 빈 뒤 자동 재생을 한 번 시도했나 (곡이 틀어지면 다시 False)

    # ── 밖에서 부름 (worker 일감) ──
    def stop(self, reason: str = "end") -> None:
        if not self._done.is_set():
            self.reason = reason
            self._done.set()
            self._end.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wake(self) -> None:
        self._wake.set()
        if self.row and self._next is None and not self.done:   # 틀고 있는데 새 곡이 들어옴 → 다음 곡 미리 준비
            with contextlib.suppress(RuntimeError):
                self._prefetch()

    def skip(self) -> bool:
        if not self.row:
            return False
        self.loop = 0
        self._end_state = "skipped"
        if self._xf is not None and self.dec is not None:      # 겹치는 중 → 들어오던 다음 곡을 그대로 이어받음 (끊김 없이 커짐)
            n = self._xf["n"]
            self._gain *= math.sin(min(1.0, self._xf["i"] / n) * math.pi / 2)
            self._promote(self.dec)
        self._end.set()
        return True

    def pause(self) -> bool:
        if not self.row or self.paused:
            return False
        self.paused, self._paused_at = True, self.clock()
        return True

    def resume(self) -> bool:
        if not self.row or not self.paused:
            return False
        self.paused, self._paused_at = False, None
        return True

    async def seek(self, sec: float) -> bool:
        """그 위치(초)부터 다시 풀기. 곡 길이를 넘으면 False."""
        if not self.row or not self.dec:
            return False
        dur = int(self.row["duration"] or 0)
        ms = max(0, int(sec * 1000))
        if dur and ms >= dur * 1000:
            return False
        await self._open(self.row["path"], ms)
        return True

    def voice_frame(self, frame: bytes) -> None:
        """Bridge(소담 AI 목소리)가 10 ms 마다 보냄 — 무음은 버림."""
        if frame and frame != SILENCE:
            self.voice.append(frame)

    def snapshot(self) -> dict:
        return {"title": self.row["title"] if self.row else None, "pos": self.pos_ms // 1000,
                "duration": int(self.row["duration"] or 0) if self.row else 0, "paused": self.paused,
                "muted": self.muted, "volume": self.volume, "loop": self.loop}

    # ── 실행 ──
    async def run(self) -> str:
        pacer = asyncio.create_task(self._pacer())
        pacer.add_done_callback(self._pacer_died)
        conductor = asyncio.create_task(self._conductor())
        try:
            stopped = asyncio.create_task(self._done.wait())
            try:
                await asyncio.wait({conductor, stopped}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopped.cancel()
            if not conductor.done():                       # 멈췄는데 진행 담당이 뭔가(텔레그램 답 등)를 기다리며 붙잡힘
                try:                                       # → 잠깐만 기다리고 끊음 (2026-10-10 파멸방: 통화가 끊겼는데 정리를 못 해 노래가 죽은 채로 남음)
                    await asyncio.wait_for(asyncio.shield(conductor), STOP_GRACE)
                except asyncio.TimeoutError:
                    log.warning("노래 진행 담당이 멈추지 않아 끊음 %s (%s)", self.chat_id, self.reason)
                    conductor.cancel()
                    with contextlib.suppress(BaseException):
                        await conductor
            else:
                conductor.result()                         # 진행 담당의 오류는 그대로 (worker 가 기록)
        finally:
            if not conductor.done():
                conductor.cancel()
                with contextlib.suppress(BaseException):
                    await conductor
            pacer.cancel()
            with contextlib.suppress(BaseException):
                await pacer
            for t in list(self._bg):                               # 뒤에서 하던 미리 받기·크기 재기도 끊음 (끝난 뒤 풀기 프로그램을 띄우지 않게)
                t.cancel()
            for t in list(self._bg):
                with contextlib.suppress(BaseException):
                    await t
            carried = self._carry is not None and self.dec is self._carry["dec"]
            decs = [self.dec, self._prev_dec, (self._next or {}).get("dec"), (self._carry or {}).get("dec")]
            for d in {id(d): d for d in decs if d is not None}.values():
                with contextlib.suppress(Exception):
                    await d.close()
            self.dec = self._prev_dec = self._next = self._carry = self._xf = None
            if self.row and self.reason in RESUME_REASONS:         # 재시작·음성채팅 닫힘: 곡 위치를 남겨 이어서
                with contextlib.suppress(Exception):
                    if carried:                                    # 앞 곡은 다 틀고 다음 곡으로 넘어가던 중 → 앞 곡은 끝, 다음 곡은 처음부터
                        await musicq.finish(self.db, self.row["id"], "done")
                    else:
                        await musicq.save_pos(self.db, self.row["id"], self.pos_ms)
        return self.reason or "end"

    def _pacer_died(self, t: asyncio.Task) -> None:
        """박자 담당이 예상 못 한 오류로 죽으면 곡이 영영 안 끝남 → 기록하고 끝냄 (조용히 멈춘 채 붙잡지 않게)."""
        if not t.cancelled() and t.exception() is not None and not self.done:
            log.warning("노래 박자 담당 오류 %s: %r", self.chat_id, t.exception())
            self.stop("error:play")

    async def _open(self, path: str, start_ms: int) -> None:
        if self._xf is not None:                           # 겹치는 중에 이동 → 겹치기 취소, 다음 곡은 다시 준비
            self._drop_next()
            self._prefetch()
        old, self.dec = self.dec, None
        keep = getattr(old, "norm", None) if old is not None and getattr(old, "path", None) == path else None
        if old:
            await old.close()
        dec = self._decoder(path, start_ms)
        if keep is not None:                               # 같은 곡 이동 → 재 둔 크기 그대로
            dec.norm = keep
        self._ncur = getattr(dec, "norm", 1.0)
        self._gain = 0.0                                   # 새로 연 소리는 0 에서 천천히 (앞 곡이 큰 채로 바로 바뀌면 '툭')
        await dec.start()
        t0 = self.clock()                                  # 0.5초는 미리 풀어 두고 시작 (첫 조각부터 비면 '툭툭')
        while len(getattr(dec, "buf", ())) < PREBUFFER and not getattr(dec, "eof", True) and self.clock() - t0 < 2:
            await asyncio.sleep(0.02)
        self.dec, self.pos_ms = dec, start_ms

    async def _conductor(self) -> None:
        while not self.done:
            row = await musicq.start_next(self.db, self.chat_id)
            if self.done:                                  # 꺼내는 사이 멈춤 (곡은 playing 그대로 — 끝 정리·재시작이 처리)
                return
            if not row:
                if not self._auto_tried and await self._autoplay():
                    continue
                if self._quiet_for() >= self.idle_sec and not self.has_voice():
                    return self.stop("idle")
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.poll)
                continue
            await self._play(row)

    async def _autoplay(self) -> bool:
        """대기열이 비었고 자동 재생이 켜져 있으면 방금 곡과 비슷한 노래 하나를 넣음 (사람 신청 없이 AUTO_MAX 곡까지)."""
        self._auto_tried = True
        try:
            if not hasattr(self.source, "related") or not (await musicq.modes(self.db, self.chat_id))["autoplay"]:
                return False
            if await musicq.auto_streak(self.db, self.chat_id) >= musicq.AUTO_MAX:
                log.info("자동 재생 %s: 연속 %s곡 → 멈춤", self.chat_id, musicq.AUTO_MAX)
                return False
            recent = [dict(r) for r in await musicq.recent_tracks(self.db, self.chat_id)]
            ids = [r.get("orig_vid") or r.get("vid") or "" for r in recent if r.get("state") != "removed"]
            seed = next((v for v in ids if re.fullmatch(r"[A-Za-z0-9_-]{11}", v)), None)
            if not seed:
                return False
            seen_v = {v for r in recent for v in (r.get("vid"), r.get("orig_vid")) if v}
            seen_t = {mm.song_key(r.get("title") or "") for r in recent}
            got = await asyncio.to_thread(self.source.related, seed, seen_v, seen_t)
            if not got or self.done:
                return False
            rid, _, why = await musicq.add(self.db, self.chat_id, title=got["title"], url=got["url"], vid=got["vid"],
                                           duration=got["duration"], by_id=None, by_name="📻 자동 재생", per_user=0, auto=True)
            return bool(rid)
        except Exception as e:                          # 자동 재생이 실패해도 DJ 는 그대로 (조용히 나가기만)
            log.info("자동 재생 실패 %s: %r", self.chat_id, e)
            return False

    def _lyrics_later(self, row: dict) -> None:
        """틀기 시작한 곡의 가사를 뒤에서 찾아 기록 (소담이 '가사 뭐야'에 답할 수 있게). 실패는 조용히."""
        if not row.get("vid") or not hasattr(self.source, "lyrics"):
            return

        async def run():
            try:
                if await musicq.lyrics(self.db, row["vid"]):
                    return
                got = await asyncio.to_thread(self.source.lyrics, row["title"], int(row["duration"] or 0))
                if got:
                    await musicq.save_lyrics(self.db, row["vid"], row["title"], got["track"], got["artist"], got["plain"], "lrclib")
            except Exception as e:
                log.info("가사 기록 실패 %s: %r", self.chat_id, e)
        t = asyncio.create_task(run())
        self.__dict__.setdefault("_bg", set()).add(t)
        t.add_done_callback(self.__dict__["_bg"].discard)

    async def _play(self, row) -> None:
        self._end.clear()
        self._end_state = "done"
        if self.done:                                      # stop() 의 _end 를 방금 지웠을 수 있음 → 기다리지 않고 끝
            return
        if self._next is not None:                         # 다음 곡 확인은 곡마다 새로 (그 사이 대기열이 바뀌었을 수 있음)
            self._next["checked"] = self._next["checking"] = False
        try:
            row = dict(row)
            ready = self._take_ready(row)
            if ready:                                      # 미리 풀어 둔 곡 / 겹쳐 넘어가 이미 트는 곡 → 열기 생략 (빈틈 없음)
                if ready["path"] != row["path"]:
                    await musicq.set_path(self.db, row["id"], ready["path"])
                row["path"] = ready["path"]
                self.row = row
            elif row["path"] and Path(row["path"]).exists():
                path = row["path"]
            else:
                try:
                    path = await self._or_stop(asyncio.to_thread(self.source.fetch, row["vid"]))
                except MusicError as e:          # 기본 음원이 막음 → 같은 노래를 대체 음원에서 (쿠키 없을 때 실측 전부 막힘)
                    if e.code != "blocked" or not hasattr(self.source, "fallback"):
                        raise
                    alt = await self._or_stop(asyncio.to_thread(self.source.fallback, row["title"], int(row["duration"] or 0)))
                    path = await self._or_stop(asyncio.to_thread(self.source.fetch, alt["vid"]))
                    await musicq.set_track(self.db, row["id"], alt["title"], alt["url"], alt["vid"], alt["duration"])
                    row.update(title=alt["title"], url=alt["url"], vid=alt["vid"], duration=alt["duration"])
            if not ready:
                if path != row["path"]:
                    await musicq.set_path(self.db, row["id"], path)
                row["path"] = path
                self.row = row
                await self._open(path, int(row["pos_ms"] or 0))
                self._measure_later(self.dec)
        except _Stopped:                                   # 받는 중에 멈춤 → 곡은 그대로 (재시작·닫힘이면 이어서, 끝이면 worker 가 정리)
            self.row = None
            return
        except MusicError as e:
            self.row = None
            await musicq.finish(self.db, row["id"], "failed")
            await self._say("failed", dict(row), e.args[0])
            return
        except Exception as e:                       # ffmpeg 못 켬 등
            log.warning("곡 열기 실패 %s: %r", self.chat_id, e)
            self.row = None
            await musicq.finish(self.db, row["id"], "failed")
            await self._say("failed", dict(row), "노래 파일을 못 열었어요.")
            return
        if self.done:                                      # 받는 사이 끝내기·재시작 → 재생 카드 안 올림
            return
        self.tracks += 1
        self._auto_tried = False
        self.paused, self._paused_at = False, None
        await self._say("now", row)
        self._prefetch()
        self._lyrics_later(row)
        await self._end.wait()
        state = self._end_state
        # 겹쳐 넘어갔으면 self.dec 는 이미 다음 곡 → 이 곡의 풀기는 _prev_dec
        mine = self._prev_dec if (self._carry is not None and self.dec is self._carry["dec"]) else self.dec
        if self.done and self.reason in RESUME_REASONS:
            return                                   # 곡은 playing 그대로 (재시작 뒤 이어서) — 풀기는 run() 이 닫음
        if state == "done" and self.loop > 0 and not self.done:
            self.loop -= 1
            await musicq.requeue_front(self.db, row["id"])   # playing 그대로 두면 start_next 가 같은 곡을 다시
        else:
            if mine and mine.failed and state == "done":
                state = "failed"
                await self._say("failed", row, "노래를 푸는 중 오류가 났어요.")
            await musicq.finish(self.db, row["id"], "removed" if self.done else state)
            if state == "done" and not self.done and (await musicq.modes(self.db, self.chat_id))["loopq"]:
                await musicq.requeue_end(self.db, row)     # 대기열 전체 반복: 다 튼 곡을 맨 뒤로
        self.row = None
        self._quiet_since = self.clock()
        self._prev_dec = None
        if mine:
            if self.dec is mine:
                self.dec = None
            await mine.close()

    async def _or_stop(self, aw):
        """받기처럼 오래 걸릴 수 있는 일을 기다리다가 멈춤(끝·닫힘·재시작)이 오면 바로 빠져나옴 — 일은 뒤에서 끝나게 둠.
        (2026-10-09 18:18: 음성채팅이 닫혔는데 막힌 음원을 받느라 몇 분 동안 끝을 못 내서 '노래 중'으로 남음)"""
        job = asyncio.ensure_future(aw)
        if self.done:
            job.add_done_callback(_ignore)
            raise _Stopped
        stop = asyncio.ensure_future(self._done.wait())
        try:
            await asyncio.wait({job, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if job.done():
            return job.result()
        job.add_done_callback(_ignore)
        raise _Stopped

    # ── 다음 곡 미리 풀기 · 크기 맞추기 · 겹쳐 넘어가기 ──
    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _close_later(self, dec) -> None:
        if dec is not None:
            self._spawn(dec.close())

    def _drop_next(self) -> None:
        """미리 풀어 둔 다음 곡을 버림 (대기열이 바뀜·반복·이동). 겹치는 중이었으면 겹치기도."""
        nxt, self._next, self._xf = self._next, None, None
        if nxt:
            self._close_later(nxt["dec"])

    def _take_ready(self, row: dict) -> dict | None:
        """이 곡을 이미 풀고 있으면 그걸 씀: 겹쳐 넘어가 트는 중(carry) 또는 미리 풀어 둔 것(next). id·시작 위치가 맞을 때만."""
        carry, self._carry = self._carry, None
        if carry is not None:
            if carry["id"] == row["id"]:
                return carry                               # self.dec·pos_ms 는 넘어갈 때 이미 바꿈
            if self.dec is carry["dec"]:                   # 그 사이 대기열이 바뀜 → 그 곡은 버리고 정상으로
                self.dec = None
            self._close_later(carry["dec"])
        nxt = self._next
        if nxt is not None and self._xf is None and nxt["id"] == row["id"] and nxt["pos"] == int(row["pos_ms"] or 0):
            self._next = None
            self.dec, self.pos_ms = nxt["dec"], nxt["pos"]
            self._ncur = getattr(nxt["dec"], "norm", 1.0)
            self.stats["gapless"] += 1
            return nxt
        return None

    async def _norm(self, path: str) -> float:
        if not NORMALIZE or not hasattr(self.source, "loudness"):
            return 1.0
        try:
            return norm_gain(await asyncio.to_thread(self.source.loudness, path))
        except Exception as e:
            log.info("소리 크기 재기 실패 %s: %r", self.chat_id, e)
            return 1.0

    def _measure_later(self, dec) -> None:
        """미리 못 잰 곡 (첫 곡·바로 넘긴 곡): 틀면서 뒤에서 재고, 끝나면 조금씩 그 크기로."""
        if dec is None or not NORMALIZE or not hasattr(self.source, "loudness"):
            return

        async def run():
            n = await self._norm(dec.path)
            if self.dec is dec:
                dec.norm = n
        self._spawn(run())

    def _prefetch(self) -> None:
        """다음 곡을 미리 받고 · 크기 재고 · 풀어 둠 (곡 사이 빈틈 없이 + 겹쳐 넘어가기). 실패는 그 곡 차례에 다시."""
        async def run():
            async with self._arm_lock:
                if self.done or self._next is not None:
                    return
                rows = await musicq.waiting(self.db, self.chat_id, 1)
                if not rows:
                    return
                r = dict(rows[0])
                pos = int(r["pos_ms"] or 0)
                dec = None
                try:
                    path = r["path"] if r["path"] and Path(r["path"]).exists() else None
                    if not path and r["vid"]:
                        path = await asyncio.to_thread(self.source.fetch, r["vid"])
                        await musicq.set_path(self.db, r["id"], path)
                    if not path:
                        return
                    norm = await self._norm(path)
                    if self.done or self.row is None or self.loop > 0:     # 반복 중이면 다음 곡은 나중에
                        return
                    dec = self._decoder(path, pos)
                    dec.norm = norm
                    await dec.start()
                except asyncio.CancelledError:
                    if dec is not None:
                        await dec.close()
                    raise
                except Exception as e:
                    log.info("다음 곡 미리 받기 실패 %s: %r", self.chat_id, e)
                    if dec is not None:
                        await dec.close()
                    return
                if self.done or self._next is not None or self.row is None:
                    await dec.close()
                    return
                self._next = {"id": r["id"], "path": path, "pos": pos, "dec": dec, "checked": False, "checking": False}
        self._spawn(run())

    def _check_next(self, nxt: dict) -> None:
        """곡 끝이 보이면 미리 풀어 둔 곡이 아직 대기열 맨 앞인지 확인 (섞기·빼기·비우기는 봇이 기록만 바꿈)."""
        nxt["checking"] = True

        async def run():
            try:
                rows = await musicq.waiting(self.db, self.chat_id, 1)
                ok = bool(rows) and rows[0]["id"] == nxt["id"]
            except Exception:
                ok = False
            if nxt is not self._next:
                return
            if ok:
                nxt["checked"] = True
            else:                                          # 바뀌었으면 버리고 새 맨 앞 곡으로 (겹치기엔 늦어도 바로 이어 붙이기는)
                self._drop_next()
                self._prefetch()
        self._spawn(run())

    def _maybe_xfade(self, dec) -> None:
        nxt = self._next
        n_xf = int(XFADE * 1000 / audio.FRAME_MS)
        if (n_xf <= 0 or nxt is None or self._xf is not None or self._carry is not None or self.row is None or self.loop > 0
                or self._end.is_set() or not callable(getattr(dec, "frames_left", None))):
            return
        left = dec.frames_left()
        if left is None:                                   # 아직 다 안 풀림 = 끝까지 3초 넘게 남음
            return
        if not nxt["checked"]:
            if not nxt["checking"]:
                self._check_next(nxt)
            return
        ready = getattr(nxt["dec"], "ready", None)
        if left <= n_xf and (ready is None or ready()):
            self._xf = {"n": max(1, left), "i": 0, "got": 0}

    def _promote(self, dec) -> None:
        """앞 곡 끝 → 겹쳐 들어오던 다음 곡이 지금 곡. 앞 곡 _play 는 _end 로 정리, 다음 _play 는 _carry 를 이어받음."""
        nxt, xf = self._next, self._xf or {"got": 0}
        self._prev_dec, self._carry, self._next, self._xf = dec, nxt, None, None
        self.dec = nxt["dec"]
        self._ncur = getattr(self.dec, "norm", 1.0)
        self.pos_ms = nxt["pos"] + xf["got"] * audio.FRAME_MS
        self.stats["xfade"] += 1
        self._end.set()

    def _xf_frame(self, dec):
        """겹치는 조각: 앞 곡은 cos, 다음 곡은 sin 으로 (합친 크기가 일정하게). 앞 곡이 다 끝나면 넘겨줌."""
        nxt, xf = self._next, self._xf
        a, b = dec.frame(), nxt["dec"].frame()
        n, i = xf["n"], xf["i"]
        xf["i"] = i + 1
        k0, k1 = min(1.0, i / n) * math.pi / 2, min(1.0, (i + 1) / n) * math.pi / 2
        size = audio.FRAME_BYTES // 2
        out = None
        if a is not None:
            out = pcm(a) * ramp(self._ncur * math.cos(k0), self._ncur * math.cos(k1), size)
        if b is not None:
            xf["got"] += 1
            nb = getattr(nxt["dec"], "norm", 1.0)
            t = pcm(b) * ramp(nb * math.sin(k0), nb * math.sin(k1), size)
            out = t if out is None else out + t
        if a is None and dec.finished:
            self._promote(dec)
        return out

    def _scaled(self, raw: bytes, dec):
        """크기 맞춤 배수를 곱한 조각 (배수가 늦게 바뀌면 조각마다 조금씩)."""
        target = getattr(dec, "norm", 1.0)
        n0 = self._ncur
        if n0 != target:
            self._ncur = min(target, n0 + NORM_STEP) if target > n0 else max(target, n0 - NORM_STEP)
        if n0 == 1.0 and self._ncur == 1.0:
            return raw
        return pcm(raw) * ramp(n0, self._ncur, audio.FRAME_BYTES // 2)

    def _quiet_for(self) -> float:
        return self.clock() - self._quiet_since

    async def _say(self, kind: str, row, why: str = "") -> None:
        if self.announce:
            try:
                await asyncio.wait_for(self.announce(kind, self.chat_id, row, why), ANNOUNCE_TIMEOUT)
            except Exception as e:                         # 시간 초과 포함 — 안내가 막혀도 노래·멈춤은 계속
                log.info("뮤직 안내 실패 %s: %r", self.chat_id, e)

    async def _pacer(self) -> None:
        """10 ms 마다 한 조각 (노래 + 소담 목소리). 절대 시각 기준 — 늦으면 다음에 덜 잠, 많이 밀리면 다시 맞춤."""
        step = audio.FRAME_MS / 1000
        nxt = self.clock()
        while not self.done:
            music = fade = None
            dec = self.dec
            live = dec is not None and not self.paused and (
                self.row is not None or (self._carry is not None and self._carry["dec"] is dec))
            if live:
                if self._xf is not None and self.loop > 0:    # 겹치는 중에 '반복' → 겹치기 취소 (이 곡을 다시)
                    self._drop_next()
                self._maybe_xfade(dec)
                if self._xf is not None:
                    music = self._xf_frame(dec)
                else:
                    raw = dec.frame()
                    music = self._scaled(raw, dec) if raw is not None else None
                if music is not None:
                    self._last = music
                    if self.dec is dec:                     # 넘겨준 조각이면 pos 는 _promote 가 맞춤
                        self.pos_ms += audio.FRAME_MS
                    self._quiet_since = self.clock()
                elif self.dec is not dec:
                    pass
                elif dec.finished:
                    self._end.set()
                else:
                    self.stats["underrun"] += 1
                    self._starved += 1
                    fade, self._last = self._last, None   # 조각이 비면 무음으로 '딱' 끊지 말고 마지막 조각을 줄이며 끝 → 다시 나오면 천천히 키움
                    if self._starved >= STALL_FRAMES:   # 풀기가 멈춤 (깨진 파일 등) → 무음으로 곡을 붙잡지 않고 넘김
                        log.warning("노래 풀기 멈춤 %s → 다음 곡", self.chat_id)
                        dec.failed = dec.failed or "stalled"
                        self._end.set()
                if music is not None:
                    self._starved = 0
            elif self.paused and self._paused_at is not None and self.clock() - self._paused_at >= PAUSE_MAX \
                    and not self.has_voice():
                self.stop("idle")
                return
            voice = self.voice.popleft() if self.voice else None
            target = 0.0 if self.muted else self.volume / 100 * (DUCK if voice or self._voice_tail > 0 else 1.0)
            self._voice_tail = VOICE_HOLD if voice else max(0, self._voice_tail - 1)   # 말 사이 짧은 틈엔 다시 키우지 않음
            if fade is not None:
                prev, self._gain = self._gain, 0.0
                frame = mix(fade, voice, 0.0, prev)
            elif music is None:                            # 멈춤·곡 사이 → 다음 소리는 0 에서 천천히 (다시 재생·이동·새 곡 '툭' 방지)
                self._gain = 0.0
                frame = mix(None, voice, 0.0)
            else:
                prev, self._gain = self._gain, glide(self._gain, target)
                frame = mix(music, voice, self._gain, prev)
            t_send = self.clock()
            try:
                await self._send(frame)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("노래 소리 보내기 실패 %s: %s", self.chat_id, e)
                self.stop("chat_closed" if "not in a call" in str(e).lower() else "error:play")
                return
            self.stats["frames"] += 1
            took = (self.clock() - t_send) * 1000         # ntgcalls 는 버퍼 없이 바로 WebRTC 로 → 보내기가 밀리면 그대로 '튐' (소스 확인 2026-10-08)
            if took > 20:
                self.stats["send_slow"] += 1
            self.stats["send_max_ms"] = max(self.stats["send_max_ms"], int(took))
            nxt += step
            wait = nxt - self.clock()
            if wait > 0:
                await self.sleep(wait)
            else:
                late = -wait * 1000
                if late > 30:                   # 30ms 넘게 밀림 = 받는 쪽 버퍼가 비어 '튐' 가능
                    self.stats["jitter"] += 1
                self.stats["max_late_ms"] = max(self.stats["max_late_ms"], int(late))
                self.win_max_late = max(self.win_max_late, int(late))
                if wait < -RESYNC:              # 밀렸으면 몰아서 보내지 말고 지금 박자로 다시 — ntgcalls 는 받아 둘 통이 없어서
                    self.stats["late"] += 1     # 몰아 보낸 만큼 듣는 쪽이 빨리 감아 틂 ('멈췄다가 2배속', 2026-10-09)
                    nxt = self.clock()
