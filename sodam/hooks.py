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
# 버튼 콜백 접두어 → async fn(svc, bot, q, parts) (handlers.on_callback 이 아는 접두어가 아니면 여기서 찾음 — sodam/stickerpack.py 'spk')
CALLBACK_HANDLERS: dict = {}


def add_callback_handler(prefix: str, fn) -> None:
    CALLBACK_HANDLERS[prefix] = fn


# 1:1 딥링크 '/start <접두어>_<값>' → async fn(svc, bot, msg, 값) (sub_·cfg_ 는 handlers 가 직접 — sodam/stickerpack.py 'spk')
DEEP_LINKS: dict = {}


def add_deep_link(prefix: str, fn) -> None:
    DEEP_LINKS[prefix] = fn


# 1:1 글자 (메뉴 글자 입력 다음, 명령·AI 전): async fn(svc, bot, msg) -> bool — True 면 처리 끝 (sodam/panels/brand.py 아래쪽 메뉴 버튼)
DM_TEXT_HOOKS: list = []
# 봇 시작(post_init 끝, 딜러 봇 제외) · 종료(post_shutdown, DB 닫기 전): async fn(svc, bot) (sodam/miniapp.py 웹 서버)
STARTUP_HOOKS: list = []
SHUTDOWN_HOOKS: list = []


def add_dm_text_hook(fn) -> None:
    if fn not in DM_TEXT_HOOKS:
        DM_TEXT_HOOKS.append(fn)


async def dm_text(svc, bot, msg) -> bool:
    for fn in DM_TEXT_HOOKS:
        try:
            if await fn(svc, bot, msg):
                return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception("dm text hook failed: %s", getattr(fn, "__name__", fn))
    return False


def add_startup_hook(fn) -> None:
    if fn not in STARTUP_HOOKS:
        STARTUP_HOOKS.append(fn)


def add_shutdown_hook(fn) -> None:
    if fn not in SHUTDOWN_HOOKS:
        SHUTDOWN_HOOKS.append(fn)


async def run_all(fns: list, svc, bot) -> None:
    """시작·종료 훅을 차례로 — 하나가 터져도 봇 시작·종료는 계속."""
    import logging
    for fn in list(fns):
        try:
            await fn(svc, bot)
        except Exception:
            logging.getLogger(__name__).exception("hook failed: %s", getattr(fn, "__name__", fn))


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
