"""🧹 서비스 메시지 청소: python tests/run_all.py cleanservice

2026-11-19 경쟁조사(Rose /cleanservice·GroupHelp): 입장 메시지 삭제(delete_join_message)만 있고 퇴장·핀·영상채팅 등
기타 서비스 메시지는 안 지웠음 → clean_service(off/basic/all)로 확장. on_left·on_service 가 실제로 지우는지 확인."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeBot, FakeJobQueue, fake_user, make_db, make_svc, runner  # noqa: E402

from sodam import cleanservice, handlers  # noqa: E402

test, run_all = runner()
CHAT = -1007777
MEM = fake_user(5, "멤버", "mem")


def ctx_of(svc, bot):
    return SimpleNamespace(bot=bot, job_queue=FakeJobQueue(),
                           bot_data={"svc": svc, "joins": {}, "cas_seen": set(), "tasks": set(), "chats": set()})


async def world(mode="off"):
    db = await make_db()
    svc = await make_svc(db, admins={1})
    await db.ensure_chat(CHAT, "방")
    if mode != "off":
        await db.set_setting(CHAT, "clean_service", mode)
    return svc, FakeBot()


class SvcMsg:
    """서비스 메시지 (퇴장·핀 등) — delete() 호출됐는지 추적."""
    def __init__(self, user=None):
        self.chat_id = CHAT
        self.left_chat_member = user
        self.from_user = user
        self.deleted = False

    async def delete(self):
        self.deleted = True


# ── wants() 로직 ──────────────────────────────────────────────
@test
def wants_covers_the_right_kinds():
    assert not cleanservice.wants({"clean_service": "off"}, "join")
    assert not cleanservice.wants({"clean_service": "off"}, "leave")
    for k in ("join", "leave"):
        assert cleanservice.wants({"clean_service": "basic"}, k)
        assert cleanservice.wants({"clean_service": "all"}, k)
    assert not cleanservice.wants({"clean_service": "basic"}, "other")   # 기타는 all 에서만
    assert cleanservice.wants({"clean_service": "all"}, "other")
    assert not cleanservice.wants({}, "leave")                           # 설정 없으면 기본 off


# ── on_left: 퇴장 서비스 메시지 청소 ──────────────────────────
@test
async def leave_message_deleted_when_basic():
    svc, bot = await world("basic")
    msg = SvcMsg(MEM)                                                    # 스스로 나감 (by == 본인)
    await handlers.on_left(SimpleNamespace(message=msg), ctx_of(svc, bot))
    assert msg.deleted, "basic 이면 퇴장 서비스 메시지를 지워야 함"


@test
async def leave_message_kept_when_off():
    svc, bot = await world("off")                                       # 기본값
    msg = SvcMsg(MEM)
    await handlers.on_left(SimpleNamespace(message=msg), ctx_of(svc, bot))
    assert not msg.deleted, "꺼진 방은 퇴장 메시지를 그대로 둬야 함 (회귀 방지)"


# ── on_service: 핀·영상채팅 등 기타 서비스 메시지 청소 ──────────
@test
async def other_service_deleted_only_when_all():
    svc, bot = await world("all")
    msg = SvcMsg()                                                      # 예: 📌 고정 알림
    await handlers.on_service(SimpleNamespace(message=msg), ctx_of(svc, bot))
    assert msg.deleted, "all 이면 기타 서비스 메시지(핀·영상채팅 등)를 지워야 함"


@test
async def other_service_kept_when_basic():
    svc, bot = await world("basic")                                    # basic 은 입장·퇴장만
    msg = SvcMsg()
    await handlers.on_service(SimpleNamespace(message=msg), ctx_of(svc, bot))
    assert not msg.deleted, "basic 은 기타(핀 등)는 안 지움 — all 에서만"


@test
async def on_service_ignores_empty_update():
    svc, bot = await world("all")
    await handlers.on_service(SimpleNamespace(message=None), ctx_of(svc, bot))   # 터지면 안 됨


if __name__ == "__main__":
    asyncio.run(run_all())
