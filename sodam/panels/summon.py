"""📣 집합(소집) 화면·명령·AI 도구. 코어는 sodam/summon.py.

선택형: 관리자가 부를 사람만 골라 한 번에 @태그. 자동 규칙(이벤트·출석) 없음 (오너 결정 2026-10-21).
m:smn:<방>            메인(선택 현황·고르기 버튼·집합 보내기)
m:smr:<방>:<페이지>    🕐 최근 활동 멤버 체크
m:smq:<방>            🔍 이름 검색 결과 체크 (검색어는 chat_state summon_q:<관리자>)
m:sml:<방>            📂 저장 명단 목록 (불러오기·삭제)
m:smx:<방>:<사람>:<복귀>  한 명 토글 (복귀 = m|r<페이지>|q)
m:smc:<방>            선택 비우기 · m:smg 미리보기 · m:smgo 집합 실행 · m:smld 명단 불러오기 · m:smdl 명단 삭제
입력: smns(이름 검색) · smna(@아이디·ID 여러 명) · smnsave(명단 저장)
명령 `.집합 [명단|@a @b|답장]` · AI 도구 summon_members.
"""
from __future__ import annotations

from telegram import Message

from .. import commands, menu, summon, tools
from ..menu import TG_ADMIN, B, HubItem, PanelCtx, Route, Screen, _back, _kb
from ..permissions import Role
from ..settings import register_setting
from ..util import esc, to_int

PER = 8   # 최근활동·검색 한 페이지 인원


def _qkey(admin_id: int) -> str:
    return f"summon_q:{admin_id}"


async def _sel_set(c: PanelCtx) -> set[int]:
    return set(await summon.picked(c.svc.db, c.cid, c.uid))


def _mark(uid: int, sel: set[int]) -> str:
    return "✅" if uid in sel else "➕"


# ── 메인 ─────────────────────────────────────────────────
async def s_smn(c: PanelCtx) -> Screen:
    sel = await summon.picked(c.svc.db, c.cid, c.uid)
    rows = await summon.member_rows(c.svc.db, c.cid, sel)
    names = ", ".join(esc(summon.name_of(rows.get(u, {"user_id": u}))) for u in sel[:20])
    more = f" 외 {len(sel) - 20}명" if len(sel) > 20 else ""
    lines = ["📣 <b>집합 (소집)</b>",
             "부를 사람만 골라서 한 번에 @태그해요. (자동 규칙 없이 직접 선택)", ""]
    lines.append(f"<b>선택됨 {len(sel)}명</b>" + (f": {names}{more}" if sel else " — 아직 없어요."))
    kb = [[B("🕐 최근 활동", f"m:smr:{c.cid}:0"), B("🔍 이름 검색", f"m:in:{c.cid}:smns")],
          [B("✏️ @아이디·ID 추가", f"m:in:{c.cid}:smna")],
          [B("📂 명단 불러오기", f"m:sml:{c.cid}"), B("💾 명단 저장", f"m:in:{c.cid}:smnsave")]]
    if sel:
        kb.append([B("🗑 선택 비우기", f"m:smc:{c.cid}")])
        kb.append([B(f"📣 집합 보내기 ({len(sel)}명)", f"m:smg:{c.cid}")])
    return Screen("\n".join(lines), _kb([*kb, _back(c.cid)]))


