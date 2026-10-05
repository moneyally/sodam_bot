"""코덱스식 하네스 보강 (2026-10-05, 서버 실수 기록 기반): python tests/run_all.py harness_codex

1) 앞 요청에서 한 일(도구·결과)·방금 그린 그림을 다음 요청이 앎  2) '해 줘'에 방법만 말하면 한 번 다시
3) 스포츠 도구가 못 찾으면 web_search 로 넘어가라고 결과에 적음  4) 답 첫머리에서 엉뚱한 사람을 부르면 한 번 다시
"""
import json
import time
from types import SimpleNamespace

from fake_llm import Room, ScriptedLLM, fast_timers, restore_timers, tool_call
from fakes import FakeBot, fake_user, make_db, make_svc, runner

from sodam import agent, memory, tools
from sodam.agent import ADVICE_NOTE, advice_only, wrong_vocative
from sodam.permissions import Role
from sodam.prompt import build_messages
from sodam.tools import ToolCtx

test, run_all = runner()
BOSS, RUFFY = fake_user(1, "방장", "boss"), fake_user(30, "루피", "LF2030")
NEWBIE = fake_user(31, "️" * 6, "Jjmmm6")
NEWBIE.last_name = "맞링공 문의주세연"


async def room(script):
    r = Room()
    r.llm = ScriptedLLM(script)
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    for u in (BOSS, RUFFY, NEWBIE):
        await r.join(u)
    return r


def noted(llm, text):
    return [c for c in llm.of("chat") if any(m.get("content", "") == text or
                                              (isinstance(m.get("content"), str) and m["content"].startswith(text))
                                              for m in c["messages"] if m.get("role") == "system")]


# ── 4) 엉뚱한 사람 부르기 ─────────────────────────────────
@test
def vocative_check_only_flags_people_nobody_mentioned():
    people = [("맞링공 문의주세연", {"맞링공문의주세연", "문의주세연", "jjmmm6"}), ("카츄", {"카츄"})]
    me = {"루피", "lf2030"}
    assert wrong_vocative("문의주세연님, 불렀죠? ㅎㅎ", me, people, ["(이름만 부름)"]) == "맞링공 문의주세연"
    assert wrong_vocative("루피님, 불렀어요?", me, people, ["(이름만 부름)"]) is None            # 말한 본인
    assert wrong_vocative("대표님, 불렀어요?", me, people, ["(이름만 부름)"]) is None            # 일반 호칭
    assert wrong_vocative("문의주세연님, 환영해요!", me, people, ["★★ 맞링공 문의주세연 (ID 31): 새로 들어옴"]) is None
    assert wrong_vocative("카츄, 루피가 제일 활발해요", me, people, ["", "카츄 120개 · 루피 80개"]) is None   # 도구 결과에 있음
    assert wrong_vocative("문의주세연님은 아까 들어오셨어요", me, people, [""]) is None          # 부르는 말이 아님
    assert wrong_vocative("ㅋㅋ 오케이, 바로 갈게", me, people, [""]) is None


@test
async def name_only_call_answered_to_newcomer_gets_one_recheck():
    old = fast_timers()
    try:
        r = await room(["문의주세연님, 불렀죠? ㅎㅎ", "루피님 불렀어요? ㅎㅎ"])
        m = await r.say(RUFFY, "소담아")
        assert len(noted(r.llm, "검사: 답 첫머리에서 '맞링공 문의주세연'")) == 1 and not r.llm.script
        sent = m.replies + [c[2] for c in r.bot.named("send_message")]
        assert any("루피님 불렀어요" in t for t in sent) and not any("문의주세연님" in t for t in sent), sent
        r = await room(["루피님, 왜요?"])
        await r.say(RUFFY, "소담아")
        assert not noted(r.llm, "검사:") and not r.llm.script
    finally:
        restore_timers(old)


# ── 2) 말만 하고 안 함 ─────────────────────────────────────
@test
def advice_pattern():
    assert advice_only("이거 톡이 아니라 텔레그램으로 해줘", "우측 하단은 이렇게 넣으시면 됩니다.")
    assert advice_only("인폴레이 아니라 인플레이다 다시 해주라", "유의사항은 '인플레이 0%'로 다시 잡으시면 됩니다.")
    assert not advice_only("이거 어떻게 넣어?", "설정에서 넣으시면 됩니다.")                 # 방법을 물은 것
    assert not advice_only("다시 해줘", "다시 만들어 올렸어요 ㅎㅎ")


