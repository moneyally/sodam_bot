"""멤버가 말로 하는 것 + 버그 (2026-09-30 멤버 말하기 감사 scratchpad nl_member.md):

- [중] 1:1 에서 방 기록 도구가 1:1 채팅을 '방'으로 셈 → '내 포인트 몇 점?' 에 '포인트 없음'·'메시지 0개' 를 사실처럼 답함.
- set_my_style 에 '기본'(방 기본 말투로) 없음 → '없는 말투' 뒤 엉뚱한 말투로 재시도.
- 기간 '어제' = 어제 0시부터 지금까지(오늘 포함) → '어제 몇 개?' 에 오늘 것까지 더한 숫자.
- ops_inbox 가 그룹방 멤버에게도 보임(늘 거절) · lookup_user 가 4글자↑ 영어 이름을 @아이디로만 봄.
- 새로: point_game(! 명령을 말로) · tag_alerts(태그 알림 켜기/끄기) · my_ids · 본인 경고 횟수.
"""
import json
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, add_member, fake_user, make_db, make_svc, runner

import sodam.opsdesk  # noqa: F401 — ops_inbox 등록
import sodam.panels  # noqa: F401 — 도구 등록
from sodam import prompt, stats, tagnotify, tools
from sodam.casino import basic, core
from sodam.panels import checkup as C
from sodam.panels import membertools as M
from sodam.permissions import Role
from sodam.tools import ToolCtx
from sodam.util import day_start

test, run_all = runner()
A, B = -100111, -100222
ME, OTHER, ADMIN = 501, 502, 1
basic.DICE_WAIT = 0


class DiceBot(FakeBot):
    username = "sodam_ai_bot"

    def __init__(self, values=(), **kw):
        super().__init__(**kw)
        self.values = list(values)

    async def send_dice(self, chat_id, emoji="🎲", **kw):
        v = self.values.pop(0)
        self.calls.append(("send_dice", chat_id, emoji, v))
        return SimpleNamespace(message_id=1, dice=SimpleNamespace(value=v, emoji=emoji))


async def world(values=(), **cfg_kw):
    db = await make_db()
    svc = await make_svc(db, admins=(ADMIN,), **cfg_kw)
    await db.ensure_chat(A, "벳블리 소통방")
    await db.ensure_chat(B, "두번째방")
    await add_member(db, A, fake_user(ME, "영희"))
    await add_member(db, A, fake_user(OTHER, "철수"))
    core._last_bet.clear()
    return db, svc, DiceBot(values, admins=[fake_user(ADMIN, "관리")])


def ctx(svc, bot, chat, uid=ME, role=Role.MEMBER, name="영희", msg=None, settings=None):
    return ToolCtx(svc, bot, chat, fake_user(uid, name), role, settings or {}, request_msg=msg)


async def run(c, name, args=None):
    if not c.settings:
        c.settings = await c.svc.db.get_settings(c.chat_id)
    return await tools.execute(name, json.dumps(args or {}, ensure_ascii=False), c)


# ── 1. 1:1 에서 방 기록 도구 ─────────────────────────────
@test
async def dm_points_use_my_room_not_the_dm_itself():
    db, svc, bot = await world()
    await db._write("UPDATE members SET points=4200 WHERE chat_id=? AND user_id=?", (A, ME))
    await db._write("INSERT INTO messages(chat_id,user_id,msg_id,text,ts) VALUES(?,?,?,?,?)", (A, ME, 1, "안녕", day_start(svc.cfg.tz) + 1))
    c = ctx(svc, bot, ME)
    out = await run(c, "member_info", {"name": "영희"})
    assert "4200" in out and "벳블리" in out and c.tainted, out            # 1:1 이 아니라 영희가 있는 방 기준
    out = await run(ctx(svc, bot, ME), "points_ranking")
    assert "4200" in out and "아직 포인트" not in out, out
    out = await run(ctx(svc, bot, ME), "chat_stats", {"period": "오늘"})
    assert "메시지 1개" in out and "벳블리" in out, out
    await db.set_setting(A, "rules", "광고 금지")
    assert "광고 금지" in await run(ctx(svc, bot, ME), "room_rules")
    out = await run(ctx(svc, bot, ME), "read_chat", {"hours": 24})
    assert "안녕" in out, out


