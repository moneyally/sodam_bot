"""대본대로 답하는 가짜 LLM + 가짜 단톡방 (tests/test_ai_core.py, tools/ai_dryrun.py 공용).

네트워크 없이 handlers.on_group_message → ai_reply → run_agent → 도구 → 답장 전체 흐름을 돌린다.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, FakeJobQueue, FakeMsg, add_member, fake_user, make_db, make_svc  # noqa: E402

from sodam import handlers, memory, social  # noqa: E402
from sodam.util import RateLimiter  # noqa: E402


class ChatBot(FakeBot):
    async def send_chat_action(self, chat_id, action, **kw):
        self.calls.append(("chat_action", chat_id, action))


def tool_call(name: str, args: dict | None = None, call_id: str = "c1"):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args or {}, ensure_ascii=False))
    return SimpleNamespace(content="", tool_calls=[SimpleNamespace(id=call_id, type="function", function=fn)])


def reply(text: str):
    return SimpleNamespace(content=text, tool_calls=None)


class ScriptedLLM:
    """chat() 은 script 에서 순서대로, json() 은 purpose 별 json_script 에서 꺼내 답한다.
    각 항목은 응답 객체 / 문자열 / dict 또는 fn(messages|user) → 그중 하나."""
    enabled = True

    def __init__(self, script=None, json_script=None, injection=False):
        self.script = list(script or [])
        self.json_script = {k: list(v) for k, v in (json_script or {}).items()}
        self.injection = injection
        self.calls: list[dict] = []

    async def chat(self, messages, *, tools=None, tool_choice="auto", **kw):
        self.calls.append({"kind": "chat", "messages": [dict(m) for m in messages], "tools": tools,
                           "tool_choice": tool_choice, **kw})
        if not self.script:
            raise AssertionError("LLM 대본이 바닥남: 예상보다 많이 호출됨")
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        return reply(item) if isinstance(item, str) else item

    async def json(self, system, user, **kw):
        purpose = kw.get("purpose", "json")
        self.calls.append({"kind": "json", "system": system, "user": user, **kw})
        queue = self.json_script.get(purpose) or []
        if not queue:
            return {}
        item = queue.pop(0)
        return item(user) if callable(item) else item

    async def classify_injection(self, text):
        self.calls.append({"kind": "classify", "text": text})
        return self.injection, "테스트 판별"

    def of(self, kind: str, purpose: str | None = None) -> list[dict]:
        return [c for c in self.calls if c["kind"] == kind and (purpose is None or c.get("purpose") == purpose)]


class Room:
    """가짜 그룹방. say() 로 멤버가 말하면 봇 파이프라인 전체가 돈다."""
    CHAT = -100777

    def __init__(self):
        self.db = self.svc = self.bot = self.ctx = None
        self.llm = ScriptedLLM()
        self._mid = 100

    async def open(self, *, admins=(), settings=None):
        self.db = await make_db()
        self.svc = await make_svc(self.db, admins=admins)
        self.svc.llm = self.llm
        self.bot = ChatBot(admins=[fake_user(a) for a in admins])
        self.ctx = SimpleNamespace(bot=self.bot, job_queue=FakeJobQueue(),
                                   bot_data={"svc": self.svc, "limiter": RateLimiter(), "chats": set(),
                                             "cas_seen": set(), "tasks": set(), "joins": {}})
        await self.db.ensure_chat(self.CHAT, "대표님들 소통방")
        base = {"cas_enabled": False, "user_rate_per_min": 20, "flood_count": 100}  # 대본은 몇 초 안에 몰아서 말함
        for k, v in {**base, **(settings or {})}.items():
            await self.db.set_setting(self.CHAT, k, v)
        return self

    async def join(self, user):
        await add_member(self.db, self.CHAT, user, joined=True)

    def msg(self, user, text, reply_to=None):
        self._mid += 1
        m = FakeMsg(self.CHAT, user, text, message_id=self._mid, reply_to=reply_to)
        m.chat = SimpleNamespace(id=self.CHAT, title="대표님들 소통방", type="supergroup")
        m.sender_chat = None
        return m

    async def say(self, user, text, reply_to=None, *, settle=True):
        m = self.msg(user, text, reply_to)
        await handlers.on_group_message(SimpleNamespace(message=m), self.ctx)
        if settle:
            await self.settle()
        return m

    async def settle(self):
        """훅·기억 정리·끼어들기 같은 백그라운드 작업이 끝날 때까지 기다린다."""
        for _ in range(20):
            pending = [t for t in list(self.ctx.bot_data["tasks"]) + list(memory.state(self.svc).tasks) if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def bot_user(self):
        return fake_user(self.bot.id, "소담", "sodambot", is_bot=True)


def fast_timers():
    """기다리는 시간을 0 으로 (테스트·드라이런용). 원래 값을 돌려준다."""
    old = (memory.EXTRACT_DELAY, memory.EXTRACT_MIN_GAP, social.sleep)

    async def no_sleep(_):
        return None
    memory.EXTRACT_DELAY, memory.EXTRACT_MIN_GAP, social.sleep = 0, 0, no_sleep
    return old


def restore_timers(old):
    memory.EXTRACT_DELAY, memory.EXTRACT_MIN_GAP, social.sleep = old
