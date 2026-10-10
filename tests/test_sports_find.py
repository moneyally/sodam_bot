"""🔎 경기 찾기 — 한글 이름 번역·배운 이름·부분 맞춤·후보 버튼·한국 시각 (2026-10-11 벳블리 '생테티엔 로데즈 골 나오면 알려줘' 4번 실패:
AI 가 한국어만 넘김 → 도구가 영어 이름 경기를 못 찾음 → 웹 검색이 현지 시각 18시를 '예정'으로 오보). python tests/run_all.py sports_find"""
from types import SimpleNamespace

from fakes import fake_user, runner
from test_sports import Game
from test_sports_watch import ADMIN, KICK, MEMBER, ROOM, ask, setup

import sodam.panels  # noqa: F401
from sodam import tools
from sodam.panels import sportsdm as D
from sodam.permissions import Role
from sodam.sports import ui as sports_ui

test, run_all = runner()


def ligue2(state="in", key="espn:88"):
    return Game(key, "world", KICK, "AS Saint-Étienne", "Rodez AF", 0 if state == "in" else None, 1 if state == "in" else None,
                state, "37'" if state == "in" else "", (), "20261010", title="French Ligue 2")


class LLM:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    async def json(self, system, user, **kw):
        self.calls += 1
        return self.answer


@test
async def korean_only_names_are_translated_found_and_remembered():
    e = await setup(KICK + 600)
    e.src.games["world"] = [ligue2()]
    e.svc.llm = LLM({"생테티엔": "Saint-Etienne", "로데즈": "Rodez"})
    out = await ask(e, MEMBER, Role.MEMBER, ROOM, query="생테티엔 로데즈")
    assert "이 방에" in out and (await e.sp.alerts.watches(ROOM))[0]["game"] == "espn:88", out
    assert e.svc.llm.calls == 1
    games = await sports_ui.UI(e.sp).find_games("생테티엔 경기")          # 다음엔 배운 이름으로 AI 없이
    assert [g.key for g in games] == ["espn:88"] and e.svc.llm.calls == 1


@test
async def wrong_translation_is_not_remembered_and_extra_words_are_tolerated():
    e = await setup(KICK + 600)
    e.src.games["world"] = [ligue2()]
    e.svc.llm = LLM({"생테티엔": "Marseille"})
    ui = sports_ui.UI(e.sp)
    assert not await ui.find_games("생테티엔", translate=tools._team_translator(SimpleNamespace(svc=e.svc)))
    assert not await e.db._all("SELECT * FROM sports_alias"), "틀린 번역은 안 남김"
    e.svc.llm = LLM({"생테티엔": "Saint-Etienne", "로데즈": "Rodez", "프랑스": "France"})
    games = await ui.find_games("프랑스 리그2 생테티엔 로데즈", translate=tools._team_translator(SimpleNamespace(svc=e.svc)))
    assert [g.key for g in games] == ["espn:88"], "4낱말 중 하나(프랑스)가 안 맞아도"


@test
async def live_question_about_unknown_team_answers_from_data_not_web_search():
    e = await setup(KICK + 600)
    e.src.games["world"] = [ligue2()]
    e.svc.llm = LLM({"생테티엔": "Saint-Etienne"})
    ctx = tools.ToolCtx(e.svc, e.bot, ROOM, MEMBER, Role.MEMBER, {})
    out = await tools.t_sports(ctx, {"action": "live", "query": "생테티엔"})
    assert "French Ligue 2" in out and "37'" in out and "web_search" not in out, out
    e.src.games["world"] = []
    out = await tools.t_sports(ctx, {"action": "alert", "query": "없는팀이름"})
    assert "web_search" not in out, "알림 걸기 실패에 웹 검색으로 일정 추측 X"


@test
async def several_matches_give_buttons_only_the_asker_can_press():
    e = await setup()
    a = Game("espn:91", "nhl", KICK, "Boston Bruins", "Toronto Maple Leafs", src="20261010")
    b = Game("espn:92", "nhl", KICK + 3600, "Boston Bruins", "Florida Panthers", src="20261010")
    e.src.games["nhl"] = [a, b]
    out = await ask(e, ADMIN, Role.ADMIN, ROOM, query="NHL 보스턴")
    assert "버튼으로" in out and not await e.sp.alerts.watches(ROOM), out
    [card] = [c for c in e.bot.named("send_message") if c[1] == ROOM and "어느 경기" in c[2]]
    rows = card[3]["reply_markup"].inline_keyboard
    assert len(rows) == 2 and rows[0][0].callback_data.startswith(f"sgw:r:{ADMIN.id}:g:nhl:espn:9")
    box = []

    async def answer(t=None, show_alert=False):
        box.append(t)

    async def edit(text, **kw):
        box.append(("edit", text))
    q = SimpleNamespace(from_user=fake_user(99, "남"), message=SimpleNamespace(chat=SimpleNamespace(id=ROOM)), answer=answer,
                        edit_message_text=edit)
    await D.on_choice(e.svc, e.bot, q, rows[1][0].callback_data.split(":")[1:])
    assert "부탁한 사람만" in box[0] and not await e.sp.alerts.watches(ROOM)
    q.from_user = ADMIN
    await D.on_choice(e.svc, e.bot, q, rows[1][0].callback_data.split(":")[1:])
    [w] = await e.sp.alerts.watches(ROOM)
    assert w["game"] == "espn:92" and any(isinstance(x, tuple) and "🔔" in x[1] for x in box)


if __name__ == "__main__":
    run_all()
