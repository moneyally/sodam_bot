"""🎬 AI 도구 make_video — 짧은 AI 영상(소리 포함)을 만들어 방에 올림 (제공자·API 는 sodam/video.py).

오너 결정 2026-09-30: 키는 오너가 직접(.env GEMINI_API_KEY 또는 XAI_API_KEY), 방마다 **한 주 6개**(video_weekly,
한국시간 월요일 0시 초기화 — counters 의 그 주 월요일 날짜 칸 'video_week'), 관리자는 줄이기만(settings.OWNER_CAP), 오너는 한도 무시.
규칙 (make_image 와 같음 + 영상은 비싸서 더):
- 키 없으면 도구가 아예 안 보임(Tool.enabled). 이용 중인 방(paid_features)·그룹방만. 한 답변 1개, 방마다 동시에 1개.
- 요금 = 초당 요금 × 초 (costs.VIDEO_PER_SEC) 를 **시작 전에** 방·전체 하루 달러 한도로 검사(llm.can_spend), 만들어진 뒤에만 청구(llm.charge) —
  Veo 는 막힌 영상은 청구 안 함(공식 요금 문서). 실패·시간 초과는 한 주 개수도 안 셈.
- 1~3분 걸려서 도구는 바로 끝나고(방에 '🎬 만드는 중…' 답장 + 영상 올리는 중 표시) 뒤에서 만든 뒤 요청 메시지에 답장으로 영상.
- 원본 사진(mode=image): photo_of(이 방 멤버 프사) > 붙은·답장한 사진 > 요청자 프사 — panels/avatar.source_photo 와 같은 규칙
  (그 파일은 다른 작업이 고치는 중이라 여기 최소 복사).
- 안전 (오너 결정 2026-09-30): 성인 내용은 영상 AI 정책이 판단(거절 = '영상 AI 쪽 정책으로 거절됐어요'), 우리는 두 가지만 막음 —
  미성년자+성적 내용 · 실제 사람 사진으로 성적·노출 영상 (hard_line).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from telegram import ReplyParameters
from telegram.constants import ChatAction
from telegram.error import TelegramError

from .. import agentlog, costs, mediaintent, memory, persist, tools, video
from ..llm import BudgetExceeded
from ..permissions import Role
from ..settings import OWNER_CAP, RANGES
from ..util import display_name, esc, mention, user_name

log = logging.getLogger(__name__)
KEY = "video_week"          # counters: (그 주 월요일 날짜, 방) 칸에 이번 주 만든 개수
RUNNING: dict[int, asyncio.Future] = {}   # 방 → 자리 잡은 요청·만드는 중인 작업 (방마다 동시에 1개)
# 만드는 중이라 거절당한 사람 → 끝나면 태그해서 '이제 돼요' (실제 2026-10-03 베베: 1분 뒤 같은 요청을 또 함)
WAITING: dict[int, dict[int, str]] = {}


def _free(cid: int, t, bot) -> None:
    """만들던 작업이 끝남 → 자리 풂 + 기다린 사람 태그."""
    if RUNNING.get(cid) is t:
        RUNNING.pop(cid, None)
    who = WAITING.pop(cid, None)
    if who and bot is not None:
        text = ", ".join(mention(uid, name) for uid, name in list(who.items())[:5]) + " 🎬 이제 영상 다시 부탁하셔도 돼요!"
        persist.spawn(_tell(bot, cid, text))


async def _tell(bot, cid: int, text: str) -> None:
    try:
        await bot.send_message(cid, text, parse_mode="HTML")
    except TelegramError as e:
        log.info("video wait notice failed %s: %s", cid, e)
NO_KEY = "영상 AI 키가 아직 설정 안 됨 (운영자). 운영자가 영상 AI 를 켜야 한다고 짧게 안내하고, 대신 사진으로 움프·스티커는 된다고 한마디."
# 우리 코드가 막는 건 딱 두 가지 (오너 결정 2026-09-30 — 성인 내용 자체는 영상 AI(xAI·Veo) 정책이 판단, 거절되면 한국어로 알림):
# ① 미성년자 + 성적 내용 ② 실제 사람 사진(멤버 프사·붙은/답장한 사진)으로 만드는 성적·노출 영상 (동의 없는 딥페이크 위험).
# 낱말 검사는 최소한으로 (영어 prompt 는 낱말 경계, 한국어는 뜻이 분명한 말만 — '귤 껍질 벗기기'·'노출 과다 야경' 은 안 걸림).
POLICY_TEXT = ("성인 내용도 부탁 그대로 prompt 에 담는다 (되고 안 되고는 영상 AI 가 판단). 단 두 가지는 부르지 않는다: "
               "미성년자가 나오는 성적인 내용, 실제 사람 사진(프사·붙은 사진)으로 만드는 성적·노출 영상")
_SEXUAL = re.compile(
    r"\b(nude|nudes|nudity|naked|nsfw|porn\w*|sex|sexual\w*|sexy|erotic\w*|undress\w*|topless|bottomless|lingerie|"
    r"strip(?:tease|ping)|seductive\w*|lewd|explicit|hentai|boobs?|breasts?|genitals?|intercourse|orgasm\w*)\b"
    r"|알몸|나체|누드|옷\s*(?:을\s*)?벗|벗겨|섹시|야하|야한|19금|야동|섹스|성행위|성관계|음란|속옷|란제리|가슴\s*노출|젖꼭지", re.I)
_MINOR = re.compile(
    r"\b(child|children|kids?|minors?|underage|teen|teens|teenage\w*|preteen\w*|schoolgirls?|schoolboys?|loli\w*|shota\w*|"
    r"toddlers?|infants?|juvenile)\b"
    r"|미성년|초등학생|초딩|중학생|중딩|고등학생|고딩|여고생|남고생|여중생|남중생|어린이|아동|유아|꼬마|로리|쇼타|소녀|소년", re.I)
REFUSE_MINOR = "미성년자가 나오는 성적인 영상은 못 만듦. 다른 내용이면 된다고 한마디만."
REFUSE_REAL = ("실제 사람 사진(프사·붙은 사진)으로는 성적·노출 영상을 못 만듦 (그 사람 동의를 확인할 수 없음). "
               "사진 없이 글로만(mode=text, 가상 인물) 만드는 건 된다고 한마디만.")


def hard_line(text: str, real_photo: bool) -> str | None:
    """우리가 막는 두 가지만. 나머지는 영상 AI 정책에 맡김."""
    if not _SEXUAL.search(text):
        return None
    if _MINOR.search(text):
        return REFUSE_MINOR
    if real_photo:
        return REFUSE_REAL
    return None


FAIL_TEXT = {
    "policy": "🙅 영상 AI 쪽 정책으로 거절됐어요. 표현을 바꿔서 다시 부탁해 주세요.",
    "timeout": "⏰ 영상이 너무 오래 걸려서 멈췄어요. 잠시 후 다시 부탁해 주세요.",
    "auth": "⚠️ 영상 AI 키에 문제가 있어서 못 만들었어요 (운영자 확인 필요).",
    "quota": "⚠️ 영상 AI 사용 한도(결제·속도)에 걸려서 못 만들었어요. 나중에 다시 부탁해 주세요.",
    "failed": "😵 영상 만들기가 실패했어요. 잠시 후 다시 부탁해 주세요.",
}


def week_start(tz, now: datetime | None = None) -> str:
    """이번 주 월요일 날짜 (한국시간 월요일 0시에 바뀜) = counters 칸."""
    now = now or datetime.now(tz)
    return (now.date() - timedelta(days=now.weekday())).strftime("%Y-%m-%d")


async def used_this_week(svc, chat_id: int) -> int:
    return await svc.db.counter(week_start(svc.cfg.tz), chat_id, KEY)


def weekly_limit(settings: dict) -> int:
    return max(0, min(int(settings.get("video_weekly", 0) or 0), RANGES["video_weekly"][1]))


async def _profile_photo(bot, uid: int) -> bytes | None:
    try:
        got = await bot.get_user_profile_photos(uid, limit=1)
        if not got or not got.photos:
            return None
        f = await bot.get_file(got.photos[0][-1].file_id)
        return bytes(await f.download_as_bytearray())
    except TelegramError as e:
        log.info("video: profile photo fetch failed %s: %s", uid, e)
        return None


async def _source(ctx: tools.ToolCtx, a: dict) -> tuple[tuple[bytes, str] | None, str | None]:
    who = str(a.get("photo_of") or "").strip()
    if who:
        row, err = await tools._resolve(ctx, who)
        if err:
            return None, err
        data = await _profile_photo(ctx.bot, row["user_id"])
        if not data:
            return None, (f"{tools._row_name(row)} 님 프사를 못 가져옴 (프사가 없거나 공개 설정이 막힘). "
                          "그 사람 사진에 답장하면서 다시 부탁하라고 안내.")
        return (data, "image/jpeg"), None
    if ctx.image is not None:
        return (ctx.image.data, ctx.image.mime if ctx.image.mime.startswith("image/") else "image/jpeg"), None
    data = await _profile_photo(ctx.bot, ctx.caller.id)
    if not data:
        return None, ("움직일 사진이 없음: 붙은·답장한 사진이 없고 프사도 못 가져옴. 사진과 함께 또는 사진에 답장하며 다시 부탁하거나, "
                      "사진 없이 글로만 만들려면 mode=text 로 다시.")
    return (data, "image/jpeg"), None


async def _busy(bot, chat_id: int) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        except (TelegramError, AttributeError):
            pass
        await asyncio.sleep(4)


EDIT_MAX_SEC = 10   # 고치기는 결과 = 원본 길이만큼 청구 → 긴 영상으로 요금이 커지지 않게 (오너는 15초)


def _source_video(ctx: tools.ToolCtx):
    """고칠·이어 붙일 영상: 요청 글에 붙은 것 > 답장한 글 > 3분 안 같은 사람이 방금 올린 것 (vision.recent_media)."""
    from .. import vision
    msg = ctx.request_msg
    for m in (msg, getattr(msg, "reply_to_message", None)):
        md = vision._media_of(m) if m is not None else None
        if md and md.kind in ("video", "gif", "video_note"):
            return md
    recent = vision.recent_media(msg) if msg is not None else None
    md = vision._media_of(recent) if recent is not None else None
    return md if md and md.kind in ("video", "gif", "video_note") else None


async def t_make_video(ctx: tools.ToolCtx, a: dict) -> str:
    prov = video.active()
    if prov is None:
        return NO_KEY
    mode = str(a.get("mode") or "text")
    if mode not in ("edit", "extend") and (back := mediaintent.redirect(getattr(ctx, "media_intent", None), "make_video")):
        return back
    if ctx.chat_id > 0:
        return "영상은 그룹방에서만 만들 수 있음. 그룹방에서 '소담아 …영상 만들어줘' 하라고 안내."
    if not await ctx.svc.paid_features(ctx.chat_id):
        return "이 방은 이용 기간이 아니라 영상을 못 만듦. 관리자가 구독을 연장하면 된다고 짧게 안내."
    prompt = str(a.get("prompt", "")).strip()[:1500]
    if not prompt:
        return "만들 영상 설명(prompt)이 비어 있음."
    # 고치기·이어 붙이기의 원본 영상은 실제 사람일 수도 있어서 사진과 같은 선으로 봄
    uses_photo = mode in ("image", "edit", "extend") or bool(a.get("photo_of"))
    if why := hard_line(prompt + "\n" + str(getattr(ctx.request_msg, "text", "") or ""), uses_photo):
        return why
    if getattr(ctx, "_video_asked", False):
        return "영상은 한 답변에 하나만. 방금 부탁한 영상이 끝나면 다시 부탁하라고 안내."
    cid = ctx.chat_id
    if cid in RUNNING and not RUNNING[cid].done():
        WAITING.setdefault(cid, {})[ctx.caller.id] = user_name(ctx.caller) or "대표님"
        return ("이 방에서 영상을 하나 만드는 중이라 지금은 못 만듦. 끝나면 소담이 이 사람을 태그해서 알려 준다고 짧게 안내 (1~3분). "
                "다 됐다고 하거나 지금 만든다고 하지 말 것.")
    # 검사와 자리 잡기 사이에 await 없음 → 같은 방 동시 요청 두 개가 둘 다 통과(주 한도 7/6·요금 두 번) 못 함
    hold = asyncio.get_running_loop().create_future()
    RUNNING[cid] = hold
    try:
        if mode in ("edit", "extend"):
            return await _start_edit(ctx, a, prov, prompt, mode)
        return await _start(ctx, a, prov, prompt, uses_photo)
    finally:
        if RUNNING.get(cid) is hold:   # 뒤 작업으로 못 넘겼으면(거절·실패) 자리 풂
            RUNNING.pop(cid, None)
        hold.cancel()


async def _start_edit(ctx: tools.ToolCtx, a: dict, prov, prompt: str, mode: str) -> str:
    """🎬 영상 고치기(edit: '옷 빨간색으로') · 이어 붙이기(extend: '5초 더'). xAI 만, 한 주 개수·하루 요금 한도는 만들기와 같이 셈."""
    svc = ctx.svc
    if not prov.edits:
        return f"지금 영상 AI({prov.label})는 영상 고치기·이어 붙이기를 못 함. 새로 만드는 것만 된다고 안내."
    md = _source_video(ctx)
    if md is None:
        return "고칠 영상이 없음: 영상에 답장하면서 부탁하거나 영상과 함께 보내 달라고 안내 (방금 올린 영상이면 3분 안)."
    if md.size and md.size > video.MAX_IN_BYTES:
        return "원본 영상이 20MB 를 넘어서 못 받음. 더 짧거나 작은 영상으로 다시 부탁하라고 안내."
    limit, used = weekly_limit(ctx.settings), await used_this_week(svc, ctx.chat_id)
    if ctx.role < Role.OWNER and used >= limit:
        return f"이번 주 영상 {used}/{limit}개 (고치기·이어 붙이기도 1개로 셈) — 한도를 다 썼음. 월요일에 다시 된다고 안내."
    room_sec = int(ctx.settings.get("video_seconds") or 6)
    src_sec = int(-(-float(md.duration or 0) // 1)) or room_sec
    if mode == "edit":
        cap = 15 if ctx.role >= Role.OWNER else EDIT_MAX_SEC
        if src_sec > cap:
            return f"고치기는 {cap}초 이하 영상만 (원본 {src_sec}초 — 결과도 원본 길이만큼 요금이 듦). 짧게 잘라서 다시 부탁하라고 안내."
        seconds = src_sec
    else:
        try:
            want = int(a.get("seconds") or room_sec)
        except (TypeError, ValueError):
            want = room_sec
        seconds = max(1, min(want, room_sec if ctx.role < Role.OWNER else 15))
    micro = costs.video_usd_micro(video.XAI_VIDEO_IN, seconds)
    try:
        await svc.llm.can_spend(ctx.chat_id, micro)
    except BudgetExceeded:
        return "오늘 AI 사용량 한도가 모자라서 못 함 (영상은 비쌈). 내일 다시 가능하다고 안내할 것."
    try:
        f = await ctx.bot.get_file(md.file_id)
        data = bytes(await f.download_as_bytearray())
    except TelegramError as e:
        return f"원본 영상을 못 받음 ({e.message}). 다시 올려서 부탁하라고 안내."
    ctx._video_asked = True
    req_id = getattr(ctx.request_msg, "message_id", None)
    reply = ReplyParameters(req_id, allow_sending_without_reply=True) if req_id else None
    doing = "고치는" if mode == "edit" else f"{seconds}초 이어 붙이는"
    try:
        status = await ctx.bot.send_message(ctx.chat_id, f"🎬 영상 {doing} 중… (1~3분 걸려요)", reply_parameters=reply)
    except TelegramError as e:
        return f"영상 안내를 방에 못 보냄: {e.message}"
    make = (lambda: prov.edit(data, prompt)) if mode == "edit" else (lambda: prov.extend(data, prompt, seconds))
    job = _job(svc, ctx.bot, ctx.chat_id, ctx.caller, prov, prompt, None, seconds, None, micro, reply,
               getattr(status, "message_id", None), make=make, model=video.XAI_VIDEO_IN)
    task = persist.spawn(job)
    if task is None:
        return "지금은 영상을 못 만듦. 잠시 후 다시 부탁하라고 안내."
    RUNNING[ctx.chat_id] = task
    task.add_done_callback(lambda t, cid=ctx.chat_id, bot=ctx.bot: _free(cid, t, bot))
    ctx.quiet = True
    return (f"영상 {'고치기' if mode == 'edit' else '이어 붙이기'}를 시작했고 방에 안내를 올렸음 (1~3분 뒤 따로 올라감). "
            f"이번 주 영상 {used + 1}/{limit}개. 다 됐다고 말하지 말 것.")


async def _start(ctx: tools.ToolCtx, a: dict, prov, prompt: str, uses_photo: bool) -> str:

    svc = ctx.svc
    limit, used = weekly_limit(ctx.settings), await used_this_week(svc, ctx.chat_id)
    if ctx.role < Role.OWNER and used >= limit:
        return (f"이번 주 영상 {used}/{limit}개 (월요일 0시에 초기화) — 이번 주 한도를 다 썼음. 월요일에 다시 된다고 안내. "
                f"방 관리자는 한도를 {OWNER_CAP.get('video_weekly', limit)}개까지 정할 수 있고 더 늘리는 건 봇 오너만.")
    room_sec = int(ctx.settings.get("video_seconds") or 6)
    try:
        want = int(a.get("seconds") or room_sec)
    except (TypeError, ValueError):
        want = room_sec
    if ctx.role < Role.OWNER:
        want = min(want, room_sec)
    seconds = prov.clamp_seconds(max(1, min(want, 8)))
    micro = costs.video_usd_micro(prov.model, seconds)
    try:
        await svc.llm.can_spend(ctx.chat_id, micro)
    except BudgetExceeded:
        return "오늘 AI 사용량 한도가 모자라서 영상을 못 만듦 (영상은 비쌈). 내일 다시 가능하다고 안내할 것."
    image = None
    if uses_photo:
        image, err = await _source(ctx, a)
        if not image:
            return err
    aspect = prov.aspect(str(a.get("aspect") or "")) if a.get("aspect") else None
    ctx._video_asked = True
    req_id = getattr(ctx.request_msg, "message_id", None)
    reply = ReplyParameters(req_id, allow_sending_without_reply=True) if req_id else None
    try:
        status = await ctx.bot.send_message(ctx.chat_id, f"🎬 영상 만드는 중… ({seconds}초짜리, 1~3분 걸려요)",
                                            reply_parameters=reply)
    except TelegramError as e:
        return f"영상 만들기 안내를 방에 못 보냄: {e.message}"
    job = _job(svc, ctx.bot, ctx.chat_id, ctx.caller, prov, prompt, image, seconds, aspect, micro, reply,
               getattr(status, "message_id", None))
    task = persist.spawn(job)
    if task is None:
        return "지금은 영상을 못 만듦. 잠시 후 다시 부탁하라고 안내."
    RUNNING[ctx.chat_id] = task   # 자리를 뒤 작업이 이어받음 (끝나면 풂)
    task.add_done_callback(lambda t, cid=ctx.chat_id, bot=ctx.bot: _free(cid, t, bot))
    ctx.quiet = True   # '만드는 중' 답장을 이미 올림 → AI 답은 안 보냄 (다 됐다고 먼저 말하지 않게)
    return ("영상 만들기를 시작했고 방에 '만드는 중' 안내를 올렸음 (1~3분 뒤 영상이 따로 올라감). "
            f"이번 주 영상 {used + 1}/{limit}개. 다 됐다고 말하지 말 것.")


async def _say(bot, chat_id: int, status_id: int | None, text: str, reply) -> None:
    try:
        if status_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=status_id)
            return
    except TelegramError:
        pass
    try:
        await bot.send_message(chat_id, text, reply_parameters=reply)
    except TelegramError as e:
        log.warning("video: notice failed %s: %s", chat_id, e)


async def _job(svc, bot, chat_id, caller, prov, prompt, image, seconds, aspect, micro, reply, status_id,
               make=None, model: str | None = None) -> None:
    """make = 고치기·이어 붙이기 (없으면 새로 만들기). model = 요금·기록에 쓸 모델 (고치기는 영상 입력 모델)."""
    agentlog.current.set(None)   # 에이전트 실행은 이미 끝남 → 요금은 방·전체 하루 달러에만 (llm.charge)
    busy = asyncio.create_task(_busy(bot, chat_id))
    model = model or prov.model
    try:
        try:
            data = await (make() if make else prov.generate(prompt, image, seconds, aspect))
        except video.VideoError as e:
            log.warning("video %s failed (%s): %s", prov.name, e.kind, video.redact(e.detail))
            await _say(bot, chat_id, status_id, FAIL_TEXT.get(e.kind, FAIL_TEXT["failed"]), reply)
            return
        except Exception as e:   # 네트워크 끊김 등 — 키가 섞일 수 있어 redact
            log.warning("video %s error: %s", prov.name, video.redact(repr(e)))
            await _say(bot, chat_id, status_id, FAIL_TEXT["failed"], reply)
            return
        # 만들어졌으면 제공자가 청구함 → 전송 성공 여부와 상관없이 셈
        await svc.llm.charge(chat_id, micro, "video", model)
        tz = svc.cfg.tz
        await svc.db.bump(week_start(tz), chat_id, KEY)
        await svc.db.bump(datetime.now(tz).strftime("%Y-%m-%d"), 0, f"video_sec:{model}", seconds)
        name = esc(display_name(caller.first_name, caller.last_name, caller.username))
        try:
            sent = await bot.send_video(chat_id, video=data, filename="sodam.mp4", supports_streaming=True,
                                        caption=f"🎬 {name}님 요청", parse_mode="HTML", reply_parameters=reply)
        except TelegramError as e:
            await _say(bot, chat_id, status_id, f"영상은 만들었는데 보내기 실패: {esc(e.message)}", reply)
            return
        if status_id:
            try:
                await bot.delete_message(chat_id, status_id)
            except TelegramError:
                pass
        await memory.record_turn(svc.db, chat_id, caller.id, "video", prompt[:500], "(영상을 만들어 보냄)",
                                 getattr(sent, "message_id", None))
    finally:
        busy.cancel()


tools.register_tool(tools.Tool(
    "make_video",
    "짧은 AI 영상(소리 포함)을 새로 만들어 방에 올린다 (1~3분 걸림, 방마다 한 주 개수 한도). "
    "prompt 는 **영어로**: 사용자의 한국어 부탁을 영상 프롬프트로 옮기고 구체적으로 한 문단 (피사체·동작·카메라 움직임·장소·조명·분위기·"
    "들릴 소리나 대사). mode=text 는 글로만, mode=image 는 붙은·답장한 사진(누가 올렸든)이나 photo_of 멤버 프사(없으면 요청자 프사)를 "
    "첫 장면으로 움직인다. mode=edit 는 답장한·붙은·방금 올린 **영상**을 말대로 고친다(옷 색·소품 추가·배경 등, prompt 는 바꿀 것만 영어로), "
    "mode=extend 는 그 영상 끝에서 이어서 seconds 초 더 만든다(prompt 는 이어질 내용). "
    f"{POLICY_TEXT}. 한 답변에 한 번만. 결과는 따로 올라가니 '다 됐다'고 말하지 말 것. "
    "앞에서 한도·실패였어도 다시 부탁하면 이 도구를 다시 부른다 (관리자가 한도를 바꿨을 수 있음).",
    {"prompt": {"type": "string", "description": "영어 영상 프롬프트 (구체적으로, 1,000자 안)"},
     "mode": {"type": "string", "enum": ["text", "image", "edit", "extend"]},
     "photo_of": {"type": "string", "description": "이 방 멤버 프사를 첫 장면으로 (이름·@아이디·ID). 있으면 image"},
     "seconds": {"type": "integer", "description": "길이(초). 비우면 방 설정값, 그보다 길게는 안 됨"},
     "aspect": {"type": "string", "enum": ["9:16", "16:9", "1:1"], "description": "세로 9:16(휴대폰)·가로 16:9·정사각 1:1(지원할 때만)"}},
    ["prompt", "mode"], t_make_video, setting="video_weekly", where="room", enabled=lambda: video.active() is not None))
