"""포인트 게임 뼈대·기본 게임: python tests/test_casino_core.py"""
import asyncio
import sys
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, fake_user, make_db, make_svc, runner

from sodam import casino, handlers
from sodam.casino import basic, core

test, run_all = runner()
CHAT = -1005550000001
basic.DICE_WAIT = 0


class DiceBot(FakeBot):
    username = "sodam_ai_bot"

    def __init__(self, values=()):
        super().__init__()
        self.values = list(values)

    async def send_dice(self, chat_id, emoji="🎲", **kw):
        v = self.values.pop(0)
        self.calls.append(("send_dice", chat_id, emoji, v))
        return SimpleNamespace(message_id=1, dice=SimpleNamespace(value=v, emoji=emoji))


async def setup(values=()):
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "테스트방")
    await db.set_setting(CHAT, "cas_enabled", False)
    bot = DiceBot(values)
    ctx = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                          bot_data={"svc": svc, "chats": set(), "cas_seen": set(), "tasks": set(), "joins": {}})
    core._last_bet.clear()
    return db, svc, bot, ctx


async def say(ctx, u, text):
    core._last_bet.clear()  # 테스트에선 2초 간격 제한 끔 (따로 검사)
    m = FakeMsg(CHAT, u, text)
    m.chat = SimpleNamespace(id=CHAT, title="테스트방", type="supergroup")
    m.sender_chat = None
    await handlers.on_group_message(SimpleNamespace(message=m), ctx)
    await asyncio.gather(*ctx.bot_data["tasks"], return_exceptions=True)
    return m.replies[-1] if m.replies else ""


A, B = fake_user(5550001003, "회원A", "member_a"), fake_user(5550001002, "메인관리자")


async def ledger_ok(db, uid):
    """원장 합계 == 잔액 (포인트는 원장을 거쳐서만 움직임)."""
    s = (await db._one("SELECT COALESCE(SUM(delta),0) AS s FROM casino_ledger WHERE chat_id=? AND user_id=?", (CHAT, uid)))["s"]
    return s == await core.balance(db, CHAT, uid)


@test
async def must_join_first_and_join_once():
    db, svc, bot, ctx = await setup()
    assert "!가입" in await say(ctx, A, "!채굴")
    r = await say(ctx, A, "!가입")
    assert "가입 완료" in r and "10,000P" in r
    assert "이미 가입" in await say(ctx, A, "!가입")
    assert await core.balance(db, CHAT, A.id) == 10_000 and await ledger_ok(db, A.id)
    assert "순위" in await say(ctx, B, "!순위")                           # 가입 없이 볼 수 있는 명령


@test
async def mine_cooldown_streak_and_no_double():
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    r = await say(ctx, A, "!채굴")
    assert "채굴 성공" in r
    assert "곡괭이 식는 중" in await say(ctx, A, "!채굴")
    bal = await core.balance(db, CHAT, A.id)
    assert 10_000 + core.MINE_MIN <= bal <= 10_000 + core.MINE_MAX * 10
    await db._write("UPDATE casino_accounts SET last_mine=last_mine-? WHERE user_id=?", (core.MINE_COOLDOWN, A.id))
    assert "연속 2회" in await say(ctx, A, "!채굴")                        # 1시간 안에 다시 → 연속 보너스
    assert await ledger_ok(db, A.id)


@test
async def daily_and_bailout_once_per_day():
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    assert "출석 완료" in await say(ctx, A, "!출석")
    assert "이미 출석" in await say(ctx, A, "!출석")
    assert "미만일 때만" in await say(ctx, A, "!파산")
    await db._write("UPDATE members SET points=50 WHERE user_id=?", (A.id,))
    await db._write("INSERT INTO casino_ledger(chat_id,user_id,delta,reason,ts) VALUES(?,?,?,?,0)",
                    (CHAT, A.id, 50 - 15_000, "test"))
    assert "지원금" in await say(ctx, A, "!파산")
    await db._write("UPDATE members SET points=50 WHERE user_id=?", (A.id,))
    await db._write("INSERT INTO casino_ledger(chat_id,user_id,delta,reason,ts) VALUES(?,?,?,?,0)",
                    (CHAT, A.id, -3000, "test"))
    assert "하루 한 번" in await say(ctx, A, "!파산")
    assert await ledger_ok(db, A.id)


