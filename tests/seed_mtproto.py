"""하네스 데이터 + 가짜 Telethon 클라이언트: 🔧 헬퍼 (sodam/mtproto.py · sodam/panels/mtproto.py). 네트워크 없음.

FakeClient 는 tests/test_mtproto.py 도 쓴다. 하네스 세상엔 ① 연결됨(FloodWait 기록) · ② 오류 상태를 넣어
오류 글의 HTML 글자(<b>, &)까지 이스케이프 검사.
"""
import dataclasses
import time
from types import SimpleNamespace

import harness

from sodam import mtproto


class FakeClient:
    """TelegramClient 흉내: connect·로그인·참가자·조회수. 호출은 calls 에, 다음 호출에 던질 예외는 raises 에."""
    made: list = []

    def __init__(self, session, api_id, api_hash, *, bot_id=999, authorized=None, members=(), views=None,
                 connect_error=None, sign_in_error=None, is_user=None, extra=None, full=None, unknown=()):
        self.session_in, self.api = session, (api_id, api_hash)
        self.authorized = bool(session) if authorized is None else authorized
        self.bot_id = bot_id
        # 기본: 봇 세션(빈 것 또는 'S:…')이 아니면 사용자 계정
        self.is_user = (bool(session) and not session.startswith("S:")) if is_user is None else is_user
        self.members = list(members)
        self.view_counts = dict(views or {})
        self.connect_error, self.sign_in_error = connect_error, sign_in_error
        self.connected = False
        self.calls: list = []
        self.raises: list = []
        self.cached_entities: set = set()
        # 멤버 정리·프로필 테스트용 (인스턴스마다 — 테스트 파일끼리 섞이지 않게)
        self.extra = dict(extra or {})     # 사람 ID → User 속성 덮어쓰기 (status·photo·deleted·scam·premium·usernames …)
        self.full = dict(full or {})       # 사람 ID → {"about": …, "common_chats_count": …} (users.getFullUser)
        self.unknown = set(unknown)        # 이 세션이 access_hash 를 모르는 사람 ID (get_input_entity 가 ValueError)
        self.session = SimpleNamespace(save=lambda: f"S:{self.bot_id}")
        FakeClient.made.append(self)

    def _maybe_raise(self):
        if self.raises:
            raise self.raises.pop(0)

    async def connect(self):
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.calls.append("disconnect")
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=self.bot_id, username="sodambot" if not self.is_user else "helper", bot=not self.is_user)

    async def sign_in(self, bot_token=None, **kw):
        self.calls.append(("sign_in", bot_token))
        if self.sign_in_error:
            raise self.sign_in_error
        self.authorized = True
        return await self.get_me()

    async def get_input_entity(self, peer):
        self.calls.append(("entity", peer))
        if getattr(peer, "user_id", None) in self.unknown:
            raise ValueError(f"Could not find the input entity for {peer}")
        if isinstance(peer, str) and isinstance(m := self.usernames.get(peer.lower()), tuple):
            from telethon.tl.types import PeerUser
            return PeerUser(m[0])
        return peer

    def user(self, m):
        """members 항목 (id, 이름, 아이디[, 봇[, 성]]) → 텔레그램 User 흉내 (+ extra 속성)."""
        u = SimpleNamespace(id=m[0], first_name=m[1], username=m[2], bot=m[3] if len(m) > 3 else False,
                            last_name=m[4] if len(m) > 4 else None, deleted=False, min=False)
        for k, v in getattr(self, "extra", {}).get(m[0], {}).items():
            setattr(u, k, v)
        return u

    async def iter_participants(self, entity, limit=None):   # 기본 그룹 (getFullChat)
        self.calls.append(("participants", entity, limit))
        self._maybe_raise()
        for m in self.members[:limit]:
            yield self.user(m)

    async def get_dialogs(self, limit=None):
        self.calls.append(("dialogs", limit))

    usernames: dict = {}   # 아이디 → members 항목 (resolveUsername)

    async def __call__(self, req):
        kind = type(req).__name__
        if kind == "GetParticipantsRequest":
            self.calls.append(("page", req.channel, req.offset, req.limit))
            self._maybe_raise()
            page = self.members[req.offset:req.offset + req.limit]
            inviter = self.user((424242, "초대한 사람", "inviter"))   # users 엔 참가자 아닌 사람도 섞여 옴
            return SimpleNamespace(participants=[SimpleNamespace(user_id=m[0]) for m in page],
                                   users=[self.user(m) for m in page] + ([inviter] if page else []))
        if kind == "ResolveUsernameRequest":
            self.calls.append(("resolve", req.username))
            self._maybe_raise()
            m = self.usernames.get(req.username.lower())
            if m is None:
                from telethon.errors import UsernameNotOccupiedError
                raise UsernameNotOccupiedError(req)
            from telethon.tl.types import PeerChannel, PeerUser
            if m == "channel":
                return SimpleNamespace(peer=PeerChannel(5), users=[self.user((5, "관련 없는 사람", "x"))], chats=[])
            return SimpleNamespace(peer=PeerUser(m[0]), users=[self.user(m)], chats=[])
        if kind == "GetFullUserRequest":
            uid = getattr(req.id, "user_id", None) or getattr(req.id, "id", None)
            self.calls.append(("full", uid))
            self._maybe_raise()
            info = self.full.get(uid)
            m = next((m for m in self.members if m[0] == uid), None)
            if info is None or m is None:
                from telethon.errors import UserIdInvalidError
                raise UserIdInvalidError(req)
            return SimpleNamespace(full_user=SimpleNamespace(id=uid, about=info.get("about"),
                                                             common_chats_count=info.get("common_chats_count", 0)),
                                   users=[self.user(m)], chats=[])
        self.calls.append(("call", kind, list(req.id), req.increment))
        self._maybe_raise()
        return SimpleNamespace(views=[SimpleNamespace(views=self.view_counts.get(i)) for i in req.id])


def factory(**kw):
    def make(session, api_id, api_hash):
        return FakeClient(session, api_id, api_hash, **kw)
    return make


async def seed(svc):
    cfg = dataclasses.replace(svc.cfg, mtproto_api_id=12345, mtproto_api_hash="hash")
    mt = mtproto.MTProto(cfg, svc.db, factory=factory(members=[(1, "a", "a")]))
    await mt._start_bot()
    mt.bot.last_flood = (int(time.time()) - 60, 17)
    mt.user.configured = True
    mt.user.fail("사용자 세션 실패", "<b>세션</b> 만료 & 다시")
    mt._restart_at = time.monotonic()   # 하네스가 누르는 🔄 는 '방금 시작' 안내 (뒤에서 로그인 작업이 안 돌게)
    svc.mtproto = mt


harness.SEEDERS.append(seed)
