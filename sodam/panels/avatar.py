"""🎞️ 움프 AI 도구 make_profile_video (스티커 엔진 spec 으로 만들기 = 기본, 옛 부품 조합 avatar.Spec = fallback, 그림체는 llm.image).

원본 = 요청·답장에 붙은 사진 (남의 사진도 됨 — 사용자 결정 2026-09-28, 하루 한도로 충분), 없으면 요청자의 지금 프사.
spec 을 주면 스티커 엔진(stickerforge.forge_video: photo 모드·framing·펀치·광채·글리치·스파클…)으로 640×640 H.264 6초를 만들고
qc 경고를 돌려줌. spec 이 없으면 옛 부품 조합(motion·speed·color·particles → avatar.make).
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
from ..llm import BudgetExceeded
from ..util import display_name, esc
from ..vision import Attached

log = logging.getLogger(__name__)
FREE_DAILY = 5
GUIDE = "텔레그램 → 설정 → 프로필 사진 설정 → 이 파일 (먼저 기기에 저장)"
DESIGN = (
    "spec(권장) = make_sticker 와 같은 형식이되 mode=photo·radius 0 기본: sticker_catalog(요청, 'photo')로 후보를 받아 "
    "{recipe, seed} 또는 motion(zoom·punch·shake·pan)+fx(sweep·sparkle·glitch·rays·glow·meteors·slice_glitch) 1~3개. "
    "'글리치'는 잠깐 R/B 가 어긋나는 glitch 이지 색이 도는 게 아니고, '네온'은 glow(color)·sweep 이지 채도 올리기가 아님. "
    "강렬=punch(hits 2, amount 0.08)+glitch+sparkle, 잔잔=zoom+meteors, 화사=zoom+rays+sweep. framing auto 가 얼굴을 살림. "
    "경고(warnings)가 오면 하나만 고쳐 다시(두 번째면 accept_warnings). 원본이 이미 그림(캐릭터·일러스트)이면 art 를 쓰지 말 것. "
    "sticker_catalog(for_video=true)의 학습 레시피는 recipe 이름으로 바로. 없는 효과는 가까운 조합 + wanted(기능 요청), 새 코드·필터는 절대 안 만듦."
)


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
    forge_spec = None
    if a.get("spec"):
        from .sticker import resolve_spec
        raw = dict(a["spec"]) if isinstance(a["spec"], dict) else {}
        raw.setdefault("mode", "photo")
        raw.setdefault("radius", 0)
        forge_spec, err = await resolve_spec(ctx, raw, "ump")
        if err:
            return f"spec 오류: {err}. 고쳐서 다시 부를 것."
    spec = avatar.Spec(*(str(a.get(k) or d) for k, d in (("motion", "breathe"), ("speed", "slow"),
                                                          ("color", "none"), ("particles", "none"))))
    art = str(a.get("art") or "none")
    err = (None if forge_spec else spec.check()) or (None if art == "none" or art in avatar.AI_STYLES else f"art 는 none 또는 {list(avatar.AI_STYLES)} 중 하나")
    if err:
        return err
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
            if forge_spec:
                res = await SF.forge_video(src, forge_spec)
                if not res.ok:
                    raise RuntimeError(res.summary())
                mp4, warnings = res.mp4, res.warnings
                if warnings and not a.get("accept_warnings"):
                    return ("만들었지만 검수 경고 (아직 안 보냄): " + " / ".join(warnings)
                            + f". 지표 {res.metrics}. 하나만 고쳐 다시 부를 것 — 그래도 경고면 accept_warnings=true.")
                label = "스티커 엔진: " + " + ".join([m["type"] for m in forge_spec["motion"]] + [f["type"] for f in forge_spec["fx"]])
            else:
                mp4 = await avatar.make(src, spec)
                label = spec.label()
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
    spec_used = forge_spec or {"motion": [{"type": spec.motion}], "fx": [{"type": k} for k in (spec.color, spec.particles) if k != "none"]}
    await L.log(db, chat_id=ctx.chat_id, user_id=uid, request=str(a.get("request") or ""), kind="photo" if forge_spec else "legacy",
                spec=spec_used, outcome="ok", msg_id=getattr(sent, "message_id", 0), product="ump")
    note = f" (경고 안고 보냄: {'; '.join(warnings)})" if warnings else ""
    from .sticker import note_wanted
    note += await note_wanted(ctx, a.get("wanted") or "", label)
    return f"{skipped_art}움프 파일을 방에 보냈음{note}. 설정 방법은 파일에 적혀 있으니 한마디만 짧게."


def _enum(table) -> dict:
    return {"type": "string", "enum": list(table),
            "description": " · ".join(f"{k}={v[0]}" for k, v in table.items() if v[0])}


tools.register_tool(tools.Tool(
    "make_profile_video",
    "텔레그램 '움직이는 프로필(움프)' 영상 파일(640×640·6초)을 만들어 보낸다. 원본 = photo_of(이 방 멤버 누구든 프사) > "
    "붙었거나 답장한 사진(누가 올렸든) > 요청자 프사. "
    "'내 프사 움프로 만들어줘', '글리치+네온으로 강렬하게', '눈 내리고 흑백으로 천천히'. " + DESIGN +
    " spec 없이 부르면 옛 부품(motion·speed·color·particles: 색 필터·날리는 것)으로 만듦 — 하트·눈·꽃잎 날리기는 이쪽.",
    {"spec": {"type": "object", "description": "스티커 엔진 spec (make_sticker 와 같음; mode photo·radius 0 기본)"},
     "photo_of": PHOTO_OF,
     "motion": _enum(avatar.MOTIONS), "speed": _enum(avatar.SPEEDS), "color": _enum(avatar.COLORS),
     "particles": _enum(avatar.PARTICLES), "art": {"type": "string", "enum": ["none", *avatar.AI_STYLES]},
     "accept_warnings": {"type": "boolean", "description": "검수 경고를 한 번 고친 뒤에도 남으면 true 로 그대로 보냄"},
     "request": {"type": "string", "description": "사용자 요청 원문 (200자, 학습 기록용)"},
     "wanted": {"type": "string", "description": "목록에 없는 효과를 원했을 때 그 말 그대로 — 가까운 조합으로 만들고 기능 요청 접수"}},
    [], t_make_profile_video))
