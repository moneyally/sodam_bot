"""🏆 순위 고정 지정 (오너만, sodam/rankfix.py + chatrank.standings/my_place, 2026-10-27 오너 '일루왕 1~3등 지정').
집계는 그대로 두고 보일 때만 상단 고정 — 초활발방(하루 수천 개)에서 가짜 채팅 수십만 행 안 넣게.
python tests/run_all.py rankfix

뮤테이션:
 1) chatrank.standings 에서 `_apply_fixed` 호출 줄 빼기 → fixed_on_top FAIL
 2) rankfix.display_counts 에서 `v = real_max` → `v = 0` → counts_beat_real FAIL
 3) chatrank.my_place 에서 `rank += len(...)` 빼기 → real_user_pushed_down FAIL
 4) rankfix.get 에서 `until` 만료 검사 빼기 → expired_no_override FAIL
"""
import json
import time
from types import SimpleNamespace

from fakes import fake_user, make_db, make_svc, runner

import sodam.panels  # noqa: F401
from sodam import chatrank as cr, rankfix

test, run_all = runner()
ROOM = -100777
ADMIN = fake_user(91, "방장", "boss")
A = fake_user(92, "실제1등", "real1")      # 실제 채팅 많이 침 (고정 아님)
B_ = fake_user(93, "실제2등", "real2")
KA = fake_user(94, "카츄", "kachu")        # 고정 1등 (실제 채팅 적음)
JA = fake_user(95, "자이", "zai")          # 고정 2등
PI = fake_user(96, "피츄", "pichu")        # 고정 3등


async def setup():
    db = await make_db()
    svc = await make_svc(db, admins={ADMIN.id})
    await db.ensure_chat(ROOM, "일루왕")
    now = int(time.time())
    from sodam.util import day_start
    today0 = day_start(svc.cfg.tz, 0)
    mid = 1
    for u in (ADMIN, A, B_, KA, JA, PI):
        await db.upsert_user(u)
        await db._write("INSERT INTO members(chat_id, user_id, joined_at, last_seen) VALUES(?,?,?,?)", (ROOM, u.id, now - 9e5, now))
    # 실제 채팅: A=120, B_=80, 카츄/자이/피츄는 거의 안 침 (3개씩)
    for u, n in ((A, 120), (B_, 80), (KA, 3), (JA, 2), (PI, 1)):
        for i in range(n):
            mid += 1
            await db.log_message(ROOM, u.id, mid, f"글{i}", ts=max(today0, now - 600) + i % 120)
    return SimpleNamespace(svc=svc, db=db)


@test
async def fixed_on_top():
    """고정 지정하면 카츄>자이>피츄가 상단, 실제 1·2등은 그 아래."""
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    cfg = cr.Cfg(top=5)
    _, _, rows = await cr.standings(e.svc, ROOM, cfg)
    order = [r["user_id"] for r in rows]
    assert order[:3] == [KA.id, JA.id, PI.id], order
    assert A.id in order[3:] and B_.id in order[3:], order    # 실제 1·2등은 아래로 밀림
    assert order.index(A.id) < order.index(B_.id)             # 실제끼리 순서는 유지


@test
async def counts_beat_real_and_descend():
    """보이는 횟수: 고정 3명 전부 실제 1등(120)보다 크고 내림차순."""
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    _, _, rows = await cr.standings(e.svc, ROOM, cr.Cfg(top=5))
    ka, ja, pi = rows[0]["n"], rows[1]["n"], rows[2]["n"]
    assert ka > ja > pi > 120, (ka, ja, pi)                   # 실제 1등 120보다 다 위, 내림차순


@test
async def stable_within_day():
    """같은 날 두 번 불러도 보이는 횟수 같음 (30초 캐시 새로고침마다 안 튐)."""
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    cr._CACHE.clear()
    _, _, r1 = await cr.standings(e.svc, ROOM, cr.Cfg(top=5))
    cr._CACHE.clear()
    _, _, r2 = await cr.standings(e.svc, ROOM, cr.Cfg(top=5))
    assert [x["n"] for x in r1[:3]] == [x["n"] for x in r2[:3]]


@test
async def fixed_user_rank_and_real_user_pushed_down():
    """.나: 카츄는 1위, 실제 1등(A)은 고정 3명만큼 밀려 4위."""
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    cfg = cr.Cfg(top=5)
    kr, kn = await cr.my_place(e.svc, ROOM, KA.id, cfg)
    assert kr == 1 and kn > 120, (kr, kn)
    ar, an = await cr.my_place(e.svc, ROOM, A.id, cfg)
    assert ar == 1 + 3 and an == 120, (ar, an)                # 실제론 1위인데 고정 3명 아래 = 4위, 횟수는 실제값


@test
async def expired_no_override():
    """기간 지나면 override 없음 (실제 집계대로)."""
    e = await setup()
    past = int(time.time()) - 10
    await e.db.set_state(ROOM, rankfix.KEY, json.dumps({"ids": [KA.id, JA.id, PI.id], "until": past}))
    e.db.uncache(("rankfix", ROOM))
    assert await rankfix.get(e.db, ROOM) is None
    _, _, rows = await cr.standings(e.svc, ROOM, cr.Cfg(top=5))
    assert rows[0]["user_id"] == A.id                          # 실제 1등이 1위


@test
async def clear_turns_off():
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    await rankfix.clear(e.db, ROOM)
    assert await rankfix.get(e.db, ROOM) is None
    _, _, rows = await cr.standings(e.svc, ROOM, cr.Cfg(top=5))
    assert rows[0]["user_id"] == A.id


@test
async def award_record_uses_fixed():
    """자정 시상식 기록(rank_awards)도 고정 명단으로 (panels.chatrank.award 가 standings 를 타서)."""
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    cfg = cr.Cfg(top=3)
    _, _, rows = await cr.standings(e.svc, ROOM, cfg, "today")
    await cr.record_awards(e.db, ROOM, "2026-10-27", cfg, rows)
    got = await e.db._all("SELECT rank, user_id FROM rank_awards WHERE chat_id=? ORDER BY rank", (ROOM,))
    assert [(g["rank"], g["user_id"]) for g in got] == [(1, KA.id), (2, JA.id), (3, PI.id)]


@test
async def respects_top_limit():
    """top=3 이면 고정 3명으로 꽉 차 실제 사람은 안 보임 (고정이 top 보다 많아도 top 까지만)."""
    e = await setup()
    await rankfix.set_fixed(e.db, ROOM, [KA.id, JA.id, PI.id], 30)
    _, _, rows = await cr.standings(e.svc, ROOM, cr.Cfg(top=3))
    assert len(rows) == 3 and [r["user_id"] for r in rows] == [KA.id, JA.id, PI.id]


if __name__ == "__main__":
    run_all()