@test
async def dm_without_any_group_says_ask_in_group_not_zero():
    db, svc, bot = await world()
    stranger = 777
    for name, args in (("member_info", {"name": "나"}), ("points_ranking", {}), ("chat_stats", {}),
                       ("search_chat", {"keyword": "안녕"}), ("read_chat", {}), ("room_rules", {})):
        out = await run(ctx(svc, bot, stranger, uid=stranger), name, args)
        assert "그룹방에서" in out and "메시지 0개" not in out and "아직 포인트" not in out, (name, out)
        assert tools.RETRY_HINT not in out, "다시 시도할 일이 아님 (방이 없음)"


@test
async def dm_with_two_groups_asks_which_then_uses_named_room():
    db, svc, bot = await world()
    await add_member(db, B, fake_user(ME, "영희"))
    await db._write("UPDATE members SET points=900 WHERE chat_id=? AND user_id=?", (B, ME))
    out = await run(ctx(svc, bot, ME), "points_ranking")
    assert "어느 방" in out and "두번째방" in out and "벳블리" in out, out
    out = await run(ctx(svc, bot, ME), "points_ranking", {"room": "두번째"})
    assert "900" in out and "두번째방" in out, out
    assert "못 찾음" in await run(ctx(svc, bot, ME), "points_ranking", {"room": "없는방"})


@test
async def dm_skips_rooms_i_left_and_group_ignores_room_arg():
    db, svc, bot = await world()
    await add_member(db, B, fake_user(ME, "영희"))
    await db._write("INSERT INTO member_left(chat_id, user_id, ts) VALUES(?,?,?)", (B, ME, 1))
    out = await run(ctx(svc, bot, ME), "chat_stats")
    assert "벳블리" in out and "어느 방" not in out, out                     # 나간 방은 후보에서 빠져 한 방뿐
    await db._write("UPDATE members SET points=4200 WHERE chat_id=? AND user_id=?", (A, ME))
    room = ctx(svc, bot, A)
    out = await run(room, "points_ranking", {"room": "두번째방"})             # 그룹방에선 room 무시 = 이 방
    assert "4200" in out and not room.tainted, out


# ── 2. 말투 '기본' ────────────────────────────────────────
@test
async def set_my_style_default_resets_to_room_style():
    db, svc, bot = await world()
    c = ctx(svc, bot, A)
    assert "기본" in tools._BY_NAME["set_my_style"].params["style"]["enum"]
    await run(c, "set_my_style", {"style": "츤데레"})
    assert (await db.get_member(A, ME))["style"]
    out = await run(ctx(svc, bot, A), "set_my_style", {"style": "기본"})
    assert (await db.get_member(A, ME))["style"] is None and "방 기본" in out and "없는 말투" not in out, out


