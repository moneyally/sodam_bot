"""🗂️ 미디어 보관 (sodam/mediastore.py): python tests/run_all.py mediastore

실제 2026-10-04: @sodam_ai_bot 삭제 → 새 봇 토큰. 텔레그램 file_id 는 봇마다 달라서 미디어 든 예약공지(백악관 #6 영상·#10 GIF)와
입장 인사(베베 — 옛 봇 1:1 글 복사 + 영상)가 'Wrong file identifier' 로 전부 실패.
→ 저장할 때 원본 보관, 보낼 때 파일 id 오류면 원본을 올리고 새 id 를 기억, 원본이 없으면 글만 + 만든 관리자에게 하루 한 번.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, fake_user, make_db, make_svc, runner  # noqa: E402
from telegram import InputFile  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

from sodam import mediastore, persist  # noqa: E402
from sodam.greet import Greeter, snapshot_of  # noqa: E402

test, run_all = runner()
CHAT = -1004489986446
BAD = "Wrong file identifier/http url specified"


class OldBot(FakeBot):
    """옛 봇: 자기 file_id 원본을 get_file 로 내려줌."""
    id = 111

    def __init__(self, files):
        super().__init__()
        self.files = dict(files)


class NewBot(FakeBot):
    """새 봇: 자기가 만든 file_id 만 받음 (실제 텔레그램처럼 다른 봇 id 는 'Wrong file identifier')."""
    id = 222

    def __init__(self, owned=(), files=None):
        super().__init__()
        self.owned = set(owned)
        self.files = dict(files or {})
        self.uploads: list[tuple[str, bytes]] = []

    async def _send_media(self, kind, chat_id, media, caption=None, **kw):
        if isinstance(media, InputFile):
            fid = f"new{len(self.uploads) + 1}"
            self.uploads.append((kind, media.input_file_content))
            self.owned.add(fid)
            self.calls.append((kind, chat_id, "UPLOAD", caption, kw))
            msg = self._msg(chat_id)
            attr = kind[len("send_"):]
            setattr(msg, attr, [SimpleNamespace(file_id=fid)] if attr == "photo" else SimpleNamespace(file_id=fid))
            return msg
        if media not in self.owned:
            raise BadRequest(BAD)
        return await super()._send_media(kind, chat_id, media, caption, **kw)


async def setup():
    db = await make_db()
    svc = await make_svc(db)
    await db.ensure_chat(CHAT, "백악관")
    return db, svc


async def schedule(db, media_type, media_id, title="백악관 방 규칙"):
    sid = await db.add_schedule(CHAT, kind="interval", at_time=None, interval_min=40, title=title, text="규칙 잘 지켜요",
                                media_type=media_type, media_id=media_id, pin=False, created_by=7)
    return next(r for r in await db.schedules() if r["id"] == sid)


@test
async def kept_original_is_reuploaded_after_bot_change_then_new_id_reused():
    db, svc = await setup()
    old = OldBot({"old_v": b"VIDEO-BYTES"})
    assert await mediastore.remember(old, db, "video", "old_v")
    row = await schedule(db, "video", "old_v")
    new = NewBot()
    await svc.announcer.publish(new, row)
    assert [k for k, _ in new.uploads] == ["send_video"] and new.uploads[0][1] == b"VIDEO-BYTES", new.calls
    kind, chat, media, caption, _ = new.named("send_video")[-1]
    assert (chat, media) == (CHAT, "UPLOAD") and "규칙 잘 지켜요" in caption
    alias = await db._one("SELECT new_id FROM media_alias WHERE bot_id=? AND file_id=?", (new.id, "old_v"))
    assert alias and alias["new_id"] == "new1"
    assert (await db._one("SELECT media_id FROM schedules WHERE id=?", (row["id"],)))["media_id"] == "old_v", "예약 값은 그대로"
    await svc.announcer.publish(new, row)                           # 두 번째: 새 id 로 바로 (다시 안 올림)
    assert len(new.uploads) == 1 and new.named("send_video")[-1][2] == "new1", new.calls
    assert not new.named("send_message"), "관리자 알림 없음"


@test
async def lost_media_posts_text_only_and_tells_creator_once_a_day():
    db, svc = await setup()
    row = await schedule(db, "animation", "gone_gif", title="📢 이벤트")
    new = NewBot()
    await svc.announcer.publish(new, row)
    room = [c for c in new.named("send_message") if c[1] == CHAT]
    assert room and "규칙 잘 지켜요" in room[-1][2], new.calls                    # 공지는 글만이라도 올라감
    dm = [c for c in new.named("send_message") if c[1] == 7]
    assert len(dm) == 1 and f"#{row['id']}" in dm[0][2] and "GIF" in dm[0][2] and "다시 넣어" in dm[0][2], dm
    ev = await db._one("SELECT kind FROM ops_events WHERE ref=? ORDER BY id DESC", (row["id"],))
    assert ev["kind"] == "sched_media"
    await svc.announcer.publish(new, row)
    assert len([c for c in new.named("send_message") if c[1] == 7]) == 1, "같은 날 두 번째는 알림 없음"
    assert len([c for c in new.named("send_message") if c[1] == CHAT]) == 2


@test
async def other_bad_request_is_not_treated_as_lost_file():
    db, _ = await setup()

    class NoRight(FakeBot):
        async def send_video(self, *a, **kw):
            raise BadRequest("Not enough rights to send videos to the chat")
    bot = NoRight()
    await mediastore._put(db, "v", "video", b"x")
    try:
        await mediastore.send(bot, db, "video", CHAT, "v", caption="a")
    except mediastore.MediaLost:
        raise AssertionError("권한 오류를 파일 사라짐으로 보면 안 됨")
    except BadRequest as e:
        assert "rights" in str(e)
    else:
        raise AssertionError("오류가 그대로 나와야 함")
    assert mediastore.is_bad_file(BadRequest("Wrong remote file identifier specified: wrong padding in the string"))
    assert mediastore.is_bad_file(BadRequest(BAD)) and not mediastore.is_bad_file(BadRequest("Chat not found"))


@test
async def greet_media_recovers_and_lost_greet_media_falls_back_to_text():
    db, svc = await setup()
    await db.set_setting(CHAT, "greet_template", "{names} 어서 와요")
    await db.set_setting(CHAT, "greet_media_type", "video")
    await db.set_setting(CHAT, "greet_media_id", "old_v")
    await mediastore.remember(OldBot({"old_v": b"HELLO"}), db, "video", "old_v")
    new = NewBot()
    g = Greeter(svc)
    g._pending[CHAT] = [(20, "새사람")]
    await g.flush(new, CHAT)
    assert new.uploads == [("send_video", b"HELLO")] and "어서 와요" in new.named("send_video")[-1][3], new.calls
    await db._write("INSERT INTO subscriptions(chat_id, trial_until, added_by, updated_at) VALUES(?,?,?,?)",
                    (CHAT, int(time.time()) + 86400, 7, int(time.time())))
    await db.set_setting(CHAT, "greet_media_id", "ghost")
    g._pending[CHAT] = [(21, "또사람")]
    await g.flush(new, CHAT)
    texts = [c for c in new.named("send_message") if c[1] == CHAT]
    assert texts and "어서 와요" in texts[-1][2], new.calls                          # 미디어 없이 글로
    assert len([c for c in new.named("send_message") if c[1] == 7]) == 1, "방 등록 관리자에게 알림"


@test
async def copied_greeting_survives_bot_change_with_animated_emoji():
    db, svc = await setup()
    rich = '<tg-emoji emoji-id="5368324170671202286">😉</tg-emoji> 이벤트 안내'
    post = SimpleNamespace(text="😉 이벤트 안내", caption=None, entities=[SimpleNamespace(type="custom_emoji")],
                           text_html=rich, photo=(), video=None, animation=None, document=None)
    snap = snapshot_of(post)
    assert snap == {"html": rich, "kind": None, "file_id": None}
    await db.set_setting(CHAT, "greet_copy", [1, 777])
    await db.set_setting(CHAT, "greet_copy_snap", snap)

    class Gone(NewBot):   # 새 봇은 옛 봇 1:1 의 글을 못 꺼냄
        async def copy_message(self, *a, **kw):
            raise BadRequest("Message to copy not found")
    new = Gone()
    g = Greeter(svc)
    g._pending[CHAT] = [(20, "새사람")]
    await g.flush(new, CHAT)
    sent = new.named("send_message")[-1]
    assert sent[1] == CHAT and sent[2] == rich and sent[3]["parse_mode"] == "HTML", new.calls
    plain = snapshot_of(SimpleNamespace(text=None, caption="<사진> 안내", photo=(SimpleNamespace(file_id="p1"),),
                                        video=None, animation=None, document=None))
    assert plain == {"html": "&lt;사진&gt; 안내", "kind": "photo", "file_id": "p1"}


@test
async def backfill_keeps_current_bot_media_skips_foreign_and_big():
    db, svc = await setup()
    mediastore._failed.clear()
    await schedule(db, "video", "mine")
    await schedule(db, "photo", "foreign")
    await schedule(db, "video", "huge")
    await db.set_setting(CHAT, "greet_media_type", "animation")
    await db.set_setting(CHAT, "greet_media_id", "gmine")

    class Bot(FakeBot):
        files = {"mine": b"M", "gmine": b"G", "huge": b"H"}

        async def get_file(self, file_id):
            f = await super().get_file(file_id)
            f.file_size = mediastore.MAX_BYTES + 1 if file_id == "huge" else 1
            return f
    bot = Bot()
    assert await mediastore.backfill(bot, db) == 2
    assert await mediastore.stored(db, "mine") and await mediastore.stored(db, "gmine")
    assert not await mediastore.stored(db, "foreign") and not await mediastore.stored(db, "huge")
    n = len(bot.named("get_file"))
    assert await mediastore.backfill(bot, db) == 0
    assert len(bot.named("get_file")) == n, "실패한 id·이미 보관한 것은 다시 안 받음"
    assert await mediastore.load(db, "mine") == b"M"


@test
async def unused_kept_files_are_cleaned_after_30_days():
    db, _ = await setup()
    await mediastore._put(db, "old_unused", "photo", b"OLD")
    await mediastore._put(db, "old_used", "video", b"USED")
    await db._write("UPDATE media_store SET ts=?", (int(time.time()) - mediastore.KEEP_UNUSED - 10,))
    await schedule(db, "video", "old_used")
    await mediastore.backfill(FakeBot(), db)
    assert not await mediastore.stored(db, "old_unused") and await mediastore.stored(db, "old_used")


@test
async def saving_in_announce_wizard_and_greet_editor_keeps_original():
    """저장 화면에서 remember_soon → 뒤에서 원본 보관."""
    db, svc = await setup()
    mediastore._failed.clear()
    bot = OldBot({"w1": b"W"})
    mediastore.remember_soon(bot, db, "photo", "w1")
    mediastore.remember_soon(bot, db, None, None)       # 미디어 없음 → 아무것도 안 함
    await persist.drain()
    assert await mediastore.load(db, "w1") == b"W"
    src = Path(mediastore.__file__).read_text()
    assert "remember_soon" in Path(mediastore.__file__).parent.joinpath("announce.py").read_text()
    assert "remember_soon" in Path(mediastore.__file__).parent.joinpath("panels", "greet.py").read_text()
    assert "MediaLost" in src


if __name__ == "__main__":
    asyncio.run(run_all())
