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


def probe(data: bytes) -> str:
    p = subprocess.run([avatar.ffmpeg_path(), "-hide_banner", "-f", "mp4", "-i", "pipe:0"], input=data,
                       capture_output=True)
    return p.stderr.decode(errors="replace")


@test
async def every_style_meets_telegram_avatar_spec():
    assert avatar.available(), "imageio-ffmpeg (requirements.txt)"
    for style in avatar.STYLES:
        data = await avatar.make(png(), style)                          # 가로로 긴 사진도 정사각형으로
        info = probe(data)
        assert len(data) <= avatar.MAX_BYTES and data[4:8] == b"ftyp", style
        assert "h264" in info and "yuv420p" in info and "640x640" in info and "Audio" not in info, (style, info)
        assert "Duration: 00:00:06" in info, (style, info)                 # ≤ 10초
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
    r.bot.files = {"prof1": png(640, 640)}

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
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "shine"})])
    assert "보냈음" in res[0], res
    [d] = docs(r)
    assert ("profile_photos", A.id) in r.bot.calls and "움프" in d[3] and "반짝임" in d[3], d
    for _ in range(avatar_limit() - 1):
        await ask(r, A, [tool_call("make_profile_video", {"style": "breathe"})])
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "breathe"})])
    assert "하루" in res[0] and len(docs(r)) == avatar_limit(), res


def avatar_limit():
    from sodam.panels import avatar as P
    return P.FREE_DAILY


@test
async def someone_elses_photo_is_refused_own_attached_photo_works():
    r = await world(profile=False)
    res = await ask(r, BOSS, [tool_call("make_profile_video", {"style": "sway"})], image=Attached(png(), "image/png", A.id))
    assert "남의 사진" in res[0] and not docs(r), res
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "sway"})])            # 프사 없음
    assert "프사를 못 가져옴" in res[0], res
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "sway"})], image=Attached(png(), "image/png", A.id))
    assert "보냈음" in res[0] and len(docs(r)) == 1, res


@test
async def ai_art_uses_image_edit_and_room_image_limit():
    r = await world()
    seen = []

    async def image(prompt, source=None, chat_id=None):
        seen.append((prompt, source.owner if source else None))
        return png(1024, 1024, (20, 200, 220))
    r.llm.image = image
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "breathe", "art": "anime"})])
    assert "보냈음" in res[0] and seen and "anime" in seen[0][0] and seen[0][1] == A.id, (res, seen)
    await r.db.set_setting(r.CHAT, "image_daily", 1)
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "breathe", "art": "neon"})])
    assert "이미지 한도" in res[0] and len(seen) == 1, res                         # 그림체는 방 이미지 한도
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "breathe"})])
    assert "보냈음" in res[0], res                                               # 그림체 없이는 됨
    res = await ask(r, A, [tool_call("make_profile_video", {"style": "x; rm -rf /"})])
    assert "중" in res[0] and len(docs(r)) == 2, res                              # 정해진 스타일만


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
