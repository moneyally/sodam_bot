"""소담 답 언어 (2026-10-22 오너 '외국인한테 한국어로 가는 문제'): 방 설정 ai_reply_lang
auto = 손님이 쓴 언어로 (기본, 모든 방) · ko = 항상 한국어. sodam/prompt.py + ai_settings.py."""
from fakes import fake_user, runner

from sodam import prompt, settings
from sodam.prompt import build_messages

test, run_all = runner()


def _msgs(**kw):
    return build_messages(bot_name="소담", bot_id=999, style_key="polite", tz=None,
                          caller=fake_user(5, "철수"), role_label="member", notes={},
                          history=[], reply_to=None, request="hello", **kw)


def _user_text(msgs):
    u = msgs[-1]["content"]
    return u if isinstance(u, str) else u[0]["text"]


@test
async def setting_default_auto():
    assert settings.DEFAULTS["ai_reply_lang"] == "auto"


@test
async def fixed_rule_says_reply_in_their_language():
    # 고정 규칙(모든 방 공용)에 '상대가 쓴 언어로' — auto 가 기본이라 캐시 공용
    assert "상대가 지금 쓴 언어로" in prompt.SYSTEM


@test
async def auto_adds_no_korean_override():
    txt = _user_text(_msgs(reply_lang="auto"))
    assert "반드시 한국어" not in txt, txt


@test
async def ko_forces_korean_at_tail():
    txt = _user_text(_msgs(reply_lang="ko"))
    assert "어느 나라 말로 하든 반드시 한국어" in txt, txt


@test
async def default_param_is_auto():
    # reply_lang 안 주면 auto (한국어 강제 안 붙음)
    assert "반드시 한국어" not in _user_text(_msgs())


@test
async def ko_in_choices():
    from sodam.settings import coerce
    assert coerce("ai_reply_lang", "한국어") == "ko" and coerce("ai_reply_lang", "자동") == "auto"


if __name__ == "__main__":
    run_all()
