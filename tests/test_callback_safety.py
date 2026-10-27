"""콜백 안전망: 어떤 버튼 핸들러가 무슨 예외로 터지든, 버튼이 '뱅뱅 돌며 씹히지' 않게
handlers.on_callback 이 스피너(q.answer)부터 끈다 (2026-10-27 버그 조사 — casino 등은 TelegramError 아닌
예외에 무방비였음). python tests/run_all.py callback_safety"""
from types import SimpleNamespace

from fakes import FakeBot, FakeQuery, fake_user, make_db, make_svc, runner

from sodam import handlers, hooks

test, run_all = runner()


class CountQuery(FakeQuery):
    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def _fire(svc, bot, data):
    q = CountQuery(-1001234, fake_user(5, "손님"), data=data)
    ctx = SimpleNamespace(bot=bot, bot_data={"svc": svc})
    await handlers.on_callback(SimpleNamespace(callback_query=q), ctx)
    return q


@test
async def non_telegram_crash_still_answers_and_reraises():
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot()

    async def boom(svc, bot, q, parts):
        raise KeyError("boom")   # TelegramError 가 아닌 예외 (casino 류 실제 함정)

    hooks.CALLBACK_HANDLERS["zzt"] = boom
    try:
        q = CountQuery(-1001234, fake_user(5, "손님"), data="zzt:x")
        ctx = SimpleNamespace(bot=bot, bot_data={"svc": svc})
        try:
            await handlers.on_callback(SimpleNamespace(callback_query=q), ctx)
            assert False, "예외가 re-raise 되어 on_error 로 가야 함"
        except KeyError:
            pass
        assert q.answers, "핸들러가 터져도 q.answer 로 스피너를 꺼야 (버튼 씹힘 방지)"
    finally:
        del hooks.CALLBACK_HANDLERS["zzt"]
    await db.close()


@test
async def normal_callback_still_works():
    db = await make_db()
    svc = await make_svc(db)
    bot = FakeBot()
    q = await _fire(svc, bot, "zzz_unknown")   # 모르는 prefix → else 분기 q.answer
    assert q.answers, q.answers
    await db.close()


if __name__ == "__main__":
    run_all()
