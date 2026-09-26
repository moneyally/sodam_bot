"""handlers.GROUP_MESSAGE_HOOKS 에 안전하게 등록하기 (순환 import 방지).

`python -m sodam` 은 handlers 를 먼저 불러오고, handlers → menu → panels/* 순서로 패널이 import 된다.
그 순간 handlers 는 아직 반쯤 만들어진 상태라 GROUP_MESSAGE_HOOKS 가 없다 (파일 아래쪽에서 새 리스트로 만들어짐).
그래서 그땐 대기열에 넣어두고, handlers import 가 끝난 뒤 처음 일어나는 import 때 옮겨 담는다.
(__main__ 은 handlers 다음 줄에서 backup 등을 새로 import 하므로 봇이 메시지를 받기 전에 반드시 옮겨짐)
"""
from __future__ import annotations

import sys

_PENDING: list = []


def _hooks() -> list | None:
    h = sys.modules.get("sodam.handlers")
    return getattr(h, "GROUP_MESSAGE_HOOKS", None) if h else None


def flush() -> bool:
    """대기 중인 훅을 handlers 로 옮긴다. 다 옮겼으면 True."""
    hooks = _hooks()
    if hooks is None:
        return not _PENDING
    while _PENDING:
        fn = _PENDING.pop(0)
        if fn not in hooks:
            hooks.append(fn)
    return True


class _FlushOnNextImport:
    """아무 모듈도 찾아주지 않는 finder. 다음 import 때 flush 만 하고 빠진다."""

    def find_spec(self, name, path=None, target=None):
        if flush() and self in sys.meta_path:
            sys.meta_path.remove(self)
        return None


def add_group_message_hook(fn) -> None:
    """handlers.GROUP_MESSAGE_HOOKS.append(fn) 와 같지만 import 순서와 상관없이 동작.
    (여기서 handlers 를 직접 import 하면 commands 가 반쯤 만들어진 상태에서 CmdCtx 를 못 찾아 터질 수 있어서 기다린다)"""
    _PENDING.append(fn)
    if not flush() and not any(isinstance(f, _FlushOnNextImport) for f in sys.meta_path):
        sys.meta_path.insert(0, _FlushOnNextImport())
