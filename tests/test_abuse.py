"""🤬 패드립·성적 모욕 자동 제재 (sodam/abuse.py): python tests/run_all.py abuse

실제 요청 2026-10-05 베베 가족방: 욕설·성적 발언 1회 1분 · 2회 3분 · 3회 추방 + 누가 추방됐는지 관리자에게.
오너 결정: 패드립 + 성적 모욕만 (감탄사 욕 허용), 소담에게 한 말은 안 셈, 3회 = 밴, 24시간 지나면 초기화.
아래 문장들은 베베방 실측(5.6일, 16,975개)에서 걸리던/안 걸려야 하던 모양을 본뜬 것.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room  # noqa: E402
from fakes import FakeQuery, fake_user, runner  # noqa: E402

from sodam import abuse, menu  # noqa: E402

test, run_all = runner()
ADMIN, WEAK, BAD, OTHER, FREE = 5, 6, 40, 41, 42

# 코드 1차 후보가 돼야 하는 것 (AI 가 최종 판단)
TP = ["니애미야~", "느금마", "너거매 죽창 날리기전에 싸물어라", "느그할배", "애미뒤졌냐", "너거부모님 보지가",
      "이 개보지년아", "개보지들아", "후장걸레년 ㅋㅋ", "보지년이 .", "응 니 자지", "테드 보지에 박자"]
# 낱말은 들어 있지만 후보도 아니어야 하는 것 (감탄·장난·닉네임·감사·연도·메타·직접 욕)
FP = ["ㅅㅅㅅ 감사합니다", "개섹스 ㅅㅅ", "자지버스야", "딸딸형 ㅈㄴ 웃기네", "슈퍼엠창 깔게요", "5년 전에 했음",
      "가야한다 진짜", "좆같네 그냥", "시발련아 가만히 있는데", "꺼져 시발련아", "애미욕하던데요", "느그 오토바이 타지",
      "니 엄마 오셨어?", "네 메일 봤어", "섹스하러가자", "좆됐네 3발빼고 가야겠네", "보지냐 ㅋㅋ", "니매너 좋네",
      "느금마 드립 하지마", "니애미 욕 그만해"]   # 욕하지 말라는 말·욕 얘기


def yes(kind="parent", conf=0.95):
    return {"abuse": True, "kind": kind, "conf": conf, "why": "남의 부모 욕"}


def no():
    return {"abuse": False, "kind": "", "conf": 0.9, "why": "감탄"}


async def room(mode="ladder", **extra):
    r = Room()
    await r.open(admins=(ADMIN, WEAK), settings={"abuse_mode": mode, "link_filter": False, **extra})
    r.bot.admins = [fake_user(ADMIN, "방장"), fake_user(WEAK, "부방장"), fake_user(r.bot.id, "소담", "sodambot", is_bot=True)]
    perms = r.svc.perms

    async def can(bot, cid, uid, right="restrict"):
        return uid == ADMIN
    perms.can = can
    return r


def script(r, *items):
    r.llm.json_script["abuse"] = list(items)


def ai_calls(r):
    return r.llm.of("json", "abuse")


def restricts(r, uid=BAD):
    return [c for c in r.bot.calls if c[0] == "restrict" and c[2] == uid]


def bans(r, uid=BAD):
    return [c for c in r.bot.calls if c[0] == "ban" and c[2] == uid]


def dms(r, uid=ADMIN):
    return [c for c in r.bot.named("send_message") if c[1] == uid]


def cbs(call):
    kb = call[3].get("reply_markup")
    return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


async def say(r, text, uid=BAD, name="나쁜말", reply_to=None):
    return await r.say(fake_user(uid, name), text, reply_to=reply_to)


async def age(r, seconds):
    """기록을 과거로 (1분 간격·24시간 초기화 재현)."""
    await r.db._write("UPDATE abuse_strikes SET ts=ts-?", (seconds,))


@test
async def code_candidates_catch_parent_and_sexual_insults_but_not_banter():
    miss = [t for t in TP if abuse.candidate(t, names=["테드"]) is None]
    wrong = [(t, abuse.candidate(t, names=["딸딸"])) for t in FP if abuse.candidate(t, names=["딸딸"]) is not None]
    assert not miss, miss
    assert not wrong, wrong
    assert abuse.candidate("로이 개보지", names=["로이"]).kind == "sexual"            # 멤버 이름 + 성적 욕
    assert abuse.candidate("로이 개보지", names=[]) is None
    assert abuse.candidate("개보지", to_person=True) is not None                    # 답장·@ 로 사람을 정한 짧은 욕
    assert abuse.candidate("개보지", to_person=False) is None
    assert abuse.candidate("딸딸이 개보지년아", allow=["딸딸이 개보지년아"]) is None     # 허용 낱말은 지우고 봄


@test
async def ladder_one_minute_three_minutes_then_ban_and_admin_told():
    r = await room()
    script(r, yes(), yes("sexual"), yes())
    await say(r, "니애미야~")
    [m1] = restricts(r)
    assert m1[4] is not None and 50 <= (m1[4].timestamp() - time.time()) <= 70, m1      # 1분
    notice = [c for c in r.bot.named("send_message") if c[1] == r.CHAT][-1][2]
    assert "패드립 경고 1회/3" in notice and "1분 채팅 금지" in notice and "3회가 되면 밴" in notice, notice
    assert not dms(r), "추방 전엔 관리자 1:1 안 보냄 (기본 '추방만 알림')"
    await age(r, 120)
    await say(r, "이 개보지년아")
    m2 = restricts(r)[-1]
    assert 170 <= (m2[4].timestamp() - time.time()) <= 190, m2                         # 3분
    await age(r, 120)
    await say(r, "느그할배")
    assert bans(r) and "밴" in [c for c in r.bot.named("send_message") if c[1] == r.CHAT][-1][2]
    [dm] = dms(r)
    assert "추방했어요" in dm[2] and f"<code>{BAD}</code>" in dm[2] and "느그할배" in dm[2] and "3번째" in dm[2], dm[2]
    assert any(":u" in d for d in cbs(dm)) and any(":f" in d for d in cbs(dm))
    assert not dms(r, WEAK), "'사용자 차단' 권한 없는 관리자는 안 받음"
    rows = await r.db._all("SELECT step, action FROM abuse_strikes ORDER BY id")
    assert [(x["step"], x["action"]) for x in rows] == [(1, "mute:1"), (2, "mute:3"), (3, "ban")]


@test
async def ai_must_confirm_and_banter_never_reaches_ai():
    r = await room()
    script(r, no(), yes(conf=0.5))
    await say(r, "니애미 ㅇㅈㄹ할것같은데")                     # 후보지만 AI 가 아니라고
    await asyncio.sleep(0)
    await r.db._write("DELETE FROM abuse_strikes")
    await say(r, "느그메 봉지", uid=OTHER)                       # AI 확신 0.5 → 안 함
    assert not restricts(r) and not restricts(r, OTHER) and not bans(r)
    n = len(ai_calls(r))
    for t in FP:
        await say(r, t)
    assert len(ai_calls(r)) == n == 2, "후보가 아닌 말은 AI 도 안 부름 (비용 0)"
    r.llm.enabled = False                                        # AI 를 못 쓰면 제재 안 함
    await say(r, "느금마", uid=OTHER)
    assert not restricts(r, OTHER)


@test
async def sodam_admin_free_member_and_quick_repeats_are_not_counted():
    r = await room()
    script(r, yes(), yes(), yes(), yes())
    r.llm.script = ["어림없지", "어림없지"]                         # 소담 부른 말·소담 글 답장엔 소담이 답함 (제재 아님)
    await say(r, "소담이 애미창련")                               # 소담에게 한 말
    bot_msg = r.msg(fake_user(r.bot.id, "소담", "sodambot", is_bot=True), "안녕")
    await say(r, "느금마", reply_to=bot_msg)                      # 소담 글에 답장
    await say(r, "느금마", uid=ADMIN, name="방장")                  # 관리자
    from sodam import free
    await free.add(r.db, r.CHAT, FREE, ADMIN)
    await say(r, "느금마", uid=FREE, name="자유")
    assert not ai_calls(r), "후보 단계 전에 빠짐"
    await say(r, "느금마")
    await say(r, "니애미야~")                                     # 1분 안 연달아 → 한 번만 셈
    assert len(restricts(r)) == 1 and len(ai_calls(r)) == 1


@test
async def counts_reset_after_24_hours():
    r = await room()
    script(r, yes(), yes(), yes())
    await say(r, "느금마")
    await age(r, 120)
    await say(r, "니애미야~")
    await age(r, 25 * 3600)                                      # 둘 다 24시간 넘음
    await say(r, "느그할배")
    assert not bans(r) and restricts(r)[-1][4].timestamp() - time.time() < 70, "다시 1회부터 (1분)"


@test
async def shadow_mode_only_reports_and_feedback_is_recorded():
    r = await room("shadow")
    script(r, yes())
    await say(r, "니애미야~")
    assert not restricts(r) and not bans(r)
    assert not [c for c in r.bot.named("send_message") if c[1] == r.CHAT], "방엔 아무것도 안 올림"
    [dm] = dms(r)
    assert "기록만" in dm[2] and any(d.endswith(":y") for d in cbs(dm)) and any(d.endswith(":f") for d in cbs(dm))
    fp = next(d for d in cbs(dm) if d.endswith(":f"))
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"), fp)
    await menu.on_callback(r.svc, r.bot, q, fp.split(":")[1:])
    row = await r.db._one("SELECT status, by_id FROM abuse_strikes")
    assert (row["status"], row["by_id"]) == ("fp", ADMIN), dict(row)
    st = await abuse.stats(r.db, r.CHAT)
    assert st["fp"] == 1 and st["total"] == 1


@test
async def undo_button_unbans_once_and_needs_right():
    r = await room(abuse_ladder="0,60,ban", abuse_notify="all")
    script(r, yes(), yes(), yes())
    for i, t in enumerate(("느금마", "니애미야~", "느그할배")):
        await say(r, t)
        await age(r, 120)
    assert bans(r)
    dm = dms(r)[-1]
    undo = next(d for d in cbs(dm) if d.endswith(":u"))
    q = FakeQuery(WEAK, fake_user(WEAK, "부방장"), undo)                       # 권한 없는 관리자
    await menu.on_callback(r.svc, r.bot, q, undo.split(":")[1:])
    assert not [c for c in r.bot.calls if c[0] == "unban"]
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"), undo)
    await menu.on_callback(r.svc, r.bot, q, undo.split(":")[1:])
    assert [c for c in r.bot.calls if c[0] == "unban" and c[2] == BAD]
    q2 = FakeQuery(ADMIN, fake_user(ADMIN, "방장"), undo)
    await menu.on_callback(r.svc, r.bot, q2, undo.split(":")[1:])
    assert len([c for c in r.bot.calls if c[0] == "unban"]) == 1 and "이미" in q2.answers[-1][0]
    assert await abuse.count_active(r.db, r.CHAT, BAD, 0) == 2, "되돌린 건 횟수에서 빠짐"
    first = [d for d in dms(r) if "경고" in d[2] or "자동 제재" in d[2]]
    assert len(dms(r)) == 3 and first, "매번 알림 설정이면 단계마다 1:1"


@test
async def off_by_default_and_panel_and_allow_words():
    r = Room()
    await r.open(admins=(ADMIN,), settings={"link_filter": False})
    script(r, yes())
    await say(r, "느금마")
    assert not ai_calls(r) and (await r.db.get_settings(r.CHAT))["abuse_mode"] == "off"
    q = FakeQuery(ADMIN, fake_user(ADMIN, "방장"), f"m:abu:{r.CHAT}")
    await menu.on_callback(r.svc, r.bot, q, q.data.split(":")[1:])
    text = q.edits[-1] if q.edits else ""
    assert "패드립" in text and "감탄 욕" in text, text
    from sodam.panels import abuse as panel
    c = type("C", (), {"svc": r.svc, "cid": r.CHAT, "uid": ADMIN, "bot": r.bot})()
    ok, _ = await panel.in_allow(c, type("M", (), {"text": "딸딸, 슈퍼엠창, x"})())
    assert ok and (await r.db.get_settings(r.CHAT))["abuse_allow"] == ["딸딸", "슈퍼엠창"]


if __name__ == "__main__":
    asyncio.run(run_all())
