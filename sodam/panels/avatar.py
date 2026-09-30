"""🎞️ 움프 AI 도구 make_profile_video — 엔진은 스티커와 하나(sodam/stickerforge), 연출은 부품 조합 spec.

원본 = 요청·답장에 붙은 사진 (남의 사진도 됨 — 사용자 결정 2026-09-28, 하루 한도로 충분), 없으면 요청자의 지금 프사.
spec = 부품 조합(keyframes·particles·grade·flash·lightning·transition·효과 이름) → stickerforge.forge_video → 640×640 H.264
(loop 기본 3초×2, loop=false 면 6초 한 번). 옛 인자(motion·speed·color·particles)는 도구 설명에서 뺐지만 오면
avatar.to_forge_spec 로 같은 엔진의 부품 값으로 옮김 (호환). 2026-09-30 오너 지시: 옛 고정 목록만 골라 17건 중 8건이 같은 조합이었음.
art(그림체) 는 gpt-image 고치기 — **원본이 이미 그림이면 코드가 건너뜀**(looks_illustrated, 돈·시간 낭비).
한도: 사람마다 하루 FREE_DAILY 개, 그림체는 방 이미지 한도(image_daily)도 같이 씀 (한 장 ≈ $0.03).
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

from .. import avatar, stickerforge as SF, stickerlearn as L, tools
from ..stickerforge import examples as EX
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


PHOTO_OF = {"type": "string", "description": "이 방 다른 멤버의 프사로 만들 때 그 사람 이름·@아이디·ID "
                                            "('민수 프사로', '이 사람 프사로'+답장·태그). 붙은·답장한 사진을 쓸 땐 비움."}


async def source_photo(ctx, a: dict) -> tuple[bytes | None, str | None]:
    """원본 사진: photo_of(이 방 누구든 프사) > 붙은·답장한 사진(누구 것이든) > 요청자 프사.
    누구 사진이든 됨 (오너 결정 2026-09-28·09-29 — 기능마다 달랐던 제한을 하나로). 한도는 사람·방 하루 한도."""
    who = str(a.get("photo_of") or "").strip()
    if who:
        row, err = await tools._resolve(ctx, who)
        if err:
            return None, err
        data = await _profile_photo(ctx.bot, row["user_id"])
        if not data:
            return None, (f"{tools._row_name(row)} 님 프사를 못 가져옴 (프사가 없거나 공개 설정이 막힘). "
                          "그 사람 사진에 답장하면서 다시 부탁하라고 안내.")
        return data, None
    if ctx.image is not None:
        return ctx.image.data, None
    data = await _profile_photo(ctx.bot, ctx.caller.id)
    if not data:
        return None, "원본 사진이 없음: 붙은·답장한 사진이 없고 프사를 못 가져옴 (프사가 없거나 공개 설정이 막힘). 사진과 함께 또는 사진에 답장하며 다시 부탁하라고 안내."
    return data, None


async def _busy(ctx) -> None:
    while True:
        try:
            await ctx.bot.send_chat_action(ctx.chat_id, ChatAction.UPLOAD_VIDEO)
        except TelegramError:
            pass
        await asyncio.sleep(4)


async def t_make_profile_video(ctx: tools.ToolCtx, a: dict) -> str:
    from .sticker import lighter, note_wanted, resolve_spec, same_as_last  # noqa: F401 (순환 import 피해 함수 안에서)
    from .. import mediaintent
    if back := mediaintent.redirect(getattr(ctx, "media_intent", None), "make_profile_video"):
        return back
    legacy = None
    if a.get("spec"):
        raw = dict(a["spec"]) if isinstance(a["spec"], dict) else {}
    else:   # 옛 인자(호환) → 같은 엔진의 부품 값
        legacy = avatar.Spec(*(str(a.get(k) or d) for k, d in (("motion", "breathe"), ("speed", "slow"),
                                                              ("color", "none"), ("particles", "none"))))
        err = legacy.check()
        if err:
            return err
        raw = avatar.to_forge_spec(legacy)
        raw.pop("seed", None)
    if isinstance(a.get("seed"), int) and not isinstance(a.get("seed"), bool):
        raw["seed"] = a["seed"]
    raw.setdefault("mode", "photo")
    raw.setdefault("radius", 0)
    forge_spec, err = await resolve_spec(ctx, raw, "ump")
    if err:
        return f"spec 오류: {err}. 고쳐서 다시 부를 것."
    art = str(a.get("art") or "none")
    if art != "none" and art not in avatar.AI_STYLES:
        return f"art 는 none 또는 {list(avatar.AI_STYLES)} 중 하나"
    if not a.get("accept_warnings"):
        same = await same_as_last(ctx, forge_spec, str(a.get("request") or ""), "ump")
        if same:
            return same
    if not avatar.available():
        return "지금 서버에 영상 도구(ffmpeg)가 없어 움프를 못 만듦. 운영자에게 알리겠다고 짧게 안내."
    uid = ctx.caller.id
    src, err = await source_photo(ctx, a)
    if not src:
        return err
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    db = ctx.svc.db
    if await db.counter(day, 0, f"ava:{uid}") >= FREE_DAILY:
        return f"움프는 한 사람 하루 {FREE_DAILY}개까지. 내일 다시 가능하다고 안내."
    skipped_art = ""
    if art != "none" and await asyncio.to_thread(SF.looks_illustrated, src):   # 이미 그림 → 그림체 변환은 낭비
        skipped_art, art = f"(원본이 이미 그림이라 그림체 {art} 는 건너뜀) ", "none"
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
        warnings: list = []
        try:
            res = await SF.forge_video(src, forge_spec)
            if not res.ok and not res.error and lighter(forge_spec):     # 2MB 못 맞춤 → 레이어 하나 덜고 한 번 더
                forge_spec = lighter(forge_spec)
                res = await SF.forge_video(src, forge_spec)
            if not res.ok:
                raise RuntimeError(res.summary())
            mp4, warnings = res.mp4, res.warnings
            if warnings and not a.get("accept_warnings"):
                return ("만들었지만 검수 경고 (아직 안 보냄): " + " / ".join(warnings)
                        + f". 지표 {res.metrics}. 하나만 고쳐 다시 부를 것 — 그래도 경고면 accept_warnings=true.")
            label = legacy.label() if legacy else "부품: " + L.describe(forge_spec)
        except Exception as e:   # 이상한 사진·시간 초과
            log.warning("avatar make failed: %s", e)
            return "이 사진으로는 영상을 못 만듦 (형식 문제). 다른 사진으로 다시 부탁하라고 안내."
    finally:
        busy.cancel()
    label += f" · {avatar.AI_STYLES[art][0]}" if art != "none" else ""
    name = display_name(ctx.caller.first_name, ctx.caller.last_name, ctx.caller.username)
    try:
        sent = await ctx.bot.send_document(ctx.chat_id, InputFile(mp4, filename="sodam_ump.mp4"), parse_mode="HTML",
                                           caption=f"🎞️ {esc(name)}님 움프 ({esc(label)})\n{GUIDE}")
    except TelegramError as e:
        return f"영상은 만들었는데 전송 실패: {e.message}"
    await db.bump(day, 0, f"ava:{uid}")
    await L.log(db, chat_id=ctx.chat_id, user_id=uid, request=str(a.get("request") or ""), kind="legacy" if legacy else "photo",
                spec=forge_spec, outcome="ok", msg_id=getattr(sent, "message_id", 0), product="ump")
    note = f" (경고 안고 보냄: {'; '.join(warnings)})" if warnings else ""
    note += await note_wanted(ctx, a.get("wanted") or "", label)
    return f"{skipped_art}움프 파일을 방에 보냈음{note}. 설정 방법은 파일에 적혀 있으니 한마디만 짧게."


tools.register_tool(tools.Tool(
    "make_profile_video",
    "텔레그램 '움직이는 프로필(움프)' 영상(640×640 약 6초 mp4)을 만들어 보낸다. 원본 = photo_of(이 방 멤버 프사) > 붙은·답장한 사진 > "
    "요청자 프사. mode 는 photo 기본(사진 꽉 채움). 타서 없어지기처럼 한 번 흐르는 연출은 spec.loop=false. art = 그림체 바꾸기(원본이 이미 그림이면 생략됨). "
    + EX.COMPOSE,
    {"spec": {"type": "object", "description": "{motion:[…], layers:[{type,…,start,end}], loop, cover, caption, framing, seed} — sticker_catalog 참고"},
     "photo_of": PHOTO_OF,
     "art": {"type": "string", "enum": ["none", *avatar.AI_STYLES]},
     "seed": {"type": "integer", "description": "변주 번호 (안 주면 매번 새로)"},
     "accept_warnings": {"type": "boolean", "description": "검수 경고를 한 번 고친 뒤에도 남으면 true"},
     "request": {"type": "string", "description": "사용자 요청 원문"},
     "wanted": {"type": "string", "description": "정말 못 하는 연출(사진 속 사람이 실제로 움직이기 등)을 원했을 때 그 말 그대로"}},
    [], t_make_profile_video))
