"""🎬 AI 영상 만들기 (make_video, sodam/video.py · panels/videogen.py): python tests/run_all.py test_videogen

오너 결정 2026-09-30: 키는 오너가 직접(GEMINI_API_KEY / XAI_API_KEY), 방마다 한 주 6개(한국시간 월요일 0시 초기화), 관리자는 줄이기만.
네트워크 없이 httpx.MockTransport 로 공식 문서의 요청·응답 모양(시작 → 확인 → 내려받기)을 흉내 낸다.
"""
import asyncio
import base64
import json
import os
from datetime import datetime
from types import SimpleNamespace

import httpx
from fakes import FakeBot, add_member, fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import costs, tools, video
from sodam.llm import LLM
from sodam.panels import checkup, videogen
from sodam.permissions import Role
from sodam.settings import OWNER_CAP, over_cap
from sodam.tools import ToolCtx
from sodam.vision import Attached

test, run_all = runner()
CHAT = -100888
USER, OWNER, ADMIN = fake_user(5, "민수"), fake_user(1, "오너"), fake_user(2, "방장")
GKEY, XKEY = "AIzaTESTgeminiKEY123", "xai-TESTkey456"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 100
ENV = ("GEMINI_API_KEY", "XAI_API_KEY", "VIDEO_PROVIDER", "VIDEO_MODEL")


def set_env(**kw):
    for k in ENV:
        os.environ.pop(k, None)
    os.environ.update(kw)


class Bot(FakeBot):
    photos: dict = {}

    async def get_user_profile_photos(self, uid, limit=1):
        if uid not in self.photos:
            return SimpleNamespace(photos=[])
        self.files[f"p{uid}"] = self.photos[uid]
        return SimpleNamespace(photos=[[SimpleNamespace(file_id=f"p{uid}")]])

    async def send_chat_action(self, chat_id, action, **kw):
        self.calls.append(("action", chat_id, action))


class Api:
    """가짜 제공자 서버. mode: ok · pending(끝나지 않음) · policy · http400 · rai(xai 는 respect_moderation false)."""

    def __init__(self, mode="ok", polls_pending=1):
        self.mode, self.left = mode, polls_pending
        self.reqs: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.reqs.append(req)
        u = str(req.url)
        if req.method == "POST" and u.endswith(":predictLongRunning"):
            if self.mode == "http400":
                return httpx.Response(400, json={"error": {"message": f"bad key {GKEY} safety filter blocked"}})
            return httpx.Response(200, json={"name": "models/veo-3.1-lite-generate-preview/operations/op1"})
        if u.endswith("/operations/op1"):
            if self.mode == "pending" or self.left > 0:
                self.left -= 1
                return httpx.Response(200, json={"name": "op1", "done": False})
            if self.mode == "policy":
                return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {
                    "raiMediaFilteredCount": 1, "raiMediaFilteredReasons": ["sexual content"]}}})
            return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {"generatedSamples": [
                {"video": {"uri": "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"}}]}}})
        if "files/abc:download" in u:   # 실제처럼 다른 호스트(저장소)로 넘김 → 키 헤더는 떼야 함
            return httpx.Response(302, headers={"location": "https://storage.googleusercontent.com/v/abc.mp4"})
        if u == "https://storage.googleusercontent.com/v/abc.mp4":
            return httpx.Response(200, content=MP4)
        if req.method == "POST" and json.loads(req.content or b"{}").get("model") in getattr(self, "fail_models", ()):
            return httpx.Response(500, json={"error": "internal"})
        if req.method == "POST" and u in ("https://api.x.ai/v1/videos/generations", "https://api.x.ai/v1/videos/edits",
                                          "https://api.x.ai/v1/videos/extensions"):
            return httpx.Response(200, json={"request_id": "r1"})
        if u == "https://api.x.ai/v1/videos/r1":
            if self.mode == "pending" or self.left > 0:
                self.left -= 1
                return httpx.Response(202, json={"status": "pending", "progress": 40})
            if self.mode == "rai":
                return httpx.Response(200, json={"status": "done", "video": {"url": None, "duration": 6,
                                                                             "respect_moderation": False}})
            if self.mode == "policy":
                return httpx.Response(200, json={"status": "failed", "error": {
                    "code": "invalid_argument", "message": "content blocked by moderation"}})
            return httpx.Response(200, json={"status": "done", "model": "grok-imagine-video",
                                             "video": {"url": "https://vidgen.x.ai/abc/video.mp4", "duration": 6,
                                                       "respect_moderation": True}})
        if u == "https://vidgen.x.ai/abc/video.mp4":
            return httpx.Response(200, content=MP4)
        return httpx.Response(404, text="no route " + u)

    def body(self, i=0):
        return json.loads([r for r in self.reqs if r.method == "POST"][i].content)


