"""🗂️ 방에 올라온 스티커·사진·영상 기록 (sodam/medialog.py). python tests/run_all.py medialog"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import Room, fast_timers, restore_timers  # noqa: E402
from fakes import fake_user, runner  # noqa: E402

from sodam import handlers, medialog  # noqa: E402

test, run_all = runner()
LUFFY, BOSS = fake_user(20, "루피", "luffy"), fake_user(10, "방장", "boss")


def sticker(fid="STK1", emoji="👋", set_name="onepiece_by_x", video=True):
    return SimpleNamespace(file_id=fid, file_unique_id="u" + fid, emoji=emoji, set_name=set_name,
                           is_video=video, is_animated=False, file_size=1000, thumbnail=None)


async def room():
    r = Room()
    await r.open(admins=(BOSS.id,), settings={"captcha_enabled": False, "ai_enabled": False})
    for u in (BOSS, LUFFY):
        await r.join(u)
    return r


async def post(r, user, **media):
    m = r.msg(user, "")
    for k, v in media.items():
        setattr(m, k, v)
    await handlers.on_group_message(SimpleNamespace(message=m), r.ctx)
    await r.settle()
    return m


@test
async def sticker_and_photo_are_logged_but_not_counted_as_chat():
    old = fast_timers()
    try:
        r = await room()
        m = await post(r, LUFFY, sticker=sticker())
        rows = await medialog.recent(r.db, Room.CHAT, LUFFY.id)
        assert len(rows) == 1 and rows[0]["kind"] == "sticker" and rows[0]["file_id"] == "STK1", [dict(x) for x in rows]
        assert rows[0]["emoji"] == "👋" and rows[0]["set_name"] == "onepiece_by_x" and rows[0]["fmt"] == "video"
        assert rows[0]["msg_id"] == m.message_id
        small, big = SimpleNamespace(file_id="P_s", file_unique_id="a"), SimpleNamespace(file_id="P_b", file_unique_id="b")
        await post(r, LUFFY, photo=(small, big), caption="이거 봐")
        rows = await medialog.recent(r.db, Room.CHAT, LUFFY.id)
        assert [x["kind"] for x in rows] == ["photo", "sticker"] and rows[0]["file_id"] == "P_b" and rows[0]["caption"] == "이거 봐"
        assert [x["file_id"] for x in await medialog.recent(r.db, Room.CHAT, LUFFY.id, kind="sticker")] == ["STK1"]
        n = await r.db._one("SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND user_id=?", (Room.CHAT, LUFFY.id))
        assert n["n"] == 1, "스티커는 messages(채팅 순위)에 안 셈 — 글이 있는 사진만"
    finally:
        restore_timers(old)


@test
async def text_and_bots_are_not_logged():
    old = fast_timers()
    try:
        r = await room()
        await r.say(LUFFY, "그냥 글")
        await post(r, fake_user(99, "게임봇", "gamebot", is_bot=True), sticker=sticker("BOT"))
        assert not await r.db._all("SELECT * FROM media_log")
        bot_msg = r.msg(fake_user(99, "게임봇", "gamebot", is_bot=True), "")
        bot_msg.sticker = sticker("BOT2")
        assert await medialog.record(r.db, bot_msg) is False                 # 기록 함수를 직접 불러도 봇 글은 안 남김
        assert not await r.db._all("SELECT * FROM media_log")
    finally:
        restore_timers(old)


@test
async def old_rows_pruned_and_window_respected():
    old = fast_timers()
    try:
        r = await room()
        t0 = time.time()
        medialog._last_prune = t0
        m = r.msg(LUFFY, "")
        m.sticker = sticker("OLD")
        await medialog.record(r.db, m, now=t0 - medialog.KEEP_SEC - 10)
        assert not await medialog.recent(r.db, Room.CHAT, LUFFY.id, now=t0), "10분 창 밖"
        assert len(await medialog.recent(r.db, Room.CHAT, LUFFY.id, within=10 ** 7, now=t0)) == 1
        medialog._last_prune = 0                                   # 한 시간 지난 셈 → 다음 기록 때 사흘 지난 줄 지움
        m.sticker = sticker("NEW")
        await medialog.record(r.db, m, now=t0)
        rows = await r.db._all("SELECT file_id FROM media_log")
        assert [x["file_id"] for x in rows] == ["NEW"], [dict(x) for x in rows]
    finally:
        restore_timers(old)


@test
async def log_failure_does_not_break_room():
    old, real = fast_timers(), medialog.record

    async def boom(db, msg, now=None):
        raise RuntimeError("db 고장")
    medialog.record = boom
    try:
        r = await room()
        await post(r, LUFFY, sticker=sticker())
        await r.say(LUFFY, "글은 그대로 기록")
        n = await r.db._one("SELECT COUNT(*) AS n FROM messages WHERE user_id=?", (LUFFY.id,))
        assert n["n"] == 1
    finally:
        medialog.record = real
        restore_timers(old)


if __name__ == "__main__":
    run_all()