@test
async def fix_request_answered_with_how_to_gets_one_recheck():
    old = fast_timers()
    try:
        r = await room(["우측 하단은 '텔레그램 CB4885' 로 넣으시면 됩니다.", "그건 지금 여기선 못 해요."])
        await r.say(BOSS, "소담아 이거 톡이 아니라 텔레그램으로 해줘")
        assert len(noted(r.llm, ADVICE_NOTE)) == 1 and not r.llm.script
        r = await room(["설정 메뉴에서 넣으시면 됩니다."])
        await r.say(BOSS, "소담아 공지 문구는 어디서 넣어?")
        assert not noted(r.llm, ADVICE_NOTE) and not r.llm.script
    finally:
        restore_timers(old)


# ── 1) 앞 요청에서 한 일 · 방금 그린 그림 ─────────────────────
class PhotoBot(FakeBot):
    async def send_photo(self, chat_id, photo, caption=None, **kw):
        m = await super().send_photo(chat_id, photo, caption, **kw)
        fid = f"made{m.message_id}"
        self.files[fid] = bytes(photo)
        m.photo = [SimpleNamespace(file_id=fid)]
        return m


class ImageLLM:
    enabled = True

    def __init__(self):
        self.calls = []

    async def image(self, prompt, source=None, chat_id=None):
        self.calls.append((prompt, source))
        return b"\x89PNG-made-%d" % len(self.calls)


async def image_ctx(db, svc, bot, request):
    c = ToolCtx(svc, bot, -1009, BOSS, Role.ADMIN, await db.get_settings(-1009),
                request_msg=SimpleNamespace(message_id=9, text=request))
    c.request_text, c.reply_text = request, ""
    return c


@test
async def edit_without_reply_uses_the_picture_just_made_for_that_person():
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(-1009, "방")
    svc.llm, bot = ImageLLM(), PhotoBot()
    fn = tools._BY_NAME["make_image"].fn
    out = await fn(await image_ctx(db, svc, bot, "맥심 커피 광고 그려줘"), {"prompt": "Maxim coffee ad", "mode": "new"})
    assert "보냈음" in out
    out = await fn(await image_ctx(db, svc, bot, "박스 적은 건 빼줘"), {"prompt": "remove the box text", "mode": "edit"})
    assert "보냈음" in out, out
    src = svc.llm.calls[-1][1]
    assert src is not None and src.data == b"\x89PNG-made-1", src                      # 첫 그림이 원본
    other = await image_ctx(db, svc, bot, "박스 빼줘")
    other.caller = RUFFY                                                                 # 다른 사람은 남의 그림을 이어받지 않음
    assert "고칠 사진이 없음" in await fn(other, {"prompt": "x", "mode": "edit"})
    await db._write("UPDATE ai_turns SET ts=ts-? WHERE via='image'", (memory.LAST_MADE_SEC + 5,))
    assert "고칠 사진이 없음" in await fn(await image_ctx(db, svc, bot, "박스 빼줘"), {"prompt": "x", "mode": "edit"})


@test
async def recent_actions_reach_the_next_request_as_data():
    db = await make_db()
    svc = await make_svc(db)
    now = int(time.time())
    steps = json.dumps([{"tool": "make_image", "args": "mode=edit, prompt=무한 첫충 10% 배너",
                         "result": "이미지를 방에 보냈음"}], ensure_ascii=False)
    await db._write("INSERT INTO agent_runs(chat_id, user_id, ts, purpose, trigger, steps, status) VALUES(?,?,?,?,?,?,?)",
                    (-1009, BOSS.id, now - 60, "agent:admin", "배너 만들어줘", steps, "answered"))
    await db._write("INSERT INTO agent_runs(chat_id, user_id, ts, purpose, trigger, steps, status) VALUES(?,?,?,?,?,?,?)",
                    (-1009, RUFFY.id, now - 60, "agent:member", "남의 요청", steps, "answered"))
    await db._write("INSERT INTO agent_runs(chat_id, user_id, ts, purpose, trigger, steps, status) VALUES(?,?,?,?,?,?,?)",
                    (-1009, BOSS.id, now - 3 * 3600, "agent:admin", "옛날 요청", steps, "answered"))
    got = await memory.recent_actions(db, -1009, BOSS.id, svc.cfg.tz)
    assert len(got) == 1 and "배너 만들어줘" in got[0] and "make_image(mode=edit" in got[0], got
    ex = await memory.context_for(svc, -1009, BOSS.id, await db.get_settings(-1009), [])
    msgs = build_messages(bot_name="소담", bot_id=999, style_key="polite", tz=svc.cfg.tz, caller=BOSS, role_label="admin",
                          notes={}, history=[], reply_to=None, request="다시 해주라", **ex)
    assert "<recent_actions" in msgs[-1]["content"] and "make_image" in msgs[-1]["content"]
    assert "<recent_actions>" in msgs[0]["content"] or "<recent_actions>" in msgs[0]["content"].replace(", ", ">")


