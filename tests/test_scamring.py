"""🕸️ 사기 무리 탐지 (sodam/scamring.py · panels/scamring.py): python tests/run_all.py scamring"""
import asyncio
import time

from fake_llm import Room, tool_call
from fakes import FakeQuery, add_member, fake_user, runner
from test_sanction_multi import A, B, BOSS, ask

from sodam import menu, scamring as SR
from sodam.panels import scamring as P

test, run_all = runner()
C, D, E = fake_user(30, "사기3", "s3"), fake_user(31, "사기4", "s4"), fake_user(32, "평범", "nm")
OTHER = -100888                                             # 다른 방
WAL = "TXYZabcdefghijkmnopqrstuvwxyz12345"                  # TRON 모양 (34자)
PROMO = "🔥 VIP 리딩방 무료 입장 수익 보장 지금 바로 문의 주세요 선착순 마감 임박 🔥"


async def world():
    r = await Room().open(admins={BOSS.id}, settings={"captcha_enabled": False})
    for u in (BOSS, A, B, E):
        await r.join(u)
    await add_member(r.db, OTHER, C, joined=True)
    await r.db.ensure_chat(OTHER, "다른 방")
    SR.invalidate(r.db)
    return r


async def says(r, chat, user, text):
    await r.db.upsert_user(user)
    await r.db.log_message(chat, user.id, None, text)


async def dm(r, user, data):
    q = FakeQuery(user.id, user, data)
    await menu.on_callback(r.svc, r.bot, q, data.split(":")[1:])
    return q


@test
def build_links_strong_soft_and_ignores_noise():
    rows = [(1, f"입금 주소 {WAL} 로 보내세요"), (2, f"여기로 {WAL}"), (2, "초대 t.me/+AbCdEfGh123 들어와"),
            (3, "링크 t.me/+AbCdEfGh123"), (4, PROMO), (5, PROMO), (9, f"관리자 주소 {WAL}"),
            (6, "안녕하세요"), (7, "안녕하세요")]
    rows += [(100 + i, "공지 채널 t.me/+CommonNews99 확인") for i in range(SR.COMMON + 1)]   # 너무 흔한 값
    g = SR.build(rows, exclude={9}, flagged={3})
    ring = g[1]
    assert ring.members == {1, 2, 3} and ring.strong and ring.flagged == {3} and 9 not in g, ring
    assert {k for k, _, _ in ring.evidence} == {"wal", "inv"} and WAL not in str(ring.evidence)   # 값은 가림
    soft = g[4]
    assert soft.members == {4, 5} and not soft.strong and not soft.alertable          # 같은 글 하나로는 알림 X
    assert 6 not in g and 100 not in g                                                 # 짧은 글·흔한 값은 연결 안 함


@test
async def ban_alerts_admins_of_rooms_where_ring_remains_no_auto_ban():
    r = await world()
    await says(r, OTHER, E, f"입금은 {WAL} 여기로")
    for u, chat in ((A, r.CHAT), (B, r.CHAT), (C, OTHER)):
        await says(r, chat, u, f"입금은 {WAL} 여기로")
    await r.db.log_mod(r.CHAT, BOSS.id, E.id, "ban", "옛 밴")                           # 기능 전 밴 → 알림 대상 아님
    await P.on_tick(r.svc, r.bot)                                                      # 커서 시작 (옛 밴은 무시)
    assert not [c for c in r.bot.named("send_message") if "🕸️" in c[2]]
    await says(r, r.CHAT, E, "오늘 날씨 좋네요 다들 점심 맛있게 드세요 ㅎㅎ 저는 짜장면 먹었어요 여러분은요")
    await r.svc.mod.ban(r.bot, r.CHAT, A.id, BOSS.id, "사기")
    await P.on_tick(r.svc, r.bot)
    dms = [c for c in r.bot.named("send_message") if c[1] == BOSS.id and "🕸️" in c[2]]
    room1 = [c for c in dms if "대표님들 소통방" in c[2]]
    assert room1 and 'tg://user?id=%d' % B.id in room1[0][2] and "다른 방 1곳" in room1[0][2], dms
    assert "다른 방</b>" not in room1[0][2] and "tg://user?id=%d" % E.id not in room1[0][2]   # 다른 방 이름·무관한 사람 X
    assert [c[2] for c in r.bot.named("ban")] == [A.id], "알림만, 자동 밴 없음"
    assert await SR.present(r.db, r.CHAT, {A.id, B.id}) == [B.id]                     # 밴된 사람은 '지금 있음' 에서 빠짐
    before = len(r.bot.named("send_message"))
    await P.on_tick(r.svc, r.bot)                                                      # 같은 밴으로 두 번 알림 X
    assert len(r.bot.named("send_message")) == before


@test
async def panel_ban_rechecks_rights_skips_admins_and_runs_once():
    r = await world()
    await r.db._write("INSERT INTO chat_admins(chat_id, user_id) VALUES(?,?)", (r.CHAT, BOSS.id))   # 관리자 캐시
    for u in (A, B, BOSS):
        await says(r, r.CHAT, u, f"입금은 {WAL} 여기로")
    await says(r, r.CHAT, A, "초대 t.me/+AbCdEfGh123 들어와")
    q = await dm(r, A, f"m:rgl:{r.CHAT}")
    assert not q.edits or "🕸️" not in q.edits[-1]                                     # 멤버는 못 봄
    q = await dm(r, BOSS, f"m:rgl:{r.CHAT}")
    assert "🕸️" in q.edits[-1] and "2명" in q.edits[-1], q.edits                       # 관리자(BOSS) 글은 무리에서 빠짐
    q = await dm(r, A, f"m:rngx:{r.CHAT}:{A.id}")
    assert not r.bot.named("ban")
    q = await dm(r, BOSS, f"m:rngb:{r.CHAT}:{A.id}")
    assert "이 방에서 2명을 밴할까요" in q.edits[-1], q.edits
    r.svc.perms.can = lambda *a, **k: _false()                                          # 누를 때 권한 다시 확인
    q = await dm(r, BOSS, f"m:rngx:{r.CHAT}:{A.id}")
    assert not r.bot.named("ban") and "권한" in q.answers[-1][0], q.answers
    del r.svc.perms.can
    r.svc.perms.admins.add(B.id)                                                       # 그 사이 B 가 관리자가 됨 (캐시엔 없음)
    await asyncio.gather(dm(r, BOSS, f"m:rngx:{r.CHAT}:{A.id}"), dm(r, BOSS, f"m:rngx:{r.CHAT}:{A.id}"))   # 동시 연타
    assert [c[2] for c in r.bot.named("ban")] == [A.id], r.bot.named("ban")          # 한 번만 · 관리자는 절대 밴 안 함


@test
async def ai_tool_reads_ring_without_other_room_names():
    r = await world()
    for u, chat in ((A, r.CHAT), (B, r.CHAT), (C, OTHER)):
        await says(r, chat, u, f"입금은 {WAL} 여기로")
    res = await ask(r, BOSS, [tool_call("scam_ring", {"name": "캎이바라요"})])
    assert "무리 3명" in res[0] and "조이킨" in res[0] and "다른 방 1곳" in res[0] and "다른 방\"" not in res[0], res
    assert WAL not in res[0]
    res = await ask(r, BOSS, [tool_call("scam_ring", {"name": "평범"})])
    assert "무리가 없음" in res[0], res



async def _false():
    return False


_ = time
