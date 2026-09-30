"""👮 방 관리자가 말로 하는 관리 (전수 점검 2026-09-30): python tests/run_all.py test_admin_nl

실제 버그 3개 + 새 도구:
① change_setting 키 목록이 tools.py import 때 `list(DEFAULTS)` 로 굳어 113개 중 51개(잠금·사기 의심·퇴장 인사·뉴스…)가 빠짐
  → 이제 부를 때마다 만들고, 모든 설정은 '목록에 있거나 EXCLUDED 에 이유와 함께' (새 설정이 조용히 빠지지 않게).
② AI 밴 카드가 '내보내기'로 보였는데 실행은 영구 밴 → 밴 = '밴(영구 추방)', 내보내기 = kick_member(재입장 가능).
③ change_setting·.설정변경 whitelist_domains 가 목록을 통째로 덮고 URL 을 그대로 저장 → link_allowed 와 절대 안 맞음
  → coerce 가 normalize_domain, 말로는 edit_list 로 더하기/빼기.
새 도구(panels/admintools.py): kick_member · member_action(밴 해제·경고 취소/초기화·자유 멤버·캡차 통과, 전부 확인 카드) ·
edit_list · manage_schedule(목록·끄기/켜기/삭제 카드) · room_control(잠금·청소·공지 카드).
사람: 방장(1)·차단 권한 관리자(2)·차단 권한 없는 관리자(3)·오너(7)·멤버(20)·대상(21·22)·밴된 사람(50, 방 멤버 기록 없음).
"""
import json
import time
from types import SimpleNamespace

from fakes import FakeBot, FakeMsg, FakeQuery, fake_user, make_db, make_svc, runner

import sodam.__main__  # noqa: F401  (실제 시작 순서로 모든 설정·도구 등록)
import sodam.panels  # noqa: F401
from sodam import cards, commands, free, handlers, menu, rules, setkeys, settings, tools
from sodam.commands import CmdCtx
from sodam.permissions import Permissions, Role
from sodam.security import find_links, link_allowed

test, run_all = runner()
A, B = -1001000000001, -1001000000002
CREATOR, STRONG, WEAK, OWNER, MEMBER, T1, T2, GONE = 1, 2, 3, 7, 20, 21, 22, 50
NO_RIGHT = "'사용자 차단' 권한"


class RightsBot(FakeBot):
    """getChatAdministrators 가 실제처럼 권한 필드를 돌려준다 (WEAK = 차단·삭제 권한 없는 관리자)."""

    def __init__(self):
        super().__init__()
        self.member_status = {}
        self.rights = {CREATOR: ("creator", True), STRONG: ("administrator", True), WEAK: ("administrator", False)}

    async def get_chat_administrators(self, chat_id):
        return [SimpleNamespace(user=fake_user(uid, f"관리자{uid}"), status=st, can_restrict_members=r,
                                can_delete_messages=r) for uid, (st, r) in self.rights.items()]


async def setup():
    db = await make_db()
    svc = await make_svc(db, owner_ids=frozenset({OWNER}))
    svc.perms = Permissions(svc.cfg, db)
    svc.mod.perms = svc.perms
    for cid in (A, B):
        await db.ensure_chat(cid, f"방{cid % 10}")
        for uid in (CREATOR, STRONG, WEAK, MEMBER, T1, T2):
            await db.upsert_user(fake_user(uid, f"사람{uid}", username=f"user{uid}"))
            await db.touch_member(cid, uid)
    await db.upsert_user(fake_user(GONE, "먹튀왕", username="scammer50"))   # 밴돼서 방 멤버 기록 없음
    return db, svc, RightsBot()


def ctx(svc, bot, uid=STRONG, chat=A, role=Role.ADMIN):
    return tools.ToolCtx(svc, bot, chat, fake_user(uid, f"사람{uid}"), role, {"link_filter": True})


async def run(c, name, **args):
    c.settings = await c.svc.db.get_settings(c.chat_id) | {"image_daily": 5}
    return await tools.execute(name, json.dumps(args, ensure_ascii=False), c)