# ── 3) 스포츠 못 찾음 → 다음 길 ────────────────────────────
@test
async def sports_not_found_points_to_web_search():
    from sodam.sports import ui as sports_ui
    db = await make_db()
    svc = await make_svc(db)
    svc.sports = object()
    orig = sports_ui.UI

    class FakeUI:
        def __init__(self, _):
            pass

        async def team_text(self, q):
            return f"'{q}' 팀을 못 찾았어요. 한국어·영어 이름 둘 다 돼요."

        async def games_text(self, q, *a, **k):
            return "오늘 경기: 토트넘 2-1 첼시"

        def today(self):
            import datetime
            return datetime.date(2026, 10, 5)
    sports_ui.UI = FakeUI
    try:
        c = ToolCtx(svc, FakeBot(), -1009, BOSS, Role.ADMIN, await db.get_settings(-1009))
        out = await tools._BY_NAME["sports"].fn(c, {"action": "team", "query": "Brazil"})
        assert "web_search" in out and "Brazil" in out, out
        out = await tools._BY_NAME["sports"].fn(c, {"action": "today", "query": "EPL"})
        assert "web_search" not in out, out
    finally:
        sports_ui.UI = orig



# ── 5) 도구 고르기: 핵심만 처음부터, 나머지는 find_tools (클로드 코드 deferred tools · OpenAI tool_search 방식) ──
@test
def admin_starts_with_core_tools_and_a_catalog_of_the_rest():
    import sodam.panels  # noqa: F401  (패널 도구까지 다 등록된 상태)
    shown = tools.offered(Role.ADMIN, False)
    core, deferred = tools.split_core(shown)
    assert len(shown) > 40 and len(core) <= 21, (len(shown), len(core))          # 69개 → 21개 이하 + 목록
    names = {t.name for t in core}
    assert {"make_image", "greet_members", "change_setting", "mute_member", "read_chat"} <= names
    sch = tools.find_tools_schema(deferred)["function"]
    assert "warn_member" in sch["description"] and "ban_member" in sch["description"]
    assert set(sch["parameters"]["properties"]["names"]["items"]["enum"]) == {t.name for t in deferred}
    assert all(len(line) < 120 for line in sch["description"].splitlines() if line.startswith("- ")), "목록은 한 줄씩 짧게"


@test
async def find_tools_loads_then_the_tool_runs_next_round():
    old = fast_timers()
    try:
        r = await room([tool_call("find_tools", {"names": ["warn_member", "없는도구"]}),
                        tool_call("warn_member", {"names": ["루피"], "reason": "도배"}, "c2"),
                        "루피님 경고 줬어요."])
        await r.say(BOSS, "소담아 루피 경고 줘")
        calls = r.llm.of("chat")
        first = [t["function"]["name"] for t in calls[0]["tools"]]
        second = [t["function"]["name"] for t in calls[1]["tools"]]
        assert "warn_member" not in first and "find_tools" in first
        assert "warn_member" in second, "불러온 뒤 라운드부터 실림"
        res = [m["content"] for m in calls[1]["messages"] if m["role"] == "tool"][0]
        assert "불러옴: warn_member" in res and "없는 도구: 없는도구" in res, res
        assert await r.db.warning_count(r.CHAT, RUFFY.id) == 1                 # 실제로 실행됨 (관리자 요청 = 바로)
    finally:
        restore_timers(old)


@test
async def find_tools_reports_room_disabled_tools():
    from sodam.agent import _ToolSet
    t = tools._BY_NAME["warn_member"]
    ts = _ToolSet([{"function": {"name": "find_tools"}}], {"find_tools"}, {"warn_member": t})
    out = ts.load(["warn_member"])
    assert "꺼져 있는 기능" in out and any(s["function"]["name"] == "warn_member" for s in ts.schemas)

if __name__ == "__main__":
    run_all()
