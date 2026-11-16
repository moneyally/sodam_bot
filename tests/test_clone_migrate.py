"""🏷→🏷 방 이관 엔진 (sodam/clone_migrate.py): python tests/run_all.py clone_migrate

본체 DB 의 한 방(chat_id) 데이터를 클론 DB 로 복사 — 포인트·설정·구독·기억·기록 + 전역 users/name_history.
돈(members.points) 안 날아가고, 딴 방은 안 섞이고, 열린 베팅이면 거절하는지 검사.
"""
import asyncio
import os
import tempfile

from fakes import fake_user, runner

from sodam import clone_migrate
from sodam.db import DB

test, run_all = runner()


async def _open(path):
    d = DB(path)
    await d.open()
    return d


async def _seed_src(src):
    A, OTHER = -1001, -1002
    await src.ensure_chat(A, "넘길방")
    await src.ensure_chat(OTHER, "딴방")
    for uid, pts in [(11, 5000), (22, 3000)]:
        await src.upsert_user(fake_user(uid, f"손{uid}", username=f"user{uid}"))
        await src.touch_member(A, uid)
        await src._write("UPDATE members SET points=? WHERE chat_id=? AND user_id=?", (pts, A, uid))
    # 딴 방 멤버·포인트 (이관 대상 아님 — 안 섞여야)
    await src.upsert_user(fake_user(99, "남", username="other"))
    await src.touch_member(OTHER, 99)
    await src._write("UPDATE members SET points=? WHERE chat_id=? AND user_id=?", (777, OTHER, 99))
    # 설정 + chat_state 숨은 값 + 구독 + 전역 이름기록
    await src.set_setting(A, "casino_enabled", True)
    await src.set_state(A, "ai_savage_max", True)
    await src.extend_paid(A, 30 * 86400, 1000)
    await src._write("INSERT INTO name_history(user_id, first_name, ts) VALUES(?,?,?)", (11, "옛이름", 1))
    return A, OTHER


@test
async def copies_wallet_settings_subscription_global_names_not_other_rooms():
    tmp = tempfile.mkdtemp()
    srcp, dstp = os.path.join(tmp, "s.db"), os.path.join(tmp, "d.db")
    src = await _open(srcp)
    A, OTHER = await _seed_src(src)
    await src.close()

    (await _open(dstp)).conn  # 빈 클론 DB 스키마 생성
    dst0 = await _open(dstp)
    await dst0.close()

    counts = clone_migrate._copy_room(srcp, dstp, A)

    dst = await _open(dstp)
    try:
        # 포인트 보존 + 합 일치
        assert await clone_migrate.wallet_sum(dst, A) == 8000, counts
        # 멤버 정확히 2명, 딴 방은 안 옮김
        m = {r["user_id"] for r in await dst._all("SELECT user_id FROM members WHERE chat_id=?", (A,))}
        assert m == {11, 22}, m
        assert not await dst._all("SELECT 1 FROM members WHERE chat_id=?", (OTHER,)), "딴 방이 섞임"
        # 설정 + chat_state 숨은 값
        s = await dst.get_settings(A)
        assert s.get("casino_enabled") is True, s.get("casino_enabled")
        assert await dst.get_state(A, "ai_savage_max", None) is True
        # 구독
        assert (await dst.get_subscription(A))["paid_until"], "구독 안 옮겨짐"
        # 전역 users: 그 방 멤버만 (딴 방 99 는 안 옴)
        us = {r["user_id"] for r in await dst._all("SELECT user_id FROM users")}
        assert 11 in us and 22 in us and 99 not in us, us
        # 전역 name_history 복사
        assert await dst._one("SELECT 1 FROM name_history WHERE user_id=11 AND first_name='옛이름'")
        assert counts["_members"] == 2 and counts["_users"] == 2, counts
    finally:
        await dst.close()


@test
async def reransfer_is_idempotent_no_double_points():
    """같은 방을 두 번 옮겨도 포인트가 2배가 되면 안 됨(DELETE 먼저 → 멱등)."""
    tmp = tempfile.mkdtemp()
    srcp, dstp = os.path.join(tmp, "s.db"), os.path.join(tmp, "d.db")
    src = await _open(srcp)
    A, _ = await _seed_src(src)
    await src.close()
    (await _open(dstp)); dst0 = await _open(dstp); await dst0.close()

    clone_migrate._copy_room(srcp, dstp, A)
    clone_migrate._copy_room(srcp, dstp, A)   # 두 번째
    dst = await _open(dstp)
    try:
        assert await clone_migrate.wallet_sum(dst, A) == 8000, "재이관에 포인트 2배"
        assert len(await dst._all("SELECT 1 FROM members WHERE chat_id=?", (A,))) == 2
    finally:
        await dst.close()


@test
async def transfer_room_full_seeds_clone_copies_and_verifies():
    """transfer_room 성공 경로: 클론 DB 폴더·스키마 자동 생성 → 복사 → 포인트 합 검증 통과."""
    from types import SimpleNamespace
    from sodam import clone as cl
    tmp = tempfile.mkdtemp()
    srcp = os.path.join(tmp, "s.db")
    src = await _open(srcp)
    A, _ = await _seed_src(src)
    svc = SimpleNamespace(db=src, cfg=SimpleNamespace(db_path=srcp))
    ok, note, counts = await clone_migrate.transfer_room(svc, A, "myclone")
    await src.close()
    assert ok, note
    assert counts["_members"] == 2, counts
    dst = await _open(str(cl.clones_dir(srcp) / "myclone" / "sodam.db"))
    try:
        assert await clone_migrate.wallet_sum(dst, A) == 8000
        assert (await dst.get_subscription(A))["paid_until"]
    finally:
        await dst.close()


@test
async def transfer_room_blocks_on_open_bets():
    """열린 베팅(casino_open)이 있으면 돈이 꼬이니 이관 거절."""
    from types import SimpleNamespace
    tmp = tempfile.mkdtemp()
    srcp = os.path.join(tmp, "s.db")
    src = await _open(srcp)
    A, _ = await _seed_src(src)
    await src._write("INSERT INTO casino_open(chat_id, user_id, amount, ts) VALUES(?,?,?,?)",
                     (A, 11, 100, 1))
    svc = SimpleNamespace(db=src, cfg=SimpleNamespace(db_path=srcp))
    ok, msg, _ = await clone_migrate.transfer_room(svc, A, "testslug")
    await src.close()
    assert not ok and "게임" in msg, msg


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
