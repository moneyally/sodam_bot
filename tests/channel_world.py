"""📢 채널·✍️ 편집기 테스트 공용 가짜 세상 (tests/test_channel.py · test_composer.py).

실제 Permissions(관리자 캐시·forget) + 가짜 텔레그램(bot.chat_admins 로 채널마다 관리자) + 실제 handlers 경로.
"""
from types import SimpleNamespace

from fakes import FakeBot, FakeJobQueue, FakeMsg, FakeQuery, fake_user, make_db, make_svc

from sodam import handlers
from sodam.permissions import Permissions
from sodam.util import RateLimiter

CH, ROOM = -1005550000001, -1005550000009
BOSS, SUB, STRANGER = 501, 502, 503        # 채널 관리자 · 다른 관리자 · 관리자 아님 (1 = 설정상 오너)


class World:
    async def open(self, *, can_post=True, invite=False, billing=False):
        self.db = await make_db()
        self.svc = await make_svc(self.db)
        self.svc.perms = Permissions(self.svc.cfg, self.db)
        self.svc.mod.perms = self.svc.perms
        self.bot = FakeBot()
        self.bot.chat_admins = {CH: [fake_user(BOSS, "대표"), fake_user(SUB, "부대표")], ROOM: [fake_user(BOSS, "대표")]}
        self.bot.linked = {CH: ROOM}
        self.ctx = SimpleNamespace(bot=self.bot, job_queue=FakeJobQueue(),
                                   bot_data={"svc": self.svc, "limiter": RateLimiter(), "chats": set(), "tasks": set()})
        await self.db.ensure_chat(ROOM, "대표님 방")
        if billing:
            from sodam.billing import Billing
            from fakes import cfg
            self.svc.cfg = cfg(self.db.path, pay_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
            self.svc.billing = Billing(self.svc.cfg, self.db)
        await self.promote(BOSS, can_post=can_post, invite=invite)
        self._mid = 0
        return self

    async def promote(self, by: int, *, can_post=True, invite=False, status="administrator", title="대표 <공지> 채널"):
        member = SimpleNamespace(status=status, user=SimpleNamespace(id=self.bot.id), can_post_messages=can_post,
                                 can_edit_messages=True, can_delete_messages=True, can_invite_users=invite)
        old = SimpleNamespace(status="left", user=SimpleNamespace(id=self.bot.id))
        upd = SimpleNamespace(my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=CH, type="channel", title=title, username="boss_news"),
            from_user=fake_user(by, "대표"), old_chat_member=old, new_chat_member=member))
        await handlers.on_my_chat_member(upd, self.ctx)

    async def press(self, uid: int, data: str) -> FakeQuery:
        q = FakeQuery(uid, fake_user(uid), data)
        self.svc.menu_limiter._hits.clear()
        await handlers.on_callback(SimpleNamespace(callback_query=q), self.ctx)
        assert len(q.answers) == 1, (data, q.answers)            # answer 는 정확히 한 번
        for row in getattr(q.kb, "inline_keyboard", ()):
            for b in row:
                assert b.callback_data is None or len(b.callback_data.encode()) <= 64, b.callback_data
                assert b.url is None or b.url.startswith(("https://", "tg://")), b.url
        return q

    async def say(self, uid: int, text=None, **kw) -> FakeMsg:
        """관리자가 1:1 에 보낸 메시지 (글자 입력)."""
        self._mid += 1
        m = FakeMsg(uid, fake_user(uid), text, message_id=self._mid, **kw)
        m.forward_origin = None
        await handlers.on_private(SimpleNamespace(message=m), self.ctx)
        return m

    def sent_to(self, chat_id: int, kinds=("send_message", "send_photo", "send_video")) -> list:
        return [c for c in self.bot.calls if c[0] in kinds and c[1] == chat_id]

    async def draft(self, uid=BOSS) -> int:
        q = await self.press(uid, f"m:chn:{CH}")
        return int(next(b.callback_data for r in q.kb.inline_keyboard for b in r if b.text == "✏️ 본문").split(":")[2])
