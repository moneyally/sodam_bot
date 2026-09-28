"""📚 말로 방 규칙·공지·가격·FAQ 저장 (AI 도구 save_room_rule → 확인 카드 → 📚 학습 자료).

기억 구분 (CLAUDE.md '기억과 자료'):
- 👤 멤버 기억(member_memory) = 멤버 본인 얘기 · 🏠 방 흐름 메모(room_memory) = 분위기·화제 요약 (memory.py)
- 📚 자료(knowledge_docs) = 방 규칙·공지·가격·FAQ ← 여기 · 📜 기록(mod_log·messages) = 있었던 일
관리자가 "우리 방에서는 광고 올릴 때 관리자에게 먼저 말해야 해, 기억해" 하면 멤버 기억이 아니라 방 자료로.

바로 저장하지 않고 방에 확인 카드(menu.lasting_token, 요청한 관리자만 · 30분 · 재시작 뒤에도 유효):
자료는 AI 가 모든 멤버에게 '등록된 자료를 보면' 하고 사실처럼 전하는 곳이라, 대화 속 숨은 지시나 잘못 알아들은 말이
조용히 방 규칙이 되면 안 됨 → schedule_task·alert_rule 과 같은 방식. 누르면 knowledge.add_document (📚 학습 자료에서 삭제 가능).
"""
from __future__ import annotations

from .. import cards, knowledge, menu, tools
from ..menu import PanelCtx, Screen
from ..permissions import Role
from ..security import scan
from ..util import esc

MAX_RULE_CHARS = 1500
MIN_RULE_CHARS = 10          # knowledge.add_document 최소 길이와 같게
TITLE_CHARS = 40
SOURCE = "AI 대화 (방 규칙)"


def default_title(text: str) -> str:
    first = " ".join(text.split())[:20]
    return f"방 규칙: {first}" + ("…" if len(" ".join(text.split())) > 20 else "")


async def t_save_room_rule(ctx: tools.ToolCtx, a: dict) -> str:
    text = str(a.get("text", "")).strip()[:MAX_RULE_CHARS]
    title = " ".join(str(a.get("title", "")).split())[:TITLE_CHARS] or default_title(text)
    if len(text) < MIN_RULE_CHARS:
        return "저장할 내용이 너무 짧음. 규칙·안내 문장을 그대로 한 번 더 말해 달라고 할 것."
    if not await ctx.svc.paid_features(ctx.chat_id):
        return "이 방은 이용 기간이 아니라 방 자료를 저장할 수 없음. 그렇게 짧게 안내할 것."
    if scan(text).blocked or scan(title).blocked:   # 봇 조종 문구는 방 자료로 만들지 않음
        return "지시문(봇 조종) 같은 문장이라 방 자료로 저장하지 않았음. 규칙 내용만 다시 말해 달라고 할 것."
    spec = {"title": title, "text": text}
    kb = await cards.card(ctx.svc, ctx.caller.id, ctx.chat_id, "save_room_rule", "kbr_save", "kbr_no", spec,
                          ok_label="✅ 저장")   # 확인 생략 없음 (자료는 모든 멤버에게 사실처럼 전해짐)
    await ctx.bot.send_message(
        ctx.chat_id, f"📚 방 자료로 저장할까요?\n제목: <b>{esc(title)}</b>\n내용: {esc(text[:300])}"
                     + ("…" if len(text) > 300 else "")
                     + f"\n(AI 가 질문에 답할 때 참고해요 · 요청한 {esc(ctx.caller.first_name)}님만 누를 수 있어요)",
        parse_mode="HTML", reply_markup=kb)
    return "확인 버튼을 보냈음. 요청한 관리자가 눌러야 방 자료로 저장된다고 짧게 안내할 것. 아직 저장된 게 아니니 '했다'고 말하지 말 것."


async def t_kbr_save(c: PanelCtx, spec) -> Screen:
    """확인 카드 [✅ 저장] (토큰 = 요청한 관리자만, 누를 때 관리자 다시 확인, 카드당 한 번)."""
    if not await cards.claim(c.svc, spec, "ok"):
        return Screen(None, toast=cards.ALREADY)
    title, text = str(spec.get("title", ""))[:TITLE_CHARS], str(spec.get("text", ""))[:MAX_RULE_CHARS]
    if not await c.svc.paid_features(c.cid):
        await cards.pressed(c.svc, c.cid, c.uid, "save_room_rule", spec, f"⚠️ 방 자료 '{title}' 저장 못 함 (이용 기간 아님)",
                            done=False)
        return Screen("이용 기간이 아니라 저장하지 못했어요.", None, toast="저장 못 함", alert=True)
    try:
        doc_id, _, _ = await knowledge.add_document(c.svc.db, c.cid, title, text, SOURCE, c.uid)
    except knowledge.KnowledgeError as e:
        await cards.pressed(c.svc, c.cid, c.uid, "save_room_rule", spec, f"⚠️ 방 자료 '{title}' 저장 못 함 ({e})", done=False)
        return Screen(f"❌ {esc(str(e))}", None, toast="저장 못 함", alert=True)
    await c.svc.db.log_mod(c.cid, c.uid, None, "knowledge_add", f"#{doc_id} {title}")
    await cards.pressed(c.svc, c.cid, c.uid, "save_room_rule", spec, f"✅ 방 자료 #{doc_id} '{title}' 저장됨", done=True)
    return Screen(f"✅ 방 자료 <code>#{doc_id}</code> <b>{esc(title)}</b> 저장했어요.\n"
                  "보기·삭제는 관리자 1:1 메뉴 🤖 AI → 📚 학습 자료에서", None, toast="저장했어요")


async def t_kbr_no(c: PanelCtx, spec) -> Screen:
    if not await cards.claim(c.svc, spec, "no"):
        return Screen(None, toast=cards.ALREADY)
    await cards.pressed(c.svc, c.cid, c.uid, "save_room_rule", spec, "❌ 방 자료 저장 취소", done=False)
    return Screen("방 자료로 저장하지 않았어요.", None)


TOOL = tools.Tool(
    "save_room_rule",
    "[관리자] 방 규칙·공지·가격·회비·운영 방식·자주 묻는 질문을 방 자료(📚)로 저장한다 (확인 버튼을 보냄). "
    "'우리 방에서는 광고 올릴 때 관리자에게 먼저 말해야 해, 기억해', '회비는 월 2만원이야 기억해 둬' 같은 요청. "
    "멤버 본인 얘기(호칭·업종·관심사)는 save_my_note — 여기에 넣지 않는다.",
    {"text": {"type": "string", "description": "저장할 규칙·안내 내용 그대로 (말한 사람의 문장을 다듬기만, 없는 내용 추가 금지)"},
     "title": {"type": "string", "description": "짧은 제목 (예: 광고 규칙, 회비 안내). 없으면 비움"}},
    ["text"], t_save_room_rule, Role.ADMIN, where="room")
tools.register_tool(TOOL)
menu.register_token_action("kbr_save", t_kbr_save, fresh=True)
menu.register_token_action("kbr_no", t_kbr_no)
