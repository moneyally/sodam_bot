"""📝 AI 방 안내 (sodam/ai_instructions.py): 방 관리자(텔레그램 관리자) = 이 방, 오너 = 모든 방 공통.

그룹 허브 📝 AI 방 안내 m:ain:<방>  ✏️ m:in:<방>:ain → 글 → 미리보기 + [✅ 저장](토큰 ain_save) · 🗑 m:aind → m:ainx · 👁 m:ainv
오너 메인 📝 전체 AI 안내 m:aio      ✏️ m:aioi → 글(입력 aio) → 미리보기 + [✅ 저장](토큰 aio_save) · 🗑 m:aiod → m:aiox
AI 도구 set_room_instructions(text): 방에 확인 카드 (cards.card, 요청자만·텔레그램 관리자·확인 생략 없음) → 누르면 저장.
저장 전 매번 ai_instructions.check (길이·인젝션·링크), 저장은 mod_log 'ai_instructions' + 캐시 비움.
"""
from __future__ import annotations

from telegram import Message

from .. import ai_instructions as AI, cards, menu, tools
from ..menu import OWNER, TG_ADMIN, B, HubItem, PanelCtx, Route, Screen, _back, _kb
from ..permissions import Role
from ..services import PendingInput
from ..util import esc, fmt_time

GUIDE = ("소담이 <b>어떻게 말할지</b> 정해요: 캐릭터·말투·호칭(예: '사장님'이라고 부르기)·어떤 방인지·피할 화제·답 길이.\n"
         "가격·규칙 같은 <b>사실</b>은 🤖 AI → 📚 학습 자료에 넣어 주세요. 봇 기본 안전 규칙보다 우선하지는 않아요.")
EXAMPLE = "예: <code>이 방은 카페 사장님 모임이에요. 멤버를 '사장님'이라고 부르고, 정치 얘기엔 끼지 마세요. 답은 두 문장 안으로.</code>"
NOT_OWNER = Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)
DRAFT_TTL = 600


def _quote(text: str) -> str:
    return f"<blockquote>{esc(text)}</blockquote>"


async def _current(c: PanelCtx, scope: str) -> str:
    row = await AI.get(c.svc.db, 0 if scope == "owner" else c.cid, scope)
    if not row:
        return "<b>지금 안내</b>: 없음 (기본 캐릭터 — 업체 대표 소통방, '대표님' 호칭)"
    by = await c.svc.db.first_name(row["by"]) if row["by"] else None
    return (f"<b>지금 안내</b> ({len(row['text'])}/{AI.cap(scope)}자 · {fmt_time(row['ts'], c.svc.cfg.tz)}"
            + (f" · {esc(by)}" if by else "") + f"):\n{_quote(row['text'])}")


# ── 방 (텔레그램 관리자) ───────────────────────────────────
async def s_ain(c: PanelCtx) -> Screen:
    owner = await AI.get(c.svc.db, 0, "owner")
    lines = ["📝 <b>AI 방 안내</b>", GUIDE, "", await _current(c, "room")]
    if owner:
        lines.append("(운영자가 정한 모든 방 공통 안내도 먼저 들어가요)")
    has = await AI.get(c.svc.db, c.cid, "room")
    rows = [[B("✏️ 새로 쓰기", f"m:in:{c.cid}:ain")] + ([B("🗑 지우기", f"m:aind:{c.cid}")] if has else []),
            [B("👁 AI 에 들어가는 모양", f"m:ainv:{c.cid}")], _back(c.cid)]
    return Screen("\n".join(lines), _kb(rows))


async def s_ain_view(c: PanelCtx) -> Screen:
    row = await AI.get(c.svc.db, c.cid, "room")
    owner = await AI.get(c.svc.db, 0, "owner")
    block = AI.render("(운영자 공통 안내 — 여기선 안 보여요)" if owner else "", row["text"] if row else "")
    text = ("👁 <b>AI 에 이렇게 들어가요</b> (말투 설정 다음, 대화 앞)\n" + f"<pre>{esc(block)}</pre>" if block
            else "👁 지금은 안내가 없어서 기본 캐릭터로 답해요.")
    return Screen(text, _kb([[B("⬅️ 뒤로", f"m:ain:{c.cid}")]]))


async def s_ain_ask_delete(c: PanelCtx) -> Screen:
    if not await AI.get(c.svc.db, c.cid, "room"):
        return await s_ain(c)
    return Screen("🗑 이 방 AI 안내를 지울까요? 기본 캐릭터로 돌아가요.",
                  _kb([[B("🗑 지우기", f"m:ainx:{c.cid}"), B("❌ 취소", f"m:ain:{c.cid}")]]))


async def r_ain_delete(c: PanelCtx) -> Screen:
    had = await AI.get(c.svc.db, c.cid, "room")
    if had:
        await AI.save(c.svc.db, c.cid, "room", "", c.uid)
    screen = await s_ain(c)
    screen.toast = "지웠어요." if had else "이미 없어요."
    return screen


