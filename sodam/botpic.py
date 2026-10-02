"""🖼 봇 프로필 바꾸기 (오너 `.봇프사`) — Bot API 9.4 setMyProfilePhoto / removeMyProfilePhoto.

오너 결정 2026-10-03: @BotFather 는 사진만 받음 → 움직이는 프로필(MP4)은 API 로만. 소담 1:1 에 영상·GIF·사진을 보내고
그 글에 답장으로 `.봇프사` (또는 캡션으로) → 정사각형·소리 없음·2MB 아래로 다듬어서 소담 프로필로. `.봇프사 원래대로` = 지움
(지우면 바로 전 프사가 다시 보임). python-telegram-bot 21 에 이 메서드가 없어서 HTTP 로 직접 부름.
문서: https://core.telegram.org/bots/api#setmyprofilephoto · InputProfilePhotoAnimated(type=animated, animation, main_frame_timestamp)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile

import httpx

log = logging.getLogger(__name__)

SIDE = 800              # 텔레그램 앱이 프로필 영상을 만들 때 쓰는 크기
MAX_SEC = 10
MAX_BYTES = 2 * 1024 * 1024
API = "https://api.telegram.org/bot{token}/{method}"


def square_video(data: bytes) -> bytes:
    """영상 → 정사각형(가로 영상은 가운데, 세로 영상은 위쪽 — 얼굴이 보통 위), 800px, 10초, 소리 없음, H.264, 2MB 아래.
    동기 — to_thread 로 부를 것."""
    from .avatar import ffmpeg_path
    crop = ("crop='min(iw,ih)':'min(iw,ih)':'(iw-min(iw,ih))/2':'if(gt(ih,iw),(ih-iw)/4,0)',"
            f"scale={SIDE}:{SIDE},fps=30,format=yuv420p")
    with tempfile.TemporaryDirectory(prefix="sodam_botpic_") as tmp:
        src, out = os.path.join(tmp, "in"), os.path.join(tmp, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        for crf in (23, 27, 31, 35):
            p = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", str(MAX_SEC),
                                "-an", "-vf", crop, "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                                "-movflags", "+faststart", out], capture_output=True, timeout=120)
            if p.returncode != 0:
                raise RuntimeError(p.stderr.decode(errors="replace")[-300:])
            if os.path.getsize(out) <= MAX_BYTES:
                break
        with open(out, "rb") as f:
            return f.read()


async def _call(token: str, method: str, data: dict | None = None, files: dict | None = None) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(API.format(token=token, method=method), data=data, files=files)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"연결 오류: {type(e).__name__}"
    return bool(body.get("ok")), str(body.get("description") or "")


async def set_photo(token: str, data: bytes, animated: bool, main_frame: float = 0.0) -> tuple[bool, str]:
    if animated:
        photo = {"type": "animated", "animation": "attach://pic", "main_frame_timestamp": main_frame}
        files = {"pic": ("pic.mp4", data, "video/mp4")}
    else:
        photo = {"type": "static", "photo": "attach://pic"}
        files = {"pic": ("pic.jpg", data, "image/jpeg")}
    return await _call(token, "setMyProfilePhoto", {"photo": json.dumps(photo)}, files)


async def remove(token: str) -> tuple[bool, str]:
    return await _call(token, "removeMyProfilePhoto")


def _media(m) -> tuple[str, str] | None:
    """(file_id, 'video'|'photo') — 영상·GIF·동그라미 영상·영상 파일·사진."""
    if m is None:
        return None
    for attr in ("animation", "video", "video_note"):
        v = getattr(m, attr, None)
        if v:
            return v.file_id, "video"
    doc = getattr(m, "document", None)
    if doc and (doc.mime_type or "").startswith("video/"):
        return doc.file_id, "video"
    if getattr(m, "photo", None):
        return m.photo[-1].file_id, "photo"
    return None


async def run(ctx) -> None:
    """CmdCtx (오너·1:1). 이 글 또는 답장한 글의 영상·사진 → 봇 프로필."""
    token = ctx.svc.cfg.telegram_token
    if ctx.args and ctx.args[0] in ("원래대로", "지우기", "삭제", "reset"):
        ok, why = await remove(token)
        await ctx.reply("🖼 봇 프로필을 지웠어요 (전에 쓰던 프사가 있으면 그게 다시 보여요)." if ok
                        else f"❌ 못 지웠어요: {why}")
        return
    found = _media(ctx.msg) or _media(getattr(ctx.msg, "reply_to_message", None))
    if not found:
        await ctx.reply("🖼 프로필로 쓸 <b>영상·GIF·사진</b>을 이 1:1 에 보내고, 그 글에 <b>답장</b>으로 <code>.봇프사</code> 라고 쳐 주세요.\n"
                        "되돌리기: <code>.봇프사 원래대로</code>")
        return
    file_id, kind = found
    try:
        f = await ctx.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
    except Exception as e:
        await ctx.reply(f"❌ 파일을 못 받았어요 (20MB 넘으면 봇이 못 받아요): {type(e).__name__}")
        return
    if kind == "video":
        await ctx.reply("🎞 정사각형·소리 없이 다듬는 중… (10~30초)")
        try:
            data = await asyncio.to_thread(square_video, data)
        except Exception as e:
            log.warning("botpic convert failed: %s", e)
            await ctx.reply("❌ 영상을 다듬지 못했어요. 다른 파일로 해 주세요.")
            return
    ok, why = await set_photo(token, data, kind == "video")
    if ok:
        await ctx.reply("✅ 소담 프로필을 바꿨어요! 모든 방에 바로 보여요. 되돌리기: <code>.봇프사 원래대로</code>")
    else:
        await ctx.reply(f"❌ 텔레그램이 거절했어요: {why}")
