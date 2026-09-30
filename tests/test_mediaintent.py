"""🎞️ 움프 vs 🎬 AI 영상 의도 (sodam/mediaintent.py): 실제 요청 평가표 · 도구 돌려보내기 · AI 영상 없는 방은 애매=움프.
python tests/run_all.py mediaintent"""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, ScriptedLLM, fast_timers, reply, restore_timers, tool_call  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import mediaintent  # noqa: E402
from sodam.mediaintent import classify, redirect  # noqa: E402

test, run_all = runner()
BOSS = fake_user(10, "방장", "boss")

# 실제 agent_runs 요청 (09-28 ~ 10-01) + 흔한 말: 기대 판정
TABLE = [
    ("이런 느낌아니지나 막 걸어가면서 담배 때우는 느낌으로 만들러줘야디", "video"),
    ("원피스 루피로 그냥 톰하디 맹키러 멋지게 담배피우는 영상 하나 만들어줘", "video"),
    ("움직이는프로필말고 영상을 제작해", "video"),
    ("루피님 프로필사진을 보고 움직이는영상프로필을만들어줘", "ump"),
    ("돈돈이 프로필보고 개멋지게 움직이는 프로필 하나만들어드려라", "ump"),
    ("이사진 텔레그램 프로필 만들건데 gif로 너가 할수있는 모든 방법을 동원해서 그냥 개멋지게 4초대로 하나 만들어줘봐", "ump"),
    ("글리치 + 네온으로 강렬하게 움직이는프로필 생성", "ump"),
    ("이거 움직이게 만들어줘", "ambiguous"),
    ("원형테두리 없애주고 영상으로 움직이게 해줘", "ump"),          # 움프 고치던 중 (실제: AI 영상으로 잘못 감)
    ("영상제작해줘 움직이는거로", "video"),
    ("움직이는프로필 제작", "ump"),
    ("줌 애니메이션으로 트럼프대표님이 원하실만한거 움프 만들어줘", "ump"),
    ("이 인물이 움직이는 프사 만들어봐 수건으러 쌍절곤 휘둘러", "ump"),
    ("이 사진을 이제 멋잇게 움직여봐. 인물이 움직이면좋겠어", "ambiguous"),
    ("상품권이 다 타서 없어지게해줘 움직이게", "ambiguous"),
    ("움직이는 프로필사진 생성 애니메이션은 아무거나", "ump"),
    (f"(선택: {mediaintent.CHOICE_VIDEO}) 어떤 걸로 만들까요?", "video"),
    (f"(선택: {mediaintent.CHOICE_UMP}) 어떤 걸로 만들까요?", "ump"),
    ("바닷가에서 강아지가 뛰어노는 영상 만들어줘", "video"),
    ("움프 말고 진짜 영상으로", "video"),
    ("영상 말고 움프로 해줘", "ump"),
    ("고양이가 춤추는 동영상", "video"),
    ("내 프사 반짝이게", "ump"),
    ("내 프사로 춤추는 영상 만들어줘", "video"),                         # 프사가 원본인 AI 영상 (둘 다 + 동작)
    ("움직이는 영상 프로필 하나 만들어줘", "ump"),                         # 둘 다 + 동작 없음 = 프로필용
    ("프로필용 영상 만들어줘 반짝이게", "ump"),                             # 둘 다 + 프로필 말 = 움프
    ("글리치 효과 넣은 영상 만들어줘", "video"),                           # 둘 다 + 프로필 말 없음 = 영상
    ("안녕 소담아", None),
    ("오늘 날씨 어때", None),
    ("고양이 그림 그려줘", None),
]


@test
def evaluation_table():
    wrong = [(t, w, classify(t)) for t, w in TABLE if classify(t) != w]
    assert len(TABLE) >= 25
    assert not wrong, wrong


@test
def reply_context_and_redirects():
    # '움직이게' 만 있어도 답장 대상이 움프(프로필) 얘기면 움프
    assert classify("이거 움직이게 해줘", reply_to="[움프] 반짝 네온 프로필") == "ump"
    assert classify("이거 움직이게 해줘", reply_to="") == "ambiguous"
    assert redirect("ump", "make_video") and "make_profile_video" in redirect("ump", "make_video")
    assert "ask_choice" in redirect("ambiguous", "make_video")
    assert redirect("video", "make_profile_video") and "make_video" in redirect("video", "make_profile_video")
    assert redirect("video", "make_video") is None and redirect("ump", "make_profile_video") is None
    assert redirect("ambiguous", "make_profile_video") is None          # 애매 + 움프 = 싸고 한도 넉넉 → 그대로
    assert redirect(None, "make_video") is None


async def _room(llm):
    r = Room()
    r.llm = llm
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    r.svc.cfg = dataclasses.replace(r.svc.cfg, agent_think="off")
    await r.join(BOSS)
    return r


@test
async def profile_tool_is_sent_back_for_video_request():
    old = fast_timers()
    try:
        llm = ScriptedLLM([tool_call("make_profile_video", {"request": "x"}), reply("영상 AI 로 만들게요")])
        r = await _room(llm)
        await r.say(BOSS, "소담아 고양이가 춤추는 동영상 만들어줘")
        second = llm.of("chat")[1]["messages"]
        tool_msgs = [m["content"] for m in second if m.get("role") == "tool"]
        assert tool_msgs and "make_video" in tool_msgs[-1]                      # 움프를 안 만들고 돌려보냄
    finally:
        restore_timers(old)


@test
async def ambiguous_becomes_ump_when_room_has_no_video_tool():
    old = fast_timers()
    try:
        seen = {}
        from sodam.panels import avatar

        async def fake_ump(ctx, a):
            seen["intent"] = ctx.media_intent
            return "움프 파일을 방에 보냈음."
        orig = avatar.t_make_profile_video
        tool = next(t for t in __import__("sodam.tools", fromlist=["TOOLS"]).TOOLS if t.name == "make_profile_video")
        orig_fn, tool.fn = tool.fn, fake_ump
        try:
            llm = ScriptedLLM([tool_call("make_profile_video", {"request": "x"}), reply("보냈어요")])
            r = await _room(llm)
            await r.say(BOSS, "소담아 이거 움직이게 만들어줘")
            assert seen.get("intent") == "ump"                                   # 영상 도구 없는 방: 물을 것 없이 움프
        finally:
            tool.fn = orig_fn
        assert avatar.t_make_profile_video is orig
    finally:
        restore_timers(old)


if __name__ == "__main__":
    run_all()
