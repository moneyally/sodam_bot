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


def add_group_message_hook(fn) -> None:
    if fn not in GROUP_MESSAGE_HOOKS:
        GROUP_MESSAGE_HOOKS.append(fn)


def add_group_edit_hook(fn) -> None:
    if fn not in GROUP_EDIT_HOOKS:
        GROUP_EDIT_HOOKS.append(fn)


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
