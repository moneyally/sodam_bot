"""관리자 목록 DB 캐시 (P2): python tests/test_perms_cache.py"""
import asyncio
import sys
import time

from fakes import FakeBot, cfg, fake_user, make_db, runner

from sodam import menu
from sodam.permissions import ADMIN_STALE, ADMIN_TTL, Permissions

test, run_all = runner()
A, B, C = -1001, -1002, -1003


class CountingBot(FakeBot):
    def __init__(self, admins_by_chat):
        super().__init__()
        self.by_chat = admins_by_chat
        self.fetches: list[int] = []

    async def get_chat_administrators(self, chat_id):
        self.fetches.append(chat_id)
        from types import SimpleNamespace
        return [SimpleNamespace(user=fake_user(u), status="administrator") for u in self.by_chat.get(chat_id, ())]


async def setup():
    db = await make_db()
    for cid in (A, B, C):
        await db.ensure_chat(cid, f"방{cid}")
    c = cfg(db.path, owner_ids=frozenset({7}))
    return db, c, CountingBot({A: {1}, B: {2}, C: {1, 2}})


@test
async def cache_survives_restart_and_forget_refetches():
    db, c, bot = await setup()
    p = Permissions(c, db)
    assert await p.is_admin(bot, A, 1) and await p.is_admin(bot, A, 1)
    assert bot.fetches == [A]                                   # 두 번째는 메모리 캐시
    p2 = Permissions(c, db)                                     # 재시작
    assert await p2.is_admin(bot, A, 1) and bot.fetches == [A]  # DB 캐시로 API 호출 없음
    p2.forget(A)                                                # 관리자 변경 이벤트 → 무조건 새로
    assert await p2.is_admin(bot, A, 1) and bot.fetches == [A, A]
    await db._write("UPDATE chat_admins_fetched SET ts=?", (int(time.time()) - ADMIN_TTL - 1,))
    p3 = Permissions(c, db)                                     # DB 캐시가 오래됨 → 새로
    bot.by_chat[A] = {9}                                        # 그 사이 1번이 관리자에서 내려옴
    assert not await p3.is_admin(bot, A, 1) and bot.fetches[-1] == A
    assert {r["user_id"] for r in await db._all("SELECT user_id FROM chat_admins WHERE chat_id=?", (A,))} == {9}


@test
async def group_list_skips_rooms_where_user_is_not_admin():
    db, c, bot = await setup()
    p = Permissions(c, db)
    svc = type("S", (), {})()
    svc.perms, svc.db = p, db
    orig = menu.chat_title
    menu.chat_title = lambda s, cid: _async(f"방{cid}")
    try:
        first = await menu.admin_groups(svc, bot, 1)
        assert sorted(cid for cid, _ in first) == sorted([A, C])
        assert sorted(bot.fetches) == sorted([A, B, C])          # 처음엔 모두 확인
        bot.fetches.clear()
        p._admin_cache.clear()
        assert await p.candidate_chats(1) and B not in await p.candidate_chats(1)  # 관리자 아닌 방은 후보 제외
        assert sorted(await p.candidate_chats(7)) == sorted([A, B, C])            # 오너는 전체
        await db._write("INSERT INTO bot_admins(chat_id, user_id) VALUES(?, ?)", (B, 1))
        assert B in await p.candidate_chats(1)                                    # 봇관리자로 등록된 방은 후보
        await db._write("UPDATE chat_admins_fetched SET ts=? WHERE chat_id=?", (int(time.time()) - ADMIN_STALE - 1, B))
        await db._write("DELETE FROM bot_admins")
        assert B in await p.candidate_chats(1)                                    # 오래된 방은 다시 확인
    finally:
        menu.chat_title = orig


@test
async def migrate_drops_admin_cache():
    db, c, bot = await setup()
    p = Permissions(c, db)
    await p.is_admin(bot, A, 1)
    await db.migrate_chat(A, -1009)
    assert not await db._all("SELECT 1 FROM chat_admins WHERE chat_id IN (?, ?)", (A, -1009))


async def _async(v):
    return v


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run_all()) else 0)
