"""사진 읽기(고화질)·이미지 만들기/고치기·크레딧 소진 안내: python tests/run_all.py vision"""
import asyncio
import base64
import sys
from types import SimpleNamespace

import httpx
from fake_llm import Room, reply, tool_call
from fakes import FakeMsg, fake_user, runner
from openai import BadRequestError, RateLimitError

from sodam import handlers, vision

test, run_all = runner()
BOSS = fake_user(1, "방장", "boss")
SMALL, BIG = b"small-jpeg", b"BIG-JPEG-2560px"


def photo_sizes():
    """텔레그램은 작은 해상도부터 준다."""
    return (SimpleNamespace(file_id="p-small", file_size=len(SMALL)), SimpleNamespace(file_id="p-big", file_size=len(BIG)))


async def room():
    r = await Room().open(admins=(BOSS.id,), settings={"captcha_enabled": False})
    await r.join(BOSS)
    r.bot.files = {"p-small": SMALL, "p-big": BIG}
    return r


def group_msg(r, text="", caption=None, photo=(), reply_to=None):
    r._mid += 1
    m = FakeMsg(r.CHAT, BOSS, text, caption=caption, photo=photo, message_id=r._mid, reply_to=reply_to)
    m.chat = SimpleNamespace(id=r.CHAT, title="방", type="supergroup")
    m.sender_chat = None
    return m


async def say(r, m):
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)
    await r.settle()
    return m


def image_parts(call):
    content = call["messages"][-1]["content"] if call["messages"][-1]["role"] == "user" else \
        next(x["content"] for x in call["messages"] if x["role"] == "user")
    return [c for c in content if isinstance(c, dict) and c.get("type") == "image_url"] if isinstance(content, list) else []


@test
async def photo_with_caption_is_read_in_high_detail_largest_size():
    r = await room()
    r.llm.script = ["커피잔 사진이네요"]
    m = await say(r, group_msg(r, caption="소담아 이거 뭐야", photo=photo_sizes()))
    parts = image_parts(r.llm.of("chat")[0])
    assert len(parts) == 1 and parts[0]["image_url"]["detail"] == "high"
    assert parts[0]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(BIG).decode()   # 가장 큰 것
    assert ("get_file", "p-small") not in r.bot.calls and m.replies[-1] == "커피잔 사진이네요"


@test
async def reply_to_photo_is_read_and_plain_text_has_no_image():
    r = await room()
    r.llm.script = ["표 내용은 이래요", "네 안녕하세요"]
    shot = SimpleNamespace(from_user=fake_user(5, "하나"), text=None, caption=None, photo=photo_sizes(),
                           document=None, message_id=3, forward_origin=None)
    await say(r, group_msg(r, text="소담아 이 표 정리해줘", reply_to=shot))
    await say(r, group_msg(r, text="소담아 안녕"))
    first, second = r.llm.of("chat")
    assert len(image_parts(first)) == 1 and not image_parts(second)
    assert isinstance(second["messages"][-1]["content"], str)                # 사진 없으면 예전 그대로 (캐시 동일)


@test
def image_documents_are_read_but_not_pdf():
    doc = SimpleNamespace(photo=(), document=SimpleNamespace(file_id="d1", mime_type="image/png", file_size=10))
    pdf = SimpleNamespace(photo=(), document=SimpleNamespace(file_id="d2", mime_type="application/pdf", file_size=10))
    assert vision._file_of(doc) == ("d1", "image/png", 10) and vision._file_of(pdf) is None


@test
async def too_big_photo_is_skipped():
    huge = SimpleNamespace(photo=(SimpleNamespace(file_id="h", file_size=vision.MAX_BYTES + 1),), reply_to_message=None)

    class NoDownload:
        async def get_file(self, fid):
            raise AssertionError("큰 파일은 내려받지 않음")
    assert await vision.fetch(NoDownload(), huge) is None


