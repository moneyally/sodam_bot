"""📝 멤버 노트 (sodam/notes.py · panels/notes.py). python tests/run_all.py notes

2026-11-22 경쟁조사 ②. 저장·#호출·중복·권한·한도 + 오탐(그냥 해시태그는 노트 아님)."""
from types import SimpleNamespace

from fakes import FakeBot, make_db, runner

from sodam import notes

test, run_all = runner()
CHAT = -1004444
A, B = 1, 2
SNAP = {"html": "오늘 규칙입니다", "kind": None, "file_id": None}


def svc_of(db):
    return SimpleNamespace(db=db)


def fmsg(text, uid=A):
    return SimpleNamespace(chat_id=CHAT, from_user=SimpleNamespace(id=uid),
                           message_id=100, text=text, caption=None)


@test
def find_hashes_only_real_tags():
    assert notes.find_hashes("#공지 봐") == ["공지"]
    assert notes.find_hashes("규칙은 #rule_1 이야") == ["rule_1"]
    assert notes.find_hashes("#a 그리고 #b 둘다") == ["a", "b"]
    assert notes.find_hashes("색은#FF0000 코드") == []       # 앞에 글자 붙음 → 해시 아님 (오탐 방지)
    assert notes.find_hashes("# 공백뒤") == []                # # 뒤 공백 → 이름 아님
    assert notes.find_hashes("그냥 평범한 대화") == []


@test
async def save_get_remove_and_dupe():
    db = await make_db()
    assert await notes.save(db, CHAT, "공지", by=A, snap=SNAP) == "ok"
    assert (await notes.get_snap(db, CHAT, "공지"))["html"] == "오늘 규칙입니다"
    assert "이미 있어" in await notes.save(db, CHAT, "공지", by=B, snap=SNAP)   # 중복 거절
    # 남(B)은 못 지움, 만든 사람(A) OK
    assert "만든 사람이나 관리자만" in await notes.remove(db, CHAT, "공지", by=B, is_admin=False)
    assert "🗑" in await notes.remove(db, CHAT, "공지", by=A, is_admin=False)
    assert await notes.get_snap(db, CHAT, "공지") is None
    # 관리자는 남이 만든 것도 지움
    await notes.save(db, CHAT, "짤", by=A, snap=SNAP)
    assert "🗑" in await notes.remove(db, CHAT, "짤", by=B, is_admin=True)


@test
async def invalid_key_and_limits():
    db = await make_db()
    assert "30자" in await notes.save(db, CHAT, "x" * 31, by=A, snap=SNAP)      # 너무 긺
    assert "30자" in await notes.save(db, CHAT, "빈 칸", by=A, snap=SNAP)        # 공백 포함 → 이름 아님
    orig = notes.MAX_PER_USER
    notes.MAX_PER_USER = 2
    try:
        assert await notes.save(db, CHAT, "a", by=A, snap=SNAP) == "ok"
        assert await notes.save(db, CHAT, "b", by=A, snap=SNAP) == "ok"
        assert "꽉 찼어" in await notes.save(db, CHAT, "c", by=A, snap=SNAP)     # 한 사람 한도
        assert await notes.save(db, CHAT, "d", by=B, snap=SNAP) == "ok"          # 다른 사람은 됨
    finally:
        notes.MAX_PER_USER = orig


@test
async def handle_reposts_only_registered_hash():
    db = await make_db()
    svc, bot = svc_of(db), FakeBot()
    notes._last_call.clear()
    await notes.save(db, CHAT, "공지", by=A, snap=SNAP)

    assert await notes.handle(svc, bot, fmsg("#공지"), "#공지") is True          # 등록된 노트 → 재전송·중단
    assert any("오늘 규칙" in str(c) for c in bot.calls), bot.calls               # 실제로 보냄

    assert await notes.handle(svc, bot, fmsg("#없는거"), "#없는거") is False      # 미등록 해시 → 계속(게임·AI)
    assert await notes.handle(svc, bot, fmsg("그냥 대화"), "그냥 대화") is False   # # 없음 → 계속


@test
async def call_cooldown_blocks_spam():
    db = await make_db()
    svc, bot = svc_of(db), FakeBot()
    notes._last_call.clear()
    await notes.save(db, CHAT, "공지", by=A, snap=SNAP)
    assert await notes.handle(svc, bot, fmsg("#공지", uid=A), "#공지") is True
    n = len(bot.calls)
    # 같은 사람 바로 또 → 쿨다운: 중단(True)이지만 다시 안 보냄
    assert await notes.handle(svc, bot, fmsg("#공지", uid=A), "#공지") is True
    assert len(bot.calls) == n, "쿨다운 중엔 재전송 안 함"
    # 다른 사람은 바로 됨
    assert await notes.handle(svc, bot, fmsg("#공지", uid=B), "#공지") is True
    assert len(bot.calls) > n