def last_card(bot, chat=A):
    return [x for x in bot.named("send_message") if x[1] == chat and x[3].get("reply_markup")][-1]


async def confirm(svc, bot, uid=STRONG, yn="y"):
    key = next(iter(svc.pending))
    q = FakeQuery(A, fake_user(uid, f"사람{uid}"), f"act:{key}:{yn}")
    await handlers._confirm_action(svc, bot, q, [key, yn])
    return q


async def press_token(svc, bot, uid, data, chat=A):
    q = FakeQuery(chat, fake_user(uid, f"사람{uid}"), data)
    await menu.on_callback(svc, bot, q, data.split(":")[1:])
    return q


def buttons(card):
    return [b for row in card[3]["reply_markup"].inline_keyboard for b in row]


# ── ① 설정 키 전부 (부를 때마다) ────────────────────────────
@test
async def every_registered_setting_is_reachable_or_explicitly_excluded():
    fn = tools._BY_NAME["change_setting"].schema()["function"]
    enum = fn["parameters"]["properties"]["key"]["enum"]
    assert set(setkeys.EXCLUDED) <= set(settings.DEFAULTS), "EXCLUDED 에 없어진 키가 남음"
    missing = set(settings.DEFAULTS) - set(enum) - set(setkeys.EXCLUDED)
    assert not missing, f"말로 못 바꾸는데 이유도 없는 설정: {sorted(missing)} → 목록에 두거나 setkeys.EXCLUDED 에 이유와 함께"
    assert not set(enum) & set(setkeys.EXCLUDED)
    assert len(settings.DEFAULTS) >= 110 and len(enum) == len(settings.DEFAULTS) - len(setkeys.EXCLUDED)
    for key in ("lock_photo", "scam_guard", "spamshield_mode", "farewell_mode", "news_mode", "voice_who", "tag_notify",
                "name_change_notice", "digest_hour", "wc_level", "botlink_mode", "forward_filter", "ai_enabled"):
        assert key in enum, key                                      # 예전에 빠졌던 것들
    assert "lock_photo=사진 막기" in fn["description"] and "forward_filter=전달(포워드) 메시지 막기(off|newbie|all)" in fn["description"]
    # 나중에 등록된 설정도 다음 호출부터 목록에 (import 순서와 무관)
    settings.register_setting("zz_test_later", False, "나중에 생긴 설정")
    try:
        enum2 = tools._BY_NAME["change_setting"].schema()["function"]["parameters"]["properties"]["key"]["enum"]
        assert "zz_test_later" in enum2
    finally:
        del settings.DEFAULTS["zz_test_later"], settings.LABELS["zz_test_later"]
    assert "zz_test_later" not in tools._BY_NAME["change_setting"].schema()["function"]["parameters"]["properties"]["key"]["enum"]


@test
async def change_setting_takes_korean_names_and_refuses_excluded_keys():
    db, svc, bot = await setup()
    c = ctx(svc, bot)
    out = await run(c, "change_setting", key="사진 막기", value="켜기")
    assert "설정 변경" in out and (await db.get_settings(A))["lock_photo"] is True, out
    out = await run(c, "change_setting", key="scam_guard", value="on")
    assert (await db.get_settings(A))["scam_guard"] is True, out
    for key in ("gt_setter", "greet_media_id", "whitelist_domains"):
        before = (await db.get_settings(A))[key]
        out = await run(c, "change_setting", key=key, value="12345")
        assert "말로 못 바꿈" in out and (await db.get_settings(A))[key] == before, (key, out)
    assert "edit_list" in await run(c, "change_setting", key="whitelist_domains", value="a.com")
    assert "없음" in await run(c, "change_setting", key="없는설정", value="1")
    out = await run(c, "change_setting", key="ai_enabled", value="off")
    assert ".AI대화 켜기" in out and (await db.get_settings(A))["ai_enabled"] is False, out
    out = await run(c, "change_setting", key="greet_template", value="가" * 801)
    assert "실패" in out and (await db.get_settings(A))["greet_template"] == "", out   # 편집기와 같은 800자
    out = await run(c, "change_setting", key="botlink_members", value="아무거나")
    assert "실패" in out and (await db.get_settings(A))["botlink_members"] == "request", out
    assert "설정 변경" in await run(c, "change_setting", key="botlink_members", value="조작")
    assert (await db.get_settings(A))["botlink_members"] == "control"
    m = ctx(svc, bot, MEMBER, role=Role.MEMBER)
    assert "권한 없음" in await run(m, "change_setting", key="lock_photo", value="off")
    assert (await db.get_settings(A))["lock_photo"] is True


