"""기능 모듈이 등록하는 확장 지점. 어디서도 import 하지 않는 가벼운 모듈이라 순환 import 걱정이 없다.

GROUP_MESSAGE_HOOKS: 관리 검사를 통과한 그룹 메시지마다 백그라운드로 불린다. hook(svc, bot, msg, role)
(handlers.GROUP_MESSAGE_HOOKS 는 이 리스트와 같은 객체)
"""
GROUP_MESSAGE_HOOKS: list = []
# 멤버가 나가거나 밴·킥 됐을 때 (동기 함수): hook(svc, chat_id, user_id)
MEMBER_LEFT_HOOKS: list = []


def add_group_message_hook(fn) -> None:
    if fn not in GROUP_MESSAGE_HOOKS:
        GROUP_MESSAGE_HOOKS.append(fn)


def add_member_left_hook(fn) -> None:
    if fn not in MEMBER_LEFT_HOOKS:
        MEMBER_LEFT_HOOKS.append(fn)


def member_left(svc, chat_id: int, user_id: int) -> None:
    for fn in MEMBER_LEFT_HOOKS:
        fn(svc, chat_id, user_id)
