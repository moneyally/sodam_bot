"""📢 말로 방에 공지 올리기 (AI 도구 room_announce): 관리자가 "소담아 '○○' 방에 올려줘" 하면 봇이 그 방에 **봇 명의로, 문구 그대로** 올린다.

2026-11-19 태산 대표 루디 요청 — "소담아 '몽키씨발려나 닥쳐' 이 문구 방에 올려줘 하면 그냥 소담이가 문구그대로 전달하는 거".
예전엔 소담이 "방에 직접 올리는 기능이 없어요, 복사해서 올리세요" 로 거절했음(도구가 없었음). 자러갈 때 멘트 올리기 등에도 씀.

**확인 카드 없이 바로** 올린다(대표 '그냥 전달' 요청). 대신: ① 관리자 전용(1:1 은 그 방 TG 관리자인지 직접 확인) ② 인젝션(봇 조종)
문구 거절 ③ 멤버 글을 읽은 답변(tainted)에선 안 올림(숨은 지시로 못 올리게) ④ 이용 기간인 방만. 글 그대로(링크 OK, 서식 안 건드림).
리모컨(버튼·사진)·예약공지와 달리 **글만 말로 즉시**.
"""
from __future__ import annotations

from telegram.error import TelegramError

from .. import menu, tools  # noqa: F401  (menu: 패널 자동 로드 묶음 일관성)
from ..permissions import Role
from ..security import scan

MIN_CHARS, MAX_CHARS = 1, 2000


async def t_room_announce(ctx: tools.ToolCtx, a: dict) -> str:
    text = str(a.get("text", "")).strip()
    if len(text) < MIN_CHARS:
        return "방에 올릴 내용(text)을 알려 달라고 할 것. 예: '방에 \"오늘 10시 정모\" 올려줘'."
    text = text[:MAX_CHARS]
    if ctx.tainted:   # 멤버 글(숨은 지시)을 읽은 답변에선 방에 못 올림
        return "다른 사람 글을 본 뒤라 방에 공지를 올리지 않았음(안전). 관리자가 직접 올릴 내용만 다시 말하면 된다고 안내할 것."
    if scan(text).blocked:   # 봇 조종(인젝션) 문구는 방에 안 올림
        return "지시문(봇 조종) 같은 문장이라 방에 올리지 않았음. 올릴 공지 내용만 다시 말해 달라고 할 것."
    room, head = await tools.room_scope(ctx, a)   # 대상 방 (1:1 이면 내가 관리자인 방 찾기/되물음)
    if room is None:
        return head
    rid = room.chat_id
    if not (await ctx.svc.perms.is_tg_admin(ctx.bot, rid, ctx.caller.id)
            or ctx.caller.id in await ctx.svc.perms.owners()):   # 1:1 은 역할이 늘 member → 직접 확인
        return "그 방 관리자만 공지를 올릴 수 있다고 안내할 것."
    if not await ctx.svc.paid_features(rid):
        return "그 방은 이용 기간이 아니라 공지를 올릴 수 없다고 안내할 것."
    try:
        await ctx.bot.send_message(rid, text)   # 봇 명의로, 글 그대로 (링크 OK · parse_mode 없음 = 서식 깨짐 방지)
    except TelegramError as e:
        return f"방에 올리지 못했음({e}). 봇에게 글 보내는 권한이 있는지 확인하라고 안내할 것."
    await ctx.svc.db.log_mod(rid, ctx.caller.id, None, "announce", text[:100])
    if ctx.chat_id < 0:          # 방에서 시킴 → 올린 글이 곧 결과 (AI 답 생략)
        ctx.quiet = True
        return head + "방에 그대로 올렸음(올린 글이 곧 결과). AI 답은 보내지 않음."
    return head + "방에 그대로 올렸음. 짧게 '올렸어요'만, 내용 반복하지 말 것."


TOOL = tools.Tool(
    "room_announce",
    "[관리자] 방에 공지·안내 글을 **봇 명의로, 문구 그대로** 바로 올린다. '방에 \"오늘 10시 정모\" 올려줘'·"
    "'나 자러간다고 방에 올려줘'·'이 문구 방에 올려줘'·'방에 공지 올려: …'. 1:1 에서도 내가 관리자인 방에 올림(방 이름으로). "
    "봇이 말하듯 글 그대로 올라감(링크 OK, 다듬지 말 것). 리모컨(버튼·사진)·예약공지와 달리 **글만 말로 즉시**.",
    {"text": {"type": "string", "description": "방에 올릴 글 그대로 (말한 사람 문구만, 없는 내용 추가·순화 금지)"}, **tools.ROOM_PARAM},
    ["text"], t_room_announce, Role.MEMBER, where="any", room_role=Role.ADMIN)   # 1:1 에선 도구가 직접 관리자 확인
tools.register_tool(TOOL)