def _preview(scope: str, text: str, tok: str, back: str, redo: str) -> Screen:
    block = AI.render(text, "") if scope == "owner" else AI.render("", text)
    return Screen("📝 <b>미리보기</b> — 저장하면 AI 에 이렇게 들어가요:\n" + f"<pre>{esc(block)}</pre>\n"
                  "[✅ 저장]을 눌러야 적용돼요 (10분).",
                  _kb([[B("✅ 저장", f"m:k:{tok}"), B("✏️ 다시 쓰기", redo)], [B("❌ 취소", back)]]))


async def _input(c: PanelCtx, msg: Message, scope: str) -> tuple[bool, str]:
    text = AI.clean(msg.text or "")
    err = AI.check(text, scope)
    if err:
        return False, esc(err)
    c.draft = text   # 다음 화면(미리보기)이 씀
    return True, "📝 받았어요."


async def i_room(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    return await _input(c, msg, "room")


async def s_after_room(c: PanelCtx) -> Screen:
    text = getattr(c, "draft", None)
    if not text:
        return await s_ain(c)
    tok = menu.token(c.svc, c.uid, c.cid, "ain_save", {"text": text}, DRAFT_TTL)
    return _preview("room", text, tok, f"m:ain:{c.cid}", f"m:in:{c.cid}:ain")


async def _save(c: PanelCtx, scope: str, spec) -> tuple[bool, str]:
    """토큰·카드 저장 공통: 누를 때 다시 검사 (그 사이 규칙이 바뀌었을 수 있음)."""
    text = AI.clean(str((spec or {}).get("text", "")))
    err = AI.check(text, scope)
    if err:
        return False, err
    await AI.save(c.svc.db, c.cid, scope, text, c.uid)
    return True, ""


async def t_ain_save(c: PanelCtx, spec) -> Screen:
    ok, err = await _save(c, "room", spec)
    screen = await s_ain(c)
    screen.toast, screen.alert = ("저장했어요. 다음 답부터 적용돼요.", False) if ok else (err[:190], True)
    return screen


# ── 오너: 모든 방 공통 ────────────────────────────────────
async def s_aio(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():   # 라우터 확인과 이중으로
        return NOT_OWNER
    lines = ["📝 <b>전체 AI 안내</b> (모든 방·1:1 공통, 방 관리자 안내보다 먼저)", GUIDE, EXAMPLE, "",
             await _current(c, "owner")]
    has = await AI.get(c.svc.db, 0, "owner")
    rows = [[B("✏️ 새로 쓰기", "m:aioi")] + ([B("🗑 지우기", "m:aiod")] if has else []), [B("⬅️ 처음으로", "m:home")]]
    return Screen("\n".join(lines), _kb(rows))


async def r_aio_input(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():
        return NOT_OWNER
    c.svc.inputs[c.uid] = PendingInput("aio", 0)
    if c.svc.announcer:   # 1:1 입력 흐름은 하나만
        c.svc.announcer.drafts.pop((c.uid, c.uid), None)
    return Screen(f"📝 모든 방에 공통으로 넣을 <b>AI 안내</b>를 보내주세요 ({AI.OWNER_MAX}자까지, 지금 안내는 통째로 바뀌어요).\n"
                  f"{EXAMPLE}\n\n5분 안에 보내주세요. 그만두려면 <code>취소</code>", _kb([[B("❌ 취소", "m:aio")]]))


async def i_owner(c: PanelCtx, msg: Message) -> tuple[bool, str]:
    if c.uid not in await c.svc.perms.owners():
        return True, "봇 오너만 쓸 수 있어요."
    return await _input(c, msg, "owner")


async def s_after_owner(c: PanelCtx) -> Screen:
    text = getattr(c, "draft", None)
    if not text or c.uid not in await c.svc.perms.owners():
        return Screen("", _kb([[B("⬅️ 전체 AI 안내", "m:aio")]]))
    tok = menu.token(c.svc, c.uid, 0, "aio_save", {"text": text}, DRAFT_TTL)
    return _preview("owner", text, tok, "m:aio", "m:aioi")


async def t_aio_save(c: PanelCtx, spec) -> Screen:
    ok, err = await _save(c, "owner", spec)
    screen = await s_aio(c)
    screen.toast, screen.alert = ("저장했어요. 모든 방의 다음 답부터 적용돼요.", False) if ok else (err[:190], True)
    return screen


async def s_aio_ask_delete(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():
        return NOT_OWNER
    return Screen("🗑 모든 방 공통 AI 안내를 지울까요?", _kb([[B("🗑 지우기", "m:aiox"), B("❌ 취소", "m:aio")]]))


async def r_aio_delete(c: PanelCtx) -> Screen:
    if c.uid not in await c.svc.perms.owners():
        return NOT_OWNER
    had = await AI.get(c.svc.db, 0, "owner")
    if had:
        await AI.save(c.svc.db, 0, "owner", "", c.uid)
    screen = await s_aio(c)
    screen.toast = "지웠어요." if had else "이미 없어요."
    return screen


# ── AI 도구: 말로 방 안내 바꾸기 (확인 카드) ───────────────
async def t_set_room_instructions(ctx: tools.ToolCtx, a: dict) -> str:
    text = AI.clean(str(a.get("text", "")))
    if not await ctx.svc.perms.is_tg_admin(ctx.bot, ctx.chat_id, ctx.caller.id):
        return "방 AI 안내는 텔레그램 관리자만 바꿀 수 있음 (권한). 그렇게 짧게 안내할 것."
    err = AI.check(text, "room")
    if err:
        return f"확인 버튼을 보내지 않았음: {err} 그대로 짧게 안내할 것."
    kb = await cards.card(ctx.svc, ctx.caller.id, ctx.chat_id, "set_room_instructions", "ain_ai", "ain_ai_no",
                          {"text": text}, ok_label="✅ 적용")
    await ctx.bot.send_message(
        ctx.chat_id, f"📝 이 방 AI 안내를 이렇게 바꿀까요? (지금 안내는 통째로 바뀌어요)\n{_quote(text)}"
                     f"(요청한 {esc(ctx.caller.first_name)}님만 누를 수 있어요)",
        parse_mode="HTML", reply_markup=kb)
    return "확인 버튼을 보냈음. 요청한 관리자가 눌러야 적용된다고 짧게 안내할 것. 아직 바뀐 게 아니니 '했다'고 말하지 말 것."


async def t_ain_ai(c: PanelCtx, spec) -> Screen:
    """방 확인 카드 [✅ 적용] (요청한 텔레그램 관리자만, 카드당 한 번, 누를 때 다시 검사)."""
    if not await cards.claim(c.svc, spec, "ok"):
        return Screen(None, toast=cards.ALREADY)
    ok, err = await _save(c, "room", spec)
    await cards.pressed(c.svc, c.cid, c.uid, "set_room_instructions", spec,
                        "✅ 방 AI 안내 바꿈" if ok else f"⚠️ 방 AI 안내 못 바꿈 ({err[:40]})", done=ok)
    if not ok:
        return Screen(f"📝 바꾸지 않았어요. {esc(err)}", None, toast="바꾸지 않았어요", alert=True)
    return Screen(f"📝 이 방 AI 안내를 바꿨어요. 다음 답부터 적용돼요.\n{_quote(AI.clean(spec['text']))}"
                  "(보기·지우기: 관리자 1:1 메뉴 📝 AI 방 안내)", None, toast="바꿨어요")


async def t_ain_ai_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY)
    await cards.pressed(c.svc, c.cid, c.uid, "set_room_instructions", spec, "❌ 방 AI 안내 변경 취소", done=False)
    return Screen("📝 바꾸지 않았어요.", None)


# ── 등록 ──────────────────────────────────────────────────
menu.register_hub(HubItem(41, "ain", "📝 AI 방 안내", TG_ADMIN))
menu.register_screen("ain", s_ain, need=TG_ADMIN)
menu.register_route("ainv", Route(s_ain_view, TG_ADMIN))
menu.register_route("aind", Route(s_ain_ask_delete, TG_ADMIN))
menu.register_route("ainx", Route(r_ain_delete, TG_ADMIN, fresh=True))
menu.register_input("ain", f"📝 이 방 <b>AI 안내</b>를 보내주세요 ({AI.ROOM_MAX}자까지, 지금 안내는 통째로 바뀌어요).\n{EXAMPLE}",
                    "ain", i_room, s_after_room, need=TG_ADMIN)
menu.register_token_action("ain_save", t_ain_save, fresh=True, need=TG_ADMIN)

menu.register_main(61, "aio", "📝 전체 AI 안내", OWNER)
for _code, _fn in (("aio", s_aio), ("aioi", r_aio_input), ("aiod", s_aio_ask_delete), ("aiox", r_aio_delete)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
menu.register_input("aio", "", "aio", i_owner, s_after_owner, need=OWNER)
menu.register_token_action("aio_save", t_aio_save, need=OWNER)

tools.register_tool(tools.Tool(
    "set_room_instructions",
    "[관리자] 이 방에서 소담이 어떻게 말할지(캐릭터·말투·호칭·어떤 방인지·피할 화제·답 길이)를 방 AI 안내로 정한다 "
    "(확인 버튼을 보냄, 지금 안내는 통째로 바뀜). '앞으로 우리 방에선 사장님이라고 불러', '답은 짧게 해' 같은 요청. "
    "가격·규칙 같은 사실은 save_room_rule, 한 사람 말투는 set_member_style.",
    {"text": {"type": "string", "description": f"안내 전문 ({AI.ROOM_MAX}자 이내, 말한 내용을 다듬기만, 없는 내용 추가 금지)"}},
    ["text"], t_set_room_instructions, Role.ADMIN, where="room"))
menu.register_token_action("ain_ai", t_ain_ai, fresh=True, need=TG_ADMIN)
menu.register_token_action("ain_ai_no", t_ain_ai_no)
