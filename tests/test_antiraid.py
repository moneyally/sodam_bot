"""가짜 계정·대량 입장 방어: python tests/run_all.py antiraid

① 스팸 명단 CAS + lols 동시 조회  ② 대량 입장 중 새 입장자 바로 내보내기  ③ 최근 만든 계정(ID 추정) 캡차
④ 가입 신청 1:1 그림 버튼 확인 → 자동 승인·거절, 통과자는 방 캡차·대량 입장 내보내기 생략.
본코드 경로(handlers.handle_new_member / on_join_request / on_callback / job 의 expire)를 가짜 텔레그램으로 돈다.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fake_llm import Room
from fakes import FakeQuery, fake_user, runner
from telegram import ChatPermissions

from sodam import accountage, free, handlers, joinreq, menu, raid
from sodam.cas import Cas

test, run_all = runner()

ADMIN = fake_user(1, "방장")
OLD_ID, NEW_ID = 100_000_000, 9_000_000_000        # 2015년 계정 · 기준 데이터(2025-11)보다 새 계정
MUTE = ChatPermissions.no_permissions()


async def room(**settings):
    return await Room().open(admins={1}, settings={"captcha_enabled": False, "greet_enabled": False, **settings})


def muted(r, uid):
    return [c for c in r.bot.named("restrict") if c[2] == uid and c[3] == MUTE]


# ── ① CAS + lols ──────────────────────────────────────────
@test
async def spam_list_cas_or_lols():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        host, uid = req.url.host, int(req.url.params.get("user_id") or req.url.params["id"])
        calls.append((host, uid))
        if uid == 3:
            return httpx.Response(500)
        if host == "cas.test":
            return httpx.Response(200, json={"ok": uid == 1})
        return httpx.Response(200, json={"ok": True, "user_id": uid, "banned": uid == 2})

    cas = Cas("https://cas.test/check", httpx.AsyncClient(transport=httpx.MockTransport(handler)),
              lols_api="https://lols.test/account")
    assert await cas.is_banned(1) and await cas.is_banned(2), "어느 한 곳이라도 등록이면 스팸"
    assert not await cas.is_banned(4)
    assert not await cas.is_banned(3), "조회 실패 = 등록 아님"
    await cas.is_banned(3)                                          # 실패는 캐시 안 함 → 다시 조회
    assert calls.count(("cas.test", 3)) == 2 and calls.count(("lols.test", 3)) == 2, calls
    await cas.is_banned(2)                                          # 등록은 캐시
    assert calls.count(("lols.test", 2)) == 1, calls
    await cas.close()


# ── ② 대량 입장 중 바로 내보내기 ──────────────────────────
@test
async def raid_kick_mode_kicks_newcomers_and_reports():
    r = await room(raid_count=3, raid_seconds=60, raid_action="kick")
    users = [fake_user(OLD_ID + i, f"입장{i}") for i in range(5)]
    for u in users:
        await handlers.handle_new_member(r.ctx, Room.CHAT, "방", u)
    kicked = {c[2] for c in r.bot.named("ban")}
    assert kicked == {u.id for u in users[2:]}, kicked            # 3번째에서 방어 시작 → 3·4·5번째 내보냄
    assert {c[2] for c in r.bot.named("unban")} == kicked, "밴이 아니라 내보내기(재입장 가능)"
    assert not muted(r, users[3].id), "내보낸 사람에겐 캡차 없음"
    assert any("입장할 수 없어요" in c[2] for c in r.bot.named("send_message") if c[1] == Room.CHAT)
    await raid.stop(r.svc, r.bot, Room.CHAT)
    dm = [c for c in r.bot.named("send_message") if c[1] == 1 and "방어 모드가 끝났어요" in c[2]]
    assert dm and "3명을 내보냈어요" in dm[0][2], dm


@test
async def same_choice_value_keeps_own_label_per_setting():
    """'kick' 이 캡차 실패(내보내기·재입장 가능)와 대량 입장(바로 내보내기)에서 서로 다른 글자로 보여야 함."""
    from sodam.settings import render
    assert render("captcha_action", "kick") == "내보내기(재입장 가능)"
    assert render("raid_action", "kick") == "바로 내보내기"
    r = await room()
    q = FakeQuery(1, ADMIN, "")
    await menu.on_callback(r.svc, r.bot, q, ["j", str(Room.CHAT)])
    assert "실패 시: 내보내기(재입장 가능)" in q.edits[-1], q.edits[-1]


@test
async def raid_captcha_mode_unchanged():
    r = await room(raid_count=3, raid_seconds=60)
    for i in range(4):
        await handlers.handle_new_member(r.ctx, Room.CHAT, "방", fake_user(OLD_ID + i, f"입장{i}"))
    assert not r.bot.named("ban") and muted(r, OLD_ID + 3), "기본(캡차)은 내보내지 않고 캡차"


# ── ③ 최근 만든 계정 ──────────────────────────────────────
@test
async def account_age_estimate():
    ids = accountage._IDS
    assert ids == sorted(ids) and [t for _, t in accountage._POINTS] == sorted(t for _, t in accountage._POINTS)
    i0, t0 = accountage._POINTS[10]
    assert accountage.estimate(i0) == (t0, False)                   # 기준점 그대로
    (a, ta), (b, tb) = accountage._POINTS[40], accountage._POINTS[41]
    ts, newer = accountage.estimate((a + b) // 2)
    assert not newer and abs(ts - (ta + tb) / 2) < 86400            # 가운데 = 두 날짜 가운데
    assert accountage.estimate(NEW_ID)[1] is True
    now = datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp()
    assert accountage.is_recent(NEW_ID, now=now) and not accountage.is_recent(OLD_ID, now=now)
    last_id, last_t = accountage._POINTS[-1]
    assert accountage.is_recent(last_id, days=30, now=last_t + 10 * 86400)   # 기준 안: 날짜로 판단
    assert not accountage.is_recent(last_id, days=30, now=last_t + 40 * 86400)


@test
async def recent_account_gets_captcha_even_if_off():
    r = await room()
    await handlers.handle_new_member(r.ctx, Room.CHAT, "방", fake_user(NEW_ID, "새계정"))
    await handlers.handle_new_member(r.ctx, Room.CHAT, "방", fake_user(OLD_ID, "옛계정"))
    assert muted(r, NEW_ID) and not muted(r, OLD_ID)
    r2 = await room(recent_account_captcha=False)
    await handlers.handle_new_member(r2.ctx, Room.CHAT, "방", fake_user(NEW_ID, "새계정"))
    assert not muted(r2, NEW_ID), "끄면 캡차 없음"


# ── ④ 가입 신청 1:1 확인 ─────────────────────────────────
def request(user):
    return SimpleNamespace(chat_join_request=SimpleNamespace(chat=SimpleNamespace(id=Room.CHAT, type="supergroup"), from_user=user,
                                                             user_chat_id=user.id))


async def ask(r, user):
    await handlers.on_join_request(request(user), r.ctx)
    return [c for c in r.bot.named("send_message") if c[1] == user.id]


async def press(r, user, i):
    q = FakeQuery(user.id, user, f"jr:{Room.CHAT}:{i}")
    await handlers.on_callback(SimpleNamespace(callback_query=q), r.ctx)
    return q


async def answer_of(r, uid):
    return (await joinreq._row(r.svc, Room.CHAT, uid))["answer"]


@test
async def join_request_dm_puzzle_approves_and_skips_room_captcha():
    r = await room(join_verify=True, captcha_enabled=True, raid_count=3, raid_action="kick")
    u = fake_user(NEW_ID, "신청자")
    [dm] = await ask(r, u)
    data = [b.callback_data for row in dm[3]["reply_markup"].inline_keyboard for b in row]
    assert len(data) == 6 and all(d.startswith(f"jr:{Room.CHAT}:") for d in data) and "입장 신청 확인" in dm[2]
    ans = await answer_of(r, u.id)
    q = await press(r, u, (ans + 1) % 6)
    assert q.answers[-1][1] is True and not r.bot.named("approve"), "틀리면 기회 차감만"
    q = await press(r, u, ans)
    assert r.bot.named("approve") == [("approve", Room.CHAT, u.id)] and "확인됐어요" in q.edits[-1]
    await raid.start(r.svc, r.bot, Room.CHAT, 30)                    # 방어 중이어도 통과자는 안 내보냄
    await handlers.handle_new_member(r.ctx, Room.CHAT, "방", u)
    assert not muted(r, u.id) and not r.bot.named("ban"), "통과자는 방 캡차·내보내기 생략"
    q = await press(r, u, ans)
    assert q.answers[-1][0] == "이미 끝난 확인이에요." and len(r.bot.named("approve")) == 1


@test
async def join_request_three_wrong_or_timeout_declines():
    r = await room(join_verify=True)
    a, b = fake_user(OLD_ID, "틀림"), fake_user(OLD_ID + 1, "늦음")
    await ask(r, a)
    await ask(r, b)
    wrong = (await answer_of(r, a.id) + 1) % 6
    for _ in range(3):
        q = await press(r, a, wrong)
    assert ("decline", Room.CHAT, a.id) in r.bot.calls and "거절" in q.edits[-1]
    await r.db._write("UPDATE join_requests SET expires=0 WHERE user_id=?", (b.id,))
    await handlers.job_tick(r.ctx)                                   # 30초마다 도는 실제 작업
    assert ("decline", Room.CHAT, b.id) in r.bot.calls and not r.bot.named("approve")


@test
async def join_request_lists_free_off_and_bots():
    r = await room(join_verify=True, cas_enabled=True)
    r.svc.cas.banned = {OLD_ID}
    await free.add(r.db, Room.CHAT, OLD_ID + 1, 1)
    assert not await ask(r, fake_user(OLD_ID, "스팸")) and ("decline", Room.CHAT, OLD_ID) in r.bot.calls
    assert not await ask(r, fake_user(OLD_ID + 1, "자유")) and ("approve", Room.CHAT, OLD_ID + 1) in r.bot.calls
    assert not await ask(r, fake_user(OLD_ID + 2, "봇", is_bot=True)) and ("decline", Room.CHAT, OLD_ID + 2) in r.bot.calls
    off = await room()
    assert not await ask(off, fake_user(OLD_ID, "누구")) and not off.bot.named("decline") and not off.bot.named("approve")


@test
async def join_request_fedban_listed_declined_unless_off():
    for mode, declined in (("alert", True), ("off", False)):
        r = await room(join_verify=True, fedban_mode=mode)
        await r.db._write("INSERT INTO fedban_entries(user_id, name, ts) VALUES(?, 'x', 0)", (OLD_ID,))
        await r.db._write("INSERT INTO fedban_reports(user_id, chat_id, reason, ts) VALUES(?, -100999, '사기', 0)", (OLD_ID,))
        dms = await ask(r, fake_user(OLD_ID, "명단"))
        assert (("decline", Room.CHAT, OLD_ID) in r.bot.calls) is declined and bool(dms) is not declined, mode


@test
async def join_request_dm_blocked_left_for_admin():
    r = await room(join_verify=True)
    r.bot.dm_blocked = {OLD_ID}
    await ask(r, fake_user(OLD_ID, "1:1불가"))
    assert not r.bot.named("decline") and not r.bot.named("approve") and not await joinreq._row(r.svc, Room.CHAT, OLD_ID)


@test
async def join_screen_toggle_and_invite_link():
    r = await room()
    q = FakeQuery(1, ADMIN, "")
    await menu.on_callback(r.svc, r.bot, q, ["t", str(Room.CHAT), "join_verify", "1"])
    assert (await r.db.get_settings(Room.CHAT))["join_verify"] is True
    assert any(b.callback_data == f"m:jrl:{Room.CHAT}" for row in q.kb.inline_keyboard for b in row)
    await menu.on_callback(r.svc, r.bot, q, ["jrl", str(Room.CHAT)])
    [link] = r.bot.named("invite_link")
    assert link[2]["creates_join_request"] is True and "t.me/+fakeJoinRequest" in q.edits[-1]
    stranger = FakeQuery(77, fake_user(77, "남"), "")
    await menu.on_callback(r.svc, r.bot, stranger, ["jrl", str(Room.CHAT)])
    assert len(r.bot.named("invite_link")) == 1, "관리자만"


@test
async def join_request_handler_registered():
    from telegram.ext import ApplicationBuilder, ChatJoinRequestHandler
    app = ApplicationBuilder().token("1:x").build()
    handlers.register(app, timezone.utc)
    hs = [h for group in app.handlers.values() for h in group if isinstance(h, ChatJoinRequestHandler)]
    assert len(hs) == 1 and hs[0].callback is handlers.on_join_request


if __name__ == "__main__":
    run_all()