@test
async def make_new_image_sends_photo_and_counts():
    r = await room()
    await r.db.set_setting(r.CHAT, "image_daily", 1)
    r.llm.script = [tool_call("make_image", {"prompt": "노을 지는 바다, 수채화", "mode": "new"}), "그려봤어요!",
                    tool_call("make_image", {"prompt": "또", "mode": "new"}), "오늘은 끝이에요"]
    await say(r, group_msg(r, text="소담아 노을 바다 그림 그려줘"))
    photos = r.bot.named("send_photo")
    assert len(photos) == 1 and photos[0][2] == b"\x89PNG-fake" and "방장" in photos[0][3]
    img = r.llm.of("image")[0]
    assert img["source"] is None and img["chat_id"] == r.CHAT and "노을" in img["prompt"]
    member = fake_user(7, "멤버")
    await r.join(member)
    m = group_msg(r, text="소담아 하나 더")
    m.from_user = member
    await say(r, m)
    assert len(r.bot.named("send_photo")) == 1                                # 하루 한도 1장
    last_tool = [x["content"] for x in r.llm.of("chat")[-1]["messages"] if x["role"] == "tool"][-1]
    assert "한도" in last_tool
    assert "오늘 1장 만듦" in last_tool, last_tool                                # 몇 장 썼는지도 (2026-09-30 벳블리)
    await r.db.set_setting(r.CHAT, "image_daily", 2)                          # 관리자가 한도를 올리면 바로 다시 됨
    r.llm.script = [tool_call("make_image", {"prompt": "또", "mode": "new"}), "그렸어"]
    m = group_msg(r, text="소담아 다시 그려줘")
    m.from_user = member
    await say(r, m)
    assert len(r.bot.named("send_photo")) == 2
    desc = [t for t in r.llm.of("chat")[-1]["tools"] if t["function"]["name"] == "make_image"][0]["function"]["description"]
    assert "다시 부른다" in desc, "앞에서 막혔어도 기억으로 '막혔다' 하지 말고 도구를 다시 부르라는 안내"


@test
async def edit_uses_attached_photo_and_needs_one():
    r = await room()
    r.llm.script = [tool_call("make_image", {"prompt": "배경을 바다로", "mode": "edit"}), "바꿨어요",
                    tool_call("make_image", {"prompt": "배경을 바다로", "mode": "edit"}), "사진이 필요해요"]
    await say(r, group_msg(r, caption="소담아 배경 바다로 바꿔줘", photo=photo_sizes()))
    src = r.llm.of("image")[0]["source"]
    assert isinstance(src, vision.Attached) and src.data == BIG
    await say(r, group_msg(r, text="소담아 배경 바다로 바꿔줘"))               # 사진 없음
    assert len(r.llm.of("image")) == 1
    last_tool = [x["content"] for x in r.llm.of("chat")[-1]["messages"] if x["role"] == "tool"][-1]
    assert "고칠 사진이 없음" in last_tool


@test
async def moderation_block_is_explained_and_nothing_sent():
    r = await room()
    req = httpx.Request("POST", "https://api.openai.com/v1/images/generations")
    r.llm.image_result = BadRequestError("blocked", response=httpx.Response(400, request=req),
                                         body={"code": "moderation_blocked"})
    r.llm.script = [tool_call("make_image", {"prompt": "x", "mode": "new"}), "그건 못 그려요"]
    await say(r, group_msg(r, text="소담아 그려줘"))
    assert not r.bot.named("send_photo")
    last_tool = [x["content"] for x in r.llm.of("chat")[-1]["messages"] if x["role"] == "tool"][-1]
    assert "안전 정책" in last_tool
    assert await r.db.counter(__import__("datetime").datetime.now(r.svc.cfg.tz).strftime("%Y-%m-%d"), r.CHAT, "image") == 0


@test
async def image_tool_hidden_when_disabled():
    r = await room()
    await r.db.set_setting(r.CHAT, "image_daily", 0)
    r.llm.script = ["네"]
    await say(r, group_msg(r, text="소담아 그림 그려줘"))
    call = r.llm.of("chat")[0]
    # 목록엔 남기고(방마다 같은 목록 → 프롬프트 캐시 유지) 부를 수 있는 목록(allowed_tools)에서만 뺌
    assert "make_image" in [t["function"]["name"] for t in call["tools"]]
    assert call["allowed"] is not None and "make_image" not in call["allowed"]