@test
async def one_members_count_and_rank_even_outside_the_top_five():
    """2026-10-09 18:57 실제: '소담아 이분 채팅집계' → 도구가 상위 5명만 줘서 소담이 'IDK님 0개'로 지어냄 (실제론 수십 개)."""
    db, svc, bot = await world()
    t0 = day_start(svc.cfg.tz) + 10
    n = 0
    for i, cnt in enumerate((9, 8, 7, 6, 5, 4)):                    # 6명 — 마지막(4개)은 상위 5 밖
        uid = 900 + i
        await add_member(db, A, fake_user(uid, f"사람{i}"))
        for _ in range(cnt):
            n += 1
            await db._write("INSERT INTO messages(chat_id,user_id,msg_id,text,ts) VALUES(?,?,?,?,?)", (A, uid, n, "말", t0 + n))
    out = await run(ctx(svc, bot, A), "chat_stats", {"period": "오늘"})
    assert "사람5" not in out and "0개가 아님" in out, out
    out = await run(ctx(svc, bot, A), "chat_stats", {"period": "오늘", "name": "사람5"})
    assert "사람5" in out and "4개" in out and "6위" in out and "6명" in out, out
    out = await run(ctx(svc, bot, A, uid=903, name="사람3"), "chat_stats", {"name": "나"})
    assert "6개" in out and "4위" in out, out
    out = await run(ctx(svc, bot, A), "chat_stats", {"name": "영희"})
    assert "0개" in out, out                                       # 진짜 0 은 0 (기록 기준)


# ── 3. '어제' = 어제 하루만 ───────────────────────────────
@test
async def yesterday_counts_only_yesterday():
    db, svc, bot = await world()
    tz = svc.cfg.tz
    today0, yday0 = day_start(tz), day_start(tz, 1)
    rows = [(A, ME, "어제 첫", yday0), (A, ME, "어제 끝", today0 - 1), (A, OTHER, "오늘", today0 + 1),
            (A, OTHER, "그제", yday0 - 1), (A, ME, "오늘 0시 정각", today0)]   # 끝(오늘 0시)은 안 들어감
    for cid, uid, text, ts in rows:
        await db._write("INSERT INTO messages(chat_id,user_id,msg_id,text,ts) VALUES(?,?,?,?,?)", (cid, uid, 1, text, ts))
        await db._write("INSERT INTO requests(chat_id,user_id,text,ts) VALUES(?,?,?,?)", (cid, uid, text, ts))
    out = await run(ctx(svc, bot, A), "chat_stats", {"period": "어제"})
    assert "메시지 2개" in out and "참여 1명" in out and "어제 방 통계" in out and "철수" not in out, out
    rank = await stats.ranking_text(db, A, tz, "어제")                      # .랭킹 어제 도 같은 집계
    assert "영희 — 2개" in rank and "철수" not in rank, rank
    assert "메시지 5개" in await stats.summary_text(db, A, tz, "주간")       # 다른 기간은 그대로 (지금까지)
    assert "메시지 2개" in await stats.summary_text(db, A, tz, "오늘")
    req = await run(ctx(svc, bot, A), "get_my_requests", {"period": "어제"})
    assert "요청 2건" in req, req


# ── 4. ops_inbox 는 그룹방 멤버에게 안 보임 ───────────────
@test
async def ops_inbox_hidden_for_room_member_only():
    names = lambda role, dm: {t.name for t in tools.available(role, {}, dm)}   # noqa: E731
    assert "ops_inbox" not in names(Role.MEMBER, False)
    assert "ops_inbox" in names(Role.ADMIN, False) and "ops_inbox" in names(Role.OWNER, False)
    assert "ops_inbox" in names(Role.MEMBER, True), "1:1 은 역할이 늘 member — 도구가 TG 관리자인지 직접 확인"
    db, svc, bot = await world()
    assert "권한 없음" in await run(ctx(svc, bot, A), "ops_inbox")


# ── 5. lookup_user: 영어 이름 ─────────────────────────────
@test
async def english_name_without_at_falls_back_to_name_search():
    db, svc, bot = await world()
    await add_member(db, A, fake_user(900, "Major"))
    for chat in (ME, A):   # 1:1 · 그룹방
        uid, note = await C._resolve_who(ctx(svc, bot, chat), "Major")
        assert uid == 900, (chat, note)
    uid, note = await C._resolve_who(ctx(svc, bot, ME), "major")
    assert uid == 900, note
    uid, note = await C._resolve_who(ctx(svc, bot, ME), "@Major")            # @ 을 붙이면 아이디로만
    assert uid is None and "본 적 없는 아이디" in note
    await add_member(db, A, fake_user(901, "누군가", "Ghost"))
    assert (await C._resolve_who(ctx(svc, bot, ME), "Ghost"))[0] == 901     # 아이디가 맞으면 여전히 아이디 먼저


