"""🎮 30명 동시 게임 '렉/밀림' 버그 수정 — python tests/run_all.py fix_game_lag

2026-11-25 클린뱅크(난이도80, 150명) 바카라 난사 → 봇 전체 멈춤 감사로 찾은 버그들.
#1 tilt 표본추출(바카라 8덱 슈 400번)이 이벤트 루프에서 직접 돌아 봇 전체 정지 → 스레드로.
#2 '글만' 모드(casino_game_visual=text)가 블랙잭·하이로우·경마·회차판엔 안 먹히던 것 → 게이트 추가.
#5 두 사람 첫 베팅이 거의 동시면 멀티 판이 2개 열리던 레이스 → style 을 current() 전에 읽어 await 없앰.
(#3 '단판은 과부하 가드 없다'는 오진이었음 — 단판도 전부 take_bet 을 거쳐 _overloaded 로 이미 막힘.
 overloaded/normal 테스트는 그 기존 take_bet 가드가 단판에도 걸리는지 지키는 회귀 테스트.)"""
import asyncio
import threading

import test_bactable as B
import test_casino_cards as K
import test_casino_multi as TM
from fake_llm import Room
from fakes import runner

from sodam import ratelimit
from sodam.casino import cards as C
from sodam.casino import core, multi
from sodam.casino.multi import current

test, run_all = runner()


async def world(pct):
    r = await Room().open(admins=())
    await r.db.set_state(0, core.LOSE_KEY, pct)     # 전체 난이도
    core._lose.update(pct=0, at=-1e9)               # 30초 캐시 리셋 → 다시 읽게
    core._room_pct.clear()
    return r


@test
async def tilt_samples_off_the_event_loop():
    """난이도>0 이면 표본추출(무거움)이 메인 스레드가 아니라 워커 스레드에서 돌아야 (봇 전체 안 멈추게)."""
    r = await world(80)
    main = threading.get_ident()
    seen = []

    def draw():
        seen.append(threading.get_ident())
        return "W"                                  # 항상 '이김' → tilt_pick 이 표본추출 경로로

    await core.tilt(r.db, draw, lambda x: False, Room.CHAT)
    assert seen, "draw 가 불렸어야"
    assert all(t != main for t in seen), "표본추출이 메인 스레드에서 돌면 봇 전체가 멈춤 — 스레드로 빠져야"


@test
async def tilt_fair_room_draws_once_no_sampling():
    """난이도 0(공정)이면 표본추출 없이 draw 한 번만 (무거운 바카라 슈를 400번 안 깜)."""
    r = await world(0)
    cnt = [0]

    def draw():
        cnt[0] += 1
        return "W"

    out = await core.tilt(r.db, draw, lambda x: False, Room.CHAT)
    assert out == "W" and cnt[0] == 1, cnt


# ── #2 글만 모드(casino_game_visual=text)가 블랙잭·하이로우에도 먹혀야 ──────────
@test
async def text_mode_blackjack_sends_no_photo():
    """오너가 '글만'으로 껐으면 블랙잭·하이로우도 카드 사진 안 나가야 (예전엔 룰렛·바카라만 먹혔음)."""
    svc, bot, (u,) = await K.setup()
    C.IMAGES = True                                        # 사진 경로 켜고 (setup 은 끔)
    await svc.db.set_setting(K.CHAT, "casino_game_visual", "text")
    K.fix("♠5", "♠9", "♥3", "♥4", "♣2", "♣6")
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    media = [c for c in bot.calls if c[0] in ("send_photo", "send_animation", "edit_message_media")]
    assert not media, f"글만 모드인데 미디어가 나감: {[c[0] for c in media]}"
    C.IMAGES = False


@test
async def text_mode_horse_sends_no_gif():
    """글만 모드면 경마(멀티)도 결과 GIF 안 나가야 — multi.send_anim 이 막는다
    (경마만 send_anim 을 탄다; 그래프는 다른 경로)."""
    env = await TM.setup(n_users=1)
    await env.svc.db.set_setting(TM.CHAT, "casino_game_visual", "text")
    (a,) = env.users
    await TM.say(env, a, "!경마 1000 1")
    await TM.finish(env, "horse")
    gifs = env.bot.named("send_animation")
    assert not gifs, f"글만 모드인데 경마 GIF 가 나감: {len(gifs)}"
    TM.restore()


@test
async def text_mode_bactable_sends_no_photo():
    """글만 모드면 바카라 회차판도 카드 사진·공개 수정 없이 글자로 — bactable.open_send 가 막는다."""
    env = await B.env_with_photos()
    await env.svc.db.set_setting(-1009001, "casino_game_visual", "text")
    a = env.users[0]
    opened = await TM.say(env, a, "!회차 1000 플")
    assert opened and "바카라" in opened[0], "글만 모드면 판 여는 것도 글자로"
    r = current(-1009001, "bactable")
    r.rd = B.BANKER_WIN
    r.seal("고정")
    await TM.finish(env, "bactable")
    assert not env.bot.named("send_photo"), "글만 모드인데 바카라 회차 사진이 나감"
    assert not env.bot.media, "글만 모드인데 사진 수정(공개)이 나감"
    TM.restore()


