"""🎞️ 움프 AI 도구 make_profile_video (sodam/avatar.py 로 영상, 그림체는 llm.image 고치기).

원본 = 요청·답장에 붙은 사진 (남의 사진도 됨 — 사용자 결정 2026-09-28, 하루 한도로 충분), 없으면 요청자의 지금 프사.
효과 = avatar.Spec 부품 조합 (움직임·빠르기·색·날리는 것) — 사용자가 말한 효과는 AI 가 가장 가까운 부품으로 옮김.
한도: 사람마다 하루 FREE_DAILY 개, AI 그림체는 이 방 이미지 한도(image_daily)도 같이 씀 (한 장 ≈ $0.03).
결과는 파일(문서)로 보냄 — 동영상으로 보내면 텔레그램이 다시 압축해서 프로필 규격이 깨질 수 있음.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from openai import BadRequestError, OpenAIError
from telegram import InputFile
from telegram.constants import ChatAction
from telegram.error import TelegramError

from .. import avatar, tools
from ..llm import BudgetExceeded
from ..util import display_name, esc
from ..vision import Attached

log = logging.getLogger(__name__)
FREE_DAILY = 5
GUIDE = "텔레그램 → 설정 → 프로필 사진 설정 → 이 파일 (먼저 기기에 저장)"


async def _profile_photo(bot, uid: int) -> bytes | None:
    try:
        got = await bot.get_user_profile_photos(uid, limit=1)
        if not got or not got.photos:
            return None
        f = await bot.get_file(got.photos[0][-1].file_id)
        return bytes(await f.download_as_bytearray())
    except TelegramError as e:
        log.info("profile photo fetch failed %s: %s", uid, e)
        return None


async def _busy(ctx) -> None:
    while True:
        try:
            await ctx.bot.send_chat_action(ctx.chat_id, ChatAction.UPLOAD_VIDEO)
        except TelegramError:
            pass
        await asyncio.sleep(4)


async def t_make_profile_video(ctx: tools.ToolCtx, a: dict) -> str:
    spec = avatar.Spec(*(str(a.get(k) or d) for k, d in (("motion", "breathe"), ("speed", "slow"),
                                                          ("color", "none"), ("particles", "none"))))
    art = str(a.get("art") or "none")
    err = spec.check() or (None if art == "none" or art in avatar.AI_STYLES else f"art 는 none 또는 {list(avatar.AI_STYLES)} 중 하나")
    if err:
        return err
    if not avatar.available():
        return "지금 서버에 영상 도구(ffmpeg)가 없어 움프를 못 만듦. 운영자에게 알리겠다고 짧게 안내."
    uid = ctx.caller.id
    src = ctx.image.data if ctx.image is not None else await _profile_photo(ctx.bot, uid)
    if not src:
        return "프사를 못 가져옴 (프사가 없거나 공개 설정이 막혀 있음). 본인 사진을 같이 보내면서 다시 부탁하라고 안내."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    db = ctx.svc.db
    if await db.counter(day, 0, f"ava:{uid}") >= FREE_DAILY:
        return f"움프는 한 사람 하루 {FREE_DAILY}개까지. 내일 다시 가능하다고 안내."
    if art != "none" and await db.counter(day, ctx.chat_id, "image") >= ctx.settings["image_daily"]:
        return "오늘 이 방 이미지 한도를 다 써서 그림체 바꾸기는 안 됨. 그림체 없이(art=none) 움직임만은 가능하다고 안내."
    busy = asyncio.create_task(_busy(ctx))
    try:
        if art != "none":
            try:
                src = await ctx.svc.llm.image(avatar.AI_STYLES[art][1], Attached(src, "image/jpeg", uid), ctx.chat_id)
            except BudgetExceeded:
                return "오늘 AI 사용량 한도를 다 써서 그림체는 못 바꿈. 그림체 없이(art=none) 다시 할 수 있다고 안내."
            except BadRequestError:
                return "이 사진은 안전 정책에 걸려 그림체를 못 바꿈. 그림체 없이 움직임만 해 보자고 안내."
            except OpenAIError as e:
                log.warning("avatar art failed: %s", e)
                return "그림 서버가 잠깐 불안정함. 잠시 후 다시 부탁하라고 안내."
            await db.bump(day, ctx.chat_id, "image")
        try:
            mp4 = await avatar.make(src, spec)
        except Exception as e:   # 이상한 사진·시간 초과
            log.warning("avatar make failed: %s", e)
            return "이 사진으로는 영상을 못 만듦 (형식 문제). 다른 사진으로 다시 부탁하라고 안내."
    finally:
        busy.cancel()
    label = spec.label() + (f" · {avatar.AI_STYLES[art][0]}" if art != "none" else "")
    name = display_name(ctx.caller.first_name, ctx.caller.last_name, ctx.caller.username)
    try:
        await ctx.bot.send_document(ctx.chat_id, InputFile(mp4, filename="sodam_ump.mp4"), parse_mode="HTML",
                                    caption=f"🎞️ {esc(name)}님 움프 ({label})\n{GUIDE}")
    except TelegramError as e:
        return f"영상은 만들었는데 전송 실패: {e.message}"
    await db.bump(day, 0, f"ava:{uid}")
    return "움프 파일을 방에 보냈음. 설정 방법은 파일에 적혀 있으니 한마디만 짧게."


def _enum(table) -> dict:
    return {"type": "string", "enum": list(table),
            "description": " · ".join(f"{k}={v[0]}" for k, v in table.items() if v[0])}


tools.register_tool(tools.Tool(
    "make_profile_video",
    "텔레그램 '움직이는 프로필(움프)' 영상 파일을 만들어 보낸다. 원본 = 요청에 붙었거나 답장한 사진, 없으면 요청자 프사. "
    "'내 프사 움프로 만들어줘', '이 사진 하트 날리게 해줘', '눈 내리고 흑백으로 천천히'. 사용자가 말한 효과를 아래 부품 중 "
    "가장 가까운 것으로 골라 조합 (없는 효과는 가까운 걸로 만들고 무엇으로 대신했는지 한마디, 꼭 원하면 feature_request). "
    "art(그림체)는 이미지 한도를 씀.",
    {"motion": _enum(avatar.MOTIONS), "speed": _enum(avatar.SPEEDS), "color": _enum(avatar.COLORS),
     "particles": _enum(avatar.PARTICLES), "art": {"type": "string", "enum": ["none", *avatar.AI_STYLES]}},
    [], t_make_profile_video))
