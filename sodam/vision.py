"""사진·영상: 요청 메시지(또는 답장한 메시지)의 사진·영상을 받아 AI 에게 보여주고, 부탁하면 이미지를 만들거나 고친다.

- 읽기: 텔레그램이 준 가장 큰 해상도 → base64 → chat.completions 의 image_url(detail=high, 긴 변 2048px 까지 읽음)
- 영상·GIF·영상 스티커·동그라미 영상: OpenAI 비전은 영상을 직접 못 봄(공식 쿡북도 장면 추출) → ffmpeg 로 장면 3~8장을
  고르게 뽑아 긴 변 512px JPEG + '[3.0초]' 표시로 보여줌 (16:9 한 장 ≈ 173 토큰, 8장 ≈ $0.0035). 20MB 넘으면(봇 getFile 한도)
  텔레그램 미리보기 한 장만 보고 그렇다고 알림. 움직이는 스티커(TGS)는 미리보기. data = 대표 장면(가운데) → 움프·그림 고치기 원본.
- 만들기·고치기: tools.make_image → llm.image (gpt-image 계열). 방마다 하루 한도(image_daily).
사진 속 글자는 '데이터'다 (prompt.py 규칙). 파일은 디스크에 저장하지 않는다.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from telegram.error import TelegramError

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024       # 텔레그램 봇이 받을 수 있는 파일 한도와 같음
IMAGE_MIME = ("image/jpeg", "image/png", "image/webp", "image/gif")   # OpenAI 비전이 읽는 형식


FRAME_SIDE = 512
FRAMES_MIN, FRAMES_MAX, SHORT_FRAMES = 4, 8, 3   # 영상 = 길이/2초 (4~8장), GIF·스티커 = 3장
_FF = asyncio.Semaphore(1)                       # ffmpeg 는 한 번에 하나 (2 vCPU)
FF_TIMEOUT = 20


@dataclass
class Attached:
    data: bytes
    mime: str
    owner: int | None = None   # 사진을 올린 사람 (요청 글 또는 답장한 글의 작성자)
    kind: str = "photo"        # photo · video · gif · video_note · sticker
    frames: list = field(default_factory=list)   # 영상: [(초, jpeg)] — AI 에게 보여줄 장면들
    note: str = ""             # AI 에게 알려줄 한 줄 ('영상 12초, 장면 6장' · '20MB 넘어서 미리보기만')

    def part(self) -> dict:
        """chat.completions user content 에 넣는 이미지 조각 (대표 한 장)."""
        return _img(self.data, self.mime)

    def parts(self) -> list[dict]:
        """AI 에게 줄 조각 전부: 영상이면 설명 한 줄 + 장면마다 [n초] + 이미지."""
        if not self.frames:
            return ([{"type": "text", "text": f"[첨부: {self.note}]"}] if self.note else []) + [self.part()]
        out: list[dict] = [{"type": "text", "text": f"[첨부 {self.note} — 아래는 고르게 뽑은 장면이라 빠른 동작·작은 글씨는 놓칠 수 있음]"}]
        for t, jpg in self.frames:
            out += [{"type": "text", "text": f"[{t:.1f}초]"}, _img(jpg, "image/jpeg")]
        return out


def _img(data: bytes, mime: str) -> dict:
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url, "detail": "high"}}


@dataclass
class Media:
    file_id: str
    mime: str
    size: int
    kind: str                  # photo · image · video · gif · video_note · sticker
    duration: float = 0
    thumb: str | None = None   # 텔레그램 미리보기 file_id (20MB 넘을 때·TGS)


def _media_of(m) -> Media | None:
    """메시지의 사진·이미지 파일·영상·GIF·동그라미 영상·스티커 (없으면 None)."""
    if m is None:
        return None
    photos = getattr(m, "photo", None) or ()
    if photos:
        p = photos[-1]                                  # 텔레그램은 작은 것부터 준다
        return Media(p.file_id, "image/jpeg", p.file_size or 0, "photo")
    thumb = lambda o: getattr(getattr(o, "thumbnail", None), "file_id", None)   # noqa: E731
    for attr, kind in (("animation", "gif"), ("video", "video"), ("video_note", "video_note")):   # GIF 는 document 도 같이 옴 → 먼저
        v = getattr(m, attr, None)
        if v:
            return Media(v.file_id, getattr(v, "mime_type", None) or "video/mp4", v.file_size or 0, kind,
                         float(getattr(v, "duration", 0) or 0), thumb(v))
    st = getattr(m, "sticker", None)
    if st:
        if getattr(st, "is_video", False):
            return Media(st.file_id, "video/webm", st.file_size or 0, "sticker", 3, thumb(st))
        if getattr(st, "is_animated", False):          # TGS(Lottie) 는 그리지 않고 미리보기
            return Media(thumb(st) or "", "image/webp", 0, "sticker", 0, None) if thumb(st) else None
        return Media(st.file_id, "image/webp", st.file_size or 0, "sticker")
    doc = getattr(m, "document", None)
    if doc:
        mime = doc.mime_type or ""
        if mime in IMAGE_MIME:
            return Media(doc.file_id, mime, doc.file_size or 0, "image")
        if mime.startswith("video/"):
            return Media(doc.file_id, mime, doc.file_size or 0, "video", 0, thumb(doc))
    return None


def _file_of(m) -> tuple[str, str, int] | None:
    """(file_id, mime, 크기) — 사진·이미지 파일만 (예전 모양, 테스트용)."""
    md = _media_of(m)
    return (md.file_id, md.mime, md.size) if md and md.kind in ("photo", "image") else None


def has_image(msg) -> bool:
    return _media_of(msg) is not None


def has_photo(msg) -> bool:
    """사진·이미지 파일만 (1:1 에 사진만 보내면 읽어 줌 — 스티커·영상만 보낸 건 대화로 안 봄)."""
    md = _media_of(msg)
    return md is not None and md.kind in ("photo", "image")


def describe(m) -> str | None:
    """답장 대상이 글 없는 사진·영상일 때 AI 에게 알려줄 한 줄 (예: '[영상 12초, 글 없음]')."""
    md = _media_of(m)
    if not md:
        return None
    name = {"photo": "사진", "image": "이미지", "video": "영상", "gif": "GIF", "video_note": "동그라미 영상", "sticker": "스티커"}[md.kind]
    return f"[{name}{f' {md.duration:.0f}초' if md.duration else ''}]"


def _probe_duration(path: str) -> float:
    from .avatar import ffmpeg_path
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path], capture_output=True, text=True, timeout=10).stderr
    for line in out.splitlines():
        if "Duration:" in line:
            try:
                h, mi, se = line.split("Duration:")[1].split(",")[0].strip().split(":")
                return int(h) * 3600 + int(mi) * 60 + float(se)
            except ValueError:
                return 0
    return 0


def extract_frames(data: bytes, duration: float, n: int) -> list[tuple[float, bytes]]:
    """영상 bytes → 고르게 n 장 [(초, 긴 변 512px JPEG)]. 입력 앞 -ss 로 빠르게 (2 vCPU). 동기 — to_thread 로 부를 것."""
    from .avatar import ffmpeg_path
    with tempfile.TemporaryDirectory(prefix="sodam_vid_") as tmp:
        src = os.path.join(tmp, "in")
        with open(src, "wb") as f:
            f.write(data)
        dur = duration or _probe_duration(src) or 1.0
        out = []
        for i in range(n):
            t = round((i + 0.5) * dur / n, 2)
            p = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src,
                                "-frames:v", "1", "-vf", f"scale={FRAME_SIDE}:{FRAME_SIDE}:force_original_aspect_ratio=decrease",
                                "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True, timeout=FF_TIMEOUT)
            if p.returncode == 0 and p.stdout:
                out.append((t, p.stdout))
        return out


async def _download(bot, file_id: str) -> bytes | None:
    try:
        f = await bot.get_file(file_id)
        return bytes(await f.download_as_bytearray())
    except TelegramError as e:
        log.warning("media download failed: %s", e)
        return None


async def fetch(bot, msg) -> Attached | None:
    """요청 메시지의 사진·영상, 없으면 답장한 메시지의 것. 없거나 못 받으면 None."""
    src = msg if _media_of(msg) else getattr(msg, "reply_to_message", None)
    md = _media_of(src)
    if not md:
        return None
    who = getattr(getattr(src, "from_user", None), "id", None)
    if md.kind in ("photo", "image") or (md.kind == "sticker" and md.mime == "image/webp"):
        if md.size > MAX_BYTES:
            return None
        data = await _download(bot, md.file_id)
        return Attached(data, md.mime, who, md.kind) if data and len(data) <= MAX_BYTES else None
    label = describe(src).strip("[]")
    if md.size > MAX_BYTES:                               # 봇은 20MB 넘는 파일을 못 받음 → 미리보기 한 장
        thumb = await _download(bot, md.thumb) if md.thumb else None
        return Attached(thumb, "image/jpeg", who, md.kind, note=f"{label} — 20MB 넘어서 미리보기 한 장만 봄") if thumb else None
    data = await _download(bot, md.file_id)
    if not data:
        return None
    n = SHORT_FRAMES if md.kind in ("gif", "sticker") or md.duration and md.duration <= 6 else \
        min(FRAMES_MAX, max(FRAMES_MIN, round((md.duration or 8) / 2)))
    try:
        async with _FF:
            frames = await asyncio.wait_for(asyncio.to_thread(extract_frames, data, md.duration, n), FF_TIMEOUT * FRAMES_MAX)
    except Exception as e:   # 이상한 파일·시간 초과 → 미리보기로
        log.warning("frame extract failed: %s", e)
        frames = []
    if not frames:
        thumb = await _download(bot, md.thumb) if md.thumb else None
        return Attached(thumb, "image/jpeg", who, md.kind, note=f"{label} — 장면을 못 뽑아 미리보기만 봄") if thumb else None
    mid = frames[len(frames) // 2][1]
    return Attached(mid, "image/jpeg", who, md.kind, frames, f"{label}, 장면 {len(frames)}장")