@test
async def out_of_credit_tells_users_and_owner_once():
    r = await room()
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    err = RateLimitError("You have no credits remaining", response=httpx.Response(429, request=req),
                         body={"code": "credit_balance_exhausted", "type": "insufficient_quota"})

    def boom(messages):
        raise err
    reports = []
    orig = r.svc.mod.report

    async def report(bot, text):
        reports.append(text)
    r.svc.mod.report = report
    handlers._credit_reported = 0.0
    r.llm.script = [boom, boom]
    m1 = await say(r, group_msg(r, text="소담아 안녕"))
    m2 = await say(r, group_msg(r, text="소담아 안녕2"))
    assert "바닥" in m1.replies[-1] and "바닥" in m2.replies[-1]
    assert len([x for x in reports if "크레딧" in x]) == 1                   # 운영자 알림은 한 번만
    r.svc.mod.report = orig


@test
async def private_photo_without_text_is_analyzed():
    r = await room()
    r.llm.script = ["영수증이네요, 합계 12,000원"]
    m = FakeMsg(BOSS.id, BOSS, "", photo=photo_sizes(), message_id=900)
    m.chat = SimpleNamespace(id=BOSS.id, type="private")
    m.forward_origin = None
    await handlers.on_private(SimpleNamespace(message=m), r.ctx)
    await r.settle()
    call = r.llm.of("chat")[0]
    assert len(image_parts(call)) == 1 and m.replies[-1].startswith("영수증")


@test
async def llm_image_calls_openai_with_right_models_and_records_tokens():
    from fakes import cfg, make_db
    from sodam.llm import LLM
    db = await make_db()
    llm = LLM(cfg(db.path, openai_api_key="sk-test"), db)
    calls = []
    out = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"IMG").decode())],
                          usage=SimpleNamespace(total_tokens=1234, input_tokens=100, input_tokens_details=None))

    async def generate(**kw):
        calls.append(("generate", kw))
        return out

    async def edit(**kw):
        calls.append(("edit", kw))
        return out
    llm.client = SimpleNamespace(images=SimpleNamespace(generate=generate, edit=edit))
    assert await llm.image("바다", chat_id=-5) == b"IMG"
    assert await llm.image("파랗게", vision.Attached(b"src", "image/png"), chat_id=-5) == b"IMG"
    (k1, g), (k2, e) = calls
    assert k1 == "generate" and g["model"] == llm.cfg.image_model and g["size"] == "1024x1024"
    assert k2 == "edit" and e["model"] == llm.cfg.image_edit_model and e["image"] == ("photo.png", b"src", "image/png")
    assert "input_fidelity" not in e                         # sunburst 는 이 인자를 받으면 400
    from sodam.llm import ROOM_TOKENS
    assert await db.counter(llm._today(), -5, ROOM_TOKENS) == 2468                  # 방 토큰 한도에 포함


@test
async def upload_status_kept_while_drawing_and_stopped_after():
    r = await room()
    from sodam import tools
    orig_sleep = tools.asyncio.sleep
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow_image(prompt, source=None, chat_id=None):
        started.set()
        await gate.wait()                       # 그리는 중 …
        return b"PNG"

    async def fast_sleep(d):
        await orig_sleep(0)
    tools.asyncio.sleep = fast_sleep
    r.llm.image = slow_image
    r.llm.script = [tool_call("make_image", {"prompt": "고양이", "mode": "new"}), "보냈어요"]
    task = asyncio.create_task(say(r, group_msg(r, text="소담아 고양이 그려줘")))
    await asyncio.wait_for(started.wait(), 5)
    await orig_sleep(0.02)                      # 그리는 동안 표시가 여러 번 갱신되는지
    during = len([c for c in r.bot.calls if c[0] == "chat_action" and c[2] == "upload_photo"])
    gate.set()
    await task
    count = lambda: len([c for c in r.bot.calls if c[0] == "chat_action" and c[2] == "upload_photo"])  # noqa: E731
    done = count()
    await orig_sleep(0.02)
    tools.asyncio.sleep = orig_sleep
    assert during >= 2 and count() == done, (during, done, count())   # 끝나면 멈춤


