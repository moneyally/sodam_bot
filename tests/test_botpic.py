"""🖼 봇 프로필 바꾸기 (sodam/botpic.py, 오너 `.봇프사`): python tests/run_all.py botpic

실제 2026-10-03: @BotFather 는 사진만 받음 → Bot API 9.4 setMyProfilePhoto(InputProfilePhotoAnimated, MP4)로. 가로 영상(736x400)·소리·3.2MB →
정사각형·소리 없음·2MB 아래.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, FakeMsg, fake_user, make_db, make_svc, runner  # noqa: E402

from sodam import botpic, commands  # noqa: E402
from sodam.avatar import ffmpeg_path  # noqa: E402
from sodam.commands import CmdCtx  # noqa: E402
from sodam.permissions import Role  # noqa: E402

test, run_all = runner()
OWNER = fake_user(1, "오너")


def landscape_mp4(tmp: Path) -> bytes:
    out = tmp / "wide.mp4"
    subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=736x400:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "12", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out)],
                   check=True, timeout=60)
    return out.read_bytes()


def probe(data: bytes, tmp: Path) -> str:
    p = tmp / "probe.mp4"
    p.write_bytes(data)
    return subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr


@test
async def landscape_video_becomes_square_silent_small_and_is_sent_as_animated():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src = landscape_mp4(tmp)
        out = await asyncio.to_thread(botpic.square_video, src)
        info = probe(out, tmp)
        assert "800x800" in info and "Audio" not in info and len(out) <= botpic.MAX_BYTES, info
        assert "00:00:10" in info, "10초로 자름"
        db = await make_db()
        svc = await make_svc(db)
        calls = []

        async def fake_call(token, method, data=None, files=None):
            calls.append((method, data, files))
            return True, ""
        orig, botpic._call = botpic._call, fake_call
        try:
            bot = FakeBot()
            bot.files = {"vid": src}
            video = SimpleNamespace(file_id="vid", file_size=len(src), duration=12, mime_type="video/mp4")
            post = FakeMsg(OWNER.id, OWNER, "", video=video, message_id=10)
            msg = FakeMsg(OWNER.id, OWNER, ".봇프사", message_id=11, reply_to=post)
            cmd, args, argstr = commands.parse(msg.text, "sodambot")
            assert cmd.role == Role.OWNER and cmd.dm_ok
            await commands.dispatch(CmdCtx(svc, bot, msg, OWNER.id, OWNER, Role.OWNER, args, argstr), cmd)
            method, data, files = calls[-1]
            photo = json.loads(data["photo"])
            assert method == "setMyProfilePhoto" and photo["type"] == "animated" and photo["animation"] == "attach://pic"
            assert files["pic"][2] == "video/mp4" and len(files["pic"][1]) <= botpic.MAX_BYTES
            assert "바꿨어요" in msg.replies[-1], msg.replies
            # 되돌리기
            msg2 = FakeMsg(OWNER.id, OWNER, ".봇프사 원래대로", message_id=12)
            cmd, args, argstr = commands.parse(msg2.text, "sodambot")
            await commands.dispatch(CmdCtx(svc, bot, msg2, OWNER.id, OWNER, Role.OWNER, args, argstr), cmd)
            assert calls[-1][0] == "removeMyProfilePhoto" and "지웠어요" in msg2.replies[-1]
            # 아무것도 없이 → 쓰는 법
            msg3 = FakeMsg(OWNER.id, OWNER, ".봇프사", message_id=13)
            cmd, args, argstr = commands.parse(msg3.text, "sodambot")
            n = len(calls)
            await commands.dispatch(CmdCtx(svc, bot, msg3, OWNER.id, OWNER, Role.OWNER, args, argstr), cmd)
            assert len(calls) == n and "답장" in msg3.replies[-1]
        finally:
            botpic._call = orig


@test
async def telegram_refusal_is_shown():
    async def refuse(token, method, data=None, files=None):
        return False, "Bad Request: PHOTO_INVALID_DIMENSIONS"
    orig, botpic._call = botpic._call, refuse
    try:
        ok, why = await botpic.set_photo("t", b"x", True)
        assert not ok and "DIMENSIONS" in why
    finally:
        botpic._call = orig


if __name__ == "__main__":
    asyncio.run(run_all())
