"""📥 입장요청(입요) 전원 승인·가라 거절 — 쌓아둔 명단(joinpending)으로 봇이 한 번에.

왜: 텔레그램은 '가입 신청 명단 보기'를 관리자인 유저 계정에만 허용한다 (봇 API 엔 목록 기능 없음·봇 MTProto 는 BotMethodInvalid —
2026-11-22 슈퍼배드 소통방 실측). 그래서 봇이 신청을 받을 때마다 joinpending.record 로 명단을 쌓아두고, 여기서 그 명단으로
`approveChatJoinRequest`/`declineChatJoinRequest` 를 돌린다 (관리자 승격·MTProto 필요 없음, 봇 혼자). 이 기능 배포 **이후** 신청만 쌓임.

관리자: '소담아 입요 전부 받아줘'(approve all) · '가라만 거절해'(decline foreign) · '입요 몇 개야'(count).
approve·decline 은 영향이 커서 확인 버튼 한 장 (요청 관리자만 누름). 명령 .입요 · .입요승인 · .입요거절.
"""
from __future__ import annotations

from .. import cards, joinpending, menu, persist, tools
from ..menu import PanelCtx, Screen
from ..permissions import Role
from ..util import esc

SCOPE_KO = {"all": "전원", "korean": "한국인 이름만", "foreign": "외국·랜덤 이름(가라)만"}
DONE_NOTE = "요청한 관리자가 눌러야 실행돼요. 아직 안 했으니 '했다'고 말하지 말 것."


async def _ask_card(svc, bot, uid: int, first_name: str, chat_id: int, approve: bool, scope: str) -> str:
    """전원 승인/거절 확인 카드를 방에 올린다. 돌려주는 글은 AI·명령이 그대로 쓸 안내."""
    total, ko, non = await joinpending.count(svc, chat_id)
    if total == 0:
        return ("이 방에 쌓인 입장요청이 없음 (이 기능 배포 이후 들어온 신청만 쌓임 — 예전에 밀린 건 관리자 명단 조회가 1번 필요). "
                "그렇게 안내할 것.")
    n = len(await joinpending.pending(svc, chat_id, scope))
    if n == 0:
        return f"그 조건({SCOPE_KO.get(scope, scope)})에 맞는 입장요청이 없음. 전체는 {total}명이야. 그렇게 안내할 것."
    verb = "승인" if approve else "거절"
    title = f"📥 쌓인 입장요청 중 {SCOPE_KO.get(scope, scope)} {n}명을 한 번에 {verb}"
    body = ("\n봇이 하나씩 처리해요 (관리자 승격·외부 접속 필요 없어요)." if approve
            else "\n가라(외국·랜덤 이름) 정리용이에요. 거절은 밴이 아니라 다시 신청할 수 있어요.")
    kb = await cards.card(svc, uid, chat_id, "join_requests", "jreq_ok", "jreq_no",
                          {"action": "approve" if approve else "decline", "scope": scope}, ok_label=f"✅ {verb}")
    await bot.send_message(chat_id, f"<b>{title}</b> 할까요?{body}\n(요청한 {esc(first_name)}님만 누를 수 있어요)",
                           parse_mode="HTML", reply_markup=kb)
    return "확인 버튼을 보냈음. " + DONE_NOTE


# ── AI 도구 ────────────────────────────────────────────────
async def t_join_requests(ctx: tools.ToolCtx, a: dict) -> str:
    action = (a.get("action") or "count").lower()
    scope = (a.get("scope") or "all").lower()
    if scope not in SCOPE_KO:
        scope = "all"
    if action == "count":
        total, ko, non = await joinpending.count(ctx.svc, ctx.chat_id)
        if total == 0:
            return ("이 방에 쌓인 입장요청이 없음 (이 기능 배포 이후 들어온 신청만 쌓여요). 그렇게 안내할 것.")
        return (f"쌓인 입장요청 {total}명 (한국인 이름 {ko} · 외국·랜덤 {non}). "
                "전원 승인은 '입요 전부 승인', 가라만 거절은 '외국 입요 거절' 로 말하면 확인 버튼을 띄움. 그렇게 안내할 것.")
    if action not in ("approve", "decline"):
        return "action 은 count·approve·decline 중 하나."
    return await _ask_card(ctx.svc, ctx.bot, ctx.caller.id, ctx.caller.first_name, ctx.chat_id,
                           action == "approve", scope)


# ── 확인 카드 처리 ──────────────────────────────────────────
async def _run_bulk(svc, bot, cid: int, uid: int, approve: bool, scope: str) -> None:
    ok, fail = await joinpending.bulk(svc, bot, cid, approve, scope)
    verb = "승인" if approve else "거절"
    msg = f"📥 입장요청 {verb} 완료 — {ok}명" + (f" (실패 {fail}명 · 봇에 '구성원 추가' 권한 확인)" if fail else "")
    try:
        await bot.send_message(cid, msg)
    except Exception:
        pass
    try:
        await svc.db.log_mod(cid, uid, None, "join_bulk", f"{verb} {ok}명" + (f"/실패{fail}" if fail else ""))
    except Exception:
        pass


