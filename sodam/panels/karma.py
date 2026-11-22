"""👍 평판 명령 (.평판 · .평판순위). 2026-11-22 경쟁조사 ⑦.
핵심 로직은 sodam/karma.py. 명령 등록은 handlers 가 import 뒤 register_commands()."""
from __future__ import annotations

from .. import commands, karma
from ..util import esc, mention


async def c_karma(ctx: commands.CmdCtx) -> None:
    if ctx.chat_id > 0:
        await ctx.reply("그룹방에서 써 주세요. (글에 답장으로 <code>+1</code> 하면 그 사람 평판이 올라요)")
        return
    reply = ctx.msg.reply_to_message
    target = reply.from_user if (reply and reply.from_user) else ctx.user
    r, pts = await karma.rank(ctx.svc.db, ctx.chat_id, target.id)
    name = esc(target.first_name or "그분")
    if pts <= 0:
        await ctx.reply(f"👤 {name}님은 아직 평판 점수가 없어요. (글에 <code>+1</code> 답장으로 추천해요)")
        return
    await ctx.reply(f"👍 {name}님 평판 <b>{pts}</b>점 · 이 방 {r}위")


async def c_karmarank(ctx: commands.CmdCtx) -> None:
    rows = await karma.top(ctx.svc.db, ctx.chat_id, 10)
    if not rows:
        await ctx.reply("아직 평판 순위가 없어요. 좋은 글에 <code>+1</code> 답장으로 추천해 보세요.")
        return
    medal = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"{medal[i]} {mention(r['user_id'], str(r['user_id']))} — {r['points']}점"
             for i, r in enumerate(rows)]
    await ctx.reply("👍 <b>평판 순위</b>\n" + "\n".join(lines))


def register_commands() -> None:
    for cmd in (commands.Cmd(("평판", "karma", "추천점수"), c_karma, usage="[답장]",
                             help="평판 점수·순위 보기 (글에 +1 답장으로 추천)", group="평판"),
                commands.Cmd(("평판순위", "karmarank", "추천순위"), c_karmarank,
                             help="이 방 평판 순위", group="평판")):
        if any(c.fn is cmd.fn for c in commands.COMMANDS):
            continue
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)