# ── 6. 포인트 게임을 말로 ─────────────────────────────────
def gmsg(uid=ME, text="소담아 게임"):
    return FakeMsg(A, fake_user(uid, "영희"), text, message_id=77)


async def play(svc, bot, game, amount="", pick="", uid=ME, **kw):
    core._last_bet.clear()
    m = gmsg(uid)
    c = ctx(svc, bot, A, uid=uid, msg=m, **kw)
    out = await run(c, "point_game", {"game": game, "amount": amount, "pick": pick})
    return out, m, c


@test
async def point_game_join_daily_and_bet_like_commands():
    db, svc, bot = await world(values=[3])
    out, m, c = await play(svc, bot, "출석")
    assert "가입" in out and not c.quiet and not m.replies, out               # 가입 전: 이유만 AI 에게, 멋대로 가입 X
    assert not await core.account(db, A, ME)
    out, m, c = await play(svc, bot, "가입")
    assert c.quiet and "가입 완료" in m.replies[-1] and await core.balance(db, A, ME) == 10_000, out
    out, m, c = await play(svc, bot, "출석")
    assert "출석 완료" in m.replies[-1] and await core.balance(db, A, ME) == 15_000
    assert "숫자를 다시 말하지" in out
    out, m, c = await play(svc, bot, "출석")
    assert "이미 출석" in m.replies[-1] and await core.balance(db, A, ME) == 15_000   # 쿨다운 그대로
    out, m, c = await play(svc, bot, "홀짝", "1,000P", "홀")                  # 단위·쉼표 붙어도 ! 명령 금액
    assert await core.balance(db, A, ME) == 15_950 and bot.named("send_dice"), out
    s = (await db._one("SELECT COALESCE(SUM(delta),0) AS s FROM casino_ledger WHERE chat_id=? AND user_id=?", (A, ME)))["s"]
    assert s == 15_950


@test
async def point_game_same_limits_as_commands():
    db, svc, bot = await world(values=[1] * 5)
    await play(svc, bot, "가입")
    out, m, c = await play(svc, bot, "홀짝", "200000", "홀")
    assert "최대 베팅" in m.replies[-1] and await core.balance(db, A, ME) == 10_000
    out, m, c = await play(svc, bot, "홀짝", "50", "홀")
    assert "최소 베팅" in m.replies[-1]
    await db.set_setting(A, "casino_enabled", False)
    out, m, c = await play(svc, bot, "홀짝", "1000", "홀")
    assert "꺼져" in out and not m.replies and not c.quiet and "10분에 한 번" not in out, out
    await db.set_setting(A, "casino_enabled", True)
    await db.set_setting(A, "games_enabled", False)
    out, m, c = await play(svc, bot, "채굴")
    assert "꺼져" in out and not m.replies
    assert await core.balance(db, A, ME) == 10_000 and not bot.named("send_dice")


@test
async def point_game_one_per_answer_self_only_and_blocked_after_reading():
    db, svc, bot = await world()
    await play(svc, bot, "가입")
    m = gmsg()
    c = ctx(svc, bot, A, msg=m)
    await run(c, "point_game", {"game": "출석"})
    out = await run(c, "point_game", {"game": "채굴"})
    assert "한 판" in out and len(m.replies) == 1, out                     # 한 답변에 한 판
    assert "name" not in tools._BY_NAME["point_game"].params and "user" not in tools._BY_NAME["point_game"].params
    c = ctx(svc, bot, A, msg=gmsg())
    c.tainted = True
    assert "보안" in await run(c, "point_game", {"game": "채굴"})             # 기록 읽은 답변 → 포인트 안 움직임
    assert "point_game" not in {t.name for t in tools.available(Role.MEMBER, {}, True)}   # 1:1 엔 없음
    c = ctx(svc, bot, A, msg=None)                                          # 요청 메시지 없는 길(버튼 이어 하기)
    assert "그룹방 채팅" in await run(c, "point_game", {"game": "채굴"})


