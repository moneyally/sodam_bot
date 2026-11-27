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
    if os.getenv("DB_BACKEND", "").strip().lower() == "pg":
        return   # 클론 방 이관은 sqlite3 ATTACH DATABASE 로 두 .db 복사 — PG 엔 ATTACH 없음, Phase-2 재작성 전제(HANDOFF)
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
    if os.getenv("DB_BACKEND", "").strip().lower() == "pg":
        return   # 클론 방 이관은 sqlite3 ATTACH DATABASE 로 두 .db 복사 — PG 엔 ATTACH 없음, Phase-2 재작성 전제(HANDOFF)
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
    if os.getenv("DB_BACKEND", "").strip().lower() == "pg":
        return   # 클론 방 이관은 sqlite3 ATTACH DATABASE 로 두 .db 복사 — PG 엔 ATTACH 없음, Phase-2 재작성 전제(HANDOFF)
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


@test
async def transfer_room_copies_media_originals_for_autoreply_and_schedule():
    if os.getenv("DB_BACKEND", "").strip().lower() == "pg":
        return   # 클론 방 이관은 sqlite3 ATTACH DATABASE 로 두 .db 복사 — PG 엔 ATTACH 없음, Phase-2 재작성 전제(HANDOFF)
    """2026-11-17 뉴월드: 이관한 자동답글(제휴)·예약공지 사진이 클론에서 'Wrong file identifier' — file_id 가
    본체 봇 것이고 원본이 클론 보관함에 없었음. 이관이 본체 보관 원본을 클론 media/·media_store 로 같이 옮겨야.
    뮤테이션: transfer_room 의 copy_media 호출을 빼면 FAIL."""
    import json
    import sodam.autoreply  # noqa: F401  (auto_replies 표)
    from types import SimpleNamespace
    from sodam import clone as cl
    tmp = tempfile.mkdtemp()
    srcp = os.path.join(tmp, "sodam.db")
    src = await _open(srcp)
    A, _ = await _seed_src(src)
    os.makedirs(os.path.join(tmp, "media"))
    for fid, name, data in [("AgAC_ad", "h1", b"AD-PHOTO"), ("BAAC_sch", "h2", b"SCH-VIDEO"), ("AgAC_x", "h3", b"X")]:
        with open(os.path.join(tmp, "media", name), "wb") as f:
            f.write(data)
        await src._write("INSERT INTO media_store(file_id, kind, path, size, ts) VALUES(?,?,?,?,?)",
                         (fid, "photo" if fid.startswith("Ag") else "video", name, len(data), 1))
    await src._write("INSERT INTO auto_replies(chat_id, key, word, snap, created_by, created_at) VALUES(?,?,?,?,?,?)",
                     (A, "제휴", "제휴", json.dumps({"html": "제휴", "kind": "photo", "file_id": "AgAC_ad"}), 7, 1))
    await src.add_schedule(A, kind="interval", at_time=None, interval_min=40, title="공지", text="t",
                           media_type="video", media_id="BAAC_sch", pin=False, created_by=7)
    svc = SimpleNamespace(db=src, cfg=SimpleNamespace(db_path=srcp))
    ok, note, counts = await clone_migrate.transfer_room(svc, A, "nwd")
    await src.close()
    assert ok, note
    assert counts["_media"] == 2, counts                     # 딴 데서 안 쓰는 AgAC_x 는 안 옮김
    cdir = cl.clones_dir(srcp) / "nwd"
    dst = await _open(str(cdir / "sodam.db"))
    try:
        rows = {r["file_id"]: r["path"] for r in await dst._all("SELECT file_id, path FROM media_store")}
        assert set(rows) == {"AgAC_ad", "BAAC_sch"}, rows
        assert (cdir / "media" / rows["AgAC_ad"]).read_bytes() == b"AD-PHOTO"
    finally:
        await dst.close()


