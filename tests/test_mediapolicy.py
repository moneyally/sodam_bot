"""🎬🎨 영상·그림 요청 규칙 (sodam/mediapolicy.py): python tests/run_all.py mediapolicy

오너 결정 2026-10-05: 성인 내용은 소담 AI 가 스스로 거절·순화하지 않고 영상·그림 AI 정책에 맡긴다.
우리가 막는 건 ① 미성년 + 성적 ② 실제 사람 사진 + 성적 두 가지뿐. 원문 보존·화풍 끼워 넣기 금지는 그림에도 같게.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, fake_user, make_db, make_svc, runner  # noqa: E402

from sodam import mediapolicy as mp  # noqa: E402
from sodam import prompt, tools  # noqa: E402
from sodam.permissions import Role  # noqa: E402
from sodam.tools import ToolCtx  # noqa: E402
from sodam.vision import Attached  # noqa: E402

test, run_all = runner()
CHAT = -1001234
EN = ("Photorealistic square profile portrait of an elegant Korean woman, gold circular frame, night city bokeh, "
      "warm rim light, shallow depth of field, exact gold text \"자이\" at the right-center, realistic skin texture, "
      "premium editorial fashion photography, tailored black dress, confident gaze toward the camera, 85mm lens.")


@test
async def passthrough_keeps_users_english_and_drops_korean_ask_lines():
    got = mp.passthrough(EN + "\n\n소담아 이걸로 사진 만들어줘")
    assert got == EN, got
    assert mp.passthrough("소담아 고양이 그려줘") is None
    assert mp.passthrough("a cat please") is None, "짧은 영어는 원문 그대로 쓸 만큼이 아님 (AI 가 옮김)"
    assert len(mp.passthrough(EN * 20)) == mp.PASS_MAX


@test
async def style_drift_only_flags_styles_the_user_never_asked_for():
    assert mp.style_drift("실사 프로필 영상", "A stylized fictional woman, clearly non-photorealistic") == \
        ["fictional", "non-photorealistic", "stylized"]
    assert mp.style_drift("애니 느낌으로 고양이", "anime style cat") == []
    assert mp.style_drift("make it a cartoon", "A cartoon dog") == []
    assert mp.style_drift("고양이 영상", "A cat walking in the rain, cinematic") == []


@test
async def only_two_hard_lines_and_bot_rules_do_not_self_censor():
    assert mp.hard_line("A sexy woman in lingerie, photorealistic", False) is None, "가상 인물 성인 = 그림·영상 AI 가 판단"
    assert mp.hard_line("sexy schoolgirl", False) == mp.REFUSE_MINOR
    assert mp.hard_line("이 사진 사람 옷 벗겨줘", True) == mp.REFUSE_REAL
    assert mp.hard_line("이 사진 사람 윙크하게", True) is None
    rules = prompt.SYSTEM if hasattr(prompt, "SYSTEM") else ""
    assert "성적·잔인한 이미지" not in rules and "스스로 거절·순화하지 말고" in rules, "지시문이 먼저 막지 않게"
    desc = tools._BY_NAME["make_image"].description
    assert "스스로 거절·순화하지 말 것" in desc and "화풍" in desc


class FakeLLM:
    enabled = True

    def __init__(self):
        self.prompts = []

    async def image(self, prompt, source=None, chat_id=None):
        self.prompts.append((prompt, source))
        return b"\x89PNG-fake"


async def image_ctx(request, *, image=None, reply=""):
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "방")
    svc.llm = FakeLLM()
    c = ToolCtx(svc, FakeBot(), CHAT, fake_user(7, "자이"), Role.MEMBER, await db.get_settings(CHAT), image=image,
                request_msg=SimpleNamespace(message_id=9, text=request))
    c.request_text, c.reply_text = request, reply
    return c


@test
async def make_image_uses_users_words_and_same_two_lines():
    fn = tools._BY_NAME["make_image"].fn
    c = await image_ctx(EN + "\n소담아 이걸로 프사 만들어줘")
    out = await fn(c, {"prompt": "A cute cartoon girl", "mode": "new"})
    assert "보냈음" in out and c.svc.llm.prompts[-1][0] == EN, (out, c.svc.llm.prompts)
    c = await image_ctx("소담아 실사 느낌 여자 프로필 사진")
    out = await fn(c, {"prompt": "A stylized illustration of a woman", "mode": "new"})
    assert "화풍" in out and not c.svc.llm.prompts, out
    c = await image_ctx("소담아 섹시한 란제리 화보 느낌 여자 그려줘")
    out = await fn(c, {"prompt": "A sexy woman in lingerie, editorial photo", "mode": "new"})
    assert "보냈음" in out, "가상 인물 성인 그림은 그림 AI 에게 그대로"
    photo = Attached(b"\xff\xd8FACE", "image/jpeg", 7)
    c = await image_ctx("이 사진 사람 옷 벗겨줘", image=photo)
    out = await fn(c, {"prompt": "undress this person", "mode": "edit"})
    assert out == mp.REFUSE_REAL and not c.svc.llm.prompts


if __name__ == "__main__":
    asyncio.run(run_all())