@test
async def reply_to_drawn_image_continues_without_call_name():
    r = await room()
    photo_ids = []
    orig = r.bot.send_photo

    async def send_photo(chat_id, photo, caption=None, **kw):
        m = await orig(chat_id, photo, caption, **kw)
        photo_ids.append(m.message_id)
        return m
    r.bot.send_photo = send_photo
    r.llm.script = [tool_call("make_image", {"prompt": "노을 바다", "mode": "new"}), "그려봤어요", "더 밝게 해볼게요"]
    await say(r, group_msg(r, text="소담아 노을 바다 그려줘"))
    drawn = SimpleNamespace(from_user=r.bot_user(), text=None, caption="🎨 방장님 요청", photo=(), document=None,
                            message_id=photo_ids[0], forward_origin=None)
    m = await say(r, group_msg(r, text="더 밝게", reply_to=drawn))           # 호출어 없이 그림에 답장
    assert m.replies and m.replies[-1] == "더 밝게 해볼게요"


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)


# ── 영상 읽기 (2026-09-28 실제 사례: 글 없는 영상에 답장해 '저렇게 만들어줘' → 영상을 못 보고 프사로 만듦) ──


@test
async def reply_to_video_without_text_shows_frames_and_says_what_it_is():
    from test_avatar import pattern
    from sodam import avatar
    r = await room()
    mp4 = await avatar.make(pattern(), avatar.Spec("pan", "fast"))
    r.bot.files["vid"] = mp4
    clip = SimpleNamespace(from_user=fake_user(5, "하나"), text=None, caption=None, photo=(), document=None, message_id=3,
                           forward_origin=None, animation=None, video_note=None, sticker=None,
                           video=SimpleNamespace(file_id="vid", file_size=len(mp4), duration=6, mime_type="video/mp4",
                                                 thumbnail=None))
    r.llm.script = ["어두운 배경에 반짝이가 흩날리는 영상이네요"]
    await say(r, group_msg(r, text="소담아 저렇게 영상 만들어줘", reply_to=clip))
    call = r.llm.of("chat")[0]
    parts = image_parts(call)
    assert 3 <= len(parts) <= 8, len(parts)                                  # 장면 여러 장
    body = str(call["messages"])
    assert "[영상 6초]" in body and "장면" in body and "초]" in body, "답장 대상이 영상이라는 것 + 장면 시각"


@test
async def big_video_uses_thumbnail_and_says_so():
    r = await room()
    r.bot.files["thumb"] = b"thumb-jpeg"
    big = SimpleNamespace(from_user=fake_user(5, "하나"), text=None, caption=None, photo=(), document=None, message_id=4,
                          forward_origin=None, animation=None, video_note=None, sticker=None,
                          video=SimpleNamespace(file_id="huge", file_size=50 * 1024 * 1024, duration=120, mime_type="video/mp4",
                                                thumbnail=SimpleNamespace(file_id="thumb")))
    m = group_msg(r, text="이거 뭐야", reply_to=big)
    att = await vision.fetch(r.bot, m)
    assert att and att.data == b"thumb-jpeg" and "20MB" in att.note and ("get_file", "huge") not in r.bot.calls
    assert "20MB" in str(att.parts())


@test
def sticker_or_video_alone_in_dm_is_not_a_question():
    st = SimpleNamespace(photo=(), sticker=SimpleNamespace(file_id="s", is_video=True, is_animated=False, file_size=10, thumbnail=None))
    assert vision.has_image(st) and not vision.has_photo(st)


@test
async def new_drawing_becomes_source_for_profile_video_in_same_answer():
    """'새 그림 만들어서 저렇게 영상으로' → 그린 그림이 같은 답변의 움프 원본 (예전엔 프사로 만들었음)."""
    r = await room()
    asked = []

    async def photos(uid, limit=1, **kw):
        asked.append(uid)
        return SimpleNamespace(photos=[])
    r.bot.get_user_profile_photos = photos
    r.llm.script = [tool_call("make_image", {"prompt": "밤하늘 반짝이", "mode": "new"}),
                    tool_call("make_profile_video", {"motion": "zoom"}, call_id="c2"), "만들었어요"]
    await say(r, group_msg(r, text="소담아 새 그림 만들어서 움프로 만들어줘"))
    results = [x["content"] for x in r.llm.of("chat")[-1]["messages"] if x["role"] == "tool"]
    assert len(results) == 2 and "원본" in results[0], results
    assert not asked and "프사" not in results[1], (asked, results[1])     # 프사를 찾지 않고 방금 그림으로