@test
async def concurrent_join_and_mine_pay_once():
    db, svc, bot, ctx = await setup()
    from sodam import casino as C

    def mk(text):
        m = FakeMsg(CHAT, A, text)
        m.chat, m.sender_chat = SimpleNamespace(id=CHAT, title="S", type="supergroup"), None
        return m
    await asyncio.gather(*(C.dispatch(svc, bot, mk("!가입"), CHAT, A, 0, "!가입") for _ in range(5)))
    assert await core.balance(db, CHAT, A.id) == core.START_POINTS      # 5번 동시에 눌러도 한 번만
    await asyncio.gather(*(C.dispatch(svc, bot, mk("!채굴"), CHAT, A, 0, "!채굴") for _ in range(5)))
    n = (await db._one("SELECT COUNT(*) AS n FROM casino_ledger WHERE user_id=? AND reason='mine'", (A.id,)))["n"]
    assert n == 1, n                                                    # 채굴도 한 번만
    assert await ledger_ok(db, A.id)


@test
async def amount_parsing():
    assert core.parse_amount("1000", 5) == 1000 and core.parse_amount("5천", 0) == 5000
    assert core.parse_amount("3만", 0) == 30000 and core.parse_amount("1k", 0) == 1000
    assert core.parse_amount("올인", 777) == 777 and core.parse_amount("반", 777) == 388
    assert core.parse_amount("1,000", 0) == 1000
    for bad in ("홀", "-5", "1.5", "²", "9" * 13, ""):
        assert core.parse_amount(bad, 100) is None, bad
    assert core.split_bet(["홀", "1000"], 0) == (1000, ["홀"])


@test
async def oddeven_win_and_lose_with_real_dice_value():
    db, svc, bot, ctx = await setup(values=[3, 4])
    await say(ctx, A, "!가입")
    r = await say(ctx, A, "!홀짝 1000 홀")
    assert "3" in r and "+950P" in r and await core.balance(db, CHAT, A.id) == 10_950
    r = await say(ctx, A, "!홀짝 짝 1000")                                # 순서 바꿔도 됨
    assert "+950P" in r and await core.balance(db, CHAT, A.id) == 11_900
    assert bot.named("send_dice")[0][2] == "🎲" and await ledger_ok(db, A.id)


@test
async def bet_validation():
    db, svc, bot, ctx = await setup(values=[1] * 10)
    await say(ctx, A, "!가입")
    assert "최소 베팅" in await say(ctx, A, "!홀짝 50 홀")
    assert "잔액이 모자라요" in await say(ctx, A, "!홀짝 20000 홀")
    assert "최대 베팅" in await say(ctx, A, "!홀짝 200000 홀")
    assert "홀짝" in await say(ctx, A, "!홀짝 1000")                     # 선택 없음 → 사용법
    assert "홀짝" in await say(ctx, A, "!홀짝")                          # 인자 없음 → 사용법 + 잔액
    assert await core.balance(db, CHAT, A.id) == 10_000 and not bot.named("send_dice")
    m = FakeMsg(CHAT, A, "!홀짝 1000 홀")                                # 2초 간격 제한
    m.chat, m.sender_chat = SimpleNamespace(id=CHAT, title="S", type="supergroup"), None
    await handlers.on_group_message(SimpleNamespace(message=m), ctx)
    m2 = FakeMsg(CHAT, A, "!홀짝 1000 홀")
    m2.chat, m2.sender_chat = m.chat, None
    await handlers.on_group_message(SimpleNamespace(message=m2), ctx)
    assert "천천히" in m2.replies[-1]


