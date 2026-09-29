"""📘 소담 공식 안내서 + sodam_guide (실제 사례 2026-09-29 벳블리 '결제하면 얼마야?' → '가격 자료가 없어')."""
import re
from types import SimpleNamespace

from fakes import FakeBot, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import prompt, tools
from sodam.panels import guidebook as G
from sodam.permissions import Role
from sodam.tools import ToolCtx

test, run_all = runner()
CHAT = -100555


def ctx(svc, bot, chat):
    return ToolCtx(svc, bot, chat, fake_user(5, "테드정"), Role.MEMBER, {})


@test
async def guide_docs_are_linked_and_render_config_values():
    docs = G.load()
    assert {"pricing", "invite", "features", "modes", "voice", "lookup", "faq"} <= set(docs)
    for k, d in docs.items():
        assert d["title"] and d["tags"], k
        assert all(r in docs for r in d["related"]), (k, d["related"])      # 그래프 끊긴 곳 없음
    cfg = SimpleNamespace(sub_price_usdt="30", sub_days=30, trial_days=3)
    for d in docs.values():
        assert not re.search(r"\{\w+\}", G.render(d["body"], cfg)), d["title"]
    body = G.render(docs["pricing"]["body"], cfg)
    assert "30일(1달) 30 USDT" in body and "구독하기" in body and "3일 무료" in body


@test
async def questions_find_the_right_topic():
    assert G.find("소담아 근데 너 결제하면 얼마야?") == "pricing"
    assert G.find("우리방에도 데려가려면 어떻게 해") == "invite"
    assert G.find("욕 받아치기 모드 어떻게 켜") == "modes"
    assert G.find("modes") == "modes"


@test
async def tool_answers_price_from_config_and_sends_buttons_once_in_room_only():
    db = await make_db()
    svc = await make_svc(db, sub_price_usdt="30", sub_days=30, trial_days=3)
    bot = FakeBot()
    G._card_sent.clear()
    out = await G.t_sodam_guide(ctx(svc, bot, CHAT), {"topic": "결제하면 얼마야"})
    assert "30 USDT" in out and "버튼 카드" in out, out
    [(_, cid, _text, kw)] = bot.named("send_message")
    urls = [b.url for row in kw["reply_markup"].inline_keyboard for b in row]
    assert cid == CHAT and any("startgroup" in u for u in urls) and any(f"start=sub_{CHAT}" in u for u in urls)
    await G.t_sodam_guide(ctx(svc, bot, CHAT), {"topic": "pricing"})
    assert len(bot.named("send_message")) == 1, "같은 방 10분 안엔 카드 1번"
    await G.t_sodam_guide(ctx(svc, bot, 5), {"topic": "pricing"})
    assert len(bot.named("send_message")) == 1, "1:1 엔 카드 안 보냄"
    assert "sodam_guide" in {t.name for t in tools.available(Role.MEMBER, {}, False)} and "sodam_guide" in tools.READ_ONLY
    assert "sodam_guide" in prompt.SYSTEM


if __name__ == "__main__":
    run_all()
