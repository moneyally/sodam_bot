"""🏷 화이트라벨 봇 이름 틀 — python tests/run_all.py brandname

- 보내는 글의 '소담' → 그 봇 이름, 받침에 맞게 조사 (소담아→프레스티지야, 소담이가→프레스티지가, 소담은→프레스티지는)
- 받침 있는 이름은 조사 그대로 (소담이가 → 하늘이가)
- 본체('소담')면 아무것도 안 함 · 엔티티 붙은 글은 안 건드림 · 인라인 버튼·미디어 설명도 바꿈
"""
import asyncio

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, MessageEntity

from fakes import runner

from sodam import brandname

test, run_all = runner()
N = "프레스티지"


@test
async def josa_follows_new_name():
    r = lambda t: brandname.rename(t, N)     # noqa: E731
    assert r("소담아 안녕") == "프레스티지야 안녕"
    assert r("소담이가 도와줄게") == "프레스티지가 도와줄게"
    assert r("소담이 왔어") == "프레스티지가 왔어"
    assert r("소담이네 방") == "프레스티지네 방"
    assert r("소담은 AI 비서예요") == "프레스티지는 AI 비서예요"
    assert r("소담을 불러") == "프레스티지를 불러"
    assert r("소담과 대화") == "프레스티지와 대화"
    assert r("소담으로 바꿔") == "프레스티지로 바꿔"
    assert r("저는 소담이에요") == "저는 프레스티지예요"
    assert r("소담이한테 물어봐") == "프레스티지한테 물어봐"
    assert r("📘 소담 공식 안내서") == "📘 프레스티지 공식 안내서"
    assert r("소담의 기능") == "프레스티지의 기능"
    assert r("@SoDam33_bot 링크") == "@SoDam33_bot 링크"      # 영문 아이디는 그대로


@test
async def batchim_name_keeps_josa():
    r = lambda t: brandname.rename(t, "하늘")     # noqa: E731
    assert r("소담아 안녕") == "하늘아 안녕"
    assert r("소담이가 도와줄게") == "하늘이가 도와줄게"
    assert r("소담은") == "하늘은"


@test
async def main_bot_unchanged():
    assert brandname.rename("소담아 안녕", "소담") == "소담아 안녕"
    assert brandname.rename(None, N) is None


@test
async def send_path_rewrites_text_buttons_and_media():
    sent = []

    async def fake_post(self, endpoint, data=None, **kw):
        sent.append((endpoint, data))
        return True

    saved = (Bot._post, brandname._NAME, brandname._INSTALLED)
    Bot._post = fake_post
    brandname._INSTALLED = False
    try:
        brandname.install(N)
        bot = Bot("123:abc")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📘 소담 안내서", callback_data="m:g")]])
        await Bot._post(bot, "sendMessage", {"text": "소담이가 왔어요", "reply_markup": kb})
        assert sent[-1][1]["text"] == "프레스티지가 왔어요"
        assert kb.inline_keyboard[0][0].text == "📘 프레스티지 안내서"
        # 엔티티 붙은 글은 위치가 틀어지니 그대로
        ent = [MessageEntity("bold", 0, 2)]
        await Bot._post(bot, "sendMessage", {"text": "소담아", "entities": ent})
        assert sent[-1][1]["text"] == "소담아"
        media = [InputMediaPhoto("file_id", caption="소담 사진")]
        await Bot._post(bot, "sendMediaGroup", {"media": media})
        assert media[0].caption == "프레스티지 사진"
        # 본체 이름이면 끔
        brandname.install("소담")
        await Bot._post(bot, "sendMessage", {"text": "소담아"})
        assert sent[-1][1]["text"] == "소담아"
    finally:
        Bot._post, brandname._NAME, brandname._INSTALLED = saved


@test
async def each_bot_object_can_have_its_own_name():
    """음성 담당 한 프로세스가 본체 봇·클론 봇 글을 같이 보냄 → 봇 객체마다 이름 (voice/tenants.py).
    뮤테이션: _post 가 봇별 이름을 안 보면 FAIL."""
    sent = []

    async def fake_post(self, endpoint, data=None, **kw):
        sent.append(data["text"])
        return True
    saved = (Bot._post, brandname._NAME, brandname._INSTALLED, dict(brandname._BOT_NAMES))
    Bot._post = fake_post
    brandname._INSTALLED, brandname._NAME = False, None
    try:
        main, clone = Bot("1:a"), Bot("2:b")
        brandname.set_bot_name(clone, N)
        brandname.set_bot_name(main, "소담")          # 본체 이름은 등록 안 됨 (그대로)
        await Bot._post(main, "sendMessage", {"text": "소담이 노래 틀어요"})
        await Bot._post(clone, "sendMessage", {"text": "소담이 노래 틀어요"})
        assert sent == ["소담이 노래 틀어요", "프레스티지가 노래 틀어요"], sent
    finally:
        Bot._post, brandname._NAME, brandname._INSTALLED = saved[:3]
        brandname._BOT_NAMES.clear()
        brandname._BOT_NAMES.update(saved[3])


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
