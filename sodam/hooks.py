"""기능 모듈이 등록하는 확장 지점. 어디서도 import 하지 않는 가벼운 모듈이라 순환 import 걱정이 없다.

GROUP_MESSAGE_HOOKS: 관리 검사를 통과한 그룹 메시지마다 백그라운드로 불린다. hook(svc, bot, msg, role)
(handlers.GROUP_MESSAGE_HOOKS 는 이 리스트와 같은 객체)
"""
GROUP_MESSAGE_HOOKS: list = []
# 멤버가 나가거나 밴·킥 됐을 때 (동기 함수): hook(svc, chat_id, user_id)
MEMBER_LEFT_HOOKS: list = []
# 새 멤버 입장 검사 (관리 권한 있는 방, 관리자 아닌 사람, CAS 다음·캡차 전): async hook(svc, bot, chat_id, user) -> bool
# True 를 돌려주면 그 사람은 막은 것(밴·보류 등) → 캡차·인사·입장 기록을 하지 않는다
MEMBER_JOIN_HOOKS: list = []
# 수정된 그룹 메시지 (관리자·자유 멤버 제외, 관리 권한 없는 방 포함, 백그라운드): async hook(svc, bot, msg, role)
GROUP_EDIT_HOOKS: list = []
# 다른 봇이 그룹에 쓴 글·고친 글 (Bot-to-Bot 모드, sodam/botlink.py): async hook(svc, bot, msg), 백그라운드.
# 봇 글은 이 훅들만 받고 관리·명령·게임·AI·위 GROUP_MESSAGE_HOOKS 는 안 탐 → 여기서도 방에 답을 보내지 말 것 (봇끼리 무한 반복)
BOT_MESSAGE_HOOKS: list = []
BOT_EDIT_HOOKS: list = []
# 30초 틱(handlers.job_tick)마다: async hook(svc, bot). 하나가 터져도 나머지는 계속 (채널 예약 글·구독자 수 — sodam/channel.py)
TICK_HOOKS: list = []
# 메시지 반응(message_reaction 업데이트, handlers.on_any_update 에서): async hook(svc, bot, reaction) — 스티커 학습(sodam/stickerlearn.py)
REACTION_HOOKS: list = []


def add_group_message_hook(fn) -> None:
    if fn not in GROUP_MESSAGE_HOOKS:
        GROUP_MESSAGE_HOOKS.append(fn)


def add_group_edit_hook(fn) -> None:
    if fn not in GROUP_EDIT_HOOKS:
        GROUP_EDIT_HOOKS.append(fn)


def add_bot_message_hook(fn) -> None:
    if fn not in BOT_MESSAGE_HOOKS:
        BOT_MESSAGE_HOOKS.append(fn)


def add_bot_edit_hook(fn) -> None:
    if fn not in BOT_EDIT_HOOKS:
        BOT_EDIT_HOOKS.append(fn)


def add_tick_hook(fn) -> None:
    if fn not in TICK_HOOKS:
        TICK_HOOKS.append(fn)


async def tick(svc, bot) -> None:
    import logging
    import time
    for fn in TICK_HOOKS:
        t0 = time.monotonic()
        try:
            await fn(svc, bot)
        except Exception:
            logging.getLogger(__name__).exception("tick hook failed: %s", getattr(fn, "__name__", fn))
        if time.monotonic() - t0 > 10:
            logging.getLogger(__name__).warning("tick hook %s.%s 느림 %.1f초", fn.__module__, fn.__name__, time.monotonic() - t0)


def add_reaction_hook(fn) -> None:
    if fn not in REACTION_HOOKS:
        REACTION_HOOKS.append(fn)


async def reaction(svc, bot, mr) -> None:
    for fn in REACTION_HOOKS:
        try:
            await fn(svc, bot, mr)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("reaction hook failed: %s", getattr(fn, "__name__", fn))


def add_member_left_hook(fn) -> None:
    if fn not in MEMBER_LEFT_HOOKS:
        MEMBER_LEFT_HOOKS.append(fn)


def add_member_join_hook(fn) -> None:
    if fn not in MEMBER_JOIN_HOOKS:
        MEMBER_JOIN_HOOKS.append(fn)


async def member_joined(svc, bot, chat_id: int, user) -> bool:
    """입장 검사 훅을 차례로. 하나라도 막으면 True. 훅 하나가 터져도 입장 처리는 계속."""
    for fn in MEMBER_JOIN_HOOKS:
        try:
            if await fn(svc, bot, chat_id, user):
                return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception("member join hook failed: %s", getattr(fn, "__name__", fn))
    return False


def member_left(svc, chat_id: int, user_id: int) -> None:
    for fn in MEMBER_LEFT_HOOKS:
        fn(svc, chat_id, user_id)