@test
async def global_blocklists_and_owner_game_difficulty_follow_the_room():
    if os.getenv("DB_BACKEND", "").strip().lower() == "pg":
        return   # 클론 방 이관은 sqlite3 ATTACH DATABASE 로 두 .db 복사 — PG 엔 ATTACH 없음, Phase-2 재작성 전제(HANDOFF)
    """2026-11-17 이관 전수조사: 이관된 클론에 음란물 차단 목록·공동 차단 명단·오너 게임 난이도(80%)가 없어
    차단이 안 걸리고 게임은 '공정'으로 돌았음. 공용 목록은 통째로, 난이도는 클론에 값 없을 때만.
    뮤테이션: GLOBAL_ALL/GLOBAL_STATE 복사를 빼면 FAIL."""
    import sodam.fedban  # noqa: F401
    import sodam.nsfwguard  # noqa: F401
    tmp = tempfile.mkdtemp()
    srcp, dstp = os.path.join(tmp, "s.db"), os.path.join(tmp, "d.db")
    src, dst = await _open(srcp), await _open(dstp)
    A, OTHER = await _seed_src(src)
    await src._write("INSERT INTO nsfw_bad(file_uid, chat_id, score, ts) VALUES('bad1', ?, 0.9, 1)", (OTHER,))
    await src._write("INSERT INTO nsfw_set(set_name, ts) VALUES('pornpack', 1)")
    await src._write("INSERT INTO fedban_entries(user_id, name, ts, no) VALUES(555, '사기꾼', 1, 1)")
    await src.set_state(0, "casino_lose_pct", 80)
    await dst.close()
    await src.close()
    counts = clone_migrate._copy_room(srcp, dstp, A)
    assert counts["fedban_entries"] == 1 and counts["_global_state"] == 1, counts
    dst = await _open(dstp)
    try:
        assert await dst._one("SELECT 1 FROM nsfw_bad WHERE file_uid='bad1'")     # 딴 방에서 배운 것도 (공용)
        assert await dst._one("SELECT 1 FROM nsfw_set WHERE set_name='pornpack'")
        assert await dst._one("SELECT 1 FROM fedban_entries WHERE user_id=555")
        assert await dst.get_state(0, "casino_lose_pct", None) == 80
        await dst.set_state(0, "casino_lose_pct", 30)                          # 클론 오너가 바꾼 값은 다시 이관해도 안 덮음
    finally:
        await dst.close()
    clone_migrate._copy_room(srcp, dstp, A)
    dst = await _open(dstp)
    try:
        assert await dst.get_state(0, "casino_lose_pct", None) == 30
    finally:
        await dst.close()


class _Bot:
    """get_chat_member 결과를 (방/채널 → 상태)로 흉내 내는 가짜 봇."""
    def __init__(self, bid, where):
        self.id, self.where, self.left = bid, dict(where), []

    async def get_chat_member(self, chat, uid):
        from types import SimpleNamespace
        if chat not in self.where:
            raise RuntimeError("chat not found")
        return SimpleNamespace(status=self.where[chat])

    async def leave_chat(self, chat):
        self.left.append(chat)
        self.where[chat] = "left"


@test
async def main_leaves_room_only_after_clone_is_admin_in_room_and_channel():
    """넘기기 마무리 (2026-11-17 洪兴汇 본체·클론 둘이 같이 답함 → 오너가 손으로 뺌, '안 겹치게 마법사로').
    클론이 방·채널(구독 필수) 관리자일 때만 본체가 나감 — 클론 없이 비는 시간 0.
    뮤테이션: main_leave 의 ready 검사를 빼면 FAIL."""
    tmp = tempfile.mkdtemp()
    src = await _open(os.path.join(tmp, "s.db"))
    A, _ = await _seed_src(src)
    await src.set_setting(A, "subgate_mode", "on")
    await src.set_setting(A, "subgate_channel", "@nwch")
    main = _Bot(1, {A: "administrator", "@nwch": "administrator"})
    cl = _Bot(2, {A: "administrator"})                      # 채널엔 아직 없음
    chk = await clone_migrate.handover_check(main, cl, src, A)
    assert chk["channels"] == [("@nwch", "none")] and not chk["ready"], chk
    ok, _ = await clone_migrate.main_leave(main, cl, src, A)
    assert not ok and main.left == []                       # 채널 준비 전엔 안 나감
    cl.where["@nwch"] = "administrator"
    ok, note = await clone_migrate.main_leave(main, cl, src, A)
    assert ok and main.left == [A], note
    chk = await clone_migrate.handover_check(main, cl, src, A)
    assert chk["ready"] and not chk["main_in"]
    await src.close()


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