async def world(api=None, *, env=None, settings=None):
    set_env(**(env if env is not None else {"GEMINI_API_KEY": GKEY}))
    video.TRANSPORT = httpx.MockTransport(api or Api())
    video.POLL_GEMINI = video.POLL_XAI = 0.001
    video.TIMEOUT_SEC = 5
    videogen.RUNNING.clear()
    db = await make_db()
    svc = await make_svc(db, admins={2})
    svc.llm = LLM(svc.cfg, db)
    svc.perms.owner_ids = {1}
    bot = Bot()
    Bot.photos = {}
    FakeBot.files = {}
    await db.ensure_chat(CHAT, "대표님들 소통방")
    for u in (USER, OWNER, ADMIN):
        await add_member(db, CHAT, u)
    for k, v in (settings or {}).items():
        await db.set_setting(CHAT, k, v)
    return svc, bot


async def ctx(svc, bot, user=USER, role=Role.MEMBER, image=None):
    return ToolCtx(svc, bot, CHAT, user, role, await svc.db.get_settings(CHAT), image=image,
                   request_msg=SimpleNamespace(message_id=55, text="소담아 영상 만들어줘"))


async def finish():
    for t in list(videogen.RUNNING.values()):
        await asyncio.gather(t, return_exceptions=True)


async def run(c, **a):
    out = await tools._BY_NAME["make_video"].fn(c, {"prompt": "A corgi surfing a big wave at sunset, waves crashing",
                                                   "mode": "text", **a})
    await finish()
    return out


def week(svc):
    return videogen.week_start(svc.cfg.tz)


@test
async def gemini_start_poll_download_sends_video_and_charges_per_second():
    api = Api(polls_pending=2)
    svc, bot = await world(api)
    c = await ctx(svc, bot)
    out = await run(c, aspect="9:16")
    assert "시작" in out and c.quiet, out
    body = api.body()
    assert body["instances"][0]["prompt"].startswith("A corgi") and "image" not in body["instances"][0]
    assert body["parameters"] == {"durationSeconds": 6, "aspectRatio": "9:16"}, body   # 방 기본 6초, int
    start = api.reqs[0]
    assert start.url.path == "/v1beta/models/veo-3.1-lite-generate-preview:predictLongRunning"
    assert start.headers["x-goog-api-key"] == GKEY
    polls = [r for r in api.reqs if r.url.path.endswith("/operations/op1")]
    assert len(polls) == 3 and all(r.headers["x-goog-api-key"] == GKEY for r in polls)
    store = [r for r in api.reqs if r.url.host == "storage.googleusercontent.com"]
    assert store and "x-goog-api-key" not in store[0].headers, "다른 호스트로 넘어가면 키를 안 보냄"
    [(_, cid, data, caption, kw)] = bot.named("send_video")
    assert cid == CHAT and data == MP4 and "민수" in caption and kw["reply_parameters"].message_id == 55
    status = bot.named("send_message")[0]
    assert "영상 만드는 중" in status[2] and status[3]["reply_parameters"].message_id == 55
    assert bot.named("delete") == [("delete", CHAT, 1001)], "끝나면 '만드는 중' 글(첫 전송 = 1001) 지움"
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await svc.db.counter(day, CHAT, costs.ROOM_USD) == 300_000, "0.05$/초 × 6초 = $0.30"
    assert await svc.db.counter(day, 0, costs.USD) == 300_000
    assert await svc.db.counter(week(svc), CHAT, videogen.KEY) == 1
    assert await svc.db.counter(day, 0, "video_sec:veo-3.1-lite-generate-preview") == 6


