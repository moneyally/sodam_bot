"""🎞️ 움프 (sodam/avatar.py · panels/avatar.py): 진짜 ffmpeg 로 텔레그램 프로필 영상 규격 확인. python tests/run_all.py avatar"""
import io
import subprocess
from types import SimpleNamespace

from PIL import Image

from fake_llm import Room, reply, tool_call
from fakes import runner
from test_sanction_multi import A, BOSS

from sodam import avatar
from sodam.agent import run_agent
from sodam.permissions import Role
from sodam.tools import ToolCtx
from sodam.vision import Attached

test, run_all = runner()


def png(w=900, h=500, color=(200, 80, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def pattern(w=900, h=700) -> bytes:
    """무늬 있는 사진 (단색이면 확대·이동이 안 보여 반복 검사가 무의미)."""
    from PIL import ImageDraw
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 40):
        for y in range(0, h, 40):
            d.rectangle((x, y, x + 39, y + 39), fill=((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def probe(data: bytes) -> str:
    p = subprocess.run([avatar.ffmpeg_path(), "-hide_banner", "-f", "mp4", "-i", "pipe:0"], input=data,
                       capture_output=True)
    return p.stderr.decode(errors="replace")


SPECS = [avatar.PRESETS["shine"], avatar.Spec("zoom", "fast", "rainbow", "hearts"), avatar.Spec("pan", "normal", "vintage", "snow"),
         avatar.Spec("sway", "slow", "neon", "petals")]    # 대표만 진짜로 (옛 이름 전체의 값 옮기기는 test_animation 이 sanitize 로 검사)


@test
async def every_part_meets_telegram_avatar_spec():
    assert avatar.available(), "imageio-ffmpeg (requirements.txt)"
    for style in SPECS:
        data = await avatar.make(png(), style)                          # 가로로 긴 사진도 정사각형으로
        info = probe(data)
        assert len(data) <= avatar.MAX_BYTES and data[4:8] == b"ftyp", style
        assert "h264" in info and "yuv420p" in info and "640x640" in info and "Audio" not in info, (style, info)
        assert "Duration: 00:00:05.9" in info, (style, info)            # 3초 반복 × 2 (≤ 10초)
        head = data[:200]
        assert head.find(b"moov") != -1 or data.find(b"moov") < data.find(b"mdat"), "faststart (moov 가 앞)"


async def ask(r, caller, script, image=None, role=Role.MEMBER):
    r.llm.script = [*script, reply("짠!")]
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, caller, role, await r.db.get_settings(r.CHAT), image=image)
    await run_agent(ctx, style_key="polite", notes={}, history=[], reply_to=None, request="움프", extras={})
    return [m["content"] for m in r.llm.of("chat")[-1]["messages"] if m["role"] == "tool"]


async def world(profile=True):
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    for u in (BOSS, A):
        await r.join(u)
    r.bot.files = {"prof1": pattern(640, 640)}

    async def photos(uid, limit=1, **kw):
        r.bot.calls.append(("profile_photos", uid))
        return SimpleNamespace(photos=[[SimpleNamespace(file_id="prof1")]] if profile else [])
    r.bot.get_user_profile_photos = photos
    return r


def docs(r):
    return r.bot.named("send_document")


@test
async def own_profile_photo_to_file_and_daily_limit():
    r = await world()
    res = await ask(r, A, [tool_call("make_profile_video", {"color": "shine", "particles": "hearts", "speed": "fast"})])
    assert "보냈음" in res[0], res
    [d] = docs(r)
    assert ("profile_photos", A.id) in r.bot.calls and "움프" in d[3] and "반짝임" in d[3] and "하트" in d[3], d
    for _ in range(avatar_limit() - 1):
        await ask(r, A, [tool_call("make_profile_video", {})])
    res = await ask(r, A, [tool_call("make_profile_video", {})])
    assert "하루" in res[0] and len(docs(r)) == avatar_limit(), res


def avatar_limit():
    from sodam.panels import avatar as P
    return P.FREE_DAILY


@test
async def attached_photo_even_someone_elses_is_used_profile_missing_is_explained():
    r = await world(profile=False)
    res = await ask(r, BOSS, [tool_call("make_profile_video", {"motion": "sway"})], image=Attached(pattern(), "image/png", A.id))
    assert "보냈음" in res[0] and len(docs(r)) == 1, res                        # 남의 사진도 됨 (사용자 결정, 하루 한도)
    assert ("profile_photos", BOSS.id) not in r.bot.calls                        # 붙은 사진이 있으면 프사는 안 가져옴
    res = await ask(r, A, [tool_call("make_profile_video", {"motion": "sway"})])            # 프사 없음
    assert "프사를 못 가져옴" in res[0], res


@test
async def ai_art_uses_image_edit_and_room_image_limit():
    r = await world()
    seen = []

    async def image(prompt, source=None, chat_id=None):
        seen.append((prompt, source.owner if source else None))
        return pattern(1024, 1024)
    r.llm.image = image
    res = await ask(r, A, [tool_call("make_profile_video", {"art": "anime"})])
    assert "보냈음" in res[0] and seen and "anime" in seen[0][0] and seen[0][1] == A.id, (res, seen)
    await r.db.set_setting(r.CHAT, "image_daily", 1)
    res = await ask(r, A, [tool_call("make_profile_video", {"art": "neon"})])
    assert "이미지 한도" in res[0] and len(seen) == 1, res                         # 그림체는 방 이미지 한도
    res = await ask(r, A, [tool_call("make_profile_video", {})])
    assert "보냈음" in res[0], res                                               # 그림체 없이는 됨
    for bad in ({"motion": "x; rm -rf /"}, {"color": "','"}, {"particles": "hearts:y=1"}, {"art": "gore"}):
        res = await ask(r, A, [tool_call("make_profile_video", bad)])
        assert "중 하나" in res[0], (bad, res)                                     # 옛 인자는 표 이름만 (글이 엔진 값으로 안 들어감)
    assert len(docs(r)) == 2


def frame(data: bytes, n: int):
    from PIL import Image
    p = subprocess.run([avatar.ffmpeg_path(), "-loglevel", "error", "-f", "mp4", "-i", "pipe:0", "-vf",
                        f"select=eq(n\\,{n})", "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                       input=data, capture_output=True)
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


@test
async def loops_seamlessly_frame_after_last_equals_first():
    """옛 인자 → 같은 엔진: 89장 한 바퀴를 두 번 이어 붙임 → 90번째 장(두 번째 바퀴 첫 장) = 첫 장 (무늬 있는 사진으로)."""
    from PIL import ImageChops, ImageStat
    src = pattern()
    for spec in (avatar.Spec("shake", "fast", "rainbow", "hearts"), avatar.Spec("pan", "slow", "neon", "snow"),
                 avatar.Spec("breathe", "normal", "shine", "bubbles")):
        data = await avatar.make(src, spec)
        f0 = frame(data, 0)
        d = {n: max(ImageStat.Stat(ImageChops.difference(f0, frame(data, n))).mean) for n in (22, 44, 66, 89)}
        end, mid = d.pop(89), max(d.values())
        assert end <= max(5.0, 0.35 * mid) and mid > 2 * end, (spec, end, d)


@test
async def vision_fetch_records_who_posted_the_photo():
    from fakes import FakeBot, FakeMsg, fake_user
    from sodam import vision
    bot = FakeBot()
    bot.files = {"ph": png(64, 64)}
    photo = (SimpleNamespace(file_id="ph", file_size=100),)
    other = FakeMsg(-1, A, photo=photo)
    me = fake_user(77, "요청자")
    got = await vision.fetch(bot, FakeMsg(-1, me, "움프 해줘", reply_to=other))      # 남의 사진에 답장
    assert got and got.owner == A.id
    got = await vision.fetch(bot, FakeMsg(-1, me, "", photo=photo))                  # 본인이 올린 사진
    assert got and got.owner == 77


@test
async def other_members_profile_photo_via_photo_of_for_ump_sticker_and_image():
    """오너 결정 2026-09-29: 기능마다 '여긴 되고 저긴 안 되고' 없이 — 이 방 멤버 누구 프사든 원본으로."""
    r = await world()
    res = await ask(r, A, [tool_call("make_profile_video", {"photo_of": BOSS.first_name})])
    assert "보냈음" in res[0] and ("profile_photos", BOSS.id) in r.bot.calls, res
    assert ("profile_photos", A.id) not in r.bot.calls, "요청자 프사가 아니라 지목한 사람 것"
    res = await ask(r, A, [tool_call("make_profile_video", {"photo_of": "없는사람"})])
    assert "찾을 수 없" in res[0] and len(docs(r)) == 1, res
    from sodam import tools
    from sodam.panels import avatar as P
    for name in ("make_image", "make_sticker", "make_profile_video"):
        assert "photo_of" in tools._BY_NAME[name].params, name
    ctx = ToolCtx(r.svc, r.bot, r.CHAT, A, Role.MEMBER, await r.db.get_settings(r.CHAT))
    data, err = await P.source_photo(ctx, {"photo_of": BOSS.first_name})
    assert data == pattern(640, 640) and err is None
    from sodam import prompt
    assert "누구 것이든" in prompt.SYSTEM and "본인 것만이라고 거절하지 않는다" in prompt.SYSTEM