# ── 🕐 최근 활동 ─────────────────────────────────────────
async def s_recent(c: PanelCtx) -> Screen:
    page = max(0, to_int(c.arg(0)) or 0)
    sel = await _sel_set(c)
    members = await summon.recent_members(c.svc.db, c.cid, page * PER, PER)
    total = await summon.recent_count(c.svc.db, c.cid)
    lines = [f"🕐 <b>최근 활동한 멤버</b> ({page * PER + 1}~{page * PER + len(members)} / {total}명)",
             "눌러서 선택/해제. 선택된 사람은 ✅."]
    kb = [[B(f"{_mark(m['user_id'], sel)} {summon.name_of(m)[:20]}",
             f"m:smx:{c.cid}:{m['user_id']}:r{page}")] for m in members]
    nav = []
    if page > 0:
        nav.append(B("◀ 이전", f"m:smr:{c.cid}:{page - 1}"))
    if page * PER + len(members) < total:
        nav.append(B("다음 ▶", f"m:smr:{c.cid}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([B(f"✔️ 완료 (선택 {len(sel)}명)", f"m:smn:{c.cid}")])
    return Screen("\n".join(lines), _kb(kb))


# ── 🔍 이름 검색 결과 ────────────────────────────────────
async def s_search(c: PanelCtx) -> Screen:
    q = await c.svc.db.get_state(c.cid, _qkey(c.uid), "")
    if not q:
        return await s_smn(c)
    sel = await _sel_set(c)
    found = await summon.search_members(c.svc.db, c.cid, q, limit=16)
    lines = [f"🔍 <b>'{esc(q)}' 검색</b> — {len(found)}명", "눌러서 선택/해제."]
    kb = [[B(f"{_mark(m['user_id'], sel)} {summon.name_of(m)[:20]}",
             f"m:smx:{c.cid}:{m['user_id']}:q")] for m in found]
    if not found:
        lines.append("\n못 찾았어요. 🔍 이름 검색으로 다시 하거나 @아이디·ID 로 추가해 주세요.")
    kb.append([B(f"✔️ 완료 (선택 {len(sel)}명)", f"m:smn:{c.cid}"), B("🔍 다시 검색", f"m:in:{c.cid}:smns")])
    return Screen("\n".join(lines), _kb(kb))


# ── 📂 저장 명단 ─────────────────────────────────────────
async def s_lists(c: PanelCtx) -> Screen:
    rows = await summon.lists(c.svc.db, c.cid)
    lines = ["📂 <b>저장한 집합 명단</b>", "불러오면 지금 선택에 더해져요."]
    kb = []
    for r in rows[:12]:
        kb.append([B(f"📂 {r['name'][:16]} ({r['n']}명)", f"m:smld:{c.cid}:{r['id']}"),
                   B("🗑", f"m:smdl:{c.cid}:{r['id']}")])
    if not rows:
        lines.append("\n아직 없어요. 선택을 만든 뒤 💾 명단 저장으로 저장하세요.")
    kb.append([B("🔙 집합 화면", f"m:smn:{c.cid}")])
    return Screen("\n".join(lines), _kb(kb))


# ── 토글 ─────────────────────────────────────────────────
async def r_toggle(c: PanelCtx) -> Screen:
    uid = to_int(c.arg(0))
    ret = c.arg(1) or "m"
    toast = "만료된 버튼이에요."
    if uid is not None:
        before = uid in await _sel_set(c)
        now_sel = await summon.toggle(c.svc.db, c.cid, c.uid, uid)
        if now_sel:
            toast = "➕ 선택에 넣었어요."
        elif before:
            toast = "➖ 선택에서 뺐어요."
        else:
            toast = f"⚠️ 최대 {summon.MAX_PICK}명까지예요."
    if ret.startswith("r"):
        c.args = [ret[1:] or "0"]
        s = await s_recent(c)
    elif ret == "q":
        c.args = []
        s = await s_search(c)
    else:
        c.args = []
        s = await s_smn(c)
    s.toast = toast
    return s


async def r_clear(c: PanelCtx) -> Screen:
    await summon.clear(c.svc.db, c.cid, c.uid)
    s = await s_smn(c)
    s.toast = "선택을 비웠어요."
    return s


async def r_load(c: PanelCtx) -> Screen:
    lid = to_int(c.arg(0))
    ids = await summon.list_ids(c.svc.db, c.cid, lid) if lid is not None else []
    added = await summon.add_ids(c.svc.db, c.cid, c.uid, ids) if ids else 0
    s = await s_smn(c)
    s.toast = f"📂 {added}명 더했어요 (총 {len(await summon.picked(c.svc.db, c.cid, c.uid))}명)." if ids else "빈 명단이에요."
    return s


async def r_dellist(c: PanelCtx) -> Screen:
    lid = to_int(c.arg(0))
    done = await summon.delete_list(c.svc.db, c.cid, lid) if lid is not None else False
    s = await s_lists(c)
    s.toast = "명단을 지웠어요." if done else "이미 없어요."
    return s


# ── 미리보기 · 집합 실행 ──────────────────────────────────
async def r_confirm(c: PanelCtx) -> Screen:
    sel = await summon.picked(c.svc.db, c.cid, c.uid)
    if not sel:
        s = await s_smn(c)
        s.toast = "고른 사람이 없어요."
        return s
    rows = await summon.member_rows(c.svc.db, c.cid, sel)
    names = ", ".join(esc(summon.name_of(rows.get(u, {"user_id": u}))) for u in sel[:30])
    more = f" 외 {len(sel) - 30}명" if len(sel) > 30 else ""
    text = (f"📣 <b>이 {len(sel)}명에게 집합을 보낼까요?</b>\n\n{names}{more}\n\n"
            "방에 올라가고, 고른 사람들에게 알림이 가요.")
    kb = [[B(f"📣 집합 보내기 ({len(sel)}명)", f"m:smgo:{c.cid}")],
          [B("🔙 다시 고르기", f"m:smn:{c.cid}")]]
    return Screen(text, _kb(kb))


async def r_fire(c: PanelCtx) -> Screen:
    sel = await summon.picked(c.svc.db, c.cid, c.uid)
    if not sel:
        s = await s_smn(c)
        s.toast = "고른 사람이 없어요."
        return s
    s_ = await c.svc.db.get_settings(c.cid)
    header = s_.get("summon_text") or "📣 집합! 다들 모여라 📣"
    n = await summon.fire(c.svc, c.bot, c.cid, sel, header)
    screen = await s_smn(c)
    screen.toast = f"📣 {n}명에게 집합을 보냈어요!" if n else "집합을 못 보냈어요 (봇 권한 확인)."
    return screen


# ── 입력 ─────────────────────────────────────────────────
async def in_search(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    q = (msg.text or "").strip()
    if not q:
        return False, "이름을 보내 주세요."
    await c.svc.db.set_state(c.cid, _qkey(c.uid), q[:40])
    return True, ""


async def in_addids(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    toks = [t.strip().lstrip("@") for t in (msg.text or "").replace(",", " ").split() if t.strip()]
    if not toks:
        return False, "@아이디나 숫자 ID 를 보내 주세요."
    found, miss = [], []
    for t in toks[:30]:
        rows = await c.svc.db.find_members(c.cid, t)
        if len(rows) == 1:
            found.append(rows[0]["user_id"])
        else:
            miss.append(t)
    added = await summon.add_ids(c.svc.db, c.cid, c.uid, found) if found else 0
    note = f"➕ {added}명 더했어요." if added else "추가된 사람이 없어요."
    if miss:
        note += f" 못 찾음: {esc(', '.join(miss[:5]))}"
    return True, note


async def in_save(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    name = (msg.text or "").strip()
    sel = await summon.picked(c.svc.db, c.cid, c.uid)
    if not sel:
        return False, "저장할 선택이 없어요. 사람을 먼저 고르세요."
    if not name:
        return False, "명단 이름을 보내 주세요. 예: <code>단골</code>"
    lid = await summon.save_list(c.svc.db, c.cid, name, sel, c.uid)
    if lid is None:
        return False, f"저장 실패 (명단이 방마다 {summon.MAX_LISTS}개까지예요)."
    return True, f"💾 '{esc(name.strip()[:40])}' 명단으로 {len(sel)}명 저장했어요."


# ── 명령 `.집합` ─────────────────────────────────────────
async def c_summon(ctx: commands.CmdCtx) -> None:
    db = ctx.svc.db
    chat_id = ctx.chat_id
    s = await db.get_settings(chat_id)
    header = s.get("summon_text") or "📣 집합! 다들 모여라 📣"
    targets: list[int] = []
    reply = ctx.msg.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        targets.append(reply.from_user.id)
    # 인자 하나가 저장 명단 이름이면 그 명단
    if len(ctx.args) == 1 and not reply:
        rows = await summon.lists(db, chat_id)
        hit = next((r for r in rows if r["name"].lower() == ctx.args[0].lower()), None)
        if hit:
            targets = await summon.list_ids(db, chat_id, hit["id"])
    if not targets and ctx.args:
        miss = []
        for t in ctx.args[:30]:
            rows = await db.find_members(chat_id, t.lstrip("@"))
            if len(rows) == 1:
                targets.append(rows[0]["user_id"])
            elif not rows:
                miss.append(t)
        if miss and not targets:
            await ctx.reply("못 찾았어요: " + esc(", ".join(miss[:5]) )
                            + "\n답장하거나 @아이디·숫자 ID 로, 또는 1:1 ⚙️ → 📣 집합 에서 골라 보내세요.")
            return
    if not targets:  # 답장·인자 없음 → 1:1 에서 만든 내 선택
        targets = await summon.picked(db, chat_id, ctx.user.id)
    if not targets:
        await ctx.reply("부를 사람을 정해 주세요.\n"
                        "• 답장하며 <code>.집합</code>\n"
                        "• <code>.집합 @아이디 @아이디</code>\n"
                        "• <code>.집합 명단이름</code> (저장한 명단)\n"
                        "• 1:1 ⚙️ → 📣 집합 에서 골라 두고 <code>.집합</code>")
        return
    n = await summon.fire(ctx.svc, ctx.bot, chat_id, targets, header)
    if not n:
        await ctx.reply("집합을 못 보냈어요 (봇 권한을 확인해 주세요).")


def register_commands() -> None:
    """handlers 가 import 뒤 부름 (commands→menu→panels 순환 피함)."""
    if any(c.fn is c_summon for c in commands.COMMANDS):
        return
    cmd = commands.Cmd(("집합", "소집", "모여", "summon"), c_summon, Role.ADMIN,
                       usage="@아이디 @아이디 | 명단이름 | (답장) | (없으면 1:1에서 고른 선택)",
                       help="고른 사람만 한 번에 @태그(소집)", group="관리")
    commands.COMMANDS.append(cmd)
    for name in cmd.names:
        commands._INDEX.setdefault(name.lower(), cmd)


# ── AI 도구 ──────────────────────────────────────────────
async def t_summon(ctx: tools.ToolCtx, a: dict) -> str:
    if ctx.role < Role.ADMIN:
        return "집합(소집)은 관리자만 할 수 있음. 그렇게 짧게 안내."
    if ctx.chat_id > 0:
        return "집합은 그룹방에서만 돼요. 그렇게 안내."
    raw = a.get("names", "")
    names = raw if isinstance(raw, list) else [t for t in str(raw).replace(",", " ").split() if t]
    db = ctx.svc.db
    # 저장 명단 이름이면 그 명단
    targets: list[int] = []
    if len(names) == 1:
        rows = await summon.lists(db, ctx.chat_id)
        hit = next((r for r in rows if r["name"].lower() == str(names[0]).lower()), None)
        if hit:
            targets = await summon.list_ids(db, ctx.chat_id, hit["id"])
    miss = []
    if not targets:
        for nm in names[:30]:
            rows = await db.find_members(ctx.chat_id, str(nm).lstrip("@"))
            if len(rows) == 1:
                targets.append(rows[0]["user_id"])
            else:
                miss.append(str(nm))
    if not targets:
        return (f"집합할 사람을 못 찾음 ({esc(', '.join(miss[:5]))}). 정확한 이름·@아이디로 다시 알려 달라고 하거나 "
                "1:1 ⚙️ → 📣 집합 에서 고르라고 안내.")
    s = await db.get_settings(ctx.chat_id)
    header = s.get("summon_text") or "📣 집합! 다들 모여라 📣"
    n = await summon.fire(ctx.svc, ctx.bot, ctx.chat_id, targets, header)
    extra = f" (못 찾음: {esc(', '.join(miss[:5]))})" if miss else ""
    return (f"{n}명에게 집합(소집) 글을 방에 올렸음{extra}. 짧게 '○○명 불렀어요' 정도로." if n
            else "집합을 못 보냄 (봇 권한 부족). 짧게 안내.")


def _validate_text(v: str) -> str:
    v = (v or "").strip()[:100]
    if "http" in v.lower() or "t.me" in v.lower():
        raise ValueError("집합 문구에는 링크를 넣을 수 없어요.")
    return v


register_setting("summon_text", "📣 집합! 다들 모여라 📣", "집합 문구", validator=_validate_text)

menu.register_hub(HubItem(40, "smn", "📣 집합 (소집)", TG_ADMIN))
menu.register_screen("smn", s_smn, need=TG_ADMIN)
menu.register_screen("smr", s_recent, need=TG_ADMIN)
menu.register_screen("smq", s_search, need=TG_ADMIN)
menu.register_screen("sml", s_lists, need=TG_ADMIN)
menu.register_route("smx", Route(r_toggle, TG_ADMIN))
menu.register_route("smc", Route(r_clear, TG_ADMIN))
menu.register_route("smg", Route(r_confirm, TG_ADMIN))
menu.register_route("smgo", Route(r_fire, TG_ADMIN, fresh=True))
menu.register_route("smld", Route(r_load, TG_ADMIN))
menu.register_route("smdl", Route(r_dellist, TG_ADMIN))
menu.register_input("smns", "🔍 찾을 <b>이름</b>을 보내 주세요. (일부만 쳐도 돼요)", "smn", in_search, s_search)
menu.register_input("smna", "✏️ 부를 사람의 <b>@아이디나 숫자 ID</b>를 보내 주세요 (여러 명은 띄어쓰기·쉼표).",
                    "smn", in_addids, s_smn)
menu.register_input("smnsave", "💾 저장할 <b>명단 이름</b>을 보내 주세요. 예: <code>단골</code>",
                    "smn", in_save, s_smn)
tools.register_tool(tools.Tool(
    "summon_members",
    "[관리자] 고른 사람들을 한 번에 @태그해서 방에 '집합(소집)' 글을 올린다. "
    "관리자가 '철수 영희 집합시켜'·'단골 명단 불러'처럼 부를 때. names = 이름·@아이디 목록 또는 저장 명단 이름 하나. "
    "제재가 아니라 멘션이라 확인 카드 없음. 그룹방에서만.",
    {"names": {"type": "array", "items": {"type": "string"},
               "description": "부를 사람들의 이름·@아이디 목록, 또는 저장한 집합 명단 이름 하나"}},
    ["names"], t_summon, Role.ADMIN, where="room"))