@test
async def allin_twice_concurrently_never_negative():
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    r = await asyncio.gather(core.debit(db, CHAT, A.id, 10_000, "t"), core.debit(db, CHAT, A.id, 10_000, "t"))
    assert sorted(r) == [False, True] and await core.balance(db, CHAT, A.id) == 0


@test
async def slot_payouts_match_telegram_values():
    db, svc, bot, ctx = await setup(values=[64, 22, 2])
    await say(ctx, A, "!가입")
    r = await say(ctx, A, "!슬롯 1000")
    assert "잭팟" in r and "+29,000P" in r
    assert "+9,000P" in await say(ctx, A, "!슬롯 1000")
    assert "-1,000P" in await say(ctx, A, "!슬롯 1000")
    assert await ledger_ok(db, A.id)


@test
async def sports_emoji_games():
    db, svc, bot, ctx = await setup(values=[5, 1, 6, 3])
    await say(ctx, A, "!가입")
    assert "+1,400P" in await say(ctx, A, "!농구 1000")                   # 🏀 5 = 골
    assert "-1,000P" in await say(ctx, A, "!축구 1000")                   # ⚽ 1 = 노골
    assert "+4,700P" in await say(ctx, A, "!다트 1000")                   # 🎯 6 = 정중앙
    assert "-1,000P" in await say(ctx, A, "!볼링 1000")
    assert [c[2] for c in bot.named("send_dice")] == ["🏀", "⚽", "🎯", "🎳"]


@test
async def roulette_and_ladder_rules():
    assert basic.roulette_win(0, "빨강") == 0 and basic.roulette_win(0, "0") == 36
    assert basic.roulette_win(1, "빨강") == 2 and basic.roulette_win(2, "검정") == 2
    assert basic.roulette_win(19, "하이") == 2 and basic.roulette_win(18, "로우") == 2
    assert basic.ladder_payout(("좌", 3, "짝"), "좌") == 1.95 and basic.ladder_payout(("좌", 3, "짝"), "좌3짝") == 3.8
    assert basic.ladder_payout(("우", 4, "짝"), "홀") == 0
    db, svc, bot, ctx = await setup()
    await say(ctx, A, "!가입")
    r = await say(ctx, A, "!룰렛 1000 빨강")
    assert "🎡" in r and await ledger_ok(db, A.id)
    r = await say(ctx, A, "!사다리 1000 좌3짝")
    assert "🪜" in r and "도착" in r and await ledger_ok(db, A.id)


@test
async def rtp_simulation_all_basic_games():
    """기대 환급률(RTP): 모든 게임이 0.90~1.00 (오래 하면 조금씩 잃음 → 채굴·출석이 의미 있음)."""
    import random
    r = random.Random(7)
    N = 40_000

    def rtp(fn):
        return sum(fn() for _ in range(N)) / N
    results = {
        "홀짝": rtp(lambda: 1.95 if r.randint(1, 6) % 2 == 1 else 0),
        "주사위 숫자": rtp(lambda: 5.7 if r.randint(1, 6) == 6 else 0),
        "주사위 높낮이": rtp(lambda: 1.95 if r.randint(1, 6) >= 4 else 0),
        "슬롯": rtp(lambda: (lambda reels: basic.SLOT_PAY[reels[0]] if len(set(reels)) == 1 else 0)(
            basic.slot_reels(r.randint(1, 64)))),
        "룰렛 색": rtp(lambda: basic.roulette_win(r.randrange(37), "빨강")),
        "룰렛 숫자": rtp(lambda: basic.roulette_win(r.randrange(37), "7")),
        "사다리": rtp(lambda: basic.ladder_payout(basic.LADDER[r.randrange(4)], "좌")),
        "사다리 조합": rtp(lambda: basic.ladder_payout(basic.LADDER[r.randrange(4)], "우4짝")),
    }
    for name, (emoji, wins, mult, _) in basic.SPORTS.items():
        n_faces = 6 if emoji in ("🎯", "🎳") else 5
        results[name] = rtp(lambda: mult if r.randint(1, n_faces) in wins else 0)
    bad = {k: round(v, 3) for k, v in results.items() if not 0.88 <= v <= 1.0}
    assert not bad, bad