@test
async def xai_image_to_video_uses_replied_photo_and_no_key_to_download_host():
    api = Api()
    svc, bot = await world(api, env={"XAI_API_KEY": XKEY})
    photo = b"\xff\xd8JPEGDATA"
    c = await ctx(svc, bot, image=Attached(photo, "image/jpeg", 7))
    out = await run(c, mode="image", seconds=10, aspect="1:1")
    assert "시작" in out, out
    body = api.body()
    assert body["model"] == "grok-imagine-video-1.5-lite" and body["duration"] == 6 and body["aspect_ratio"] == "1:1", body
    assert body["image"]["url"] == "data:image/jpeg;base64," + base64.b64encode(photo).decode(), "답장한 사진이 첫 장면"
    assert api.reqs[0].headers["authorization"] == f"Bearer {XKEY}"
    dl = [r for r in api.reqs if r.url.host == "vidgen.x.ai"]
    assert dl and "authorization" not in dl[0].headers
    assert len(bot.named("send_video")) == 1
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await svc.db.counter(day, CHAT, costs.ROOM_USD) == 120_000, "라우터 기본 = lite $0.02×6초"


@test
async def router_cheap_by_default_quality_words_go_up_and_lite_failure_retries_once():
    """오너 2026-10-03 '한 모델 말고 라우터로': 기본 lite($0.02), 화질을 말하면 기본 모델($0.05), lite 가 그냥 실패하면 기본 모델로 한 번."""
    prov = video.Provider("xai", XKEY, "grok-imagine-video", tuple(range(1, 16)), ("1:1",))
    set_env(XAI_API_KEY=XKEY)
    assert video.route_model(prov, "고양이 춤추는 영상")[0].model == video.XAI_CHEAP
    assert video.route_model(prov, "영화처럼 고화질로 만들어줘")[0].model == "grok-imagine-video"
    assert video.route_model(prov, "make it cinematic and detailed")[1] == "quality"
    set_env(XAI_API_KEY=XKEY, VIDEO_MODEL="grok-imagine-video-1.5")
    assert video.route_model(prov, "고양이")[1] == "fixed", "VIDEO_MODEL 을 적으면 라우터 끔"
    api = Api()
    api.fail_models = {video.XAI_CHEAP}
    svc, bot = await world(api, env={"XAI_API_KEY": XKEY})
    out = await run(await ctx(svc, bot), prompt="A cat dancing", mode="text")
    assert "시작" in out, out
    models = [json.loads(r.content)["model"] for r in api.reqs if r.method == "POST"]
    assert models == [video.XAI_CHEAP, "grok-imagine-video"], models
    assert len(bot.named("send_video")) == 1
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await svc.db.counter(day, CHAT, costs.ROOM_USD) == 300_000, "실제로 만든 모델 값으로 청구"


@test
async def image_mode_uses_member_profile_photo_via_photo_of():
    api = Api()
    svc, bot = await world(api)
    Bot.photos = {2: b"\xff\xd8ADMINFACE"}
    c = await ctx(svc, bot)
    await run(c, mode="image", photo_of="방장")
    inst = api.body()["instances"][0]
    assert base64.b64decode(inst["image"]["bytesBase64Encoded"]) == b"\xff\xd8ADMINFACE"
    assert inst["image"]["mimeType"] == "image/jpeg" and api.body()["parameters"]["personGeneration"] == "allow_adult"
    svc, bot = await world(Api())
    out = await tools._BY_NAME["make_video"].fn(await ctx(svc, bot), {"prompt": "x", "mode": "image"})
    assert "사진이 없음" in out and not bot.named("send_message"), "원본 없으면 시작 안 함"


@test
async def timeout_tells_user_and_charges_nothing():
    svc, bot = await world(Api("pending"))
    video.TIMEOUT_SEC = 0.05
    out = await run(await ctx(svc, bot))
    assert "시작" in out
    edits = bot.named("edit_text")
    assert edits and "오래 걸려서" in edits[-1][2], bot.calls
    assert not bot.named("send_video")
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await svc.db.counter(day, CHAT, costs.ROOM_USD) == 0 and await svc.db.counter(week(svc), CHAT, videogen.KEY) == 0


