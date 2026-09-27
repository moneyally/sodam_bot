"""하네스 데이터: ❓ 되묻기 버튼 (panels/askchoice.py) — 방에 뜬 질문 카드의 버튼을 역할마다 눌러 봄.

열린 질문(멤버가 요청, 질문·보기에 HTML 글자) · 시간 지난 질문 · 관리자가 요청한 질문.
요청자가 아닌 역할은 '요청한 사람만', 요청자는 ✏️ 직접 입력 → 보기 → 이미 고름 순서로 눌림 (AI 없음 = 다시 실행 안 함).
"""
import time

import harness
from fakes import FakeBot, fake_user

from sodam.panels import askchoice
from sodam.permissions import Role
from sodam.tools import ToolCtx


async def _ask(svc, bot, uid, role, question, options):
    before = len(bot.calls)
    ctx = ToolCtx(svc, bot, harness.CHAT, fake_user(uid, harness.PERSONAS[uid]), role, await svc.db.get_settings(harness.CHAT))
    assert await askchoice.t_ask_choice(ctx, {"question": question, "options": options}) == askchoice.SENT
    [card] = [c for c in bot.calls[before:] if c[0] == "send_message"]
    kb = card[3]["reply_markup"]
    rows = kb.inline_keyboard
    kb = type(kb)([rows[-1], *rows[:-1]])   # ✏️ 먼저 눌러 보게 (보기를 먼저 누르면 질문이 닫힘)
    return kb


async def seed(svc):
    bot = FakeBot()
    harness.add_buttons(await _ask(svc, bot, harness.MEMBER, Role.MEMBER, "<b>어느</b> 방 & 기준?", ["<i>소통방</i>", "a&b"]))
    harness.add_buttons(await _ask(svc, bot, harness.TG, Role.ADMIN, "뮤트 얼마나 할까요?", ["1시간", "1일", "7일"]))
    old = await _ask(svc, bot, harness.MEMBER, Role.MEMBER, "지난 질문", ["예", "아니오"])
    await svc.db._write("UPDATE ask_choices SET expires=? WHERE question='지난 질문'", (time.time() - 5,))
    harness.add_buttons(old)


harness.SEEDERS.append(seed)
