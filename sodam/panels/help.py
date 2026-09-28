"""❓ 도움말 (m:help) — "소담에게 이렇게 말해보세요" 말 예시 먼저, 명령어 전체는 [📋 명령어 전체] (m:helpc).

menu.py 의 기본 도움말(r_help)을 이 파일이 덮어쓴다 (register_route). 예시 글은 commands.EXAMPLES_* 한 곳에서.
관리자 예시는 오너이거나 소담이 있는 방 하나라도 관리자인 사람에게만 (멤버에겐 멤버 예시만).
"""
from __future__ import annotations

from .. import menu
from ..menu import PUBLIC, B, PanelCtx, Route, Screen


async def r_help(c: PanelCtx) -> Screen:
    from .. import commands   # 늦게 import (commands → menu → panels 순환 방지)
    admin = await commands.is_any_admin(c.svc, c.bot, c.uid)
    text = "\n".join(["❓ <b>도움말</b>", "",
                      *commands.example_lines(c.svc.cfg.call_names[0], admin=admin, in_dm=True)])
    return Screen(text, menu._kb([[B("📋 명령어 전체", "m:helpc")], [B("⬅️ 처음으로", "m:home")]]))


async def r_help_commands(c: PanelCtx) -> Screen:
    text = menu.HELP.replace("<b>도움말</b>", "<b>명령어 전체</b>").replace(
        ".도움말 — 명령어 전체", ".명령어 — 명령어 전체 · .도움말 — 말 예시")   # .도움말 은 이제 말 예시
    return Screen(text, menu._kb([[B("⬅️ 도움말", "m:help")], [B("⬅️ 처음으로", "m:home")]]))


menu.register_route("help", Route(r_help, PUBLIC, scoped=False))
menu.register_route("helpc", Route(r_help_commands, PUBLIC, scoped=False))