@test
async def policy_errors_become_korean_notice_for_both_providers():
    for env, mode in (({"GEMINI_API_KEY": GKEY}, "policy"), ({"XAI_API_KEY": XKEY}, "policy"), ({"XAI_API_KEY": XKEY}, "rai"),
                      ({"GEMINI_API_KEY": GKEY}, "http400")):
        svc, bot = await world(Api(mode, polls_pending=0), env=env)
        await run(await ctx(svc, bot))
        edits = bot.named("edit_text")
        assert edits and "영상 AI 쪽 정책으로 거절됐어요" in edits[-1][2], (env, mode, bot.calls)
        assert not bot.named("send_video") and await svc.db.counter(week(svc), CHAT, videogen.KEY) == 0
        assert all(GKEY not in str(c) for c in bot.calls), "키가 방에 안 나감"


@test
async def missing_key_hides_tool_and_says_so():
    svc, bot = await world(env={})
    s = await svc.db.get_settings(CHAT)
    assert "make_video" not in {t.name for t in tools.available(Role.OWNER, s, False)}
    out = await tools._BY_NAME["make_video"].fn(await ctx(svc, bot), {"prompt": "x", "mode": "text"})
    assert "영상 AI 키가 아직 설정 안 됨 (운영자)" in out
    set_env(GEMINI_API_KEY=GKEY)
    assert "make_video" in {t.name for t in tools.available(Role.MEMBER, s, False)}
    assert "make_video" not in {t.name for t in tools.available(Role.MEMBER, s, True)}, "그룹방에서만"
    await svc.db.set_setting(CHAT, "video_weekly", 0)
    assert "make_video" not in {t.name for t in tools.available(Role.MEMBER, await svc.db.get_settings(CHAT), False)}, "0 = 끔"
    assert video.active().model == "veo-3.1-lite-generate-preview"
    set_env(GEMINI_API_KEY=GKEY, XAI_API_KEY=XKEY, VIDEO_PROVIDER="xai", VIDEO_MODEL="grok-imagine-video-1.5")
    assert (video.active().name, video.active().model) == ("xai", "grok-imagine-video-1.5")
    set_env(XAI_API_KEY=XKEY, VIDEO_MODEL="veo-3.1-fast-generate-preview")
    assert video.active().model == "grok-imagine-video", "다른 회사 모델 이름은 무시"


@test
async def weekly_limit_owner_bypass_and_owner_cap():
    svc, bot = await world()
    await svc.db.bump(week(svc), CHAT, videogen.KEY, 6)
    out = await run(await ctx(svc, bot))
    assert "이번 주 영상 6/6개 (월요일 0시에 초기화)" in out and not bot.named("send_message"), out
    out = await run(await ctx(svc, bot, OWNER, Role.OWNER))
    assert "시작" in out and len(bot.named("send_video")) == 1, "오너는 한도 무시 (요금은 셈)"
    assert await svc.db.counter(week(svc), CHAT, videogen.KEY) == 7
    assert OWNER_CAP["video_weekly"] == 6 and over_cap("video_weekly", 7) and not over_cap("video_weekly", 6)
    assert over_cap("video_seconds", 8) and not over_cap("video_seconds", 6)
    change = tools._BY_NAME["change_setting"].fn
    actx = ToolCtx(svc, bot, CHAT, ADMIN, Role.ADMIN, await svc.db.get_settings(CHAT))
    out = await change(actx, {"key": "video_weekly", "value": "20"})
    assert (await svc.db.get_settings(CHAT))["video_weekly"] == 6, out
    await change(ToolCtx(svc, bot, CHAT, OWNER, Role.OWNER, await svc.db.get_settings(CHAT)), {"key": "video_weekly", "value": "20"})
    assert (await svc.db.get_settings(CHAT))["video_weekly"] == 20