@test
async def point_game_main_role_leaves_casino_to_dealer_bot():
    db, svc, bot = await world(bot_role="main")
    out, m, c = await play(svc, bot, "가입")
    assert "딜러 봇" in out and "!가입" in out and not m.replies and not await core.account(db, A, ME), out


@test
async def point_game_counts_for_long_play_alert():
    db, svc, bot = await world()
    await db.set_setting(A, "gt_enabled", True)
    await play(svc, bot, "가입")
    assert await db._one("SELECT 1 FROM game_sessions WHERE chat_id=? AND user_id=?", (A, ME))


# ── 7. 태그 알림 말로 ─────────────────────────────────────
@test
async def tag_alerts_in_room_and_dm():
    db, svc, bot = await world()
    await add_member(db, B, fake_user(ME, "영희"))
    out = await run(ctx(svc, bot, A), "tag_alerts", {"action": "off"})
    assert await tagnotify.opted_out(db, ME, A) and not await tagnotify.opted_out(db, ME, B), out
    assert "꺼짐" in out
    await run(ctx(svc, bot, ME), "tag_alerts", {"action": "off"})              # 1:1, 방 없음 = 전부
    assert await tagnotify.opted_out(db, ME, B)
    out = await run(ctx(svc, bot, ME), "tag_alerts", {"action": "on", "room": "두번째"})
    assert not await tagnotify.opted_out(db, ME, B) and await tagnotify.opted_out(db, ME, A), out
    await db.set_setting(B, "tag_notify", False)
    out = await run(ctx(svc, bot, ME), "tag_alerts", {"action": "status"})
    assert "방 전체 태그 알림을 꺼 둬서" in out and "벳블리" in out, out
    assert not await tagnotify.opted_out(db, OTHER, A)                       # 남의 설정은 그대로
    out = await run(ctx(svc, bot, A, uid=OTHER, name="철수"), "tag_alerts", {"action": "on"})
    assert "/start" in out, "1:1 을 시작 안 한 사람에겐 알림이 안 간다는 안내"


# ── 8. 본인 경고 · ID ─────────────────────────────────────
@test
async def member_info_shows_own_warnings_only():
    db, svc, bot = await world()
    await db.add_warning(A, ME, ADMIN, "도배")
    await db.add_warning(A, OTHER, ADMIN, "도배")
    assert "경고: 1회" in await run(ctx(svc, bot, A), "member_info", {"name": "영희"})
    assert "경고" not in await run(ctx(svc, bot, A), "member_info", {"name": "철수"})
    assert "경고: 1회" in await run(ctx(svc, bot, A, uid=ADMIN, role=Role.ADMIN, name="관리"), "member_info", {"name": "철수"})


@test
async def my_ids_room_and_dm():
    db, svc, bot = await world()
    out = await run(ctx(svc, bot, A), "my_ids")
    assert str(ME) in out and str(A) in out
    out = await run(ctx(svc, bot, ME), "my_ids")
    assert str(ME) in out and "그 그룹방" in out
    assert "my_ids" in tools.READ_ONLY and "point_game" not in tools.READ_ONLY and "tag_alerts" not in tools.READ_ONLY


@test
async def prompt_and_tool_text_point_to_point_game():
    assert "point_game" in prompt.SYSTEM and "0개·0점" in prompt.SYSTEM
    assert "point_game" in tools._BY_NAME["start_game"].description
    assert {"출석", "채굴", "지갑", "가입", "파산", "홀짝", "주사위", "슬롯", "룰렛", "바카라", "블랙잭", "경마", "그래프"} <= set(M.GAMES)


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