@test
def coerce_normalizes_list_and_format_settings():
    assert settings.coerce("whitelist_domains", "https://www.YouTube.com/watch?v=1, naver.com") == ["youtube.com", "naver.com"]
    assert link_allowed(find_links("https://youtube.com/watch?v=2"), settings.coerce("whitelist_domains", "https://www.YouTube.com/watch"))
    for bad in ("<b>", "youtube", "https://"):
        try:
            settings.coerce("whitelist_domains", bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    try:
        settings.coerce("whitelist_domains", ",".join(f"d{i}.com" for i in range(51)))
        raise AssertionError("51개")
    except ValueError:
        pass
    assert settings.coerce("news_times", "9시, 21:00") == "09:00,21:00"
    assert settings.coerce("news_categories", "경제, 코인") == ["economy", "crypto"]
    for key, bad in (("news_categories", "요리"), ("news_times", "아침"), ("farewell_template", "가" * 801)):
        try:
            settings.coerce(key, bad)
            raise AssertionError(key)
        except ValueError:
            pass


@test
async def set_command_stores_normalized_domains():
    db, svc, bot = await setup()
    user = fake_user(STRONG, "관리자")
    text = ".설정변경 whitelist_domains https://www.YouTube.com/watch"
    parsed = commands.parse(text, bot.username)
    msg = FakeMsg(A, user, text)
    await commands.dispatch(CmdCtx(svc, bot, msg, A, user, Role.ADMIN, parsed[1], parsed[2]), parsed[0])
    assert (await db.get_settings(A))["whitelist_domains"] == ["youtube.com"], msg.replies


# ── ③ 금지어·허용 도메인 ────────────────────────────────────
@test
async def edit_list_domains_add_and_remove_keep_the_rest():
    db, svc, bot = await setup()
    await db.set_setting(A, "whitelist_domains", ["naver.com"])
    c = ctx(svc, bot)
    out = await run(c, "edit_list", list="whitelist_domains", op="add", items=["https://www.YouTube.com/watch?v=1"])
    assert (await db.get_settings(A))["whitelist_domains"] == ["naver.com", "youtube.com"], out
    assert link_allowed(find_links("여기 https://m.youtube.com/x"), (await db.get_settings(A))["whitelist_domains"])
    out = await run(c, "edit_list", list="whitelist_domains", op="add", items=["<b>", "daum.net"])
    assert "형식이 아닌" in out and (await db.get_settings(A))["whitelist_domains"] == ["naver.com", "youtube.com"], out
    out = await run(c, "edit_list", list="whitelist_domains", op="remove", items=["www.youtube.com"])
    assert (await db.get_settings(A))["whitelist_domains"] == ["naver.com"], out
    assert "바뀐 것 없음" in await run(c, "edit_list", list="whitelist_domains", op="remove", items=["daum.net"])
    assert "naver.com" in await run(c, "edit_list", list="whitelist_domains", op="list")
    await db.set_setting(A, "whitelist_domains", [f"d{i}.com" for i in range(50)])
    out = await run(c, "edit_list", list="whitelist_domains", op="add", items=["one-more.com"])
    assert "50개까지" in out and len((await db.get_settings(A))["whitelist_domains"]) == 50, out
    assert (await db.get_settings(B))["whitelist_domains"] == [], "다른 방은 그대로"
    logs = await db._all("SELECT detail FROM mod_log WHERE chat_id=? AND action='setting'", (A,))
    assert logs and all(x["detail"].startswith("whitelist_domains=") for x in logs)


@test
async def edit_list_banned_words_add_remove_and_limits():
    db, svc, bot = await setup()
    c = ctx(svc, bot)
    out = await run(c, "edit_list", list="banned_words", op="add", items=["먹튀", "사기, 먹튀"])
    assert sorted(await db.banned_words(A)) == ["먹튀", "사기"], out
    assert "이미" in await run(c, "edit_list", list="banned_words", op="add", items=["먹튀"])
    out = await run(c, "edit_list", list="banned_words", op="remove", items=["사기", "없는말"])
    assert await db.banned_words(A) == ["먹튀"] and "원래 없던 것: 없는말" in out, out
    assert "1~50자" in await run(c, "edit_list", list="banned_words", op="add", items=["가" * 51])
    for i in range(199):
        await db.set_banned_word(A, f"w{i}", True)
    out = await run(c, "edit_list", list="banned_words", op="add", items=["넘침1", "넘침2"])
    assert "200개까지" in out and "넘침1" not in await db.banned_words(A), out
    assert await db.banned_words(B) == []


# ── ② 밴 ≠ 내보내기 ─────────────────────────────────────────
@test
async def kick_is_rejoinable_and_ban_is_labelled_permanent():
    db, svc, bot = await setup()
    out = await run(ctx(svc, bot), "kick_member", names=["@user21"], reason="도배")
    assert "확인 버튼을 보냈음" in out and not bot.named("ban"), out
    card = last_card(bot)
    assert "내보내기(재입장 가능)" in card[2] and "밴" not in card[2], card[2]
    q = await confirm(svc, bot)
    assert ("ban", A, T1) in bot.calls and ("unban", A, T1) in bot.calls, bot.calls   # 킥 = 밴 후 바로 해제
    assert "다시 들어올 수 있어요" in q.edits[-1]
    assert await db._one("SELECT 1 FROM mod_log WHERE chat_id=? AND target_id=? AND action='kick'", (A, T1))
    bot.calls.clear()
    await run(ctx(svc, bot), "ban_member", names=["@user22"], reason="사기")
    card = last_card(bot)
    assert "밴(영구 추방)" in card[2] and "내보내기" not in card[2] and "밴(영구 추방)" in buttons(card)[0].text, card[2]
    q = await confirm(svc, bot)
    assert ("ban", A, T2) in bot.calls and ("unban", A, T2) not in bot.calls
    assert "밴(영구 추방)" in q.edits[-1] and "내보냈" not in q.edits[-1], q.edits
    ban = tools._BY_NAME["ban_member"].description
    assert "영구" in ban and "kick_member" in ban and "내보낸다" not in ban


# ── ④ 푸는 조치 (확인 카드) ──────────────────────────────────
@test
async def unban_finds_banned_people_who_are_not_in_the_room():
    db, svc, bot = await setup()
    await db.log_mod(A, STRONG, GONE, "ban", "사기")
    bot.member_status[(A, GONE)] = "kicked"
    for who in ("@scammer50", "먹튀왕", str(GONE)):
        svc.pending.clear()
        out = await run(ctx(svc, bot), "member_action", action="unban", names=[who])
        assert "확인 버튼을 보냈음" in out and len(svc.pending) == 1, (who, out)
    assert not bot.named("unban"), "카드만 — 누르기 전엔 안 풀림"
    q = await confirm(svc, bot)
    assert ("unban", A, GONE) in bot.calls and "밴을 풀었어요" in q.edits[-1], (bot.calls, q.edits)
    svc.pending.clear()
    bot.member_status[(A, 777)] = "kicked"
    out = await run(ctx(svc, bot), "member_action", action="unban", names=["777"])     # 기록 없는 숫자 ID 도
    assert "확인 버튼을 보냈음" in out and "ID 777" in last_card(bot)[2], out
    svc.pending.clear()
    out = await run(ctx(svc, bot), "member_action", action="unban", names=["@user21"])    # 밴 안 된 사람
    assert "밴된 상태가 아님" in out and not svc.pending, out
    out = await run(ctx(svc, bot, chat=B), "member_action", action="unban", names=["먹튀왕"])  # 다른 방 밴 기록은 안 봄
    assert "못 찾음" in out and not svc.pending, out


@test
async def unwarn_reset_free_unfree_and_captcha_pass_go_through_cards():
    db, svc, bot = await setup()
    for i in range(3):
        await db.add_warning(A, T1, STRONG, f"도배{i}")
    out = await run(ctx(svc, bot), "member_action", action="unwarn", names=["@user21"])
    assert "확인 버튼을 보냈음" in out and await db.warning_count(A, T1) == 3, out
    await confirm(svc, bot)
    assert await db.warning_count(A, T1) == 2
    svc.pending.clear()
    await run(ctx(svc, bot), "member_action", action="reset_warns", names=["@user21"])
    assert "경고 전부 지우기" in last_card(bot)[2]
    await confirm(svc, bot)
    assert await db.warning_count(A, T1) == 0
    svc.pending.clear()
    out = await run(ctx(svc, bot), "member_action", action="unwarn", names=["@user21"])
    assert "경고가 없음" in out and not svc.pending, out
    await db.add_warning(A, T1, STRONG, "또")                     # 카드가 떠 있는 동안 다른 관리자가 .경고초기화
    await run(ctx(svc, bot), "member_action", action="unwarn", names=["@user21"])
    await db.clear_warnings(A, T1)
    q = await confirm(svc, bot)
    assert "경고가 없어요" in q.edits[-1], q.edits
    line = (await db._one("SELECT text FROM ai_card_log WHERE chat_id=? ORDER BY id DESC LIMIT 1", (A,)))["text"]
    assert line.startswith("⚠️ 경고 1회 취소 실행 안 됨"), line                 # 할 게 없던 건 '실행됨' 이 아님
    svc.pending.clear()
    await db.add_warning(A, T2, STRONG, "x")
    await run(ctx(svc, bot), "member_action", action="free", names=["@user22"])
    assert not await free.is_free(db, A, T2)
    await confirm(svc, bot)
    assert await free.is_free(db, A, T2) and await db.warning_count(A, T2) == 0 and not await free.is_free(db, B, T2)
    svc.pending.clear()
    assert "이미 자유 멤버" in await run(ctx(svc, bot), "member_action", action="free", names=["@user22"])
    await run(ctx(svc, bot), "member_action", action="unfree", names=["@user22"])
    await confirm(svc, bot)
    assert not await free.is_free(db, A, T2)
    svc.pending.clear()
    assert "캡차 대기 중이 아님" in await run(ctx(svc, bot), "member_action", action="captcha_pass", names=["@user21"])
    newbie = fake_user(40, "신입", username="newbie40")
    await db.upsert_user(newbie)
    await db.touch_member(A, 40)
    assert await svc.captcha.start(bot, A, newbie)
    out = await run(ctx(svc, bot), "member_action", action="captcha_pass", names=["@newbie40"])
    assert "확인 버튼을 보냈음" in out and await svc.captcha.pending(A, 40), out
    await confirm(svc, bot)
    assert not await svc.captcha.pending(A, 40)


@test
async def rights_roles_protected_tainted_and_one_card_per_answer():
    db, svc, bot = await setup()
    weak = ctx(svc, bot, WEAK)
    for name, args in (("kick_member", {"names": ["@user21"], "reason": "x"}),
                       ("member_action", {"action": "unban", "names": ["@user21"]}),
                       ("member_action", {"action": "free", "names": ["@user21"]})):
        out = await run(weak, name, **args)
        assert out == tools.NO_RIGHT and not svc.pending, (name, out)
    assert NO_RIGHT[1:] in await run(weak, "room_control", action="lock")
    assert "메시지 삭제" in await run(weak, "room_control", action="purge", count=5)
    member = ctx(svc, bot, MEMBER, role=Role.MEMBER)
    for name in ("kick_member", "member_action", "edit_list", "manage_schedule", "room_control"):
        assert "권한 없음" in await run(member, name, action="lock", list="banned_words", op="list", target="schedule"), name
        assert name not in {t.name for t in tools.available(Role.ADMIN, {}, in_dm=True)}, "1:1 에선 안 보임"
    bot.can_moderate = False                                                            # 봇에게 B 방 '사용자 차단' 권한 없음
    assert await run(ctx(svc, bot, chat=B), "kick_member", names=["@user21"], reason="x") == tools.NO_BOT_RIGHT
    await db.add_warning(B, T1, STRONG, "x")
    assert "확인 버튼을 보냈음" in await run(ctx(svc, bot, chat=B), "member_action", action="unwarn", names=["@user21"])  # DB 만 → 됨
    svc.pending.clear()
    bot.calls.clear()
    bot.can_moderate = True
    out = await run(ctx(svc, bot), "kick_member", names=["@user3"], reason="x")          # 관리자는 제재 못 함
    assert "관리자나 봇" in out and not svc.pending, out
    tainted = ctx(svc, bot)
    tainted.tainted = True
    for name, args in (("kick_member", {"names": ["@user21"], "reason": "x"}),
                       ("member_action", {"action": "unwarn", "names": ["@user21"]}),
                       ("edit_list", {"list": "banned_words", "op": "add", "items": ["x"]}),
                       ("manage_schedule", {"target": "schedule", "op": "list"}),
                       ("room_control", {"action": "lock"})):
        tainted.settings = await db.get_settings(A)
        out = await tools.execute(name, json.dumps(args), tainted)
        assert "보안" in out, (name, out)
    assert not svc.pending and not await db.banned_words(A) and not bot.named("send_message")
    one = ctx(svc, bot)
    await db.add_warning(A, T2, STRONG, "x")
    assert "확인 버튼을 보냈음" in await run(one, "kick_member", names=["@user21"], reason="x")
    assert await run(one, "member_action", action="unwarn", names=["@user22"]) == tools.SANCTION_ONCE
    # 카드를 권한 없는 관리자가 누르면 거절 (취소는 됨)
    q = await confirm(svc, bot, WEAK)
    assert NO_RIGHT in q.answers[0][0] and not bot.named("ban"), q.answers
    q = await confirm(svc, bot, MEMBER)
    assert "관리자만" in q.answers[0][0] and not bot.named("ban")


# ── ⑤ 예약·알림 규칙 관리 ───────────────────────────────────
@test
async def manage_schedule_lists_and_changes_only_through_requester_card():
    db, svc, bot = await setup()
    sid = await db.add_schedule(A, kind="daily", at_time="09:00", interval_min=None, title="", text="좋은 아침",
                                media_type=None, media_id=None, pin=False, created_by=STRONG, action="remind")
    other = await db.add_schedule(B, kind="daily", at_time="10:00", interval_min=None, title="", text="다른 방",
                                  media_type=None, media_id=None, pin=False, created_by=STRONG)
    rid = await rules.add(svc, A, STRONG, {"trig": "keyword", "arg": "입금", "action": "dm"})
    c = ctx(svc, bot)
    out = await run(c, "manage_schedule", target="schedule", op="list")
    assert f"#{sid}" in out and "매일 09:00" in out and "좋은 아침" in out and "다른 방" not in out, out
    assert "입금" in await run(c, "manage_schedule", target="alert_rule", op="list")
    assert "없음" in await run(c, "manage_schedule", target="schedule", op="pause", id=other)   # 다른 방 번호
    out = await run(c, "manage_schedule", target="schedule", op="pause", id=sid)
    assert "확인 버튼을 보냈음" in out and (await db.get_schedule(A, sid))["enabled"] == 1, out
    ok, no = buttons(last_card(bot))[:2]
    q = await press_token(svc, bot, CREATOR, ok.callback_data)                            # 요청자만
    assert "요청한 사람만" in q.answers[-1][0] and (await db.get_schedule(A, sid))["enabled"] == 1
    await press_token(svc, bot, STRONG, ok.callback_data)
    assert (await db.get_schedule(A, sid))["enabled"] == 0
    assert "이미 꺼져" in await run(c, "manage_schedule", target="schedule", op="pause", id=sid)
    await run(c, "manage_schedule", target="alert_rule", op="delete", id=rid)
    ok, no = buttons(last_card(bot))[:2]
    await press_token(svc, bot, STRONG, no.callback_data)
    assert await rules.room_rules(db, A), "취소 = 그대로"
    await run(c, "manage_schedule", target="alert_rule", op="delete", id=rid)
    ok, _ = buttons(last_card(bot))[:2]
    await press_token(svc, bot, STRONG, ok.callback_data)
    assert not await rules.room_rules(db, A) and await db.get_schedule(B, other)
    lines = [r["text"] for r in await db._all("SELECT text FROM ai_card_log WHERE chat_id=? ORDER BY id", (A,))]
    assert lines[-1].startswith("✅") and "삭제" in lines[-1], lines
    assert "manage_schedule" not in cards.LOW_RISK and "room_control" in cards.NEVER


# ── ⑥ 방 잠금·청소·공지 ─────────────────────────────────────
@test
async def room_control_lock_purge_notice_need_card_and_rights():
    db, svc, bot = await setup()
    c = ctx(svc, bot)
    await run(c, "room_control", action="lock")
    assert not bot.named("set_perms")
    ok, _ = buttons(last_card(bot))[:2]
    await press_token(svc, bot, STRONG, ok.callback_data)
    assert bot.named("set_perms") and await db.get_state(A, "locked")
    assert "이미 잠겨" in await run(c, "room_control", action="lock")
    await run(c, "room_control", action="unlock")
    ok, _ = buttons(last_card(bot))[:2]
    bot.rights[STRONG] = ("administrator", False)                       # 누르기 전에 권한이 빠짐
    svc.perms.forget(A)
    q = await press_token(svc, bot, STRONG, ok.callback_data)
    assert "권한" in q.answers[-1][0] and await db.get_state(A, "locked"), q.answers
    assert "만료" in (await press_token(svc, bot, STRONG, ok.callback_data)).answers[-1][0]   # 카드는 닫힘
    bot.rights[STRONG] = ("administrator", True)
    svc.perms.forget(A)
    await run(c, "room_control", action="unlock")
    ok, _ = buttons(last_card(bot))[:2]
    await press_token(svc, bot, STRONG, ok.callback_data)
    assert not await db.get_state(A, "locked")
    now = int(time.time())
    for mid in (500, 501, 502):
        await db._write("INSERT INTO messages(chat_id, user_id, msg_id, text, ts) VALUES(?,?,?,?,?)", (A, T1, mid, "도배", now))
    assert "1~100" in await run(c, "room_control", action="purge", count=500)
    await run(c, "room_control", action="purge", count=3)
    ok, _ = buttons(last_card(bot))[:2]
    await press_token(svc, bot, STRONG, ok.callback_data)
    assert bot.named("delete_many")[-1] == ("delete_many", A, [502, 501, 500]), bot.named("delete_many")
    await run(c, "room_control", action="notice", text="내일 정모 <b>7시</b>")
    card = last_card(bot)
    assert "&lt;b&gt;" in card[2]
    await press_token(svc, bot, STRONG, buttons(card)[0].callback_data)
    posted = [x for x in bot.named("send_message") if x[2].startswith("📢 <b>공지</b>")]
    assert posted and "&lt;b&gt;7시" in posted[-1][2] and bot.named("pin"), posted


# ── 안내서·프롬프트 ─────────────────────────────────────────
@test
def prompt_and_guide_mention_speech_admin():
    from sodam import prompt
    from sodam.panels import guidebook as G
    sysp = prompt.static_system("소담")
    assert "kick_member" in sysp and "ban_member" in sysp
    docs = G.load()
    assert "말로도 돼요" in docs["security"]["body"] and "말로도 돼요" in docs["schedule"]["body"]
    assert "말로도 돼요" in docs["admin-menu"]["body"] and "말로도 돼요" in docs["commands"]["body"]
