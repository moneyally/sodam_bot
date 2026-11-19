"""🖼 사진 찾아 올리기 (find_photo): '이채영 사진 줘'·'골든리트리버 사진 보여줘' → 웹에서 대표 사진을 찾아 방에 올린다.

챗지피티처럼 — OpenAI web_search 가 web-grounded 이미지 url + 출처를 주면(llm.image_search), 텔레그램이 그 url 에서
사진을 받아 올린다(sendPhoto by URL, 우리 서버 부담 0). 출처는 [📄 출처] URL 버튼으로 (본문 링크 필터·미리보기 끔 정책과 무관).
오너 결정 2026-11-19: 모든 방 기본 켬. 성적 질의는 거절(mediapolicy.sexual_query — 외부 사진이라 보수적), 사람·방 하루 한도.
설계 docs/SEARCH_IMAGE_DESIGN.md 1단계.
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .. import mediapolicy
from ..llm import BudgetExceeded
from ..permissions import Role
from ..security import strip_unsafe
from ..settings import register_setting
from ..tools import Tool, ToolCtx, register_tool

log = logging.getLogger(__name__)

register_setting("photo_daily", 10, "하루 사진 찾기(번)", range_=(0, 50))   # 0 = 끔 (관리자는 줄이기만, OWNER_CAP). 기본 켬(오너 결정)

MAX_PHOTOS = 3     # 한 요청에 올릴 수 있는 최대 장수 (도배 방지 — 기본 1장)


async def t_find_photo(ctx: ToolCtx, a: dict) -> str:
    uid, db, chat_id = ctx.caller.id, ctx.svc.db, ctx.chat_id
    query = str(a.get("query", "")).strip()
    if not query:
        return "어떤 사진인지 알려줘 (query). 예: '이채영 사진'·'골든리트리버 강아지'·'에펠탑'."
    try:
        count = max(1, min(int(a.get("count") or 1), MAX_PHOTOS))
    except (TypeError, ValueError):
        count = 1

    # 안전: 성적 질의는 거절 (외부 웹 사진을 방에 올리는 거라 생성보다 보수적 — OpenAI 도 원래 안 주지만 이중 안전)
    if mediapolicy.sexual_query(query):
        return "그런 사진은 못 찾아 줘요 (성적·선정적 내용). 규칙상 거절이라고 한마디만."

    cap = int(ctx.settings.get("photo_daily", 10) or 0)
    if cap <= 0 and ctx.role < Role.OWNER:
        return "이 방은 사진 찾기가 꺼져 있어요 (photo_daily 0). 방 관리자·오너만 켤 수 있다고 한마디."
    day = datetime.now(ctx.svc.cfg.tz).strftime("%Y-%m-%d")
    if ctx.role < Role.OWNER and await db.counter(day, 0, f"photo:{uid}") >= cap:
        return f"사진 찾기는 한 사람 하루 {cap}번까지예요. 내일 다시 된다고 안내만."

    try:
        imgs = await ctx.svc.llm.image_search(query, chat_id=chat_id)
    except BudgetExceeded:
        raise
    except Exception as e:                       # noqa: BLE001 — 검색 실패는 사용자에게 한 줄로
        log.warning("image_search 실패 (chat=%s): %s", chat_id, e)
        imgs = []
    if not imgs:
        return f"'{query}' 사진을 못 찾았어요. 다른 낱말(영어 이름·더 구체적으로)로 다시 해보라고 한마디."

    reply_to = getattr(ctx.request_msg, "message_id", None)
    sent = 0
    for im in imgs:
        if sent >= count:
            break
        src = im.get("source_url") or ""
        kb = (InlineKeyboardMarkup([[InlineKeyboardButton("📄 출처", url=src)]])
              if src.lower().startswith("https://") else None)
        caption = strip_unsafe(im.get("caption") or "")[:200] or None
        try:
            await ctx.bot.send_photo(chat_id, photo=im["image_url"], caption=caption,
                                     reply_to_message_id=reply_to if sent == 0 else None, reply_markup=kb)
            sent += 1
        except TelegramError as e:               # 이 url 을 텔레그램이 못 받음(죽은 링크·형식) → 다음 후보 사진으로
            log.info("사진 올리기 실패, 다음 후보로 (chat=%s): %s", chat_id, e)
            continue
    if not sent:
        return f"'{query}' 사진을 찾긴 했는데 올리다 다 실패했어요. 다른 낱말로 다시 해보라고 한마디."

    await db.bump(day, 0, f"photo:{uid}")
    ctx.quiet = True
    return (f"'{query}' 사진 {sent}장을 방에 올렸어요 (웹에서 찾음). 짧게 한마디만 — "
            "다시 설명하거나 사진을 글로 옮기지 말 것.")


_DESC = ("웹에서 사진을 찾아 방에 바로 올린다 (챗지피티처럼) — '이채영 사진 줘'·'골든리트리버 사진 보여줘'·'에펠탑 야경'·"
         "'그 사건 사진'. 연예인·동물·사물·장소·밈 등 아무 사진. OpenAI 웹검색이 찾은 이미지를 올리고 [📄 출처] 버튼을 단다. "
         "누구나 쓸 수 있고 하루 한도가 있다(photo_daily). query=찾을 대상(한국어 그대로, 사람은 이름). count=장수(기본 1, 최대 3). "
         "make_image(새로 그림)·make_sticker(스티커)와 다름 — 이건 실제 웹 사진을 가져오는 것. 성적·선정적 사진은 거절.")
TOOL = Tool("find_photo", _DESC,
            {"query": {"type": "string", "description": "찾을 사진 (한국어 그대로, 사람은 이름 — 예: '이채영', '골든리트리버 강아지', '에펠탑 야경')"},
             "count": {"type": "integer", "description": "올릴 장수 (기본 1, 최대 3). 여러 장 보여달라고 할 때만 2~3."}},
            ["query"], t_find_photo, Role.MEMBER)   # setting 으로 숨기지 않음(캐시 일관성) — photo_daily 0 은 핸들러가 안내
register_tool(TOOL, read_only=False)
