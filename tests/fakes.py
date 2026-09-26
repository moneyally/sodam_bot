"""테스트용 가짜 텔레그램 객체와 공용 도우미. 네트워크를 쓰지 않는다."""
import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram import ChatPermissions  # noqa: E402

from sodam.config import Config  # noqa: E402
from sodam.db import DB  # noqa: E402
from sodam.moderation import Moderator  # noqa: E402
from sodam.services import Services  # noqa: E402

TZ = ZoneInfo("Asia/Seoul")


def cfg(db_path=":memory:", **kw) -> Config:
    base = dict(telegram_token="t", openai_api_key="k", owner_ids=frozenset({1}), bot_name="소담",
                call_names=("소담아", "소담이", "소담"), model="gpt-5.4", guard_model="gpt-5.4-mini",
                reasoning_effort="", daily_token_budget=1_000_000, db_path=db_path, tz=TZ,
                log_chat_id=None, sportsdb_key="123")
    base.update(kw)
    return Config(**base)


def fake_user(uid, first="홍길동", username=None, is_bot=False):
    return SimpleNamespace(id=uid, first_name=first, last_name=None, username=username, is_bot=is_bot)


class FakeBot:
    """호출을 전부 calls 에 기록한다."""
    id = 999
    username = "sodambot"

    def __init__(self, admins=(), chat_permissions=None):
        self.calls: list[tuple] = []
        self._next_id = 1000
        self.admins = list(admins)
        self.chat_permissions = chat_permissions or ChatPermissions(can_send_messages=True, can_send_photos=True)

    def _msg(self, chat_id, **kw):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id, chat_id=chat_id, **kw)

    def named(self, name):
        return [c for c in self.calls if c[0] == name]

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", chat_id, text, kw))
        return self._msg(chat_id, text=text)

    async def _send_media(self, kind, chat_id, media, caption=None, **kw):
        self.calls.append((kind, chat_id, media, caption, kw))
        return self._msg(chat_id)

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        return await self._send_media("send_photo", chat_id, photo, caption, **kw)

    async def send_video(self, chat_id, video, caption=None, **kw):
        return await self._send_media("send_video", chat_id, video, caption, **kw)

    async def send_animation(self, chat_id, animation, caption=None, **kw):
        return await self._send_media("send_animation", chat_id, animation, caption, **kw)

    async def send_document(self, chat_id, document, caption=None, **kw):
        return await self._send_media("send_document", chat_id, document, caption, **kw)

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None, **kw):
        self.calls.append(("restrict", chat_id, user_id, permissions, until_date))

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.calls.append(("ban", chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=False, **kw):
        self.calls.append(("unban", chat_id, user_id))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id))

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append(("delete_many", chat_id, list(message_ids)))

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.calls.append(("pin", chat_id, message_id))

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, permissions=self.chat_permissions)

    async def set_chat_permissions(self, chat_id, permissions, **kw):
        self.calls.append(("set_perms", chat_id, permissions))
        self.chat_permissions = permissions

    async def get_chat_administrators(self, chat_id):
        return [SimpleNamespace(user=u, status="administrator") for u in self.admins]


class FakeMsg:
    def __init__(self, chat_id, user, text="", *, caption=None, photo=None, video=None, animation=None,
                 document=None, message_id=1, reply_to=None):
        self.chat_id, self.from_user, self.text, self.caption = chat_id, user, text, caption
        self.photo = photo or ()
        self.video, self.animation, self.document = video, animation, document
        self.message_id = message_id
        self.reply_to_message = reply_to
        self.entities, self.caption_entities = (), ()  # PTB 는 튜플
        self.deleted = False
        self.replies: list[str] = []

    async def delete(self):
        self.deleted = True

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=self.message_id + 10_000)


class FakeQuery:
    def __init__(self, chat_id, user, data=""):
        self.message = SimpleNamespace(chat_id=chat_id)
        self.from_user = user
        self.data = data
        self.answers: list[tuple] = []
        self.edits: list[str] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)

    async def edit_message_reply_markup(self, markup=None):
        self.edits.append("<markup removed>")


class FakePerms:
    def __init__(self, admins=()):
        self.admins = set(admins)

    async def protected(self, bot, chat_id, uid):
        return uid in self.admins or uid == bot.id

    async def is_admin(self, bot, chat_id, uid):
        return uid in self.admins

    async def owners(self):
        return set()

    async def role(self, bot, chat_id, uid):
        from sodam.permissions import Role
        return Role.ADMIN if uid in self.admins else Role.MEMBER

    def forget(self, chat_id):
        pass


class FakeGreeter:
    def __init__(self):
        self.queued: list[tuple] = []

    def queue(self, bot, chat_id, user_id, name):
        self.queued.append((chat_id, user_id, name))


class FakeCas:
    def __init__(self, banned=()):
        self.banned = set(banned)

    async def is_banned(self, uid):
        return uid in self.banned


class FakeJobQueue:
    def __init__(self):
        self.once: list[tuple] = []

    def run_once(self, cb, when, data=None, name=None):
        self.once.append((cb, when, data))


OPEN_DBS: list[DB] = []


async def make_db() -> DB:
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    await db.open()
    OPEN_DBS.append(db)  # 테스트가 중간에 실패해도 runner 가 닫는다 (안 닫으면 프로세스가 안 끝남)
    return db


async def close_open_dbs() -> None:
    while OPEN_DBS:
        await OPEN_DBS.pop().close()


async def make_svc(db: DB, *, admins=(), cas_banned=(), **cfg_kw) -> Services:
    from sodam.announce import Announcer
    from sodam.captcha import Captcha
    from sodam.games import GameManager
    c = cfg(db.path, **cfg_kw)
    perms = FakePerms(admins)
    svc = Services(cfg=c, db=db, perms=perms, mod=Moderator(c, db, perms), llm=None, sports=None,
                   cas=FakeCas(cas_banned), backup=None)
    svc.games = GameManager(svc)
    svc.greeter = FakeGreeter()
    svc.captcha = Captcha(svc)
    svc.announcer = Announcer(svc)
    return svc


async def add_member(db: DB, chat_id: int, user, joined=False):
    await db.upsert_user(user)
    await db.touch_member(chat_id, user.id, joined=joined)


def runner():
    tests = []

    def test(fn):
        tests.append(fn)
        return fn

    async def run_all():
        failed = 0
        for fn in tests:
            try:
                r = fn()
                if asyncio.iscoroutine(r):
                    await r
                print(f"PASS {fn.__name__}")
            except Exception:
                failed += 1
                print(f"FAIL {fn.__name__}")
                traceback.print_exc()
            finally:
                await close_open_dbs()
        print(f"\n{len(tests) - failed}/{len(tests)} passed")
        return failed

    return test, run_all
