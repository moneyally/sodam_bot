"""🧩 스티커 AI 도구 make_sticker (sodam/stickerforge — 텔레그램 영상 스티커 공방).

소담(AI)이 사진을 보고 요청을 **디자이너처럼** spec 으로 옮김 (고정 틀 X — 사진 종류·분위기·요청마다 움직임 1 + 효과 1~3 + 글자 조합을
고름, 같은 사람이 또 부탁하면 다른 계열로). 코드는 sanitize 로 이름·숫자·범위만 통과시키고, 엔진 검사표가 전부 통과해야 보냄.
실패하면 효과 하나를 덜고 한 번 더. 원본 = 붙은/답장한 사진, 없으면 요청자 프사. 사람마다 하루 FREE_DAILY 개.
보내기 = 스티커(방에서 바로 움직이는 걸 봄) + 파일(@Stickers 로 팩 등록용 — 영상으로 보내면 재압축돼 거절됨).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import InputFile
from telegram.constants import ChatAction
from telegram.error import TelegramError

from .. import stickerforge as SF, tools
from ..util import display_name, esc
from .avatar import _profile_photo

log = logging.getLogger(__name__)
FREE_DAILY = 5
GUIDE = "팩 만들기: @Stickers → /newvideo → 팩 이름 → 이 파일(📎 파일로) → 이모지 → /publish"

DESIGN = (
    "사진을 먼저 보고 디자이너처럼 spec 을 정함 (고정 틀 말고 사진·요청에 맞게, 같은 사람이 또 부탁하면 다른 움직임 계열로). "
    "mode: 캐릭터·로고·단색 배경 그림=cutout(keying auto, 검은 배경에 빛나는 그림=glow) / 실사·스크린샷·꽉 찬 그림=photo(radius 56, 동그랗게 256). "
    "motion 1개(+은은한 1개): 잔잔·잘자=breathe/idle · 둥둥=float · 강조·전송=punch(hits 2~3) · 신남·덜덜=shake · 귀엽게 흔들=wobble · "
    "점프=hop · 짠 등장=pop · 쿵=slam · 총 반동=recoil · 찌르기·엄지척=jab · 사진=zoom/pan/punch. "
    "fx 1~3개(순서=그리는 순서, 배경→빛→겹침→왜곡): rays(빛살)·outline(흰 테두리)·glow(은은한 빛, color)·sweep(빛 지나감)·scan(전송 막대)·"
    "aura(glow 그림 번쩍)·flashbang·sparkle(별, subset 로 줄임)·hearts·meteors(별똥별)·zzz(잘자)·flash(at 총구)·shockwave·bolts(번개)·"
    "glitch(컬러 그림)·slice_glitch(흑백·고대비 그림)·ghost(잔상). "
    "caption: 2~7자가 가장 예쁨, palette gold·silver·ice·fire·pink·neon·white, anims bounce·punch·shine·wave·shake·glow. "
    "그림에 이미 글자가 있으면 caption 없이. 요란한 것보다 사진에 맞는 조합 (움직임 적을수록 화질 좋음). "
    "예: 인사=idle+glitch+sparkle · 전송완료=punch+scan+sparkle · 잘자요=breathe+meteors+zzz · 사랑해=punch+hearts+glow 분홍 · "
    "엄지척=punch+outline+sparkle · 출근(검은 빛 그림)=jab+aura+shockwave+bolts.")


async def _busy(ctx) -> None:
    while True:
        try:
            await ctx.bot.send_chat_action(ctx.chat_id, ChatAction.CHOOSE_STICKER)
        except TelegramError:
            pass
        await asyncio.sleep(4)


async def t_make_sticker(ctx: tools.ToolCtx, a: dict) -> str:
    spec, err = SF.sanitize(a.get("spec") or {})
    if err:
        return f"spec 오류: {err}. 고쳐서 다시 부를 것."
    from ..avatar import available
    if not available():
        return "지금 서버에 영상 도구(ffmpeg)가 없어 스티커를 못 만듦."
    uid, db = ctx.caller.id, ctx.svc.db
    src = ctx.image.data if ctx.image is not None else await _profile_photo(ctx.bot, uid)
    if not src:
        return "원본 사진이 없음 (붙은 사진·답장한 사진 없고 프사도 못 가져옴). 사진과 함께 다시 부탁하라고 안내."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    if await db.counter(day, 0, f"stk:{uid}") >= FREE_DAILY:
        return f"스티커는 한 사람 하루 {FREE_DAILY}개까지. 내일 다시 가능하다고 안내."
    busy = asyncio.create_task(_busy(ctx))
    try:
        res = await SF.forge(src, spec, icon=bool(a.get("icon")))
        if not res.ok and spec["fx"]:          # 크기·검사 실패 → 효과 하나 덜고 한 번 더 (SKILL: 움직임·효과를 줄여야 화질이 삼)
            spec = {**spec, "fx": spec["fx"][:-1]}
            res = await SF.forge(src, spec, icon=bool(a.get("icon")))
    finally:
        busy.cancel()
    if not res.ok:
        log.warning("sticker failed: %s", res.summary())
        return f"스티커가 텔레그램 규격 검사를 통과 못 함 ({res.summary()[:120]}). 효과를 줄이거나 다른 사진으로 다시 하자고 안내."
    name = display_name(ctx.caller.first_name, ctx.caller.last_name, ctx.caller.username)
    try:
        await ctx.bot.send_sticker(ctx.chat_id, InputFile(res.webm, filename="sticker.webm"))
        await ctx.bot.send_document(ctx.chat_id, InputFile(res.webm, filename="sodam_sticker.webm"), parse_mode="HTML",
                                    caption=f"🧩 {esc(name)}님 스티커 파일\n{GUIDE}")
        if res.icon:
            await ctx.bot.send_document(ctx.chat_id, InputFile(res.icon, filename="pack_icon.webm"),
                                        caption="팩 아이콘 (100×100) — /publish 뒤 아이콘 물을 때 이 파일")
    except TelegramError as e:
        return f"스티커는 만들었는데 전송 실패: {e.message}"
    await db.bump(day, 0, f"stk:{uid}")
    used = " + ".join([m["type"] for m in spec["motion"]] + [f["type"] for f in spec["fx"]])
    return f"스티커와 파일을 방에 보냈음 (배경: {res.keying}, 조합: {used}). 한마디만 짧게, 다른 느낌 원하면 말하라고."


tools.register_tool(tools.Tool(
    "make_sticker",
    "텔레그램 움직이는 스티커(영상 스티커)를 만들어 방에 보낸다. 원본 = 붙은·답장한 사진, 없으면 요청자 프사. "
    "'이걸로 스티커 만들어줘', '배경 빼고 글리치 넣어서 출근완료', '잘자요 느낌으로 잔잔하게'. " + DESIGN,
    {"spec": {"type": "object", "description": "mode·keying·motion[{type,...}]·fx[{type,...}]·caption{text,palette,anims,position}·"
                                               "margin·radius (숫자 인자는 선택, 기본값이 이미 좋음)"},
     "icon": {"type": "boolean", "description": "팩 아이콘(100×100)도 같이 — 팩 만든다고 할 때만"}},
    ["spec"], t_make_sticker))