async def jreq_ok(c: PanelCtx, spec) -> Screen:
    if not isinstance(spec, dict) or spec.get("action") not in ("approve", "decline"):
        return Screen(None, toast="만료된 카드예요.", alert=True)
    if not await c.svc.perms.is_tg_admin(c.bot, c.cid, c.uid):   # 누를 때 다시 확인
        if await cards.claim(c.svc, spec, "no"):
            await cards.pressed(c.svc, c.cid, c.uid, "join_requests", spec, "⚠️ 입장요청 처리 못 함 (관리자 아님)", done=False)
        return Screen("⚠️ 방 관리자만 할 수 있어요.", None, toast="권한이 없어요", alert=True)
    if not await cards.claim(c.svc, spec, "ok"):
        return Screen(None, toast=cards.ALREADY)
    approve = spec["action"] == "approve"
    scope = spec.get("scope", "all")
    verb = "승인" if approve else "거절"
    persist.spawn(_run_bulk(c.svc, c.bot, c.cid, c.uid, approve, scope))
    await cards.pressed(c.svc, c.cid, c.uid, "join_requests", spec, f"✅ 입장요청 전원 {verb} 시작", done=True)
    return Screen(f"📥 입장요청을 {verb}하는 중이에요. 끝나면 방에 알려드릴게요.", None, toast="처리 시작")


async def jreq_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY)
    await cards.pressed(c.svc, c.cid, c.uid, "join_requests", spec, "❌ 입장요청 처리 취소", done=False)
    return Screen("그대로 뒀어요.", None)


# ── 명령 ──────────────────────────────────────────────────
async def c_joinreq(ctx) -> None:
    total, ko, non = await joinpending.count(ctx.svc, ctx.chat_id)
    if total == 0:
        await ctx.reply("📥 쌓인 입장요청이 없어요. (이 기능 켜진 뒤 들어온 신청부터 쌓여요)")
        return
    await ctx.reply(f"📥 쌓인 입장요청 <b>{total}명</b> (한국인 이름 {ko} · 외국·랜덤 {non})\n"
                    "전원 승인: <code>.입요승인</code> · 가라만 거절: <code>.입요거절</code>")


async def c_joinreq_approve(ctx) -> None:
    await ctx.reply(await _ask_card(ctx.svc, ctx.bot, ctx.user.id, ctx.user.first_name, ctx.chat_id, True, "all"))


async def c_joinreq_decline(ctx) -> None:
    # 기본은 가라(외국·랜덤)만 거절해 한국인 보존. '.입요거절 전체' 면 전원.
    scope = "all" if (ctx.argstr or "").strip() in ("전체", "전원", "all") else "foreign"
    await ctx.reply(await _ask_card(ctx.svc, ctx.bot, ctx.user.id, ctx.user.first_name, ctx.chat_id, False, scope))


def register_commands() -> None:
    """handlers 가 import 뒤 부름 (commands→menu→panels 순환 피함)."""
    from .. import commands
    if any(c.fn is c_joinreq for c in commands.COMMANDS):
        return
    specs = [
        (("입요", "입장요청"), c_joinreq, "쌓인 가입 신청 수"),
        (("입요승인", "입요전원승인", "입장요청승인"), c_joinreq_approve, "쌓인 가입 신청 전원 승인 (확인 버튼)"),
        (("입요거절", "입장요청거절", "가라거절"), c_joinreq_decline, "쌓인 가입 신청 거절 (기본 가라만 · '전체' 붙이면 전원)"),
    ]
    for names, fn, help_ in specs:
        cmd = commands.Cmd(names, fn, Role.ADMIN, help=help_, group="관리")
        commands.COMMANDS.append(cmd)
        for name in cmd.names:
            commands._INDEX.setdefault(name.lower(), cmd)


# ── 등록 ──────────────────────────────────────────────────
tools.register_tool(tools.Tool(
    "join_requests",
    "[관리자] 이 방에 쌓인 가입 신청(입장요청)을 한 번에 — action=count(몇 명인지 바로) · approve(전원 승인) · decline(거절). "
    "scope=all(전체·기본)·korean(한국인 이름만)·foreign(외국·랜덤 이름=가라만). '입요 전부 받아줘/승인'=approve all, "
    "'가라 입요 거절·외국인 신청만 거절'=decline foreign, '입요 몇 개야'=count. approve·decline 은 확인 버튼 한 장. "
    "명단은 이 기능 배포 이후 봇이 받은 신청만 (관리자 승격·외부 접속 없이 봇이 바로 처리).",
    {"action": {"type": "string", "enum": ["count", "approve", "decline"]},
     "scope": {"type": "string", "enum": ["all", "korean", "foreign"]}},
    ["action"], t_join_requests, Role.ADMIN, where="room"))

menu.register_token_action("jreq_ok", jreq_ok, fresh=True)
menu.register_token_action("jreq_no", jreq_no)
