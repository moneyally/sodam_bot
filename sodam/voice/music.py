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
import logging
import os
import random
import re
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import audio, musicq

log = logging.getLogger("sodam.voice.music")

MAX_SEC = int(os.getenv("MUSIC_MAX_SEC", "1200"))          # 한 곡 최대 길이 (20분 — 라이브·몇 시간짜리 믹스 막기)
CACHE_MB = int(os.getenv("MUSIC_CACHE_MB", "600"))
IDLE_SEC = float(os.getenv("MUSIC_IDLE_SEC", "180"))       # 틀 곡 없음·일시정지가 이만큼이면 음성채팅에서 나감
PAUSE_MAX = float(os.getenv("MUSIC_PAUSE_MAX", "900"))     # 일시정지는 15분까지 기다림
STALL_FRAMES = 1500                                        # 15초 동안 풀린 조각이 없으면 곡을 끝냄
DUCK = 0.25                                                # 소담이 말하는 동안 노래 크기
AHEAD = 300                                                # 미리 풀어 두는 조각 (3초) — 디스크·CPU 가 잠깐 늦어도 안 끊김
VOICE_KEEP = 60                                            # 섞을 소담 목소리 조각 (0.6초 넘게 밀리면 오래된 것부터 버림)
SILENCE = bytes(audio.FRAME_BYTES)
LINK_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/|music\.youtube\.com/watch\?(?:[^#\s]*&)?v=)"
                   r"([A-Za-z0-9_-]{11})")
URL = re.compile(r"https?://\S+")
WATCH = "https://www.youtube.com/watch?v={}"
OEMBED = "https://www.youtube.com/oembed?format=json&url="


class MusicError(Exception):
    """방에 그대로 보일 이유 (code = blocked·not_found·too_long·live·download·decode)."""

    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code


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
VARIANT = ("remix", "cover", "slowed", "sped up", "speed up", "nightcore", "inst", "instrumental", "karaoke", "8d", "reverb", "mashup",
           "disco", "80s", "lofi", "lo-fi", "phonk", "bootleg", "flip", "version", "ver.", "bass boost", "mix)",
           "live", "concert", "라이브", "콘서트",
           "cello", "violin", "guitar", "duo", "orgel", "music box", "kalimba", "8bit", "8-bit", "첼로", "바이올린", "오르골",
           "원곡", "부른", "불러", "covered", "커버곡",       # '(원곡 이문세) 이보람' = 다른 가수가 부른 것 (실측)
           "리믹스", "커버", "반주", "피아노", "piano", "acoustic", "jersey", "edit")
HINT_NEED = 0.75                # 기본 음원 제목 낱말로 맞출 땐 이만큼 (한글 신청 '뉴진스 하입보이' ↔ 'NewJeans - Hype Boy')
ALT_TRY = 4                      # 실제로 받아지는지 확인해 볼 후보 수 (DRM 잠긴 공식 음원 건너뛰기)
ALT_MIN_SEC = 45                 # 대체 음원 미리듣기(30초) 조각은 건너뜀
PRIMARY_RETRY = int(os.getenv("MUSIC_RETRY_SEC", "3600"))   # 기본 음원이 막힌 뒤 이만큼은 대체 음원 먼저 (쿠키가 바뀌면 바로 다시)
_NOISE = re.compile(r"[\[(【](?:[^\])】]*?(?:mv|m/v|official|lyrics?|가사|audio|video|live|4k|hd|remaster)[^\])】]*)[\])】]", re.I)


_WORDS = re.compile(r"(?i)\b(?:official\s*(?:music\s*)?(?:video|mv|audio)|m/?v|lyrics?|live\s*clip|lyric\s*video)\b")


def _norm(text: str) -> str:
    """소문자 + 악센트 뺌 (Beyoncé → beyonce) — 일본어·중국어 글자는 그대로."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return unicodedata.normalize("NFC", "".join(c for c in t if not unicodedata.combining(c)))   # 한글은 다시 붙임


def _words(text: str) -> list[str]:
    """비교용 낱말 (2글자↑ — 한글·영문·숫자·일본어·중국어 등 모든 글자. 예전엔 한글·영문만이라 일본 노래는 낱말이 비어 아무 곡이나 통과)."""
    return [w for w in re.findall(r"[^\W_]+", _norm(text)) if len(w) >= 2]


def _variant(title: str, asked: str) -> bool:
    """신청에 없는 리믹스·커버… 영어는 낱말 단위('edit' ⊄ 'edition', 'inst' ⊄ 'instinct'), 한글은 글자 포함."""
    for v in VARIANT:
        if v in asked:
            continue
        if v.isascii():
            if re.search(rf"(?<![0-9a-z]){re.escape(v)}(?![0-9a-z])", title):
                return True
        elif v in title:
            return True
    return False


def clean_title(title: str) -> str:
    """'[MV] Paul Kim(폴킴) _ Me After You(너를 만나) [가사/Lyrics]' → 다른 곳에서 찾을 검색어 (괄호 속 MV·가사 같은 것 뺌)."""
    t = _NOISE.sub(" ", title or "")
    t = _WORDS.sub(" ", t)
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
        return time.time() - self.blocked_at >= PRIMARY_RETRY

    def _opts(self, cookie: str | None, **extra) -> dict:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 15, "retries": 2,
                "logger": _Quiet(), "noprogress": True}
        deno_bin = _deno()
        if deno_bin:
            opts["js_runtimes"] = {"deno": {"path": deno_bin}}
        if cookie:
            opts["cookiefile"] = cookie
        if os.getenv("MUSIC_PROXY"):                 # 막히면 주거용 프록시 (선택, 오너가 .env 에)
            opts["proxy"] = os.environ["MUSIC_PROXY"]
        opts.update(extra)
        return opts

    def _run(self, fn: Callable[[dict], Any], *, cookies: bool = True, where: str = "기본 음원", **extra):
        """쿠키가 있으면 쿠키부터(여러 개면 무작위 순서), '봇이냐?'로 막히면 다음 쿠키 → 마지막에 쿠키 없이.
        (쿠키 없이 먼저 두드리면 막힌 IP 로 요청만 늘어남)"""
        import yt_dlp
        self._env()
        jar = self.cookies() if cookies else []
        tries: list[str | None] = random.sample(jar, len(jar)) + [None]
        last = ""
        for cookie in tries:
            try:
                with yt_dlp.YoutubeDL(self._opts(cookie, **extra)) as ydl:
                    return fn(ydl)
            except yt_dlp.utils.DownloadError as e:
                last = str(e)
                if not _blocked(last):
                    break
        if _blocked(last):
            self.blocked_at = time.time()
            raise MusicError("blocked", "음원 서버가 잠깐 막혔어요 (운영자: 🎵 화면에서 인증 쿠키를 넣어 주세요).")
        log.info("%s 못 가져옴: %s", where, _short(last, 300))
        raise MusicError("download", f"노래를 못 가져왔어요: {_short(_hide_src(last))}")

    def _info(self, ydl, url: str) -> dict:
        info = ydl.extract_info(url, download=False)
        return info or {}

    def resolve(self, query: str) -> dict:
        """검색어·링크 → {title, url, vid, duration}. 기본 음원이 막혀 있으면 대체 음원에서 같은 노래."""
        vid = link_id(query)
        if vid:
            try:
                return self._check(self._run(lambda y: self._info(y, WATCH.format(vid))))
            except MusicError as e:
                if e.code != "blocked":
                    raise
                title = self._oembed_title(vid)      # 영상 제목은 막힌 IP 에서도 oEmbed 로 됨
                if not title:
                    raise
                return self.alt_search(clean_title(title), fallback_of=e, need=0.6)
        if URL.search(query):
            raise MusicError("not_found", "노래 링크나 제목만 돼요.")
        # 기본 음원 검색은 막힌 IP 에서도 됨 (실측) → 정확한 곡 이름은 기본 음원에서 (대체 음원 검색은 리믹스·커버가 먼저 나오기도)
        res = self._run(lambda y: y.extract_info(f"ytsearch5:{query}", download=False), extract_flat="in_playlist")
        info = None
        for e in (res or {}).get("entries") or []:
            if not e or not e.get("id"):
                continue
            dur = int(e.get("duration") or 0)
            if e.get("live_status") in ("is_live", "is_upcoming") or (dur and dur > self.max_sec):
                continue
            info = {"title": e.get("title") or query, "url": WATCH.format(e['id']),
                    "vid": e["id"], "duration": dur}
            break
        if info and self.primary_ok():
            return info
        last = None
        for q in ([clean_title(info["title"])] if info else []) + [query]:   # 기본 음원이 막혀 있음 → 같은 노래를 대체 음원에서
            try:
                return self.alt_search(q, wanted=query, ref_sec=info["duration"] if info else 0)
            except MusicError as e:
                last = e
        if info and not self.cookies():
            raise last                                # 쿠키도 없음 → 기본 음원으로 가 봐야 막힘 (안내는 대체 음원 쪽 이유)
        if info:
            return info                               # 대체 음원에도 없음 → 기본 음원으로 (쿠키가 생겼을 수도)
        raise MusicError("not_found", f"'{_short(query, 40)}' 노래를 못 찾았어요.")

    def alt_search(self, query: str, fallback_of: MusicError | None = None, wanted: str = "", ref_sec: int = 0,
                   need: float = 1.0) -> dict:
        """대체 음원 검색 → 같은 노래 한 곡 (엉뚱한 곡을 트느니 못 찾았다고 함 — 실측: 공식 음원이 DRM 이면 '다이너마이트'에
        '버터'가 걸림). 받아들이는 곡: 신청 낱말(wanted, 없으면 query)을 need 비율 이상 담고 · 신청에 없는 리믹스·커버·라이브가
        아니고 · 기본 음원 원곡 길이(ref_sec)와 비슷함. 그중 낱말이 많이 겹치고 딴 이름이 덜 붙은 순, DRM 으로 잠긴 건 건너뜀.
        vid = 'sc<번호>'."""
        res = self._run(lambda y: y.extract_info(f"scsearch10:{query}", download=False), cookies=False,
                        where="대체 음원", extract_flat="in_playlist")
        asked = _norm(f"{wanted} {query}")
        core = set(_words(wanted or query))                # 신청한 말 (가수·제목) — 가장 중요
        hint = set(_words(asked)) - core                    # 기본 음원 제목에서 온 낱말 (영어 제목 등)
        ok = []
        for e in (res or {}).get("entries") or []:
            if not e or not str(e.get("id") or "").isdecimal():
                continue
            dur = int(e.get("duration") or 0)
            if dur < ALT_MIN_SEC or dur > self.max_sec:
                continue
            if ref_sec and abs(dur - ref_sec) > max(30, ref_sec * 0.2):
                continue                                    # 원곡과 길이가 다름 (믹스·조각·다른 곡)
            title = _norm(str(e.get("title") or ""))
            if _variant(title, asked):
                continue
            core_ok = bool(core) and sum(1 for w in core if w in title) >= need * len(core)   # 낱말이 없으면 공짜 통과 아님
            hint_ok = len(hint) >= 2 and sum(1 for w in hint if w in title) >= HINT_NEED * len(hint)
            if not (core_ok or hint_ok):                   # 한글로 신청해도 영어 제목(기본 음원 제목 낱말)이면 같은 곡
                continue
            got = set(_words(title))
            score = 3 * sum(1 for w in core if w in title) + sum(1 for w in hint if w in title) \
                - 0.4 * len([w for w in got if w not in asked])   # 신청에 없는 이름이 붙음 (다른 가수 커버 등)
            ok.append((score, {"title": e.get("title") or query, "url": ALT_TRACK.format(e["id"]), "vid": f"sc{e['id']}",
                               "duration": dur}))
        ok.sort(key=lambda x: x[0], reverse=True)           # 같은 점수면 대체 음원 순서 그대로 (sort 는 안정)
        for _, t in ok[:ALT_TRY]:
            try:
                self._run(lambda y: y.extract_info(t["url"], download=False), cookies=False, where="대체 음원")
                return t
            except MusicError as e:
                log.info("대체 음원 후보 건너뜀 %s: %s", t["vid"], e)
        if fallback_of is not None:
            raise fallback_of
        raise MusicError("not_found", f"'{_short(wanted or query, 40)}' — 같은 노래를 못 찾았어요"
                         " (운영자: 🎵 화면에서 인증 쿠키를 넣으면 돼요).")

    def fallback(self, title: str, ref_sec: int = 0) -> dict:
        """기본 음원 곡을 받으려다 막힘 → 대체 음원에서 같은 노래 (Player 가 부름). 기본 음원 제목은 낱말이 많아 60%만."""
        return self.alt_search(clean_title(title), wanted=clean_title(title), ref_sec=ref_sec, need=0.6)

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
    return URL.sub("", msg).strip()


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


def mix(music: bytes | None, voice: bytes | None, gain: float) -> bytes:
    """노래(gain 배) + 소담 목소리. 둘 다 없으면 무음."""
    if not music and not voice:
        return SILENCE
    out = np.zeros(audio.FRAME_BYTES // 2, dtype=np.float32)
    if music and gain > 0:
        m = np.frombuffer(music[: audio.FRAME_BYTES], dtype="<i2").astype(np.float32)
        out[: len(m)] += m * gain
    if voice:
        v = np.frombuffer(voice[: audio.FRAME_BYTES], dtype="<i2").astype(np.float32)
        out[: len(v)] += v
    return np.clip(out, -32768, 32767).astype("<i2").tobytes()


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
        self.stats = {"frames": 0, "late": 0, "underrun": 0}
        self._starved = 0

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

    def skip(self) -> bool:
        if not self.row:
            return False
        self.loop = 0
        self._end_state = "skipped"
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
        try:
            await self._conductor()
        finally:
            pacer.cancel()
            with contextlib.suppress(BaseException):
                await pacer
            if self.dec:
                await self.dec.close()
                self.dec = None
            if self.row and self.reason in ("restart",):           # 재시작: 곡 위치를 남겨 이어서
                with contextlib.suppress(Exception):
                    await musicq.save_pos(self.db, self.row["id"], self.pos_ms)
        return self.reason or "end"

    async def _open(self, path: str, start_ms: int) -> None:
        old, self.dec = self.dec, None
        if old:
            await old.close()
        dec = self._decoder(path, start_ms)
        await dec.start()
        self.dec, self.pos_ms = dec, start_ms

    async def _conductor(self) -> None:
        while not self.done:
            row = await musicq.start_next(self.db, self.chat_id)
            if self.done:                                  # 꺼내는 사이 멈춤 (곡은 playing 그대로 — 끝 정리·재시작이 처리)
                return
            if not row:
                if self._quiet_for() >= self.idle_sec and not self.has_voice():
                    return self.stop("idle")
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.poll)
                continue
            await self._play(row)

    async def _play(self, row) -> None:
        self._end.clear()
        self._end_state = "done"
        if self.done:                                      # stop() 의 _end 를 방금 지웠을 수 있음 → 기다리지 않고 끝
            return
        try:
            row = dict(row)
            if row["path"] and Path(row["path"]).exists():
                path = row["path"]
            else:
                try:
                    path = await asyncio.to_thread(self.source.fetch, row["vid"])
                except MusicError as e:          # 기본 음원이 막음 → 같은 노래를 대체 음원에서 (쿠키 없을 때 실측 전부 막힘)
                    if e.code != "blocked" or not hasattr(self.source, "fallback"):
                        raise
                    alt = await asyncio.to_thread(self.source.fallback, row["title"], int(row["duration"] or 0))
                    path = await asyncio.to_thread(self.source.fetch, alt["vid"])
                    await musicq.set_track(self.db, row["id"], alt["title"], alt["url"], alt["vid"], alt["duration"])
                    row.update(title=alt["title"], url=alt["url"], vid=alt["vid"], duration=alt["duration"])
            if path != row["path"]:
                await musicq.set_path(self.db, row["id"], path)
            row["path"] = path
            self.row = row
            await self._open(path, int(row["pos_ms"] or 0))
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
        self.paused, self._paused_at = False, None
        await self._say("now", row)
        self._prefetch()
        await self._end.wait()
        state = self._end_state
        if self.done and self.reason == "restart":
            return                                   # 곡은 playing 그대로 (재시작 뒤 이어서)
        if state == "done" and self.loop > 0 and not self.done:
            self.loop -= 1
            await musicq.requeue_front(self.db, row["id"])   # playing 그대로 두면 start_next 가 같은 곡을 다시
        else:
            if self.dec and self.dec.failed and state == "done":
                state = "failed"
                await self._say("failed", row, "노래를 푸는 중 오류가 났어요.")
            await musicq.finish(self.db, row["id"], "removed" if self.done else state)
        self.row = None
        self._quiet_since = self.clock()
        if self.dec:
            await self.dec.close()
            self.dec = None

    def _prefetch(self) -> None:
        """다음 곡을 미리 받아 둠 (곡 사이 빈틈 줄이기). 실패는 그 곡 차례에 다시."""
        async def run():
            rows = await musicq.waiting(self.db, self.chat_id, 1)
            if rows and rows[0]["vid"] and not rows[0]["path"]:
                try:
                    path = await asyncio.to_thread(self.source.fetch, rows[0]["vid"])
                    await musicq.set_path(self.db, rows[0]["id"], path)
                except Exception as e:
                    log.info("다음 곡 미리 받기 실패 %s: %r", self.chat_id, e)
        t = asyncio.create_task(run())
        self.__dict__.setdefault("_bg", set()).add(t)
        t.add_done_callback(self.__dict__["_bg"].discard)

    def _quiet_for(self) -> float:
        return self.clock() - self._quiet_since

    async def _say(self, kind: str, row, why: str = "") -> None:
        if self.announce:
            try:
                await self.announce(kind, self.chat_id, row, why)
            except Exception as e:
                log.info("뮤직 안내 실패 %s: %r", self.chat_id, e)

    async def _pacer(self) -> None:
        """10 ms 마다 한 조각 (노래 + 소담 목소리). 절대 시각 기준 — 늦으면 다음에 덜 잠, 많이 밀리면 다시 맞춤."""
        step = audio.FRAME_MS / 1000
        nxt = self.clock()
        while not self.done:
            music = None
            dec = self.dec
            if dec and self.row and not self.paused:
                music = dec.frame()
                if music is not None:
                    self.pos_ms += audio.FRAME_MS
                    self._quiet_since = self.clock()
                elif dec.finished:
                    self._end.set()
                else:
                    self.stats["underrun"] += 1
                    self._starved += 1
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
            gain = 0.0 if self.muted else self.volume / 100 * (DUCK if voice else 1.0)
            try:
                await self._send(mix(music, voice, gain))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("노래 소리 보내기 실패 %s: %s", self.chat_id, e)
                self.stop("chat_closed" if "not in a call" in str(e).lower() else "error:play")
                return
            self.stats["frames"] += 1
            nxt += step
            wait = nxt - self.clock()
            if wait > 0:
                await self.sleep(wait)
            elif wait < -0.2:
                self.stats["late"] += 1
                nxt = self.clock()