@test
async def week_boundary_is_monday_midnight_kst():
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    sun = datetime(2026, 10, 4, 23, 59, tzinfo=kst)     # 일요일 밤
    mon = datetime(2026, 10, 5, 0, 0, tzinfo=kst)       # 월요일 0시
    assert videogen.week_start(kst, sun) == "2026-09-28" and videogen.week_start(kst, mon) == "2026-10-05"
    assert videogen.week_start(kst, datetime(2026, 9, 30, 12, tzinfo=kst)) == "2026-09-28"
    utc_sun_night = datetime(2026, 10, 4, 15, 30, tzinfo=ZoneInfo("UTC"))   # = 한국 월요일 00:30
    assert videogen.week_start(kst, utc_sun_night.astimezone(kst)) == "2026-10-05"


@test
async def paid_period_and_room_only():
    svc, bot = await world()

    async def inactive(cid):
        return False
    svc.billing = SimpleNamespace(active=inactive)
    out = await run(await ctx(svc, bot))
    assert "이용 기간" in out and not bot.named("send_message"), out
    svc.billing = None
    dm = ToolCtx(svc, bot, 5, USER, Role.MEMBER, await svc.db.get_settings(5))
    assert "그룹방에서만" in await tools._BY_NAME["make_video"].fn(dm, {"prompt": "x", "mode": "text"})


@test
async def budget_exceeded_refuses_before_any_api_call():
    api = Api()
    svc, bot = await world(api)
    day = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    cap = await costs.room_cap_micro(svc.db, CHAT)
    await svc.db.bump(day, CHAT, costs.ROOM_USD, cap - 200_000)   # $0.20 남음 < 영상 $0.30
    out = await run(await ctx(svc, bot))
    assert "한도" in out and not api.reqs and not bot.named("send_message"), out
    svc2, bot2 = await world(api)
    svc2.llm.usd_budget = 0.25   # 전체 하루 예산도
    assert "한도" in await run(await ctx(svc2, bot2))


@test
async def cost_table_per_second():
    assert costs.video_usd_micro("veo-3.1-lite-generate-preview", 6) == 300_000
    assert costs.video_usd_micro("grok-imagine-video-1.5", 8) == 640_000
    assert costs.video_usd_micro("grok-imagine-video", 4) == 200_000
    assert costs.video_usd_micro("veo-9-unknown", 1) == 400_000, "모르는 모델 = 가장 비싼 값"
    p = video.Provider("gemini", "k", "m", (4, 6, 8), ("16:9", "9:16"))
    assert (p.clamp_seconds(5), p.clamp_seconds(8), p.clamp_seconds(3)) == (4, 8, 4)
    assert p.aspect("1:1") is None and p.aspect("9:16") == "9:16"


@test
async def one_per_answer_one_per_room():
    api = Api()
    svc, bot = await world(api)
    c = await ctx(svc, bot)
    fn = tools._BY_NAME["make_video"].fn
    first = await fn(c, {"prompt": "A cat", "mode": "text"})
    again = await fn(c, {"prompt": "A dog", "mode": "text"})
    assert "시작" in first and "한 답변에 하나만" in again
    other = await fn(await ctx(svc, bot, OWNER, Role.OWNER), {"prompt": "A dog", "mode": "text"})
    assert "만드는 중" in other and "태그해서 알려" in other, "방마다 동시에 1개"
    await finish()
    assert len([r for r in api.reqs if r.method == "POST"]) == 1
    await asyncio.sleep(0.05)
    told = [x for x in bot.named("send_message") if "이제 영상 다시" in x[2]]
    assert told and f"id={OWNER.id}" in told[-1][2] and not videogen.WAITING, "기다린 사람 태그"


