"""🚫 방별 명령 끄기 명령 (.명령끄기 · .명령켜기 · .꺼진명령). 2026-11-22 경쟁조사 ③.
핵심 로직은 sodam/disable.py. 명령 등록은 handlers 가 import 뒤 register_commands()."""
from __future__ import annotations

from .. import commands, disable
from ..permissions import Role
from ..util import esc


async def c_disable(ctx: commands.CmdCtx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("그룹방에서 써 주세요. 예: <code>.명령끄기 시세</code>")
        return
    raw = ctx.argstr.strip()
    if not raw or raw in ("목록", "list"):
        await ctx.reply(await _list_text(ctx))
        return
    await ctx.reply(esc(await disable.disable_cmd(ctx.svc.db, ctx.chat_id, raw)))


async def c_enable(ctx: commands.CmdCtx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("그룹방에서 써 주세요.")
        return
    raw = ctx.argstr.strip()
    if not raw:
        await ctx.reply("켤 명령을 써 주세요. 예: <code>.명령켜기 시세</code>")
        return
    await ctx.reply(esc(await disable.enable_cmd(ctx.svc.db, ctx.chat_id, raw)))


async def c_offlist(ctx: commands.CmdCtx) -> None:
    await ctx.reply(await _list_text(ctx))


async def _list_text(ctx: commands.CmdCtx) -> str:
    names = await disable.off_list(ctx.svc.db, ctx.chat_id)
    if not names:
        return "이 방에서 꺼진 명령이 없어. 끄려면 <code>.명령끄기 이름</code>"
    chips = "  ".join(f"<code>.{esc(n)}</code>" for n in names)
    return f"🔇 이 방에서 꺼진 명령 {len(names)}개:\n{chips}"


def register_commands() -> None:
    for cmd in (commands.Cmd(("명령끄기", "disablecmd"), c_disable, Role.ADMIN, usage="명령이름 | 목록",
                             help="이 방에서 그 명령을 못 쓰게 (관리·안전 명령은 못 끔)", group="관리자"),
                commands.Cmd(("명령켜기", "enablecmd"), c_enable, Role.ADMIN, usage="명령이름",
                             help="꺼 둔 명령 다시 켜기", group="관리자"),
                commands.Cmd(("꺼진명령", "disabledcmds"), c_offlist, Role.ADMIN,
                             help="이 방에서 꺼진 명령 목록", group="관리자")):
        if any(c.fn is cmd.fn for c in commands.COMMANDS):
            continue
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)