@test
async def full_mode_blackjack_does_send_photo():
    """대비: full(기본)이면 사진이 나가야 — 위 text 테스트가 '사진 억제'를 진짜 검사한다는 보장."""
    svc, bot, (u,) = await K.setup()
    C.IMAGES = True
    await svc.db.set_setting(K.CHAT, "casino_game_visual", "full")
    K.fix("♠5", "♠9", "♥3", "♥4", "♣2", "♣6")
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    assert any(c[0] == "send_photo" for c in bot.calls), "full 모드면 카드 사진이 나가야 (gate 가 의미있음)"
    C.IMAGES = False


# ── (#3 회귀) 밀린 방: 단판 베팅 게임도 take_bet 의 _overloaded 로 이미 거절된다 ─────────
@test
async def overloaded_room_declines_single_bet_game():
    """방에 글이 잔뜩 밀렸으면(backlog≥OVERLOAD_AT) 단판 베팅 게임(블랙잭)도 돈 안 빼고 거절돼야.
    단판도 전부 take_bet 을 거치므로 take_bet 맨 앞의 _overloaded 가 멀티와 똑같이 막는다 — 그걸 지키는 회귀."""
    svc, bot, (u,) = await K.setup()
    rl = ratelimit.ChatRateLimiter()
    rl.backlog = lambda cid: core.OVERLOAD_AT + 5      # 방이 꽉 밀린 상황
    bot.rate_limiter = rl
    before = await core.balance(svc.db, K.CHAT, u.id)
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    after = await core.balance(svc.db, K.CHAT, u.id)
    assert after == before, f"밀린 방인데 단판 베팅이 빠짐 (가드 없음): {before}→{after}"


@test
async def normal_room_runs_single_bet_game():
    """대비: 안 밀린 방이면 단판 게임은 그대로 돌아가야 (가드가 평소 플레이를 막지 않음)."""
    svc, bot, (u,) = await K.setup()
    rl = ratelimit.ChatRateLimiter()
    rl.backlog = lambda cid: 0
    bot.rate_limiter = rl
    before = await core.balance(svc.db, K.CHAT, u.id)
    await K.cmd(svc, bot, u, "!블랙잭 1000")
    after = await core.balance(svc.db, K.CHAT, u.id)
    assert after != before, "안 밀린 방인데 게임이 안 돌았음 (가드가 과하게 막음)"


# ── #5 동시 첫 베팅이 멀티 판을 2개 열던 레이스 ────────────────────────────────
@test
async def concurrent_first_bets_open_one_round():
    """두 사람이 거의 동시에 첫 베팅(!그래프) → 판 1개에 둘 다 들어가야 (예전엔 판 2개 개설).

    버그는 `current()==None` 확인부터 `_start`(판 등록)까지 **await 가 끼면** 재현된다 (그 틈에 두 번째
    사람이 current()==None 을 또 봐서 같은 게임 판이 2개 열리고, 나중 _start 가 _ROUNDS 를 덮어써 한 명만 남음).
    두 _join 이 get_settings 마다 서로 만나는 '랑데부 게이트'로 락스텝을 만들어, style 을 current() 뒤로 옮기는
    뮤테이션이면 둘 다 current()==None 을 지난 뒤 _start 가 겹치도록 결정적으로 몰아준다. 첫 _start 때 게이트를
    꺼서 판의 run()·다른 get_settings 는 영향 없음."""
    env = await TM.setup(n_users=2)
    orig = env.svc.db.get_settings
    orig_start = multi._start
    gate = {"ev": asyncio.Event(), "n": 0, "off": False}

    def stop_gate(r):                   # 판이 하나라도 등록되면 게이트 끔 (run() 등은 자유롭게)
        gate["off"] = True
        return orig_start(r)

    async def paired_get(cid):          # 두 _join 이 get_settings 마다 짝지어 만나게 (락스텝)
        if not gate["off"]:
            gate["n"] += 1
            if gate["n"] % 2 == 0:      # 짝이 도착 → 기다리던 쪽 깨우고 새 약속
                gate["ev"].set()
                gate["ev"] = asyncio.Event()
            else:                       # 먼저 온 쪽은 짝을 기다림 (짝 없으면 1초 뒤 그냥 진행)
                try:
                    await asyncio.wait_for(gate["ev"].wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
        return await orig(cid)

    env.svc.db.get_settings = paired_get
    multi._start = stop_gate
    u0, u1 = env.users
    try:
        await asyncio.gather(TM.say(env, u0, "!그래프 1000"), TM.say(env, u1, "!그래프 1000"))
        r = multi.current(TM.CHAT, "crash")
        n = 0 if r is None else len(r.players)
        assert n == 2, f"동시 첫 베팅이 판을 2개 열면 current() 엔 한 명만 남음 — players={n}"
    finally:
        env.svc.db.get_settings = orig
        multi._start = orig_start
        env.clock.go.set()
        tasks = [rr.task for rr in multi._ROUNDS.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)   # 취소가 닫힌 DB 건드리기 전에 가라앉힘
        multi._ROUNDS.clear()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