@test
async def same_room_simultaneous_requests_only_one_starts():
    """코드 리뷰 2026-09-30: 검사(RUNNING)와 등록 사이에 await 가 여럿 → 5/6 인 방에서 동시에 두 번 부르면 둘 다 통과(7/6·요금 두 번)."""
    api = Api()
    svc, bot = await world(api)
    await svc.db.bump(week(svc), CHAT, videogen.KEY, 5)
    fn = tools._BY_NAME["make_video"].fn
    c1, c2 = await ctx(svc, bot), await ctx(svc, bot, ADMIN, Role.ADMIN)
    outs = await asyncio.gather(fn(c1, {"prompt": "A cat", "mode": "text"}), fn(c2, {"prompt": "A dog", "mode": "text"}))
    await finish()
    assert sum("시작" in o for o in outs) == 1 and any("만드는 중" in o for o in outs), outs
    assert len([r for r in api.reqs if r.method == "POST"]) == 1 and len(bot.named("send_video")) == 1
    assert await svc.db.counter(week(svc), CHAT, videogen.KEY) == 6, "7/6 안 됨"
    assert not videogen.RUNNING, "끝나면 자리 풂"
    await svc.db.bump(week(svc), CHAT, videogen.KEY, -6)
    out = await fn(await ctx(svc, bot), {"prompt": "x", "mode": "image"})   # 원본 없음 = 시작 전 거절
    assert "사진이 없음" in out and not videogen.RUNNING, "거절돼도 자리 풂 (다음 요청이 막히지 않게)"
    assert "시작" in await run(await ctx(svc, bot))


@test
async def only_two_hard_lines_rest_goes_to_provider():
    """오너 결정 2026-09-30: 성인 내용은 영상 AI 정책이 판단. 우리는 ① 미성년+성적 ② 실제 사람 사진+성적 만 막음.
    예전 낱말 필터는 '귤 껍질 벗기는 영상'·'노출 과다된 야경' 까지 막았음 (리뷰)."""
    H = videogen.hard_line
    for ok in ("귤 껍질 벗기는 영상", "노출 과다된 야경 타임랩스", "Peeling a tangerine, close-up", "아이들이 공원에서 축구하는 영상",
               "A sexy woman in lingerie dancing on a neon stage"):
        assert H(ok, False) is None, ok
    for ok in ("귤 껍질 벗기는 영상", "노출 과다된 야경 타임랩스", "이 사진 사람이 춤추게"):
        assert H(ok, True) is None, ("사진이어도 성적이 아니면 됨", ok)
    assert H("a naked teen girl", False) == videogen.REFUSE_MINOR
    assert H("여고생 섹시 댄스", False) == videogen.REFUSE_MINOR
    assert H("make her undress slowly", True) == videogen.REFUSE_REAL
    assert H("이 사람 옷 벗기는 영상", True) == videogen.REFUSE_REAL and H("민수 프사로 야한 영상", True)
    api = Api()
    svc, bot = await world(api)
    fn = tools._BY_NAME["make_video"].fn
    out = await fn(await ctx(svc, bot), {"prompt": "A sexy woman in lingerie dancing, fictional character", "mode": "text"})
    await finish()
    assert "시작" in out and len([r for r in api.reqs if r.method == "POST"]) == 1, "가상 인물 성인 내용은 영상 AI 에 그대로"
    Bot.photos = {2: b"\xff\xd8FACE"}
    out = await fn(await ctx(svc, bot), {"prompt": "Make this person undress, sexy", "mode": "image", "photo_of": "방장"})
    assert out == videogen.REFUSE_REAL and len([r for r in api.reqs if r.method == "POST"]) == 1, out
    out = await fn(await ctx(svc, bot), {"prompt": "sexy dance of a schoolgirl", "mode": "text"})
    assert out == videogen.REFUSE_MINOR and not videogen.RUNNING


@test
async def keys_are_redacted_from_errors():
    set_env(GEMINI_API_KEY=GKEY, XAI_API_KEY=XKEY)
    assert GKEY not in video.redact(f"url?key={GKEY} and {XKEY}") and XKEY not in video.redact(XKEY)
    err = video._http_error(httpx.Response(401, text=f"invalid {GKEY}"))
    assert err.kind == "auth" and GKEY not in err.detail
    assert video._http_error(httpx.Response(429, text="RESOURCE_EXHAUSTED")).kind == "quota"


@test
async def checkup_shows_weekly_video_usage():
    svc, bot = await world()
    await svc.db.bump(week(svc), CHAT, videogen.KEY, 2)
    c = ToolCtx(svc, bot, CHAT, OWNER, Role.OWNER, await svc.db.get_settings(CHAT))
    out = await checkup.t_room_checkup(c, {})
    assert "영상 이번 주 2/6개" in out and "키 없음" not in out, out
    set_env()
    assert "영상 AI 키 없음" in await checkup.t_room_checkup(c, {})