@test
async def disabled_room_and_dm():
    db, svc, bot, ctx = await setup()
    await db.set_setting(CHAT, "casino_enabled", False)
    assert "꺼져" in await say(ctx, A, "!가입")
    m = FakeMsg(A.id, A, "!가입")
    ctx2 = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), bot_data=ctx.bot_data)
    handled = await casino.dispatch(svc, bot, m, A.id, A, 0, "!가입")
    assert handled and "그룹방" in m.replies[0]
    del ctx2


@test
async def repeated_game_commands_are_not_spam():
    db, svc, bot, ctx = await setup(values=[1] * 12)
    await db.set_setting(CHAT, "dup_limit", 2)                          # 엄격한 방 설정에서도
    await db.set_setting(CHAT, "flood_count", 4)
    await say(ctx, A, "!가입")
    for _ in range(8):
        assert "🎲" in await say(ctx, A, "!홀짝 100 짝")
    assert not bot.named("restrict") and not bot.named("delete")
    assert not [c for c in bot.named("send_message") if "반복" in c[2] or "도배" in c[2]]
    for _ in range(3):                                                  # 일반 잡담 반복은 여전히 잡힘
        await say(ctx, A, "ㅋㅋㅋㅋ 도배")
    assert [c for c in bot.named("send_message") if "반복" in c[2] or "도배" in c[2] or "경고" in c[2]]


@test
async def dealer_sodam_comments_in_room_style():
    db, svc, bot, ctx = await setup(values=[3, 3])
    await say(ctx, A, "!가입")
    r = await say(ctx, A, "!홀짝 1000 홀")
    assert "딜러 소담" in r and ("대표님" in r or "습니다" in r or "요" in r)
    await db.set_setting(CHAT, "style", "free")                         # 자유분방 방 → 반말 딜러
    r = await say(ctx, A, "!홀짝 1000 홀")
    line = r.split("딜러 소담</b>: ")[1]
    from sodam.casino import dealer
    assert line in dealer.LINES["free"]["win"] + dealer.LINES["free"]["jackpot"]


@test
async def bot_roles_split_main_and_dealer():
    import dataclasses
    db, svc, bot, ctx = await setup(values=[3])
    svc.cfg = dataclasses.replace(svc.cfg, bot_role="main")             # 메인 봇: ! 명령은 무시 (딜러 봇 몫)
    assert await say(ctx, A, "!가입") == "" and not await core.account(db, CHAT, A.id)
    svc.cfg = dataclasses.replace(svc.cfg, bot_role="dealer")           # 딜러 봇: ! 명령만
    m = FakeMsg(CHAT, A, "!가입")
    m.chat, m.sender_chat = SimpleNamespace(id=CHAT, title="테스트방", type="supergroup"), None
    await handlers.on_dealer_group(SimpleNamespace(message=m), ctx)
    assert "가입 완료" in m.replies[-1]
    m = FakeMsg(CHAT, A, "소담아 안녕")                                  # 딜러 봇은 일반 대화엔 반응 안 함
    m.chat, m.sender_chat = SimpleNamespace(id=CHAT, title="테스트방", type="supergroup"), None
    await handlers.on_dealer_group(SimpleNamespace(message=m), ctx)
    assert not m.replies


@test
async def unknown_bang_text_is_ignored():
    db, svc, bot, ctx = await setup()
    assert await say(ctx, A, "!!!") == "" and await say(ctx, A, "!ㅋㅋ") == ""


@test
async def help_lists_all_and_no_money_words():
    text = casino.help_text()
    for c in casino.COMMANDS:
        assert "!" + c.names[0] in text, c.names
    assert "원" not in text.replace("원하", "") and "₩" not in text and "환전" not in text
    names = [n for c in casino.COMMANDS for n in c.names]
    assert len(names) == len(set(names))


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
