"""📝 멤버 노트 명령 (.저장 이름 → #이름). 2026-11-22 경쟁조사 ②.

핵심 로직·표는 sodam/notes.py, 재전송은 autoreply.snapshot/send_snap 재사용.
명령 등록은 handlers 가 import 뒤 notes.register_commands() (commands→menu→panels 순환 피함)."""
from __future__ import annotations

from .. import commands, notes
from ..util import esc


async def c_save(ctx: commands.CmdCtx) -> None:
    db, chat_id = ctx.svc.db, ctx.chat_id
    if chat_id > 0:
        await ctx.reply("그룹방에서 써 주세요. (저장한 노트는 그 방에서 #이름 으로 불러요)")
        return
    argstr = ctx.argstr.strip()
    head, _, rest = argstr.partition(" ")
    reply = ctx.msg.reply_to_message

    if not argstr or head in ("목록", "list"):
        await ctx.reply(await _list_text(db, chat_id))
        return
    if head in ("삭제", "지우기", "취소", "del", "remove"):
        await _del(ctx, rest)
        return

    is_admin = await ctx.svc.perms.is_admin(ctx.bot, chat_id, ctx.user.id)
    if reply is not None:
        res = await notes.save(db, chat_id, head, by=ctx.user.id, snap=_snap(reply))
    elif rest.strip():
        res = await notes.save(db, chat_id, head, by=ctx.user.id, snap={"html": esc(rest.strip()), "kind": None, "file_id": None})
    else:
        await ctx.reply("저장할 글에 <b>답장</b>으로 <code>.저장 이름</code> 하거나, "
                        "<code>.저장 이름 저장할 글</code> 처럼 써 주세요. 불러올 땐 <code>#이름</code>")
        return
    await ctx.reply((f"✅ <code>#{esc(head)}</code> 저장했어요. 방에서 <code>#{esc(head)}</code> 치면 나와요."
                     if res == "ok" else "❌ " + esc(res)))


async def _del(ctx: commands.CmdCtx, key: str) -> None:
    key = key.strip()
    if not key:
        await ctx.reply("지울 이름을 써 주세요. 예: <code>.저장 삭제 공지</code>")
        return
    is_admin = await ctx.svc.perms.is_admin(ctx.bot, ctx.chat_id, ctx.user.id)
    out = await notes.remove(ctx.svc.db, ctx.chat_id, key, by=ctx.user.id, is_admin=is_admin)
    await ctx.reply(esc(out))


async def c_notedel(ctx: commands.CmdCtx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("그룹방에서 써 주세요.")
        return
    await _del(ctx, ctx.argstr)


def _snap(msg) -> dict:
    from .. import autoreply
    return autoreply.snapshot(msg)


async def _list_text(db, chat_id: int) -> str:
    rows = await notes.rows(db, chat_id)
    if not rows:
        return "저장된 노트가 없어요. 글에 답장으로 <code>.저장 이름</code> 해보세요 → <code>#이름</code> 으로 불러요."
    names = "  ".join(f"<code>#{esc(r['key'])}</code>" for r in rows[:50])
    more = f"\n…외 {len(rows) - 50}개" if len(rows) > 50 else ""
    return f"📝 이 방 노트 {len(rows)}개:\n{names}{more}"


def register_commands() -> None:
    for cmd in (commands.Cmd(("저장", "노트", "save", "note"), c_save,
                             usage="이름 (글에 답장) | 삭제 이름 | 목록",
                             help="글을 저장해 두고 #이름 으로 불러오기", group="노트"),
                commands.Cmd(("노트삭제", "노트취소", "notedel"), c_notedel, usage="이름",
                             help="내 노트 지우기 (관리자는 아무거나)", group="노트")):
        if any(c.fn is cmd.fn for c in commands.COMMANDS):
            continue
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)