_run = run_all


async def run_all():
    """같은 프로세스에서 다른 테스트가 도니까 환경변수·모듈 값을 되돌림 (키가 남으면 다른 테스트 도구 목록이 바뀜)."""
    saved = {k: os.environ.get(k) for k in ENV}
    mod = (video.TRANSPORT, video.POLL_GEMINI, video.POLL_XAI, video.TIMEOUT_SEC)
    try:
        return await _run()
    finally:
        set_env(**{k: v for k, v in saved.items() if v is not None})
        video.TRANSPORT, video.POLL_GEMINI, video.POLL_XAI, video.TIMEOUT_SEC = mod


@test
async def edit_and_extend_existing_video_with_grok():
    """2026-10-03 오너: 그록 API 의 영상 고치기·이어 붙이기 (비용은 만들기와 같은 초당 요금·주 개수)."""
    api = Api()
    svc, bot = await world(api, env={"XAI_API_KEY": XKEY, "VIDEO_MODEL": "grok-imagine-video-1.5-lite"})
    src = b"\x00\x00\x00\x18ftypmp42" + b"s" * 50
    bot.files = {"src": src}
    clip = SimpleNamespace(file_id="src", file_size=len(src), duration=5.2, mime_type="video/mp4", thumbnail=None)
    posted = SimpleNamespace(from_user=USER, text=None, caption=None, photo=(), document=None, animation=None, video_note=None,
                             sticker=None, video=clip, message_id=40, chat_id=CHAT)
    c = await ctx(svc, bot)
    c.request_msg = SimpleNamespace(message_id=55, text="소담아 옷 빨간색으로", photo=(), video=None, animation=None,
                                    video_note=None, sticker=None, document=None, reply_to_message=posted, chat_id=CHAT,
                                    from_user=USER)
    out = await run(c, prompt="Change the outfit color to red", mode="edit")
    assert "고치기를 시작" in out and "1/6" in out, out
    post = [r for r in api.reqs if r.method == "POST"][-1]
    body = json.loads(post.content)
    assert str(post.url).endswith("/videos/edits") and body["model"] == "grok-imagine-video", "영상 입력은 기본 모델 (lite 는 못 받음)"
    assert body["video"]["url"] == "data:video/mp4;base64," + base64.b64encode(src).decode() and "duration" not in body
    sent = bot.named("send_video")
    assert sent and sent[-1][2] == MP4
    assert await svc.db.counter(videogen.week_start(svc.cfg.tz), CHAT, videogen.KEY) == 1
    today = datetime.now(svc.cfg.tz).strftime("%Y-%m-%d")
    assert await svc.db.counter(today, 0, "video_sec:grok-imagine-video") == 6, "5.2초 → 6초로 청구"
    # 이어 붙이기: 늘릴 초 = 방 설정까지
    c2 = await ctx(svc, bot)
    c2.request_msg = c.request_msg
    out = await run(c2, prompt="She turns and walks away", mode="extend", seconds=30)
    body = json.loads([r for r in api.reqs if r.method == "POST"][-1].content)
    assert "이어 붙이기를 시작" in out and body["duration"] == 6, (out, body)
    # 영상이 없으면 / 너무 긴 영상
    c3 = await ctx(svc, bot)
    out = await run(c3, prompt="x", mode="edit")
    assert "고칠 영상이 없음" in out
    clip.duration = 14
    c4 = await ctx(svc, bot)
    c4.request_msg = c.request_msg
    assert "10초 이하" in await run(c4, prompt="x", mode="edit")
    # Veo 는 고치기 없음
    svc2, bot2 = await world(Api(), env={"GEMINI_API_KEY": GKEY})
    c5 = await ctx(svc2, bot2)
    c5.request_msg = c.request_msg
    assert "못 함" in await run(c5, prompt="x", mode="edit")
    assert costs.video_usd_micro("grok-imagine-video-1.5-lite", 10) == 200_000


if __name__ == "__main__":
    asyncio.run(run_all())
