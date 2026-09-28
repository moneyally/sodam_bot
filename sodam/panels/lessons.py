"""🧠 소담이 교훈 (sodam/lessons.py): 그룹 허브 m:lsn:<방> 목록 · 🗑 m:lsx:<방>:<id> (텔레그램 관리자, 누를 때 다시 확인).
AI 도구 save_lesson(text): 관리자가 소담의 일하는 법을 정정했을 때 (확인 카드 없이 저장 — 말투·사실·제재가 아니라서, 목록에서 지움)."""
from __future__ import annotations

from .. import agentlog, lessons, menu, tools
from ..menu import TG_ADMIN, B, HubItem, PanelCtx, Route, Screen, _back, _kb
from ..permissions import Role
from ..util import esc, fmt_time, to_int

GUIDE = ("소담이 대표님 정정에서 배운 <b>일하는 법</b>이에요 (예: '케테르 플레이어 배팅은 /플').\n"
         "대화에서 '아니 그거 말고 …' 라고 하면 소담이 여기에 적고 다음부터 참고해요. 틀린 건 🗑 로 지워 주세요.\n"
         "말투·호칭은 📝 AI 방 안내, 멤버에게 알려줄 규칙·가격은 📚 자료로.")


async def s_lsn(c: PanelCtx) -> Screen:
    rows = await lessons.list_(c.svc.db, c.cid)
    lines = [f"🧠 <b>소담이 교훈</b> ({len(rows)}/{lessons.MAX_PER_ROOM})", GUIDE, ""]
    lines += [f"{i}. {esc(r['text'])} <i>· {fmt_time(r['ts'], c.svc.cfg.tz)}</i>" for i, r in enumerate(rows, 1)] or ["아직 없어요."]
    kb = [[B(f"🗑 {i}", f"m:lsx:{c.cid}:{r['id']}") for i, r in enumerate(rows[k:k + 5], k + 1)] for k in range(0, len(rows), 5)]
    return Screen("\n".join(lines), _kb([*kb, _back(c.cid)]))


async def r_lsx(c: PanelCtx) -> Screen:
    lid = to_int(c.args[0]) if c.args else None
    done = lid is not None and await lessons.delete(c.svc.db, c.cid, lid, c.uid)
    screen = await s_lsn(c)
    screen.toast = "지웠어요." if done else "이미 없어요."
    return screen


async def t_save_lesson(ctx: tools.ToolCtx, a: dict) -> str:
    text = lessons.clean(a.get("text", ""))
    if not await ctx.svc.perms.is_tg_admin(ctx.bot, ctx.chat_id, ctx.caller.id):
        return "교훈은 텔레그램 관리자만 남길 수 있음 (권한). 그렇게 짧게 안내할 것."
    run = agentlog.current.get()
    if run is not None and any(s.get("tool") in tools.READ_ONLY for s in run.steps):
        return "저장하지 않았음: 교훈은 관리자가 직접 한 말로만 (이 답변에서 기록·봇 글을 읽었음). 관리자가 다시 직접 말해 달라고 안내."
    err = lessons.check(text)
    if err:
        return f"저장하지 않았음: {err} 그대로 짧게 안내할 것."
    new = await lessons.add(ctx.svc.db, ctx.chat_id, text, ctx.caller.id)
    return ("교훈으로 저장함" if new else "이미 있는 교훈이라 최신으로 표시함") + \
        " (다음 답부터 참고, 관리자 1:1 메뉴 🧠 에서 지울 수 있음). 짧게 '기억할게요' 정도로 답하고 원래 요청도 이어서 처리."


menu.register_hub(HubItem(42, "lsn", "🧠 소담이 교훈", TG_ADMIN))
menu.register_screen("lsn", s_lsn, need=TG_ADMIN)
menu.register_route("lsx", Route(r_lsx, TG_ADMIN, fresh=True))
tools.register_tool(tools.Tool(
    "save_lesson",
    "[관리자] 관리자가 소담의 일하는 법을 바로잡아 줬을 때 그 교훈을 이 방에 적어 둔다 ('아니 플레이어는 /플이야', "
    "'예약은 한국 시각으로 해'). 관리자가 방금 직접 한 말만, 한 문장으로. 말투·호칭은 set_room_instructions, "
    "멤버에게 알릴 규칙·가격은 save_room_rule, 제재·권한은 안 됨.",
    {"text": {"type": "string", "description": f"교훈 한 문장 ({lessons.MAX_CHARS}자 이내, 관리자가 말한 내용만)"}},
    ["text"], t_save_lesson, Role.ADMIN, where="room"))
